from typing import Optional, Tuple

import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.auth_token import AuthToken

UPSTOX_AUTH_URL = "https://api.upstox.com/v2/login/authorization/dialog"
UPSTOX_TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
UPSTOX_PROFILE_URL = "https://api.upstox.com/v2/user/profile"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; treat them as UTC for comparisons."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class UpstoxAuthService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.settings = get_settings()

    def get_login_url(self, state: Optional[str] = None) -> Tuple[str, str]:
        """Return (authorization_url, state) for redirect."""
        state = state or secrets.token_urlsafe(32)
        params = {
            "client_id": self.settings.upstox_api_key,
            "redirect_uri": self.settings.upstox_redirect_uri,
            "response_type": "code",
            "state": state,
        }
        url = f"{UPSTOX_AUTH_URL}?{urlencode(params)}"
        return url, state

    async def exchange_code(self, code: str) -> AuthToken:
        """Exchange authorization code for access token and persist."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                UPSTOX_TOKEN_URL,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "code": code,
                    "client_id": self.settings.upstox_api_key,
                    "client_secret": self.settings.upstox_api_secret,
                    "redirect_uri": self.settings.upstox_redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            response.raise_for_status()
            token_data = response.json()

            access_token = token_data["access_token"]
            profile = await self._fetch_profile(client, access_token)

        expires_at = _utc_now() + timedelta(hours=24)

        # Upsert: keep only the latest token
        result = await self.db.execute(select(AuthToken).limit(1))
        existing = result.scalar_one_or_none()

        if existing:
            existing.access_token = access_token
            existing.user_id = profile.get("user_id")
            existing.user_name = profile.get("user_name")
            existing.expires_at = expires_at
            token = existing
        else:
            token = AuthToken(
                access_token=access_token,
                user_id=profile.get("user_id"),
                user_name=profile.get("user_name"),
                expires_at=expires_at,
            )
            self.db.add(token)

        await self.db.commit()
        await self.db.refresh(token)
        return token

    async def _fetch_profile(self, client: httpx.AsyncClient, access_token: str) -> dict:
        response = await client.get(
            UPSTOX_PROFILE_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        if response.status_code == 200:
            data = response.json()
            return data.get("data", {})
        return {}

    async def get_token(self) -> Optional[AuthToken]:
        result = await self.db.execute(
            select(AuthToken).order_by(AuthToken.updated_at.desc()).limit(1)
        )
        return result.scalar_one_or_none()

    async def is_authenticated(self) -> bool:
        token = await self.get_token()
        if not token:
            return False
        if token.expires_at and _as_utc(token.expires_at) < _utc_now():
            return False
        return True

    async def get_access_token(self) -> Optional[str]:
        token = await self.get_token()
        if not token or not await self.is_authenticated():
            return None
        return token.access_token
