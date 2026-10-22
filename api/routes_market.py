import asyncio
import logging
import time
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.routes_position import _cache as position_cache
from api.routes_strategy import strategy_engine
from core import forecast_log, precheck, range_suggest, stop_suggest
from core.broker import broker
from core.daily_summary import TICK_SEC, market_state
from core.flow_store import flow_recorder
from core.live_state import FLOW_DOWN, FLOW_MARGIN, FLOW_UP
from core.viz_replay import build_replay, validate as validate_replay

logger = logging.getLogger(__name__)
router = APIRouter()


class IVRequest(BaseModel):
    iv: float = Field(gt=0, lt=300, description="ATM 隱含波動率，單位 %（例如 18.5）")
    date: Optional[str] = Field(default=None, description="YYYY-MM-DD；不給 = 今日（會重算今日狀態）")


class NoteRequest(BaseModel):
    basis: Optional[str] = Field(default=None, max_length=2000)   # 判斷依據（你看到了什麼）
    notes: Optional[str] = Field(default=None, max_length=2000)   # 備註


async def market_state_loop() -> None:
    """背景：等券商連線 → 載入/計算今日狀態 → 每 30s 取樣策略績效 + 盤前/盤後排程。"""
    for _ in range(180):
        if broker.is_connected:
            break
        await asyncio.sleep(1)
    if broker.is_connected:
        # 讓啟動訂閱（_startup_bg）與快取刷新先佔 worker：日K查詢會讓 worker 排隊指令
        await asyncio.sleep(10)
    try:
        await market_state.startup()
    except Exception as e:
        logger.warning("市場狀態啟動計算失敗（之後由排程重試/手動 refresh）: %r", e)
    while True:
        await asyncio.sleep(TICK_SEC)
        try:
            await market_state.tick(strategy_engine.strategies)
        except Exception as e:
            logger.warning("市場狀態排程例外: %r", e)


@router.get("/state")
def get_state() -> dict[str, Any]:
    """今日市場狀態（純讀記憶體快取，不對券商發查詢）。"""
    return market_state.snapshot()


@router.get("/precheck")
async def get_precheck() -> dict[str, Any]:
    """下單前檢查（只顯示）：今天預期日盤振幅、隱含波動百分位、36 小時內的美國重要事件。讀記憶體＋唯讀的 lh-intel（在執行緒裡），不查券商。"""
    now = time.time()
    ctx = None
    try:
        ctx = (await market_state.live_snapshot()).get("ctx")
    except Exception:  # noqa: BLE001
        logger.debug("precheck：取不到盤中脈絡（略過）", exc_info=True)
    events = await asyncio.to_thread(precheck.fetch_events, now)
    evening = await asyncio.to_thread(precheck.fetch_evening, now, market_state.store)       # 13:45～05:00：今晚夜盤與下一個日盤的預期振幅（發現 67）
    hits = await asyncio.to_thread(forecast_log.summary, market_state.store.path)            # 預測的命中率（發現 68）
    return precheck.build(ctx, market_state.snapshot(), events, now, evening, hits)


@router.get("/live")
async def get_live() -> dict[str, Any]:
    """盤中即時狀態：真實成交的外/內盤比例、日盤振幅、與盤前判斷是否同向（純讀記憶體，僅供顯示）。
    用 async（跑在 event loop）讀取，與 tick 餵入同一執行緒，不需要鎖。"""
    return await market_state.live_snapshot()


def _bar(r: dict[str, Any]) -> Optional[dict[str, Any]]:
    """flow_1m 的一列（或進行中的那一分鐘）→ 走勢圖的一根 K 棒；沒有價格的丟掉。v＝成交口數、b／s＝外盤／內盤口數、n＝成交筆數。"""
    try:
        o, h, lo, c = (float(r[k]) for k in ("open", "high", "low", "close"))
    except (KeyError, TypeError, ValueError):
        return None
    if not (o > 0 and h > 0 and lo > 0 and c > 0):
        return None
    buy, sell, unk = int(r.get("buy_vol") or 0), int(r.get("sell_vol") or 0), int(r.get("unk_vol") or 0)
    return {"t": int(r["ts"]), "o": o, "h": h, "l": lo, "c": c, "v": buy + sell + unk, "b": buy, "s": sell,
            "n": int(r.get("buy_n") or 0) + int(r.get("sell_n") or 0) + int(r.get("unk_n") or 0)}


@router.get("/bars")
async def get_bars(minutes: int = Query(default=1500, ge=5, le=4000), since: Optional[float] = Query(default=None, ge=0)) -> dict[str, Any]:
    """微台（TMF）真實成交的 1 分 K（走勢圖用，update.md 發現 42）：資料來自後端每分鐘記錄的 flow_1m（日盤＋夜盤），
    加上目前這一分鐘（還沒寫進資料庫）的進行中那一根。只讀本機資料庫與記憶體，不查券商、不碰下單。
    minutes＝往前看幾分鐘（預設 1,500＝約 25 小時）；since＝只要這個時間（epoch 秒）以後的（前端第一次抓全部、之後只抓增量）。
    後端沒開著的分鐘沒有資料，K 棒會有缺口。"""
    now = time.time()
    lo = since if since is not None else now - minutes * 60
    rows = await asyncio.to_thread(market_state.store.flow_1m, "TMF", lo)
    bars = [b for b in (_bar(r) for r in rows) if b is not None]
    fills_at = 0.0
    try:                                    # 後端沒在記錄的分鐘：用盤後回補的券商 1 分 K 補畫（f＝1，畫成灰色；沒有外內盤；發現 66）
        fills, fills_at = await asyncio.to_thread(lambda: (market_state.store.fill_1m("TMF", lo), market_state.store.fills_at("TMF")))
        have = {b["t"] for b in bars}
        extra = [{"t": int(f["ts"]), "o": f["open"], "h": f["high"], "l": f["low"], "c": f["close"], "v": int(f["volume"] or 0), "b": 0, "s": 0, "n": 0, "f": 1}
                 for f in fills if int(f["ts"]) not in have and f["open"] and f["close"]]
        if extra:
            bars = sorted(bars + extra, key=lambda b: b["t"])
    except Exception:  # noqa: BLE001
        logger.debug("bars：讀回補的 K 棒失敗（略過）", exc_info=True)
    live = None
    try:
        cur = flow_recorder.current_row()
        live = _bar(cur) if cur else None
    except Exception:  # noqa: BLE001
        logger.debug("bars：取不到進行中的 K 棒（略過）", exc_info=True)
    if live is not None and since is not None and live["t"] < since:
        live = None
    return {"contract": "TMF", "interval": 60, "now": now, "bars": bars, "live": live, "fills_at": fills_at}


@router.get("/suggest")
async def get_suggest(contract: str = "TMF", horizon: int = 15) -> dict[str, Any]:
    """依波動的建議停損停利（update.md 發現 33）：最近 30 分鐘的 σ、持有時間 T 的雜訊範圍、三組停損停利與它們的基準勝率、要靠時機多贏幾個百分點才打平成本、
    單口虧多少與單筆風險限額下最多幾口。只讀本機資料庫與記憶體，不對券商發查詢；只顯示，不碰下單。"""
    contract = contract.upper()
    if contract not in stop_suggest.POINT_VALUE:
        raise HTTPException(422, "contract 要是 TMF／MXF／TXF")
    if horizon not in stop_suggest.HORIZONS:
        raise HTTPException(422, f"horizon 要是 {list(stop_suggest.HORIZONS)} 其中之一（分鐘）")
    now = time.time()
    rows = await asyncio.to_thread(market_state.store.flow_1m, "TMF", now - 3600)
    live: Optional[dict[str, Any]] = None
    try:
        live = await market_state.live_snapshot()                       # 取今天的尺度（已走多少、預期還會走多少）；壞了不影響主要內容
    except Exception:
        logger.debug("suggest：取不到盤中時段脈絡（略過）", exc_info=True)
    margin = position_cache.get("margin") or {}
    equity = margin.get("equity")
    return stop_suggest.suggest_now(rows, now, contract=contract, horizon=horizon, equity=float(equity) if equity else None,
                                    table=stop_suggest.load_table(), ctx=(live or {}).get("ctx"))


@router.get("/range")
async def get_range() -> dict[str, Any]:
    """機率範圍帶（update.md 發現 48）：以最新成交價為中心，T＝5／15／30／60 分鐘與「到收盤」的 50%／80%／95% 範圍（落點與期間兩種），
    用最近 30 分鐘的 σ 與歷史實測表（core/range_calibration.json）算。**只說會走多遠、不說往哪個方向**；只讀本機資料庫與記憶體，不對券商發查詢；只顯示，不碰下單。"""
    now = time.time()
    rows = await asyncio.to_thread(market_state.store.flow_1m, "TMF", now - 3600)
    live_row = None
    try:
        live_row = flow_recorder.current_row()                          # 進行中的那一分鐘（還沒寫進資料庫）；壞了不影響主要內容
    except Exception:  # noqa: BLE001
        logger.debug("range：取不到進行中的 K 棒（略過）", exc_info=True)
    return range_suggest.range_now(rows, now, live_row=live_row, table=range_suggest.load_table())


@router.post("/refresh")
async def refresh(force: bool = False) -> dict[str, Any]:
    """立刻重算今日狀態（向券商抓日K + ATM 選擇權報價）。
    worker 是單執行緒，查詢期間下單指令會排隊，所以有策略執行中時預設拒絕。"""
    running = [n for n, s in strategy_engine.strategies.items() if s.state.is_running]
    if running and not force:
        raise HTTPException(
            409, f"策略 {running[0]} 執行中：查詢期間 worker 會暫時排隊下單指令。"
                 "確定要重算請加 ?force=true"
        )
    await market_state.refresh("manual")
    return market_state.snapshot()          # 與 GET /state 同形狀（含 ready / config）


@router.post("/iv")
async def set_iv(req: IVRequest) -> dict[str, Any]:
    """手動輸入 ATM IV（%）。今日 → 重算今日狀態；過去日期 → 只回填歷史。"""
    if req.date:
        try:
            date.fromisoformat(req.date)
        except ValueError:
            raise HTTPException(422, "date 格式需為 YYYY-MM-DD")
    await market_state.set_manual_iv(req.iv, req.date)
    return market_state.snapshot()          # 與 GET /state 同形狀（含 ready / config）


@router.get("/journal")
def get_journal(limit: int = 30) -> list[dict[str, Any]]:
    """每日日誌（新→舊）：市場判斷 + 當日策略結果 + 你填的判斷依據/備註。"""
    return market_state.store.list_journal(market_state.mode, max(1, min(limit, 365)))


@router.patch("/journal/{day}")
def patch_journal(day: str, req: NoteRequest) -> dict[str, str]:
    if not market_state.store.set_note(day, market_state.mode, req.basis, req.notes):
        raise HTTPException(404, f"找不到 {day} 的日誌")
    return {"status": "updated", "date": day}


@router.get("/stats")
def get_stats(strategy: str = "scalp") -> dict[str, Any]:
    """驗證統計：各市場狀態下該策略的勝率/賺賠比；各 IV 狀態下的大波動比例。"""
    return market_state.store.stats(market_state.mode, strategy)


@router.get("/replay")
async def get_replay(
    days: int = Query(40, ge=5, le=250, description="最近幾個日盤資料完整的交易日"),
    block_min: int = Query(15, ge=5, le=60, description="每格幾分鐘（也是預期報酬的期間）"),
    flow_window: int = Query(100, ge=20, le=500, description="買賣力道看最近幾筆成交（即時面板是 100）"),
    flow_up: float = Query(FLOW_UP, gt=0.5, lt=1, description="外盤占比 ≥ 此值 → 買方主動"),
    flow_down: float = Query(FLOW_DOWN, gt=0, lt=0.5, description="外盤占比 ≤ 此值 → 賣方主動"),
    flow_margin: float = Query(FLOW_MARGIN, ge=0, lt=0.2, description="遲滯帶寬度"),
    neutral_band: float = Query(0.05, ge=0, lt=0.45, description="|H−0.5| 在此範圍內是灰色（中性）"),
    full_at: float = Query(0.10, gt=0, le=0.5, description="|H−0.5| 到此值顏色全開"),
    check: bool = Query(True, alias="validate", description="同時做統計驗證（排列檢定＋CUSUM）"),
    perms: int = Query(2000, ge=200, le=10000, description="排列檢定的次數"),
) -> dict[str, Any]:
    """市場指標視覺化的歷史回放（Hurst 色相、IV 飽和度、外/內盤形狀）＋統計驗證。純讀本機歷史資料，不連券商。
    需求文件規定：這個驗證沒通過之前，不能當交易訊號、也不做即時版。"""
    if full_at <= neutral_band:
        raise HTTPException(422, "full_at 必須大於 neutral_band")
    if flow_down >= flow_up:
        raise HTTPException(422, "flow_down 必須小於 flow_up")

    def work() -> dict[str, Any]:
        rep = build_replay(market_state.store, days=days, block_min=block_min, flow_window=flow_window, flow_up=flow_up,
                           flow_down=flow_down, flow_margin=flow_margin, neutral_band=neutral_band, full_at=full_at)
        if check:
            rep["validation"] = validate_replay(rep, n_perm=perms)
        return rep

    return await asyncio.to_thread(work)                     # 檢定是 CPU 運算，別卡住 event loop
