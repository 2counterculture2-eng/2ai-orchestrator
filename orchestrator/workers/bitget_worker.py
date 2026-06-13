"""
bitget_worker.py v1
Bitget SANDBOX-ONLY worker for algorithmic trading development/testing.
DO NOT use with real funds - paptrading: 1 header enforced at all times.
Strategy: RSI2 Mean Reversion + MA50 filter (same logic as Alpaca/GMO workers).
API: https://api.bitget.com + paptrading header + demo API key required.
Pairs: BTCUSDT, ETHUSDT.
"""
import base64
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

BITGET_BASE = "https://api.bitget.com"
SYMBOLS = ["BTCUSDT", "ETHUSDT"]
MIN_BARS = 55
POSITION_PCT = 0.20


def _sign_bitget(api_secret: str, timestamp: str, method: str, request_path: str, body: str = "") -> str:
    text = timestamp + method.upper() + request_path + body
    mac = hmac.new(api_secret.encode(), text.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


def _bitget_headers(api_key: str, api_secret: str, passphrase: str,
                    method: str, request_path: str, body: str = "") -> dict:
    ts = str(int(time.time() * 1000))
    return {
        "ACCESS-KEY": api_key,
        "ACCESS-SIGN": _sign_bitget(api_secret, ts, method, request_path, body),
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
        "paptrading": "1",  # SANDBOX MODE - always on
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
        parts.append(f"price({price:.2f})<MA50({ma50:.2f}) DOWNTREND")
        if rsi2 > 65:
            action, confidence = "sell", 0.6
            parts.append(f"RSI2({rsi2:.1f})>65 exit")
    return {"action": action, "confidence": confidence, "reason": "; ".join(parts),
            "ma50": ma50, "rsi2": rsi2, "price": price, "above_ma50": above}


class BitgetWorker(BaseWorker):
    worker_name = "bitget"
    task_type = "trading"

    def __init__(self, config: Config, db: LearningDB):
        super().__init__(config, db)
        self._http = httpx.AsyncClient(timeout=30)
        self._api_key = config.bitget_api_key
        self._api_secret = config.bitget_api_secret
        self._passphrase = config.bitget_passphrase

    async def close(self):
        await self._http.aclose()

    async def execute(self, task: dict) -> TaskResult:
        task_id = self.new_task_id()
        self.db.create_task(task_id, self.task_type, "bitget", task)
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
            return result
        except Exception as e:
            logger.exception(f"BitgetWorker error {task_id}")
            self.db.update_task(task_id, "failed", error_msg=str(e))
            return TaskResult(success=False, task_id=task_id, error=str(e))

    async def _account_status(self, task_id: str) -> TaskResult:
        if not self._api_key:
            return TaskResult(success=False, task_id=task_id, error="Bitget sandbox API key not configured")
        path = "/api/v2/spot/account/assets"
        headers = _bitget_headers(self._api_key, self._api_secret, self._passphrase, "GET", path)
        try:
            resp = await self._http.get(BITGET_BASE + path, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            assets = {a["coin"]: a for a in data.get("data", [])}
            usdt = float(assets.get("USDT", {}).get("available", 0))
            btc = float(assets.get("BTC", {}).get("available", 0))
            eth = float(assets.get("ETH", {}).get("available", 0))
            return TaskResult(success=True, task_id=task_id, data={
                "usdt_available": usdt, "btc": btc, "eth": eth,
                "mode": "SANDBOX", "assets": assets
            })
        except Exception as e:
            return TaskResult(success=False, task_id=task_id, error=str(e))

    async def _fetch_closes(self, symbol: str) -> Optional[List[float]]:
        try:
            resp = await self._http.get(
                f"{BITGET_BASE}/api/v2/spot/market/candles",
                params={"symbol": symbol, "granularity": "1day", "limit": "150"}
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
            if len(data) < MIN_BARS:
                logger.warning(f"Bitget candles {symbol}: {len(data)} bars")
                return None
            closes = [float(bar[4]) for bar in data]  # index 4 = close
            closes.reverse()  # newest first
            logger.info(f"Bitget candles {symbol}: {len(closes)} bars")
            return closes
        except Exception as e:
            logger.warning(f"Bitget candles failed {symbol}: {e}")
            return None

    async def _analyze_and_trade(self, task_id: str, task: dict) -> TaskResult:
        if not self._api_key:
            return TaskResult(success=False, task_id=task_id, error="Bitget sandbox API key not configured")

        signals = {}
        for sym in SYMBOLS:
            closes = await self._fetch_closes(sym)
            if closes is None:
                continue
            signals[sym] = rule_based_signal(sym, closes)
            logger.info(f"Bitget[SANDBOX] {sym}: {signals[sym]['action']} rsi2={signals[sym].get('rsi2')}")

        if not signals:
            return TaskResult(success=False, task_id=task_id, error="No market data from Bitget")

        # Get sandbox account balance
        path = "/api/v2/spot/account/assets"
        headers = _bitget_headers(self._api_key, self._api_secret, self._passphrase, "GET", path)
        try:
            resp = await self._http.get(BITGET_BASE + path, headers=headers)
            resp.raise_for_status()
            assets_data = resp.json().get("data", [])
            assets = {a["coin"]: a for a in assets_data}
            usdt_available = float(assets.get("USDT", {}).get("available", 0))
        except Exception as e:
            return TaskResult(success=False, task_id=task_id, error=f"Cannot get sandbox balance: {e}")

        buys = sorted(
            [(s, sig) for s, sig in signals.items()
             if sig["action"] == "buy" and sig["confidence"] >= 0.6],
            key=lambda x: x[1]["rsi2"]
        )

        if not buys:
            sells = [(s, sig) for s, sig in signals.items() if sig["action"] == "sell"]
            if sells:
                sym, sig = sells[0]
                base_coin = sym.replace("USDT", "")
                holding = float(assets.get(base_coin, {}).get("available", 0))
                if holding > 0:
                    order_result = await self._place_order(sym, "sell", str(round(holding, 6)), "base_coin")
                    return TaskResult(success=True, task_id=task_id, data={
                        "action": "sell", "symbol": sym, "size": holding,
                        "order": order_result, "executed": True,
                        "signals": signals, "mode": "SANDBOX"
                    })
            return TaskResult(success=True, task_id=task_id, data={
                "signals": signals, "executed": False,
                "reason": "No RSI2 buy signal", "usdt_available": usdt_available,
                "mode": "SANDBOX"
            })

        sym, sig = buys[0]
        budget_usdt = usdt_available * POSITION_PCT
        order_result = await self._place_order(sym, "buy", str(round(budget_usdt, 2)), "quote_coin")
        return TaskResult(success=True, task_id=task_id, data={
            "action": "buy", "symbol": sym, "budget_usdt": budget_usdt,
            "order": order_result, "executed": True,
            "signals": signals, "mode": "SANDBOX"
        })

    async def _place_order(self, symbol: str, side: str, size: str, size_type: str) -> dict:
        path = "/api/v2/spot/trade/place-order"
        body_dict = {
            "symbol": symbol,
            "side": side,
            "orderType": "market",
            "force": "gtc",
            size_type: size,
        }
        body = json.dumps(body_dict)
        headers = _bitget_headers(self._api_key, self._api_secret, self._passphrase, "POST", path, body)
        try:
            resp = await self._http.post(BITGET_BASE + path, headers=headers, content=body)
            resp.raise_for_status()
            result = resp.json()
            logger.info(f"Bitget[SANDBOX] order {side} {symbol} {size}: {result}")
            return result
        except Exception as e:
            logger.error(f"Bitget sandbox order failed: {e}")
            return {"error": str(e)}
