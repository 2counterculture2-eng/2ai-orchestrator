"""
trading_worker.py v5
RSI2 Mean Reversion + 50-day MA trend filter strategy.
Market data: Alpaca data API (primary, confirmed working, ~137 bars).
Entry: close > MA50 AND RSI(2) < 5
Exit: RSI(2) > 65
Universe: SPY, QQQ, IWM (liquid ETFs - lower individual stock risk)
Claude: sentiment filter only - NOT the primary trade decision maker.
Phase 1: paper trading only (Alpaca paper account).
Note: Using MA50 instead of MA200 because Alpaca returns ~137 bars max.
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

SENTIMENT_SYSTEM = """You are a market sentiment analyst. Assess if mean reversion conditions are favorable.
Return JSON only: {"sentiment": "positive"|"neutral"|"negative", "risk_level": "low"|"medium"|"high", "note": "brief"}
Secondary filter only - primary signal is rule-based RSI2 mean reversion."""

MIN_DAYS_REQUIRED = 55  # MA50 needs 50+ bars; Alpaca returns ~137 bars


def compute_ma(prices: List[float], period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    return statistics.mean(prices[:period])


def compute_rsi(prices: List[float], period: int = 2) -> Optional[float]:
    """RSI calculation. prices sorted newest-first."""
    if len(prices) < period + 1:
        return None
    p = list(reversed(prices[:period + 10]))
    gains, losses = [], []
    for i in range(1, len(p)):
        diff = p[i] - p[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    if not gains:
        return None
    avg_gain = statistics.mean(gains[-period:])
    avg_loss = statistics.mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def rule_based_signal(symbol: str, closes: List[float]) -> Dict:
    """RSI2 mean reversion strategy with MA50 trend filter. closes sorted newest-first."""
    ma50 = compute_ma(closes, 50)
    rsi2 = compute_rsi(closes, 2)
    current_price = closes[0] if closes else None

    if ma50 is None or rsi2 is None or current_price is None:
        return {"action": "hold", "confidence": 0.0, "reason": "insufficient data",
                "ma50": None, "rsi2": None, "price": current_price}

    above_ma50 = current_price > ma50
    action = "hold"
    confidence = 0.0
    reason_parts = []

    if above_ma50:
        reason_parts.append(f"price({current_price:.2f})>MA50({ma50:.2f})")

        if rsi2 < 5:
            action = "buy"
            confidence = 0.8
            reason_parts.append(f"RSI2({rsi2:.1f})<5 OVERSOLD")
        elif rsi2 < 10:
            action = "buy"
            confidence = 0.65
            reason_parts.append(f"RSI2({rsi2:.1f})<10 oversold")
        elif rsi2 > 65:
            action = "sell"
            confidence = 0.7
            reason_parts.append(f"RSI2({rsi2:.1f})>65 OVERBOUGHT exit")
        else:
            reason_parts.append(f"RSI2({rsi2:.1f}) neutral - wait")
    else:
        action = "hold"
        confidence = 0.0
        reason_parts.append(f"price({current_price:.2f})<MA50({ma50:.2f}) DOWNTREND - no entry")
        if rsi2 > 65:
            action = "sell"
            confidence = 0.6
            reason_parts.append(f"RSI2({rsi2:.1f})>65 exit")

    return {
        "action": action,
        "confidence": round(min(confidence, 1.0), 2),
        "reason": "; ".join(reason_parts) if reason_parts else "no signal",
        "ma50": ma50, "rsi2": rsi2, "price": current_price,
        "above_ma50": above_ma50,
    }


async def fetch_closes_alpaca(symbol: str, alpaca_client) -> Optional[List[float]]:
    """Primary: Alpaca data API via internal Cognito JWT. No rate limits."""
    try:
        bars = await alpaca_client.get_bars(symbol, limit=150)
        if not bars or len(bars) < MIN_DAYS_REQUIRED:
            logger.warning(f"Alpaca bars {symbol}: {len(bars) if bars else 0} bars")
            return None
        closes = [float(b["c"]) for b in reversed(bars)]  # newest first
        logger.info(f"Alpaca data API {symbol}: {len(closes)} bars")
        return closes
    except Exception as e:
        logger.warning(f"Alpaca data API failed for {symbol}: {e}")
        return None


async def fetch_closes_stooq_http(symbol: str, http: httpx.AsyncClient) -> Optional[List[float]]:
    """Fallback: Direct Stooq HTTP."""
    try:
        from datetime import timedelta
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=320)
        url = f"https://stooq.com/q/d/l/?s={symbol.lower()}.us&d1={start.strftime('%Y%m%d')}&d2={end.strftime('%Y%m%d')}&i=d"
        headers = {"User-Agent": "Mozilla/5.0 (compatible; datareader/1.0)"}
        resp = await http.get(url, timeout=20, follow_redirects=True, headers=headers)
        if resp.status_code != 200:
            return None
        lines = resp.text.strip().splitlines()
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
        closes.reverse()
        logger.info(f"Stooq HTTP {symbol}: {len(closes)} bars")
        return closes
    except Exception as e:
        logger.warning(f"Stooq HTTP failed for {symbol}: {e}")
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
        except Exception as e:
            return TaskResult(success=False, task_id=task_id, error=f"Alpaca account error: {e}")

    async def _alpaca_analyze_and_trade(self, task_id: str, task: dict) -> TaskResult:
        symbols = task.get("symbols", ["AAPL", "MSFT", "NVDA"])

        signal_only = False
        portfolio_value = 100000.0  # paper account default
        try:
            if self._alpaca:
                account = await self._alpaca.get_account()
            else:
                base = self.config.alpaca_base_url
                acct_resp = await self._http.get(f"{base}/v2/account", headers=self._alpaca_headers)
                acct_resp.raise_for_status()
                account = acct_resp.json()
            portfolio_value = float(account.get("portfolio_value", 100000))
        except Exception as e:
            logger.warning(f"get_account failed ({e}), running in signal-only mode")
            signal_only = True

        import asyncio
        market_data = {}
        signals = {}
        loop = asyncio.get_event_loop()

        for symbol in symbols[:3]:
            closes = None

            if self._alpaca:
                closes = await fetch_closes_alpaca(symbol, self._alpaca)

            if closes is None:
                closes = await fetch_closes_stooq_http(symbol, self._http)

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
            logger.info(f"{symbol}: action={sig['action']} conf={sig['confidence']} rsi2={sig.get('rsi2')} ma50={sig.get('ma50')} | {sig['reason']}")

        if not market_data:
            return TaskResult(success=False, task_id=task_id, error="No market data fetched")

        buy_candidates = sorted(
            [(sym, sig) for sym, sig in signals.items()
             if sig["action"] == "buy" and sig["confidence"] >= 0.6],
            key=lambda x: x[1]["rsi2"],
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
                "reason": "No RSI2 buy signal (price below 200MA or RSI2 not oversold)"
            })

        best_sym, best_sig = buy_candidates[0]

        sentiment_ok = True
        claude_cost = 0.0
        try:
            summary = (f"Symbol: {best_sym}\nPrice: {market_data[best_sym]['close']:.2f}\n"
                       f"RSI2: {best_sig.get('rsi2')}\nMA50: {best_sig.get('ma50', 0):.2f}\n"
                       f"Signal: {best_sig['reason']}")
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

        notional = portfolio_value * 20.0 / 100
        if signal_only:
            logger.info(f"SIGNAL-ONLY: would buy {best_sym} ${notional:.2f} RSI2={best_sig.get('rsi2')}")
            return TaskResult(
                success=True, task_id=task_id,
                data={"signal": best_sig, "executed": False, "symbol": best_sym,
                      "notional": notional, "mode": "signal_only",
                      "reason": "Alpaca trading API unavailable (401) - signal logged only"},
                cost_usd=claude_cost,
            )
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
            logger.info(f"Executed: buy {best_sym} ${notional:.2f} RSI2={best_sig.get('rsi2')}")
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
