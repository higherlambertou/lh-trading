"""選擇權隱含波動率（IV）與之後的實際波動（RV）的檢驗（update.md 發現 27 ⑥）。

要驗證的是《選擇權波段策略》文件的前提：「IV 低 → 買跨式（之後的波動會比選擇權定價的大）」「IV 高 → 賣權利金（之後的波動會比定價的小）」。
不模擬損益（買賣價差、滑價、保證金的假設太多），只問前提成不成立：之後實際發生的波動，相對於 IV 的高低有沒有系統性的差異。

資料：期交所〈選擇權每日交易行情〉（免費下載，一次一個月）。`fetch` 每個月下載一次、立刻**只留每天一筆的 ATM IV**（原始檔每個月 3~11 MB，不留）：
  - IV 的定義和系統即時算的一樣（core/iv_monitor）：最近的月選擇權、距到期 ≥ 7 天、平價 Call／Put 用買賣權平價取遠期、Black-76、r＝0；
    價格用**結算價**（交易所每天算出的），只用一般交易時段的月合約（不含週選）。
  - RV：同一個選擇權從該日到到期日之間，TX 日線收盤對收盤報酬（core/trend_study 的連續報酬，展期不跳空）的年化標準差（%）；至少 4 個報酬。
  - VRP ＝ IV − RV（波動點）。IV 百分位：只用該日**以前**最近 250 筆 IV 的排名（同 iv_monitor；≥ 250 筆才有）；低＝≤ 20%、高＝≥ 80%。
3 個事先固定的檢定（兩尾，Bonferroni |t| ≥ 2.39 才宣稱，|t| ≥ 2 只算線索）。每天的 RV 視窗都延伸到同一個到期日（巢狀、大幅重疊），
同一個到期月內的各天高度相依，所以 t 值的標準誤用**以到期月為群組的穩健標準誤**（群組之間才獨立）：
  T1 全部日子 VRP 的平均 ≠ 0；T2 IV 百分位低的日子 RV − IV 的平均 ≠ 0（文件說應該 > 0）；T3 IV 百分位高的日子 IV − RV 的平均 ≠ 0（文件說應該 > 0）。
描述（不檢定）：各組平均 IV、RV、VRP、RV > IV 的比例，以及每個到期月只取一筆（距到期約 30 天）的版本。

用法：
    python -m core.vrp_study fetch [--start-year 2016]      # 補齊缺的月（每次請求間隔 1 秒；已有的不重抓，當月每次重抓）
    python -m core.vrp_study run [--json out.json]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import NormalDist
from typing import Any, Callable, Optional

import numpy as np

from core.iv_monitor import IV_SANE_RANGE, atm_iv_from_quotes, expiry_of
from core.oi_study import DEFAULT_DIR, _iso, _num
from core.trend_study import build_series, load_tx_contracts

OPT_URL = "https://www.taifex.com.tw/cht/3/optDataDown"
OPT_HEADER = "交易日期,契約,到期月份(週別)"
START_YEAR = 2016
MIN_DAYS = 7.0                    # 距到期至少幾天（避開到期週的 gamma 扭曲，同 iv_monitor）
PCT_WINDOW = 250
LOW_PCT, HIGH_PCT = 20.0, 80.0
MIN_RETURNS = 4
N_TESTS = 3
Z_CLAIM = NormalDist().inv_cdf(1 - 0.05 / N_TESTS / 2)       # ≈ 2.39
Z_HINT = 2.0
TRADING_DAYS = 252
IV_FIELDS = ("date", "expiry", "expiry_date", "T_days", "F", "K", "iv")


def verdict(t: float) -> str:
    return "過關" if abs(t) >= Z_CLAIM else ("線索" if abs(t) >= Z_HINT else "沒有")


# ── 從原始行情算每天一筆的 ATM IV ───────────────────────────────────

def _expiry_dt(month: str, raw: str) -> datetime:
    """契約到期日：行情檔有『契約到期日』就用它（遇假日順延的情形才準），沒有就是第三個週三 13:30。"""
    for fmt in ("%Y%m%d", "%Y/%m/%d"):                                                # 真實檔案是 20260223；斜線格式是早期寫的假設
        try:
            return datetime.strptime(raw.strip(), fmt).replace(hour=13, minute=30)
        except ValueError:
            pass
    return expiry_of(month)


def reduce_month(text: str) -> list[dict[str, Any]]:
    """一個月的選擇權行情（CSV 文字）→ 每個交易日一筆：{date, expiry, expiry_date, T_days, F, K, iv}。"""
    by_day: dict[str, dict[str, dict[float, dict[str, float]]]] = {}
    exp_dt: dict[tuple[str, str], datetime] = {}
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        month = (r.get("到期月份(週別)") or "").strip()
        if (r.get("契約") or "").strip() != "TXO" or (r.get("交易時段") or "一般").strip() not in ("一般", "") or not re.fullmatch(r"\d{6}", month):
            continue                                                                  # 只要月選擇權、一般交易時段（週選與盤後不算）
        price, strike = _num(r.get("結算價", "")), _num(r.get("履約價", ""))
        side = {"買權": "C", "賣權": "P"}.get((r.get("買賣權") or "").strip())
        if not price or price <= 0 or not strike or side is None:
            continue
        day = _iso(r["交易日期"])
        by_day.setdefault(day, {}).setdefault(month, {}).setdefault(strike, {})[side] = price
        exp_dt.setdefault((day, month), _expiry_dt(month, r.get("契約到期日") or ""))
    out = []
    for day, months in sorted(by_day.items()):
        now = datetime.strptime(day, "%Y-%m-%d").replace(hour=13, minute=45)
        usable = [m for m in sorted(months) if (exp_dt[(day, m)] - now).total_seconds() >= MIN_DAYS * 86400]
        if not usable:
            continue
        month = usable[0]
        pairs = {k: v for k, v in months[month].items() if "C" in v and "P" in v}
        if len(pairs) < 3:
            continue
        T = (exp_dt[(day, month)] - now).total_seconds() / (365 * 86400)
        k0 = min(pairs, key=lambda k: abs(pairs[k]["C"] - pairs[k]["P"]))             # 平價：C−P 最小的履約價 ≈ 遠期價
        f0 = k0 + pairs[k0]["C"] - pairs[k0]["P"]
        k_atm = min(pairs, key=lambda k: abs(k - f0))
        res = atm_iv_from_quotes(pairs[k_atm]["C"], pairs[k_atm]["P"], k_atm, T, 0.0)
        if res is None or not (IV_SANE_RANGE[0] <= res[0] <= IV_SANE_RANGE[1]):
            continue
        out.append({"date": day, "expiry": month, "expiry_date": exp_dt[(day, month)].date().isoformat(),
                    "T_days": round(T * 365, 2), "F": res[1], "K": k_atm, "iv": res[0]})
    return out


# ── 下載與讀取 ────────────────────────────────────────────────────

def _months(start_year: int, today: date) -> list[tuple[str, date, date]]:
    out = []
    for y in range(start_year, today.year + 1):
        for m in range(1, 13):
            a = date(y, m, 1)
            if a > today:
                break
            b = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
            out.append((f"iv_{y}_{m:02d}.csv", a, min(b, today)))
    return out


def fetch(directory: Path = DEFAULT_DIR, start_year: int = START_YEAR, today: Optional[date] = None, pause: float = 1.0,
          opener: Callable[..., Any] = urllib.request.urlopen) -> dict[str, int]:
    """每個月下載一次原始行情、算好每天一筆的 IV 就存成小檔（iv_YYYY_MM.csv）；原始檔不留。已經有而且那個月已結束的不重抓。"""
    today = today or date.today()
    out = directory / "vrp"
    out.mkdir(parents=True, exist_ok=True)
    got = {"downloaded": 0, "skipped": 0, "failed": 0}
    for name, a, b in _months(start_year, today):
        f = out / name
        if f.exists() and f.stat().st_size > 0 and b < today:
            got["skipped"] += 1
            continue
        body = urllib.parse.urlencode({"down_type": "1", "commodity_id": "TXO", "commodity_id2": "",
                                       "queryStartDate": f"{a:%Y/%m/%d}", "queryEndDate": f"{b:%Y/%m/%d}"}).encode()
        try:
            with opener(urllib.request.Request(OPT_URL, data=body), timeout=120) as resp:
                text = resp.read().decode("cp950", errors="replace")
            if not text.lstrip("﻿").startswith(OPT_HEADER):
                raise ValueError("不是預期的 CSV（可能是錯誤頁）")
            rows = reduce_month(text)
            if not rows:
                raise ValueError("這個月算不出任何一天的 IV")
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=IV_FIELDS, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
            f.write_text(buf.getvalue(), encoding="utf-8")
            got["downloaded"] += 1
        except Exception as e:                                                        # noqa: BLE001
            got["failed"] += 1
            print(f"下載失敗 {name}：{e}", file=sys.stderr)
        time.sleep(pause)
    return got


def load_iv(directory: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for f in sorted(directory.glob("iv_*.csv")):
        with f.open(encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                out[r["date"]] = {"expiry": r["expiry"], "expiry_date": r["expiry_date"], "T_days": float(r["T_days"]),
                                  "F": float(r["F"]), "K": float(r["K"]), "iv": float(r["iv"])}
    return dict(sorted(out.items()))


# ── 資料集 ────────────────────────────────────────────────────────

def build_dataset(iv: dict[str, dict[str, Any]], contracts: dict[str, dict[str, tuple[float, float]]]) -> dict[str, np.ndarray]:
    """每個有 IV 的日子 t：RV＝t 之後到到期日（含）的 TX 收盤對收盤報酬的年化標準差；IV 百分位只用 t 以前最近 250 筆。"""
    series = build_series(contracts)
    dates, r, keep = series["dates"], series["r_close"], series["keep"]
    pos = {d: i for i, d in enumerate(dates)}
    rec = []
    for d, v in iv.items():
        i = pos.get(d)
        if i is None:
            continue
        ks = [k for k in range(i, len(r)) if dates[k + 1] <= v["expiry_date"]]
        rets = np.array([r[k] for k in ks if keep[k]])
        if len(rets) < MIN_RETURNS:
            continue
        rv = math.sqrt(float(np.sum(rets ** 2)) / len(rets) * TRADING_DAYS) * 100.0
        rec.append((d, v["iv"], rv, v["expiry"], v["T_days"]))
    ivs_all = [x["iv"] for x in iv.values()]
    day_idx = {d: j for j, d in enumerate(iv)}
    pct = []
    for d, ivv, *_ in rec:
        j = day_idx[d]
        past = ivs_all[j - PCT_WINDOW:j] if j >= PCT_WINDOW else None
        pct.append(float(np.sum(np.array(past) < ivv) / len(past) * 100.0) if past else np.nan)
    return {"dates": np.array([x[0] for x in rec]), "iv": np.array([x[1] for x in rec]), "rv": np.array([x[2] for x in rec]),
            "expiry": np.array([x[3] for x in rec]), "T_days": np.array([x[4] for x in rec]), "pct": np.array(pct)}


# ── 統計 ──────────────────────────────────────────────────────────

def mean_t(y: np.ndarray, clusters: np.ndarray) -> dict[str, float]:
    """平均數的 t 值，標準誤用群組穩健（以到期月為群組）：Var(mean) ＝ G/(G−1) · Σ_c (Σ_{i∈c}(y_i − m))² / n²。
    同一個群組內可以任意相依（巢狀的視窗），群組之間視為獨立。"""
    n = len(y)
    groups = np.unique(clusters) if n else np.array([])
    if n < 30 or len(groups) < 10:
        return {"n": n, "mean": float(np.mean(y)) if n else 0.0, "se": 0.0, "t": 0.0, "clusters": int(len(groups))}
    m = float(np.mean(y))
    e = y - m
    scores = np.array([float(e[clusters == g].sum()) for g in groups])
    G = len(groups)
    se = math.sqrt(G / (G - 1) * float(scores @ scores)) / n
    return {"n": n, "mean": m, "se": se, "t": m / se if se > 0 else 0.0, "clusters": int(G)}


def _group(ds: dict[str, np.ndarray], mask: np.ndarray, sign: float) -> dict[str, Any]:
    iv, rv = ds["iv"][mask], ds["rv"][mask]
    y = sign * (iv - rv)
    st = mean_t(y, ds["expiry"][mask])
    st.update(verdict=verdict(st["t"]), mean_iv=float(iv.mean()) if len(iv) else 0.0, mean_rv=float(rv.mean()) if len(rv) else 0.0,
              rv_above_iv_share=float(np.mean(rv > iv)) if len(iv) else 0.0, mde=2.8 * st["se"])
    return st


def study(ds: dict[str, np.ndarray]) -> dict[str, Any]:
    if len(ds["iv"]) < 300:
        raise ValueError(f"可用的天數只有 {len(ds['iv'])} 天，太少（先執行 python -m core.vrp_study fetch）")
    pct = ds["pct"]
    has = ~np.isnan(pct)
    tests = {"T1_all": _group(ds, np.ones(len(pct), bool), 1.0),
             "T2_low_iv": _group(ds, has & (pct <= LOW_PCT), -1.0),                # RV − IV：文件說低 IV 時應 > 0
             "T3_high_iv": _group(ds, has & (pct >= HIGH_PCT), 1.0)}              # IV − RV：文件說高 IV 時應 > 0
    # 每個到期月只取一筆（距到期最接近 30 天）：不靠重疊的視窗
    one = {}
    for i, e in enumerate(ds["expiry"]):
        if e not in one or abs(ds["T_days"][i] - 30) < abs(ds["T_days"][one[e]] - 30):
            one[e] = i
    idx = np.array(sorted(one.values()))
    y1 = (ds["iv"] - ds["rv"])[idx]
    sd = float(np.std(y1, ddof=1)) if len(y1) > 1 else 0.0
    monthly = {"n": len(y1), "mean": float(y1.mean()), "t": float(y1.mean() / (sd / math.sqrt(len(y1)))) if sd > 0 else 0.0}
    return {"n": int(len(ds["iv"])), "first": str(ds["dates"][0]), "last": str(ds["dates"][-1]), "tests": tests, "one_per_expiry": monthly}


def format_study(s: dict[str, Any]) -> str:
    names = {"T1_all": "T1 全部：IV − RV", "T2_low_iv": "T2 IV 低：RV − IV", "T3_high_iv": "T3 IV 高：IV − RV"}
    L = [f"══ 選擇權 IV vs 之後的實際波動 RV：{s['n']} 個日子（{s['first']} → {s['last']}）══",
         "  （波動點；t 值的標準誤以到期月為群組；文件的前提是 T2、T3 的平均都 > 0）"]
    for k, t in s["tests"].items():
        L.append(f"  {names[k]:<18}n={t['n']:<5}（{t['clusters']} 個到期月）平均 {t['mean']:+.2f}（標準誤 {t['se']:.2f}）  t={t['t']:+.2f}  [{t['verdict']}]   "
                 f"平均 IV {t['mean_iv']:.1f}／RV {t['mean_rv']:.1f}，RV>IV 的比例 {t['rv_above_iv_share']:.0%}   偵測力：看得出 ≥ {t['mde']:.2f} 個波動點")
    m = s["one_per_expiry"]
    L.append(f"  （描述）每個到期月只取一筆（距到期約 30 天）：n={m['n']}，IV − RV 平均 {m['mean']:+.2f}，t={m['t']:+.2f}")
    passed = [n for n, t in s["tests"].items() if t["verdict"] == "過關"]
    hints = [n for n, t in s["tests"].items() if t["verdict"] == "線索"]
    L.append(f"\n判定（{N_TESTS} 個檢定；過關 |t| ≥ {Z_CLAIM:.2f}、線索 |t| ≥ {Z_HINT:g}）：過關 {len(passed)} 個 {passed or ''}；線索 {len(hints)} 個 {hints or ''}。")
    return "\n".join(L)


# ── CLI ───────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.vrp_study", description="選擇權 IV 與之後實際波動的檢驗（期交所公開資料）")
    ap.add_argument("what", choices=("run", "fetch"), nargs="?", default="run")
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="CSV 存放位置（底下有 vrp/、tx/）")
    ap.add_argument("--start-year", type=int, default=START_YEAR)
    ap.add_argument("--json", help="把完整結果寫成 JSON")
    args = ap.parse_args(argv)
    d = Path(args.dir)
    try:
        if args.what == "fetch":
            r = fetch(d, args.start_year)
            print(f"下載 {r['downloaded']} 個月、略過 {r['skipped']} 個月、失敗 {r['failed']} 個月 → {d / 'vrp'}")
            return 0 if not r["failed"] else 1
        if not (d / "vrp").is_dir() or not (d / "tx").is_dir():
            raise ValueError(f"找不到 {d}/vrp 或 {d}/tx，請先執行 python -m core.vrp_study fetch 與 python -m core.oi_study fetch --only tx")
        res = study(build_dataset(load_iv(d / "vrp"), load_tx_contracts(d / "tx")))
    except (ValueError, OSError) as e:
        print(f"錯誤：{e}", file=sys.stderr)
        return 2
    print(format_study(res))
    if args.json:
        Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"完整結果已寫入 {args.json}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
