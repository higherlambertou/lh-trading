"""口數試算（update.md 發現 51）：依停損距離與單筆風險上限算「最多幾口」。純函式、**只顯示、不碰下單**——不擋、不改任何委託，口數仍是你自己填。

公式（來自《部位大小控管》需求文件的口數計算，這裡是顯示版，不是那份文件的獨立風控層）
    每口風險 ＝ (停損點數 ＋ 緩衝點數) × 每點價值
    口數上限 ＝ min( floor(權益 × 單筆風險% ÷ 每口風險),
                    floor(max(0, 權益 × 總風險上限% − 已佔用風險) ÷ 每口風險),
                    單筆口數上限（有設才算） )
哪一個上限生效、算出不足 1 口時要多少權益或多近的停損才夠 1 口，都一併回傳。

fail-closed：權益取不到、已過期（超過 5 分鐘沒刷新）、停損沒填或 ≤ 0、合約不認得 → 不給口數（lots＝None＋原因），不是用預設值湊一個。
算出 < 1 口＝0 口（不補到 1 口）。

「已佔用風險」＝手動停損監控裡有停損的期貨部位，到停損的距離×口數×每點價值（同樣加緩衝）。**沒有登記停損的部位（策略單、沒設停損的手動單）風險未知**，
只列出來、不計入；選擇權買方的最大虧損是權利金，也不計入。所以總風險上限在有未登記部位時只是下限估計——畫面會標出來。
⚠ 風險用「到停損的距離」估算，假設停損會成交；跳空與極端行情下成交價可能更差（緩衝只是一個小墊，不是保證）。
環境變數：SUGGEST_RISK_PCT（單筆風險%，與建議停損停利共用，預設 1）、SIZE_SLIP_PTS（停損滑價緩衝，預設 2 點）、SIZE_TOTAL_RISK_PCT（總風險上限%，預設 3）、SIZE_LOT_CAP（單筆口數上限，預設不設）。"""
from __future__ import annotations

import math
import os
from typing import Any, Optional, Sequence

from core import stop_suggest

POINT_VALUE = stop_suggest.POINT_VALUE
MAX_EQUITY_AGE = 300.0           # 權益資料超過這麼多秒沒刷新就不給口數


def _env_float(name: str, default: float, lo: float, hi: float, inclusive_lo: bool = True) -> float:
    try:
        v = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    ok = (lo <= v if inclusive_lo else lo < v) and v <= hi
    return v if ok and math.isfinite(v) else default


def slip_pts() -> float:
    return _env_float("SIZE_SLIP_PTS", 2.0, 0.0, 1000.0)


def total_risk_pct() -> float:
    return _env_float("SIZE_TOTAL_RISK_PCT", 3.0, 0.0, 100.0, inclusive_lo=False)


def lot_cap() -> Optional[int]:
    raw = os.getenv("SIZE_LOT_CAP", "").strip()
    if not raw:
        return None
    try:
        v = int(raw)
    except ValueError:
        return None
    return v if v >= 1 else None


def config() -> dict[str, Any]:
    return {"risk_pct": stop_suggest.risk_pct(), "slip_pts": slip_pts(), "total_risk_pct": total_risk_pct(), "lot_cap": lot_cap()}


def family(code: Any) -> Optional[str]:
    """合約代碼或名稱 → TMF／MXF／TXF；不是這三種期貨（選擇權、其他）None。"""
    c = str(code or "").upper()
    for f in POINT_VALUE:
        if c.startswith(f):
            return f
    return None


def _valid(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# ── 已佔用風險 ───────────────────────────────────────────────────

def open_risk(positions: Sequence[dict[str, Any]], watches: Sequence[dict[str, Any]], slip: float) -> dict[str, Any]:
    """期貨部位到停損的風險（元）。同合約同方向：登記了停損的監看先分配口數（口數以 remaining 為準），超出的部位列為未登記。
    選擇權部位不計入（列出）。回傳 {"twd", "covered_lots", "uncovered": [{code, direction, quantity}], "options": [...]}。"""
    cover: dict[tuple[str, str], list[tuple[float, int]]] = {}          # (合約, 方向) → [(停損點數, 口數)]
    for w in watches or []:
        f = None if w.get("is_option") else family(w.get("contract") or w.get("match_code"))
        sl = w.get("stop_loss_pts")
        if f is None or not w.get("seen") or not _valid(sl) or sl <= 0:
            continue
        qty = int(w.get("remaining") or w.get("quantity") or 0)
        if qty > 0:
            cover.setdefault((f, str(w.get("direction"))), []).append((float(sl), qty))
    twd, covered, uncovered, options = 0.0, 0, [], []
    for p in positions or []:
        qty = int(p.get("quantity", 0) or 0)
        code = str(p.get("code", ""))
        if qty <= 0 or not code:
            continue
        f = family(code)
        if f is None:
            options.append({"code": code, "direction": p.get("direction"), "quantity": qty})
            continue
        left = qty
        slots = cover.get((f, str(p.get("direction"))), [])
        while left > 0 and slots:
            sl, q = slots[0]
            take = min(left, q)
            twd += (sl + slip) * POINT_VALUE[f] * take
            covered += take
            left -= take
            if take == q:
                slots.pop(0)
            else:
                slots[0] = (sl, q - take)
        if left > 0:
            uncovered.append({"code": code, "direction": p.get("direction"), "quantity": left})
    return {"twd": round(twd, 2), "covered_lots": covered, "uncovered": uncovered, "options": options}


# ── 口數 ─────────────────────────────────────────────────────────

def compute(equity: Any, stop_pts: Any, contract: str, *, risk_pct: float, slip: float, total_pct: float, open_twd: float = 0.0,
            cap: Optional[int] = None, qty: Optional[int] = None, equity_age: Optional[float] = None) -> dict[str, Any]:
    """口數上限與計算過程。lots＝None 表示『不給口數』（reason 是原因）；lots＝0 表示算出不足 1 口。"""
    c = str(contract or "").upper()
    pv = POINT_VALUE.get(c)
    out: dict[str, Any] = {"contract": c, "lots": None, "reason": None, "equity": None, "stop_pts": None, "slip_pts": slip, "risk_pct": risk_pct, "total_risk_pct": total_pct,
                           "point_value": pv, "open_risk_twd": round(float(open_twd), 2), "caps": [], "binding": None, "qty": qty}
    if pv is None:
        out["reason"] = "contract_unknown"
        return out
    if not _valid(equity) or equity <= 0:
        out["reason"] = "equity_missing"
        return out
    out["equity"] = float(equity)
    if equity_age is not None and equity_age > MAX_EQUITY_AGE:
        out["reason"] = "equity_stale"
        return out
    if not _valid(stop_pts) or stop_pts <= 0:
        out["reason"] = "stop_missing"
        return out
    eff = float(stop_pts) + slip
    per_lot = eff * pv
    budget = equity * risk_pct / 100.0
    remaining = max(0.0, equity * total_pct / 100.0 - open_twd)
    caps = [{"name": "single", "lots": int(budget // per_lot), "budget_twd": round(budget, 2)},
            {"name": "total", "lots": int(remaining // per_lot), "budget_twd": round(remaining, 2)}]
    if cap is not None:
        caps.append({"name": "cap", "lots": int(cap), "budget_twd": None})
    best = min(caps, key=lambda x: x["lots"])                              # 同值時取排在前面的（單筆風險優先）
    out.update(stop_pts=float(stop_pts), eff_stop=eff, risk_per_lot=round(per_lot, 2), risk_per_lot_pct=round(per_lot / equity * 100, 4), caps=caps,
               lots=best["lots"], binding=best["name"])
    if best["lots"] < 1:                                                    # 不補到 1 口；告訴你要什麼才夠
        out["reason"] = "below_one"
        out["need_equity_single"] = round(per_lot / (risk_pct / 100.0), 0)
        out["max_stop_for_one"] = max(0, int(min(budget, remaining) // pv - slip))
    if qty is not None and qty >= 1:
        out["qty_risk_twd"] = round(qty * per_lot, 2)
        out["qty_risk_pct"] = round(qty * per_lot / equity * 100, 4)
        out["qty_over"] = qty > best["lots"]
        out["qty_over_by"] = [c_["name"] for c_ in caps if qty > c_["lots"]]
    return out


def size_now(positions: Sequence[dict[str, Any]], watches: Sequence[dict[str, Any]], margin: Optional[dict[str, Any]], margin_at: float, now: float,
             contract: str, stop_pts: Any, qty: Optional[int] = None) -> dict[str, Any]:
    """API 用的組合：從部位快取、手動監看、保證金快取（權益）算出口數與已佔用風險。全部讀記憶體。"""
    cfg = config()
    risk = open_risk(positions, watches, cfg["slip_pts"])
    equity = (margin or {}).get("equity")
    age = (now - margin_at) if margin_at else None
    out = compute(equity, stop_pts, contract, risk_pct=cfg["risk_pct"], slip=cfg["slip_pts"], total_pct=cfg["total_risk_pct"], open_twd=risk["twd"],
                  cap=cfg["lot_cap"], qty=qty, equity_age=age)
    out["open_risk"] = {**risk, "pct": None if not out["equity"] else round(risk["twd"] / out["equity"] * 100, 4)}
    out["lot_cap"] = cfg["lot_cap"]
    out["as_of"] = now
    return out
