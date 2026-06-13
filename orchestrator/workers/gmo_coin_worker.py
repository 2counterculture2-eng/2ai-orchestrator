"""
gmo_coin_worker.py v1
GMO Coin spot trading worker (LIVE - FSA registered Japanese exchange).
Strategy: RSI2 Mean Reversion + MA50 filter (same as Alpaca worker).
API: https://api.coin.z.com - HMAC-SHA256 authentication.
Exchange mode only (board trading, not dealer mode - no conflict of interest).
Pairs: BTC, ETH (primary).
"""
import hashlib
import hmac
import json
import logging
import statistics
import time
from typing import Dict, List, Optional

import httpx

from .base_worker import BaseWorker, TaskResult
from ..config import Config
from ..learning import LearningDB

logger = logging.getLogger(__name__)

GMO_PUBLIC = "https://api.coin.z.com/public/v1"
GMO_PRIVATE = "https://api.coin.z.com/private/v1"

SYMBOLS = ["BTC", "ETH"]
MIN_BARS = 55
POSITION_PCT = 0.20


def _sign(api_secret: str, timestamp: str, method: str, path: str, body: str = "") -> str:
    text = timestamp + method + path + body
    return hmac.new(api_secret.encode(), text.encode(), hashlib.sha256).hexdigest()


def _private_headers(api_key: str, api_secret: str, method: str, path: str, body: str = "") -> dict:
    ts = str(int(time.time() * 1000))
    return {
        "API-KEY": api_key,
        "API-TIMESTAMP": ts,
        "API-SIGN": _sign(api_secret, ts, method, path, body),
        "Content-Type": "application/json",
    }


def compute_ma(prices: List[float], period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    return statistics.mean(prices[:period])


def compute_rsi(prices: List[float], period: int = 2) -> Optional[float]:
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
    ma50 = compute_ma(closes, 50)
    rsi2 = compute_rsi(closes, 2)
    price = closes[0] if closes else None
    if ma50 is None or rsi2 is None or price is None:
        return {"action": "hold", "confidence": 0.0, "reason": "insufficient data",
                "ma50": None, "rsi2": None, "price": price, "above_ma50": False}
    above = price > ma50
    action, confidence, parts = "hold", 0.0, []
    if above:
        parts.append(f"price({price:.0f})>MA50({ma50:.0f})")
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
        parts.append(f"price({price:.0f})<MA50({ma50:.0f}) DOWNTREND")
        if rsi2 > 65:
            action, confidence = "sell", 0.6
            parts.append(f"RSI2({rsi2:.1f})>65 exit")
    return {"action": action, "confidence": confidence, "reason": "; ".join(parts),
            "ma50": ma50, "rsi2": rsi2, "price": price, "above_ma50": above}


class GmoCoinWorker(BaseWorker):
    worker_name = "gmo_coin"
    task_type = "trading"

    def __init__(self, config: Config, db: LearningDB):
        super().__init__(config, db)
        self._http = httpx.AsyncClient(timeout=30)
        self._api_key = config.gmo_coin_api_key
        self._api_secret = config.gmo_coin_api_secret

    async def close(self):
        await self._http.aclose()

    async def execute(self, task: dict) -> TaskResult:
        task_id = self.new_task_id()
        self.db.create_task(task_id, self.task_type, "gmo_coin", task)
        try:
            action = task.get("action", "analyze")
            if action == "analyze":
                result = await self._analyze_and_trade(task_id, task)
            elif action == "status":
                result = await self._account_status(task_id)
            else:
                result = TaskResult(success=False, task_id=task_id, error=f"Unknown action: {action}")
            status = "completed" if result.success else "failed"
            self.db.update_task(task_id, status,
                                result_data=result.data if isinstance(result.data, dict) else {},
                                revenue_usd=result.revenue_usd, error_msg=result.error)
            if result.revenue_usd > 0:
                self.db.log_revenue("gmo_coin", result.revenue_usd, f"Trade {task_id}", task_id)
            return result
        except Exception as e:
            logger.exception(f"GmoCoinWorker error {task_id}")
            self.db.update_task(task_id, "failed", error_msg=str(e))
            return TaskResult(success=False, task_id=task_id, error=str(e))

    async def _account_status(self, task_id: str) -> TaskResult:
        if not self._api_key:
            return TaskResult(success=False, task_id=task_id, error="GMO Coin API key not configured")
        path = "/v1/account/assets"
        headers = _private_headers(self._api_key, self._api_secret, "GET", path)
        try:
            resp = await self._http.get(GMO_PRIVATE + "/account/assets", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            assets = {a["symbol"]: a for a in data.get("data", [])}
            jpy = float(assets.get("JPY", {}).get("available", 0))
            btc = float(assets.get("BTC", {}).get("amount", 0))
            eth = float(assets.get("ETH", {}).get("amount", 0))
            return TaskResult(success=True, task_id=task_id, data={
                "jpy_available": jpy, "btc": btc, "eth": eth,
                "assets": {k: v for k, v in assets.items()}
            })
        except Exception as e:
            return TaskResult(success=False, task_id=task_id, error=str(e))

    async def _fetch_closes(self, symbol: str) -> Optional[List[float]]:
        try:
            resp = await self._http.get(
                f"{GMO_PUBLIC}/klines",
                params={"symbol": symbol, "interval": "1day", "limit": 150}
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
            if len(data) < MIN_BARS:
                logger.warning(f"GMO Coin klines {symbol}: {len(data)} bars")
                return None
            closes = [float(bar["close"]) for bar in reversed(data)]
            logger.info(f"GMO Coin klines {symbol}: {len(closes)} bars")
            return closes
        except Exception as e:
            logger.warning(f"GMO Coin klines failed {symbol}: {e}")
            return None

    async def _get_ticker_price(self, symbol: str) -> Optional[float]:
        try:
            resp = await self._http.get(f"{GMO_PUBLIC}/ticker", params={"symbol": symbol})
            resp.raise_for_status()
            data = resp.json().get("data", [])
            if data:
                return float(data[0].get("last", 0))
            return None
        except Exception:
            return None

    async def _analyze_and_trade(self, task_id: str, task: dict) -> TaskResult:
        if not self._api_key:
            return TaskResult(success=False, task_id=task_id, error="GMO Coin API key not configured")

        signals = {}
        for sym in SYMBOLS:
            closes = await self._fetch_closes(sym)
            if closes is None:
                continue
            signals[sym] = rule_based_signal(sym, closes)
            logger.info(f"GMO {sym}: {signals[sym]['action']} rsi2={signals[sym].get('rsi2')}")

        if not signals:
            return TaskResult(success=False, task_id=task_id, error="No market data from GMO Coin")

        # Get account balance
        path = "/v1/account/assets"
        headers = _private_headers(self._api_key, self._api_secret, "GET", path)
        try:
            resp = await self._http.get(GMO_PRIVATE + "/account/assets", headers=headers)
            resp.raise_for_status()
            assets_data = resp.json().get("data", [])
            jpy_available = float(next((a["available"] for a in assets_data if a["symbol"] == "JPY"), 0))
        except Exception as e:
            return TaskResult(success=False, task_id=task_id, error=f"Cannot get balance: {e}")

        # Find best buy signal
        buys = sorted(
            [(s, sig) for s, sig in signals.items()
             if sig["action"] == "buy" and sig["confidence"] >= 0.6],
            key=lambda x: x[1]["rsi2"]
        )

        if not buys:
            # Check sells
            sells = [(s, sig) for s, sig in signals.items() if sig["action"] == "sell"]
            if sells:
                sym, sig = sells[0]
                # Check current holdings
                holdings = {a["symbol"]: float(a["amount"]) for a in assets_data}
                holding_qty = holdings.get(sym, 0)
                if holding_qty > 0:
                    order_result = await self._place_order(sym, "SELL", size=str(holding_qty))
                    return TaskResult(success=True, task_id=task_id, data={
                        "action": "sell", "symbol": sym, "size": holding_qty,
                        "order": order_result, "executed": True, "signals": signals
                    })
            return TaskResult(success=True, task_id=task_id, data={
                "signals": signals, "executed": False,
                "reason": "No RSI2 buy signal", "jpy_available": jpy_available
            })

        sym, sig = buys[0]
        # Buy with 20% of JPY balance
        budget_jpy = jpy_available * POSITION_PCT
        price = await self._get_ticker_price(sym)
        if not price or price <= 0:
            return TaskResult(success=False, task_id=task_id, error=f"Cannot get {sym} price")

        size = budget_jpy / price
        # GMO Coin minimum: BTC 0.0001, ETH 0.01
        min_size = 0.0001 if sym == "BTC" else 0.01
        size = round(max(size, min_size), 6)

        order_result = await self._place_order(sym, "BUY", size=str(size))
        return TaskResult(success=True, task_id=task_id, data={
            "action": "buy", "symbol": sym, "size": size, "price_approx": price,
            "budget_jpy": budget_jpy, "order": order_result,
            "executed": order_result.get("status") == "0",
            "signals": signals
        })

    async def _place_order(self, symbol: str, side: str, size: str) -> dict:
        path = "/v1/order"
        body_dict = {
            "symbol": symbol,
            "side": side,
            "executionType": "MARKET",
            "size": size,
        }
        body = json.dumps(body_dict)
        headers = _private_headers(self._api_key, self._api_secret, "POST", path, body)
        try:
            resp = await self._http.post(GMO_PRIVATE + "/order", headers=headers, content=body)
            resp.raise_for_status()
            result = resp.json()
            logger.info(f"GMO Coin order {side} {symbol} {size}: {result}")
            return result
        except Exception as e:
            logger.error(f"GMO Coin order failed: {e}")
            return {"error": str(e)}
