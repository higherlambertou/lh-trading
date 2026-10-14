"""依波動的建議停損停利（update.md 發現 33）：純函式、只顯示、不碰下單與策略。

做什麼
  1. 用最近 30 分鐘 1 分鐘收盤價變動的標準差 σ（點／分鐘；定義與 core/stop_calibration.py 完全相同）
     與你預計的持有時間 T，算出「雜訊範圍」σ·√T。
  2. 建議三組（停利 k_tp·σ√T／停損 k_sl·σ√T）：
       高勝率型 0.5／2.0、平衡 1.0／1.5、高賺賠比型 1.5／1.0。三組的 TP＋SL 都是 2.5σ√T，所以「要靠時機多贏幾個百分點才打平成本」一樣——
       改比例只是把勝率換成賺賠比，期望值不變（進場沒有資訊時每筆期望值＝−成本，不管停損停利怎麼放）。
  3. 每組附：停損被雜訊掃到的機率（實測表，core/stop_calibration.json）、沒有技巧時持有到碰到一邊為止的勝率（＝停損÷(停利＋停損)）、
     T 分鐘內停利先到／停損先到／都沒到（實測，沒有技巧時）、要比基準多贏幾個百分點才打平成本（成本÷(停利＋停損)）、單口停損虧多少（元、占權益 %）與單筆風險限額下最多幾口。
這不是進場訊號，也不會讓一個沒有優勢的進場變賺錢；它只決定停損離雜訊多遠、倉位多大。"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Optional, Sequence

CAL_PATH = Path(__file__).resolve().parent / "stop_calibration.json"
WINDOW, MIN_VALID = 30, 25                       # 與 core/stop_calibration.py 相同的 σ 定義
HORIZONS = (5, 15, 30, 60)
POINT_VALUE = {"TMF": 10.0, "MXF": 50.0, "TXF": 200.0}
# (名稱, k_tp, k_sl)：TP＋SL 都是 2.5σ√T
PRESETS = (("高勝率型", 0.5, 2.0), ("平衡", 1.0, 1.5), ("高賺賠比型", 1.5, 1.0))

_cache: dict[str, Any] = {"mtime": None, "table": None}


def cost_pts() -> float:
    """每筆來回成本（點）：手續費＋稅＋滑價。預設 4 點是假設（回測也是用這個），請改成自己的數字（SUGGEST_COST_PTS）。"""
    try:
        v = float(os.getenv("SUGGEST_COST_PTS", "4"))
    except ValueError:
        return 4.0
    return v if v >= 0 else 4.0


def risk_pct() -> float:
    """單筆風險占權益的上限（%），預設 1（SUGGEST_RISK_PCT）。"""
    try:
        v = float(os.getenv("SUGGEST_RISK_PCT", "1"))
    except ValueError:
        return 1.0
    return v if 0 < v <= 100 else 1.0


def load_table(path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """讀校準表（core/stop_calibration.json）；檔案換了會重讀；沒有或壞掉回 None（UI 會寫「未校準」，不會亂給數字）。"""
    path = path or CAL_PATH
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _cache["mtime"] == (str(path), mtime) and _cache["table"] is not None:
        return _cache["table"]
    try:
        t = json.loads(path.read_text(encoding="utf-8"))
        if not (isinstance(t, dict) and "day" in t and "night" in t and t.get("version") == 1):
            return None
    except (OSError, ValueError):
        return None
    _cache["mtime"], _cache["table"] = (str(path), mtime), t
    return t


def sigma_from_closes(closes: Sequence[Optional[float]]) -> tuple[Optional[float], int]:
    """closes：最近 WINDOW+1 個 1 分鐘收盤價（沒成交的分鐘是 None／NaN），由舊到新。回傳 (σ, 有效變動數)；有效少於 MIN_VALID 回 (None, n)。"""
    xs = [float("nan") if c is None else float(c) for c in closes][-(WINDOW + 1):]
    ch = [b - a for a, b in zip(xs, xs[1:]) if math.isfinite(a) and math.isfinite(b)]
    if len(ch) < MIN_VALID:
        return None, len(ch)
    m = sum(ch) / len(ch)
    var = sum((x - m) ** 2 for x in ch) / (len(ch) - 1)
    return math.sqrt(var), len(ch)


def closes_from_flow(rows: Sequence[dict[str, Any]], now_ts: float) -> list[Optional[float]]:
    """flow_1m 的列（ts＝分鐘起點）→ 最近 WINDOW+1 個「已收完的分鐘」的收盤價（缺的分鐘＝None）。目前這一分鐘還沒收完，不算。"""
    last_done = int(now_ts // 60) * 60 - 60                       # 最近一個收完的分鐘的起點
    by_ts = {int(r["ts"]) // 60 * 60: r.get("close") for r in rows}
    return [by_ts.get(last_done - 60 * i) for i in range(WINDOW, -1, -1)]


def session_of(hour: int, minute: int, weekday: int) -> str:
    """08:45~13:45（週一～五）＝日盤，其餘＝夜盤（與其他地方相同的簡化；盤前試算時段與休市日不特別處理）。"""
    m = hour * 60 + minute
    return "day" if weekday < 5 and 8 * 60 + 45 <= m < 13 * 60 + 45 else "night"


def build(sigma: Optional[float], n_changes: int, *, session: str, horizon: int, contract: str = "TMF", equity: Optional[float] = None,
          table: Optional[dict[str, Any]] = None, today: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """組出整張建議卡的內容。sigma 是 None（資料不夠）時只回說明，不給數字。"""
    pv = POINT_VALUE.get(contract.upper(), 10.0)
    cost, rp = cost_pts(), risk_pct()
    out: dict[str, Any] = {"contract": contract.upper(), "session": session, "horizon": horizon, "sigma": None if sigma is None else round(sigma, 2),
                           "n_changes": n_changes, "cost_pts": cost, "risk_pct": rp, "point_value": pv, "equity": equity, "rows": [],
                           "calibrated": table is not None, "today": today}
    if sigma is None or sigma <= 0:
        out["note"] = f"最近 30 分鐘成交太少（有效的 1 分鐘變動只有 {n_changes} 個，要 {MIN_VALID} 個以上），算不出波動，不給建議。"
        return out
    unit = sigma * math.sqrt(horizon)
    out["noise"] = round(unit, 1)
    cal = (table or {}).get(session) or {}
    th = (cal.get("table") or {}).get(str(horizon)) if table else None
    ks, ks_tp, ks_sl = (table or {}).get("ks", []), (table or {}).get("ks_tp", []), (table or {}).get("ks_sl", [])
    out["cal"] = None if th is None else {"sessions": cal.get("sessions"), "n": th["n"], "few": bool(th["few"] or cal.get("few")), "from": cal.get("from"), "to": cal.get("to")}
    exp_hi = (today or {}).get("expect_hi")
    for name, ktp, ksl in PRESETS:
        tp, sl = max(1, round(ktp * unit)), max(1, round(ksl * unit))
        row: dict[str, Any] = {"name": name, "k_tp": ktp, "k_sl": ksl, "tp": tp, "sl": sl, "ratio": round(tp / sl, 2),
                               "p0": round(sl / (tp + sl), 4),                                   # 沒有技巧、持有到碰到一邊為止的勝率
                               "need": round(cost / (tp + sl), 4),                               # 要比基準多贏（機率差）才打平成本
                               "loss_twd": round(sl * pv), "loss_pct": None if not equity else round(sl * pv / equity * 100, 2),
                               "max_lots": None if not equity else int(equity * rp / 100 // (sl * pv)),
                               "far_tp": bool(exp_hi is not None and tp > exp_hi)}
        if th is not None and ktp in ks_tp and ksl in ks_sl and ksl in ks:
            i, j, k = ks_tp.index(ktp), ks_sl.index(ksl), ks.index(ksl)
            win, unres = th["win"][i][j], th["unres"][i][j]
            row["touch_sl"] = th["touch"][k]                                                      # 停損被雜訊掃到的機率（實測）
            row["touch_unstable"] = bool(th["unstable_touch"][k])
            if win is not None:
                row["within"] = {"tp": round(win * (1 - unres), 4), "sl": round((1 - win) * (1 - unres), 4), "none": round(unres, 4)}   # T 分鐘內，沒有技巧時
            row["win_unstable"] = bool(th["unstable_win"][i][j])
        out["rows"].append(row)
    return out


def today_from_ctx(ctx: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """/api/market/live 的 ctx（盤中時段脈絡）→ 今天的尺度：已走的振幅與預期之後還會走多少（68% 區間）。09:15 之前、重啟在開盤後等情況沒有就是 None。"""
    proj = (ctx or {}).get("proj")
    if not proj:
        return None
    e = proj.get("expect") or {}
    return {"r20": ctx.get("r20"), "early": proj.get("early"), "done": proj.get("done"),
            "expect_lo": e.get("lo"), "expect_mid": e.get("mid"), "expect_hi": e.get("hi")}


def suggest_now(rows: Sequence[dict[str, Any]], now_ts: float, *, contract: str, horizon: int, equity: Optional[float],
                table: Optional[dict[str, Any]], ctx: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """rows＝flow_1m（TMF 真實成交的 1 分 K）最近一小時；全部是純計算，不碰券商。"""
    import time as _t
    lt = _t.localtime(now_ts)
    session = session_of(lt.tm_hour, lt.tm_min, lt.tm_wday)
    sigma, n = sigma_from_closes(closes_from_flow(rows, now_ts))
    out = build(sigma, n, session=session, horizon=horizon, contract=contract, equity=equity, table=table,
                today=today_from_ctx(ctx) if session == "day" else None)
    out["as_of"] = now_ts
    return out
