"""波動放大提醒（update.md 發現 37、39）：只通知、不動任何委託，也不給方向。

為什麼：停損是用「k 倍雜訊範圍」定的（core/stop_suggest.py：σ＝最近 30 個 1 分鐘收盤價變動的樣本標準差、雜訊範圍＝σ√T）。
σ 變成 2 倍，同一個停損點數就只剩一半的 k——被雜訊掃到的機率從 5.4%（2σ）變 27.5%（1σ）（core/stop_calibration.json，日盤 T=30）。
持倉中：告訴你「停損的意義變了」；空手：告訴你「現在進場，停損要照現在的雜訊範圍放」（發現 39：空手也發）。
提醒之後怎麼做比較好沒有驗證過，所以訊息只列事實，決定留給你。

判斷（每分鐘一次；有持倉沒持倉都做；只讀本機資料庫與記憶體，不查券商）：
  現在的 σ ÷「同一個時段（距該時段開始的第幾分鐘）最近 20 個時段 σ 的中位數」≥ VOL_ALERT_RATIO（預設 2.0）。
  用同時段比、不用全天平均：開盤本來就比較大，不這樣比每天開盤都會響。基準只用「當天以前」的時段（tests 有金絲雀）。
  日盤的基準來自 TXF 1 分 K（bars_1m，1,600 多個時段），夜盤來自 TMF 真實成交的 flow_1m（累積中，同一分鐘不到 MIN_SESSIONS 個時段有值就不判斷）。
  時段開始後 30 分鐘內算不出 σ，不判斷。σ 的定義與建議停損停利卡完全相同（直接呼叫 stop_suggest.sigma_from_closes）。
頻率（TXF 日盤 1,614 天的描述統計，不是檢定；60 分鐘冷卻；與有沒有持倉無關）：2× 約 23% 的日子至少一次、平均每個日盤 0.39 次；1.5× 是 1.17 次、3× 是 0.08 次。夜盤還沒量過。
持倉：只有期貨部位（TMF／MXF／TXF）會列「離停損還有幾點」；選擇權與沒有持倉的訊息只講現在的尺度。"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import statistics
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from core import stop_suggest
from core.daytrade import TAIPEI
from core.notify import notify

logger = logging.getLogger(__name__)

POLL_SEC = 60.0
N_SESSIONS = 20                 # 基準：最近幾個時段
MIN_SESSIONS = 10               # 同一分鐘至少要有幾個時段有 σ，才拿來當基準
COOLDOWN_SEC = 3600.0
HORIZON = 30                    # 把停損距離換算成「雜訊範圍」用的持有時間（分鐘）；要在 stop_suggest.HORIZONS 裡
BASELINE_TTL = 6 * 3600.0
FUT_PREFIX = ("TMF", "MXF", "TXF")
DAY_START, DAY_MIN = 8 * 60 + 45, 300          # 日盤 08:45 起 300 分鐘
NIGHT_START, NIGHT_MIN = 15 * 60, 14 * 60      # 夜盤 15:00 起 840 分鐘（到隔天 05:00）
WINDOW = stop_suggest.WINDOW                   # 30


def enabled() -> bool:
    return os.getenv("VOL_WATCH", "true").lower() != "false"


def ratio_threshold() -> float:
    try:
        v = float(os.getenv("VOL_ALERT_RATIO", "2.0"))
    except ValueError:
        return 2.0
    return v if v > 1.0 else 2.0


# ── 時段 ────────────────────────────────────────────────────────

def session_pos(t: datetime) -> Optional[tuple[str, str, int]]:
    """台北時間 t → (時段 day／night, 時段代號＝開始那天的日期, 距時段開始的分鐘數)；不在連續交易時段（05:00~08:45、13:45~15:00、週末）回 None。
    夜盤跨午夜：隔天凌晨（週二～週六）算前一天的夜盤。假日不處理（那天沒有資料就沒有 σ，不會響）。"""
    t = t.astimezone(TAIPEI)
    m, wd = t.hour * 60 + t.minute, t.weekday()
    if wd < 5 and DAY_START <= m < DAY_START + DAY_MIN:
        return "day", t.date().isoformat(), m - DAY_START
    if wd < 5 and m >= NIGHT_START:
        return "night", t.date().isoformat(), m - NIGHT_START
    if 1 <= wd <= 5 and m < 5 * 60:
        return "night", (t - timedelta(days=1)).date().isoformat(), 24 * 60 - NIGHT_START + m
    return None


# ── 基準：同時段、過去 N 個時段的 σ 中位數 ─────────────────────────

def sigma_at(closes: dict[int, float], idx: int) -> Optional[float]:
    """時段內第 idx 分鐘「收完」時的 σ：idx−30..idx 共 31 個收盤（缺的分鐘＝None）。與建議停損停利卡同一個函式。"""
    s, _ = stop_suggest.sigma_from_closes([closes.get(i) for i in range(idx - WINDOW, idx + 1)])
    return s


def build_baseline(sessions: dict[str, dict[int, float]], current_key: str, n: int = N_SESSIONS, min_sessions: int = MIN_SESSIONS) -> dict[int, float]:
    """sessions＝{時段代號: {分鐘序號: 收盤價}}。只用代號小於 current_key 的最近 n 個時段（今天自己與之後的不算）。
    回傳 {分鐘序號: σ 的中位數}，同一分鐘有值的時段少於 min_sessions 個的分鐘不給。"""
    keys = sorted(k for k in sessions if k < current_key)[-n:]
    per: dict[int, list[float]] = {}
    for k in keys:
        closes = sessions[k]
        if not closes:
            continue
        for idx in range(WINDOW, max(closes) + 1):
            s = sigma_at(closes, idx)
            if s is not None:
                per.setdefault(idx, []).append(s)
    return {i: statistics.median(v) for i, v in per.items() if len(v) >= min_sessions}


def day_sessions(rows: list[dict[str, Any]]) -> dict[str, dict[int, float]]:
    """market_store.bars_1m（date、hhmm、close）→ 日盤各時段的 {分鐘序號: 收盤}。
    bars_1m 的 hhmm 是 K 棒「結束」時間（08:46～13:45，共 300 根）；flow_1m 與這裡的序號用「分鐘起點」，所以 08:46 結束的那根是第 0 分鐘。"""
    out: dict[str, dict[int, float]] = {}
    for r in rows:
        try:
            hhmm = int(r["hhmm"])
            idx = (hhmm // 100) * 60 + hhmm % 100 - DAY_START - 1
            c = r.get("close")
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= idx < DAY_MIN and c:
            out.setdefault(str(r["date"]), {})[idx] = float(c)
    return out


def night_sessions(rows: list[dict[str, Any]]) -> dict[str, dict[int, float]]:
    """market_store.flow_1m（ts＝分鐘起點、close）→ 夜盤各時段的 {分鐘序號: 收盤}。"""
    out: dict[str, dict[int, float]] = {}
    for r in rows:
        try:
            pos = session_pos(datetime.fromtimestamp(int(r["ts"]), TAIPEI))
            c = r.get("close")
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        if pos and pos[0] == "night" and c:
            out.setdefault(pos[1], {})[pos[2]] = float(c)
    return out


def typical_sigma(baseline: dict[int, float]) -> Optional[float]:
    """整個時段的典型 σ（基準各分鐘的中位數）；盤前摘要用。"""
    return statistics.median(baseline.values()) if baseline else None


# ── 掃到機率（實測表）與停損的暴露程度 ──────────────────────────────

def touch_prob(table: Optional[dict[str, Any]], session: str, horizon: int, k: float) -> Optional[tuple[float, str]]:
    """k（停損距離 ÷ 雜訊範圍）→ T 分鐘內被雜訊掃到的機率（core/stop_calibration.json 的實測表，格點之間線性內插）。
    回傳 (機率, "≈"／"≤")；k 超出最大格點（3σ）只能說「不超過」；表不在回 None。"""
    try:
        ks = [float(x) for x in table["ks"]]
        touch = [float(x) for x in table[session]["table"][str(horizon)]["touch"]]
    except (TypeError, KeyError, ValueError):
        return None
    if not ks or len(ks) != len(touch) or k != k:
        return None
    if k <= 0:
        return 1.0, "≈"
    if k < ks[0]:
        return 1.0 + (touch[0] - 1.0) * k / ks[0], "≈"
    for (k0, p0), (k1, p1) in zip(zip(ks, touch), zip(ks[1:], touch[1:])):
        if k <= k1:
            return p0 + (p1 - p0) * (k - k0) / (k1 - k0), "≈"
    return touch[-1], "≤"


def held_futures(positions: Optional[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    out = []
    for p in positions or []:
        code, qty = str(p.get("code", "")), int(p.get("quantity", 0) or 0)
        if qty > 0 and code.startswith(FUT_PREFIX):
            out.append(p)
    return out


def _match_watch(p: dict[str, Any], watches: Optional[list[dict[str, Any]]]) -> Optional[dict[str, Any]]:
    """這個期貨部位的停損監看：非選擇權、已看到部位、方向相同、合約前綴對得上、有設停損。"""
    for w in watches or []:
        mc = str(w.get("match_code") or "")
        if (not w.get("is_option") and w.get("seen") and mc and str(p.get("code", "")).startswith(mc)
                and w.get("direction") == p.get("direction") and float(w.get("stop_loss_pts") or 0) > 0):
            return w
    return None


def stop_exposure(held: list[dict[str, Any]], watches: Optional[list[dict[str, Any]]], sigma: float, ref: float, horizon: int, session: str,
                  table: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """每個期貨部位：離停損還有幾點、是現在雜訊範圍（σ√T）的幾倍、平常（基準 σ）是幾倍，以及對應的被掃到機率。沒有停損監控的標 stop=None。"""
    rt, out = math.sqrt(horizon), []
    for p in held:
        d = 1 if p.get("direction") == "Buy" else -1
        item: dict[str, Any] = {"code": p.get("code"), "direction": p.get("direction"), "quantity": int(p.get("quantity", 0) or 0), "stop": None}
        w = _match_watch(p, watches)
        last = float(p.get("last_price") or p.get("price") or 0)
        if w is not None:
            sl = float(w["stop_loss_pts"])
            item.update(stop=sl, dist=None)
            if last > 0:
                dist = sl + (last - float(w["entry_price"])) * d      # 離停損價還有幾點（已經賺的會拉遠、虧的會拉近）
                item["dist"] = dist
            else:
                dist = 0.0
            if last > 0 and dist > 0:
                k_now, k_ref = dist / (sigma * rt), dist / (ref * rt)
                item.update(k_now=k_now, k_ref=k_ref, p_now=touch_prob(table, session, horizon, k_now), p_ref=touch_prob(table, session, horizon, k_ref))
        out.append(item)
    return out


# ── 判斷與訊息 ──────────────────────────────────────────────────

def assess(now_ts: float, rows: list[dict[str, Any]], baseline: dict[int, float], session: str, idx: int, positions: Optional[list[dict[str, Any]]],
           watches: Optional[list[dict[str, Any]]], table: Optional[dict[str, Any]], horizon: int = HORIZON, ratio_th: Optional[float] = None) -> Optional[dict[str, Any]]:
    """回 {alert, ratio, sigma, ref, stops, held, suggest, ...}（算不出 σ 或沒有基準時 alert=False、ratio=None）。held 可能是空的（空手也提醒）。"""
    held = held_futures(positions)
    th = ratio_threshold() if ratio_th is None else ratio_th
    out: dict[str, Any] = {"alert": False, "ratio": None, "sigma": None, "ref": None, "session": session, "idx": idx, "horizon": horizon,
                           "held": held, "stops": [], "suggest": None, "threshold": th}
    if idx < 1:
        return out
    sigma, _ = stop_suggest.sigma_from_closes(stop_suggest.closes_from_flow(rows, now_ts))
    ref = baseline.get(idx - 1)                    # 最近一個「收完」的分鐘（closes_from_flow 也不含還沒收完的這一分鐘）
    out.update(sigma=sigma, ref=ref)
    if sigma is None or sigma <= 0 or not ref or ref <= 0:
        return out
    out["ratio"] = sigma / ref
    out["alert"] = out["ratio"] >= th
    if out["alert"]:
        out["stops"] = stop_exposure(held, watches, sigma, ref, horizon, session, table)
        out["suggest"] = {"now": _balanced(sigma, session, horizon), "usual": _balanced(ref, session, horizon)}
    return out


def _balanced(sigma: float, session: str, horizon: int) -> Optional[tuple[int, int]]:
    """這個 σ 下「平衡」那組建議的 (停利, 停損) 點數（與建議停損停利卡同一個函式；點數不依賴實測表，所以不傳表）。"""
    row = next((r for r in stop_suggest.build(sigma, 30, session=session, horizon=horizon, contract="TMF", table=None).get("rows", []) if r["name"] == "平衡"), None)
    return (row["tp"], row["sl"]) if row else None


def _pct(x: Optional[tuple[float, str]]) -> str:
    if x is None:
        return "—"
    p, kind = x
    return f"約 {p * 100:.0f}%" if kind == "≈" else f"不超過 {p * 100:.1f}%"


def message(a: dict[str, Any]) -> tuple[str, str]:
    T, rt = a["horizon"], math.sqrt(a["horizon"])
    lines = [f"最近 30 分鐘 σ ≈ {a['sigma']:.1f} 點／分鐘（同時段近 {N_SESSIONS} 個時段中位數 {a['ref']:.1f}），{T} 分鐘的雜訊範圍約 {a['sigma'] * rt:.0f} 點。"]
    for s in a["stops"]:
        who = f"{s['code']} {'多' if s['direction'] == 'Buy' else '空'} {s['quantity']} 口"
        if s["stop"] is None:
            lines.append(f"{who}：沒有停損監控。")
        elif s.get("dist") is None:
            lines.append(f"{who}：停損 {s['stop']:.0f} 點（目前取不到最新價，算不出離停損多遠）。")
        elif s["dist"] <= 0:
            lines.append(f"{who}：停損 {s['stop']:.0f} 點，目前價位已經在停損價之外（監控會處理）。")
        else:
            lines.append(f"{who}：停損 {s['stop']:.0f} 點，離停損還有 {s['dist']:.0f} 點＝現在雜訊範圍的 {s['k_now']:.1f} 倍，"
                         f"{T} 分鐘內被雜訊掃到的機率{_pct(s['p_now'])}（平常這個距離是 {s['k_ref']:.1f} 倍、{_pct(s['p_ref'])}）。")
    if not a["stops"]:
        lines.append("目前沒有期貨持倉。")
    sg = a.get("suggest") or {}
    if sg.get("now") and sg.get("usual"):
        lines.append(f"以現在的波動，平衡型停利 {sg['now'][0]}／停損 {sg['now'][1]} 點（平常是 {sg['usual'][0]}／{sg['usual'][1]} 點）。")
    if a.get("held"):
        lines.append("只是提醒停損的意義變了：拉遠停損、減碼或不動由你決定；這則通知不會動任何委託。")
    else:
        lines.append("如果現在要進場，停損距離要照現在的雜訊範圍放，別沿用平常的點數；這則通知不會動任何委託。")
    return f"⚠️ 波動放大：現在是同時段的 {a['ratio']:.1f} 倍", "\n".join(lines)


# ── 背景迴圈 ────────────────────────────────────────────────────

class VolWatcher:
    """每分鐘看一次：在連續交易時段內才讀資料庫、建基準（基準一個時段只建一次，6 小時後重建）；有沒有持倉都做。所有例外吞掉，不影響任何交易路徑。"""

    def __init__(self) -> None:
        self._base: Optional[tuple[str, str, float, dict[int, float]]] = None      # (時段, 時段代號, 建立時間, 基準)
        self.last: Optional[dict[str, Any]] = None

    async def _baseline(self, session: str, key: str, now_ts: float, store: Any) -> dict[int, float]:
        b = self._base
        if b and b[0] == session and b[1] == key and now_ts - b[2] < BASELINE_TTL:
            return b[3]
        if session == "day":
            since = (datetime.fromtimestamp(now_ts, TAIPEI) - timedelta(days=45)).date().isoformat()
            rows = await asyncio.to_thread(store.bars_1m, "TXF", since)
            base = await asyncio.to_thread(lambda: build_baseline(day_sessions(rows), key))
        else:
            rows = await asyncio.to_thread(store.flow_1m, "TMF", now_ts - 40 * 86400)
            base = await asyncio.to_thread(lambda: build_baseline(night_sessions(rows), key))
        self._base = (session, key, now_ts, base)
        logger.info("波動基準已建立：%s %s，%d 個分鐘有值", session, key, len(base))
        return base

    async def check(self, now_ts: float, positions: Optional[list[dict[str, Any]]], watches: Optional[list[dict[str, Any]]], store: Any,
                    table: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        try:
            pos = session_pos(datetime.fromtimestamp(now_ts, TAIPEI))
            if pos is None:
                return None
            session, key, idx = pos
            baseline = await self._baseline(session, key, now_ts, store)
            rows = await asyncio.to_thread(store.flow_1m, "TMF", now_ts - 3600)
            a = assess(now_ts, rows, baseline, session, idx, positions, watches, table if table is not None else stop_suggest.load_table())
            self.last = a
            if a and a["alert"]:
                title, body = message(a)
                notify(title, body, key=f"volwatch:{session}", cooldown=COOLDOWN_SEC, priority="high")
            return a
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("波動放大提醒檢查失敗（已忽略）")
            return None


vol_watcher = VolWatcher()


async def vol_watch_loop(get_positions: Callable[[], list[dict[str, Any]]], get_watches: Callable[[], list[dict[str, Any]]], store: Any) -> None:
    """positions／watches 由呼叫端提供（讀快取與記憶體，不查券商）。VOL_WATCH=false 關閉。"""
    if not enabled():
        logger.info("波動放大提醒已關閉（VOL_WATCH=false）")
        return
    while True:
        await asyncio.sleep(POLL_SEC)
        try:
            await vol_watcher.check(time.time(), get_positions(), get_watches(), store)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("波動放大提醒迴圈例外（已忽略）")
