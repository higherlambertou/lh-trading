"""Hurst 穩定度驗證（離線）：比較「現行日 K 60 根」與「5 分 K 滾動 N 日」等做法在真實歷史上的日間穩定度。

為什麼要做：現行盤前判斷（日 K 60 根）在真實資料上 23 個交易日狀態切換 18 次，相鄰兩天 |ΔH| 平均 0.18，
比純隨機漫步的理論值（0.08）還大一倍多——策略對應表的建議幾乎每天不同，兩週驗證沒有意義。
這支程式在真實歷史上比較幾種替代做法，量化「日間穩定度」，再決定要不要換。

用法：
  python -m core.hurst_study fetch [天數=130]   # 向券商抓日盤 1 分 K 存進 data/market_state.db（只讀行情、不下單）
  python -m core.hurst_study [study]            # 用資料庫裡的歷史跑比較

做法（每個做法都產出「每個交易日盤前的 H」序列）：
  A 日 K 60 根（現行）        DFA + iid 常態蒙地卡羅校準
  B 5 分 K 近 N 日（原始報酬） 日內相鄰 5 分收盤的對數報酬（不含隔夜跳空），串接後做 DFA；校準用「置換檢定」
  C 5 分 K 近 N 日（去季節性＋波動標準化）：每天先除以當天日內標準差，再除以同一個 5 分鐘時段的平均強度
     （去掉開盤/收盤的 U 型波動與大波動日的影響）
置換檢定：把窗口內的報酬隨機打亂（保留厚尾分布、拆掉時間順序）算 DFA，得到「沒有序列相關」時 H 的平均與標準差，
比 iid 常態更貼近真實資料。每個做法都再用純雜訊跑一次，當作「穩定度的理論下限」。
"""
from __future__ import annotations

import math
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any, Sequence

import numpy as np

from core.hurst_analyzer import (
    REVERT_TH, TREND_TH, aggregate_daily, analyze, day_returns, day_session_minutes, deseasonalize,
    dfa_permutation, five_min_closes,
)
from core.market_store import MarketStore

CODE = "TXF"
NULL_PERMS = 60                   # 置換檢定次數（真實資料）
NOISE_PERMS = 25                  # 置換檢定次數（純雜訊對照，省時間）
NOISE_REPEATS = 6                 # 純雜訊對照重複幾組


# ── 資料處理（共用計算在 core/hurst_analyzer.py）──────────────────────────────

def classify(h: float) -> str:
    return "TREND" if h > TREND_TH else "REVERT" if h < REVERT_TH else "RANDOM"


# ── 各做法的滾動序列 ──────────────────────────────────────────────

def rolling_daily(dates: Sequence[str], closes: Sequence[float], window: int = 60) -> list[tuple[str, float, float]]:
    """A：現行。dates[i] 這天盤前的 H = 用到 dates[i] 為止的最近 window 根日 K。"""
    out = []
    for i in range(window - 1, len(closes)):
        r = analyze(closes[i - window + 1: i + 1], dates[i - window + 1: i + 1], window=window)
        if r.value is not None:
            out.append((dates[i], r.value, r.z if r.z is not None else float("nan")))
    return out


def rolling_intraday(days: Sequence[str], rets: Sequence[np.ndarray], window_days: int, *, normalize: bool,
                     rng: np.random.Generator, n_perm: int) -> list[tuple[str, float, float]]:
    """B / C：最近 window_days 個交易日的日內 5 分 K 報酬（串接）。"""
    out = []
    for i in range(window_days - 1, len(days)):
        win = list(rets[i - window_days + 1: i + 1])
        r = deseasonalize(win) if normalize else np.concatenate(win)
        h, z, _ = dfa_permutation(r, rng, n_perm)
        if math.isfinite(h):
            out.append((days[i], h, z))
    return out


def metrics(series: Sequence[tuple[str, float, float]]) -> dict[str, Any]:
    hs = np.array([h for _, h, _ in series], dtype=float)
    states = [classify(h) for h in hs]
    d = np.abs(np.diff(hs)) if len(hs) > 1 else np.array([np.nan])
    return {
        "n": len(hs), "mean_abs_dh": float(np.nanmean(d)),
        "flips": int(sum(a != b for a, b in zip(states, states[1:]))),
        "h_std": float(hs.std()) if len(hs) else float("nan"),
        "share": {k: states.count(k) / len(states) if states else 0.0 for k in ("TREND", "RANDOM", "REVERT")},
        "from": series[0][0] if series else None, "to": series[-1][0] if series else None,
    }


# ── 純雜訊對照（同樣的流程，資料換成 iid 常態）────────────────────

def noise_baseline(kind: str, *, n_days: int, window_days: int, per_day: int, rng: np.random.Generator) -> dict[str, float]:
    abs_dh, flips = [], []
    for _ in range(NOISE_REPEATS):
        if kind == "daily":
            closes = list(20000 * np.exp(np.cumsum(rng.normal(0, 0.01, n_days))))
            dates = [str(i) for i in range(n_days)]
            s = rolling_daily(dates, closes, window_days)
        else:
            days = [str(i) for i in range(n_days)]
            rets = [rng.normal(0, 1, per_day) for _ in days]
            s = rolling_intraday(days, rets, window_days, normalize=(kind == "norm"), rng=rng, n_perm=NOISE_PERMS)
        m = metrics(s)
        abs_dh.append(m["mean_abs_dh"])
        flips.append(m["flips"] / max(m["n"] - 1, 1))
    return {"mean_abs_dh": float(np.mean(abs_dh)), "flip_rate": float(np.mean(flips))}


# ── 研究主程式 ────────────────────────────────────────────────────

def study(store: MarketStore | None = None, *, seed: int = 11, quiet: bool = False) -> dict[str, Any]:
    store = store or MarketStore()
    log = (lambda *a: None) if quiet else print
    daily = store.bars(CODE, 10_000)
    minutes = store.bars_1m(CODE)
    if len(daily) < 60 or not minutes:
        raise SystemExit("資料不足：需要日 K ≥60 根與 1 分 K 歷史。先執行 python -m core.hurst_study fetch")
    closes5 = five_min_closes(minutes)
    days = sorted(d for d, c in closes5.items() if len(c) >= 40)                # 日內至少 40 個 5 分時段才算完整的一天
    rets = day_returns(closes5, days)
    per_day = int(np.median([len(r) for r in rets]))
    rng = np.random.default_rng(seed)
    log(f"日 K {len(daily)} 根（{daily[0]['date']} ~ {daily[-1]['date']}）｜5 分 K {len(days)} 個完整交易日、每天約 {per_day} 筆報酬")

    schemes: dict[str, list[tuple[str, float, float]]] = {
        "A 日K 60根（現行）": rolling_daily([b["date"] for b in daily], [b["close"] for b in daily], 60),
        "B 5分K 20日 原始報酬": rolling_intraday(days, rets, 20, normalize=False, rng=rng, n_perm=NULL_PERMS),
        "C 5分K 20日 去季節性+波動標準化": rolling_intraday(days, rets, 20, normalize=True, rng=rng, n_perm=NULL_PERMS),
        "D 5分K 40日 去季節性+波動標準化": rolling_intraday(days, rets, 40, normalize=True, rng=rng, n_perm=NULL_PERMS),
    }
    noise_kind = {"A": ("daily", 60), "B": ("raw", 20), "C": ("norm", 20), "D": ("norm", 40)}
    results: dict[str, Any] = {}
    common_n = min(len(s) for s in schemes.values())                            # 共同期間：最後 common_n 個交易日
    log(f"共同比較期間：最後 {common_n} 個交易日（A 最短）；純雜訊對照每個做法跑 {NOISE_REPEATS} 組\n")
    for name, series in schemes.items():
        kind, win = noise_kind[name[0]]
        nb = noise_baseline(kind, n_days=win + 60, window_days=win, per_day=per_day, rng=rng)
        tail = series[-common_n:]
        results[name] = {"common": metrics(tail), "full": metrics(series), "noise": nb, "series": series}

    log(f"{'做法':<30s}{'天數':>5s}{'平均|ΔH|':>9s}{'純雜訊':>8s}{'倍數':>6s}{'切換':>9s}{'純雜訊切換率':>12s}{'H標準差':>8s}{'趨勢/隨機/回歸':>18s}")
    for name, r in results.items():
        c, nb = r["common"], r["noise"]
        sh = c["share"]
        log(f"{name:<30s}{c['n']:>5d}{c['mean_abs_dh']:>9.3f}{nb['mean_abs_dh']:>8.3f}{c['mean_abs_dh'] / nb['mean_abs_dh']:>6.1f}"
            f"{c['flips']:>5d}/{c['n'] - 1:<3d}{nb['flip_rate']:>11.0%}{c['h_std']:>9.3f}"
            f"   {sh['TREND']:>4.0%}/{sh['RANDOM']:>4.0%}/{sh['REVERT']:>4.0%}")
    full = {n: r["full"] for n, r in results.items() if n[0] in "BCD"}
    log("\n5 分 K 做法在完整歷史上（天數較多）：")
    for name, m in full.items():
        log(f"  {name:<30s} {m['n']:>3d} 天（{m['from']} ~ {m['to']}）平均|ΔH| {m['mean_abs_dh']:.3f}，切換 {m['flips']}/{m['n'] - 1}，H 範圍 "
            f"{min(h for _, h, _ in results[name]['series']):.2f}~{max(h for _, h, _ in results[name]['series']):.2f}")
    # 不同做法的 H 是否一致（共同期間的相關係數）
    names = list(results)
    hs = {n: np.array([h for _, h, _ in results[n]["series"][-common_n:]]) for n in names}
    log("\n共同期間各做法 H 的相關係數：")
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            log(f"  {a[0]} vs {b[0]}: {np.corrcoef(hs[a], hs[b])[0, 1]:+.2f}")
    return results


# ── 歷史資料抓取（獨立登入，只讀行情）─────────────────────────────

def fetch_history(days: int = 130, code: str = CODE, store: MarketStore | None = None) -> tuple[int, int]:
    """向券商抓日盤 1 分 K（分段，每段 25 天），存進 bars_1m 與 bars_daily。回傳 (日 K 根數, 1 分 K 筆數)。
    用 simulation=True 登入（只讀歷史行情、不下單、不動到正式盤連線），結束一定 logout。"""
    import shioaji as sj
    from dotenv import load_dotenv
    load_dotenv()
    store = store or MarketStore()
    api = sj.Shioaji(simulation=True)
    api.login(api_key=os.environ["SHIOAJI_API_KEY"], secret_key=os.environ["SHIOAJI_SECRET_KEY"], contracts_timeout=15000)
    try:
        contract = getattr(getattr(api.Contracts.Futures, code), code + "R1")
        now = datetime.now()
        complete_today = now.hour * 100 + now.minute >= 1346
        today = date.today()
        cursor, end = today - timedelta(days=days), today + timedelta(days=1)
        daily: dict[str, dict[str, Any]] = {}
        minutes: list[dict[str, Any]] = []
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=25), end)
            kb = None
            for attempt in (1, 2):
                try:
                    kb = api.kbars(contract=contract, start=cursor.isoformat(), end=chunk_end.isoformat(), timeout=45000)
                    break
                except Exception as e:                                  # 券商資料伺服器偶發逾時：重試一次
                    print(f"  {cursor}~{chunk_end} 第 {attempt} 次失敗：{e!r}", file=sys.stderr)
            if kb is not None:
                raw = {"ts": [int(x) for x in kb.ts], "open": list(kb.Open), "high": list(kb.High),
                       "low": list(kb.Low), "close": list(kb.Close), "volume": list(kb.Volume)}
                for b in aggregate_daily(raw):
                    daily[b["date"]] = b
                minutes += day_session_minutes(raw)
                print(f"  {cursor} ~ {chunk_end}: 累計日 K {len(daily)} 根")
            cursor = chunk_end + timedelta(days=1)
        ds = today.isoformat()
        bars = [b for b in daily.values() if b["nbars"] >= 30 and (b["date"] < ds or complete_today)]
        ok = {b["date"] for b in bars}
        mins = [m for m in minutes if m["date"] in ok]
        store.upsert_bars(code, bars)
        store.upsert_bars_1m(code, mins)
        return len(bars), len(mins)
    finally:
        try:
            api.logout()
        except Exception:
            pass


def _main() -> None:  # pragma: no cover
    from dotenv import load_dotenv
    load_dotenv()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "study"
    if cmd == "fetch":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 130
        t0 = time.time()
        bars, mins = fetch_history(n)
        print(f"完成：日 K {bars} 根、日盤 1 分 K {mins} 筆（{time.time() - t0:.0f} 秒）")
    else:
        study()


if __name__ == "__main__":  # pragma: no cover
    _main()
