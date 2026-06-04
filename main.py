import asyncio
import logging
import os
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
    """每 4 分鐘輕量 ping 一次，保持 session 活躍並提早偵測斷線。

    用 acall（在 worker thread 執行）而非同步 call：否則一旦永豐回應卡住，
    list_positions 會卡在 event loop 上、凍結整個服務（health 變 000）。
    再包一層 wait_for 硬逾時，確保即使 worker 卡死，keepalive 任務也不會
    永遠懸著、下一輪仍會繼續嘗試。
    """
    while True:
        await asyncio.sleep(240)
        try:
            await asyncio.wait_for(
                broker.acall(
                    lambda: broker.api.list_positions(broker.api.futopt_account)
                ),
                timeout=15,
            )
            logger.debug("keepalive OK")
        except asyncio.TimeoutError:
            logger.warning("keepalive 逾時（永豐回應卡住，session 可能異常）")
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

# 每台機器的 Tailscale IP 不同 → 由 .env 的 CORS_ORIGINS 指定（逗號分隔），
# 沒設時退回 localhost，main.py 因此可兩台共用、安心 push。
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
    # BIND_HOST 由各機器 .env 指定（建議填該機 Tailscale IP 做硬化）；
    # 沒設時退回 0.0.0.0，確保任何機器都能直接起得來。
    uvicorn.run(
        "main:app",
        host=os.getenv("BIND_HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8002")),
        reload=dev_mode,
        reload_dirs=["api", "core", "strategies"] if dev_mode else None,
    )
