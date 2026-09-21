from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings, load_settings
from app.engine import Engine, Hub
from app.executor import PaperAccount, build_executor
from app.market import LighterMarket
from app.model import build_model
from app.public import public_payload

WEB = Path(__file__).resolve().parent.parent / "web"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    hub = Hub()
    market = LighterMarket(
        settings.lighter_base_url,
        account_index=settings.lighter_account_index if settings.live else None,
        api_private_key=settings.lighter_api_private_key if settings.live else None,
        api_key_index=settings.lighter_api_key_index,
    )
    model = build_model(settings)
    account = PaperAccount(settings.paper_equity_usd, settings.markets)
    executor = build_executor(settings, account)
    engine = Engine(settings, market, model, executor, account, hub)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        engine.start()
        yield
        await engine.close()

    app = FastAPI(title="JEV Lighter Trader", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.settings = settings

    if WEB.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/state")
    async def state():
        return public_payload(engine.snapshot_state())

    @app.get("/api/health")
    async def health():
        return {
            "ok": True,
            "mode": settings.trading_mode,
            "backend": settings.resolved_backend,
        }

    @app.post("/api/start")
    @app.post("/api/stop")
    @app.post("/api/tick")
    async def disabled_control():
        return JSONResponse({"ok": False, "error": "disabled"}, status_code=404)

    @app.get("/api/events")
    async def events(request: Request):
        q = hub.subscribe()
        async def gen():
            try:
                snap = json.dumps(public_payload(engine.snapshot_state()), default=str)
                yield f"event: snapshot\ndata: {snap}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        rec = await asyncio.wait_for(q.get(), timeout=15)
                        ev = rec.get("event", "message")
                        payload = json.dumps(public_payload(rec), default=str)
                        yield f"event: {ev}\ndata: {payload}\n\n"
                    except asyncio.TimeoutError:
                        yield "event: ping\ndata: {}\n\n"
            finally:
                hub.unsubscribe(q)
        return StreamingResponse(gen(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        })

    return app


app = create_app()


def run() -> None:
    import uvicorn
    settings = app.state.settings
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
