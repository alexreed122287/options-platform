"""FastAPI app: serves the single-page dashboard and the /api routes."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from api.deps import get_deps
from api.routes_market import router as market_router
from api.routes_settings import router as settings_router
from api.routes_trading import router as trading_router
from data.env import ROOT, env

APP_VERSION = "1.3.0"

log = logging.getLogger("api.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    deps = get_deps()
    log.info(
        "options-platform v%s starting (alpaca mode=%s, fmp configured=%s, alpaca configured=%s)",
        APP_VERSION,
        "paper" if deps.alpaca.paper else "LIVE",
        deps.fmp.configured,
        deps.alpaca.configured,
    )
    deps.alerts.start()
    yield
    deps.alerts.stop()
    await deps.aclose()


app = FastAPI(title="Options Platform", version=APP_VERSION, lifespan=lifespan)
app.include_router(market_router, prefix="/api")
app.include_router(trading_router, prefix="/api")
app.include_router(settings_router, prefix="/api")


@app.middleware("http")
async def dashboard_token_gate(request: Request, call_next):
    """Optional shared-secret gate for every route. Set DASHBOARD_TOKEN in
    .env whenever the server is bound beyond localhost (HOST=0.0.0.0), so the
    API - including the order endpoints - is not open to the whole network.
    First page load uses /?key=TOKEN; the dashboard then sends the header."""
    token = env("DASHBOARD_TOKEN")
    if token:
        provided = (
            request.headers.get("x-dashboard-token")
            or request.query_params.get("key")
        )
        if provided != token:
            return JSONResponse(
                {"detail": "unauthorized - open /?key=YOUR_DASHBOARD_TOKEN"},
                status_code=401,
            )
    return await call_next(request)


@app.get("/")
async def index():
    page = ROOT / "web" / "index.html"
    if page.exists():
        return FileResponse(page)
    return JSONResponse({"message": "dashboard not built yet - see /docs for the API"})
