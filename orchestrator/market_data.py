"""
market_data.py v1
Alpha Vantage market data fetcher for trading analysis.
"""
import logging
import httpx
from typing import Optional

logger = logging.getLogger(__name__)

ALPHA_VANTAGE_BASE = "https://www.alphavantage.co/query"


class MarketDataClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def get_quote(self, symbol: str) -> Optional[dict]:
        if not self.api_key:
            return None
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    ALPHA_VANTAGE_BASE,
                    params={"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": self.api_key},
                    timeout=10,
                )
            data = resp.json()
            q = data.get("Global Quote", {})
            if not q:
                return None
            return {
                "symbol": q.get("01. symbol"),
                "price": float(q.get("05. price", 0)),
                "change": float(q.get("09. change", 0)),
                "change_pct": q.get("10. change percent", "0%"),
                "volume": int(q.get("06. volume", 0)),
                "latest_day": q.get("07. latest trading day"),
            }
        except Exception as e:
            logger.warning(f"MarketData get_quote {symbol} failed: {e}")
            return None

    async def get_forex_rate(self, from_currency: str, to_currency: str) -> Optional[dict]:
        if not self.api_key:
            return None
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    ALPHA_VANTAGE_BASE,
                    params={
                        "function": "CURRENCY_EXCHANGE_RATE",
                        "from_currency": from_currency,
                        "to_currency": to_currency,
                        "apikey": self.api_key,
                    },
                    timeout=10,
                )
            data = resp.json()
            r = data.get("Realtime Currency Exchange Rate", {})
            if not r:
                return None
            return {
                "from": r.get("1. From_Currency Code"),
                "to": r.get("3. To_Currency Code"),
                "rate": float(r.get("5. Exchange Rate", 0)),
                "last_refreshed": r.get("6. Last Refreshed"),
            }
        except Exception as e:
            logger.warning(f"MarketData forex {from_currency}/{to_currency} failed: {e}")
            return None

    async def analyze_for_trade(self, symbol: str, claude_client) -> Optional[dict]:
        """Get quote + ask Claude for a simple buy/hold/sell signal."""
        quote = await self.get_quote(symbol)
        if not quote:
            return None
        prompt = (
            f"Stock analysis request. Symbol: {symbol}\n"
            f"Current price: ${quote['price']}\n"
            f"Today's change: {quote['change_pct']}\n"
            f"Volume: {quote['volume']}\n"
            f"Based only on this data, give a one-word signal: BUY, HOLD, or SELL. "
            f"Then one sentence of reasoning. Format: SIGNAL: <word>\\nReason: <sentence>"
        )
        try:
            msg = await claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}],
            )
            analysis_text = msg.content[0].text
            return {
                "symbol": symbol,
                "quote": quote,
                "analysis": analysis_text,
            }
        except Exception as e:
            logger.warning(f"Claude analysis for {symbol} failed: {e}")
            return {"symbol": symbol, "quote": quote, "analysis": "Analysis unavailable"}
