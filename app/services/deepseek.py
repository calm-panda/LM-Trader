"""DeepSeek AI service — chart analysis, strategy code generation, news sentiment,
structured AI signal recommendations.

Uses the OpenAI-compatible DeepSeek API:
  deepseek-chat     → fast, cheap  (chart analysis, news sentiment, free chat, signals)
  deepseek-reasoner → chain-of-thought (strategy code generation)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal, Optional

import httpx
import pandas as pd
import pandas_ta as ta
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select

from app.config import get_settings
from app.database import async_session
from app.models.ohlcv import OHLCV

logger = logging.getLogger(__name__)

_CHAT_MODEL     = "deepseek-chat"
_REASONER_MODEL = "deepseek-reasoner"
_BASE_URL       = "https://api.deepseek.com"

_IST_OFFSET = 19800   # seconds (UTC+5:30)

_HIGHER_TF: dict[str, str] = {"1m": "5m", "5m": "15m", "15m": "30m", "30m": "1d"}


def _client() -> AsyncOpenAI:
    key = get_settings().deepseek_api_key
    if not key:
        raise ValueError("DEEPSEEK_API_KEY is not configured. Add it to your .env file.")
    return AsyncOpenAI(api_key=key, base_url=_BASE_URL, timeout=60.0)


def _last_val(s) -> Optional[float]:
    if s is None or (hasattr(s, "empty") and s.empty):
        return None
    v = s.dropna()
    return round(float(v.iloc[-1]), 2) if len(v) else None


def _col(df: pd.DataFrame | None, *candidates: str) -> Optional[pd.Series]:
    """Return the first matching column (exact, then prefix) from a DataFrame."""
    if df is None or df.empty:
        return None
    for name in candidates:
        if name in df.columns:
            return df[name]
    for name in candidates:
        for col in df.columns:
            if str(col).startswith(name):
                return df[col]
    return None


async def _fetch_ohlcv_df(symbol: str, timeframe: str, n: int = 50) -> pd.DataFrame:
    """Fetch and optionally resample OHLCV into a DataFrame with n bars."""
    db_tf = "1m" if timeframe in ("5m", "15m", "30m") else timeframe
    limit_map = {"5m": n * 5, "15m": n * 15, "30m": n * 30}
    limit = limit_map.get(timeframe, n)

    async with async_session() as db:
        result = await db.execute(
            select(OHLCV)
            .where(OHLCV.symbol == symbol, OHLCV.timeframe == db_tf)
            .order_by(OHLCV.timestamp.desc())
            .limit(limit)
        )
        rows = list(reversed(result.scalars().all()))

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(
        [{"open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume}
         for r in rows]
    )
    ts_idx = pd.to_datetime([r.timestamp for r in rows])

    if timeframe in ("5m", "15m", "30m"):
        rule = {"5m": "5min", "15m": "15min", "30m": "30min"}[timeframe]
        df.index = ts_idx
        df = (
            df.resample(rule, label="left", closed="left")
            .agg(
                open=("open", "first"), high=("high", "max"),
                low=("low", "min"), close=("close", "last"), volume=("volume", "sum"),
            )
            .dropna(subset=["open"])
            .tail(n)
            .reset_index(drop=True)
        )
    else:
        df = df.tail(n).reset_index(drop=True)

    return df


def _compute_indicators(df: pd.DataFrame) -> dict[str, Any]:
    close = df["close"]
    ema9 = ta.ema(close, 9)
    ema21 = ta.ema(close, 21)
    ema50 = ta.ema(close, 50)
    rsi = ta.rsi(close, 14)
    macd_df = ta.macd(close)
    bb_df = ta.bbands(close, 20, 2.0)
    atr = ta.atr(df["high"], df["low"], close, 14)
    # VWAP needs a DatetimeIndex; fall back to None if unavailable
    vwap_val = None
    try:
        if isinstance(df.index, pd.DatetimeIndex):
            vwap_val = _last_val(ta.vwap(df["high"], df["low"], close, df["volume"]))
    except Exception:
        vwap_val = None

    return {
        "ema9": _last_val(ema9),
        "ema21": _last_val(ema21),
        "ema50": _last_val(ema50),
        "rsi14": _last_val(rsi),
        "macd": _last_val(_col(macd_df, "MACD_12_26_9")),
        "macd_sig": _last_val(_col(macd_df, "MACDs_12_26_9")),
        "macd_hist": _last_val(_col(macd_df, "MACDh_12_26_9")),
        "bb_upper": _last_val(_col(bb_df, "BBU_20_2.0", "BBU_20_2")),
        "bb_mid": _last_val(_col(bb_df, "BBM_20_2.0", "BBM_20_2")),
        "bb_lower": _last_val(_col(bb_df, "BBL_20_2.0", "BBL_20_2")),
        "atr14": _last_val(atr),
        "vwap": vwap_val,
        "last_close": round(float(close.iloc[-1]), 2) if len(close) else None,
    }


def _support_resistance(df: pd.DataFrame, window: int = 5, n_levels: int = 3) -> dict[str, list[float]]:
    """Simple local-min/max clustering for support / resistance levels."""
    if len(df) < window * 2 + 1:
        return {"support": [], "resistance": []}

    lows = df["low"].values
    highs = df["high"].values
    supports: list[float] = []
    resistances: list[float] = []

    for i in range(window, len(df) - window):
        if lows[i] == min(lows[i - window : i + window + 1]):
            supports.append(float(lows[i]))
        if highs[i] == max(highs[i - window : i + window + 1]):
            resistances.append(float(highs[i]))

    def _cluster(levels: list[float], tol_pct: float = 0.3) -> list[float]:
        if not levels:
            return []
        levels = sorted(levels)
        clusters: list[list[float]] = [[levels[0]]]
        for lv in levels[1:]:
            if abs(lv - clusters[-1][-1]) / clusters[-1][-1] * 100 <= tol_pct:
                clusters[-1].append(lv)
            else:
                clusters.append([lv])
        means = [round(sum(c) / len(c), 2) for c in clusters]
        return means

    last = float(df["close"].iloc[-1])
    support = sorted([s for s in _cluster(supports) if s < last], reverse=True)[:n_levels]
    resistance = sorted([r for r in _cluster(resistances) if r > last])[:n_levels]
    return {"support": support, "resistance": resistance}


def _ema_trend(indicators: dict[str, Any]) -> str:
    e20 = indicators.get("ema21")  # use EMA21 as proxy for ~EMA20
    e50 = indicators.get("ema50")
    if e20 is None or e50 is None:
        return "unknown"
    if e20 > e50:
        return "bullish"
    if e20 < e50:
        return "bearish"
    return "neutral"


# ── Chart context builder ──────────────────────────────────────────────────────

async def _build_chart_context(symbol: str, timeframe: str, n: int = 30) -> dict[str, Any]:
    """Fetch last n candles + compute indicators; return a compact analysis dict."""
    df = await _fetch_ohlcv_df(symbol, timeframe, n=n)
    if df.empty:
        return {}

    indicators = _compute_indicators(df)
    # Drop extra keys not needed by analyze_chart
    chart_ind = {k: indicators[k] for k in (
        "ema9", "ema21", "ema50", "rsi14", "macd", "macd_sig", "macd_hist",
        "bb_upper", "bb_mid", "bb_lower",
    )}
    last = df.tail(20)
    candles = last[["open", "high", "low", "close", "volume"]].round(2).to_dict("records")
    return {"symbol": symbol, "timeframe": timeframe, "candles": candles, "indicators": chart_ind}


async def _build_signal_context(symbol: str, timeframe: str = "5m", n: int = 50) -> dict[str, Any]:
    """Richer context for structured signal generation: indicators + S/R + MTF confluence."""
    df = await _fetch_ohlcv_df(symbol, timeframe, n=n)
    if df.empty or len(df) < 30:
        return {}

    indicators = _compute_indicators(df)
    sr = _support_resistance(df)
    primary_trend = _ema_trend(indicators)

    # Multi-timeframe confluence
    higher_tf = _HIGHER_TF.get(timeframe)
    mtf: dict[str, Any] = {"higher_timeframe": higher_tf, "higher_trend": "unknown", "confluence": False}
    if higher_tf:
        hdf = await _fetch_ohlcv_df(symbol, higher_tf, n=60)
        if not hdf.empty and len(hdf) >= 50:
            hind = _compute_indicators(hdf)
            higher_trend = _ema_trend(hind)
            mtf["higher_trend"] = higher_trend
            mtf["higher_ema21"] = hind.get("ema21")
            mtf["higher_ema50"] = hind.get("ema50")
            mtf["confluence"] = (
                primary_trend == higher_trend and primary_trend in ("bullish", "bearish")
            )

    last = df.tail(20)
    candles = last[["open", "high", "low", "close", "volume"]].round(2).to_dict("records")
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "candles": candles,
        "indicators": indicators,
        "support_resistance": sr,
        "primary_trend": primary_trend,
        "mtf": mtf,
    }


# ── 1. Chart Analysis ──────────────────────────────────────────────────────────

async def analyze_chart(symbol: str, timeframe: str) -> str:
    """Return a markdown analysis of the chart using deepseek-chat."""
    ctx = await _build_chart_context(symbol, timeframe, n=30)
    if not ctx:
        return f"No OHLCV data found for {symbol}/{timeframe}. Please ensure data is loaded."

    ind = ctx["indicators"]
    candles_text = "\n".join(
        f"  open={c['open']} high={c['high']} low={c['low']} close={c['close']} vol={c['volume']}"
        for c in ctx["candles"]
    )

    prompt = f"""You are an expert NSE equity trader and technical analyst.

Analyze the following market data for **{symbol}** on **{timeframe}** timeframe.

**Recent {len(ctx['candles'])} candles (oldest → newest):**
{candles_text}

**Current indicator values:**
- EMA 9:  {ind['ema9']}    EMA 21: {ind['ema21']}    EMA 50: {ind['ema50']}
- RSI 14: {ind['rsi14']}
- MACD: {ind['macd']}  Signal: {ind['macd_sig']}  Histogram: {ind['macd_hist']}
- Bollinger Bands — Upper: {ind['bb_upper']}  Mid: {ind['bb_mid']}  Lower: {ind['bb_lower']}

Provide a **concise** analysis (4–6 bullet points) covering:
1. Trend direction and strength
2. Key support / resistance levels visible in the candles
3. Indicator confluence or divergence
4. Suggested signal (BUY / SELL / HOLD) with 1-line reasoning
5. Suggested stop-loss and target levels

Be specific and practical. Use ₹ for price levels. Keep total response under 250 words."""

    client = _client()
    resp = await client.chat.completions.create(
        model=_CHAT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
        temperature=0.3,
    )
    return resp.choices[0].message.content or ""


# ── 2. Strategy Code Generation ───────────────────────────────────────────────

_STRATEGY_SYSTEM = """\
You are an expert algorithmic trader who writes clean Python strategy code for NSE equity markets.

The user will describe a trading strategy in plain English. You must generate a Python class
that fits the following interface exactly:

```python
# Available in scope (do NOT re-import):
#   pandas as pd, pandas_ta as ta
#   from .base import BaseStrategy, Signal

class MyStrategy(BaseStrategy):
    NAME = "MY_STRATEGY"  # UPPERCASE_UNDERSCORE slug

    @classmethod
    def default_params(cls) -> dict:
        return {}   # sensible numeric defaults

    def on_tick(self, symbol: str, df: pd.DataFrame) -> Signal:
        # df columns: open, high, low, close, volume (ascending time, int index)
        # Return Signal.BUY, Signal.SELL, or Signal.HOLD
        ...
```

Rules:
- Only use pandas-ta for indicators (ta.ema, ta.rsi, ta.macd, ta.bbands, ta.supertrend, etc.)
- Always call self._check_min_rows(df, N, symbol) and return Signal.HOLD if False
- Handle NaN values explicitly
- Return ONLY the class definition — no imports, no main, no explanation outside the code block
"""

async def generate_strategy(description: str) -> str:
    """Use deepseek-reasoner to generate a BaseStrategy subclass from plain English."""
    client = _client()
    resp = await client.chat.completions.create(
        model=_REASONER_MODEL,
        messages=[
            {"role": "system", "content": _STRATEGY_SYSTEM},
            {"role": "user",   "content": f"Strategy description: {description}"},
        ],
        max_tokens=1500,
        temperature=0.0,
    )
    content = resp.choices[0].message.content or ""
    # deepseek-reasoner may include <think> blocks — strip them
    if "<think>" in content and "</think>" in content:
        start = content.find("</think>") + len("</think>")
        content = content[start:].strip()
    return content


# ── 3. News Sentiment ─────────────────────────────────────────────────────────

async def _fetch_headlines(symbol: str) -> list[str]:
    """Fetch recent news headlines for the symbol via NewsAPI (if key present)."""
    key = get_settings().newsapi_key
    if not key:
        return []

    url = "https://newsapi.org/v2/everything"
    params = {
        "q":        f"{symbol} NSE stock",
        "language": "en",
        "pageSize": 8,
        "sortBy":   "publishedAt",
        "apiKey":   key,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(url, params=params)
            r.raise_for_status()
            articles = r.json().get("articles", [])
            return [a["title"] for a in articles if a.get("title")]
    except Exception as exc:
        logger.warning("NewsAPI fetch failed for %s: %s", symbol, exc)
        return []


async def analyze_sentiment(symbol: str) -> dict[str, Any]:
    """Return sentiment analysis: score (bullish/bearish/neutral), summary, headlines."""
    headlines = await _fetch_headlines(symbol)
    has_live_news = bool(headlines)
    headlines_text = (
        "\n".join(f"- {h}" for h in headlines)
        if headlines
        else "(No live news headlines available — using general market knowledge)"
    )

    prompt = f"""You are a financial analyst specializing in Indian equities (NSE/BSE).

Analyze the market sentiment for **{symbol}** based on the following:

**Recent news headlines:**
{headlines_text}

Return a JSON object with exactly these fields:
{{
  "sentiment": "bullish" | "bearish" | "neutral",
  "score": <integer from -100 (very bearish) to +100 (very bullish)>,
  "summary": "<2–3 sentence reasoning>",
  "key_factors": ["<factor 1>", "<factor 2>", "<factor 3>"]
}}

Return ONLY valid JSON, no markdown fences."""

    client = _client()
    resp = await client.chat.completions.create(
        model=_CHAT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300,
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {"sentiment": "neutral", "score": 0, "summary": raw, "key_factors": []}
    result["symbol"] = symbol
    result["live_news"] = has_live_news
    result["headlines"] = headlines
    return result


# ── 4. Free-form chat ─────────────────────────────────────────────────────────

_CHAT_SYSTEM = """\
You are an expert algorithmic trader and financial analyst focused on Indian equity markets (NSE/BSE).
You help users with: market analysis, trading strategies, risk management, Python strategy code, and portfolio concepts.
Keep answers concise and practical. Use ₹ for prices. Reference NSE/BSE context where relevant."""

async def chat(messages: list[dict[str, str]]) -> str:
    """Send a conversation to deepseek-chat and return the assistant reply."""
    client = _client()
    full_messages = [{"role": "system", "content": _CHAT_SYSTEM}] + messages
    resp = await client.chat.completions.create(
        model=_CHAT_MODEL,
        messages=full_messages,
        max_tokens=800,
        temperature=0.4,
    )
    return resp.choices[0].message.content or ""


# ── 5. Structured AI signal generation ────────────────────────────────────────

class SignalContract(BaseModel):
    """Validated LLM output for a trading recommendation."""

    symbol: str
    direction: Literal["BUY", "SELL"]
    confidence: float = Field(ge=0.0, le=1.0)
    entry: float
    stop_loss: float
    target: float
    risk_reward_ratio: float
    indicators_triggered: list[str]
    reasoning: str
    invalidation: str

    @field_validator("symbol")
    @classmethod
    def _upper_symbol(cls, v: str) -> str:
        return v.strip().upper()


_SIGNAL_SYSTEM = """\
You are an NSE/BSE intraday technical analyst. Synthesize ONLY from the indicator
snapshot provided — never invent indicator values that were not given.

Return a single JSON object with exactly these fields:
{
  "symbol": "<SYMBOL>",
  "direction": "BUY" | "SELL",
  "confidence": <float 0-1>,
  "entry": <float>,
  "stop_loss": <float>,
  "target": <float>,
  "risk_reward_ratio": <float>,
  "indicators_triggered": ["<reason 1>", "<reason 2>", ...],
  "reasoning": "<short plain-English paragraph>",
  "invalidation": "<what would prove this setup wrong>"
}

Rules:
- If indicators conflict, volume is thin, or the setup is unclear, set confidence below 0.55
  (the caller will discard low-confidence signals).
- Prefer setups where primary timeframe trend agrees with the higher-timeframe trend (confluence).
- risk_reward_ratio = |target - entry| / |entry - stop_loss|; require roughly >= 1.5 when possible.
- Do NOT invent candles or indicators. Use only the provided data.
- Return ONLY valid JSON, no markdown fences.
"""


async def generate_signal(symbol: str, timeframe: str = "5m") -> dict[str, Any] | None:
    """Ask DeepSeek for a structured BUY/SELL recommendation.

    Returns a validated dict on success, or None (fail-closed) when:
    - OHLCV/indicator context is missing/stale
    - LLM response fails schema validation after one retry
    - confidence is below the configured threshold
    """
    ctx = await _build_signal_context(symbol, timeframe)
    if not ctx:
        logger.info("generate_signal(%s/%s): no context — skip", symbol, timeframe)
        return None

    ind = ctx["indicators"]
    if ind.get("last_close") is None or ind.get("rsi14") is None:
        logger.info("generate_signal(%s/%s): stale/incomplete indicators — skip", symbol, timeframe)
        return None

    sr = ctx["support_resistance"]
    mtf = ctx["mtf"]
    candles_text = "\n".join(
        f"  o={c['open']} h={c['high']} l={c['low']} c={c['close']} v={c['volume']}"
        for c in ctx["candles"][-12:]
    )

    user_prompt = f"""Symbol: {symbol}
Timeframe: {timeframe}
Primary trend (EMA21 vs EMA50): {ctx['primary_trend']}
Higher TF ({mtf.get('higher_timeframe')}): trend={mtf.get('higher_trend')} confluence={mtf.get('confluence')}

Indicators (latest bar):
  last_close={ind.get('last_close')} ema9={ind.get('ema9')} ema21={ind.get('ema21')} ema50={ind.get('ema50')}
  rsi14={ind.get('rsi14')} macd={ind.get('macd')} macd_sig={ind.get('macd_sig')} macd_hist={ind.get('macd_hist')}
  bb_upper={ind.get('bb_upper')} bb_mid={ind.get('bb_mid')} bb_lower={ind.get('bb_lower')}
  atr14={ind.get('atr14')} vwap={ind.get('vwap')}

Support levels: {sr.get('support')}
Resistance levels: {sr.get('resistance')}

Recent candles (oldest → newest):
{candles_text}

Produce the JSON signal contract now."""

    client = _client()
    threshold = get_settings().signal_confidence_threshold

    async def _call() -> dict[str, Any] | None:
        resp = await client.chat.completions.create(
            model=_CHAT_MODEL,
            messages=[
                {"role": "system", "content": _SIGNAL_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=600,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("generate_signal(%s): invalid JSON", symbol)
            return None
        try:
            contract = SignalContract.model_validate(data)
        except ValidationError as exc:
            logger.warning("generate_signal(%s): schema mismatch: %s", symbol, exc)
            return None
        return contract.model_dump()

    result = await _call()
    if result is None:
        result = await _call()  # one retry
    if result is None:
        return None

    if result["confidence"] < threshold:
        logger.info(
            "generate_signal(%s): confidence %.2f < %.2f — discard",
            symbol, result["confidence"], threshold,
        )
        return None

    # Attach snapshot for persistence
    result["indicators_snapshot"] = {
        "indicators": ind,
        "support_resistance": sr,
        "primary_trend": ctx["primary_trend"],
        "mtf": mtf,
    }
    result["timeframe"] = timeframe
    result["model_used"] = _CHAT_MODEL
    return result
