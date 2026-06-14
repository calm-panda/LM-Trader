"""DeepSeek AI service — chart analysis, strategy code generation, news sentiment.

Uses the OpenAI-compatible DeepSeek API:
  deepseek-chat     → fast, cheap  (chart analysis, news sentiment, free chat)
  deepseek-reasoner → chain-of-thought (strategy code generation)
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pandas as pd
import pandas_ta as ta
from openai import AsyncOpenAI
from sqlalchemy import select

from app.config import get_settings
from app.database import async_session
from app.models.ohlcv import OHLCV

logger = logging.getLogger(__name__)

_CHAT_MODEL     = "deepseek-chat"
_REASONER_MODEL = "deepseek-reasoner"
_BASE_URL       = "https://api.deepseek.com"

_IST_OFFSET = 19800   # seconds (UTC+5:30)


def _client() -> AsyncOpenAI:
    key = get_settings().deepseek_api_key
    if not key:
        raise ValueError("DEEPSEEK_API_KEY is not configured. Add it to your .env file.")
    return AsyncOpenAI(api_key=key, base_url=_BASE_URL, timeout=60.0)


# ── Chart context builder ──────────────────────────────────────────────────────

async def _build_chart_context(symbol: str, timeframe: str, n: int = 30) -> dict[str, Any]:
    """Fetch last n candles + compute indicators; return a compact analysis dict."""
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
        return {}

    df = pd.DataFrame(
        [{"open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume}
         for r in rows]
    )

    # Resample if needed
    if timeframe in ("5m", "15m", "30m") and rows:
        rule = {"5m": "5min", "15m": "15min", "30m": "30min"}[timeframe]
        ts_idx = pd.to_datetime([r.timestamp for r in rows])
        df.index = ts_idx
        df = df.resample(rule, label="left", closed="left").agg(
            open=("open", "first"), high=("high", "max"),
            low=("low", "min"), close=("close", "last"), volume=("volume", "sum"),
        ).dropna(subset=["open"]).tail(n)
        df = df.reset_index(drop=True)

    close = df["close"]
    last = df.tail(20)

    # Indicator values (last bar)
    ema9  = ta.ema(close, 9)
    ema21 = ta.ema(close, 21)
    ema50 = ta.ema(close, 50)
    rsi   = ta.rsi(close, 14)
    macd_df = ta.macd(close)
    bb_df   = ta.bbands(close, 20, 2.0)

    def _last(s):
        if s is None or s.empty:
            return None
        v = s.dropna()
        return round(float(v.iloc[-1]), 2) if len(v) else None

    indicators = {
        "ema9":      _last(ema9),
        "ema21":     _last(ema21),
        "ema50":     _last(ema50),
        "rsi14":     _last(rsi),
        "macd":      _last(macd_df["MACD_12_26_9"])      if macd_df is not None else None,
        "macd_sig":  _last(macd_df["MACDs_12_26_9"])     if macd_df is not None else None,
        "macd_hist": _last(macd_df["MACDh_12_26_9"])     if macd_df is not None else None,
        "bb_upper":  _last(bb_df["BBU_20_2.0"])          if bb_df is not None else None,
        "bb_mid":    _last(bb_df["BBM_20_2.0"])          if bb_df is not None else None,
        "bb_lower":  _last(bb_df["BBL_20_2.0"])          if bb_df is not None else None,
    }

    candles = last[["open", "high", "low", "close", "volume"]].round(2).to_dict("records")
    return {"symbol": symbol, "timeframe": timeframe, "candles": candles, "indicators": indicators}


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
