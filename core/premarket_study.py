"""盤前跨市場資訊的歷史預檢（update.md 發現 31 A；判定標準在跑之前就寫在 update.md，只跑一次）。

問題：費半（SOX）、台積電 ADR（TSM）、VIX 有沒有在「台指期夜盤」之外，多提供關於**日盤波動**的資訊？
資料：期交所 TX 日行情（data/taifex/tx/，2017-05-16 起有盤後列；盤後列的交易日期＝它之後那個日盤的交易日）＋ Nasdaq（TSM、SOX）＋ Cboe（VIX）的日線（data/premarket/，python -m core.premarket_study fetch 下載）。不需要連券商。
美股對齊、特徵的算法**直接用** core/premarket_log 的函式（compute_us_feature、latest_us_session），所以歷史預檢與前向紀錄是同一套定義。

目標 y＝ln((H−L) ÷ R20)；基準 M0：y ~ 1 ＋ a ＋ p（a＝夜盤移動幅度、p＝前一個樣本日的 y）；候選 s、t、v（費半、TSM 的報酬幅度、VIX 相對近 20 日平均）。
擴展視窗 OLS 的樣本外預測，巢狀模型用 Clark–West 比較（Newey–West 落後 10 期、單尾）；4 個檢定（M1_all、M1_s、M1_t、M1_v 各對 M0）Bonferroni：z ≥ 2.24 才宣稱。
方向（目標＝(C−O)÷R20）只當描述、不下結論、不計入 Bonferroni。這個預檢只決定要不要繼續記錄、優先等哪個特徵，不決定上線。

用法：
    python -m core.premarket_study fetch      # 下載 TSM、SOX（Nasdaq）、VIX（Cboe）日線到 data/premarket/（請求間隔 1.5 秒）
    python -m core.premarket_study sanity     # 只看資料處理有沒有對（樣本數、對齊、缺幾天），不算任何模型
    python -m core.premarket_study run [--json out.json] [--no-graveyard]
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from bisect import bisect_right
from datetime import date, datetime, time as dtime
from pathlib import Path
from statistics import NormalDist
from typing import Any, NamedTuple, Optional, Sequence

import numpy as np

import core.premarket_log as pm
from core.oi_study import DEFAULT_DIR, TX_HEADER, _iso, _num, _rows

STUDY_T = dtime(8, 30)                    # 預登記：取樣時間 08:30，與環境變數 PREMARKET_TIME 無關
INIT_WINDOW = 500
NW_LAG = 10
R_DAYS = 20
N_TESTS = 4
Z_CLAIM = NormalDist().inv_cdf(1 - 0.05 / N_TESTS)        # 單尾 α＝0.05÷4 ≈ 2.24
Z_HINT = NormalDist().inv_cdf(0.95)                       # 單尾 5%
Z_POWER = NormalDist().inv_cdf(0.8)
GAP_CLIP, RET_CLIP = 3.0, 4.0                             # 方向描述用：夜盤缺口（÷R20）、美股報酬（÷σ20）的截尾
FIRST_NIGHT = date(2017, 5, 16)
SERIES = ("sox", "tsm", "vix")
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "premarket"
RANGES = {f.key: f.default_range for f in pm.FEATURES}    # 固定的異常值範圍（不讀環境變數）
STUDY_NAME = "premarket_hist_precheck_v1"
MODELS = {"M1_all": ("s", "t", "v"), "M1_s": ("s",), "M1_t": ("t",), "M1_v": ("v",)}
MODEL_TEXT = {"M1_all": "費半＋TSM＋VIX 一起", "M1_s": "只加費半", "M1_t": "只加 TSM", "M1_v": "只加 VIX"}


# ── 資料 ──────────────────────────────────────────────────────────

class Day(NamedTuple):
    date: date
    contract: str
    o: float
    h: float
    l: float
    c: float
    night: float            # 夜盤收盤（盤後列的收盤價）
    prev_close: float       # 前一個交易日日盤收盤（同一個合約）
    volume: float


def load_tx_days(tx_dir: Path, first: date = FIRST_NIGHT) -> list[Day]:
    """TX 日行情 → 每個台股交易日一筆。合約的選法（預登記）：D 有盤後列與一般列（成交量 > 0）、前一個交易日也有一般列，取 D 日盤成交量最大者。"""
    day_rows: dict[str, dict[str, tuple[float, float, float, float, float]]] = {}      # 日期 → 合約 → (o, h, l, c, vol)
    night_rows: dict[str, dict[str, float]] = {}                                      # 日期 → 合約 → 盤後收盤
    for f in sorted(tx_dir.glob("*.csv")):
        for r in _rows(f, TX_HEADER):
            code = (r.get("到期月份(週別)") or "").strip()
            if not code.isdigit() or len(code) != 6:
                continue
            vol, c = _num(r.get("成交量", "")), _num(r.get("收盤價", ""))
            if not vol or vol <= 0 or c is None:
                continue
            d = _iso(r["交易日期"])
            if (r.get("交易時段") or "一般").strip() in ("一般", ""):
                o, h, l = _num(r.get("開盤價", "")), _num(r.get("最高價", "")), _num(r.get("最低價", ""))
                if None not in (o, h, l):
                    day_rows.setdefault(d, {})[code] = (o, h, l, c, vol)                # type: ignore[assignment]
            elif r["交易時段"].strip() == "盤後":
                night_rows.setdefault(d, {})[code] = c
    dates = sorted(day_rows)
    out: list[Day] = []
    for i in range(1, len(dates)):
        d, prev = dates[i], dates[i - 1]
        if date.fromisoformat(d) < first:
            continue
        cands = [c for c in day_rows[d] if c in night_rows.get(d, {}) and c in day_rows[prev]]
        if not cands:
            continue
        c = max(cands, key=lambda k: day_rows[d][k][4])
        o, h, l, cl, vol = day_rows[d][c]
        out.append(Day(date.fromisoformat(d), c, o, h, l, cl, night_rows[d][c], day_rows[prev][c][3], vol))
    return out


def load_series(path: Path) -> list[pm.Bar]:
    """data/premarket/{key}.csv（date,close）→ 日線，依日期排序。"""
    with path.open(encoding="utf-8", newline="") as fh:
        return sorted(pm.Bar(date.fromisoformat(r["date"]), float(r["close"]), path.stem) for r in csv.DictReader(fh))


def fetch(directory: Path = DATA_DIR, since: date = date(2016, 12, 1), http=pm.http_get, pause: float = 1.5) -> dict[str, int]:
    """TSM、SOX（Nasdaq）、VIX（Cboe）日線存成 CSV；每個來源一次請求、間隔 pause 秒；失敗的不存、回報。"""
    directory.mkdir(parents=True, exist_ok=True)
    got: dict[str, int] = {}
    for key, fn in (("tsm", lambda: pm.fetch_nasdaq("TSM", "stocks", since, http)), ("sox", lambda: pm.fetch_nasdaq("SOX", "index", since, http)),
                    ("vix", lambda: pm.fetch_cboe_vix(since, http))):
        try:
            bars = fn()
            tmp = directory / f"{key}.csv.part"
            tmp.write_text("date,close\n" + "".join(f"{b.date.isoformat()},{b.close}\n" for b in bars), encoding="utf-8")
            tmp.replace(directory / f"{key}.csv")
            got[key] = len(bars)
        except pm.SourceError as e:
            got[key] = -1
            print(f"下載失敗 {key}：{e}", file=sys.stderr)
        time.sleep(pause)
    return got


# ── 資料集 ─────────────────────────────────────────────────────────

class Dataset(NamedTuple):
    dates: list[date]
    cols: dict[str, np.ndarray]          # y a p s t v（波動）；dirt gap pdir rs rt（方向，描述用）；NaN＝缺
    info: dict[str, Any]


def _window(bars: Sequence[pm.Bar], dates: Sequence[date], session: date, before: int = 45, after: int = 3) -> list[pm.Bar]:
    """session 附近的日線（前 before 筆到後 after 筆）：compute_us_feature 只需要這些，全序列太慢。"""
    i = bisect_right(dates, session)
    return list(bars[max(0, i - before): i + after])


def build_dataset(days: Sequence[Day], series: dict[str, list[pm.Bar]]) -> Dataset:
    """每個樣本日的目標與特徵。第 R_DAYS 天起才有 R20、再一天起才有 p；美股特徵走 premarket_log 的同一套函式。"""
    n = len(days)
    rng_pts = np.array([d.h - d.l for d in days], dtype=float)
    keys = ("y", "a", "p", "s", "t", "v", "dirt", "gap", "pdir", "rs", "rt")
    cols = {k: np.full(n, np.nan) for k in keys}
    R = np.full(n, np.nan)
    for i in range(R_DAYS, n):
        R[i] = rng_pts[i - R_DAYS: i].mean()
    ok = (R > 0) & (rng_pts > 0)
    cols["y"][ok] = np.log(rng_pts[ok] / R[ok])
    cols["a"][ok] = np.log1p(np.abs(np.array([d.night - d.prev_close for d in days]))[ok] / R[ok])
    cols["dirt"][ok] = (np.array([d.c - d.o for d in days]))[ok] / R[ok]
    cols["gap"][ok] = np.clip((np.array([d.night - d.prev_close for d in days]))[ok] / R[ok], -GAP_CLIP, GAP_CLIP)
    cols["p"][1:] = cols["y"][:-1]
    cols["pdir"][1:] = cols["dirt"][:-1]
    sdates = {k: [b.date for b in v] for k, v in series.items()}
    info: dict[str, Any] = {"days": n, "us_closed": 0, "us_stale": 0, "flagged": {k: 0 for k in SERIES}, "missing": {k: 0 for k in SERIES}}
    for i, d in enumerate(days):
        t_local = datetime.combine(d.date, STUDY_T, tzinfo=pm.TPE)
        w = pm.latest_us_session(t_local)
        if pm.us_session_info(w)[0] == "closed":
            info["us_closed"] += 1
            continue
        for f in pm.FEATURES:
            out = pm.compute_us_feature(f, _window(series[f.key], sdates[f.key], w), w, t_local.astimezone(pm.UTC), ranges=RANGES)
            if out["value"] is None:
                info["missing"][f.key] += 1
                info["flagged"][f.key] += out["flag"] == "out_of_range"
                info["us_stale"] += (out["missing_reason"] or "").startswith(("stale", "insufficient"))
                continue
            det = out["detail"]
            if f.key == "vix":
                cols["v"][i] = det["rel20"]
            elif det["sigma20"] > 0:
                cols["s" if f.key == "sox" else "t"][i] = math.log1p(abs(det["ret"]) / det["sigma20"])
                cols["rs" if f.key == "sox" else "rt"][i] = float(np.clip(det["ret"] / det["sigma20"], -RET_CLIP, RET_CLIP))
    return Dataset([d.date for d in days], cols, info)


# ── 統計 ──────────────────────────────────────────────────────────

def nw_se_mean(x: np.ndarray, lag: int = NW_LAG) -> float:
    """平均數的 Newey–West（Bartlett）標準誤：sqrt(長期變異 ÷ n)。"""
    n = len(x)
    e = x - x.mean()
    lrv = float(e @ e) / n
    for l in range(1, min(lag, n - 1) + 1):
        lrv += 2 * (1 - l / (lag + 1)) * float(e[l:] @ e[:-l]) / n
    return math.sqrt(max(lrv, 0.0) / n)


def oos_predict(y: np.ndarray, X: np.ndarray, init: int) -> np.ndarray:
    """擴展視窗 OLS：第 i 天用 [0, i) 的資料估計、預測第 i 天（i ≥ init）。只用過去。"""
    n = len(y)
    out = np.empty(n - init)
    for i in range(init, n):
        beta, *_ = np.linalg.lstsq(X[:i], y[:i], rcond=None)
        out[i - init] = X[i] @ beta
    return out


def clark_west(y: np.ndarray, p0: np.ndarray, p1: np.ndarray, lag: int = NW_LAG, years: Optional[np.ndarray] = None) -> dict[str, Any]:
    """Clark–West（2007）：f ＝ e0² − (e1² − (p0 − p1)²)，H1：mean(f) > 0（大模型較好）。z＝mean(f) ÷ NW 標準誤（單尾）。"""
    e0, e1 = y - p0, y - p1
    f = e0 ** 2 - (e1 ** 2 - (p0 - p1) ** 2)
    se = nw_se_mean(f, lag)
    mspe0, mspe1 = float((e0 ** 2).mean()), float((e1 ** 2).mean())
    z = float(f.mean() / se) if se > 0 else float("nan")
    out = {"n": int(len(y)), "mean_f": float(f.mean()), "se": se, "z": z, "mspe0": mspe0, "mspe1": mspe1, "improve_pct": 1 - mspe1 / mspe0,
           "adj_improve_pct": float(f.mean()) / mspe0, "mde_pct": (Z_CLAIM + Z_POWER) * se / mspe0, "p_one_sided": 1 - NormalDist().cdf(z) if not math.isnan(z) else float("nan")}
    if years is not None:
        ys = sorted(set(int(v) for v in years))
        per = {str(v): float(f[years == v].mean()) for v in ys}
        out["by_year"] = per
        out["years_positive"] = sum(1 for v in per.values() if v > 0)
    return out


def ols_hac(y: np.ndarray, X: np.ndarray, lag: int = NW_LAG) -> dict[str, Any]:
    """全樣本 OLS（描述用）：係數與 Newey–West 的 t 值。"""
    n, k = X.shape
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    u = y - X @ beta
    xtx_inv = np.linalg.inv(X.T @ X)
    S = (X * u[:, None]).T @ (X * u[:, None])
    for l in range(1, lag + 1):
        w = 1 - l / (lag + 1)
        G = (X[l:] * u[l:, None]).T @ (X[:-l] * u[:-l, None])
        S += w * (G + G.T)
    cov = xtx_inv @ S @ xtx_inv
    se = np.sqrt(np.diag(cov))
    return {"beta": beta.tolist(), "t": (beta / se).tolist(), "r2": float(1 - (u @ u) / ((y - y.mean()) @ (y - y.mean())))}


def verdict(z: float) -> str:
    if math.isnan(z):
        return "無法檢定"
    return "過關" if z >= Z_CLAIM else ("線索" if z >= Z_HINT else "沒有")


def _design(cols: dict[str, np.ndarray], base: Sequence[str], extra: Sequence[str]) -> np.ndarray:
    n = len(cols["y"])
    return np.column_stack([np.ones(n)] + [cols[k] for k in list(base) + list(extra)])


def compare(cols: dict[str, np.ndarray], dates: Sequence[date], target: str, base: Sequence[str], feats: dict[str, Sequence[str]], init: int = INIT_WINDOW) -> dict[str, Any]:
    """對一個目標：基準 vs 各個加了特徵的模型，在同一批日子上做樣本外比較。cols 已經是『所有欄位都不缺』的那批日子。"""
    y = cols[target]
    n = len(y)
    if n <= init + 30:
        raise ValueError(f"可用的日子只有 {n}，不夠做樣本外（第一個訓練窗 {init}）")
    p0 = oos_predict(y, _design(cols, base, ()), init)
    years = np.array([d.year for d in dates[init:]])
    out: dict[str, Any] = {"n": n, "n_oos": n - init, "first_oos": dates[init].isoformat(), "last": dates[-1].isoformat(), "mspe0": float(((y[init:] - p0) ** 2).mean()), "models": {}}
    for name, extra in feats.items():
        p1 = oos_predict(y, _design(cols, base, extra), init)
        cw = clark_west(y[init:], p0, p1, NW_LAG, years)
        cw["features"] = list(extra)
        out["models"][name] = cw
    return out


def run_study(ds: Dataset, init: int = INIT_WINDOW) -> dict[str, Any]:
    """預登記的全部分析：波動（4 個檢定）＋描述（全樣本係數、方向對照）。"""
    c = ds.cols
    vol_need = ("y", "a", "p", "s", "t", "v")
    keep = np.all([np.isfinite(c[k]) for k in vol_need], axis=0)
    sub = {k: v[keep] for k, v in c.items()}
    dates = [d for d, k in zip(ds.dates, keep) if k]
    res: dict[str, Any] = {"info": {**ds.info, "usable": int(keep.sum())}, "vol": compare(sub, dates, "y", ("a", "p"), MODELS, init)}
    for name, m in res["vol"]["models"].items():
        m["verdict"] = verdict(m["z"])
    res["descriptive"] = {"full_sample_ols": ols_hac(sub["y"], _design(sub, ("a", "p"), ("s", "t", "v"))),
                          "corr": {k: float(np.corrcoef(sub[k], sub["a"])[0, 1]) for k in ("s", "t", "v", "p")}}
    dir_need = ("dirt", "gap", "pdir", "rs", "rt", "v")
    kd = np.all([np.isfinite(c[k]) for k in dir_need], axis=0)
    subd = {k: v[kd] for k, v in c.items()}
    datesd = [d for d, k in zip(ds.dates, kd) if k]
    res["direction"] = compare({**subd, "y": subd["dirt"]}, datesd, "y", ("gap", "pdir"), {"M1_all": ("rs", "rt", "v"), "M1_s": ("rs",), "M1_t": ("rt",), "M1_v": ("v",)}, init)
    res["direction"]["note"] = "只當描述：沒有判定、不計入 Bonferroni、不下結論"
    return res


def graveyard_rows(res: dict[str, Any]) -> list[dict[str, Any]]:
    """這次試過的每一個模型（含沒通過的、含方向的描述）→ 墳場紀錄。"""
    rows = []
    for family, key in (("vol", "vol"), ("dir_desc", "direction")):
        blk = res[key]
        for name, m in blk["models"].items():
            rows.append({"family": family, "feature_set": f"M0+{','.join(m['features'])}", "target": "ln((H-L)/R20)" if family == "vol" else "(C-O)/R20",
                         "params": {"init": INIT_WINDOW, "nw_lag": NW_LAG, "model": name, "first_oos": blk["first_oos"], "last": blk["last"]}, "n": blk["n_oos"],
                         "stat": {k: m[k] for k in ("z", "improve_pct", "adj_improve_pct", "mde_pct", "years_positive")}, "verdict": m.get("verdict", "描述"), "note": ""})
    return rows


# ── 報告 ──────────────────────────────────────────────────────────

def _pct(x: float, d: int = 2) -> str:
    return f"{x * 100:+.{d}f}%"


def format_report(res: dict[str, Any]) -> str:
    v, info = res["vol"], res["info"]
    L = ["盤前跨市場資訊的歷史預檢：台指期日盤波動（目標 ln((H−L)÷R20)）",
         f"樣本日 {info['days']} 天 → 美股特徵都有的 {info['usable']} 天（美股休市 {info['us_closed']}、其餘缺：費半 {info['missing']['sox']}／TSM {info['missing']['tsm']}／VIX {info['missing']['vix']}；"
         f"超出異常值範圍 {info['flagged']['sox']}／{info['flagged']['tsm']}／{info['flagged']['vix']}）",
         f"樣本外 {v['n_oos']} 天（{v['first_oos']} ~ {v['last']}；第一個訓練窗 {INIT_WINDOW} 天、擴展視窗）；基準 M0（a ＋ p）的 MSPE ＝ {v['mspe0']:.4f}", "",
         f"4 個事先登記的檢定（Clark–West，單尾；z ≥ {Z_CLAIM:.2f} 過關、≥ {Z_HINT:.3f} 線索；MDE＝80% 力道看得出來的最小 MSPE 改善，占基準 MSPE）"]
    for name, m in v["models"].items():
        L.append(f"  {name:<7}{MODEL_TEXT[name]:<12} MSPE 改善 {_pct(m['improve_pct'])}（調整後 {_pct(m['adj_improve_pct'])}）  z={m['z']:+.2f}  MDE {_pct(m['mde_pct'], 1)}  "
                 f"{m['years_positive']}／{len(m['by_year'])} 年為正  → {m['verdict']}")
    d = res["descriptive"]
    names = ["常數", "a 夜盤幅度", "p 前日波動", "s 費半", "t TSM", "v VIX"]
    L += ["", "（描述）全樣本 OLS 係數與 Newey–West t 值：" + "  ".join(f"{n} {b:+.3f}({t:+.1f})" for n, b, t in zip(names, d["full_sample_ols"]["beta"], d["full_sample_ols"]["t"])) + f"　R²＝{d['full_sample_ols']['r2']:.3f}",
          "（描述）與夜盤幅度 a 的相關：" + "　".join(f"{k} {c:+.2f}" for k, c in d["corr"].items())]
    dr = res["direction"]
    L += ["", f"（描述，不判定）方向：目標 (C−O)÷R20，樣本外 {dr['n_oos']} 天；基準＝有正負號的夜盤缺口＋前一日同一目標。Clark–West z（MSPE 改善）："]
    L.append("  " + "　".join(f"{k} z={m['z']:+.2f}（{_pct(m['improve_pct'])}）" for k, m in dr["models"].items()))
    L += ["", "沒過關不等於證明沒有；預檢只決定要不要繼續記錄、優先等哪個特徵，不決定上線。"]
    return "\n".join(L)


def sanity(days: Sequence[Day], ds: Dataset) -> str:
    """只看資料處理有沒有對：樣本日數、合約、美股對齊的缺值；不算任何模型。"""
    info = ds.info
    c = ds.cols
    fin = {k: int(np.isfinite(c[k]).sum()) for k in ("y", "a", "p", "s", "t", "v")}
    contracts = sorted({d.contract for d in days})
    return "\n".join([f"TX 樣本日 {len(days)} 天（{days[0].date} ~ {days[-1].date}）、{len(contracts)} 個不同合約；夜盤收盤與前收的差（點）中位數 {np.median([abs(d.night - d.prev_close) for d in days]):.0f}",
                      f"欄位有值的天數：{fin}",
                      f"美股休市 {info['us_closed']} 天、資料過期或不足 {info['us_stale']} 天；缺值（費半／TSM／VIX）{info['missing']['sox']}／{info['missing']['tsm']}／{info['missing']['vix']}；"
                      f"超出異常值範圍 {info['flagged']['sox']}／{info['flagged']['tsm']}／{info['flagged']['vix']}"])


def _load_all(tx_dir: Path, data_dir: Path) -> tuple[list[Day], Dataset]:
    series = {k: load_series(data_dir / f"{k}.csv") for k in SERIES}
    days = load_tx_days(tx_dir)
    return days, build_dataset(days, series)


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    ap = argparse.ArgumentParser(prog="python -m core.premarket_study", description="盤前跨市場資訊的歷史預檢")
    ap.add_argument("what", choices=("fetch", "sanity", "run"))
    ap.add_argument("--tx-dir", default=str(DEFAULT_DIR / "tx"))
    ap.add_argument("--data-dir", default=str(DATA_DIR))
    ap.add_argument("--json", help="把完整結果寫成 JSON")
    ap.add_argument("--no-graveyard", action="store_true")
    a = ap.parse_args(argv)
    if a.what == "fetch":
        got = fetch(Path(a.data_dir))
        print(f"下載：{got} → {a.data_dir}")
        return 0 if all(v > 0 for v in got.values()) else 1
    days, ds = _load_all(Path(a.tx_dir), Path(a.data_dir))
    if a.what == "sanity":
        print(sanity(days, ds))
        return 0
    res = run_study(ds)
    print(format_report(res))
    if a.json:
        Path(a.json).write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    if not a.no_graveyard:
        store = pm.default_store()
        for r in graveyard_rows(res):
            store.add_graveyard(STUDY_NAME, **r)
        print(f"墳場紀錄已寫入 {len(graveyard_rows(res))} 筆（premarket_graveyard）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
