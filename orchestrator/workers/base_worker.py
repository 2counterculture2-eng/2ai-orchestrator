"""
base_worker.py v1
Abstract base class for all worker agents.
Each worker wraps Claude API calls with cost tracking, retry logic, and result normalization.
"""
import time
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional
import anthropic

from ..config import Config
from ..learning import LearningDB

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF = [2, 5, 10]  # seconds


@dataclass
class TaskResult:
    success: bool
    data: Any = None
    revenue_usd: float = 0.0
    cost_usd: float = 0.0
    error: Optional[str] = None
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class BaseWorker(ABC):
    """All workers inherit this. Provides: Claude call, retry, cost tracking."""

    worker_name: str = "base"
    task_type: str = "generic"

    def __init__(self, config: Config, db: LearningDB):
        self.config = config
        self.db = db
        self.client = anthropic.Anthropic(api_key=config.anthropic_api_key) if config.anthropic_api_key else None

    @abstractmethod
    async def execute(self, task: dict) -> TaskResult:
        """Execute a task. Override in subclasses."""

    def call_claude(
        self,
        system: str,
        user: str,
        model: Optional[str] = None,
        max_tokens: int = 2048,
        use_cache: bool = True,
    ) -> tuple[str, float]:
        """
        Synchronous Claude API call with prompt caching.
        Returns (response_text, cost_usd).
        """
        model = model or self.config.claude_haiku_model
        start = time.time()

        system_content = (
            [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            if use_cache
            else system
        )

        for attempt in range(MAX_RETRIES):
            try:
                resp = self.client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system_content,
                    messages=[{"role": "user", "content": user}],
                )
                latency_ms = (time.time() - start) * 1000
                cost = self._estimate_cost(model, resp.usage.input_tokens, resp.usage.output_tokens)
                text = resp.content[0].text if resp.content else ""
                self.db.record_agent_result(self.worker_name, self.task_type, True, cost, latency_ms)
                return text, cost
            except anthropic.RateLimitError:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF[attempt])
                    continue
                raise
            except anthropic.APIError as e:
                logger.error(f"{self.worker_name}: Claude API error: {e}")
                self.db.record_error("claude_api_error", str(e))
                raise

        return "", 0.0

    def _estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        # Prices per 1M tokens (USD), as of 2025
        prices = {
            "claude-haiku-4-5-20251001":   (0.80,  4.00),
            "claude-sonnet-4-6":           (3.00, 15.00),
            "claude-opus-4-8":            (15.00, 75.00),
        }
        for key, (inp_price, out_price) in prices.items():
            if key in model:
                return (input_tokens * inp_price + output_tokens * out_price) / 1_000_000
        return 0.0

    def new_task_id(self) -> str:
        return f"{self.worker_name}-{uuid.uuid4().hex[:8]}"
