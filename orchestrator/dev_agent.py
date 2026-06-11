"""
dev_agent.py v1
Claude Opus agent with tools for code editing and deployment via LINE.
All strings are ASCII/English only (Rule 55).
Tools:
  - read_file: read file from GitHub repo
  - list_files: list files in a directory
  - write_file: write/update file in GitHub repo (auto-commits)
  - deploy: trigger Railway deployment
  - system_status: get current system status from DB
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

SYSTEM_PROMPT = """You are Claude Code running inside the 2AI Orchestrator system on Railway.
You have tools to read/write code files on GitHub and deploy to Railway.
The user communicates with you via LINE messaging app.
You are always online - even when the user's PC is off.

Rules:
- All code you write must use English strings only (no Japanese in code).
- When modifying Python files, preserve existing imports and structure.
- After writing a file, always offer to deploy unless the user says otherwise.
- Keep LINE responses concise (max 500 chars). For code output, summarize.
- Respond in Japanese to the user.

Available tools: read_file, list_files, write_file, deploy, system_status
"""

TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file from the GitHub repository",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to repo root, e.g. orchestrator/main.py"}
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
                "path": {"type": "string", "description": "Directory path, e.g. orchestrator or orchestrator/workers"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "Write or update a file in the GitHub repository with a commit",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to repo root"},
                "content": {"type": "string", "description": "Full file content (ASCII/English only - no Japanese characters)"},
                "commit_message": {"type": "string", "description": "Git commit message"}
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
                "reason": {"type": "string", "description": "Reason for deploying"}
            },
            "required": ["reason"]
        }
    },
    {
        "name": "system_status",
        "description": "Get current system status including trading and task summary",
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

    async def close(self):
        await self._http.aclose()

    async def run(self, user_message: str, history: list) -> str:
        messages = list(history) + [{"role": "user", "content": user_message}]
        try:
            return await self._agentic_loop(messages)
        except Exception as e:
            logger.exception(f"DevAgent error: {e}")
            return f"Error: {str(e)[:200]}"

    async def _agentic_loop(self, messages: list, depth: int = 0) -> str:
        if depth > 8:
            return "Max tool call depth reached."

        resp = self.claude.messages.create(
            model="claude-opus-4-8",
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
                        "content": str(result)[:3000],
                    })

            messages = messages + [
                {"role": "assistant", "content": resp.content},
                {"role": "user", "content": tool_results},
            ]
            return await self._agentic_loop(messages, depth + 1)

        return "Unexpected stop reason."

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
        # Get current SHA
        r = await self._http.get(f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}", headers=headers)
        sha = r.json().get("sha") if r.status_code == 200 else None
        # Write file
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
            return f"File written: {path} (commit: {r.json()['commit']['sha'][:8]})"
        return f"Write error {r.status_code}: {r.text[:200]}"

    async def _deploy(self, reason: str) -> str:
        query = 'mutation { serviceConnect(id: "' + RAILWAY_SERVICE_ID + '", input: { branch: "master", repo: "' + RAILWAY_REPO + '" }) { id } }'
        r = await self._http.post(
            "https://backboard.railway.app/graphql/v2",
            headers={"Authorization": f"Bearer {RAILWAY_TOKEN}", "Content-Type": "application/json"},
            json={"query": query},
        )
        if r.status_code == 200 and "errors" not in r.json():
            return f"Deploy triggered. Reason: {reason}. Build takes ~3 min."
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