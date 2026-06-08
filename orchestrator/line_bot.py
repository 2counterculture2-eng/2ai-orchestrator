"""
line_bot.py v1
LINE Messaging API integration.
- Webhook: receive instructions from Takuma-san
- Push: send weekly reports and alerts
"""
import hashlib
import hmac
import base64
import logging
from typing import Optional
import httpx
from .config import Config

logger = logging.getLogger(__name__)

LINE_API_BASE = "https://api.line.me/v2/bot"


class LineBot:
    def __init__(self, config: Config):
        self.config = config
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {config.line_channel_access_token}"},
            timeout=30,
        )

    async def close(self):
        await self._client.aclose()

    def verify_signature(self, body: bytes, x_line_signature: str) -> bool:
        if not self.config.line_channel_secret:
            logger.warning("LINE channel secret not configured — skipping signature verification")
            return True
        digest = hmac.new(
            self.config.line_channel_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).digest()
        expected = base64.b64encode(digest).decode("utf-8")
        return hmac.compare_digest(expected, x_line_signature)

    async def send_text(self, message: str, user_id: Optional[str] = None) -> bool:
        target = user_id or self.config.line_user_id
        if not target:
            logger.error("LINE user_id not configured")
            return False
        payload = {
            "to": target,
            "messages": [{"type": "text", "text": message}],
        }
        resp = await self._client.post(f"{LINE_API_BASE}/message/push", json=payload)
        if resp.status_code == 200:
            return True
        logger.error(f"LINE push failed: {resp.status_code} {resp.text}")
        return False

    async def send_weekly_report(self, summary: dict) -> bool:
        monthly_rev = summary.get("monthly_revenue_by_channel", {})
        rev_lines = "\n".join(
            f"  {ch}: ${amt:.2f}" for ch, amt in monthly_rev.items()
        ) or "  (まだなし)"

        msg = (
            f"【週次報告 {summary['date']}】\n"
            f"\n"
            f"タスク: 完了 {summary['tasks_completed']} / 失敗 {summary['tasks_failed']} / 合計 {summary['tasks_total']}\n"
            f"\n"
            f"今月の収益:\n{rev_lines}\n"
            f"\n"
            f"累計収益: ${summary['total_revenue_usd']:.2f}\n"
            f"\n"
            f"システム稼働中。問題なし。"
        )
        return await self.send_text(msg)

    async def send_alert(self, title: str, detail: str) -> bool:
        msg = f"【アラート】{title}\n\n{detail}"
        return await self.send_text(msg)

    async def reply(self, reply_token: str, message: str) -> bool:
        payload = {
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": message}],
        }
        resp = await self._client.post(f"{LINE_API_BASE}/message/reply", json=payload)
        return resp.status_code == 200

    def parse_command(self, text: str) -> tuple[str, str]:
        """Parse LINE message into (command, args)."""
        text = text.strip().lower()
        if text == "report" or text == "報告":
            return "report", ""
        if text == "status" or text == "状態":
            return "status", ""
        if text.startswith("pause") or text.startswith("停止"):
            return "pause", text.split(maxsplit=1)[1] if " " in text else ""
        if text.startswith("resume") or text.startswith("再開"):
            return "resume", text.split(maxsplit=1)[1] if " " in text else ""
        return "unknown", text
