"""
trading_worker.py v1
Algorithmic trading worker.
Phase 1: paper trading only (ALPACA_BASE_URL = paper API).
Phase 2: switch to live after 2 weeks of paper verification.
Channels: Alpaca (US stocks), OANDA (FX), placeholder for Freqtrade/Bybit
"""
import logging
import json
import httpx
from datetime import datetime, timezone, timedelta
from typing import Optional

from .base_worker import BaseWorker, TaskResult
from ..config import Config
from ..learning import LearningDB
from ..alpaca_client import AlpacaInternalClient

logger = logging.getLogger(__name__)

ANALYSIS_SYSTEM = """You are a quantitative trading analyst. Analyze market data and return trading signals.

Rules:
- Never risk more than 2% of portfolio per trade
- Only trade with the trend (no counter-trend)
- Prefer high-liquidity assets
- Be conservative — missing a trade is better than a bad trade

Output JSON only:
{"action": "buy"|"sell"|"hold", "symbol": "...", "confidence": 0-1, "reason": "...", "size_pct": 0-2}"""


class TradingWorker(BaseWorker):
    worker_name = "trading"
    task_type = "trading"

    def __init__(self, config: Config, db: LearningDB):
        super().__init__(config, db)
        self._http = httpx.AsyncClient(timeout=30)
        # Use internal Cognito-based client if credentials are provided
        self._alpaca: Optional[AlpacaInternalClient] = None
        if config.alpaca_email and config.alpaca_password and config.alpaca_mfa_secret:
            self._alpaca = AlpacaInternalClient(
                email=config.alpaca_email,
                password=config.alpaca_password,
                mfa_secret=config.alpaca_mfa_secret,
                paper_account_id=config.alpaca_paper_account_id,
            )
        # Legacy API key headers (fallback if internal client not available)
        self._alpaca_headers = {
            "APCA-API-KEY-ID": config.alpaca_api_key,
            "APCA-API-SECRET-KEY": config.alpaca_secret_key,
        }

    async def close(self):
        await self._http.aclose()
        if self._alpaca:
            await self._alpaca.close()

    async def execute(self, task: dict) -> TaskResult:
        task_id = self.new_task_id()
        channel = task.get("channel", "alpaca")
        self.db.create_task(task_id, self.task_type, channel, task)

        try:
            if channel == "alpaca":
                result = await self._handle_alpaca(task_id, task)
            elif channel == "oanda":
                result = await self._handle_oanda(task_id, task)
            elif channel == "freqtrade":
                result = await self._handle_freqtrade(task_id, task)
            else:
                result = TaskResult(success=False, error=f"Unknown trading channel: {channel}")

            status = "completed" if result.success else "failed"
            self.db.update_task(
                task_id, status,
                result_data=result.data if isinstance(result.data, dict) else {},
                revenue_usd=result.revenue_usd,
                error_msg=result.error,
            )
            if result.revenue_usd > 0:
                self.db.log_revenue(channel, result.revenue_usd, f"Trade {task_id}", task_id)
            return result

        except Exception as e:
            logger.exception(f"TradingWorker error on task {task_id}")
            self.db.update_task(task_id, "failed", error_msg=str(e))
            self.db.record_error("trading_worker", str(e))
            return TaskResult(success=False, task_id=task_id, error=str(e))

    # ---- Alpaca ----

    async def _handle_alpaca(self, task_id: str, task: dict) -> TaskResult:
        # Prefer internal Cognito-based client; fall back to legacy API key check
        if not self._alpaca and not self.config.alpaca_api_key:
            return TaskResult(success=False, task_id=task_id, error="Alpaca not configured (set ALPACA_EMAIL+ALPACA_PASSWORD+ALPACA_MFA_SECRET)")

        action = task.get("action", "analyze")
        if action == "analyze":
            return await self._alpaca_analyze_and_trade(task_id, task)
        elif action == "status":
            return await self._alpaca_account_status(task_id)
        elif action == "close_all":
            return await self._alpaca_close_all(task_id)
        return TaskResult(success=False, task_id=task_id, error=f"Unknown Alpaca action: {action}")

    async def _alpaca_account_status(self, task_id: str) -> TaskResult:
        try:
            if self._alpaca:
                account = await self._alpaca.get_account()
            else:
                base = self.config.alpaca_base_url
                resp = await self._http.get(f"{base}/v2/account", headers=self._alpaca_headers)
                resp.raise_for_status()
                account = resp.json()
            return TaskResult(
                success=True,
                task_id=task_id,
                data={
                    "portfolio_value": float(account.get("portfolio_value", 0)),
                    "cash": float(account.get("cash", 0)),
                    "equity": float(account.get("equity", account.get("portfolio_value", 0))),
                    "buying_power": float(account.get("buying_power", 0)),
                    "status": account.get("status"),
                },
            )
        except httpx.HTTPStatusError as e:
            return TaskResult(success=False, task_id=task_id, error=f"Alpaca HTTP {e.response.status_code}")

    async def _alpaca_analyze_and_trade(self, task_id: str, task: dict) -> TaskResult:
        symbols = task.get("symbols", ["AAPL", "MSFT", "NVDA", "SPY", "QQQ"])

        # Get account info
        try:
            if self._alpaca:
                account = await self._alpaca.get_account()
            else:
                base = self.config.alpaca_base_url
                acct_resp = await self._http.get(f"{base}/v2/account", headers=self._alpaca_headers)
                acct_resp.raise_for_status()
                account = acct_resp.json()
            portfolio_value = float(account.get("portfolio_value", 10000))
        except Exception as e:
            return TaskResult(success=False, task_id=task_id, error=f"Cannot get account: {e}")

        # Get market data via Alpha Vantage (available without Alpaca API keys)
        market_data = {}
        for symbol in symbols[:3]:  # limit to 3 symbols to keep API calls low
            try:
                av_key = self.config.alpha_vantage_api_key
                if av_key:
                    bars_resp = await self._http.get(
                        "https://www.alphavantage.co/query",
                        params={
                            "function": "TIME_SERIES_DAILY",
                            "symbol": symbol,
                            "outputsize": "compact",
                            "apikey": av_key,
                        },
                    )
                    if bars_resp.status_code == 200:
                        ts = bars_resp.json().get("Time Series (Daily)", {})
                        dates = sorted(ts.keys(), reverse=True)[:2]
                        if len(dates) >= 2:
                            today = ts[dates[0]]
                            prev = ts[dates[1]]
                            market_data[symbol] = {
                                "close": float(today["4. close"]),
                                "open": float(today["1. open"]),
                                "high": float(today["2. high"]),
                                "low": float(today["3. low"]),
                                "volume": int(today["5. volume"]),
                                "change_pct": round(
                                    (float(today["4. close"]) - float(prev["4. close"]))
                                    / float(prev["4. close"]) * 100, 2
                                ),
                            }
            except Exception:
                pass

        if not market_data:
            return TaskResult(success=False, task_id=task_id, error="No market data fetched")

        # Ask Claude for trading signal
        analysis_prompt = (
            f"Portfolio value: ${portfolio_value:.0f}\n"
            f"Market data (last 2 days):\n{json.dumps(market_data, indent=2)}\n\n"
            f"Should we trade? Pick the best single action or hold."
        )
        try:
            signal_text, cost = self.call_claude(
                system=ANALYSIS_SYSTEM,
                user=analysis_prompt,
                model=self.config.claude_haiku_model,
                max_tokens=200,
            )
            signal = json.loads(signal_text.strip())
        except Exception as e:
            return TaskResult(success=False, task_id=task_id, error=f"Claude analysis failed: {e}")

        if signal.get("action") == "hold" or signal.get("confidence", 0) < 0.65:
            return TaskResult(
                success=True,
                task_id=task_id,
                data={"signal": signal, "executed": False, "reason": "Below confidence threshold"},
                cost_usd=cost,
            )

        # Execute the trade
        symbol = signal["symbol"]
        size_pct = min(signal.get("size_pct", 1.0), 2.0)  # cap at 2%
        notional = portfolio_value * size_pct / 100
        side = signal["action"]

        try:
            if self._alpaca:
                order = await self._alpaca.place_order(symbol, side, notional=notional)
            else:
                base = self.config.alpaca_base_url
                order_resp = await self._http.post(
                    f"{base}/v2/orders",
                    headers=self._alpaca_headers,
                    json={"symbol": symbol, "notional": round(notional, 2), "side": side,
                          "type": "market", "time_in_force": "day"},
                )
                order_resp.raise_for_status()
                order = order_resp.json()
            logger.info(f"Alpaca order placed: {side} {symbol} ${notional:.2f}")
            return TaskResult(
                success=True,
                task_id=task_id,
                data={"signal": signal, "executed": True, "order": order},
                cost_usd=cost,
            )
        except httpx.HTTPStatusError as e:
            return TaskResult(
                success=False,
                task_id=task_id,
                error=f"Order failed: {e.response.status_code} {e.response.text}",
            )

    async def _alpaca_close_all(self, task_id: str) -> TaskResult:
        try:
            if self._alpaca:
                results = await self._alpaca.close_all_positions()
                return TaskResult(success=True, task_id=task_id, data={"closed": results})
            base = self.config.alpaca_base_url
            resp = await self._http.delete(
                f"{base}/v2/positions",
                headers=self._alpaca_headers,
                params={"cancel_orders": True},
            )
            resp.raise_for_status()
            return TaskResult(success=True, task_id=task_id, data={"closed": True})
        except httpx.HTTPStatusError as e:
            return TaskResult(success=False, task_id=task_id, error=f"Close all failed: {e.response.status_code}")

    # ---- OANDA ----

    async def _handle_oanda(self, task_id: str, task: dict) -> TaskResult:
        if not self.config.oanda_api_key:
            return TaskResult(success=False, task_id=task_id, error="OANDA API key not configured")

        env = self.config.oanda_environment
        base = "https://api-fxtrade.oanda.com" if env == "live" else "https://api-fxpractice.oanda.com"
        headers = {
            "Authorization": f"Bearer {self.config.oanda_api_key}",
            "Content-Type": "application/json",
        }
        action = task.get("action", "status")

        if action == "status":
            try:
                resp = await self._http.get(
                    f"{base}/v3/accounts/{self.config.oanda_account_id}",
                    headers=headers,
                )
                resp.raise_for_status()
                account = resp.json().get("account", {})
                return TaskResult(
                    success=True,
                    task_id=task_id,
                    data={
                        "balance": float(account.get("balance", 0)),
                        "unrealized_pl": float(account.get("unrealizedPL", 0)),
                        "open_positions": int(account.get("openPositionCount", 0)),
                    },
                )
            except httpx.HTTPStatusError as e:
                return TaskResult(success=False, task_id=task_id, error=f"OANDA HTTP {e.response.status_code}")

        return TaskResult(success=False, task_id=task_id, error=f"OANDA action not implemented: {action}")

    # ---- Freqtrade (placeholder — runs as separate Docker container) ----

    async def _handle_freqtrade(self, task_id: str, task: dict) -> TaskResult:
        """
        Freqtrade runs as a separate Docker container with its own REST API.
        This worker queries its status via HTTP.
        """
        freqtrade_url = task.get("freqtrade_url", "http://localhost:8080")
        action = task.get("action", "status")

        try:
            if action == "status":
                resp = await self._http.get(
                    f"{freqtrade_url}/api/v1/status",
                    auth=("freqtrade", task.get("freqtrade_password", "")),
                )
                resp.raise_for_status()
                return TaskResult(
                    success=True,
                    task_id=task_id,
                    data=resp.json(),
                )
            elif action == "profit":
                resp = await self._http.get(
                    f"{freqtrade_url}/api/v1/profit",
                    auth=("freqtrade", task.get("freqtrade_password", "")),
                )
                resp.raise_for_status()
                profit_data = resp.json()
                realized_profit = float(profit_data.get("profit_all_coin", 0))
                return TaskResult(
                    success=True,
                    task_id=task_id,
                    data=profit_data,
                    revenue_usd=max(realized_profit, 0),
                )
        except Exception as e:
            return TaskResult(success=False, task_id=task_id, error=f"Freqtrade: {e}")

        return TaskResult(success=False, task_id=task_id, error=f"Unknown Freqtrade action: {action}")
