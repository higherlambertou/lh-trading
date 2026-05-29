import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.broker import broker
from core.quote_hub import quote_hub
from api.routes_order import router as order_router
from api.routes_position import router as position_router
from api.routes_strategy import router as strategy_router, strategy_engine
from api.routes_quote import router as quote_router
from core.manual_monitor import manual_monitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)


async def _keepalive_loop() -> None:
    """每 4 分鐘輕量 ping 一次，保持 session 活躍並提早偵測斷線。"""
    while True:
        await asyncio.sleep(240)
        try:
            broker.call(lambda: broker.api.list_positions(broker.api.futopt_account))
            logger.debug("keepalive OK")
        except Exception as e:
            logger.warning("keepalive 失敗（已嘗試重連）: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    broker.login()
    loop = asyncio.get_event_loop()
    strategy_engine.loop = loop
    quote_hub.setup(loop)
    manual_monitor.setup(loop)
    keepalive_task = loop.create_task(_keepalive_loop())
    logger.info("系統啟動完成")
    yield
    keepalive_task.cancel()
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3002", "http://localhost:5173"],
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
    import os
    dev_mode = os.getenv("DEV", "false").lower() == "true"
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8002,
        reload=dev_mode,
        reload_dirs=["api", "core", "strategies"] if dev_mode else None,
    )
