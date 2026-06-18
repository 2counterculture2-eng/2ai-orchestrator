"""
yt_agent.py v1 - YouTubeAI DevAgent via LINE
Handles 'yt: <command>' messages. Edits 2counterculture2-eng/youtube-ai-pipeline on GitHub.
ASCII/English only in code.
"""
import base64, json, logging, os
from typing import Optional
import httpx, anthropic

logger = logging.getLogger(__name__)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
YT_REPO = "2counterculture2-eng/youtube-ai-pipeline"
GITHUB_API = "https://api.github.com"
YT_HISTORY_PATH = "data/yt_line_history.json"
MAX_HISTORY_EXCHANGES = 15

YT_CONTEXT = (
    "=== YouTubeAI PROJECT CONTEXT ===\n"
    "Goal: Autonomous faceless YouTube channel AI pipeline.\n"
    "GitHub: 2counterculture2-eng/youtube-ai-pipeline\n"
    "Stack: Local Python pipeline (moviepy, edge-tts, Claude API, YouTube Data API v3)\n"
    "Pipeline: niche_researcher -> script_generator -> voice_generator -> video_generator -> youtube_uploader\n"
    "\n"
    "Current state:\n"
    "- Local video generation: WORKING (moviepy + edge-tts, zero API keys)\n"
    "- Script generation: needs ANTHROPIC_API_KEY in .env\n"
    "- YouTube upload: needs Google OAuth credentials in channels/\n"
    "- Scheduler: pipeline/scheduler.py (runs locally)\n"
    "\n"
    "Key files:\n"
    "- pipeline/run_local_pipeline.py: zero-API full pipeline run\n"
    "- pipeline/orchestrator.py: full pipeline with API fallbacks\n"
    "- pipeline/scheduler.py: weekly schedule runner\n"
    "- briefing.md: current session status\n"
    "=== END CONTEXT ==="
)

YT_SYSTEM_PROMPT = (
    "You are an autonomous AI managing the YouTubeAI pipeline on behalf of Takuma.\n"
    "You receive instructions via LINE and execute them by editing GitHub files.\n"
    "This is a LINE-controlled code editing system for the YouTube AI pipeline.\n\n"
    + YT_CONTEXT + "\n\n"
    "Tools: read_file, list_files, write_file\n"
    "Rules:\n"
    "- ASCII/English only in source code files.\n"
    "- Respond in Japanese to Takuma.\n"
    "- Keep LINE responses under 1200 chars.\n"
    "- Use generate_video tool when Takuma says 'make a video', 'generate video', or similar.\n"
    "- Use check_video_status to check if a video generation is done.\n"
    "- Video artifacts are downloadable from GitHub Actions for 7 days after generation.\n"
)

YT_TOOLS = [
    {"name": "read_file", "description": "Read a file from YouTubeAI GitHub repo",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "list_files", "description": "List files in YouTubeAI GitHub dir",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write/update file in YouTubeAI GitHub",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}, "commit_message": {"type": "string"}
     }, "required": ["path", "content", "commit_message"]}},
    {"name": "generate_video", "description": "Trigger GitHub Actions to generate a YouTube video (runs in cloud, no PC needed)",
     "input_schema": {"type": "object", "properties": {
         "niche": {"type": "string", "description": "Video niche e.g. 'personal finance', 'investing', 'AI tools'"},
         "title": {"type": "string", "description": "Optional custom video title"}
     }, "required": []}},
    {"name": "check_video_status", "description": "Check status of latest GitHub Actions video generation run",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
]


class YouTubeAIAgent:
    def __init__(self, anthropic_client: anthropic.Anthropic):
        self.claude = anthropic_client
        self._http = httpx.AsyncClient(timeout=30)
        self._history_sha: Optional[str] = None

    async def close(self):
        await self._http.aclose()

    async def run(self, user_message: str) -> str:
        history = await self._load_history()
        messages = history + [{"role": "user", "content": user_message}]
        try:
            response = await self._agentic_loop(messages)
            new_history = history + [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": response},
            ]
            await self._save_history(new_history)
            return response
        except Exception as e:
            logger.exception("YouTubeAIAgent error: %s", e)
            return "Error: " + str(e)[:200]

    async def _agentic_loop(self, messages: list, depth: int = 0) -> str:
        if depth > 8:
            return "Max depth reached."
        resp = self.claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=YT_SYSTEM_PROMPT,
            tools=YT_TOOLS,
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
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(result)[:4000]})
            messages = messages + [
                {"role": "assistant", "content": resp.content},
                {"role": "user", "content": tool_results},
            ]
            return await self._agentic_loop(messages, depth + 1)
        return "Unexpected stop reason."

    async def _execute_tool(self, name: str, inputs: dict) -> str:
        try:
            if name == "read_file":       return await self._read_file(inputs["path"])
            if name == "list_files":      return await self._list_files(inputs["path"])
            if name == "write_file":      return await self._write_file(inputs["path"], inputs["content"], inputs["commit_message"])
            if name == "generate_video":  return await self._trigger_video_generation(inputs.get("niche", "personal finance"), inputs.get("title", ""))
            if name == "check_video_status": return await self._check_actions_status()
            return "Unknown tool: " + name
        except Exception as e:
            return "Tool error (" + name + "): " + str(e)

    async def _trigger_video_generation(self, niche: str, title: str) -> str:
        gh = {"Authorization": "token " + GITHUB_TOKEN, "Accept": "application/vnd.github.v3+json"}
        payload = {
            "ref": "master",
            "inputs": {"niche": niche, "title": title or ""}
        }
        r = await self._http.post(
            GITHUB_API + "/repos/" + YT_REPO + "/actions/workflows/generate_video.yml/dispatches",
            headers=gh, json=payload
        )
        if r.status_code == 204:
            return "GitHub Actions triggered! Video generation started for niche: " + niche + ". ETA ~5 min. Use check_video_status to track progress."
        return "Trigger failed " + str(r.status_code) + ": " + r.text[:200]

    async def _check_actions_status(self) -> str:
        gh = {"Authorization": "token " + GITHUB_TOKEN, "Accept": "application/vnd.github.v3+json"}
        r = await self._http.get(
            GITHUB_API + "/repos/" + YT_REPO + "/actions/runs?per_page=3",
            headers=gh
        )
        if r.status_code != 200:
            return "Status check failed: " + r.text[:200]
        runs = r.json().get("workflow_runs", [])
        if not runs:
            return "No runs found."
        lines = []
        for run in runs:
            status = run.get("status", "?")
            conclusion = run.get("conclusion") or "in_progress"
            run_id = run.get("id", "")
            num = run.get("run_number", "?")
            url = f"https://github.com/{YT_REPO}/actions/runs/{run_id}"
            lines.append(f"Run #{num}: {status}/{conclusion} -> {url}")
        return "\n".join(lines)

    async def _read_file(self, path: str) -> str:
        gh = {"Authorization": "token " + GITHUB_TOKEN, "Accept": "application/vnd.github.v3+json"}
        r = await self._http.get(GITHUB_API + "/repos/" + YT_REPO + "/contents/" + path, headers=gh)
        if r.status_code != 200:
            return "Error " + str(r.status_code) + ": " + r.text[:200]
        return "FILE: " + path + "\n" + base64.b64decode(r.json()["content"]).decode("utf-8")

    async def _list_files(self, path: str) -> str:
        gh = {"Authorization": "token " + GITHUB_TOKEN, "Accept": "application/vnd.github.v3+json"}
        r = await self._http.get(GITHUB_API + "/repos/" + YT_REPO + "/contents/" + path, headers=gh)
        if r.status_code != 200:
            return "Error " + str(r.status_code) + ": " + r.text[:200]
        items = r.json()
        return "\n".join(i["type"] + ": " + i["path"] for i in items) if isinstance(items, list) else str(items)

    async def _write_file(self, path: str, content: str, commit_message: str) -> str:
        gh = {"Authorization": "token " + GITHUB_TOKEN, "Accept": "application/vnd.github.v3+json"}
        r = await self._http.get(GITHUB_API + "/repos/" + YT_REPO + "/contents/" + path, headers=gh)
        sha = r.json().get("sha") if r.status_code == 200 else None
        payload = {
            "message": commit_message,
            "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
            "branch": "master",
        }
        if sha:
            payload["sha"] = sha
        r = await self._http.put(GITHUB_API + "/repos/" + YT_REPO + "/contents/" + path, headers=gh, json=payload)
        if r.status_code in (200, 201):
            return "Written: " + path + " (commit: " + r.json()["commit"]["sha"][:8] + ")"
        return "Write error " + str(r.status_code) + ": " + r.text[:200]

    async def _load_history(self) -> list:
        gh = {"Authorization": "token " + GITHUB_TOKEN, "Accept": "application/vnd.github.v3+json"}
        r = await self._http.get(GITHUB_API + "/repos/" + YT_REPO + "/contents/" + YT_HISTORY_PATH, headers=gh)
        if r.status_code == 200:
            data = r.json()
            self._history_sha = data["sha"]
            try:
                return json.loads(base64.b64decode(data["content"]).decode("utf-8"))[-(MAX_HISTORY_EXCHANGES * 2):]
            except Exception:
                return []
        self._history_sha = None
        return []

    async def _save_history(self, history: list):
        trimmed = history[-(MAX_HISTORY_EXCHANGES * 2):]
        encoded = base64.b64encode(json.dumps(trimmed, ensure_ascii=False, indent=2).encode("utf-8")).decode("utf-8")
        payload = {"message": "chore: update YT LINE history", "content": encoded, "branch": "master"}
        if self._history_sha:
            payload["sha"] = self._history_sha
        gh = {"Authorization": "token " + GITHUB_TOKEN, "Accept": "application/vnd.github.v3+json"}
        r = await self._http.put(GITHUB_API + "/repos/" + YT_REPO + "/contents/" + YT_HISTORY_PATH, headers=gh, json=payload)
        if r.status_code in (200, 201):
            self._history_sha = r.json()["content"]["sha"]
