"""
main.py v1
FastAPI application entry point.
Endpoints:
  GET  /         — health check
  GET  /status   — system status JSON
  POST /webhook/line — LINE Messaging API webhook
  POST /task     — internal task submission (future: authenticated)
"""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Header, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

from .config import Config
from .learning import LearningDB
from .line_bot import LineBot
from .orchestrator_core import OrchestratorCore

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# Global instances (initialized at startup)
_config: Config = None
_db: LearningDB = None
_line: LineBot = None
_orchestrator: OrchestratorCore = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _db, _line, _orchestrator
    _config = Config.from_env()
    _db = LearningDB(_config.db_path)
    _line = LineBot(_config)
    _orchestrator = OrchestratorCore(_config, _db, _line)
    await _orchestrator.start()
    logger.info("2AI Orchestrator v1 started")
    yield
    await _orchestrator.stop()
    logger.info("2AI Orchestrator v1 stopped")


app = FastAPI(title="2AI Orchestrator", version="1.0.0", lifespan=lifespan)


@app.get("/")
async def health():
    return {"status": "ok", "service": "2AI Orchestrator v1"}


@app.get("/status")
async def system_status():
    if not _db:
        raise HTTPException(status_code=503, detail="Not initialized")
    summary = _db.build_weekly_summary()
    pending = len(_db.get_pending_tasks())
    return {
        "status": "running",
        "pending_tasks": pending,
        "summary": summary,
    }


@app.post("/webhook/line")
async def line_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_line_signature: str = Header(default=""),
):
    body = await request.body()

    if not _line.verify_signature(body, x_line_signature):
        raise HTTPException(status_code=400, detail="Invalid LINE signature")

    import json
    payload = json.loads(body)
    events = payload.get("events", [])

    for event in events:
        if event.get("type") == "message" and event["message"]["type"] == "text":
            text = event["message"]["text"]
            reply_token = event.get("replyToken", "")
            command, args = _line.parse_command(text)
            background_tasks.add_task(
                _orchestrator.handle_line_command, command, args, reply_token
            )

    return JSONResponse(content={"status": "ok"})


@app.post("/task")
async def submit_task(request: Request):
    """Internal endpoint to submit a task to the orchestrator queue."""
    task = await request.json()
    if not task:
        raise HTTPException(status_code=400, detail="Empty task body")
    task_id = await _orchestrator.enqueue_task(task)
    return {"task_id": task_id, "status": "queued"}


@app.get("/revenue")
async def get_revenue():
    if not _db:
        raise HTTPException(status_code=503, detail="Not initialized")
    return {
        "monthly": _db.get_monthly_revenue(),
        "total_usd": _db.get_total_revenue(),
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("orchestrator.main:app", host="0.0.0.0", port=port, reload=False)
