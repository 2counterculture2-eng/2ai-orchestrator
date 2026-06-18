"""
ibkr_worker.py v1
Interactive Brokers trading worker.
Uses IBKR Client Portal Web API (REST) via IB Gateway.
Supports paper and live accounts. Japanese and US stocks.
Auth: IB Gateway session (CP Gateway or headless Docker in production).
"""
import logging
import json
import httpx
import statistics
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict

from .base_worker import BaseWorker, TaskResult
from ..config import Config
from ..learning import LearningDB

logger = logging.getLogger(__name__)

# IB Gateway runs as a local proxy (or on same Railway network)
GATEWAY_BASE = "https://localhost:5000/v1/api"  # default CP Gateway port

SENTIMENT_SYSTEM = """You are a market sentiment analyst. Assess if mean reversion conditions are favorable.
Return JSON only: {"sentiment": "positive"|"neutral"|"negative", "risk_level": "low"|"medium"|"high", "note": "brief"}
Secondary filter only - primary signal is rule-based RSI2 mean reversion."""

MIN_BARS = 55


def compute_rsi(closes: List[float], period: int = 2) -> Optional[float]:
    """RSI on closes (newest first)."""
    if len(closes) < period + 1:
        return None
    p = list(reversed(closes[:period + 10]))
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
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)


def compute_ma(closes: List[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    return statistics.mean(closes[:period])


def rsi2_signal(symbol: str, closes: List[float]) -> Dict:
    """RSI2 mean reversion + MA50 trend filter."""
    ma50 = compute_ma(closes, 50)
    rsi2 = compute_rsi(closes, 2)
    price = closes[0] if closes else None
    if ma50 is None or rsi2 is None or price is None:
        return {"action": "hold", "confidence": 0.0, "reason": "insufficient data",
                "ma50": None, "rsi2": None, "price": price}
    above_ma = price > ma50
    action, confidence, parts = "hold", 0.0, []
    if above_ma:
        parts.append(f"price({price:.2f})>MA50({ma50:.2f})")
        if rsi2 < 5:
            action, confidence = "buy", 0.8
            parts.append(f"RSI2({rsi2:.1f})<5 OVERSOLD")
        elif rsi2 < 10:
            action, confidence = "buy", 0.65
            parts.append(f"RSI2({rsi2:.1f})<10 oversold")
        elif rsi2 > 65:
            action, confidence = "sell", 0.7
            parts.append(f"RSI2({rsi2:.1f})>65 exit")
        else:
            parts.append(f"RSI2({rsi2:.1f}) neutral")
    else:
        parts.append(f"price({price:.2f})<MA50({ma50:.2f}) bearish")
    return {"action": action, "confidence": round(confidence, 2),
            "reason": "; ".join(parts) or "no signal",
            "ma50": ma50, "rsi2": rsi2, "price": price}


class IBKRClient:
    """
    Thin wrapper around IBKR Client Portal Web API.
    Requires IB Gateway running (locally or as a sidecar).
    Paper account: set IBKR_PAPER=true in env.
    """

    def __init__(self, gateway_url: str, account_id: str, paper: bool = True):
        self.base = gateway_url.rstrip("/")
        self.account_id = account_id
        self.paper = paper
        # Gateway uses self-signed cert — disable SSL verify
        self._http = httpx.AsyncClient(verify=False, timeout=30,
                                       base_url=self.base)

    async def close(self):
        await self._http.aclose()

    async def get_account(self) -> dict:
        r = await self._http.get(f"/portfolio/{self.account_id}/summary")
        r.raise_for_status()
        return r.json()

    async def get_positions(self) -> list:
        r = await self._http.get(f"/portfolio/{self.account_id}/positions/0")
        r.raise_for_status()
        return r.json() or []

    async def search_contract(self, symbol: str, exchange: str = "SMART",
                               currency: str = "USD", sec_type: str = "STK") -> Optional[int]:
        """Resolve symbol to conid (contract ID)."""
        r = await self._http.get("/trsrv/stocks", params={"symbols": symbol})
        if r.status_code != 200:
            return None
        data = r.json()
        for entry in data.get(symbol, []):
            for contract in entry.get("contracts", []):
                if contract.get("currency") == currency:
                    return contract["conid"]
        return None

    async def get_historical_bars(self, conid: int, period: str = "1y",
                                  bar: str = "1d") -> Optional[List[float]]:
        """Fetch historical closes. Returns list newest-first."""
        params = {"conid": conid, "period": period, "bar": bar,
                  "outsideRth": False, "startTime": ""}
        r = await self._http.get("/iserver/marketdata/history", params=params)
        if r.status_code != 200:
            return None
        data = r.json()
        bars = data.get("data", [])
        if not bars:
            return None
        closes = [float(b["c"]) for b in reversed(bars)]  # newest first
        return closes if len(closes) >= MIN_BARS else None

    async def place_order(self, conid: int, side: str, quantity: float,
                          order_type: str = "MKT", tif: str = "DAY") -> dict:
        body = {
            "orders": [{
                "acctId": self.account_id,
                "conid": conid,
                "orderType": order_type,
                "side": side.upper(),
                "quantity": quantity,
                "tif": tif,
                "useAdaptive": False,
            }]
        }
        r = await self._http.post(f"/iserver/account/{self.account_id}/orders", json=body)
        r.raise_for_status()
        result = r.json()
        # Handle reply confirmation if required
        if isinstance(result, list) and result and result[0].get("id"):
            reply_id = result[0]["id"]
            reply = await self._http.post(f"/iserver/reply/{reply_id}",
                                          json={"confirmed": True})
            reply.raise_for_status()
            return reply.json()
        return result

    async def tickle(self):
        """Keep session alive (call periodically)."""
        try:
            await self._http.post("/tickle")
        except Exception:
            pass


class IBKRWorker(BaseWorker):
    worker_name = "ibkr"
    task_type = "trading"

    def __init__(self, config: Config, db: LearningDB):
        super().__init__(config, db)
        self._client: Optional[IBKRClient] = None
        if config.ibkr_gateway_url and config.ibkr_account_id:
            paper = config.ibkr_paper
            self._client = IBKRClient(
                gateway_url=config.ibkr_gateway_url,
                account_id=config.ibkr_account_id,
                paper=paper,
            )
        # Alpha Vantage for market data fallback
        self._av_key = config.alpha_vantage_api_key
        self._http = httpx.AsyncClient(timeout=30)

    async def close(self):
        if self._client:
            await self._client.close()
        await self._http.aclose()

    async def execute(self, task: dict) -> TaskResult:
        task_id = self.new_task_id()
        channel = task.get("channel", "ibkr")
        self.db.create_task(task_id, self.task_type, channel, task)
        try:
            if not self._client:
                result = TaskResult(success=False, task_id=task_id,
                                    error="IBKR not configured (set IBKR_GATEWAY_URL + IBKR_ACCOUNT_ID)")
            else:
                result = await self._run(task_id, task)
            status = "completed" if result.success else "failed"
            self.db.update_task(task_id, status,
                                result_data=result.data if isinstance(result.data, dict) else {},
                                revenue_usd=result.revenue_usd,
                                error_msg=result.error)
            if result.revenue_usd > 0:
                self.db.log_revenue(channel, result.revenue_usd, f"IBKR {task_id}", task_id)
            return result
        except Exception as e:
            logger.exception(f"IBKRWorker error {task_id}")
            self.db.update_task(task_id, "failed", error_msg=str(e))
            return TaskResult(success=False, task_id=task_id, error=str(e))

    async def _run(self, task_id: str, task: dict) -> TaskResult:
        action = task.get("action", "analyze")
        if action == "analyze":
            return await self._analyze_and_trade(task_id, task)
        elif action == "status":
            return await self._account_status(task_id)
        return TaskResult(success=False, task_id=task_id, error=f"Unknown action: {action}")

    async def _account_status(self, task_id: str) -> TaskResult:
        acct = await self._client.get_account()
        positions = await self._client.get_positions()
        return TaskResult(success=True, task_id=task_id, data={
            "account": acct,
            "positions": positions,
            "paper": self._client.paper,
        })

    async def _analyze_and_trade(self, task_id: str, task: dict) -> TaskResult:
        symbols = task.get("symbols", ["AAPL", "MSFT", "NVDA", "7203.T", "6758.T"])
        # Keep session alive
        await self._client.tickle()

        market_data, signals = {}, {}
        for symbol in symbols[:5]:
            closes = await self._fetch_closes(symbol)
            if closes is None:
                logger.warning(f"IBKR: no data for {symbol}")
                continue
            market_data[symbol] = {"close": closes[0], "days": len(closes)}
            sig = rsi2_signal(symbol, closes)
            signals[symbol] = sig
            logger.info(f"IBKR {symbol}: {sig['action']} rsi2={sig.get('rsi2')} ma50={sig.get('ma50')}")

        if not market_data:
            return TaskResult(success=False, task_id=task_id, error="No market data fetched")

        buy_candidates = sorted(
            [(s, sig) for s, sig in signals.items()
             if sig["action"] == "buy" and sig["confidence"] >= 0.6],
            key=lambda x: x[1]["rsi2"],
        )

        if not buy_candidates:
            return TaskResult(success=True, task_id=task_id, data={
                "signals": signals, "executed": False,
                "reason": "No RSI2 buy signal"
            })

        best_sym, best_sig = buy_candidates[0]

        # Claude sentiment filter
        sentiment_ok, claude_cost = True, 0.0
        try:
            summary = (f"Symbol: {best_sym}\nPrice: {market_data[best_sym]['close']:.2f}\n"
                       f"RSI2: {best_sig.get('rsi2')}\nSignal: {best_sig['reason']}")
            sent_text, claude_cost = self.call_claude(
                system=SENTIMENT_SYSTEM, user=summary,
                model=self.config.claude_haiku_model, max_tokens=100)
            import re as _re
            m = _re.search(r'\{.*\}', sent_text, _re.DOTALL)
            sentiment = json.loads(m.group(0) if m else sent_text.strip())
            if sentiment.get("sentiment") == "negative" and sentiment.get("risk_level") == "high":
                sentiment_ok = False
        except Exception as e:
            logger.warning(f"Sentiment skipped: {e}")

        if not sentiment_ok:
            return TaskResult(success=True, task_id=task_id, data={
                "signals": signals, "executed": False,
                "reason": "Blocked by sentiment filter"
            }, cost_usd=claude_cost)

        # Resolve contract
        conid = await self._client.search_contract(best_sym)
        if not conid:
            return TaskResult(success=True, task_id=task_id, data={
                "signals": signals, "executed": False,
                "reason": f"Could not resolve contract for {best_sym}"
            }, cost_usd=claude_cost)

        # Position sizing: 20% of notional $100k = $20k
        price = market_data[best_sym]["close"]
        notional = 20000.0
        quantity = round(notional / price, 2)

        try:
            order = await self._client.place_order(conid, "BUY", quantity)
            logger.info(f"IBKR order placed: BUY {best_sym} x{quantity} @ ~{price:.2f}")
            return TaskResult(success=True, task_id=task_id,
                              data={"signal": best_sig, "executed": True,
                                    "symbol": best_sym, "quantity": quantity,
                                    "notional": notional, "order": order,
                                    "paper": self._client.paper},
                              cost_usd=claude_cost)
        except httpx.HTTPStatusError as e:
            sc = e.response.status_code
            logger.warning(f"IBKR order {sc}: {e.response.text[:200]}")
            return TaskResult(success=True, task_id=task_id,
                              data={"signal": best_sig, "executed": False,
                                    "symbol": best_sym, "mode": "signal_only",
                                    "reason": f"Order API {sc}"},
                              cost_usd=claude_cost)

    async def _fetch_closes(self, symbol: str) -> Optional[List[float]]:
        """Fetch via IBKR historical data. Fallback to Alpha Vantage."""
        try:
            conid = await self._client.search_contract(symbol)
            if conid:
                closes = await self._client.get_historical_bars(conid)
                if closes:
                    logger.info(f"IBKR history {symbol}: {len(closes)} bars")
                    return closes
        except Exception as e:
            logger.warning(f"IBKR history {symbol} failed: {e}")

        # Fallback: Alpha Vantage (daily cache)
        if self._av_key:
            closes = await self._fetch_alpha_vantage(symbol)
            if closes:
                return closes
        return None

    async def _fetch_alpha_vantage(self, symbol: str) -> Optional[List[float]]:
        cache_dir = "/tmp/av_cache"
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        cache_file = f"{cache_dir}/{symbol}_{today}.json"
        try:
            os.makedirs(cache_dir, exist_ok=True)
            if os.path.exists(cache_file):
                with open(cache_file) as f:
                    closes = json.load(f)
                if len(closes) >= MIN_BARS:
                    return closes
        except Exception:
            pass
        try:
            r = await self._http.get(
                "https://www.alphavantage.co/query",
                params={"function": "TIME_SERIES_DAILY", "symbol": symbol,
                        "outputsize": "compact", "apikey": self._av_key}, timeout=20)
            if r.status_code != 200:
                return None
            ts = r.json().get("Time Series (Daily)", {})
            if not ts:
                return None
            closes = [float(v["4. close"]) for v in list(ts.values())]
            if len(closes) < MIN_BARS:
                return None
            with open(cache_file, "w") as f:
                json.dump(closes, f)
            return closes
        except Exception as e:
            logger.warning(f"Alpha Vantage {symbol}: {e}")
            return None
