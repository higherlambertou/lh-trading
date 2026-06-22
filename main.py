import asyncio
import logging
import os
from contextlib import asynccontextmanager

# ── 除錯：凍結時用 `kill -USR1 <pid>` 把所有 thread 的 Python 堆疊印到 log ──
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


async def _keepalive_loop() -> None:
    """shioaji 的 Solace session 本身有心跳保活機制，不需要額外 ping。
    list_positions keepalive 已移除：在 Solace 不穩時此呼叫持 GIL 凍住整個進程。
    此 task 保留但為空迴圈，避免改動 lifespan 的 task 管理結構。"""
    while True:
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    broker.login()
    loop = asyncio.get_event_loop()
    strategy_engine.loop = loop
    quote_hub.setup(loop)
    manual_monitor.setup(loop)

    # 啟動就訂閱三檔期貨：讓 dashboard 閒置（沒跑策略）時也有台指報價可看。
    # 關鍵：subscribe / snapshots 都是同步 shioaji 呼叫，開盤時 Solace 慢就會卡住。
    # 若直接在 startup event loop 執行，uvicorn 永遠綁不上 port → watchdog 誤殺。
    # 全部丟到背景 task + broker.acall（executor）：startup 立刻完成、port 立刻開，
    # 訂閱與底價種入在背景非同步補上。
    async def _startup_bg() -> None:
        for getter in ("tmf_contract", "mxf_contract", "txf_contract"):
            try:
                c = await broker.acall(lambda g=getter: getattr(broker, g)())
                await broker.acall(
                    lambda contract=c: quote_hub.ensure_contract_subscribed(contract)
                )
            except Exception as e:
                logger.warning("啟動訂閱期貨 %s 失敗: %s", getter, e)

    tick_recorder.start()
    keepalive_task = loop.create_task(_keepalive_loop())
    startup_task = loop.create_task(_startup_bg())
    cache_task = loop.create_task(cache_refresh_loop())
    logger.info("系統啟動完成")
    yield
    keepalive_task.cancel()
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
