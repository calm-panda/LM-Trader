"""Lightweight alerting — log file + optional webhook.

Used for token failures, risk-gate blocks, and EOD square-off notices.
Keeps Pi deploy simple: no external deps beyond httpx (already in stack).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


def _alerts_path() -> Path:
    return get_settings().data_dir / "alerts.log"


def _append_local(level: str, title: str, detail: str) -> None:
    path = _alerts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] [{level}] {title} — {detail}\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)


async def send_alert(title: str, detail: str = "", *, level: str = "WARNING") -> None:
    """Persist an alert locally and POST to ALERT_WEBHOOK_URL if configured."""
    level = level.upper()
    try:
        _append_local(level, title, detail)
    except Exception:
        logger.exception("alert: failed to write alerts.log")

    log_fn = logger.error if level in ("CRITICAL", "ERROR") else logger.warning
    log_fn("ALERT: %s — %s", title, detail)

    webhook = get_settings().alert_webhook_url.strip()
    if not webhook:
        return

    payload = {
        "text": f"[LM-Trader] [{level}] {title}\n{detail}".strip(),
        "title": title,
        "level": level,
        "detail": detail,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(webhook, json=payload)
            if resp.status_code >= 400:
                logger.warning(
                    "alert webhook HTTP %s: %s", resp.status_code, resp.text[:200]
                )
    except Exception:
        logger.exception("alert: webhook POST failed")
