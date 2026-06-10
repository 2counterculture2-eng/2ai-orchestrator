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


def _sync_cognito_auth(email: str, password: str, mfa_secret: str) -> str:
    """Blocking Cognito auth — run via run_in_executor."""
    import boto3
    from botocore.config import Config

    cognito = boto3.client(
        "cognito-idp",
        region_name=COGNITO_REGION,
        config=Config(signature_version="unsigned"),
    )
    # Wait for a safe TOTP window (not within 3s of expiry)
    secs = int(time.time()) % 30
    if secs > 27:
        time.sleep(33 - secs)

    resp = cognito.initiate_auth(
        ClientId=COGNITO_CLIENT_ID,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": email, "PASSWORD": password},
    )
    if resp.get("ChallengeName") == "SOFTWARE_TOKEN_MFA":
        totp = _gen_totp(mfa_secret)
        mfa_resp = cognito.respond_to_auth_challenge(
            ClientId=COGNITO_CLIENT_ID,
            ChallengeName="SOFTWARE_TOKEN_MFA",
            Session=resp["Session"],
            ChallengeResponses={
                "USERNAME": email,
                "SOFTWARE_TOKEN_MFA_CODE": totp,
            },
        )
        return mfa_resp["AuthenticationResult"]["IdToken"]
    return resp["AuthenticationResult"]["IdToken"]


class AlpacaInternalClient:
    """
    Trades on Alpaca paper account via the internal API with Cognito JWT auth.
    Thread-safe: a single asyncio lock guards token refresh.
    """

    def __init__(self, email: str, password: str, mfa_secret: str, paper_account_id: str):
        self.email = email
        self.password = password
        self.mfa_secret = mfa_secret
        self.paper_account_id = paper_account_id
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
            logger.info("Refreshing Alpaca Cognito JWT...")
            loop = asyncio.get_event_loop()
            self._jwt = await loop.run_in_executor(
                None, _sync_cognito_auth, self.email, self.password, self.mfa_secret
            )
            self._jwt_expiry = time.time() + TOKEN_TTL
            logger.info("Alpaca JWT refreshed, valid for ~55 min")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._jwt}", "Content-Type": "application/json"}

    async def get_account(self) -> dict:
        await self._ensure_jwt()
        r = await self._http.get(
            f"{INTERNAL_BASE}/paper_accounts/{self.paper_account_id}/trade_account",
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    async def get_orders(self, status: str = "open") -> list:
        await self._ensure_jwt()
        r = await self._http.get(
            f"{INTERNAL_BASE}/paper_accounts/{self.paper_account_id}/orders",
            headers=self._headers(),
            params={"status": status},
        )
        r.raise_for_status()
        return r.json()

    async def get_positions(self) -> list:
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
        await self._ensure_jwt()
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
        r = await self._http.post(
            f"{INTERNAL_BASE}/paper_accounts/{self.paper_account_id}/orders",
            headers=self._headers(),
            json=body,
        )
        r.raise_for_status()
        return r.json()

    async def cancel_order(self, order_id: str) -> bool:
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
