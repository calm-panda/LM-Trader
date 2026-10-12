# LM Trader

AI-assisted NSE/BSE trading app for Raspberry Pi — Upstox API, DeepSeek AI, HTMX + Jinja2 UI.

**Hybrid model:** automated paper trading for practice; live Upstox orders only after you Confirm an AI signal.

## Quick Start (Development)

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your Upstox and DeepSeek API keys
python run.py
```

Open [http://localhost:8000](http://localhost:8000).

## Project Structure

```
app/
├── main.py              # FastAPI entry point
├── config.py            # Settings from .env
├── database.py          # SQLAlchemy + aiosqlite
├── models/              # SQLite schema (OHLCV, strategies, paper_orders, signals, trades, …)
├── routers/             # HTTP routes (dashboard, chart, signals, auth, …)
├── services/            # Business logic (Upstox, DeepSeek, paper engine, scheduler, …)
└── templates/           # Jinja2 + HTMX pages
static/css/              # Minimal custom styles
data/                    # SQLite database (created at runtime)
```

## Configuration

Copy `.env.example` to `.env` and set:

| Variable | Description |
|----------|-------------|
| `UPSTOX_API_KEY` | From [developer.upstox.com](https://developer.upstox.com) |
| `UPSTOX_API_SECRET` | App secret |
| `UPSTOX_REDIRECT_URI` | Must match app config, e.g. `http://localhost:8000/auth/callback` |
| `DEEPSEEK_API_KEY` | From [platform.deepseek.com](https://platform.deepseek.com) |
| `LIVE_TRADING_ENABLED` | `false` by default — Confirm still required for every live order |
| `SIGNAL_VALIDITY_MINUTES` | How long a pending AI signal stays confirmable (default 15) |
| `SIGNAL_GENERATION_INTERVAL_MINUTES` | Watchlist scan interval (default 5) |
| `SIGNAL_CONFIDENCE_THRESHOLD` | Discard AI signals below this confidence (default 0.55) |

## Build Progress

See `base_plan.md` for the original architecture and `overhead_plan.md` for the human-in-the-loop Phase 2 design.

- [x] Project scaffold (FastAPI + Jinja2 + SQLite)
- [x] Upstox OAuth2 login flow + token storage
- [x] Data pipeline (OHLCV + WebSocket ticks)
- [x] Chart UI with Lightweight Charts
- [x] Paper trading engine
- [x] Strategy engine + APScheduler
- [x] Backtester integration
- [x] DeepSeek AI integration
- [x] AI signal confirmation gate (structured recommendations → Confirm/Reject → live order)
- [x] Portfolio dashboard (paper + live sync + trade journal + win-rate / R:R analytics)
- [x] systemd unit + alerting (`data/alerts.log` + optional webhook)
- [x] Live EOD HITL default (notify-only; opt-in `LIVE_AUTO_SQUAREOFF`)

## Raspberry Pi Deployment

```bash
sudo apt update && sudo apt install python3.11 python3.11-venv python3-pip nginx -y
python3.11 -m venv ~/trading-env
source ~/trading-env/bin/activate
pip install -r requirements.txt
```

Set `UPSTOX_REDIRECT_URI=http://<pi-ip>:8000/auth/callback` in `.env`.

### systemd (production)

```bash
sudo bash scripts/setup_systemd.sh
systemctl status lm-trader
journalctl -u lm-trader -f
# Alerts also land in data/alerts.log (optional ALERT_WEBHOOK_URL)
```
