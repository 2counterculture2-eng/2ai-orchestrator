"""
orchestrator_core.py v4
Main orchestrator with DevAgent integration for LINE-based Claude Code sessions.
All strings ASCII/English only (Rule 55).

Schedule:
  - Trading loop: every 30 min during US market hours (Mon-Fri 13:30-21:00 UTC)
  - Slow loop: every 4 hours, translation scans only if API keys set
LINE commands:
  - status / progress queries -> system_status
  - report -> weekly report
  - pause / resume -> control system
  - everything else -> DevAgent (Claude Opus with GitHub/Railway tools)
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
from .dev_agent import DevAgent

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

STATUS_KEYWORDS = [
    "status", "progress", "roadmap", "current", "now",
    "report", "revenue", "income", "how much", "trade",
    "where are we", "what happened",
]


MAX_HISTORY = 10


def _is_us_market_open() -> bool:
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    current_minutes = now.hour * 60 + now.minute
    return MARKET_OPEN_MINUTES_UTC <= current_minutes < MARKET_CLOSE_MINUTES_UTC


def _is_status_query(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in STATUS_KEYWORDS)


class OrchestratorCore:
    def __init__(self, config: Config, db: LearningDB, line: LineBot):
        self.config = config
        self.db = db
        self.line = line
        self.claude = anthropic.Anthropic(api_key=config.anthropic_api_key)
        self.translation_worker = TranslationWorker(config, db)
        self.trading_worker = TradingWorker(config, db)
        self.dev_agent = DevAgent(self.claude, db)
        self._running = False
        self._task_queue: asyncio.Queue = asyncio.Queue()
        self._worker_semaphore = asyncio.Semaphore(10)

    async def start(self):
        self._running = True
        logger.info("OrchestratorCore v4 starting")
        self.db.log_event("orchestrator_start", {"version": "v4"})
        asyncio.create_task(self._task_processor())
        asyncio.create_task(self._trading_loop())
        asyncio.create_task(self._slow_loop())
        logger.info("OrchestratorCore v4 started")

    async def stop(self):
        self._running = False
        await self.translation_worker.close()
        await self.trading_worker.close()
        await self.dev_agent.close()
        await self.line.close()
        logger.info("OrchestratorCore stopped")

    async def handle_line_command(self, command, args, reply_token):
        full_text = (args or command or "").strip()
        cmd = command.lower()

        if cmd in ("report",):
            summary = self.db.build_weekly_summary()
            await self.line.send_weekly_report(summary)

        elif cmd in ("status",) or _is_status_query(full_text):
            status = await self._system_status()
            await self.line.reply(reply_token, status)

        elif cmd == "pause":
            self._running = False
            await self.line.reply(reply_token, "System paused.")

        elif cmd == "resume":
            self._running = True
            asyncio.create_task(self._trading_loop())
            asyncio.create_task(self._slow_loop())
            await self.line.reply(reply_token, "System resumed.")

        elif cmd == "help":
            help_text = (
                "2AI Orchestrator - available commands:\n"
                "status - system status\n"
                "report - weekly report\n"
                "pause / resume - control system\n"
                "Any other message -> Claude Code agent (can edit code, deploy, etc.)"
            )
            await self.line.reply(reply_token, help_text)

        else:
            # Route to DevAgent v2 (history managed internally via GitHub)
            await self.line.reply(reply_token, "Processing...")
            response = await self.dev_agent.run(full_text)
            uid = self.db.get_config("line_user_id") or self.config.line_user_id
            if len(response) <= 2000:
                await self.line.send_text(response, user_id=uid)
            else:
                for i in range(0, len(response), 2000):
                    await self.line.send_text(response[i:i+2000], user_id=uid)
                    await asyncio.sleep(0.5)

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
        logger.info("Trading loop started (30-min interval, market hours only)")
        while self._running:
            try:
                if self.config.alpaca_email and _is_us_market_open():
                    logger.info("Market open -- enqueuing trade analysis")
                    await self.enqueue_task({
                        "type": "trading", "channel": "alpaca",
                        "action": "analyze", "symbols": TRADE_SYMBOLS,
                    })
            except Exception as e:
                logger.exception(f"Trading loop error: {e}")
            await asyncio.sleep(TRADE_INTERVAL_SECONDS)

    async def _slow_loop(self):
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
                model=self.config.claude_haiku_model, max_tokens=20,
                system=ROUTER_SYSTEM,
                messages=[{"role": "user", "content": description}],
            )
            return resp.content[0].text.strip()
        except Exception:
            return "unknown"

    async def _self_heal(self, failed_task, error, attempt=0):
        if attempt >= 3:
            uid = self.db.get_config("line_user_id") or self.config.line_user_id
            if uid:
                await self.line.send_text(f"Repeated error - check needed\nTask: {failed_task}\nError: {error[:200]}", user_id=uid)
            return
        known_solution = self.db.get_known_solution(type(error).__name__)
        if known_solution:
            logger.info(f"Self-heal: known solution: {known_solution}")
            return
        try:
            resp = self.claude.messages.create(
                model=self.config.claude_haiku_model, max_tokens=300, system=SELF_HEAL_SYSTEM,
                messages=[{"role": "user", "content": f"Error: {error[:500]}\nTask: {str(failed_task)[:300]}\n\nPropose a fix."}],
            )
            import json
            data = json.loads(resp.content[0].text)
            solution = data.get("solution", "")
            action = data.get("action", "skip")
            self.db.record_error(type(error).__name__, error[:300], solution)
            if action == "retry":
                await asyncio.sleep(5 * (attempt + 1))
                await self._run_task_safe(failed_task)
        except Exception as e:
            logger.error(f"Self-heal failed: {e}")

    async def _system_status(self) -> str:
        summary = self.db.build_weekly_summary()
        pending = self._task_queue.qsize()
        monthly = self.db.get_monthly_revenue()
        total_rev = self.db.get_total_revenue()
        tasks_ok = summary.get("tasks_completed", 0)
        tasks_fail = summary.get("tasks_failed", 0)
        tasks_total = summary.get("tasks_total", 0)
        market_status = "OPEN" if _is_us_market_open() else "CLOSED"
        rev_str = "\n".join(f"  {k}: ${v:.2f}" for k, v in monthly.items()) if monthly else "  none"
        return (
            f"=== 2AI Orchestrator v4 ===\n"
            f"Status: running\n"
            f"US Market: {market_status}\n"
            f"Today: {tasks_ok} ok / {tasks_fail} fail / {tasks_total} total\n"
            f"Monthly revenue:\n{rev_str}\n"
            f"Total: ${total_rev:.2f}\n"
            f"Queue: {pending}\n"
            f"---\n"
            f"Trading: Alpaca paper (30min, market hours)\n"
            f"Translation: waiting for API keys"
        )