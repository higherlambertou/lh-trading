import asyncio
import logging
import os
from contextlib import asynccontextmanager

import faulthandler
import signal
from dotenv import load_dotenv

load_dotenv()  # .env → os.environ，讓 CORS_ORIGINS/BIND_HOST/PORT 等設定生效

faulthandler.enable()
if hasattr(signal, "SIGUSR1"):
    faulthandler.register(signal.SIGUSR1, all_threads=True)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.broker import broker
from core.quote_hub import quote_hub
from api.routes_order import router as order_router, trades_refresh_loop
from api.routes_position import router as position_router, cache_refresh_loop, positions_refresh_loop, _cache as _position_cache
from api.routes_strategy import router as strategy_router, strategy_engine
from api.routes_quote import router as quote_router
from api.routes_market import router as market_router, market_state_loop
from api.routes_tradelog import router as tradelog_router
from api.routes_risk import router as risk_router
from api.routes_backtest import router as backtest_router, manager as backtest_manager
from api.routes_options import router as options_router
from api.routes_premarket import router as premarket_router
from core.option_spread_log import spread_log_loop
from core.premarket_log import premarket_log_loop
from core.manual_monitor import manual_monitor
from core.startup_protection import protect_after_start
from core import fill_notify
from core.reminders import reminder_loop
from core.event_reminder import event_reminder_loop
from core.daily_summary import market_state
from core.vol_watch import vol_watch_loop
from core.daily_brief import brief_loop
from core import weekly_report
from core.premarket_log import TwCalendar
from core.session_watch import TradingDays, session_watch, session_watch_loop
from core.log_throttle import install_access_filter
from core.taifex_update import taifex_update_loop
from core.bar_gapfill import gapfill_loop
from core.signal_shadow import shadow_loop
from core import entry_log
from core.live_state import live_state
from api.routes_intel import router as intel_router
from api.routes_signals import router as signals_router
from api.routes_system import router as system_router
from core import rollover
from core.tick_store import tick_recorder
from core.flow_store import flow_recorder
from core.trade_log import current_mode, trade_log

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

    try:        # 進場市場狀態紀錄：開始記錄前就存在的部位，被平倉時才不會被誤判成新進場
        trade_log.record_positions(await broker.list_positions())
    except Exception as e:
        logger.warning("進場紀錄對帳：取得券商持倉失敗（略過）: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    install_access_filter()                  # 成功的 GET 輪詢不記存取紀錄（占 app log 96%；update.md 發現 66；ACCESS_LOG=all 全記）
    loop = asyncio.get_event_loop()
    broker.setup(loop)
    broker.login()           # 啟動 shioaji_worker 子進程（非阻塞）
    strategy_engine.loop = loop
    quote_hub.setup(loop)
    manual_monitor.setup(loop)

    entry_log.flow_provider = live_state.snapshot          # 進場快照要記的內外盤（面板上同一組數字；只讀記憶體，失敗只記 null 與原因）
    tick_recorder.start()
    flow_recorder.start()
    trade_log.start()
    startup_task   = loop.create_task(_startup_bg())
    cache_task     = loop.create_task(cache_refresh_loop())
    positions_task = loop.create_task(positions_refresh_loop())
    trades_task    = loop.create_task(trades_refresh_loop())
    market_task    = loop.create_task(market_state_loop())
    spread_task    = (loop.create_task(spread_log_loop(broker, quote_hub))      # 選擇權買賣價差的時段紀錄（只記錄；OPTION_SPREAD_LOG=false 關閉）
                      if os.getenv("OPTION_SPREAD_LOG", "true").lower() == "true" else None)
    premarket_task = loop.create_task(premarket_log_loop())               # 盤前跨市場資訊紀錄（只記錄、不影響交易；PREMARKET_LOG=false 關閉）
    fill_notify.install(trade_log)                                       # 手動單成交就推播（NOTIFY_FILLS=manual|all|off；只通知、不動委託）
    reminder_task = loop.create_task(reminder_loop(lambda: _position_cache["positions"], manual_monitor.list_watches))    # 當沖倉位收盤前提醒（13:15、13:25）
    cpi_task = loop.create_task(event_reminder_loop(lambda: _position_cache["positions"], manual_monitor.list_watches))    # CPI 公布當天的持倉提醒（只通知；EVENT_REMINDER=false 關閉）
    protect_task = loop.create_task(protect_after_start())                # 重啟後：恢復手動停損監控、報告沒有保護的持倉（自己等券商連上；不下單）
    vol_task = loop.create_task(vol_watch_loop(lambda: _position_cache["positions"], manual_monitor.list_watches, market_state.store))   # 波動放大提醒（有沒有持倉都發；只通知；VOL_WATCH=false 關閉）
    brief_task = loop.create_task(brief_loop(market_state.snapshot, market_state.store))   # 盤前摘要（只通知；DAILY_BRIEF=false 關閉）
    weekly_task = loop.create_task(weekly_report.weekly_loop(          # 週報（週六 10:00；券商已實現損益一週查一次；只通知；WEEKLY_REPORT=false 關閉）
        lambda b, e: broker.list_profit_loss(b, e),
        lambda m, s: weekly_report.stop_counts(trade_log.path, current_mode(), m, s)))
    shadow_task = loop.create_task(shadow_loop(market_state.store))      # 順勢爆量訊號的影子模式（只記錄與通知、不下單；SIGNAL_SHADOW=false 關閉）
    rollover_task = loop.create_task(rollover.rollover_loop(lambda: broker.resolved_codes))   # 近月合約結算後要求 watchdog 重啟換月（ROLLOVER_RESTART=false 關閉）
    session_task = loop.create_task(session_watch_loop(lambda: bool(broker._known_subs), TradingDays(TwCalendar().check)))   # 券商連線假死偵測（/api/health 回報 stale → watchdog 重啟；SESSION_WATCH=false 關閉）
    taifex_task = loop.create_task(taifex_update_loop())                  # 期交所日資料自動更新（獨立子行程；只有正式盤跑；TAIFEX_UPDATE=false 關閉）
    gapfill_task = loop.create_task(gapfill_loop(market_state.store, broker.kbars, lambda: broker.is_connected))   # 走勢圖缺口回補（盤前／盤後各一次；只有正式盤；GAPFILL=false 關閉）
    logger.info("系統啟動完成（等待 worker 連線中…）")
    yield
    startup_task.cancel()
    cache_task.cancel()
    positions_task.cancel()
    trades_task.cancel()
    market_task.cancel()
    if spread_task:
        spread_task.cancel()
    premarket_task.cancel()
    protect_task.cancel()
    reminder_task.cancel()
    cpi_task.cancel()
    vol_task.cancel()
    brief_task.cancel()
    weekly_task.cancel()
    rollover_task.cancel()
    session_task.cancel()
    taifex_task.cancel()
    gapfill_task.cancel()
    shadow_task.cancel()
    tick_recorder.stop()
    flow_recorder.stop()
    backtest_manager.shutdown()          # 回測子行程不能變成孤兒繼續吃 CPU
    await strategy_engine.stop_all()
    await manual_monitor.shutdown()
    broker.logout()
    trade_log.stop()          # 最後才停：收盤前的取消委託/成交回報也要寫進去
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
app.include_router(market_router,   prefix="/api/market",   tags=["market"])
app.include_router(tradelog_router, prefix="/api/tradelog", tags=["tradelog"])
app.include_router(risk_router,     prefix="/api/risk",     tags=["risk"])
app.include_router(backtest_router, prefix="/api/backtest", tags=["backtest"])
app.include_router(options_router,  prefix="/api/options",  tags=["options"])
app.include_router(premarket_router, prefix="/api/premarket", tags=["premarket"])
app.include_router(signals_router,  prefix="/api/signals",  tags=["signals"])
app.include_router(intel_router,    prefix="/api/intel",    tags=["intel"])
app.include_router(system_router,   prefix="/api/system",   tags=["system"])


@app.get("/api/health")
def health() -> dict[str, str]:
    stale = session_watch.stale_reason                # 連線假死（行程活著、券商那邊沒報價或查詢一直失敗；update.md 發現 65）：當成沒連上，watchdog 才會重啟
    out = {
        "status": "ok",
        "broker_connected": str(broker.is_connected and not stale),
    }
    if stale:
        out["session"] = "stale"
        out["stale_reason"] = stale
    if rollover.wanted():                           # 近月合約已結算、要 watchdog 重啟換月（core/rollover.py）；只讀記憶體
        out["restart_wanted"] = rollover.wanted() or ""
    return out


@app.get("/api/health/session")
def health_session() -> dict:
    """連線假死偵測的內部狀態（除錯用；只讀記憶體）。"""
    return session_watch.snapshot()


if __name__ == "__main__":
    import uvicorn
    from core.bind_check import preflight
    dev_mode = os.getenv("DEV", "false").lower() == "true"
    preflight(os.getenv("BIND_HOST", "0.0.0.0"), int(os.getenv("PORT", "8002")))    # Tailscale 位址不在就在這裡結束，不要先開 worker、登入券商（update.md 發現 36）
    uvicorn.run(
        "main:app",
        host=os.getenv("BIND_HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8002")),
        reload=dev_mode,
        reload_dirs=["api", "core", "strategies"] if dev_mode else None,
    )
