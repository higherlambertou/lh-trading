"""事件日分析（update.md 發現 50）：FOMC／CPI／非農的「反應日」日盤，振幅與跳空是不是系統性比平常大。離線分析，不碰交易路徑。

判定標準在跑之前寫進 update.md。只問「會走多遠」，不問方向。
- 資料：TXF 日盤 1 分 K（bars_1m）；事件日期來自 lh-intel 的 intel.db（fed＝FOMC 聲明時間；fred_hist＝CPI、非農的歷史發布日）。
- 反應日＝開盤（台北 08:45）晚於事件時間的第一個日盤；指標只用當天以前的資料當分母。
- 6 個檢定（3 事件 × 2 指標）：依星期分層的單尾排列檢定；探索期 p < 0.05/6 才進保留期，保留期 p < 0.05 才算成立。
用法：python -m core.event_day_study run [--intel-db 路徑]（結果存 data/event_day_result.json，只跑一次；--rerun 才重跑）"""
from __future__ import annotations

import json
import sqlite3
import sys
from bisect import bisect_right
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from core.intraday_context import SESSION_MIN, Session, settlement_dates

ROOT = Path(__file__).resolve().parent.parent
RESULT_PATH = ROOT / "data" / "event_day_result.json"
DEFAULT_INTEL_DB = ROOT.parent / "lh-intel" / "data" / "intel.db"

TPE, NY = ZoneInfo("Asia/Taipei"), ZoneInfo("America/New_York")
START = "2021-01-01"                  # 分析起點（基準可以用更早的資料）
SPLIT = "2025-01-01"                  # 保留期起點（探索時不看）
EVENTS = ("FOMC", "CPI", "NFP")
METRICS = ("range", "gap")
N_BASE = 20                           # 分母：前 20 個日盤
N_TESTS = len(EVENTS) * len(METRICS)
ALPHA_EXPLORE = 0.05 / N_TESTS        # Bonferroni
ALPHA_HOLDOUT = 0.05
MIN_AMPLIFY = 1.10                    # 95% 區間下限要到這個才「值得調範圍帶」
B_PERM, B_BOOT, SEED = 20000, 5000, 20261019
MAX_REACTION_DAYS = 6                 # 事件到反應日超過這麼多天＝資料有洞，略過
CANARY_FAIL = 4                       # 金絲雀：≥ 這麼多組 p < 0.05 → 方法有問題

# 同月兩個日期的是『修訂資料』的發布、不是當月數據的發布日（登記在 update.md 發現 50 的邊界規則）
NOT_RELEASE = {("CPI", "2021-02-08"), ("CPI", "2022-02-08"), ("CPI", "2023-02-10"), ("CPI", "2024-02-09"), ("NFP", "2024-01-10")}
CPI_TITLE, NFP_TITLE = "Consumer Price Index", "Employment Situation"


# ── 事件 ──────────────────────────────────────────────────────────

def load_events(intel_db: Path | str) -> dict[str, list[float]]:
    """從 intel.db 唯讀取出三種事件的發生時間（epoch 秒）。這是研究讀取：日期是事實（不是由結果推出來的），不套 as_of。"""
    conn = sqlite3.connect(f"file:{intel_db}?mode=ro", uri=True)
    try:
        out: dict[str, set[float]] = {k: set() for k in EVENTS}
        for t, in conn.execute("SELECT scheduled_time FROM events e WHERE source='fed' AND title LIKE 'FOMC 利率決策%' AND scheduled_time IS NOT NULL AND status!='cancelled' "
                               "AND revision=(SELECT MAX(revision) FROM events WHERE uid=e.uid)"):
            out["FOMC"].add(float(t))
        for key, title in (("CPI", CPI_TITLE), ("NFP", NFP_TITLE)):
            for d, in conn.execute("SELECT DISTINCT scheduled_date FROM events WHERE source='fred_hist' AND title=?", (title,)):
                if (key, d) not in NOT_RELEASE:
                    out[key].add(release_time(d))
        return {k: sorted(v) for k, v in out.items()}
    finally:
        conn.close()


def release_time(iso_date: str) -> float:
    """美國當地日期的 08:30（美東，含夏令）→ epoch 秒。"""
    d = date.fromisoformat(iso_date)
    return datetime(d.year, d.month, d.day, 8, 30, tzinfo=NY).timestamp()


def session_starts(dates: Sequence[str]) -> list[float]:
    return [datetime.fromisoformat(d).replace(hour=8, minute=45, tzinfo=TPE).timestamp() for d in dates]


def reaction_index(t: float, starts: Sequence[float], dates: Sequence[str]) -> Optional[int]:
    """事件時間 t 之後的第一個日盤的序號；超出資料、或離事件超過 MAX_REACTION_DAYS 天＝None。"""
    i = bisect_right(starts, t)
    if i >= len(starts):
        return None
    if (date.fromisoformat(dates[i]) - datetime.fromtimestamp(t, TPE).date()).days > MAX_REACTION_DAYS:
        return None
    return i


# ── 指標 ──────────────────────────────────────────────────────────

def day_table(sessions: Sequence[Session]) -> dict[str, np.ndarray]:
    """每個日盤的高低差（點）、開盤、收盤、跳空（開盤 − 前一日盤收盤，點）。"""
    n = len(sessions)
    rng, op, cl = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    for i, s in enumerate(sessions):
        ok = np.isfinite(s.hi) & np.isfinite(s.lo)
        if not ok.any():
            continue
        rng[i] = np.nanmax(s.hi) - np.nanmin(s.lo)
        j = np.flatnonzero(np.isfinite(s.op))
        k = np.flatnonzero(np.isfinite(s.cl))
        if len(j) and len(k):
            op[i], cl[i] = s.op[j[0]], s.cl[k[-1]]
    gap = np.full(n, np.nan)
    gap[1:] = op[1:] - cl[:-1]
    return {"range": rng, "gap_abs": np.abs(gap), "op": op, "cl": cl}


def log_ratios(values: np.ndarray, n: int = N_BASE) -> np.ndarray:
    """ln(第 i 天 ÷ 前 n 天的平均)；分母**只用第 i 天以前**；值或分母 ≤ 0／NaN → NaN（跳空為 0 的日子在這裡被剔除）。"""
    out = np.full(len(values), np.nan)
    for i in range(n, len(values)):
        base = values[i - n:i]
        if not np.isfinite(base).all():
            continue
        m, v = base.mean(), values[i]
        if np.isfinite(v) and v > 0 and m > 0:
            out[i] = np.log(v / m)
    return out


# ── 檢定 ──────────────────────────────────────────────────────────

def strat_perm_test(x_ev: np.ndarray, wd_ev: np.ndarray, x_pool: np.ndarray, wd_pool: np.ndarray, b: Optional[int] = None, seed: int = SEED) -> dict[str, float]:
    """單尾分層排列檢定。對照＝從 pool 依事件日的星期分布抽同樣天數（不放回），算平均 b 次。
    回傳 mean_event、mean_ctrl（對照的期望值＝依星期加權的 pool 平均）、amp（exp 差）、p（(1+對照平均 ≥ 事件平均 的次數)/(b+1)）、n。"""
    b = B_PERM if b is None else b
    n = len(x_ev)
    if n == 0:
        return {"n": 0, "mean_event": float("nan"), "mean_ctrl": float("nan"), "amp": float("nan"), "p": float("nan")}
    rng = np.random.default_rng(seed)
    ctrl_sum = np.zeros(b)
    exp_sum = 0.0
    for w in np.unique(wd_ev):
        k = int((wd_ev == w).sum())
        pool = x_pool[wd_pool == w]
        if len(pool) < k:
            raise ValueError(f"星期 {w} 的對照只有 {len(pool)} 天，事件有 {k} 天")
        exp_sum += k * pool.mean()
        idx = np.argpartition(rng.random((b, len(pool))), k - 1, axis=1)[:, :k]
        ctrl_sum += pool[idx].sum(axis=1)
    mean_ev = float(x_ev.mean())
    ctrl = ctrl_sum / n
    ctrl_mean = exp_sum / n
    p = (1 + int((ctrl >= mean_ev - 1e-12).sum())) / (b + 1)
    return {"n": n, "mean_event": mean_ev, "mean_ctrl": float(ctrl_mean), "amp": float(np.exp(mean_ev - ctrl_mean)), "p": float(p)}


def boot_amp_ci(x_ev: np.ndarray, ctrl_mean: float, b: Optional[int] = None, seed: int = SEED) -> tuple[float, float]:
    """放大倍數的 95% 區間：事件日重抽 b 次，對照平均固定。"""
    if len(x_ev) == 0:
        return float("nan"), float("nan")
    b = B_BOOT if b is None else b
    rng = np.random.default_rng(seed)
    means = x_ev[rng.integers(0, len(x_ev), (b, len(x_ev)))].mean(axis=1)
    lo, hi = np.quantile(means, [0.025, 0.975])
    return float(np.exp(lo - ctrl_mean)), float(np.exp(hi - ctrl_mean))


def mde_amp(sd: float, n: int, alpha: float, power: float = 0.8) -> float:
    """這個樣本數與標準差下，單尾 alpha、檢定力 power 能偵測的最小放大倍數（常態近似）。"""
    from statistics import NormalDist
    z = NormalDist().inv_cdf
    return float(np.exp((z(1 - alpha) + z(power)) * sd / np.sqrt(n))) if n > 0 else float("nan")


# ── 主流程 ────────────────────────────────────────────────────────

def build(sessions: Sequence[Session], events: dict[str, list[float]]) -> dict[str, Any]:
    """把日盤與事件整理成：日期、星期、兩個指標、各事件的反應日序號、結算日與可用的對照日。"""
    dates = [s.date for s in sessions]
    t = day_table(sessions)
    x = {"range": log_ratios(t["range"]), "gap": log_ratios(t["gap_abs"])}
    starts = session_starts(dates)
    react: dict[str, set[int]] = {}
    for k in EVENTS:
        react[k] = {i for i in (reaction_index(e, starts, dates) for e in events.get(k, [])) if i is not None}
    st = settlement_dates([date.fromisoformat(d) for d in dates])
    settle = {i for i, d in enumerate(dates) if d in st}
    return {"dates": dates, "wd": np.array([date.fromisoformat(d).weekday() for d in dates]), "x": x, "react": react, "settle": settle, "table": t}


def _days(b: dict[str, Any], idx: set[int], lo: str, hi: Optional[str], metric: str) -> np.ndarray:
    """落在 [lo, hi] 且該指標有值、不是結算日的序號。"""
    dates, x = b["dates"], b["x"][metric]
    return np.array(sorted(i for i in idx if dates[i] >= lo and (hi is None or dates[i] < hi) and i not in b["settle"] and np.isfinite(x[i])), int)


def one_test(b: dict[str, Any], ev_idx: set[int], metric: str, lo: str, hi: Optional[str], exclude: set[int]) -> dict[str, float]:
    """某事件（序號集合）在 [lo, hi) 內、某指標的排列檢定；對照＝同期間不在 exclude 內的日子。"""
    x = b["x"][metric]
    ev = _days(b, ev_idx, lo, hi, metric)
    pool = _days(b, set(range(len(b["dates"]))) - exclude, lo, hi, metric)
    return strat_perm_test(x[ev], b["wd"][ev], x[pool], b["wd"][pool])


def run(sessions: Sequence[Session], events: dict[str, list[float]]) -> dict[str, Any]:
    b = build(sessions, events)
    dates = b["dates"]
    all_react = set().union(*b["react"].values())
    out: dict[str, Any] = {"window": [START, dates[-1]], "split": SPLIT, "n_sessions": len(dates), "alpha_explore": ALPHA_EXPLORE, "alpha_holdout": ALPHA_HOLDOUT,
                           "tests": {}, "descriptive": {}, "canary": {}, "verdict": {}}
    for k in EVENTS:
        for m in METRICS:
            key = f"{k}/{m}"
            ex = one_test(b, b["react"][k], m, START, SPLIT, all_react)
            res: dict[str, Any] = {"explore": ex, "candidate": bool(ex["n"] > 0 and ex["p"] < ALPHA_EXPLORE and ex["mean_event"] > ex["mean_ctrl"])}
            hold = one_test(b, b["react"][k], m, SPLIT, None, all_react)
            res["holdout"] = hold                                   # 保留期一律算好存下，但只有 candidate 才拿來判定
            res["confirmed"] = bool(res["candidate"] and hold["n"] > 0 and hold["p"] < ALPHA_HOLDOUT and hold["mean_event"] > hold["mean_ctrl"])
            full = one_test(b, b["react"][k], m, START, None, all_react)
            x = b["x"][m]
            ev = _days(b, b["react"][k], START, None, m)
            lo, hi = boot_amp_ci(x[ev], full["mean_ctrl"])
            res["pooled"] = {**full, "amp_lo": lo, "amp_hi": hi}
            res["useful"] = bool(res["confirmed"] and lo >= MIN_AMPLIFY)
            out["tests"][key] = res
            out["verdict"][key] = "有用（確認且下限≥1.10）" if res["useful"] else "確認但幅度小" if res["confirmed"] else "探索過、保留期沒確認" if res["candidate"] else "沒過"
    out["descriptive"] = describe(b, all_react)
    out["canary"] = canary(b, all_react)
    return out


def describe(b: dict[str, Any], all_react: set[int]) -> dict[str, Any]:
    """只描述（不是檢定）：每種事件全期的筆數、放大倍數、事件日振幅比 P50／P80／P95 對照一般日、每年的放大倍數、事件日的點數中位數。"""
    t = b["table"]
    out: dict[str, Any] = {}
    for k in EVENTS:
        d: dict[str, Any] = {}
        for m, col in (("range", "range"), ("gap", "gap_abs")):
            x = b["x"][m]
            ev = _days(b, b["react"][k], START, None, m)
            pool = _days(b, set(range(len(b["dates"]))) - all_react, START, None, m)
            if len(ev) == 0:
                continue
            q = [50, 80, 95]
            d[m] = {"n": int(len(ev)), "ratio_event": {f"P{p}": float(np.exp(np.percentile(x[ev], p))) for p in q},
                    "ratio_ctrl": {f"P{p}": float(np.exp(np.percentile(x[pool], p))) for p in q},
                    "points_median_event": float(np.median(t[col][ev])), "points_median_ctrl": float(np.median(t[col][pool])),
                    "sd_log": float(x[ev].std(ddof=1)) if len(ev) > 1 else float("nan")}
            n_ex = sum(b["dates"][i] < SPLIT for i in ev)
            d[m]["mde_explore"] = mde_amp(d[m]["sd_log"], n_ex, ALPHA_EXPLORE) if len(ev) > 1 else float("nan")
            yearly = {}
            for y in sorted({b["dates"][i][:4] for i in ev}):
                evy = ev[[b["dates"][i][:4] == y for i in ev]]
                poolw = [x[pool][b["wd"][pool] == w].mean() for w in b["wd"][evy]]
                yearly[y] = {"n": int(len(evy)), "amp": float(np.exp(x[evy].mean() - np.mean(poolw)))}
            d[m]["yearly"] = yearly
        out[k] = d
    return out


def canary(b: dict[str, Any], all_react: set[int]) -> dict[str, Any]:
    """把每個事件換成『反應日的前一個日盤』，同樣 6 組各跑一次（全期）。≥ CANARY_FAIL 組單尾 p < 0.05 → 方法有問題。"""
    prev = {k: {i - 1 for i in b["react"][k] if i >= 1} for k in EVENTS}
    all_prev = set().union(*prev.values())
    res: dict[str, Any] = {}
    n_sig = 0
    for k in EVENTS:
        for m in METRICS:
            r = one_test(b, prev[k], m, START, None, all_react | all_prev)
            res[f"{k}/{m}"] = r
            n_sig += int(r["n"] > 0 and r["p"] < 0.05)
    res["n_significant"] = n_sig
    res["suspect"] = n_sig >= CANARY_FAIL
    return res


def yearly_text(yearly: dict[str, Any]) -> str:
    return ", ".join("%s:%.2f(n%d)" % (y, q["amp"], q["n"]) for y, q in yearly.items())


def format_report(r: dict[str, Any]) -> str:
    L = [f"事件日分析  {r['window'][0]} ～ {r['window'][1]}（探索期 < {r['split']}，保留期 ≥ {r['split']}）  日盤 {r['n_sessions']} 個",
         f"判定：探索 p < {r['alpha_explore']:.4f}（6 個檢定 Bonferroni）→ 保留 p < {r['alpha_holdout']}；幅度下限 ≥ {MIN_AMPLIFY}\n"]
    L.append(f"{'事件/指標':<10}{'探索 n':>7}{'倍數':>7}{'p':>8}  {'保留 n':>7}{'倍數':>7}{'p':>8}  {'全期倍數(95%區間)':>22}  結論")
    for key, t in r["tests"].items():
        e, h, p = t["explore"], t["holdout"], t["pooled"]
        L.append(f"{key:<10}{e['n']:>7}{e['amp']:>7.2f}{e['p']:>8.4f}  {h['n']:>7}{h['amp']:>7.2f}{h['p']:>8.4f}  {p['amp']:>6.2f} ({p['amp_lo']:.2f}~{p['amp_hi']:.2f}){'':>3}  {r['verdict'][key]}")
    L.append("")
    c = r["canary"]
    L.append(f"金絲雀（前一個日盤）：{c['n_significant']}/6 組 p<0.05 → {'⚠ 方法有問題，結果作廢' if c['suspect'] else '正常'}")
    for key, v in c.items():
        if "/" in key:
            L.append(f"  {key:<10} n={v['n']:>3} 倍數 {v['amp']:.2f} p={v['p']:.3f}")
    L.append("\n描述（全期，事件日 vs 一般日）：")
    for k, d in r["descriptive"].items():
        for m, v in d.items():
            L.append(f"  {k}/{m}: n={v['n']}  比值 P50/P80/P95 事件 {v['ratio_event']['P50']:.2f}/{v['ratio_event']['P80']:.2f}/{v['ratio_event']['P95']:.2f}  "
                     f"一般 {v['ratio_ctrl']['P50']:.2f}/{v['ratio_ctrl']['P80']:.2f}/{v['ratio_ctrl']['P95']:.2f}  點數中位數 {v['points_median_event']:.0f} vs {v['points_median_ctrl']:.0f}  "
                     f"探索期最小可測倍數 {v['mde_explore']:.2f}  每年 {yearly_text(v['yearly'])}")
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] != "run":
        print(__doc__)
        return 2
    if RESULT_PATH.exists() and "--rerun" not in a:
        print("（已經跑過，只跑一次；以下是存的結果）\n")
        print(format_report(json.loads(RESULT_PATH.read_text(encoding="utf-8"))))
        return 0
    from core.intraday_context import load_sessions
    from core.market_store import MarketStore
    db = Path(a[a.index("--intel-db") + 1]) if "--intel-db" in a else DEFAULT_INTEL_DB
    res = run(load_sessions(MarketStore(), "TXF"), load_events(db))
    RESULT_PATH.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(format_report(res))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
