"""
translation_worker.py v1
Translation and freelance automation worker.
Channels: Smartcat REST API, Upwork via GigRadar, Fiverr (internal API).
Flow: scan for jobs → filter by fit → apply/accept → translate with Claude → deliver → log revenue
"""
import asyncio
import logging
import json
import httpx
from typing import Optional

from .base_worker import BaseWorker, TaskResult
from ..config import Config
from ..learning import LearningDB

logger = logging.getLogger(__name__)

SMARTCAT_BASE = "https://smartcat.com/api/integration/v1"
GIGRADAR_BASE = "https://api.gigradar.io/v1"


TRANSLATION_SYSTEM = """You are an expert professional translator and technical writer.
Your translations must be:
- Accurate and faithful to the source meaning
- Natural in the target language (not literal/robotic)
- Consistent in terminology throughout the document
- Appropriately formal or casual based on context

You ONLY output the translated text. No explanations, no commentary, no wrapping."""

PROPOSAL_SYSTEM = """You are an expert freelance proposal writer optimizing for acceptance rate.
Write concise, specific, personalized proposals that:
- Reference the exact client need from their job post
- Lead with relevant experience and a concrete result
- Are under 150 words
- End with a clear call to action
Output ONLY the proposal text."""


class TranslationWorker(BaseWorker):
    worker_name = "translation"
    task_type = "translation"

    def __init__(self, config: Config, db: LearningDB):
        super().__init__(config, db)
        self._http = httpx.AsyncClient(timeout=30)

    async def close(self):
        await self._http.aclose()

    async def execute(self, task: dict) -> TaskResult:
        task_id = self.new_task_id()
        channel = task.get("channel", "unknown")
        self.db.create_task(task_id, self.task_type, channel, task)

        try:
            if channel == "smartcat":
                result = await self._handle_smartcat(task_id, task)
            elif channel == "upwork":
                result = await self._handle_upwork(task_id, task)
            elif channel == "fiverr":
                result = await self._handle_fiverr(task_id, task)
            elif channel == "direct_translation":
                result = await self._translate_text(task_id, task)
            else:
                result = TaskResult(success=False, error=f"Unknown channel: {channel}")

            status = "completed" if result.success else "failed"
            self.db.update_task(
                task_id, status,
                result_data=result.data if isinstance(result.data, dict) else {"output": str(result.data)},
                revenue_usd=result.revenue_usd,
                error_msg=result.error,
            )
            if result.revenue_usd > 0:
                self.db.log_revenue(channel, result.revenue_usd, f"Translation task {task_id}", task_id)
            return result

        except Exception as e:
            logger.exception(f"TranslationWorker error on task {task_id}")
            self.db.update_task(task_id, "failed", error_msg=str(e))
            self.db.record_error("translation_worker", str(e))
            return TaskResult(success=False, task_id=task_id, error=str(e))

    # ---- Direct translation ----

    async def _translate_text(self, task_id: str, task: dict) -> TaskResult:
        source_text = task.get("source_text", "")
        source_lang = task.get("source_lang", "English")
        target_lang = task.get("target_lang", "Japanese")
        domain = task.get("domain", "general")

        if not source_text:
            return TaskResult(success=False, task_id=task_id, error="No source_text provided")

        user_prompt = (
            f"Translate the following {source_lang} text to {target_lang}. Domain: {domain}.\n\n"
            f"SOURCE:\n{source_text}"
        )

        try:
            translated, cost = self.call_claude(
                system=TRANSLATION_SYSTEM,
                user=user_prompt,
                model=self.config.claude_sonnet_model,
                max_tokens=4096,
            )
            return TaskResult(
                success=True,
                task_id=task_id,
                data={"translated_text": translated, "source_lang": source_lang, "target_lang": target_lang},
                cost_usd=cost,
            )
        except Exception as e:
            return TaskResult(success=False, task_id=task_id, error=str(e))

    # ---- Smartcat ----

    async def _handle_smartcat(self, task_id: str, task: dict) -> TaskResult:
        """Scan Smartcat for available translation projects, accept and deliver."""
        if not self.config.smartcat_api_key:
            return TaskResult(success=False, task_id=task_id, error="Smartcat API key not configured")

        action = task.get("action", "scan")
        if action == "scan":
            return await self._smartcat_scan_projects(task_id)
        elif action == "deliver":
            return await self._smartcat_deliver(task_id, task)
        return TaskResult(success=False, task_id=task_id, error=f"Unknown Smartcat action: {action}")

    async def _smartcat_scan_projects(self, task_id: str) -> TaskResult:
        """Fetch available Smartcat projects for this account."""
        headers = {
            "Authorization": f"Basic {self.config.smartcat_api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = await self._http.get(
                f"{SMARTCAT_BASE}/project/list",
                headers=headers,
                params={"accountId": self.config.smartcat_account_id, "status": "inProgress"},
            )
            resp.raise_for_status()
            projects = resp.json()
            actionable = [p for p in projects if p.get("status") in ("inProgress", "created")]
            logger.info(f"Smartcat: found {len(actionable)} actionable projects")
            return TaskResult(
                success=True,
                task_id=task_id,
                data={"projects": actionable, "count": len(actionable)},
            )
        except httpx.HTTPStatusError as e:
            return TaskResult(success=False, task_id=task_id, error=f"Smartcat HTTP {e.response.status_code}")

    async def _smartcat_deliver(self, task_id: str, task: dict) -> TaskResult:
        """Mark a Smartcat document as delivered after translation."""
        doc_id = task.get("document_id")
        translated_text = task.get("translated_text", "")
        if not doc_id or not translated_text:
            return TaskResult(success=False, task_id=task_id, error="Missing document_id or translated_text")

        headers = {"Authorization": f"Basic {self.config.smartcat_api_key}"}
        try:
            resp = await self._http.put(
                f"{SMARTCAT_BASE}/document/{doc_id}/segments",
                headers=headers,
                json={"segments": [{"text": translated_text}]},
            )
            resp.raise_for_status()
            estimated_revenue = task.get("estimated_revenue_usd", 0.0)
            return TaskResult(
                success=True,
                task_id=task_id,
                data={"document_id": doc_id, "delivered": True},
                revenue_usd=estimated_revenue,
            )
        except httpx.HTTPStatusError as e:
            return TaskResult(success=False, task_id=task_id, error=f"Smartcat delivery failed: {e.response.status_code}")

    # ---- Upwork via GigRadar ----

    async def _handle_upwork(self, task_id: str, task: dict) -> TaskResult:
        if not self.config.gigradar_api_key:
            return TaskResult(success=False, task_id=task_id, error="GigRadar API key not configured")

        action = task.get("action", "scan")
        if action == "scan":
            return await self._upwork_scan_jobs(task_id)
        elif action == "apply":
            return await self._upwork_apply(task_id, task)
        return TaskResult(success=False, task_id=task_id, error=f"Unknown Upwork action: {action}")

    async def _upwork_scan_jobs(self, task_id: str) -> TaskResult:
        headers = {"Authorization": f"Bearer {self.config.gigradar_api_key}"}
        try:
            resp = await self._http.get(
                f"{GIGRADAR_BASE}/opportunities",
                headers=headers,
                params={
                    "categories": "translation,writing",
                    "minBudget": 50,
                    "sort": "newest",
                    "limit": 20,
                },
            )
            resp.raise_for_status()
            jobs = resp.json().get("opportunities", [])
            # Score each job with Claude Haiku (cheap)
            scored = await self._score_jobs(jobs)
            top_jobs = sorted(scored, key=lambda x: x["score"], reverse=True)[:5]
            return TaskResult(
                success=True,
                task_id=task_id,
                data={"top_jobs": top_jobs, "total_scanned": len(jobs)},
            )
        except httpx.HTTPStatusError as e:
            return TaskResult(success=False, task_id=task_id, error=f"GigRadar HTTP {e.response.status_code}")

    async def _score_jobs(self, jobs: list) -> list:
        """Score jobs by fit using Claude Haiku (cheapest model)."""
        scored = []
        for job in jobs:
            title = job.get("title", "")
            description = job.get("description", "")[:500]
            budget = job.get("budget", {})

            prompt = (
                f"Job: {title}\nBudget: {budget}\nDescription: {description}\n\n"
                f"Score this translation/writing job from 0-10 for an AI agent. "
                f"High score = high pay, clear requirements, short deadline, EN-JP translation. "
                f"Respond with ONLY a number 0-10."
            )
            try:
                score_text, _ = self.call_claude(
                    system="You are a job scoring agent. Output only a number 0-10.",
                    user=prompt,
                    model=self.config.claude_haiku_model,
                    max_tokens=5,
                    use_cache=False,
                )
                score = float(score_text.strip())
            except Exception:
                score = 5.0
            scored.append({**job, "score": score})
        return scored

    async def _upwork_apply(self, task_id: str, task: dict) -> TaskResult:
        job = task.get("job", {})
        job_id = job.get("id") or task.get("job_id")
        if not job_id:
            return TaskResult(success=False, task_id=task_id, error="No job_id provided")

        # Generate proposal with Claude Sonnet
        proposal_prompt = (
            f"Job title: {job.get('title', '')}\n"
            f"Client description: {job.get('description', '')[:800]}\n"
            f"Budget: {job.get('budget', 'not specified')}\n\n"
            f"Write a winning proposal for this translation/writing job."
        )
        proposal, cost = self.call_claude(
            system=PROPOSAL_SYSTEM,
            user=proposal_prompt,
            model=self.config.claude_sonnet_model,
            max_tokens=300,
        )

        # Submit via GigRadar
        headers = {"Authorization": f"Bearer {self.config.gigradar_api_key}"}
        try:
            resp = await self._http.post(
                f"{GIGRADAR_BASE}/opportunities/{job_id}/application",
                headers=headers,
                json={"coverLetter": proposal, "bidAmount": job.get("suggestedBid", 50)},
            )
            resp.raise_for_status()
            return TaskResult(
                success=True,
                task_id=task_id,
                data={"job_id": job_id, "proposal": proposal, "applied": True},
                cost_usd=cost,
            )
        except httpx.HTTPStatusError as e:
            return TaskResult(success=False, task_id=task_id, error=f"Upwork apply failed: {e.response.status_code}")

    # ---- Fiverr ----

    async def _handle_fiverr(self, task_id: str, task: dict) -> TaskResult:
        """Auto-respond to Fiverr order inquiries."""
        inquiry_text = task.get("inquiry_text", "")
        if not inquiry_text:
            return TaskResult(success=False, task_id=task_id, error="No inquiry_text provided")

        prompt = (
            f"Client inquiry: {inquiry_text}\n\n"
            f"Write a professional, friendly response that confirms you can do the job, "
            f"asks 1-2 clarifying questions if needed, and gives a timeline estimate. Under 100 words."
        )
        response, cost = self.call_claude(
            system="You are a professional freelance translator responding to client inquiries on Fiverr.",
            user=prompt,
            model=self.config.claude_haiku_model,
            max_tokens=200,
        )
        return TaskResult(
            success=True,
            task_id=task_id,
            data={"response_text": response},
            cost_usd=cost,
        )
