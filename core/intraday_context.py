"""盤中時段脈絡（只顯示、不影響下單）：現在是哪個時段、這個時段典型有多跳、開盤後的振幅預告之後大概還會走多少。

依據 update.md 發現 28（TXF 日盤 1 分 K，2020-03 起約 1,600 天；5 個檢定事先登記、單尾 α=0.01，只跑一次）：
- 開盤後 30／60 分鐘的振幅，有助於預測「之後」的振幅：樣本外誤差只降約 6%（Diebold-Mariano t=4.4／4.1，通過），
  早盤振幅最大與最小三分位差 1.3×；但只是小幅改善，剩下的誤差仍約 ±40%——所以顯示的是「範圍」，不是目標價。
- 相對成交量沒有額外資訊（t=0.2／−0.4，沒過）→ 這裡不做。
- 結算日尾盤振幅沒有比較大（t=−0.9，沒過），但結算日成交量只有一般日的 0.67×（近月連續合約可能已換月，量測的不是到期合約）→ 不能下結論，也不做。
各時段每分鐘波動的倍數是描述不是預測（開盤 30 分鐘約全日平均的 1.6×，11:00 後約 0.7×）。

設計：
- 迴歸與統計都在背景執行緒、每天算一次（IntradayContext），讀取只回快取，絕不在 event loop 上讀資料庫或做迴歸。
- 參考振幅 R20＝前 20 個時段全日振幅的平均，**只用當天以前**（ref_ranges；偷看未來是這類分析最常見的錯，tests 有金絲雀）。
- 迴歸：log(之後振幅) ~ log R20 + log(早盤振幅)；區間用殘差的 16／84% 分位數（振幅是右偏的，不對稱）。
- 用 TXF 的 1 分 K 估計、套在 TMF 的即時行情上（同一個標的指數）。

CLI：python -m core.intraday_context screen|show
  screen 重現發現 28 的 5 個檢定；show 印今天的時段脈絡（只讀 market_state.db，不連券商）。
"""
from __future__ import annotations

import asyncio
import logging
import math
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import NormalDist
from typing import Any, Sequence

import numpy as np

logger = logging.getLogger(__name__)

OPEN_MIN = 8 * 60 + 45          # 日盤第 0 分鐘（08:45）
SESSION_MIN = 300               # 08:45~13:45
SETTLE_CLOSE_MIN = 285          # 結算日近月合約 13:30 收
BUCKET_MIN = 15
CHECKPOINTS = (30, 60)          # 開盤後幾分鐘為「早盤」
MIN_BARS = 280                  # 少於這麼多根（提早收盤、資料殘缺）的日子不納入
N_REF = 20
PROFILE_DAYS = 250              # 時段倍數取近幾個時段
MIN_FIT_DAYS = 120              # 迴歸至少要幾天
RETRY_SEC = 600                 # 算失敗後隔多久再試
WARM = 250                      # 樣本外：前幾個樣本只做暖機
LAG = 10                        # HAC 的落後期數（波動有長記憶，取大一點）
N_TESTS = 5
Z_ONE_SIDED = NormalDist().inv_cdf(1 - 0.05 / N_TESTS)       # 單尾 α=0.01 → 2.326


# ── 資料 ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Session:
    """一個日盤時段：從 08:45 起每分鐘一格（長度 300），缺的格是 NaN。"""
    date: str
    hi: np.ndarray
    lo: np.ndarray
    op: np.ndarray
    cl: np.ndarray
    vol: np.ndarray

    def bars(self) -> int:
        return int(np.isfinite(self.hi).sum())


def load_sessions(store: Any, code: str = "TXF", before: str | None = None, min_bars: int = MIN_BARS) -> list[Session]:
    """讀 bars_1m。hhmm 是 K 棒「結束」時間，所以 08:46 結束的那根是第 0 格。before 給定時只取日期 < before。"""
    by_day: dict[str, list[dict]] = {}
    for r in store.bars_1m(code):
        if before and r["date"] >= before:
            continue
        by_day.setdefault(r["date"], []).append(r)
    out: list[Session] = []
    for d, rows in sorted(by_day.items()):
        arr = {k: np.full(SESSION_MIN, np.nan) for k in ("hi", "lo", "op", "cl", "vol")}
        for r in rows:
            i = (r["hhmm"] // 100) * 60 + r["hhmm"] % 100 - 1 - OPEN_MIN
            if 0 <= i < SESSION_MIN:
                arr["hi"][i], arr["lo"][i], arr["op"][i], arr["cl"][i] = r["high"], r["low"], r["open"], r["close"]
                arr["vol"][i] = r["volume"] or 0
        s = Session(d, arr["hi"], arr["lo"], arr["op"], arr["cl"], arr["vol"])
        if s.bars() >= min_bars:
            out.append(s)
    return out


def window_range(s: Session, a: int, b: int) -> float:
    h, lo = s.hi[a:b], s.lo[a:b]
    if not np.isfinite(h).any():
        return float("nan")
    return float(np.nanmax(h) - np.nanmin(lo))


def ref_ranges(sessions: Sequence[Session], n: int = N_REF) -> np.ndarray:
    """R20[i]＝前 n 個時段全日振幅的平均（不含第 i 天自己）；前 n 天沒有（NaN）。"""
    rng = np.array([window_range(s, 0, SESSION_MIN) for s in sessions])
    out = np.full(len(sessions), np.nan)
    for i in range(n, len(sessions)):
        out[i] = rng[i - n:i].mean()
    return out


def is_settlement_day(d: date) -> bool:
    """每月第三個週三（TXF／TMF 近月合約的最後交易日，13:30 收盤）。遇假日順延的情形忽略。"""
    return d.weekday() == 2 and 15 <= d.day <= 21


# ── 迴歸與檢定（screen 與正式模型共用）──────────────────────────────

def ols_hac(X: np.ndarray, y: np.ndarray, lag: int = LAG) -> tuple[np.ndarray, np.ndarray]:
    """OLS 係數與 Newey-West 標準誤。"""
    beta = np.linalg.lstsq(X, y, rcond=None)[0]
    u = y - X @ beta
    Xu = X * u[:, None]
    S = Xu.T @ Xu
    for k in range(1, lag + 1):
        G = Xu[k:].T @ Xu[:-k]
        S += (1 - k / (lag + 1)) * (G + G.T)
    inv = np.linalg.inv(X.T @ X)
    return beta, np.sqrt(np.diag(inv @ S @ inv))


def oos_errors(X: np.ndarray, y: np.ndarray, warm: int = WARM) -> np.ndarray:
    """擴張視窗的樣本外誤差：第 i 個樣本只用前 i 個樣本估計。"""
    e = []
    for i in range(warm, len(y)):
        b = np.linalg.lstsq(X[:i], y[:i], rcond=None)[0]
        e.append(y[i] - X[i] @ b)
    return np.array(e)


def dm_t(err_a: np.ndarray, err_b: np.ndarray, lag: int = LAG) -> float:
    """Diebold-Mariano：B 比 A 準 → 正值（平方誤差差的平均 ÷ HAC 標準誤）。"""
    d = err_a ** 2 - err_b ** 2
    n = len(d)
    dm = d.mean()
    var = ((d - dm) ** 2).sum() / n
    for k in range(1, lag + 1):
        var += 2 * (1 - k / (lag + 1)) * ((d[k:] - dm) * (d[:-k] - dm)).sum() / n
    return float(dm / math.sqrt(var / n)) if var > 0 else 0.0          # 兩組誤差完全一樣 → 沒有差別


# ── 模型 ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RangeModel:
    """log(開盤 c 分鐘之後的振幅) ＝ a ＋ b_ref·log R20 ＋ b_early·log(早盤振幅)，加上殘差分位數。"""
    c: int
    a: float
    b_ref: float
    b_early: float
    q16: float
    q50: float
    q84: float
    n: int


def checkpoint_table(sessions: Sequence[Session], c: int) -> dict[str, np.ndarray]:
    """每個時段：R20、早盤振幅 E（前 c 分）、之後振幅 Y（c 分之後）、早盤量 V；只留 R20、E、Y、V 都有效的。"""
    R = ref_ranges(sessions)
    E = np.array([window_range(s, 0, c) for s in sessions])
    Y = np.array([window_range(s, c, SESSION_MIN) for s in sessions])
    V = np.array([np.nansum(s.vol[:c]) for s in sessions])
    ok = np.isfinite(R) & (E > 0) & (Y > 0) & (V > 0)
    idx = np.where(ok)[0]
    RV = np.full(len(sessions), np.nan)
    for i in range(N_REF, len(sessions)):
        prev = np.array([np.nansum(sessions[j].vol[:c]) for j in range(i - N_REF, i)])
        RV[i] = V[i] / prev.mean() if prev.mean() > 0 else np.nan
    keep = idx[np.isfinite(RV[idx])]
    return {"idx": keep, "R": R[keep], "E": E[keep], "Y": Y[keep], "V": V[keep], "RV": RV[keep]}


def fit_range_model(sessions: Sequence[Session], c: int) -> RangeModel | None:
    t = checkpoint_table(sessions, c)
    if len(t["idx"]) < MIN_FIT_DAYS:
        return None
    y = np.log(t["Y"])
    X = np.column_stack([np.ones(len(y)), np.log(t["R"]), np.log(t["E"])])
    beta = np.linalg.lstsq(X, y, rcond=None)[0]
    q16, q50, q84 = np.quantile(y - X @ beta, [0.16, 0.5, 0.84])
    return RangeModel(c, float(beta[0]), float(beta[1]), float(beta[2]), float(q16), float(q50), float(q84), len(y))


def project(model: RangeModel, r20: float, early: float) -> dict[str, float]:
    """早盤振幅 → 之後振幅的中位數與 68% 區間（點）。"""
    mu = model.a + model.b_ref * math.log(r20) + model.b_early * math.log(early)
    return {"lo": round(math.exp(mu + model.q16), 1), "mid": round(math.exp(mu + model.q50), 1),
            "hi": round(math.exp(mu + model.q84), 1)}


def tod_profile(sessions: Sequence[Session], last_n: int = PROFILE_DAYS, bucket: int = BUCKET_MIN) -> list[float]:
    """各時段（每 bucket 分鐘一格）每分鐘 |收−開| 相對該日全日平均的倍數，取近 last_n 個時段的中位數。"""
    nb = SESSION_MIN // bucket
    rows = []
    for s in list(sessions)[-last_n:]:
        r = np.abs(s.cl - s.op)
        day_mean = np.nanmean(r) if np.isfinite(r).any() else float("nan")
        if not day_mean or not math.isfinite(day_mean):
            continue
        rows.append([np.nanmean(r[b * bucket:(b + 1) * bucket]) / day_mean for b in range(nb)])
    if not rows:
        return []
    return [round(float(x), 3) for x in np.nanmedian(np.array(rows), axis=0)]


@dataclass(frozen=True)
class ContextData:
    day: str                              # 為哪一天算的
    r20: float | None                     # 今天的參考振幅（今天以前最近 20 個時段）
    models: dict[int, RangeModel]
    profile: list[float]
    n_days: int
    last_session: str | None
    day_iv: dict | None = None            # 開盤前預期日盤振幅（core/iv_day_range，發現 55）：{expect, …} 或 {note}


def compute(store: Any, code: str, today: str) -> ContextData:
    """（同步、會讀資料庫）背景執行緒呼叫。只用今天以前的時段。"""
    sessions = load_sessions(store, code, before=today)
    rng = [window_range(s, 0, SESSION_MIN) for s in sessions]
    r20 = round(float(np.mean(rng[-N_REF:])), 1) if len(rng) >= N_REF else None
    models = {}
    for c in CHECKPOINTS:
        m = fit_range_model(sessions, c)
        if m:
            models[c] = m
    try:
        from core import iv_day_range                 # 延後 import：它也 import 這個模組
        day_iv: dict | None = iv_day_range.for_today(sessions, today)
    except Exception:
        logger.warning("開盤前預期日盤振幅計算失敗（略過）", exc_info=True)
        day_iv = {"note": "計算失敗（細節在後端 log）"}
    return ContextData(today, r20, models, tod_profile(sessions), len(sessions), sessions[-1].date if sessions else None, day_iv)


# ── 描述：把資料＋現在的時間＋即時的早盤振幅，組成給畫面用的 dict ───────────────

def bucket_label(i: int, bucket: int = BUCKET_MIN) -> str:
    a, b = OPEN_MIN + i * bucket, OPEN_MIN + (i + 1) * bucket
    return f"{a // 60:02d}:{a % 60:02d}~{b // 60:02d}:{b % 60:02d}"


def describe(data: ContextData | None, now: datetime, cp: dict | None, session: str) -> dict[str, Any]:
    """cp＝live_state 的 snapshot['cp']（每個檢查點的 early／post 振幅）；重啟後沒有完整開盤資料時是 None。"""
    minute = now.hour * 60 + now.minute
    out: dict[str, Any] = {"session": session, "ready": data is not None}
    settle = is_settlement_day(now.date())
    if session == "night":
        out["mins_to_close"] = (24 * 60 - minute + 300) if minute >= 15 * 60 else (300 - minute)
        out["close_hhmm"] = "05:00"
        return out
    if session != "day":
        return out
    m = minute - OPEN_MIN
    close_min = SETTLE_CLOSE_MIN if settle else SESSION_MIN
    out.update({"elapsed": m, "settle_day": settle, "mins_to_close": max(close_min - m, 0),
                "close_hhmm": "13:30" if settle else "13:45"})
    if data is None:
        return out
    out["r20"] = data.r20
    out["n_days"] = data.n_days
    if data.day_iv is not None:
        out["day_iv"] = data.day_iv
    if data.profile:
        i = min(m // BUCKET_MIN, len(data.profile) - 1)
        out["bucket"] = {"label": bucket_label(i), "mult": data.profile[i], "idx": i}
        out["profile"] = data.profile
    # 預期之後的振幅：用已經走完的最後一個檢查點（09:15 之後用 30 分、09:45 之後用 60 分）
    done = [c for c in CHECKPOINTS if m >= c and c in data.models]
    if not done:
        out["proj_note"] = "開盤後 30 分鐘（09:15）才會有預期振幅"
    elif cp is None or not data.r20 or data.r20 <= 0:
        out["proj_note"] = "後端在開盤後才啟動，沒有完整的開盤資料，不預測"
    else:
        c = done[-1]
        cc = cp.get(str(c)) or {}
        early = cc.get("early")
        if not early or early <= 0:
            out["proj_note"] = "沒有早盤資料"
        else:
            mod = data.models[c]
            exp = project(mod, data.r20, early)
            post = cc.get("post") or 0.0
            out["proj"] = {"c": c, "early": early, "expect": exp, "done": post,
                           "done_ratio": round(post / exp["mid"], 2) if exp["mid"] > 0 else None, "n": mod.n}
    return out


class IntradayContext:
    """每天算一次，讀取只回快取。第一次讀到的是 None，背景算好之後下一次輪詢就有（約幾秒）。"""

    def __init__(self) -> None:
        self.data: ContextData | None = None
        self._task: asyncio.Future | None = None
        self._fail_at = 0.0

    async def get(self, store: Any, code: str, today: str) -> ContextData | None:
        if self.data is not None and self.data.day == today:
            return self.data
        if (self._task is None or self._task.done()) and time.time() - self._fail_at >= RETRY_SEC:
            self._task = asyncio.ensure_future(self._refresh(store, code, today))
        return None

    async def _refresh(self, store: Any, code: str, today: str) -> None:
        try:
            self.data = await asyncio.to_thread(compute, store, code, today)
            logger.info("盤中時段脈絡已更新（%s）：%d 個時段、R20=%s、模型 %s", today, self.data.n_days, self.data.r20,
                        sorted(self.data.models))
        except Exception:
            self._fail_at = time.time()
            logger.warning("盤中時段脈絡計算失敗（%d 秒後再試）", RETRY_SEC, exc_info=True)


intraday_context = IntradayContext()


# ── 發現 28 的篩檢（重現用）────────────────────────────────────────

def settlement_dates(dates: Sequence[date]) -> set[str]:
    """資料期間內每月第三個週三；那天沒開盤就取之後最近的交易日（4 天內）。"""
    out: set[str] = set()
    for y in range(dates[0].year, dates[-1].year + 1):
        for mo in range(1, 13):
            d1 = date(y, mo, 1)
            w3 = d1 + timedelta(days=(2 - d1.weekday()) % 7 + 14)
            nxt = next((x for x in dates if x >= w3), None)
            if nxt and (nxt - w3).days <= 4 and dates[0] <= w3 <= dates[-1]:
                out.add(nxt.isoformat())
    return out


def screen(sessions: Sequence[Session]) -> dict[str, Any]:
    """事先登記的 5 個檢定：Q1(c=30,60) 早盤振幅有助預測之後振幅；Q2(c=30,60) 相對成交量再加分；Q3 結算日尾盤振幅較大。"""
    res: dict[str, Any] = {"z": Z_ONE_SIDED, "q": {}}
    for c in CHECKPOINTS:
        t = checkpoint_table(sessions, c)
        y = np.log(t["Y"])
        one = np.ones(len(y))
        XA = np.column_stack([one, np.log(t["R"])])
        XB = np.column_stack([one, np.log(t["R"]), np.log(t["E"])])
        XC = np.column_stack([one, np.log(t["R"]), np.log(t["E"]), np.log(t["RV"])])
        eA, eB, eC = (oos_errors(X, y) for X in (XA, XB, XC))
        rmse = lambda e: math.sqrt(float((e ** 2).mean()))     # noqa: E731
        ratio = t["E"] / t["R"]
        q1, q2 = np.quantile(ratio, [1 / 3, 2 / 3])
        yr = t["Y"] / t["R"]
        res["q"][c] = {
            "n": len(y), "n_oos": len(eA), "dm_q1": dm_t(eA, eB), "dm_q2": dm_t(eB, eC),
            "rmse_a": rmse(eA), "rmse_b": rmse(eB), "rmse_c": rmse(eC),
            "tercile": float(yr[ratio >= q2].mean() / yr[ratio <= q1].mean()),
        }
    R = ref_ranges(sessions)
    dates = [datetime.strptime(s.date, "%Y-%m-%d").date() for s in sessions]
    settle = settlement_dates(dates)
    last30 = np.array([window_range(s, 255, 285) if np.isfinite(s.hi[255:285]).sum() >= 25 else float("nan") for s in sessions])
    ratio = last30 / R
    is_s = np.array([s.date in settle for s in sessions])
    ok = np.isfinite(ratio)
    a, b = np.log(ratio[ok & is_s]), np.log(ratio[ok & ~is_s])
    res["q3"] = {"n_settle": len(a), "n_other": len(b), "mult": float(math.exp(a.mean() - b.mean())),
                 "t": float((a.mean() - b.mean()) / math.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))),
                 "vol_ratio": float(np.median([np.nansum(s.vol) for s, f in zip(sessions, is_s) if f])
                                    / np.median([np.nansum(s.vol) for s, f in zip(sessions, is_s) if not f]))}
    return res


def format_screen(r: dict[str, Any]) -> str:
    z = r["z"]
    lines = [f"盤中波動指標篩檢（5 個檢定，單尾 α=0.01，z>{z:.2f}）"]
    for c, q in r["q"].items():
        lines.append(f"  開盤後 {c} 分：樣本 {q['n']}、樣本外 {q['n_oos']}；RMSE(對數) 只看 R20 {q['rmse_a']:.4f} → 加早盤振幅 {q['rmse_b']:.4f}"
                     f"（{(1 - q['rmse_b'] / q['rmse_a']) * 100:+.1f}%）→ 再加相對量 {q['rmse_c']:.4f}")
        lines.append(f"    Q1 早盤振幅 DM t={q['dm_q1']:+.2f} {'過' if q['dm_q1'] > z else '沒過'}；早盤振幅最大／最小三分位 {q['tercile']:.2f}×"
                     f"（{'≥1.2 實用' if q['tercile'] >= 1.2 else '<1.2 不實用'}）；Q2 相對量 DM t={q['dm_q2']:+.2f} {'過' if q['dm_q2'] > z else '沒過'}")
    q3 = r["q3"]
    lines.append(f"  Q3 結算日 13:00~13:30 振幅 ÷ R20：結算日 {q3['n_settle']} 天 vs 一般 {q3['n_other']} 天，{q3['mult']:.2f}×，"
                 f"t={q3['t']:+.2f} {'過' if q3['t'] > z else '沒過'}（結算日成交量只有一般日的 {q3['vol_ratio']:.2f}×，近月連續合約可能已換月，結論不可靠）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="盤中時段脈絡（只讀 market_state.db）")
    ap.add_argument("cmd", choices=["screen", "show"])
    ap.add_argument("--code", default="TXF")
    a = ap.parse_args(argv)
    from core.market_store import MarketStore
    store = MarketStore()
    if a.cmd == "screen":
        sessions = load_sessions(store, a.code)
        print(f"{len(sessions)} 個時段：{sessions[0].date} ~ {sessions[-1].date}")
        print(format_screen(screen(sessions)))
        return 0
    today = date.today().isoformat()
    data = compute(store, a.code, today)
    print(f"{data.n_days} 個時段（到 {data.last_session}）；今天的參考振幅 R20={data.r20}")
    for c, m in data.models.items():
        print(f"  開盤後 {c} 分模型：log Y = {m.a:.3f} + {m.b_ref:.3f}·log R20 + {m.b_early:.3f}·log E；殘差 16/50/84% = "
              f"{m.q16:+.3f}/{m.q50:+.3f}/{m.q84:+.3f}（n={m.n}）")
    print("  各時段每分鐘波動倍數：" + "  ".join(f"{bucket_label(i)} {v:.2f}" for i, v in enumerate(data.profile)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
