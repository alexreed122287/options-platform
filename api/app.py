"""FastAPI app: serves the single-page dashboard and the /api routes."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from api.deps import get_deps
from api.routes_market import router as market_router
from api.routes_trading import router as trading_router
from data.env import ROOT

APP_VERSION = "1.0.0"

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


@app.get("/")
async def index():
    page = ROOT / "web" / "index.html"
    if page.exists():
        return FileResponse(page)
    return JSONResponse({"message": "dashboard not built yet - see /docs for the API"})
