"""Headless TOTP-based Upstox token refresh.

How it works
------------
Upstox access tokens expire at midnight IST every day.  This module
implements a headless login flow that simulates the browser steps:

  1. Initiate the OAuth dialog (GET) — establishes a session cookie.
  2. POST mobile number → receive a session identifier.
  3. POST 6-digit PIN   → advance to the 2FA step.
  4. POST TOTP code     → server redirects to redirect_uri?code=XXXX.
  5. Exchange the one-time code for a fresh access_token (same as manual login).

Prerequisites
-------------
Add these three lines to your .env:

  UPSTOX_MOBILE=9876543210          # 10-digit registered mobile
  UPSTOX_PIN=123456                 # 6-digit Upstox trading PIN
  UPSTOX_TOTP_SECRET=JBSWY3DPEHPK  # base32 secret shown in Upstox 2FA setup

If any of these are absent, auto_refresh_token() logs a warning and
returns False — the operator must log in manually via /auth/login.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlparse

import httpx
import pyotp

from app.config import get_settings

logger = logging.getLogger(__name__)

_DIALOG_URL = "https://api.upstox.com/v2/login/authorization/dialog"
_TOKEN_URL  = "https://api.upstox.com/v2/login/authorization/token"

# Headers that mimic a real browser session
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux aarch64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Origin": "https://api.upstox.com",
    "Referer": "https://api.upstox.com/",
}


def _is_configured() -> bool:
    s = get_settings()
    return bool(s.upstox_mobile and s.upstox_pin and s.upstox_totp_secret)


async def auto_refresh_token() -> bool:
    """Attempt a headless TOTP login and persist the new token to the DB.

    Returns True on success, False on any failure (credentials missing,
    wrong PIN/TOTP, network error, or Upstox API change).
    """
    if not _is_configured():
        logger.warning(
            "Auto token refresh skipped — UPSTOX_MOBILE / UPSTOX_PIN / "
            "UPSTOX_TOTP_SECRET not set in .env.  "
            "Log in manually at https://<pi>:8000/auth/login before 09:15."
        )
        return False

    cfg = get_settings()

    try:
        auth_code = await _headless_login(cfg)
    except Exception:
        logger.exception("Headless login failed — manual login required")
        return False

    if not auth_code:
        logger.error("Headless login returned no auth code — manual login required")
        return False

    # Exchange code for token (reuse the existing auth service logic)
    try:
        from app.database import async_session
        from app.services.upstox.auth import UpstoxAuthService
        async with async_session() as db:
            svc = UpstoxAuthService(db)
            token = await svc.exchange_code(auth_code)
        logger.info(
            "Auto token refresh successful — user=%s expires=%s",
            token.user_name,
            token.expires_at,
        )
        return True
    except Exception:
        logger.exception("Token exchange after headless login failed")
        return False


async def _headless_login(cfg) -> str | None:
    """Run the Upstox browser-login flow headlessly; return the OAuth auth code."""

    params = {
        "client_id":     cfg.upstox_api_key,
        "redirect_uri":  cfg.upstox_redirect_uri,
        "response_type": "code",
        "state":         "autologin",
    }

    async with httpx.AsyncClient(
        headers=_BROWSER_HEADERS,
        follow_redirects=False,
        timeout=20.0,
    ) as client:

        # ── Step 1: Initiate dialog → establish session cookie ────────────────
        r1 = await client.get(_DIALOG_URL, params=params)
        logger.debug("Step 1 status: %s", r1.status_code)

        # ── Step 2: POST mobile number ────────────────────────────────────────
        r2 = await client.post(
            _DIALOG_URL,
            params=params,
            json={"mobile_number": cfg.upstox_mobile},
        )
        logger.debug("Step 2 (mobile) status: %s  body: %s", r2.status_code, r2.text[:200])
        _raise_for_login_error(r2, "mobile POST")

        # ── Step 3: POST PIN ──────────────────────────────────────────────────
        r3 = await client.post(
            _DIALOG_URL,
            params=params,
            json={"pin": cfg.upstox_pin},
        )
        logger.debug("Step 3 (PIN) status: %s  body: %s", r3.status_code, r3.text[:200])
        _raise_for_login_error(r3, "PIN POST")

        # ── Step 4: POST TOTP ─────────────────────────────────────────────────
        totp_code = pyotp.TOTP(cfg.upstox_totp_secret).now()
        r4 = await client.post(
            _DIALOG_URL,
            params=params,
            json={"otp": totp_code},
        )
        logger.debug("Step 4 (TOTP) status: %s  headers: %s", r4.status_code, dict(r4.headers))

        # Success: Upstox issues a 302 to redirect_uri?code=XXXX
        if r4.status_code in (301, 302, 303):
            location = r4.headers.get("location", "")
            return _extract_code(location)

        # Some versions return 200 with a redirect_url in JSON
        if r4.status_code == 200:
            data = r4.json()
            redirect_url = (
                data.get("data", {}).get("redirect_url")
                or data.get("redirect_url")
                or data.get("url")
                or ""
            )
            if redirect_url:
                return _extract_code(redirect_url)
            logger.error("TOTP POST returned 200 but no redirect URL: %s", r4.text[:400])
            return None

        logger.error("Unexpected TOTP response %s: %s", r4.status_code, r4.text[:400])
        return None


def _raise_for_login_error(response: httpx.Response, step: str) -> None:
    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"Headless login failed at {step}: "
            f"HTTP {response.status_code} — {response.text[:200]}"
        )


def _extract_code(url: str) -> str | None:
    """Parse the authorization code from a redirect URL."""
    if not url:
        return None
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    codes = qs.get("code", [])
    if codes:
        return codes[0]
    logger.error("No 'code' parameter in redirect URL: %s", url)
    return None
