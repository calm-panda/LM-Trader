# LM Trader

AI-assisted NSE/BSE trading app for Raspberry Pi — Upstox API, DeepSeek AI, HTMX + Jinja2 UI, paper-first automated trading.

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
├── models/              # SQLite schema (trades, portfolio, OHLCV, strategies, paper_orders)
├── routers/             # HTTP routes (dashboard, chart, auth, …)
├── services/            # Business logic (Upstox, tick store, …)
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
| `LIVE_TRADING_ENABLED` | `false` by default — paper mode only |

## Build Progress

See `.cursor/base_plan.md` for the full architecture and remaining tasks.

- [x] Project scaffold (FastAPI + Jinja2 + SQLite)
- [x] Upstox OAuth2 login flow + token storage
- [ ] Data pipeline (OHLCV + WebSocket ticks)
- [ ] Chart UI with Lightweight Charts
- [ ] Paper trading engine
- [ ] Strategy engine + APScheduler
- [ ] Backtester integration
- [ ] DeepSeek AI integration
- [ ] Portfolio dashboard
- [ ] Live trading graduation + deployment hardening

## Raspberry Pi Deployment

```bash
sudo apt update && sudo apt install python3.11 python3.11-venv python3-pip nginx -y
python3.11 -m venv ~/trading-env
source ~/trading-env/bin/activate
pip install -r requirements.txt
```

Set `UPSTOX_REDIRECT_URI=http://<pi-ip>:8000/auth/callback` in `.env`.
