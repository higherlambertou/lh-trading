import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException

from core.broker import broker

logger = logging.getLogger(__name__)
router = APIRouter()

CACHE_TTL = 10
POSITION_REFRESH_SEC = 5        # 部位背景刷新間隔
PNL_REFRESH_IDLE_SEC = 60       # 已實現損益（帳務查詢，較慢）：沒有策略在跑時
PNL_REFRESH_BUSY_SEC = 300      # 策略執行中降頻：worker 單執行緒，別在下單時搶時間
POSITION_BACKOFF_MAX_SEC = 60    # 部位刷新連續失敗時，間隔最長拉到這麼久
BACKOFF_NOTICE = 3              # 連續失敗幾次就記一行 warning（恢復時再記一行 info）

_cache: dict[str, Any] = {
    "positions": [],
    "pnl": [],
    "margin": None,
    "usage": None,
    "updated_at": 0.0,
    "positions_at": 0.0,        # 部位上次成功刷新的時間（0 = 從未成功）
    "pnl_at": 0.0,
}
_pnl_try_at = 0.0               # 上次「嘗試」刷新損益的時間（失敗也算，避免每輪重打）
_last_error = ""                # 部位刷新最近一次失敗的原因


async def cache_refresh_loop() -> None:
    """背景快取刷新：連線後立刻刷一次，之後每 60s 刷保證金、每 120s 刷流量。"""
    tick = 0
    # 等 broker 連線（最多等 180s），連上後立刻做第一次刷新
    for _ in range(180):
        if broker.is_connected:
            break
        await asyncio.sleep(1)

    while True:
        if broker.is_connected:
            tick += 1
            try:
                _cache["margin"] = await asyncio.wait_for(broker.margin(), timeout=5)
                _cache["updated_at"] = time.time()
            except Exception as e:
                logger.debug("margin cache 刷新失敗（保留舊值）: %s", e)

            if tick == 1 or tick % 2 == 0:
                try:
                    _cache["usage"] = await asyncio.wait_for(broker.usage(), timeout=5)
                except Exception as e:
                    logger.debug("usage cache 刷新失敗（保留舊值）: %s", e)

        await asyncio.sleep(60)


async def refresh_positions() -> bool:
    """向 worker 查一次券商部位並更新快取；失敗保留舊值（資料時效由 positions_at 表示）。"""
    try:
        raw = await asyncio.wait_for(broker.list_positions(), timeout=6)
    except Exception as e:
        global _last_error
        _last_error = repr(e)
        logger.debug("positions cache 刷新失敗（保留舊值）: %r", e)
        return False
    _cache["positions"] = list(raw or [])
    _cache["positions_at"] = time.time()
    return True


async def refresh_pnl() -> bool:
    """向 worker 查一次當日已實現損益並更新快取；失敗保留舊值。"""
    try:
        raw = await asyncio.wait_for(broker.list_profit_loss(), timeout=8)
    except Exception as e:
        logger.debug("pnl cache 刷新失敗（保留舊值）: %r", e)
        return False
    _cache["pnl"] = list(raw or [])
    _cache["pnl_at"] = time.time()
    return True


def _pnl_interval() -> float:
    """策略執行中降頻：帳務查詢會佔住 worker，不要在 scalp 下單的時候搶時間。"""
    try:
        from api.routes_strategy import strategy_engine
        busy = any(s.state.is_running for s in strategy_engine.strategies.values())
    except Exception:
        busy = False
    return PNL_REFRESH_BUSY_SEC if busy else PNL_REFRESH_IDLE_SEC


def next_delay(fails: int) -> float:
    """部位刷新的下一次間隔：成功 ＝ 5 秒；連續失敗 n 次 ＝ 5×2ⁿ 秒（上限 60）。
    券商端故障時（曾在 12:35 連續回 500／逾時）不要 5 秒一次地重試：每次失敗都在 worker 上佔最多 5 秒（下單要排隊），
    還會在 log 洗一整段 traceback。"""
    return min(POSITION_REFRESH_SEC * (2 ** min(max(fails, 0), 10)), POSITION_BACKOFF_MAX_SEC)


async def positions_refresh_loop() -> None:
    """背景：部位每 5 秒、已實現損益每 60 秒（策略執行中 300 秒）向 worker 刷新一次。
    部位連續失敗就退避（見 next_delay），部位都查不到時也不去打損益查詢。

    歷史：這兩個查詢曾在進程內執行、Solace 不穩時會持 GIL 卡死整個服務，所以一度整個拿掉
    （結果儀表板的部位面板永遠是空的）。現在 shioaji 在子進程，查詢卡住只會讓這裡逾時、保留舊值。"""
    global _pnl_try_at
    for _ in range(180):
        if broker.is_connected:
            break
        await asyncio.sleep(1)
    await asyncio.sleep(3)
    fails = 0
    while True:
        if broker.is_connected:
            if await refresh_positions():
                if fails >= BACKOFF_NOTICE:
                    logger.info("部位刷新恢復（先前連續失敗 %d 次），間隔回到 %d 秒", fails, POSITION_REFRESH_SEC)
                fails = 0
                now = time.time()
                if now - _pnl_try_at >= _pnl_interval():
                    _pnl_try_at = now
                    await refresh_pnl()
            else:
                fails += 1
                if fails == BACKOFF_NOTICE:
                    logger.warning("部位刷新連續失敗 %d 次（券商端異常？最近一次：%s），間隔拉長到 %.0f 秒，恢復後自動回到 %d 秒",
                                   fails, _last_error, next_delay(fails), POSITION_REFRESH_SEC)
        await asyncio.sleep(next_delay(fails))


@router.get("/")
def get_positions() -> list[dict[str, Any]]:
    return _cache["positions"]


@router.get("/meta")
def get_meta() -> dict[str, float]:
    now = time.time()

    def age(t: float) -> float:
        return round(now - t, 1) if t else -1

    return {
        "updated_at": _cache["updated_at"],
        "age_sec": age(_cache["updated_at"]),                # 保證金快取的時效（原有欄位）
        "positions_age_sec": age(_cache["positions_at"]),    # 部位（-1 = 從未成功刷新）
        "pnl_age_sec": age(_cache["pnl_at"]),
    }


@router.get("/pnl")
def get_pnl() -> list[dict[str, Any]]:
    return _cache["pnl"]


@router.get("/margin")
def get_margin() -> dict[str, float]:
    if _cache["margin"] is None:
        raise HTTPException(503, "保證金資料尚未就緒（首次查詢中）")
    return _cache["margin"]


@router.get("/usage")
def get_usage() -> dict[str, Any]:
    if _cache["usage"] is None:
        raise HTTPException(503, "流量資料尚未就緒（首次查詢中，請稍後再試）")
    return _cache["usage"]
