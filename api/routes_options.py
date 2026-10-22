"""選擇權買方試算 API（唯讀、不下單）：POST /calc 試算買跨式／買勒式／買單向；POST /close 平倉試算；GET /chain 鏈摘要。

資料：即時模式向券商一次抓整個到期月的鏈（broker.option_chain，只在使用者按下試算時查、5 秒內重複查詢用快取——
worker 是單執行緒，不要輪詢）；手動模式用 Black-76 與單一 IV 造一條模擬鏈，離線（休市、沒連券商）也能用。
計算全在 core/options_calc.py（純函式），這裡只做取資料、驗證輸入、組回應；不碰下單、不碰策略。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core import options_calc as oc
from core.broker import broker
from core.iv_monitor import expiry_of
from core.market_store import MarketStore
from core.quote_hub import quote_hub

logger = logging.getLogger(__name__)
router = APIRouter()

CHAIN_TTL = 5.0                                   # 秒：同一個到期月的鏈在這段時間內重複查詢用快取（保護券商查詢額度與單執行緒 worker）
_chain_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_closes_cache: dict[str, Any] = {"day": "", "closes": []}


class Manual(BaseModel):
    """手動（離線）模式：自己給遠期價、IV、距到期天數，用 Black-76 造一條模擬鏈。"""
    F: float = Field(gt=0, description="遠期價（點）")
    iv: float = Field(gt=0, lt=300, description="ATM IV（%）")
    days: float = Field(gt=0, le=800, description="距到期日曆天")
    spread_pct: float = Field(0.02, ge=0, le=1, description="模擬的買賣價差（佔中價）")


class CalcRequest(BaseModel):
    kind: Literal["straddle", "strangle", "call", "put"]
    category: str = "TXO"
    delivery_month: str | None = None
    select: Literal["delta", "move"] = "delta"
    delta: float = Field(0.25, gt=0, lt=1, description="目標 |delta|（勒式的兩翼、或單向那一腳）")
    k: float = Field(0.5, ge=-3, le=5, description="move 選法：履約價距離＝k × 預期波動（單向 k<0 為價內）")
    move_pts: float | None = Field(None, gt=0, description="你預期的波動點數；留空＝市場隱含的 1 個標準差")
    lots: int = Field(1, ge=1, le=1000)
    budget_twd: float | None = Field(None, gt=0)
    hold_hours: float = Field(6.0, ge=0, le=24 * 30)
    fill: Literal["ask", "mid"] = "ask"
    manual: Manual | None = None


class CompareRequest(BaseModel):
    category: str = "TXO"
    delivery_month: str | None = None
    select: Literal["delta", "move"] = "delta"
    delta: float = Field(0.25, gt=0, lt=1, description="勒式兩翼的 |delta|")
    k: float = Field(0.5, ge=0, le=5, description="move 選法：勒式兩翼距 F ± k × 預期波動")
    move_pts: float | None = Field(None, gt=0)
    lots: int = Field(1, ge=1, le=1000)
    hold_days: float = Field(1.0, ge=0, le=800, description="「持有 N 天後」那條曲線的天數（IV 不變）")
    fill: Literal["ask", "mid"] = "ask"
    manual: Manual | None = None


class CloseLeg(BaseModel):
    right: Literal["C", "P"]
    strike: float = Field(gt=0)
    qty: int = Field(1, ge=1, le=1000)
    entry: float = Field(ge=0, description="買進價（點）")
    bid: float | None = Field(None, ge=0, description="目前買價（自己輸入；留空＝用即時報價或模型）")
    ask: float | None = Field(None, ge=0)


class CloseRequest(BaseModel):
    legs: list[CloseLeg] = Field(min_length=1, max_length=8)
    category: str = "TXO"
    delivery_month: str | None = None
    move_pts: float | None = Field(None, gt=0)
    hold_hours: float = Field(24.0, ge=0, le=24 * 30)
    manual: Manual | None = None


def parse_expiry(delivery_date: str, delivery_month: str) -> datetime:
    """契約的交割日（'2026/10/21'）→ 當天 13:30 到期；格式不對退回「第三個週三」的估法。"""
    try:
        y, m, d = (int(x) for x in str(delivery_date).replace("-", "/").split("/")[:3])
        return datetime(y, m, d, 13, 30)
    except (ValueError, TypeError):
        return expiry_of(delivery_month)


async def _live_chain(category: str, delivery_month: str, now: datetime | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    key = (category, delivery_month)
    hit = _chain_cache.get(key)
    under = quote_hub.last_price_by_prefix("TXF")
    if hit and time.time() - hit[0] < CHAIN_TTL:
        raw = hit[1]
    else:
        try:
            raw = await broker.option_chain(delivery_month, category, center=under)
        except asyncio.TimeoutError:
            raise HTTPException(503, "取得選擇權報價逾時（券商連線忙碌）")
        except RuntimeError as e:
            raise HTTPException(503, f"券商尚未連線，無法取得即時報價（{e}）；可以改用手動模式")
        except ValueError as e:
            raise HTTPException(404, str(e))
        _chain_cache[key] = (time.time(), raw)
    expiry = parse_expiry(raw.get("delivery_date", ""), delivery_month)
    now = now or datetime.now()
    T = (expiry - now).total_seconds() / (oc.YEAR_DAYS * 86400)
    try:
        chain = oc.build_chain(raw.get("quotes") or [], T, hint=under)
    except ValueError as e:
        raise HTTPException(422, f"{e}。休市或沒有報價時請改用手動模式")
    ts = max((q.get("ts") or 0 for q in raw.get("quotes") or []), default=0)
    return chain, {"source": "live", "category": category, "delivery_month": delivery_month, "expiry": expiry.isoformat(timespec="minutes"),
                   "quote_ts": ts or None}


def _manual_chain(m: Manual) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        chain = oc.build_chain(oc.model_quotes(m.F, m.iv, m.days, spread_pct=m.spread_pct), m.days / oc.YEAR_DAYS, F=m.F)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return chain, {"source": "manual", "category": None, "delivery_month": None, "expiry": None, "quote_ts": None}


async def _get_chain(category: str, delivery_month: str | None, manual: Manual | None) -> tuple[dict[str, Any], dict[str, Any]]:
    if manual is not None:
        return _manual_chain(manual)
    if not delivery_month:
        raise HTTPException(400, "請選到期月，或改用手動模式（自己輸入遠期價、IV、天數）")
    return await _live_chain(category, delivery_month)


async def _closes() -> list[float]:
    """歷史每日收盤（TXF 日盤，1 分 K 的最後一根）；每天只讀一次資料庫，讀不到就回空（試算照常，只是沒有歷史比較）。"""
    today = date.today().isoformat()
    if _closes_cache["day"] == today:
        return _closes_cache["closes"]
    try:
        closes = await asyncio.to_thread(MarketStore().day_closes, "TXF")
    except Exception:
        logger.warning("讀取歷史收盤失敗（選擇權試算不附歷史比較）", exc_info=True)
        closes = []
    _closes_cache.update(day=today, closes=closes)
    return closes


@router.get("/chain")
async def chain_summary(delivery_month: str, category: str = "TXO", rows: int = 8) -> dict[str, Any]:
    """某個到期月目前的鏈摘要：遠期價、ATM IV、距到期天數、市場隱含的預期波動，以及 ATM 附近各 rows 檔的報價（給畫面當背景資訊）。"""
    chain, meta = await _live_chain(category, delivery_month)
    F = chain["F"]
    near = sorted(chain["rows"], key=lambda r: abs(r["strike"] - F))[: 2 * max(1, min(rows, 40))]
    near.sort(key=lambda r: r["strike"])
    em = oc.expected_move(F, chain["atm_iv"] or 0.0, chain["T"])

    def side(s):
        return None if not s else {k: (round(v, 4) if isinstance(v, float) else v) for k, v in s.items() if k in ("bid", "ask", "mid", "iv", "delta", "spread_pct")}

    return {**meta, "F": round(F, 1), "days": round(chain["T"] * oc.YEAR_DAYS, 2),
            "atm_iv_pct": round(chain["atm_iv"] * 100, 2) if chain["atm_iv"] else None,
            "em_sigma_pts": round(em, 1), "straddle_pts": round(em * (2 / 3.141592653589793) ** 0.5, 1),
            "rows": [{"strike": r["strike"], "call": side(r["call"]), "put": side(r["put"])} for r in near], "as_of": time.time()}


@router.post("/calc")
async def calc(req: CalcRequest) -> dict[str, Any]:
    chain, meta = await _get_chain(req.category, req.delivery_month, req.manual)
    closes = await _closes()
    try:
        legs = oc.select_legs(chain, req.kind, select=req.select, delta=req.delta, k=req.k, ref_move=req.move_pts, fill=req.fill)
        res = await asyncio.to_thread(oc.analyze, chain, legs, kind=req.kind, lots=req.lots, budget_twd=req.budget_twd,
                                      ref_move=req.move_pts, hold_hours=req.hold_hours, closes=closes)
    except ValueError as e:
        raise HTTPException(422, f"{e}" + ("（若是休市或沒有報價，可以改用手動模式）" if meta["source"] == "live" else ""))
    if meta["source"] == "manual":
        res["warnings"].insert(0, "手動模式：價格是用 Black-76 與單一 IV 估的（沒有偏斜、價差是模擬的），只能當示意")
    return {**res, **meta, "as_of": time.time()}


def _marks_for(legs: list[CloseLeg], chain: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """每一腳目前的報價：自己輸入的優先，其次鏈上同履約價同買賣權的報價，都沒有就空（平倉試算會改用模型估）。回傳（marks、用模型估的腳數）。"""
    idx = {(r["strike"], "C"): r["call"] for r in chain["rows"]}
    idx.update({(r["strike"], "P"): r["put"] for r in chain["rows"]})
    out, modeled = [], 0
    for l in legs:
        if l.bid is not None or l.ask is not None:
            out.append({"bid": l.bid or 0.0, "ask": l.ask or 0.0})
            continue
        q = idx.get((float(l.strike), l.right))
        if q and (q["mid"] > 0):
            out.append({"bid": q["bid"], "ask": q["ask"], "close": q["last"], "iv": q["iv"]})
        else:
            out.append({})
            modeled += 1
    return out, modeled


@router.post("/close")
async def close(req: CloseRequest) -> dict[str, Any]:
    chain, meta = await _get_chain(req.category, req.delivery_month, req.manual)
    marks, modeled = _marks_for(req.legs, chain)
    closes = await _closes()
    try:
        res = await asyncio.to_thread(
            oc.close_analysis, [l.model_dump() for l in req.legs], F=chain["F"], T=chain["T"], iv=chain["atm_iv"],
            marks=marks, ref_move=req.move_pts, hold_hours=req.hold_hours, closes=closes)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if modeled:
        res["warnings"].append(f"有 {modeled} 腳在鏈上找不到報價（履約價沒掛單或不在這個到期月），目前價格改用模型估")
    if meta["source"] == "manual":
        res["warnings"].insert(0, "手動模式：目前價格是用 Black-76 與單一 IV 估的（或你輸入的買賣價），只能當示意")
    return {**res, **meta, "as_of": time.time()}


@router.post("/compare")
async def compare(req: CompareRequest) -> dict[str, Any]:
    """買跨式 vs 買勒式：各標的點位（到期時、持有 N 天後）的損益表與曲線。只試算、不下單。"""
    chain, meta = await _get_chain(req.category, req.delivery_month, req.manual)
    try:
        res = await asyncio.to_thread(oc.compare_structures, chain, select=req.select, delta=req.delta, k=req.k, ref_move=req.move_pts,
                                      fill=req.fill, lots=req.lots, hold_days=req.hold_days)
    except ValueError as e:
        raise HTTPException(422, f"{e}" + ("（若是休市或沒有報價，可以改用手動模式）" if meta["source"] == "live" else ""))
    if meta["source"] == "manual":
        res["warnings"].insert(0, "手動模式：價格是用 Black-76 與單一 IV 估的（沒有偏斜、價差是模擬的），只能當示意")
    return {**res, **meta, "as_of": time.time()}
