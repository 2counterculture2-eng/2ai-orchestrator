"""
claude_code_agent.py v1
Autonomous code agent: executes LINE instructions by modifying the GitHub repo.
"""
import base64
import logging
import anthropic
import httpx

logger = logging.getLogger(__name__)

REPO = "2counterculture2-eng/2ai-orchestrator"
GITHUB_API = "https://api.github.com"

REPO_FILES = [
    "orchestrator/main.py",
    "orchestrator/config.py",
    "orchestrator/line_bot.py",
    "orchestrator/orchestrator_core.py",
    "orchestrator/workers/claude_code_agent.py",
    "requirements.txt",
]

SYSTEM_PROMPT = """You are an autonomous code agent for the 2AI Orchestrator project on Railway.
When given an instruction, make code changes using the edit_file tool.
Write complete file contents. Make minimal changes. Summarize in Japanese."""

TOOLS = [{
    "name": "edit_file",
    "description": "Create or update a file in the GitHub repo with complete content.",
    "input_schema": {"type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to repo root"},
            "content": {"type": "string", "description": "Complete new file content"},
            "commit_message": {"type": "string", "description": "Git commit message"},
        },
        "required": ["path", "content", "commit_message"],
    },
}]


class ClaudeCodeAgent:
    def __init__(self, anthropic_api_key: str, github_token: str):
        self.anthropic = anthropic.Anthropic(api_key=anthropic_api_key)
        self.github_token = github_token

    async def _get_file(self, path: str) -> tuple:
        headers = {
            "Authorization": f"token {self.github_token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "2ai-orchestrator",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{GITHUB_API}/repos/{REPO}/contents/{path}", headers=headers)
            if resp.status_code != 200:
                return "", ""
            j = resp.json()
            content = base64.b64decode(j["content"]).decode("utf-8")
            return j["sha"], content

    async def _put_file(self, path: str, content: str, sha: str, message: str) -> bool:
        headers = {
            "Authorization": f"token {self.github_token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "2ai-orchestrator",
            "Content-Type": "application/json",
        }
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        payload = {"message": message, "content": encoded}
        if sha:
            payload["sha"] = sha
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.put(f"{GITHUB_API}/repos/{REPO}/contents/{path}", headers=headers, json=payload)
            ok = resp.status_code in (200, 201)
            if not ok:
                logger.error("GitHub put failed %s: %s", path, resp.text[:200])
            return ok

    async def execute(self, instruction: str) -> str:
        file_shas = {}
        context = "Current codebase:\n\n"
        for path in REPO_FILES:
            sha, content = await self._get_file(path)
            if content:
                file_shas[path] = sha
                context += f"=== {path} ===\n{content}\n\n"

        messages = [{"role": "user", "content": f"{context}\n\n指示: {instruction}"}]
        changes = []

        for _ in range(6):
            response = self.anthropic.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            if response.stop_reason == "end_turn":
                text = " ".join(b.text for b in response.content if hasattr(b, "text"))
                prefix = "\n".join(changes) + "\n\n" if changes else ""
                return (prefix + text).strip() or "変更なし"

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    if block.name == "edit_file":
                        path = block.input["path"]
                        content = block.input["content"]
                        commit_msg = block.input["commit_message"]
                        sha = file_shas.get(path, "")
                        ok = await self._put_file(path, content, sha, f"[LINE] {commit_msg}")
                        if ok:
                            file_shas[path] = ""
                            changes.append(f"✅ {path}")
                        result_text = "OK" if ok else "FAILED"
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"{result_text}: {path}",
                        })
                messages.append({"role": "user", "content": tool_results})
            else:
                break

        return ("\n".join(changes) + "\n完了").strip() if changes else "変更なし"
