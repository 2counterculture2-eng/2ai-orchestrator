"""
alpaca_client.py v1
Alpaca paper trading client using internal API + Cognito auth.
No traditional API keys required — uses email/password/TOTP via AWS Cognito.
"""
import asyncio
import base64
import hashlib
import hmac
import logging
import re
import struct
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

COGNITO_CLIENT_ID = "3tgca6mnp6g138dbkcs7lq7j0a"
COGNITO_REGION = "us-east-1"
INTERNAL_BASE = "https://app.alpaca.markets/internal"
TOKEN_TTL = 3300  # refresh 5 min before 1h expiry


def _gen_totp(secret: str) -> str:
    s = re.sub(r"[^A-Z2-7]", "", secret.upper())
    pad = (8 - len(s) % 8) % 8
    s += "=" * pad
    key = base64.b32decode(s)
    t = int(time.time()) // 30
    msg = struct.pack(">Q", t)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = struct.unpack(">I", h[o : o + 4])[0] & 0x7FFFFFFF
    return str(code % 1_000_000).zfill(6)


COGNITO_POOL_ID = "us-east-1_CZEBlNVuv"
AUTHX_URL = "https://authx.alpaca.markets/v1/oauth2/token"


def _sync_srp_auth(email: str, password: str, mfa_secret: str) -> str:
    """SRP-based Cognito auth via pycognito — same flow as Alpaca browser app."""
    from pycognito import Cognito
    from pycognito.exceptions import SoftwareTokenMFAChallengeException

    # Wait for a safe TOTP window (not within 3s of expiry)
    secs = int(time.time()) % 30
    if secs > 27:
        time.sleep(33 - secs)

    user = Cognito(COGNITO_POOL_ID, COGNITO_CLIENT_ID, username=email)
    try:
        user.authenticate(password=password)
    except SoftwareTokenMFAChallengeException:
        totp = _gen_totp(mfa_secret)
        user.respond_to_software_token_mfa_challenge(totp)
    return user.id_token


def _sync_cognito_auth(email: str, password: str, mfa_secret: str) -> str:
    """Alias kept for compatibility — delegates to SRP auth."""
    return _sync_srp_auth(email, password, mfa_secret)


DATA_BASE = "https://data.alpaca.markets/v2/stocks"
PAPER_BASE = "https://paper-api.alpaca.markets/v2"


class AlpacaInternalClient:
    """
    Alpaca paper trading client.
    Supports two auth modes:
    - Standard API keys (APCA-API-KEY-ID + APCA-API-SECRET-KEY): preferred
    - Cognito JWT (email/password/TOTP): fallback for data API only
    """

    def __init__(self, email: str, password: str, mfa_secret: str, paper_account_id: str,
                 api_key: str = "", secret_key: str = ""):
        self.email = email
        self.password = password
        self.mfa_secret = mfa_secret
        self.paper_account_id = paper_account_id
        self.api_key = api_key
        self.secret_key = secret_key
        self._has_keys = bool(api_key and secret_key)
        self._jwt: Optional[str] = None
        self._jwt_expiry: float = 0.0
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(timeout=30)

    async def close(self):
        await self._http.aclose()

    async def _ensure_jwt(self):
        if self._jwt and time.time() < self._jwt_expiry:
            return
        async with self._lock:
            if self._jwt and time.time() < self._jwt_expiry:
                return
            logger.info("Refreshing Alpaca JWT via SRP auth...")
            loop = asyncio.get_event_loop()
            id_token = await loop.run_in_executor(
                None, _sync_srp_auth, self.email, self.password, self.mfa_secret
            )
            # Exchange Cognito IdToken for Alpaca ES256 JWT (same as browser flow)
            try:
                r = await self._http.post(
                    AUTHX_URL,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": id_token,
                        "client_id": COGNITO_CLIENT_ID,
                    },
                    headers={
                        "Origin": "https://app.alpaca.markets",
                        "Referer": "https://app.alpaca.markets/",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                        "Accept": "application/json, text/plain, */*",
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                    timeout=15,
                )
                if r.status_code == 200:
                    self._jwt = r.json()["access_token"]
                    logger.info("Alpaca ES256 JWT obtained via SRP+authx")
                else:
                    logger.warning(f"authx exchange failed {r.status_code}, using IdToken directly")
                    self._jwt = id_token
            except Exception as e:
                logger.warning(f"authx exchange error: {e}, using IdToken directly")
                self._jwt = id_token
            self._jwt_expiry = time.time() + TOKEN_TTL
            logger.info("Alpaca JWT refreshed, valid for ~55 min")

    def _api_headers(self) -> dict:
        """Standard API key auth headers."""
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Content-Type": "application/json",
        }

    def _headers(self) -> dict:
        if self._has_keys:
            return self._api_headers()
        return {"Authorization": f"Bearer {self._jwt}", "Content-Type": "application/json"}

    async def get_account(self) -> dict:
        if self._has_keys:
            r = await self._http.get(
                f"{PAPER_BASE}/account",
                headers=self._api_headers(),
            )
            r.raise_for_status()
            return r.json()
        await self._ensure_jwt()
        r = await self._http.get(
            f"{INTERNAL_BASE}/paper_accounts/{self.paper_account_id}/trade_account",
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    async def get_orders(self, status: str = "open") -> list:
        if self._has_keys:
            r = await self._http.get(f"{PAPER_BASE}/orders", headers=self._api_headers(), params={"status": status})
            r.raise_for_status()
            return r.json()
        await self._ensure_jwt()
        r = await self._http.get(
            f"{INTERNAL_BASE}/paper_accounts/{self.paper_account_id}/orders",
            headers=self._headers(), params={"status": status},
        )
        r.raise_for_status()
        return r.json()

    async def get_positions(self) -> list:
        if self._has_keys:
            r = await self._http.get(f"{PAPER_BASE}/positions", headers=self._api_headers())
            r.raise_for_status()
            return r.json()
        await self._ensure_jwt()
        r = await self._http.get(
            f"{INTERNAL_BASE}/paper_accounts/{self.paper_account_id}/positions",
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    async def place_order(
        self,
        symbol: str,
        side: str,
        qty: Optional[float] = None,
        notional: Optional[float] = None,
        order_type: str = "market",
        time_in_force: str = "day",
    ) -> dict:
        body: dict = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
        }
        if qty is not None:
            body["qty"] = qty
        elif notional is not None:
            body["notional"] = round(notional, 2)
        if self._has_keys:
            r = await self._http.post(f"{PAPER_BASE}/orders", headers=self._api_headers(), json=body)
            r.raise_for_status()
            return r.json()
        await self._ensure_jwt()
        r = await self._http.post(
            f"{INTERNAL_BASE}/paper_accounts/{self.paper_account_id}/orders",
            headers=self._headers(), json=body,
        )
        r.raise_for_status()
        return r.json()

    async def cancel_order(self, order_id: str) -> bool:
        if self._has_keys:
            r = await self._http.delete(f"{PAPER_BASE}/orders/{order_id}", headers=self._api_headers())
            return r.status_code in (200, 204)
        await self._ensure_jwt()
        r = await self._http.delete(
            f"{INTERNAL_BASE}/paper_accounts/{self.paper_account_id}/orders/{order_id}",
            headers=self._headers(),
        )
        return r.status_code in (200, 204)

    async def cancel_all_orders(self) -> int:
        orders = await self.get_orders(status="open")
        cancelled = 0
        for o in orders:
            if await self.cancel_order(o["id"]):
                cancelled += 1
        return cancelled

    async def get_bars(self, symbol: str, limit: int = 150) -> Optional[list]:
        """Fetch daily bars. Standard API keys preferred; falls back to Cognito JWT."""
        from datetime import datetime, timezone, timedelta
        end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        start = (datetime.now(timezone.utc) - timedelta(days=250)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {"timeframe": "1Day", "start": start, "end": end,
                  "limit": limit, "adjustment": "raw", "feed": "iex"}

        # Preferred: standard API key headers
        if self._has_keys:
            try:
                r = await self._http.get(
                    f"{DATA_BASE}/{symbol}/bars",
                    headers=self._api_headers(),
                    params=params,
                )
                if r.status_code == 200:
                    bars = r.json().get("bars", [])
                    if bars:
                        logger.info(f"get_bars({symbol}): {len(bars)} bars via API key")
                        return bars
                else:
                    logger.warning(f"get_bars({symbol}) API key failed: {r.status_code}")
            except Exception as e:
                logger.warning(f"get_bars({symbol}) API key error: {e}")

        # Fallback: Cognito JWT
        await self._ensure_jwt()
        for base_url in [
            "https://data.alpaca.markets/v2/stocks",
            "https://app.alpaca.markets/internal/data/v2/stocks",
        ]:
            try:
                r = await self._http.get(
                    f"{base_url}/{symbol}/bars",
                    headers={"Authorization": f"Bearer {self._jwt}", "Content-Type": "application/json"},
                    params=params,
                )
                if r.status_code == 200:
                    bars = r.json().get("bars", [])
                    if bars:
                        logger.info(f"get_bars({symbol}): {len(bars)} bars via JWT from {base_url}")
                        return bars
            except Exception:
                pass
        return None

    async def close_all_positions(self) -> list:
        positions = await self.get_positions()
        results = []
        for pos in positions:
            symbol = pos["symbol"]
            qty = abs(float(pos.get("qty", 0)))
            side = "sell" if float(pos.get("qty", 0)) > 0 else "buy"
            if qty > 0:
                try:
                    order = await self.place_order(symbol, side, qty=qty)
                    results.append({"symbol": symbol, "order_id": order.get("id")})
                except Exception as e:
                    results.append({"symbol": symbol, "error": str(e)})
        return results
