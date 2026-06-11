"""
dev_agent.py v2
Claude Sonnet agent for LINE-based sessions.
- Conversation history persisted in GitHub (data/line_history.json)
- Full project context injected into system prompt
- Same capabilities as Claude Code PC session
All strings ASCII/English only (Rule 55).
"""
import base64
import json
import logging
import os
from typing import Optional
import httpx
import anthropic

logger = logging.getLogger(__name__)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "2counterculture2-eng/2ai-orchestrator")
GITHUB_API = "https://api.github.com"
RAILWAY_TOKEN = os.getenv("RAILWAY_TOKEN", "")
RAILWAY_SERVICE_ID = os.getenv("RAILWAY_SERVICE_ID", "155db9ac-abb9-408b-8bea-00b51b8a02c7")
RAILWAY_REPO = "2counterculture2-eng/2ai-orchestrator"
HISTORY_PATH = "data/line_history.json"
MAX_HISTORY_EXCHANGES = 20  # keep last 20 exchanges (40 messages)

PROJECT_CONTEXT = """=== 2AI PROJECT CONTEXT ===
Goal: Build N autonomous AI agents that continuously generate revenue into Takuma's account.
Takuma does zero manual work - AI handles everything 0 to 100.

Stack:
- Railway: Python FastAPI server (always on)
- GitHub repo: 2counterculture2-eng/2ai-orchestrator
- Alpaca: paper trading (30-min interval during US market hours, Cognito auth)
- LINE Bot: @317fpwfv (this channel)
- Claude API: Sonnet 4.6 main, Haiku for routing

Key rules:
- Never ask Takuma to do manual work
- All code strings must be ASCII/English only (no Japanese in code)
- After writing code, always offer to deploy
- This LINE session IS the same as the PC Claude Code session

Railway service:
- URL: https://orchestrator-production-61d8.up.railway.app
- Service ID: 155db9ac-abb9-408b-8bea-00b51b8a02c7
- Deploy: serviceConnect mutation on Railway GraphQL API

Current phase: Paper trading active. Translation channels pending API keys.
=== END CONTEXT ==="""

SYSTEM_PROMPT = f"""You are Claude Code running inside the 2AI Orchestrator on Railway.
You are always online - even when Takuma's PC is off.
This LINE session has full continuity - you remember all past conversations.

{PROJECT_CONTEXT}

Tools available: read_file, list_files, write_file, deploy, system_status, read_history
Rules:
- All code you write must be ASCII/English only (no Japanese in source code).
- After modifying a file, always offer to deploy.
- Keep LINE responses under 1500 chars. Summarize code output.
- Respond in Japanese to Takuma.
- You have memory of past conversations via conversation history.
"""

TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file from the GitHub repository",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path, e.g. orchestrator/main.py"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "list_files",
        "description": "List files in a directory of the GitHub repository",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path, e.g. orchestrator"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "Write or update a file in the GitHub repository with a commit. Content must be ASCII/English only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string", "description": "File content - ASCII/English only, no Japanese"},
                "commit_message": {"type": "string"}
            },
            "required": ["path", "content", "commit_message"]
        }
    },
    {
        "name": "deploy",
        "description": "Trigger a Railway deployment from the latest GitHub master branch",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"}
            },
            "required": ["reason"]
        }
    },
    {
        "name": "system_status",
        "description": "Get current system status (trading, tasks, revenue)",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
]


class DevAgent:
    def __init__(self, anthropic_client: anthropic.Anthropic, db=None):
        self.claude = anthropic_client
        self.db = db
        self._http = httpx.AsyncClient(timeout=30)
        self._history_sha: Optional[str] = None

    async def close(self):
        await self._http.aclose()

    async def run(self, user_message: str) -> str:
        # Load history from GitHub
        history = await self._load_history()
        messages = history + [{"role": "user", "content": user_message}]
        try:
            response = await self._agentic_loop(messages)
            # Save updated history to GitHub
            new_history = history + [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": response},
            ]
            await self._save_history(new_history)
            return response
        except Exception as e:
            logger.exception(f"DevAgent error: {e}")
            return f"Error: {str(e)[:200]}"

    async def _agentic_loop(self, messages: list, depth: int = 0) -> str:
        if depth > 10:
            return "Max depth reached."

        resp = self.claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if resp.stop_reason == "end_turn":
            for block in resp.content:
                if hasattr(block, "text"):
                    return block.text
            return "Done."

        if resp.stop_reason == "tool_use":
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    result = await self._execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(result)[:4000],
                    })
            messages = messages + [
                {"role": "assistant", "content": resp.content},
                {"role": "user", "content": tool_results},
            ]
            return await self._agentic_loop(messages, depth + 1)

        return "Unexpected stop reason."

    async def _load_history(self) -> list:
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        r = await self._http.get(f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{HISTORY_PATH}", headers=headers)
        if r.status_code == 200:
            data = r.json()
            self._history_sha = data["sha"]
            try:
                raw = base64.b64decode(data["content"]).decode("utf-8")
                history = json.loads(raw)
                # Keep last MAX_HISTORY_EXCHANGES exchanges
                return history[-(MAX_HISTORY_EXCHANGES * 2):]
            except Exception:
                return []
        self._history_sha = None
        return []

    async def _save_history(self, history: list):
        trimmed = history[-(MAX_HISTORY_EXCHANGES * 2):]
        content_bytes = json.dumps(trimmed, ensure_ascii=False, indent=2).encode("utf-8")
        encoded = base64.b64encode(content_bytes).decode("utf-8")
        payload = {
            "message": "chore: update LINE conversation history",
            "content": encoded,
            "branch": "master",
        }
        if self._history_sha:
            payload["sha"] = self._history_sha
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        r = await self._http.put(
            f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{HISTORY_PATH}",
            headers=headers, json=payload
        )
        if r.status_code in (200, 201):
            self._history_sha = r.json()["content"]["sha"]
            logger.info("History saved to GitHub")
        else:
            logger.warning(f"History save failed: {r.status_code} {r.text[:100]}")

    async def _execute_tool(self, name: str, inputs: dict) -> str:
        try:
            if name == "read_file":
                return await self._read_file(inputs["path"])
            elif name == "list_files":
                return await self._list_files(inputs["path"])
            elif name == "write_file":
                return await self._write_file(inputs["path"], inputs["content"], inputs["commit_message"])
            elif name == "deploy":
                return await self._deploy(inputs["reason"])
            elif name == "system_status":
                return self._system_status()
            else:
                return f"Unknown tool: {name}"
        except Exception as e:
            return f"Tool error ({name}): {e}"

    async def _read_file(self, path: str) -> str:
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        r = await self._http.get(f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}", headers=headers)
        if r.status_code != 200:
            return f"Error {r.status_code}: {r.text[:200]}"
        data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return f"FILE: {path} (sha:{data['sha'][:8]})\n{content}"

    async def _list_files(self, path: str) -> str:
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        r = await self._http.get(f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}", headers=headers)
        if r.status_code != 200:
            return f"Error {r.status_code}: {r.text[:200]}"
        items = r.json()
        if isinstance(items, list):
            return "\n".join(f"{i['type']}: {i['path']}" for i in items)
        return str(items)

    async def _write_file(self, path: str, content: str, commit_message: str) -> str:
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        r = await self._http.get(f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}", headers=headers)
        sha = r.json().get("sha") if r.status_code == 200 else None
        payload = {
            "message": commit_message,
            "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
            "branch": "master",
        }
        if sha:
            payload["sha"] = sha
        r = await self._http.put(
            f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}",
            headers=headers, json=payload
        )
        if r.status_code in (200, 201):
            return f"Written: {path} (commit: {r.json()['commit']['sha'][:8]})"
        return f"Write error {r.status_code}: {r.text[:200]}"

    async def _deploy(self, reason: str) -> str:
        query = 'mutation { serviceConnect(id: "' + RAILWAY_SERVICE_ID + '", input: { branch: "master", repo: "' + RAILWAY_REPO + '" }) { id } }'
        r = await self._http.post(
            "https://backboard.railway.app/graphql/v2",
            headers={"Authorization": f"Bearer {RAILWAY_TOKEN}", "Content-Type": "application/json"},
            json={"query": query},
        )
        if r.status_code == 200 and "errors" not in r.json():
            return f"Deploy triggered. Reason: {reason}. Build ~3 min."
        return f"Deploy error: {r.text[:200]}"

    def _system_status(self) -> str:
        if not self.db:
            return "DB not available"
        summary = self.db.build_weekly_summary()
        monthly = self.db.get_monthly_revenue()
        return json.dumps({
            "tasks_today": summary.get("tasks_total", 0),
            "completed": summary.get("tasks_completed", 0),
            "failed": summary.get("tasks_failed", 0),
            "monthly_revenue": monthly,
            "total_revenue": self.db.get_total_revenue(),
        })