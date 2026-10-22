"""外資期貨未平倉偏向的檢驗（update.md 發現 27 ③）：外資在臺股期貨（TX）的未平倉變化，能不能預測隔天日盤的方向？

資料是期交所的公開下載（不用登入、不碰券商）：
  - 〈三大法人-區分各期貨契約〉（臺股期貨 TXF，外資及陸資的多空未平倉口數淨額，每日收盤後公布）
  - 〈期貨每日交易行情〉（TX，一般交易時段的開盤與收盤）
`fetch` 把它們存成 CSV（預設 data/taifex/oi、data/taifex/tx；下載的檔案只當資料讀，不執行），`run` 做檢驗。

怎麼避免偷看未來：訊號是「第 t 日收盤後」才知道的未平倉；目標是「下一個交易日日盤的 開盤→收盤 報酬(%)」
（明天 08:45 進場、13:45 出場，不含夜盤）。目標在日盤內，用當天的近月合約，所以不會有展期的跳空。

3 個事先固定的檢定（Bonferroni：|t| ≥ 2.39 才宣稱有現象，|t| ≥ 2 只算線索）：
  目標與 (a) 外資淨未平倉的 1 日變化、(b) 5 日變化、(c) 淨未平倉相對「前 20 日（不含當天）」的 z 分數，各一個 Spearman 相關，
  t＝r√((n−2)/(1−r²))。另外只做描述（不檢定）：訊號最高／最低五分位的平均目標，以及前半、後半期的相關符號是否一致。

用法：
    python -m core.oi_study fetch            # 補齊缺的季／月（已有的不重抓；最近一期每次重抓）
    python -m core.oi_study run [--json out.json]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import NormalDist
from typing import Any, Callable, Optional

import numpy as np

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "data" / "taifex"
START_YEAR = 2016
N_TESTS = 3
Z_CLAIM = NormalDist().inv_cdf(1 - 0.05 / N_TESTS / 2)       # ≈ 2.39
Z_HINT = 2.0
Z_WINDOW = 20
OI_URL = "https://www.taifex.com.tw/cht/3/dlFutContractsDateDown"
TX_URL = "https://www.taifex.com.tw/cht/3/futDataDown"
OI_HEADER = "日期,商品名稱,身份別"
TX_HEADER = "交易日期,契約,到期月份"


def period_complete(f: Path, end: date) -> bool:
    """那一期結束之後才下載的檔才算完整。月中下載的（例如 2026-10 的檔是 10/12 抓的），月份結束後要再抓一次補齊；
    以前只看「檔案在、那一期已結束」，月中抓的檔就永遠停在那天（update.md 發現 65）。"""
    try:
        return datetime.fromtimestamp(f.stat().st_mtime).date() > end
    except OSError:
        return False


def verdict(t: float) -> str:
    return "過關" if abs(t) >= Z_CLAIM else ("線索" if abs(t) >= Z_HINT else "沒有")


# ── 解析 ──────────────────────────────────────────────────────────

def _num(s: str) -> Optional[float]:
    s = (s or "").strip().replace(",", "")
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _rows(path: Path, header_prefix: str) -> list[dict[str, str]]:
    text = path.read_bytes().decode("cp950", errors="replace").lstrip("﻿")
    if not text.startswith(header_prefix):
        raise ValueError(f"{path.name}：不是預期的 CSV（開頭不是『{header_prefix}…』），可能是下載失敗留下的錯誤頁")
    return list(csv.DictReader(io.StringIO(text)))


def _iso(d: str) -> str:
    return datetime.strptime(d.strip(), "%Y/%m/%d").date().isoformat()


def load_foreign_net_oi(directory: Path) -> dict[str, float]:
    """日期 → 外資及陸資在臺股期貨的多空未平倉口數淨額。"""
    out: dict[str, float] = {}
    for f in sorted(directory.glob("*.csv")):
        for r in _rows(f, OI_HEADER):
            if not r.get("身份別", "").startswith("外資"):
                continue
            net = _num(r.get("多空未平倉口數淨額", ""))
            if net is not None:
                out[_iso(r["日期"])] = net
    return dict(sorted(out.items()))


def load_tx_day_sessions(directory: Path) -> dict[str, tuple[float, float]]:
    """日期 → (開盤, 收盤)：一般交易時段、當天最近月（成交量 > 0）的 TX；價差單與盤後不算。"""
    best: dict[str, tuple[str, float, float]] = {}
    for f in sorted(directory.glob("*.csv")):
        for r in _rows(f, TX_HEADER):
            exp = r.get("到期月份(週別)", "").strip()
            if r.get("交易時段", "").strip() != "一般" or "/" in exp or not exp.isdigit():
                continue
            o, c, v = _num(r.get("開盤價", "")), _num(r.get("收盤價", "")), _num(r.get("成交量", ""))
            if o is None or c is None or not v or v <= 0:
                continue
            d = _iso(r["交易日期"])
            if d not in best or exp < best[d][0]:
                best[d] = (exp, o, c)
    return {d: (o, c) for d, (_, o, c) in sorted(best.items())}


# ── 訊號與目標 ────────────────────────────────────────────────────

def build_dataset(net_oi: dict[str, float], sessions: dict[str, tuple[float, float]], z_window: int = Z_WINDOW) -> dict[str, np.ndarray]:
    """第 t 日的訊號（收盤後已知）對齊「下一個交易日」的 開盤→收盤 報酬(%)。只用日期兩邊都有資料的日子。"""
    days = [d for d in sessions if d in net_oi]                   # 兩邊都有的交易日，依日期排序
    dates, d1, d5, z, y = [], [], [], [], []
    for i in range(z_window, len(days) - 1):
        t, nxt = days[i], days[i + 1]
        if i < 5 or (date.fromisoformat(nxt) - date.fromisoformat(t)).days > 7:        # 中間缺資料太久（例如下載漏了一段）就不接起來
            continue
        hist = np.array([net_oi[d] for d in days[i - z_window:i]], float)                 # 前 20 日，不含當天
        sd = float(hist.std(ddof=1))
        if sd == 0:
            continue
        o, c = sessions[nxt]
        dates.append(t)
        d1.append(net_oi[t] - net_oi[days[i - 1]])
        d5.append(net_oi[t] - net_oi[days[i - 5]])
        z.append((net_oi[t] - float(hist.mean())) / sd)
        y.append((c - o) / o * 100.0)
    return {"dates": np.array(dates), "d1": np.array(d1), "d5": np.array(d5), "z": np.array(z), "y": np.array(y)}


def rank(x: np.ndarray) -> np.ndarray:
    """平均名次（同值取平均）。"""
    order = np.argsort(x, kind="mergesort")
    r = np.empty(len(x), float)
    r[order] = np.arange(1, len(x) + 1)
    xs = x[order]
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[j + 1] == xs[i]:
            j += 1
        if j > i:
            r[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """(r, t)：t＝r√((n−2)/(1−r²))。"""
    n = len(x)
    if n < 10 or np.std(x) == 0 or np.std(y) == 0:
        return 0.0, 0.0
    r = float(np.corrcoef(rank(x), rank(y))[0, 1])
    return r, (r * math.sqrt((n - 2) / max(1e-12, 1 - r * r)))


def study(ds: dict[str, np.ndarray]) -> dict[str, Any]:
    y = ds["y"]
    n = len(y)
    if n < 100:
        raise ValueError(f"可用的天數只有 {n} 天，太少（要先 fetch 下載資料）")
    half = n // 2
    tests: dict[str, Any] = {}
    for name, label in (("d1", "1日變化"), ("d5", "5日變化"), ("z", "相對前20日的z")):
        x = ds[name]
        r, t = spearman(x, y)
        q_hi, q_lo = np.quantile(x, 0.8), np.quantile(x, 0.2)
        r1, _ = spearman(x[:half], y[:half])
        r2, _ = spearman(x[half:], y[half:])
        tests[name] = {"label": label, "n": n, "r": r, "t": t, "verdict": verdict(t),
                       "top_quintile_mean_pct": float(y[x >= q_hi].mean()), "bottom_quintile_mean_pct": float(y[x <= q_lo].mean()),
                       "all_mean_pct": float(y.mean()), "first_half_r": r1, "second_half_r": r2,
                       "same_sign_in_both_halves": bool(r1 * r2 > 0)}
    return {"n": n, "first": str(ds["dates"][0]), "last": str(ds["dates"][-1]), "mde_r": 2.8 / math.sqrt(n), "tests": tests}


def format_study(s: dict[str, Any]) -> str:
    L = [f"══ 外資期貨未平倉 vs 下一個交易日日盤（開盤→收盤）報酬：n={s['n']}（{s['first']} → {s['last']}），可看出 |r| ≥ 約 {s['mde_r']:.3f} ══"]
    for name, t in s["tests"].items():
        L.append(f"  {t['label']:<12}Spearman r={t['r']:+.3f}  t={t['t']:+.2f}  [{t['verdict']}]   "
                 f"訊號最高五分位隔天平均 {t['top_quintile_mean_pct']:+.3f}%、最低五分位 {t['bottom_quintile_mean_pct']:+.3f}%（全部 {t['all_mean_pct']:+.3f}%）   "
                 f"前半 r={t['first_half_r']:+.3f}／後半 r={t['second_half_r']:+.3f}（{'符號一致' if t['same_sign_in_both_halves'] else '符號不同'}）")
    passed = [n for n, t in s["tests"].items() if t["verdict"] == "過關"]
    hints = [n for n, t in s["tests"].items() if t["verdict"] == "線索"]
    L.append(f"\n判定（{N_TESTS} 個檢定；過關 |t| ≥ {Z_CLAIM:.2f}、線索 |t| ≥ {Z_HINT:g}）：過關 {len(passed)} 個 {passed or ''}；線索 {len(hints)} 個 {hints or ''}。"
             + ("" if passed else "沒有任何檢定過關＝這份資料上看不出外資未平倉對隔天日盤方向的預測力。"))
    return "\n".join(L)


# ── 下載 ──────────────────────────────────────────────────────────

def _periods(start_year: int, today: date) -> tuple[list[tuple[str, date, date]], list[tuple[str, date, date]]]:
    """(季的清單, 月的清單)：(檔名, 起, 迄)。"""
    quarters, months = [], []
    for y in range(start_year, today.year + 1):
        for q in range(4):
            a = date(y, 3 * q + 1, 1)
            b = (date(y + (3 * q + 4 > 12), (3 * q + 3) % 12 + 1, 1) - timedelta(days=1))
            if a > today:
                continue
            quarters.append((f"oi_{y}_{a:%m%d}.csv", a, min(b, today)))
        for m in range(1, 13):
            a = date(y, m, 1)
            b = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
            if a > today:
                continue
            months.append((f"tx_{y}_{m:02d}.csv", a, min(b, today)))
    return quarters, months


def fetch(directory: Path = DEFAULT_DIR, start_year: int = START_YEAR, today: Optional[date] = None, pause: float = 1.0,
          opener: Callable[..., Any] = urllib.request.urlopen, only: tuple[str, ...] = ("oi", "tx")) -> dict[str, int]:
    """補齊缺的季（三大法人）與月（TX 行情）；已經有、而且那一期已經結束的不重抓；沒結束的那一期每次重抓。每次請求之間停 pause 秒。
    only：只抓哪些（oi＝三大法人、tx＝TX 日行情）。三大法人網站只開放最近約 3 年，TX 日行情可以往前到 2003 年。"""
    today = today or date.today()
    got = {"downloaded": 0, "skipped": 0, "failed": 0}
    quarters, months = _periods(start_year, today)
    for sub, items, url, header, extra in (
            ("oi", quarters, OI_URL, OI_HEADER, {"commodityId": "TXF"}),
            ("tx", months, TX_URL, TX_HEADER, {"down_type": "1", "commodity_id": "TX", "commodity_id2": ""})):
        if sub not in only:
            continue
        out = directory / sub
        out.mkdir(parents=True, exist_ok=True)
        for name, a, b in items:
            f = out / name
            if f.exists() and f.stat().st_size > 0 and b < today and period_complete(f, b):
                got["skipped"] += 1
                continue
            body = urllib.parse.urlencode({"queryStartDate": f"{a:%Y/%m/%d}", "queryEndDate": f"{b:%Y/%m/%d}", **extra}).encode()
            try:
                with opener(urllib.request.Request(url, data=body), timeout=60) as resp:
                    data = resp.read()
                if not data.decode("cp950", errors="replace").lstrip("﻿").startswith(header):
                    raise ValueError("不是預期的 CSV")
                f.write_bytes(data)
                got["downloaded"] += 1
            except Exception as e:                                       # noqa: BLE001
                got["failed"] += 1
                print(f"下載失敗 {name}：{e}", file=sys.stderr)
            time.sleep(pause)
    return got


# ── CLI ───────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.oi_study", description="外資期貨未平倉偏向的檢驗（期交所公開資料）")
    ap.add_argument("what", choices=("run", "fetch"), nargs="?", default="run")
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="CSV 存放位置（底下有 oi/、tx/）")
    ap.add_argument("--start-year", type=int, default=START_YEAR)
    ap.add_argument("--only", choices=("oi", "tx"), help="fetch：只抓三大法人（oi）或只抓 TX 日行情（tx）")
    ap.add_argument("--json", help="把完整結果寫成 JSON")
    args = ap.parse_args(argv)
    d = Path(args.dir)
    try:
        if args.what == "fetch":
            r = fetch(d, args.start_year, only=(args.only,) if args.only else ("oi", "tx"))
            print(f"下載 {r['downloaded']} 個、略過 {r['skipped']} 個、失敗 {r['failed']} 個 → {d}")
            return 0 if not r["failed"] else 1
        if not (d / "oi").is_dir() or not (d / "tx").is_dir():
            raise ValueError(f"找不到 {d}/oi 與 {d}/tx，請先執行 python -m core.oi_study fetch")
        ds = build_dataset(load_foreign_net_oi(d / "oi"), load_tx_day_sessions(d / "tx"))
        res = study(ds)
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
