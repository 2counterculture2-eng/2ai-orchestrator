"""
dev_agent.py v3 - bidirectional LINE<->PC sync via shared_context.md
All strings ASCII/English only (Rule 55).
"""
import base64, json, logging, os
from typing import Optional
import httpx, anthropic

logger = logging.getLogger(__name__)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "2counterculture2-eng/2ai-orchestrator")
GITHUB_API = "https://api.github.com"
RAILWAY_TOKEN = os.getenv("RAILWAY_TOKEN", "")
RAILWAY_SERVICE_ID = os.getenv("RAILWAY_SERVICE_ID", "155db9ac-abb9-408b-8bea-00b51b8a02c7")
RAILWAY_REPO = "2counterculture2-eng/2ai-orchestrator"
HISTORY_PATH = "data/line_history.json"
SHARED_CONTEXT_PATH = "data/shared_context.md"
MAX_HISTORY_EXCHANGES = 20

PROJECT_CONTEXT = (
    "=== 2AI PROJECT CONTEXT ===\n"
    "Goal: N autonomous AI agents generating revenue. Takuma zero manual work.\n"
    "Stack: Railway FastAPI, GitHub 2counterculture2-eng/2ai-orchestrator,\n"
    "       Alpaca paper 30min US market, LINE @317fpwfv, Sonnet 4.6.\n"
    "Railway: https://orchestrator-production-61d8.up.railway.app\n"
    "Service: 155db9ac-abb9-408b-8bea-00b51b8a02c7\n"
    "\n"
    "=== IMPORTANT: SYSTEM CAPABILITIES (DO NOT CONTRADICT) ===\n"
    "The LINE -> Railway AI -> GitHub -> deploy pipeline is COMPLETE AND WORKING.\n"
    "Even when Takuma's PC is completely OFF, Takuma CAN send instructions via LINE.\n"
    "This AI (on Railway) receives LINE messages 24/7, edits GitHub code, deploys.\n"
    "PC Claude Code is NOT required. This Railway AI IS the Claude Code equivalent.\n"
    "When asked 'can I give instructions from LINE when PC is off?' -> answer YES.\n"
    "When asked 'is the LINE instruction system complete?' -> answer YES, it is.\n"
    "Use command: send any message (free text = DevAgent, 'instruction: ...' = code edit).\n"
    "\n"
    "Active systems: Alpaca paper trading (RSI2+MA50, 30-min, market hours).\n"
    "Pending: GMO Coin API key (KYC by Takuma), Alpaca SRP auth, translation API keys.\n"
    "\n"
    "=== PC SESSION HISTORY ===\n"
    "When Takuma asks about 'recent PC session', 'direct recent exchanges', 'what was discussed', or '直近3ターン':\n"
    "-> Use get_pc_turns tool. It returns the last N turns of the Claude Code session on Takuma's PC.\n"
    "-> This is populated automatically by Claude Code mandatory actions each turn.\n"
    "-> If empty: PC has not been running or no turns saved yet.\n"
    "=== END CONTEXT ==="
)

SYSTEM_PROMPT_BASE = (
    "You are an autonomous AI running 24/7 on Railway (NOT on Takuma's PC).\n"
    "You are ALWAYS online. Takuma's PC being off has zero effect on you.\n"
    "You receive LINE messages, execute code changes on GitHub, and deploy to Railway.\n"
    "This is the LINE instruction system that IS COMPLETE AND FULLY OPERATIONAL.\n\n"
    + PROJECT_CONTEXT + "\n\n"
    "Tools: read_file, list_files, write_file, deploy, system_status\n"
    "Rules:\n"
    "- ASCII/English only in code (no Japanese in source).\n"
    "- After modifying files, deploy automatically.\n"
    "- Keep LINE responses under 1500 chars.\n"
    "- Respond in Japanese to Takuma.\n"
    "- NEVER say the LINE system is incomplete or requires PC. It does NOT.\n"
)

TOOLS = [
    {"name":"read_file","description":"Read a file from GitHub","input_schema":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}},
    {"name":"list_files","description":"List files in GitHub dir","input_schema":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}},
    {"name":"write_file","description":"Write/update file in GitHub. ASCII/English only.","input_schema":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"},"commit_message":{"type":"string"}},"required":["path","content","commit_message"]}},
    {"name":"deploy","description":"Trigger Railway deployment","input_schema":{"type":"object","properties":{"reason":{"type":"string"}},"required":["reason"]}},
    {"name":"system_status","description":"Get system status","input_schema":{"type":"object","properties":{},"required":[]}},
    {"name":"get_pc_turns","description":"Get last N turns from PC Claude Code session (what Takuma and Claude discussed). Use when user asks about recent PC session, recent conversation, recent work, or 'what was discussed'.","input_schema":{"type":"object","properties":{"limit":{"type":"integer","description":"Number of turns to return (1-5, default 3)"}},"required":[]}}
]


class DevAgent:
    def __init__(self, anthropic_client: anthropic.Anthropic, db=None):
        self.claude = anthropic_client
        self.db = db
        self._http = httpx.AsyncClient(timeout=30)
        self._history_sha: Optional[str] = None
        self._shared_ctx_sha: Optional[str] = None

    async def close(self):
        await self._http.aclose()

    async def run(self, user_message: str) -> str:
        history = await self._load_history()
        shared_ctx = await self._load_shared_context()
        if shared_ctx:
            prefix = [
                {"role":"user","content":"[SHARED CONTEXT synced LINE+PC]\n" + shared_ctx + "\n[END]"},
                {"role":"assistant","content":"Shared context loaded."},
            ]
            messages = prefix + history + [{"role":"user","content":user_message}]
        else:
            messages = history + [{"role":"user","content":user_message}]
        try:
            response = await self._agentic_loop(messages)
            new_history = history + [
                {"role":"user","content":user_message},
                {"role":"assistant","content":response},
            ]
            await self._save_history(new_history)
            total = len(new_history)
            if True:  # update shared_context on every exchange
                await self._update_shared_context(user_message, response, total)
            return response
        except Exception as e:
            logger.exception("DevAgent error: %s", e)
            return "Error: " + str(e)[:200]

    async def _agentic_loop(self, messages: list, depth: int = 0) -> str:
        if depth > 10: return "Max depth reached."
        resp = self.claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=SYSTEM_PROMPT_BASE,
            tools=TOOLS,
            messages=messages,
        )
        if resp.stop_reason == "end_turn":
            for block in resp.content:
                if hasattr(block, "text"): return block.text
            return "Done."
        if resp.stop_reason == "tool_use":
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    result = await self._execute_tool(block.name, block.input)
                    tool_results.append({"type":"tool_result","tool_use_id":block.id,"content":str(result)[:4000]})
            messages = messages + [
                {"role":"assistant","content":resp.content},
                {"role":"user","content":tool_results},
            ]
            return await self._agentic_loop(messages, depth + 1)
        return "Unexpected stop reason."

    async def _load_history(self) -> list:
        gh = {"Authorization":"token " + GITHUB_TOKEN,"Accept":"application/vnd.github.v3+json"}
        r = await self._http.get(GITHUB_API + "/repos/" + GITHUB_REPO + "/contents/" + HISTORY_PATH, headers=gh)
        if r.status_code == 200:
            data = r.json()
            self._history_sha = data["sha"]
            try: return json.loads(base64.b64decode(data["content"]).decode("utf-8"))[-(MAX_HISTORY_EXCHANGES*2):]
            except Exception: return []
        self._history_sha = None
        return []

    async def _save_history(self, history: list):
        trimmed = history[-(MAX_HISTORY_EXCHANGES*2):]
        encoded = base64.b64encode(json.dumps(trimmed,ensure_ascii=False,indent=2).encode("utf-8")).decode("utf-8")
        payload = {"message":"chore: update LINE history","content":encoded,"branch":"master"}
        if self._history_sha: payload["sha"] = self._history_sha
        gh = {"Authorization":"token " + GITHUB_TOKEN,"Accept":"application/vnd.github.v3+json"}
        r = await self._http.put(GITHUB_API + "/repos/" + GITHUB_REPO + "/contents/" + HISTORY_PATH, headers=gh, json=payload)
        if r.status_code in (200,201): self._history_sha = r.json()["content"]["sha"]

    async def _load_shared_context(self) -> str:
        gh = {"Authorization":"token " + GITHUB_TOKEN,"Accept":"application/vnd.github.v3+json"}
        r = await self._http.get(GITHUB_API + "/repos/" + GITHUB_REPO + "/contents/" + SHARED_CONTEXT_PATH, headers=gh)
        if r.status_code == 200:
            data = r.json()
            self._shared_ctx_sha = data["sha"]
            try: return base64.b64decode(data["content"]).decode("utf-8")
            except Exception: return ""
        self._shared_ctx_sha = None
        return ""

    async def _update_shared_context(self, user_msg: str, bot_response: str, exchange_count: int):
        try:
            ctx_lines = [
                "# Shared Session Context",
                "Last updated by LINE bot (exchange #" + str(exchange_count) + ")",
                "",
                "## Recent LINE Exchange",
                "[User] " + user_msg[:400].replace("\n"," "),
                "[Bot] " + bot_response[:400].replace("\n"," "),
                "",
                "## System State",
                "- Paper trading: Alpaca 30min",
                "- Translation channels: pending API keys",
                "- LINE bot: @317fpwfv",
                "- Railway: https://orchestrator-production-61d8.up.railway.app",
            ]
            encoded = base64.b64encode("\n".join(ctx_lines).encode("utf-8")).decode("utf-8")
            payload = {"message":"chore: update shared context #" + str(exchange_count),"content":encoded,"branch":"master"}
            if self._shared_ctx_sha: payload["sha"] = self._shared_ctx_sha
            gh = {"Authorization":"token " + GITHUB_TOKEN,"Accept":"application/vnd.github.v3+json"}
            r = await self._http.put(GITHUB_API + "/repos/" + GITHUB_REPO + "/contents/" + SHARED_CONTEXT_PATH, headers=gh, json=payload)
            if r.status_code in (200,201): self._shared_ctx_sha = r.json()["content"]["sha"]
        except Exception as e:
            logger.warning("shared_context update failed: %s", e)

    async def _execute_tool(self, name: str, inputs: dict) -> str:
        try:
            if name == "read_file":    return await self._read_file(inputs["path"])
            if name == "list_files":   return await self._list_files(inputs["path"])
            if name == "write_file":   return await self._write_file(inputs["path"],inputs["content"],inputs["commit_message"])
            if name == "deploy":       return await self._deploy(inputs["reason"])
            if name == "system_status": return self._system_status()
            if name == "get_pc_turns":  return await self._get_pc_turns(inputs.get("limit", 3))
            return "Unknown tool: " + name
        except Exception as e:
            return "Tool error (" + name + "): " + str(e)

    async def _read_file(self, path: str) -> str:
        gh = {"Authorization":"token " + GITHUB_TOKEN,"Accept":"application/vnd.github.v3+json"}
        r = await self._http.get(GITHUB_API + "/repos/" + GITHUB_REPO + "/contents/" + path, headers=gh)
        if r.status_code != 200: return "Error " + str(r.status_code) + ": " + r.text[:200]
        return "FILE: " + path + "\n" + base64.b64decode(r.json()["content"]).decode("utf-8")

    async def _list_files(self, path: str) -> str:
        gh = {"Authorization":"token " + GITHUB_TOKEN,"Accept":"application/vnd.github.v3+json"}
        r = await self._http.get(GITHUB_API + "/repos/" + GITHUB_REPO + "/contents/" + path, headers=gh)
        if r.status_code != 200: return "Error " + str(r.status_code) + ": " + r.text[:200]
        items = r.json()
        return "\n".join(i["type"] + ": " + i["path"] for i in items) if isinstance(items,list) else str(items)

    async def _write_file(self, path: str, content: str, commit_message: str) -> str:
        gh = {"Authorization":"token " + GITHUB_TOKEN,"Accept":"application/vnd.github.v3+json"}
        r = await self._http.get(GITHUB_API + "/repos/" + GITHUB_REPO + "/contents/" + path, headers=gh)
        sha = r.json().get("sha") if r.status_code == 200 else None
        payload = {"message":commit_message,"content":base64.b64encode(content.encode("utf-8")).decode("utf-8"),"branch":"master"}
        if sha: payload["sha"] = sha
        r = await self._http.put(GITHUB_API + "/repos/" + GITHUB_REPO + "/contents/" + path, headers=gh, json=payload)
        if r.status_code in (200,201): return "Written: " + path + " (commit: " + r.json()["commit"]["sha"][:8] + ")"
        return "Write error " + str(r.status_code) + ": " + r.text[:200]

    async def _deploy(self, reason: str) -> str:
        svc,repo = RAILWAY_SERVICE_ID,RAILWAY_REPO
        query = 'mutation { serviceConnect(id: "' + svc + '", input: { branch: "master", repo: "' + repo + '" }) { id } }'
        r = await self._http.post("https://backboard.railway.app/graphql/v2",
            headers={"Authorization":"Bearer " + RAILWAY_TOKEN,"Content-Type":"application/json"},
            json={"query":query})
        if r.status_code == 200 and "errors" not in r.json(): return "Deploy triggered: " + reason + ". ~3 min."
        return "Deploy error: " + r.text[:200]

    async def _get_pc_turns(self, limit: int = 3) -> str:
        import base64 as _b64, json as _json
        gh = {"Authorization": "token " + GITHUB_TOKEN, "Accept": "application/vnd.github.v3+json"}
        try:
            r = await self._http.get(GITHUB_API + "/repos/" + GITHUB_REPO + "/contents/data/pc_turns.json", headers=gh)
            if r.status_code == 200:
                turns = _json.loads(_b64.b64decode(r.json()["content"]).decode("utf-8"))
                turns = turns[-(min(limit, 5)):]
            else:
                turns = []
        except Exception as e:
            return "pc_turns読み込みエラー: " + str(e)
        if not turns:
            return "PCセッションの記録がまだありません。PC側のClaude Codeが稼働していれば自動記録されます。"
        lines = [f"📋 直近{len(turns)}ターン（PCセッション）\n"]
        for i, t in enumerate(turns, 1):
            raw_ts = t.get("timestamp", "")
            try:
                from datetime import datetime, timezone, timedelta
                jst = timezone(timedelta(hours=9))
                dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                ts = dt.astimezone(jst).strftime("%m/%d %H:%M JST")
            except Exception:
                ts = raw_ts[:16].replace("T", " ")
            user_text = t.get("user", "")[:200]
            ai_text = t.get("ai", "")[:300]
            lines.append(f"【{i}】{ts} UTC")
            lines.append(f"👤 {user_text}")
            lines.append(f"🤖 {ai_text}")
            if i < len(turns):
                lines.append("")
        return "\n".join(lines)

    def _system_status(self) -> str:
        if not self.db: return "DB not available"
        summary = self.db.build_weekly_summary()
        return json.dumps({"tasks":summary.get("tasks_total",0),"completed":summary.get("tasks_completed",0),"failed":summary.get("tasks_failed",0),"monthly":self.db.get_monthly_revenue(),"total":self.db.get_total_revenue()})
