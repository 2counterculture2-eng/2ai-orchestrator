"""
orchestrator_core.py v2
Main orchestrator: routes tasks to workers, manages schedules, handles self-repair.
Principle: always asks 'does this put money in Takuma-san account?'

Schedule:
  - Trading loop: every 30 min during US market hours (Mon-Fri 13:30-21:00 UTC)
  - Slow loop: every 4 hours for translation scans (only if API keys set) + weekly report
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import anthropic

from .config import Config
from .learning import LearningDB
from .line_bot import LineBot
from .workers import TranslationWorker, TradingWorker, TaskResult

logger = logging.getLogger(__name__)

ROUTER_SYSTEM = """You are the routing brain of an autonomous AI income system.
Given a task description, classify it into exactly one of:
  translation_smartcat, translation_upwork, translation_fiverr, translation_direct,
  trading_alpaca, trading_oanda, trading_freqtrade,
  system_status, system_report, unknown

Output ONLY the class name. No other text."""

SELF_HEAL_SYSTEM = """You are a self-healing agent for an autonomous AI income system.
Given an error, propose a concrete fix.
Output JSON: {"solution": "...", "action": "retry|skip|escalate"}"""

MARKET_OPEN_MINUTES_UTC = 13 * 60 + 30
MARKET_CLOSE_MINUTES_UTC = 21 * 60

TRADE_SYMBOLS = ["AAPL", "MSFT", "NVDA", "SPY", "QQQ", "META", "GOOGL"]
TRADE_INTERVAL_SECONDS = 30 * 60


def _is_us_market_open() -> bool:
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    current_minutes = now.hour * 60 + now.minute
    return MARKET_OPEN_MINUTES_UTC <= current_minutes < MARKET_CLOSE_MINUTES_UTC


class OrchestratorCore:
    def __init__(self, config: Config, db: LearningDB, line: LineBot):
        self.config = config
        self.db = db
        self.line = line
        self.claude = anthropic.Anthropic(api_key=config.anthropic_api_key)
        self.translation_worker = TranslationWorker(config, db)
        self.trading_worker = TradingWorker(config, db)
        self._running = False
        self._task_queue: asyncio.Queue = asyncio.Queue()
        self._worker_semaphore = asyncio.Semaphore(10)

    async def start(self):
        self._running = True
        logger.info("OrchestratorCore v2 starting")
        self.db.log_event("orchestrator_start", {"version": "v2"})
        asyncio.create_task(self._task_processor())
        asyncio.create_task(self._trading_loop())
        asyncio.create_task(self._slow_loop())
        logger.info("OrchestratorCore v2 started")

    async def stop(self):
        self._running = False
        await self.translation_worker.close()
        await self.trading_worker.close()
        await self.line.close()
        logger.info("OrchestratorCore stopped")

    async def handle_line_command(self, command, args, reply_token):
        if command == "report":
            summary = self.db.build_weekly_summary()
            await self.line.send_weekly_report(summary)
        elif command == "status":
            status = await self._system_status()
            await self.line.reply(reply_token, status)
        elif command == "pause":
            self._running = False
            await self.line.reply(reply_token, "システムを一時停止しました。")
        elif command == "resume":
            self._running = True
            asyncio.create_task(self._trading_loop())
            asyncio.create_task(self._slow_loop())
            await self.line.reply(reply_token, "システムを再開しました。")
        else:
            resp = await self._interpret_free_text(args or command)
            await self.line.reply(reply_token, resp)

    async def enqueue_task(self, task):
        task_id = str(uuid.uuid4())
        task["_queued_id"] = task_id
        await self._task_queue.put(task)
        return task_id

    async def _task_processor(self):
        while True:
            try:
                task = await asyncio.wait_for(self._task_queue.get(), timeout=5.0)
                asyncio.create_task(self._run_task_safe(task))
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                logger.exception(f"Task processor error: {e}")

    async def _trading_loop(self):
        """Run Alpaca trade analysis every 30 min during US market hours."""
        logger.info("Trading loop started (30-min interval, market hours only)")
        while self._running:
            try:
                if self.config.alpaca_email and _is_us_market_open():
                    logger.info("Market open -- enqueuing trade analysis")
                    await self.enqueue_task({
                        "type": "trading",
                        "channel": "alpaca",
                        "action": "analyze",
                        "symbols": TRADE_SYMBOLS,
                    })
                else:
                    now = datetime.now(timezone.utc)
                    logger.debug(
                        f"Skipping trade: market_open={_is_us_market_open()}, "
                        f"alpaca={bool(self.config.alpaca_email)}, "
                        f"utc={now.hour}:{now.minute:02d}"
                    )
            except Exception as e:
                logger.exception(f"Trading loop error: {e}")
            await asyncio.sleep(TRADE_INTERVAL_SECONDS)

    async def _slow_loop(self):
        """Every 4 hours: translation scans (if configured) + weekly report."""
        logger.info("Slow loop started (4-hour interval)")
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                now_hour_jst = (now.hour + 9) % 24
                if self.config.smartcat_api_key:
                    await self.enqueue_task({"type": "translation", "channel": "smartcat", "action": "scan"})
                if self.config.gigradar_api_key:
                    await self.enqueue_task({"type": "translation", "channel": "upwork", "action": "scan"})
                if now.weekday() == self.config.weekly_report_day and now_hour_jst == self.config.weekly_report_hour:
                    summary = self.db.build_weekly_summary()
                    await self.line.send_weekly_report(summary)
                    self.db.log_event("weekly_report_sent", summary)
            except Exception as e:
                logger.exception(f"Slow loop error: {e}")
            await asyncio.sleep(4 * 3600)

    async def _run_task_safe(self, task):
        async with self._worker_semaphore:
            try:
                return await self._dispatch_task(task)
            except Exception as e:
                logger.exception(f"Task failed: {task}")
                await self._self_heal(task, str(e))
                return None

    async def _dispatch_task(self, task):
        channel = task.get("channel", "")
        task_type = task.get("type", "")
        if task_type == "translation" or channel in ("smartcat", "upwork", "fiverr", "direct_translation"):
            return await self.translation_worker.execute(task)
        if task_type == "trading" or channel in ("alpaca", "oanda", "freqtrade"):
            return await self.trading_worker.execute(task)
        routed = self._route_task(task.get("description", str(task)))
        task["channel"] = routed.split("_", 1)[-1] if "_" in routed else routed
        task["type"] = routed.split("_", 1)[0] if "_" in routed else "unknown"
        return await self._dispatch_task(task)

    def _route_task(self, description):
        try:
            resp = self.claude.messages.create(
                model=self.config.claude_haiku_model,
                max_tokens=20,
                system=ROUTER_SYSTEM,
                messages=[{"role": "user", "content": description}],
            )
            return resp.content[0].text.strip()
        except Exception:
            return "unknown"

    async def _self_heal(self, failed_task, error, attempt=0):
        if attempt >= 3:
            await self.line.send_alert("繰り返しエラー", f"タスク: {failed_task}\nエラー: {error[:300]}")
            return
        known_solution = self.db.get_known_solution(type(error).__name__)
        if known_solution:
            logger.info(f"Self-heal: applying known solution: {known_solution}")
            return
        prompt = f"Error: {error[:500]}\nTask: {str(failed_task)[:300]}\n\nPropose a fix."
        try:
            resp = self.claude.messages.create(
                model=self.config.claude_haiku_model, max_tokens=300, system=SELF_HEAL_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            import json
            data = json.loads(resp.content[0].text)
            solution = data.get("solution", "")
            action = data.get("action", "skip")
            self.db.record_error(type(error).__name__, error[:300], solution)
            if action == "retry":
                await asyncio.sleep(5 * (attempt + 1))
                await self._run_task_safe(failed_task)
            elif action == "escalate":
                await self.line.send_alert("エスカレーション", f"解決策: {solution}\n\nタスク: {failed_task}")
        except Exception as e:
            logger.error(f"Self-heal failed: {e}")

    async def _system_status(self):
        pending = len(self.db.get_pending_tasks())
        monthly = self.db.get_monthly_revenue()
        rev_str = ", ".join(f"{k}: ${v:.2f}" for k, v in monthly.items()) or "なし"
        market_status = "開場中" if _is_us_market_open() else "閉場中"
        return (
            f"システム稼働中 (v2)\n"
            f"米国市場: {market_status}\n"
            f"待機タスク: {pending}\n"
            f"今月の収益: {rev_str}\n"
            f"累計: ${self.db.get_total_revenue():.2f}"
        )

    async def _interpret_free_text(self, text):
        try:
            resp = self.claude.messages.create(
                model=self.config.claude_haiku_model, max_tokens=300,
                system="You are the AI assistant for an autonomous income system. Answer briefly in Japanese.",
                messages=[{"role": "user", "content": text}],
            )
            return resp.content[0].text
        except Exception:
            return "申し訳ありません、処理できませんでした。"