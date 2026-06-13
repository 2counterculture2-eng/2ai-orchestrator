"""
trading_worker.py v4
Rule-based momentum trading (MA crossover + trend filter).
Market data: yfinance library (Yahoo Finance) - handles auth/cookies automatically.
Claude: sentiment filter only - NOT the primary trade decision maker.
Primary signal: MA5 > MA20 > MA50 = uptrend buy; MA5 < MA20 = exit.
Phase 1: paper trading only (Alpaca paper account).
"""
import logging
import json
import httpx
import statistics
from datetime import datetime, timezone
from typing import Optional, List, Dict

from .base_worker import BaseWorker, TaskResult
from ..config import Config
from ..learning import LearningDB
from ..alpaca_client import AlpacaInternalClient

logger = logging.getLogger(__name__)

SENTIMENT_SYSTEM = """You are a market sentiment analyst. Assess if trend-following conditions are favorable.
Return JSON only: {"sentiment": "positive"|"neutral"|"negative", "risk_level": "low"|"medium"|"high", "note": "brief"}
Secondary filter only - primary signal is rule-based MA crossover."""

MIN_DAYS_REQUIRED = 25


def compute_ma(prices: List[float], period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    return statistics.mean(prices[:period])


def compute_momentum(prices: List[float], period: int) -> Optional[float]:
    if len(prices) <= period:
        return None
    return (prices[0] - prices[period]) / prices[period] * 100


def rule_based_signal(symbol: str, closes: List[float]) -> Dict:
    """MA crossover momentum strategy. closes sorted newest-first."""
    ma5 = compute_ma(closes, 5)
    ma20 = compute_ma(closes, 20)
    ma50 = compute_ma(closes, 50)
    mom60 = compute_momentum(closes, 60)
    mom20 = compute_momentum(closes, 20)

    if ma5 is None or ma20 is None:
        return {"action": "hold", "confidence": 0.0, "reason": "insufficient data",
                "ma5": None, "ma20": None, "ma50": ma50}

    action = "hold"
    confidence = 0.0
    reason_parts = []

    uptrend_ma = ma5 > ma20
    strong_uptrend = (ma50 is not None) and ma5 > ma20 and ma20 > ma50

    if uptrend_ma:
        reason_parts.append(f"MA5({ma5:.2f})>MA20({ma20:.2f})")
        confidence += 0.4
    if strong_uptrend:
        reason_parts.append(f"MA20>MA50({ma50:.2f})")
        confidence += 0.2
    if mom60 is not None and mom60 > 5:
        reason_parts.append(f"60d_mom={mom60:.1f}%")
        confidence += 0.2
    if mom20 is not None and mom20 > 2:
        reason_parts.append(f"20d_mom={mom20:.1f}%")
        confidence += 0.1

    if uptrend_ma and confidence >= 0.6:
        action = "buy"
    elif not uptrend_ma:
        action = "sell"
        confidence = 0.5
        reason_parts = [f"MA5({ma5:.2f})<MA20({ma20:.2f}) exit signal"]

    return {
        "action": action,
        "confidence": round(min(confidence, 1.0), 2),
        "reason": "; ".join(reason_parts) if reason_parts else "no signal",
        "ma5": ma5, "ma20": ma20, "ma50": ma50, "mom60": mom60, "mom20": mom20,
    }


def fetch_closes_yfinance_sync(symbol: str, period: str = "6mo") -> Optional[List[float]]:
    """Fetch daily close prices using yfinance library (handles Yahoo auth)."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period)
        if hist is None or len(hist) < MIN_DAYS_REQUIRED:
            logger.warning(f"{symbol}: yfinance returned {0 if hist is None else len(hist)} rows")
            return None
        closes = list(reversed(hist["Close"].tolist()))
        return closes
    except Exception as e:
        logger.warning(f"yfinance failed for {symbol}: {e}")
        return None


async def fetch_closes_stooq(symbol: str, http: httpx.AsyncClient) -> Optional[List[float]]:
    """Fallback: Stooq (Polish financial portal, free, no rate limits, works from cloud)."""
    try:
        from datetime import timedelta
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=150)
        url = f"https://stooq.com/q/d/l/?s={symbol.lower()}.us&d1={start.strftime('%Y%m%d')}&d2={end.strftime('%Y%m%d')}&i=d"
        resp = await http.get(url, timeout=15, follow_redirects=True)
        if resp.status_code != 200:
            return None
        lines = resp.text.strip().split("\n")
        if len(lines) < MIN_DAYS_REQUIRED + 1:
            return None
        closes = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) >= 5:
                try:
                    closes.append(float(parts[4]))
                except ValueError:
                    pass
        if len(closes) < MIN_DAYS_REQUIRED:
            return None
        closes.reverse()  # newest first
        return closes
    except Exception as e:
        logger.warning(f"Stooq fallback failed for {symbol}: {e}")
        return None


class TradingWorker(BaseWorker):
    worker_name = "trading"
    task_type = "trading"

    def __init__(self, config: Config, db: LearningDB):
        super().__init__(config, db)
        self._http = httpx.AsyncClient(timeout=30)
        self._alpaca: Optional[AlpacaInternalClient] = None
        if config.alpaca_email and config.alpaca_password and config.alpaca_mfa_secret:
            self._alpaca = AlpacaInternalClient(
                email=config.alpaca_email,
                password=config.alpaca_password,
                mfa_secret=config.alpaca_mfa_secret,
                paper_account_id=config.alpaca_paper_account_id,
            )
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
                result = TaskResult(success=False, error=f"Unknown channel: {channel}")

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
            logger.exception(f"TradingWorker error on {task_id}")
            self.db.update_task(task_id, "failed", error_msg=str(e))
            self.db.record_error("trading_worker", str(e))
            return TaskResult(success=False, task_id=task_id, error=str(e))

    # ---- Alpaca ----

    async def _handle_alpaca(self, task_id: str, task: dict) -> TaskResult:
        if not self._alpaca and not self.config.alpaca_api_key:
            return TaskResult(success=False, task_id=task_id, error="Alpaca not configured")
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
            return TaskResult(success=True, task_id=task_id, data={
                "portfolio_value": float(account.get("portfolio_value", 0)),
                "cash": float(account.get("cash", 0)),
                "equity": float(account.get("equity", account.get("portfolio_value", 0))),
                "buying_power": float(account.get("buying_power", 0)),
                "status": account.get("status"),
            })
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

        # Fetch price history: try yfinance first, fall back to Stooq
        import asyncio
        market_data = {}
        signals = {}

        for symbol in symbols[:5]:
            # Run yfinance in thread pool (it's synchronous)
            loop = asyncio.get_event_loop()
            closes = await loop.run_in_executor(None, fetch_closes_yfinance_sync, symbol)

            if closes is None:
                # Fallback to Stooq
                logger.info(f"{symbol}: yfinance failed, trying Stooq")
                closes = await fetch_closes_stooq(symbol, self._http)

            if closes is None:
                logger.warning(f"{symbol}: all data sources failed")
                continue

            market_data[symbol] = {
                "close": closes[0],
                "change_pct": round((closes[0] - closes[1]) / closes[1] * 100, 2) if len(closes) > 1 else 0,
                "days_of_data": len(closes),
            }
            sig = rule_based_signal(symbol, closes)
            signals[symbol] = sig
            logger.info(f"{symbol}: action={sig['action']} conf={sig['confidence']} | {sig['reason']}")

        if not market_data:
            return TaskResult(success=False, task_id=task_id,
                              error="No market data fetched (yfinance + Stooq both failed)")

        # Pick best buy signal
        buy_candidates = sorted(
            [(sym, sig) for sym, sig in signals.items()
             if sig["action"] == "buy" and sig["confidence"] >= 0.6],
            key=lambda x: x[1]["confidence"], reverse=True
        )

        if not buy_candidates:
            sell_candidates = [(sym, sig) for sym, sig in signals.items() if sig["action"] == "sell"]
            if sell_candidates and self._alpaca:
                best_sym, best_sig = sell_candidates[0]
                try:
                    positions = await self._alpaca.get_positions()
                    held = [p for p in positions if p.get("symbol") == best_sym]
                    if held:
                        mv = float(held[0].get("market_value", 0))
                        order = await self._alpaca.place_order(best_sym, "sell", notional=mv)
                        return TaskResult(success=True, task_id=task_id, data={
                            "action": "sell", "symbol": best_sym, "signal": best_sig,
                            "order": order, "executed": True
                        })
                except Exception as e:
                    logger.warning(f"Sell attempt failed: {e}")

            return TaskResult(success=True, task_id=task_id, data={
                "signals": signals, "executed": False,
                "reason": "No buy signal above threshold (rule-based MA crossover)"
            })

        best_sym, best_sig = buy_candidates[0]

        # Claude sentiment filter (secondary only)
        sentiment_ok = True
        claude_cost = 0.0
        try:
            summary = (f"Symbol: {best_sym}\nPrice: {market_data[best_sym]['close']:.2f}\n"
                       f"Change: {market_data[best_sym]['change_pct']}%\nMA signal: {best_sig['reason']}")
            sent_text, claude_cost = self.call_claude(
                system=SENTIMENT_SYSTEM, user=summary,
                model=self.config.claude_haiku_model, max_tokens=100,
            )
            import re as _re
            m = _re.search(r'\{.*\}', sent_text, _re.DOTALL)
            sentiment = json.loads(m.group(0) if m else sent_text.strip())
            if sentiment.get("sentiment") == "negative" and sentiment.get("risk_level") == "high":
                sentiment_ok = False
                logger.info(f"Sentiment blocked: {sentiment}")
        except Exception as e:
            logger.warning(f"Claude sentiment skipped (proceeding): {e}")

        if not sentiment_ok:
            return TaskResult(success=True, task_id=task_id, data={
                "signals": signals, "executed": False,
                "reason": "Blocked by Claude sentiment filter"
            }, cost_usd=claude_cost)

        # Execute buy (2% of portfolio max per trade)
        notional = portfolio_value * 2.0 / 100
        try:
            if self._alpaca:
                order = await self._alpaca.place_order(best_sym, "buy", notional=notional)
            else:
                base = self.config.alpaca_base_url
                order_resp = await self._http.post(
                    f"{base}/v2/orders", headers=self._alpaca_headers,
                    json={"symbol": best_sym, "notional": round(notional, 2), "side": "buy",
                          "type": "market", "time_in_force": "day"},
                )
                order_resp.raise_for_status()
                order = order_resp.json()
            logger.info(f"Executed: buy {best_sym} ${notional:.2f} conf={best_sig['confidence']}")
            return TaskResult(
                success=True, task_id=task_id,
                data={"signal": best_sig, "executed": True, "symbol": best_sym,
                      "notional": notional, "order": order},
                cost_usd=claude_cost,
            )
        except httpx.HTTPStatusError as e:
            return TaskResult(success=False, task_id=task_id,
                              error=f"Order failed: {e.response.status_code} {e.response.text}")

    async def _alpaca_close_all(self, task_id: str) -> TaskResult:
        try:
            if self._alpaca:
                results = await self._alpaca.close_all_positions()
                return TaskResult(success=True, task_id=task_id, data={"closed": results})
            base = self.config.alpaca_base_url
            resp = await self._http.delete(
                f"{base}/v2/positions", headers=self._alpaca_headers, params={"cancel_orders": True}
            )
            resp.raise_for_status()
            return TaskResult(success=True, task_id=task_id, data={"closed": True})
        except httpx.HTTPStatusError as e:
            return TaskResult(success=False, task_id=task_id,
                              error=f"Close all failed: {e.response.status_code}")

    # ---- OANDA ----

    async def _handle_oanda(self, task_id: str, task: dict) -> TaskResult:
        if not self.config.oanda_api_key:
            return TaskResult(success=False, task_id=task_id, error="OANDA API key not configured")
        env = self.config.oanda_environment
        base = "https://api-fxtrade.oanda.com" if env == "live" else "https://api-fxpractice.oanda.com"
        headers = {"Authorization": f"Bearer {self.config.oanda_api_key}", "Content-Type": "application/json"}
        if task.get("action") == "status":
            try:
                resp = await self._http.get(f"{base}/v3/accounts/{self.config.oanda_account_id}", headers=headers)
                resp.raise_for_status()
                account = resp.json().get("account", {})
                return TaskResult(success=True, task_id=task_id, data={
                    "balance": float(account.get("balance", 0)),
                    "unrealized_pl": float(account.get("unrealizedPL", 0)),
                    "open_positions": int(account.get("openPositionCount", 0)),
                })
            except httpx.HTTPStatusError as e:
                return TaskResult(success=False, task_id=task_id, error=f"OANDA HTTP {e.response.status_code}")
        return TaskResult(success=False, task_id=task_id, error="OANDA action not implemented")

    # ---- Freqtrade ----

    async def _handle_freqtrade(self, task_id: str, task: dict) -> TaskResult:
        freqtrade_url = task.get("freqtrade_url", "http://localhost:8080")
        action = task.get("action", "status")
        try:
            if action == "status":
                resp = await self._http.get(f"{freqtrade_url}/api/v1/status",
                                            auth=("freqtrade", task.get("freqtrade_password", "")))
                resp.raise_for_status()
                return TaskResult(success=True, task_id=task_id, data=resp.json())
            elif action == "profit":
                resp = await self._http.get(f"{freqtrade_url}/api/v1/profit",
                                            auth=("freqtrade", task.get("freqtrade_password", "")))
                resp.raise_for_status()
                profit_data = resp.json()
                return TaskResult(success=True, task_id=task_id, data=profit_data,
                                  revenue_usd=max(float(profit_data.get("profit_all_coin", 0)), 0))
        except Exception as e:
            return TaskResult(success=False, task_id=task_id, error=f"Freqtrade: {e}")
        return TaskResult(success=False, task_id=task_id, error=f"Unknown Freqtrade action: {action}")
