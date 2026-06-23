import asyncio
import logging
import os
from contextlib import asynccontextmanager

import faulthandler
import signal

faulthandler.enable()
if hasattr(signal, "SIGUSR1"):
    faulthandler.register(signal.SIGUSR1, all_threads=True)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.broker import broker
from core.quote_hub import quote_hub
from api.routes_order import router as order_router
from api.routes_position import router as position_router, cache_refresh_loop
from api.routes_strategy import router as strategy_router, strategy_engine
from api.routes_quote import router as quote_router
from core.manual_monitor import manual_monitor
from core.tick_store import tick_recorder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)


async def _startup_bg() -> None:
    """等 worker 連線後訂閱三檔期貨，讓 dashboard 閒置時也有報價。"""
    for _ in range(180):          # 最多等 3 分鐘
        if broker.is_connected:
            break
        await asyncio.sleep(1)

    if not broker.is_connected:
        logger.warning("Worker 未在 180s 內連線，略過啟動訂閱")
        return

    for code in ("TMF", "MXF", "TXF"):
        try:
            await broker.subscribe(code)
            logger.info("啟動訂閱期貨 %s 完成", code)
        except Exception as e:
            logger.warning("啟動訂閱期貨 %s 失敗: %s", code, e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_event_loop()
    broker.setup(loop)
    broker.login()           # 啟動 shioaji_worker 子進程（非阻塞）
    strategy_engine.loop = loop
    quote_hub.setup(loop)
    manual_monitor.setup(loop)

    tick_recorder.start()
    startup_task   = loop.create_task(_startup_bg())
    cache_task     = loop.create_task(cache_refresh_loop())
    logger.info("系統啟動完成（等待 worker 連線中…）")
    yield
    startup_task.cancel()
    cache_task.cancel()
    tick_recorder.stop()
    await strategy_engine.stop_all()
    await manual_monitor.shutdown()
    broker.logout()
    logger.info("系統已關閉")


app = FastAPI(
    title="lh-trading TXF Trader",
    description="台指期貨程式交易後端 (Shioaji + FastAPI)",
    version="0.1.0",
    lifespan=lifespan,
)

_cors_origins = [
    o.strip()
    for o in os.getenv(
        "CORS_ORIGINS", "http://localhost:3002,http://localhost:5173"
    ).split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(order_router,    prefix="/api/order",    tags=["order"])
app.include_router(position_router, prefix="/api/position", tags=["position"])
app.include_router(strategy_router, prefix="/api/strategy", tags=["strategy"])
app.include_router(quote_router,    prefix="/api/quote",    tags=["quote"])


@app.get("/api/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "broker_connected": str(broker.is_connected),
    }


if __name__ == "__main__":
    import uvicorn
    dev_mode = os.getenv("DEV", "false").lower() == "true"
    uvicorn.run(
        "main:app",
        host=os.getenv("BIND_HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8002")),
        reload=dev_mode,
        reload_dirs=["api", "core", "strategies"] if dev_mode else None,
    )
