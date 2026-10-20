"""實驗 1：零樣本基礎模型（Chronos）預測台指期之後 30 分鐘的波動與成交量，能不能贏過 EWMA／HAR（update.md 發現 56）。離線分析，不碰交易路徑。

判定標準在跑之前寫進 update.md。流程：
  1. `python -m core.foundation_vol_study export`：把日盤 1 分 K 整理成序列與決策時點，存 data/foundation_vol_input.npz（anaconda 環境）。
  2. `scripts/chronos_forecast.py`：在裝了 chronos-forecasting 的隔離環境跑，產出 data/foundation_vol_forecast.npz（Chronos 的 30 分鐘預測總和）。
  3. `python -m core.foundation_vol_study run`：載入預測，做 4 個檢定，結果存 data/foundation_vol_result.json（只跑一次；--rerun 才重跑）。
決策時點＝日盤第 30、60、…、240 分鐘；目標＝之後 30 分鐘的 ①已實現波動 ln√(平均每分鐘報酬平方) ②成交量 ln(平均每分鐘口數＋1)。
基準與特徵一律只用決策時點以前的資料；每個預測器都過同一道擴張視窗 OLS 校準（第 d 天只用 d 之前的日子估計）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

from core.intraday_context import SESSION_MIN, Session

ROOT = Path(__file__).resolve().parent.parent
INPUT_PATH = ROOT / "data" / "foundation_vol_input.npz"
FORECAST_PATH = ROOT / "data" / "foundation_vol_forecast.npz"
RESULT_PATH = ROOT / "data" / "foundation_vol_result.json"

CHECKPOINTS = tuple(range(30, 241, 30))
H = 30                                   # 預測長度與目標窗口（分鐘）
CONTEXT = 512                            # 餵給 Chronos 的脈絡長度
MIN_TARGET_VALID = 25                    # 目標窗口至少幾個有效分鐘
MIN_VALID_FRAC = 0.8                     # 特徵窗口至少這個比例有效
HALF_LIFE = 60.0
TOD_DAYS, TOD_MIN = 20, 15
WARM_DAYS = 250
SPLIT = "2025-01-01"
LAG = 10
N_TESTS = 4
Z_EXPLORE, Z_HOLDOUT, MIN_REDUCTION = 2.24, 1.645, 0.02
WINDOWS = (30, 120, 300, 1500)           # HAR 的視窗（分鐘）
TARGETS = ("rv", "vol")
EPS_RV = 1e-12


# ── 序列 ─────────────────────────────────────────────────────────

def series(sessions: Sequence[Session]) -> dict[str, np.ndarray]:
    """每個日盤每分鐘：絕對對數報酬 a（第一分鐘用收盤對開盤，不含隔夜跳空）與成交量 v；缺的是 NaN。形狀 (天數, 300)。"""
    D = len(sessions)
    a, v = np.full((D, SESSION_MIN), np.nan), np.full((D, SESSION_MIN), np.nan)
    for d, s in enumerate(sessions):
        cl, op = s.cl, s.op
        prev = np.concatenate([[op[0]], cl[:-1]])
        with np.errstate(all="ignore"):
            r = np.log(cl / prev)
        ok = np.isfinite(r) & (cl > 0) & (prev > 0)
        a[d, ok] = np.abs(r[ok])
        v[d] = np.where(np.isfinite(s.vol) & np.isfinite(s.cl), s.vol, np.nan)
    return {"a": a, "v": v}


def level(kind: str, a: np.ndarray, v: np.ndarray) -> np.ndarray:
    """目標與特徵所用的「水準」序列：rv＝報酬平方；vol＝成交量（攤平成一條）。"""
    return (a ** 2 if kind == "rv" else v).ravel()


def transform(kind: str, mean: np.ndarray) -> np.ndarray:
    return 0.5 * np.log(mean + EPS_RV) if kind == "rv" else np.log(mean + 1.0)


class Prefix:
    """序列的前綴和（缺值當 0、另記有效個數），O(1) 取任一窗口的平均與有效比例。"""

    def __init__(self, x: np.ndarray) -> None:
        ok = np.isfinite(x)
        self.s = np.concatenate([[0.0], np.cumsum(np.where(ok, x, 0.0))])
        self.n = np.concatenate([[0], np.cumsum(ok.astype(int))])

    def mean(self, lo: np.ndarray, hi: np.ndarray, min_valid: float | np.ndarray) -> np.ndarray:
        """[lo, hi) 的平均；窗口超出資料、或有效個數 < min_valid 是 NaN。"""
        lo, hi = np.asarray(lo), np.asarray(hi)
        inside = (lo >= 0) & (hi <= len(self.s) - 1)
        lo_c, hi_c = np.clip(lo, 0, len(self.s) - 1), np.clip(hi, 0, len(self.s) - 1)
        cnt = self.n[hi_c] - self.n[lo_c]
        with np.errstate(all="ignore"):
            m = (self.s[hi_c] - self.s[lo_c]) / cnt
        return np.where(inside & (cnt >= min_valid) & (cnt > 0), m, np.nan)


def ewma(x: np.ndarray, half_life: float = HALF_LIFE) -> np.ndarray:
    """e[t]＝到第 t 個值（含）為止的指數平均；缺值就沿用前一個。第一個有效值之前是 NaN。"""
    lam = 0.5 ** (1.0 / half_life)
    out = np.full(len(x), np.nan)
    cur = np.nan
    for t, val in enumerate(x):
        if np.isfinite(val):
            cur = val if not np.isfinite(cur) else lam * cur + (1 - lam) * val
        out[t] = cur
    return out


def samples_index(D: int) -> tuple[np.ndarray, np.ndarray]:
    """所有 (第幾天, 第幾個檢查點) 與它們在攤平序列裡的決策位置 p（決策時點之前的最後一根是 p−1）。"""
    d = np.repeat(np.arange(D), len(CHECKPOINTS))
    k = np.tile(np.arange(len(CHECKPOINTS)), D)
    p = d * SESSION_MIN + np.array(CHECKPOINTS)[k]
    return np.column_stack([d, k]), p


def features(kind: str, a: np.ndarray, v: np.ndarray) -> dict[str, np.ndarray]:
    """每個樣本（天×檢查點，攤成一維）的目標 y 與基準特徵 har1～har4、ewma、tod；全部只用決策時點以前（目標除外）。"""
    D = a.shape[0]
    L = level(kind, a, v)
    pre = Prefix(L)
    idx, p = samples_index(D)
    d, k = idx[:, 0], idx[:, 1]
    out: dict[str, np.ndarray] = {"day": d, "k": k, "p": p}
    for w, name in zip(WINDOWS, ("har1", "har2", "har3", "har4")):
        out[name] = transform(kind, pre.mean(p - w, p, MIN_VALID_FRAC * w))
    out["y"] = transform(kind, pre.mean(p, p + H, MIN_TARGET_VALID))
    e = ewma(L)
    out["ewma"] = np.where(p >= 1, transform(kind, e[np.clip(p - 1, 0, len(e) - 1)]), np.nan)
    # 時段季節：前 20 個日盤同一個 30 分鐘窗口的平均
    W = np.full((D, len(CHECKPOINTS)), np.nan)
    for j, m in enumerate(CHECKPOINTS):
        base = np.arange(D) * SESSION_MIN + m
        W[:, j] = pre.mean(base, base + H, MIN_TARGET_VALID)
    tod = np.full(len(d), np.nan)
    for i in range(len(d)):
        if d[i] >= 1:
            prev = W[max(0, d[i] - TOD_DAYS):d[i], k[i]]
            good = prev[np.isfinite(prev)]
            if len(good) >= TOD_MIN:
                tod[i] = float(transform(kind, np.array([good.mean()]))[0])
    out["tod"] = tod
    return out


def chronos_feature(kind: str, fc_sum: np.ndarray) -> np.ndarray:
    """Chronos 預測 30 個值的總和 → 特徵（預測的平均每分鐘水準取對數；rv 的預測對象是絕對報酬，不是報酬平方，尺度由校準吸收）。"""
    mean = fc_sum / H
    return np.log(mean + (1e-8 if kind == "rv" else 1.0))


# ── 匯出給 Chronos 腳本 ───────────────────────────────────────────

def export_inputs(sessions: Sequence[Session], path: Path = INPUT_PATH) -> int:
    """把攤平的序列與所有可能的決策位置存成 npz（絕對報酬乘 1e4，Chronos 會自己再縮放；缺值給 0）。回傳決策位置數。"""
    s = series(sessions)
    ar = np.nan_to_num(s["a"].ravel() * 1e4, nan=0.0)
    vv = np.nan_to_num(s["v"].ravel(), nan=0.0)
    idx, p = samples_index(len(sessions))
    keep = p >= CONTEXT
    np.savez_compressed(path, a=ar.astype(np.float32), v=vv.astype(np.float32), p=p[keep], dates=np.array([x.date for x in sessions]), context=CONTEXT, horizon=H)
    return int(keep.sum())


# ── 樣本外評估 ───────────────────────────────────────────────────

def oos_predict(days: np.ndarray, y: np.ndarray, X: np.ndarray, warm: Optional[int] = None) -> np.ndarray:
    """擴張視窗：第 d 天（排序後第 ≥ warm 天）的樣本只用 d 之前所有日子的樣本做 OLS 後預測；前 warm 天是 NaN。"""
    warm = WARM_DAYS if warm is None else warm
    out = np.full(len(y), np.nan)
    ud = np.unique(days)
    for i, dd in enumerate(ud):
        if i < warm:
            continue
        tr = days < dd
        te = days == dd
        b = np.linalg.lstsq(X[tr], y[tr], rcond=None)[0]
        out[te] = X[te] @ b
    return out


def day_loss_diff(days: np.ndarray, e_a: np.ndarray, e_b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """每天把所有樣本的 (e_a² − e_b²) 平均成一個數；回傳 (日序號, 差)。"""
    ok = np.isfinite(e_a) & np.isfinite(e_b)
    ud = np.unique(days[ok])
    diff = np.array([float(np.mean(e_a[ok & (days == d)] ** 2 - e_b[ok & (days == d)] ** 2)) for d in ud])
    return ud, diff


def dm(d: np.ndarray, lag: int = LAG) -> float:
    """Diebold-Mariano（HAC 變異數，Bartlett 權重）：d 的平均 ÷ 標準誤；d＝基準損失 − 候選損失，所以候選較準 → 正。"""
    n = len(d)
    if n < 30:
        return float("nan")
    m = d.mean()
    var = ((d - m) ** 2).sum() / n
    for k in range(1, lag + 1):
        var += 2 * (1 - k / (lag + 1)) * ((d[k:] - m) * (d[:-k] - m)).sum() / n
    return float(m / np.sqrt(var / n)) if var > 0 else float("nan")


def compare(days: np.ndarray, dates_of_day: np.ndarray, y: np.ndarray, base: np.ndarray, cand: np.ndarray) -> dict[str, Any]:
    """基準 vs 候選（都是樣本外預測）：探索期／保留期的 DM 與 MSE 降幅（以日為單位）。"""
    e_a, e_b = y - base, y - cand
    ud, diff = day_loss_diff(days, e_a, e_b)
    out: dict[str, Any] = {}
    ok = np.isfinite(e_a) & np.isfinite(e_b)
    for name, test in (("explore", lambda dt: dt < SPLIT), ("holdout", lambda dt: dt >= SPLIT)):
        dsel = np.array([test(dates_of_day[x]) for x in ud])
        sel = ok & np.array([test(dates_of_day[x]) for x in days])
        if dsel.sum() < 30 or sel.sum() == 0:
            out[name] = {"days": int(dsel.sum()), "dm": None, "reduction": None, "mse_base": None, "mse_cand": None}
            continue
        mb, mc = float(np.mean(e_a[sel] ** 2)), float(np.mean(e_b[sel] ** 2))
        out[name] = {"days": int(dsel.sum()), "dm": dm(diff[dsel]), "reduction": 1 - mc / mb, "mse_base": mb, "mse_cand": mc}
    return out


def passes(r: dict[str, Any], need_positive_coef: Optional[bool] = None) -> bool:
    e, h = r["explore"], r["holdout"]
    if e["dm"] is None or h["dm"] is None or not np.isfinite(e["dm"]) or not np.isfinite(h["dm"]):
        return False
    ok = e["dm"] >= Z_EXPLORE and h["dm"] >= Z_HOLDOUT and e["reduction"] >= MIN_REDUCTION and h["reduction"] >= MIN_REDUCTION
    return bool(ok and (need_positive_coef is None or need_positive_coef))


def pick_simple(mse: dict[str, dict[str, Optional[float]]]) -> str:
    """last30 與 EWMA 兩個最簡單的基準裡，探索期樣本外 MSE 較小的那個（沒有數字的當作最差；同分取 last30）。"""
    def key(name: str) -> float:
        v = mse[name]["explore"]
        return float("inf") if v is None else v
    return "ewma" if key("ewma") < key("last30") else "last30"


def verdict_of(a_pass: bool, b_pass: bool) -> str:
    return "有優勢（T_A 與 T_B 都過關）" if a_pass and b_pass else "T_A 過關、T_B 沒過" if a_pass else "T_B 過關、T_A 沒過" if b_pass else "沒過"


def run(sessions: Sequence[Session], forecast: dict[str, np.ndarray]) -> dict[str, Any]:
    """forecast：{"p": 決策位置, "rv": Chronos 預測總和, "vol": 同}（與 export_inputs 的 p 對應）。"""
    s = series(sessions)
    dates = np.array([x.date for x in sessions])
    pos_of = {int(p): i for i, p in enumerate(forecast["p"])}
    out: dict[str, Any] = {"n_days": len(sessions), "split": SPLIT, "z_explore": Z_EXPLORE, "z_holdout": Z_HOLDOUT, "min_reduction": MIN_REDUCTION, "targets": {}, "verdict": {}}
    any_success = False
    for kind in TARGETS:
        f = features(kind, s["a"], s["v"])
        fc = np.array([forecast[kind][pos_of[int(p)]] if int(p) in pos_of else np.nan for p in f["p"]])
        f["chr"] = chronos_feature(kind, fc)
        cols = ("y", "har1", "har2", "har3", "har4", "ewma", "tod", "chr")
        ok = np.all([np.isfinite(f[c]) for c in cols], axis=0)
        n_ok = int(ok.sum())
        days = f["day"][ok]
        y = f["y"][ok]
        one = np.ones(n_ok)
        har = np.column_stack([one, f["har1"][ok], f["har2"][ok], f["har3"][ok], f["har4"][ok], f["tod"][ok]])
        X = {"last30": np.column_stack([one, f["har1"][ok]]), "ewma": np.column_stack([one, f["ewma"][ok]]), "har": har,
             "chronos": np.column_stack([one, f["chr"][ok]]), "har+chronos": np.column_stack([har, f["chr"][ok]])}
        pred = {k: oos_predict(days, y, x) for k, x in X.items()}
        t = {"n": n_ok, "n_days": int(len(np.unique(days))), "mse": {}}
        for name, pr in pred.items():
            e2 = (y - pr) ** 2
            for part, test in (("explore", lambda dt: dt < SPLIT), ("holdout", lambda dt: dt >= SPLIT)):
                sel = np.isfinite(e2) & np.array([test(dates[d]) for d in days])
                t["mse"].setdefault(name, {})[part] = float(e2[sel].mean()) if sel.any() else None
        simple = pick_simple(t["mse"])
        t["simple_baseline"] = simple
        t["T_A"] = compare(days, dates, y, pred[simple], pred["chronos"])
        t["T_B"] = compare(days, dates, y, pred["har"], pred["har+chronos"])
        t["chronos_vs_har"] = compare(days, dates, y, pred["har"], pred["chronos"])             # 只描述
        beta = np.linalg.lstsq(X["har+chronos"], y, rcond=None)[0]
        t["coef_chronos_in_har"] = float(beta[-1])
        ok_p = np.isfinite(pred["har"]) & np.isfinite(pred["har+chronos"])
        yearly = {}
        for yr in sorted({dates[d][:4] for d in days[ok_p]}):
            sel = ok_p & np.array([dates[d][:4] == yr for d in days])
            if sel.sum() >= 100:
                yearly[yr] = float(1 - np.mean((y[sel] - pred["har+chronos"][sel]) ** 2) / np.mean((y[sel] - pred["har"][sel]) ** 2))
        t["yearly_reduction_T_B"] = yearly
        t["corr_chronos_har"] = float(np.corrcoef(f["chr"][ok], har[:, 1:] @ np.linalg.lstsq(har[:, 1:], y, rcond=None)[0])[0, 1]) if n_ok > 10 else None
        a_pass, b_pass = passes(t["T_A"]), passes(t["T_B"], t["coef_chronos_in_har"] > 0)
        t["pass_T_A"], t["pass_T_B"] = a_pass, b_pass
        out["targets"][kind] = t
        out["verdict"][kind] = verdict_of(a_pass, b_pass)
        any_success = any_success or (a_pass and b_pass)
    out["experiment_1_has_edge"] = bool(any_success)
    return out


def format_report(r: dict[str, Any]) -> str:
    def f(x: Optional[float], spec: str) -> str:
        return "—" if x is None or not np.isfinite(x) else format(x, spec)
    L = [f"實驗 1：Chronos（零樣本）預測之後 30 分鐘的波動與成交量  日盤 {r['n_days']} 個（探索期 < {r['split']}，保留期 ≥ {r['split']}）",
         f"過關：探索 DM ≥ {r['z_explore']}、保留 DM ≥ {r['z_holdout']}、兩段 MSE 降幅各 ≥ {r['min_reduction']:.0%}；T_B 還要 Chronos 係數 > 0。有優勢＝同一個目標的 T_A 與 T_B 都過關\n"]
    for kind, t in r["targets"].items():
        L.append(f"【{'已實現波動' if kind == 'rv' else '成交量'}】樣本 {t['n']}（{t['n_days']} 天）→ {r['verdict'][kind]}")
        for key, label in (("T_A", f"T_A  Chronos vs {t['simple_baseline']}"), ("T_B", "T_B  HAR＋Chronos vs HAR"), ("chronos_vs_har", "（描述）Chronos vs HAR")):
            c = t[key]
            L.append(f"  {label:<28} 探索 DM {f(c['explore']['dm'], '.2f')} 降幅 {f(c['explore']['reduction'], '.1%')}　保留 DM {f(c['holdout']['dm'], '.2f')} 降幅 {f(c['holdout']['reduction'], '.1%')}")
        L.append("  樣本外 MSE（探索／保留）：" + "　".join(f"{k} {f(v['explore'], '.4f')}／{f(v['holdout'], '.4f')}" for k, v in t["mse"].items()))
        L.append(f"  Chronos 特徵在 HAR 裡的係數 {f(t['coef_chronos_in_har'], '.3f')}；與 HAR 預測的相關 {f(t['corr_chronos_har'], '.2f')}；T_B 每年降幅：" + "、".join(f"{y} {v:.1%}" for y, v in t["yearly_reduction_T_B"].items()))
    L.append(f"\n結論：{'有優勢 → 可以討論實驗 2' if r['experiment_1_has_edge'] else '沒有任何目標兩個都過關 → 照文件：停（不做實驗 2、3）'}")
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] not in ("export", "run"):
        print(__doc__)
        return 2
    from core.intraday_context import load_sessions
    from core.market_store import MarketStore
    sessions = load_sessions(MarketStore(), "TXF")
    if a[0] == "export":
        print(f"匯出 {len(sessions)} 個日盤、{export_inputs(sessions)} 個決策位置 → {INPUT_PATH}")
        return 0
    if RESULT_PATH.exists() and "--rerun" not in a:
        print("（已經跑過，只跑一次；以下是存的結果）\n")
        print(format_report(json.loads(RESULT_PATH.read_text(encoding="utf-8"))))
        return 0
    z = np.load(FORECAST_PATH)
    if int(z["horizon"]) != H or int(z["context"]) != CONTEXT:
        print("預測檔的脈絡長度或預測長度與登記的不同，不跑", file=sys.stderr)
        return 2
    if list(z["dates"]) != [s.date for s in sessions]:
        print("預測檔的日期與現在的日盤資料不一致，請重新匯出與預測", file=sys.stderr)
        return 2
    res = run(sessions, {"p": z["p"], "rv": z["rv"], "vol": z["vol"]})
    RESULT_PATH.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(format_report(res))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
