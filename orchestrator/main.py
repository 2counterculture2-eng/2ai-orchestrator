"""
main.py v2
FastAPI application entry point.
Endpoints:
  GET  /              — health check
  GET  /status        — system status JSON
  GET  /admin         — HTML admin dashboard
  GET  /setup         — LINE bot onboarding page
  GET  /revenue       — revenue summary
  GET  /test/translate — translation smoke test
  POST /webhook/line  — LINE Messaging API webhook
  POST /task          — internal task submission
"""
import logging
import os
import httpx
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Header, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse

from .config import Config
from .learning import LearningDB
from .line_bot import LineBot
from .orchestrator_core import OrchestratorCore
from .market_data import MarketDataClient

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
_market: MarketDataClient = None


RAILWAY_TOKEN = os.getenv("RAILWAY_TOKEN", "")
RAILWAY_SERVICE_ID = os.getenv("RAILWAY_SERVICE_ID", "155db9ac-abb9-408b-8bea-00b51b8a02c7")
RAILWAY_ENV_ID = os.getenv("RAILWAY_ENV_ID", "f23ef4f6-5a1f-46b3-98f9-8f5eacf2f45c")
RAILWAY_PROJECT_ID = os.getenv("RAILWAY_PROJECT_ID", "a9375e6d-1f7d-47aa-94f3-dd70f2e0b50e")


async def persist_line_user_id(user_id: str) -> None:
    """Save LINE user ID to both DB and Railway env var for persistence across redeploys."""
    _db.set_config("line_user_id", user_id)
    if RAILWAY_TOKEN:
        query = """mutation variableUpsert($input: VariableUpsertInput!) { variableUpsert(input: $input) }"""
        variables = {"input": {
            "projectId": RAILWAY_PROJECT_ID,
            "serviceId": RAILWAY_SERVICE_ID,
            "environmentId": RAILWAY_ENV_ID,
            "name": "LINE_USER_ID",
            "value": user_id,
        }}
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://backboard.railway.app/graphql/v2",
                    headers={"Authorization": f"Bearer {RAILWAY_TOKEN}", "Content-Type": "application/json"},
                    json={"query": query, "variables": variables},
                    timeout=10,
                )
            logger.info(f"Railway LINE_USER_ID updated: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Failed to persist user_id to Railway: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _db, _line, _orchestrator, _market
    _config = Config.from_env()
    _db = LearningDB(_config.db_path)
    _line = LineBot(_config)
    _orchestrator = OrchestratorCore(_config, _db, _line)
    _market = MarketDataClient(_config.alpha_vantage_api_key)
    await _orchestrator.start()
    logger.info("2AI Orchestrator v2 started")
    yield
    await _orchestrator.stop()
    logger.info("2AI Orchestrator v1 stopped")


app = FastAPI(title="2AI Orchestrator", version="1.0.0", lifespan=lifespan)


@app.get("/")
async def health():
    return {"status": "ok", "service": "2AI Orchestrator v2"}


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
        event_type = event.get("type")

        if event_type == "follow":
            user_id = event.get("source", {}).get("userId", "")
            if user_id:
                logger.info(f"New follower: {user_id}")
                background_tasks.add_task(persist_line_user_id, user_id)
                reply_token = event.get("replyToken", "")
                background_tasks.add_task(
                    _line.reply, reply_token,
                    f"フォローありがとうございます！2AI Orchestratorです。\n"
                    f"ユーザーID取得完了。定期報告を開始します。"
                )

        elif event_type == "message" and event["message"]["type"] == "text":
            user_id = event.get("source", {}).get("userId", "")
            if user_id and not _db.get_config("line_user_id"):
                logger.info(f"Captured LINE user_id from message: {user_id}")
                background_tasks.add_task(persist_line_user_id, user_id)
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


@app.get("/debug/line-user-id")
async def debug_line_user_id():
    if not _db:
        raise HTTPException(status_code=503, detail="Not initialized")
    uid = _db.get_config("line_user_id")
    return {"line_user_id": uid or None}


@app.get("/debug/send-test")
async def debug_send_test():
    if not _line or not _config or not _orchestrator:
        raise HTTPException(status_code=503, detail="Not initialized")
    uid = (_db.get_config("line_user_id") if _db else None) or (_config.line_user_id if _config else None)
    if not uid:
        return {"success": False, "error": "LINE_USER_ID not configured"}
    status_text = await _orchestrator._system_status()
    ok = await _line.send_text(f"[Test notification]\n{status_text}", user_id=uid)
    return {"success": ok, "user_id_prefix": uid[:10] + "..."}

@app.get("/revenue")
async def get_revenue():
    if not _db:
        raise HTTPException(status_code=503, detail="Not initialized")
    return {
        "monthly": _db.get_monthly_revenue(),
        "total_usd": _db.get_total_revenue(),
    }


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard():
    if not _db:
        return HTMLResponse("<h1>Initializing...</h1>", status_code=503)

    uid = _db.get_config("line_user_id") or "未設定"
    pending = len(_db.get_pending_tasks())
    monthly = _db.get_monthly_revenue()
    total = _db.get_total_revenue()
    monthly_rows = "".join(
        f"<tr><td>{ch}</td><td>${v:.2f}</td></tr>" for ch, v in monthly.items()
    ) or "<tr><td colspan='2'>収益なし</td></tr>"

    recent_tasks = _db.get_recent_tasks(10)
    status_colors = {"completed": "#4ade80", "failed": "#f87171", "pending": "#fbbf24"}
    def _task_row(t):
        sc = status_colors.get(t["status"], "#e2e8f0")
        return (
            f"<tr><td style='font-size:11px;color:#64748b'>{t['task_id'][:8]}</td>"
            f"<td>{t['task_type']}</td><td>{t['channel'] or '-'}</td>"
            f"<td style='color:{sc}'>{t['status']}</td>"
            f"<td>${t['revenue_usd']:.2f}</td>"
            f"<td style='font-size:11px;color:#64748b'>{t['created_at'][:16]}</td></tr>"
        )
    task_rows = "".join(_task_row(t) for t in recent_tasks) or "<tr><td colspan='6'>タスクなし</td></tr>"

    cfg = _config
    api_status = {
        "Claude API": "✅" if cfg.anthropic_api_key else "❌",
        "LINE Bot": "✅" if cfg.line_channel_access_token else "❌",
        "LINE User ID": "✅" if uid != "未設定" else "⚠️ 未取得",
        "Alpha Vantage": "✅ QB7HJ9U0..." if cfg.alpha_vantage_api_key else "❌ 要登録",
        "Alpaca": "✅ Cognito" if cfg.alpaca_email else ("✅" if cfg.alpaca_api_key else "❌ 要登録"),
        "OANDA": "✅" if cfg.oanda_api_key else "❌ 要登録",
        "Smartcat": "✅" if cfg.smartcat_api_key else "❌ 要登録",
        "GigRadar(Upwork)": "✅" if cfg.gigradar_api_key else "❌ 要登録",
    }
    api_rows = "".join(
        f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in api_status.items()
    )

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>2AI Orchestrator Admin</title>
<style>
  body {{ font-family: -apple-system, sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 20px; }}
  h1 {{ color: #38bdf8; margin-bottom: 4px; }}
  .subtitle {{ color: #64748b; margin-bottom: 24px; font-size: 14px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }}
  .card {{ background: #1e293b; border-radius: 12px; padding: 20px; border: 1px solid #334155; }}
  .card h2 {{ margin: 0 0 12px; font-size: 14px; text-transform: uppercase; letter-spacing: 1px; color: #94a3b8; }}
  .stat {{ font-size: 36px; font-weight: bold; color: #38bdf8; }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{ padding: 8px 4px; border-bottom: 1px solid #334155; font-size: 14px; }}
  td:first-child {{ color: #94a3b8; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 99px; font-size: 12px; background: #0ea5e9; color: white; }}
  .footer {{ margin-top: 24px; color: #475569; font-size: 12px; }}
</style>
</head>
<body>
<h1>2AI Orchestrator</h1>
<div class="subtitle">自律AI収益システム — ダッシュボード</div>
<div class="grid">
  <div class="card">
    <h2>累計収益</h2>
    <div class="stat">${total:.2f}</div>
  </div>
  <div class="card">
    <h2>待機タスク</h2>
    <div class="stat">{pending}</div>
  </div>
  <div class="card">
    <h2>LINE User ID</h2>
    <div style="font-size:14px; word-break:break-all; margin-top:8px;">{uid}</div>
    <div style="margin-top:8px;"><a href="/setup" style="color:#38bdf8;">→ LINE Bot セットアップ</a></div>
  </div>
  <div class="card">
    <h2>今月の収益チャンネル別</h2>
    <table>{monthly_rows}</table>
  </div>
  <div class="card">
    <h2>API 接続状態</h2>
    <table>{api_rows}</table>
  </div>
  <div class="card">
    <h2>クイックテスト</h2>
    <p style="font-size:13px; color:#94a3b8;">翻訳パイプラインのスモークテストを実行:</p>
    <a href="/test/translate" style="color:#38bdf8; font-size:14px;">→ /test/translate を実行</a>
    <br><a href="/test/alpaca" style="color:#38bdf8; font-size:14px; margin-top:8px; display:block;">→ /test/alpaca (Alpacaアカウント確認)</a>
    <br><a href="/test/alpaca/trade" style="color:#38bdf8; font-size:14px; margin-top:4px; display:block;">→ /test/alpaca/trade (トレード分析実行)</a>
  </div>
  <div class="card" style="grid-column: 1/-1;">
    <h2>最近のタスク</h2>
    <table>
      <tr style="color:#64748b; font-size:12px;"><td>ID</td><td>タイプ</td><td>チャンネル</td><td>状態</td><td>収益</td><td>作成日時</td></tr>
      {task_rows}
    </table>
  </div>
</div>
<div class="footer">2AI Orchestrator v2 — Railway deployment</div>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    html = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>2AI — LINE Bot セットアップ</title>
<style>
  body { font-family: -apple-system, sans-serif; background: #0f172a; color: #e2e8f0;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
  .box { background: #1e293b; border-radius: 16px; padding: 40px; max-width: 400px; text-align: center; }
  h1 { color: #38bdf8; font-size: 22px; }
  .id { background: #0f172a; padding: 12px 20px; border-radius: 8px; font-size: 24px;
        font-weight: bold; letter-spacing: 2px; color: #4ade80; margin: 20px 0; }
  p { color: #94a3b8; font-size: 14px; line-height: 1.6; }
  .step { background: #0f172a; border-radius: 8px; padding: 12px; margin: 8px 0;
          font-size: 13px; text-align: left; color: #cbd5e1; }
  .step span { color: #38bdf8; font-weight: bold; }
</style>
</head>
<body>
<div class="box">
  <h1>LINE Bot セットアップ</h1>
  <p>2AI Orchestratorをフォローすると<br>収益レポートが届きます</p>
  <div class="id">@317fpwfv</div>
  <div class="step"><span>Step 1</span> LINEアプリを開く</div>
  <div class="step"><span>Step 2</span> 友だち追加 → ID検索</div>
  <div class="step"><span>Step 3</span> <strong>@317fpwfv</strong> を検索してフォロー</div>
  <div class="step"><span>Step 4</span> フォロー後、自動的にUser IDが取得されます</div>
  <p style="margin-top:20px; font-size:12px;">フォロー完了後に <a href="/debug/line-user-id" style="color:#38bdf8;">/debug/line-user-id</a> で確認できます</p>
</div>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/test/translate")
async def test_translate():
    """Smoke test: run a translation through Claude and return result."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Not initialized")

    task = {
        "type": "translation",
        "channel": "direct_translation",
        "source_text": "The quick brown fox jumps over the lazy dog. This is a test of the autonomous translation pipeline.",
        "source_lang": "English",
        "target_lang": "Japanese",
        "domain": "general",
    }
    task_id = await _orchestrator.enqueue_task(task)
    return {"queued": True, "task_id": task_id, "message": "翻訳タスクをキューに追加しました。/statusで結果を確認してください。"}


@app.get("/market/quote/{symbol}")
async def market_quote(symbol: str):
    """Get live stock quote from Alpha Vantage."""
    if not _market:
        raise HTTPException(status_code=503, detail="Not initialized")
    quote = await _market.get_quote(symbol.upper())
    if not quote:
        raise HTTPException(status_code=404, detail=f"Quote not found for {symbol}")
    return quote


@app.get("/test/alpaca")
async def test_alpaca():
    """Smoke test: check Alpaca account status via Cognito auth."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Not initialized")
    task = {"type": "trading", "channel": "alpaca", "action": "status"}
    task_id = await _orchestrator.enqueue_task(task)
    return {"queued": True, "task_id": task_id, "message": "Alpacaアカウント確認タスクをキューに追加。/statusで結果を確認。"}


@app.get("/test/alpaca/trade")
async def test_alpaca_trade():
    """Smoke test: run one trading analysis cycle."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Not initialized")
    task = {"type": "trading", "channel": "alpaca", "action": "analyze", "symbols": ["AAPL", "MSFT", "NVDA"]}
    task_id = await _orchestrator.enqueue_task(task)
    return {"queued": True, "task_id": task_id, "message": "トレード分析タスクをキューに追加。/statusで結果を確認。"}



@app.get("/debug/av-test")
async def debug_av_test():
    """Test Alpha Vantage directly from Railway."""
    import httpx as _httpx
    av_key = _config.alpha_vantage_api_key if _config else ""
    results = {"av_key_set": bool(av_key), "av_key_prefix": av_key[:4] if av_key else ""}
    try:
        async with _httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://www.alphavantage.co/query",
                params={"function": "TIME_SERIES_DAILY", "symbol": "SPY", "outputsize": "compact", "apikey": av_key}
            )
            data = resp.json()
            results["status_code"] = resp.status_code
            results["top_keys"] = list(data.keys())[:5]
            results["has_ts"] = "Time Series (Daily)" in data
            if "Note" in data:
                results["note"] = data["Note"]
            if "Information" in data:
                results["information"] = data["Information"]
            if "Time Series (Daily)" in data:
                dates = sorted(data["Time Series (Daily)"].keys(), reverse=True)
                results["days_count"] = len(dates)
                results["latest_date"] = dates[0] if dates else None
    except Exception as e:
        results["error"] = str(e)
    return results
@app.get("/debug/tasks")
async def debug_tasks():
    """Return recent tasks with full error messages for debugging."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Not initialized")
    db = _orchestrator.db
    with db._conn() as c:
        rows = c.execute(
            "SELECT id, task_type, channel, status, result_data, error_msg, created_at FROM tasks ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
    return [
        {"id": r[0], "type": r[1], "channel": r[2], "status": r[3],
         "result": r[4], "error": r[5], "created_at": r[6]}
        for r in rows
    ]


@app.get("/market/forex/{from_currency}/{to_currency}")
async def market_forex(from_currency: str, to_currency: str):
    """Get forex exchange rate from Alpha Vantage."""
    if not _market:
        raise HTTPException(status_code=503, detail="Not initialized")
    rate = await _market.get_forex_rate(from_currency.upper(), to_currency.upper())
    if not rate:
        raise HTTPException(status_code=404, detail="Forex rate not found")
    return rate


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("orchestrator.main:app", host="0.0.0.0", port=port, reload=False)

@app.post("/debug/webhook-test")
async def debug_webhook_test(background_tasks: BackgroundTasks):
    """Simulate a LINE message to test dev_agent v3 (no signature required)."""
    import os as _os
    test_text = "v3 test: can you see shared_context.md?"
    user_id = _db.get_config("line_user_id") or _os.getenv("LINE_USER_ID", "")
    if not user_id:
        return {"error": "no line_user_id configured"}
    background_tasks.add_task(_process_dev_agent_message, test_text, "debug-reply-token", user_id)
    return {"success": True, "message": test_text, "user_id_prefix": user_id[:15] + "..."}


async def _process_dev_agent_message(text: str, reply_token: str, user_id: str):
    """Internal helper to process a message through dev_agent."""
    try:
        response = await _orchestrator.dev_agent.run(text)
        if len(response) <= 2000:
            await _line.send_text(response, user_id=user_id)
        else:
            for i in range(0, len(response), 2000):
                await _line.send_text(response[i:i+2000], user_id=user_id)
    except Exception as e:
        logger.error("dev_agent test error: %s", e)
        await _line.send_text("dev_agent error: " + str(e)[:200], user_id=user_id)

