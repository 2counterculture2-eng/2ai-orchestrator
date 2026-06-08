"""
learning.py v1
SQLite-backed learning and logging system.
Stores: task history, agent performance, revenue records, error patterns.
"""
import sqlite3
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class LearningDB:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id     TEXT UNIQUE NOT NULL,
                    task_type   TEXT NOT NULL,
                    channel     TEXT,
                    status      TEXT DEFAULT 'pending',
                    input_data  TEXT,
                    result_data TEXT,
                    revenue_usd REAL DEFAULT 0,
                    error_msg   TEXT,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_stats (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_name  TEXT NOT NULL,
                    task_type   TEXT NOT NULL,
                    success     INTEGER DEFAULT 0,
                    failure     INTEGER DEFAULT 0,
                    total_cost_usd REAL DEFAULT 0,
                    avg_latency_ms REAL DEFAULT 0,
                    updated_at  TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS revenue_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id     TEXT,
                    channel     TEXT NOT NULL,
                    amount_usd  REAL NOT NULL,
                    currency    TEXT DEFAULT 'USD',
                    description TEXT,
                    recorded_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS error_patterns (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    error_type  TEXT NOT NULL,
                    error_msg   TEXT NOT NULL,
                    solution    TEXT,
                    occurrence  INTEGER DEFAULT 1,
                    last_seen   TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS system_events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type  TEXT NOT NULL,
                    payload     TEXT,
                    created_at  TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                CREATE INDEX IF NOT EXISTS idx_tasks_channel ON tasks(channel);
                CREATE INDEX IF NOT EXISTS idx_revenue_channel ON revenue_log(channel);
            """)

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ---- Task management ----

    def create_task(self, task_id: str, task_type: str, channel: str, input_data: dict) -> None:
        now = self._now()
        with self._conn() as c:
            c.execute(
                """INSERT OR IGNORE INTO tasks
                   (task_id, task_type, channel, status, input_data, created_at, updated_at)
                   VALUES (?,?,?,'pending',?,?,?)""",
                (task_id, task_type, channel, json.dumps(input_data), now, now),
            )

    def update_task(
        self,
        task_id: str,
        status: str,
        result_data: Optional[dict] = None,
        revenue_usd: float = 0.0,
        error_msg: Optional[str] = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE tasks SET status=?, result_data=?, revenue_usd=?, error_msg=?, updated_at=?
                   WHERE task_id=?""",
                (
                    status,
                    json.dumps(result_data) if result_data else None,
                    revenue_usd,
                    error_msg,
                    self._now(),
                    task_id,
                ),
            )

    def get_pending_tasks(self, task_type: Optional[str] = None) -> list[dict]:
        with self._conn() as c:
            if task_type:
                rows = c.execute(
                    "SELECT * FROM tasks WHERE status='pending' AND task_type=? ORDER BY created_at",
                    (task_type,),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM tasks WHERE status='pending' ORDER BY created_at"
                ).fetchall()
        return [dict(r) for r in rows]

    # ---- Revenue logging ----

    def log_revenue(self, channel: str, amount_usd: float, description: str, task_id: str = "") -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO revenue_log (task_id, channel, amount_usd, description, recorded_at)
                   VALUES (?,?,?,?,?)""",
                (task_id, channel, amount_usd, description, self._now()),
            )
        logger.info(f"Revenue logged: ${amount_usd:.2f} from {channel} — {description}")

    def get_monthly_revenue(self) -> dict[str, float]:
        month_start = datetime.now(timezone.utc).strftime("%Y-%m-01")
        with self._conn() as c:
            rows = c.execute(
                """SELECT channel, SUM(amount_usd) as total
                   FROM revenue_log WHERE recorded_at >= ?
                   GROUP BY channel""",
                (month_start,),
            ).fetchall()
        return {r["channel"]: r["total"] for r in rows}

    def get_total_revenue(self) -> float:
        with self._conn() as c:
            row = c.execute("SELECT SUM(amount_usd) as total FROM revenue_log").fetchone()
        return row["total"] or 0.0

    # ---- Agent stats ----

    def record_agent_result(
        self, agent_name: str, task_type: str, success: bool, cost_usd: float, latency_ms: float
    ) -> None:
        now = self._now()
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM agent_stats WHERE agent_name=? AND task_type=?",
                (agent_name, task_type),
            ).fetchone()
            if row:
                new_success = row["success"] + (1 if success else 0)
                new_failure = row["failure"] + (0 if success else 1)
                total = new_success + new_failure
                new_latency = (row["avg_latency_ms"] * (total - 1) + latency_ms) / total
                c.execute(
                    """UPDATE agent_stats SET success=?, failure=?, total_cost_usd=total_cost_usd+?,
                       avg_latency_ms=?, updated_at=? WHERE agent_name=? AND task_type=?""",
                    (new_success, new_failure, cost_usd, new_latency, now, agent_name, task_type),
                )
            else:
                c.execute(
                    """INSERT INTO agent_stats
                       (agent_name, task_type, success, failure, total_cost_usd, avg_latency_ms, updated_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (agent_name, task_type, 1 if success else 0, 0 if success else 1, cost_usd, latency_ms, now),
                )

    # ---- Error patterns ----

    def record_error(self, error_type: str, error_msg: str, solution: str = "") -> None:
        now = self._now()
        with self._conn() as c:
            row = c.execute(
                "SELECT id, occurrence FROM error_patterns WHERE error_type=? AND error_msg=?",
                (error_type, error_msg[:500]),
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE error_patterns SET occurrence=?, solution=?, last_seen=? WHERE id=?",
                    (row["occurrence"] + 1, solution, now, row["id"]),
                )
            else:
                c.execute(
                    """INSERT INTO error_patterns (error_type, error_msg, solution, last_seen)
                       VALUES (?,?,?,?)""",
                    (error_type, error_msg[:500], solution, now),
                )

    def get_known_solution(self, error_type: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute(
                "SELECT solution FROM error_patterns WHERE error_type=? AND solution != '' ORDER BY occurrence DESC LIMIT 1",
                (error_type,),
            ).fetchone()
        return row["solution"] if row else None

    # ---- System events ----

    def log_event(self, event_type: str, payload: dict = None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO system_events (event_type, payload, created_at) VALUES (?,?,?)",
                (event_type, json.dumps(payload or {}), self._now()),
            )

    # ---- Summary for LINE reports ----

    def build_weekly_summary(self) -> dict:
        week_start = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._conn() as c:
            total_tasks = c.execute("SELECT COUNT(*) as n FROM tasks").fetchone()["n"]
            completed = c.execute(
                "SELECT COUNT(*) as n FROM tasks WHERE status='completed'"
            ).fetchone()["n"]
            failed = c.execute(
                "SELECT COUNT(*) as n FROM tasks WHERE status='failed'"
            ).fetchone()["n"]
            monthly_rev = self.get_monthly_revenue()
            total_rev = self.get_total_revenue()
        return {
            "date": week_start,
            "tasks_total": total_tasks,
            "tasks_completed": completed,
            "tasks_failed": failed,
            "monthly_revenue_by_channel": monthly_rev,
            "total_revenue_usd": total_rev,
        }
