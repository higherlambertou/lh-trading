"""期交所〈選擇權每日交易行情〉的近價鏈歷史：選擇權進場訊號與平虧損腳規則的回測、買賣價差的歷史量測用（update.md 發現 30）。

和 core/vrp_study 的差別：vrp_study 只留「每天一筆 ATM IV」；這裡留**每個履約價每天的價格**（結算價、收盤、最後最佳買賣價、成交量、未沖銷），
才能回測「買了跨式之後，虧損那一腳什麼時候平掉」。原始檔一個月 0.4~11 MB（近年大）、不留；每個月下載一次、立刻整理成近價鏈存成小檔
（data/taifex/opt/chain_YYYY_MM.csv.gz，已被 .gitignore 的 data/ 涵蓋）。

保留什麼（每個交易日、每個到期月份各自判斷）：
- 月選擇權（到期月份是 6 位數）：最近的 2 個到期月（含到期當天，到期日的結算價就是最終結算的價值），履約價在遠期價 ±12% 內；
- 週選擇權（201806W2、201806F4 這種）：距到期 ≤ 14 天、履約價在遠期價 ±6% 內；
- 一般與盤後兩個時段都留（盤後從 2017 年中才有；盤後沒有結算價，只有收盤與最後最佳買賣價）。
遠期價 F：用該到期月一般時段的結算價、買賣權平價（C − P 最小的履約價 ≈ 遠期價，同 vrp_study），算不出來的到期月整個略過。
到期當天例外：交易所不再公布結算價（整欄是 0），F 改用價內選擇權的最後成交價反推最終結算價（settle 欄位維持 0；到期價值請用 max(±(F − K), 0)）。

誠實的限制：
- 「最後最佳買／賣價」是收盤那一刻的最後報價，不是整天的成交價，可能陳舊或很寬；回測用它估買賣價差、不夠可靠時退回結算價加減一點（見 core/option_backtest）。
- 2010~2016 年的檔案沒有「契約到期日」欄位，月選擇權用第三個週三推算（遇假日順延的情形忽略），週選擇權沒有日期就略過。
- 期交所網站偶爾回錯誤頁（HTTP 520 之類），fetch 會記為失敗、重跑補抓。

用法：
    python -m core.option_history fetch [--start-year 2010]     # 補齊缺的月（請求間隔 1 秒；已經有而且那個月已結束的不重抓）
    python -m core.option_history status
"""
from __future__ import annotations

import argparse
import csv
import gzip
import html
import io
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

from core.iv_monitor import expiry_of
from core.oi_study import DEFAULT_DIR, _iso, _num

OPT_URL = "https://www.taifex.com.tw/cht/3/optDataDown"
OPT_HEADER = "交易日期,契約,到期月份(週別)"
START_YEAR = 2010
MONTHLY_KEEP = 2                  # 月選擇權保留最近幾個到期月
MONTHLY_BAND = 0.12               # 月選擇權：履約價在遠期價 ± 這個比例內
WEEKLY_MAX_DAYS = 14              # 週選擇權：距到期不超過這麼多天
WEEKLY_BAND = 0.06
FIELDS = ("date", "kind", "expiry", "expiry_date", "session", "right", "strike", "F", "settle", "close", "bid", "ask", "volume", "oi")
MONTHLY = re.compile(r"\d{6}")
WEEKLY = re.compile(r"\d{6}[WF]\d")
SESSION = {"一般": "G", "": "G", "盤後": "A"}
FSP_URL = "https://www.taifex.com.tw/cht/5/optIndxFSP"
FSP_FILE = "opt_fsp.csv"
SHORT_DIR = "optw"
SHORT_START = (2012, 11)          # 週選擇權 2012-11-28 第一次到期
SHORT_MAX_DAYS = 7                # 短天期：距到期不超過這麼多個日曆天
SHORT_BAND = 0.06


def _fmt(x: Optional[float]) -> str:
    if x is None:
        return ""
    return f"{x:.4f}".rstrip("0").rstrip(".") if x != int(x) else str(int(x))


def parse_expiry_date(raw: str, code: str) -> Optional[date]:
    """契約到期日（'20260902' 或 '2026/09/02'）；沒有時月選擇權用第三個週三，週選擇權回 None。"""
    raw = (raw or "").strip()
    for f in ("%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, f).date()
        except ValueError:
            pass
    return expiry_of(code).date() if MONTHLY.fullmatch(code) else None


def parity_forward(pairs: dict[float, dict[str, float]]) -> Optional[float]:
    """{履約價: {'C': 結算價, 'P': 結算價}} → 遠期價：C − P 最小（最靠近平值）的履約價 + (C − P)。至少 3 個買賣權都有價的履約價才算。"""
    ok = {k: v for k, v in pairs.items() if "C" in v and "P" in v}
    if len(ok) < 3:
        return None
    k0 = min(ok, key=lambda k: abs(ok[k]["C"] - ok[k]["P"]))
    return k0 + ok[k0]["C"] - ok[k0]["P"]


def final_settlement(closes: dict[float, dict[str, float]], min_price: float = 5.0, tol: float = 5.0) -> Optional[float]:
    """到期當天：交易所不再公布結算價（整欄是 0），買賣權平價也失效。改用價內選擇權最後成交價（收盤價）反推最終結算價附近：
    價內買權 → S ≈ 履約價 + 收盤價；價內賣權 → S ≈ 履約價 − 收盤價。只用收盤價 ≥ min_price 的（價外的是 0.1 之類的一檔價，沒有資訊），
    取中位數，至少 3 檔彼此差 ≤ tol 點的估計才算（最後成交的時間點不同，會差幾點）。真正的最終結算價是 13:30 前 30 分鐘的指數平均，這裡的估計通常差幾點，
    相對於跨式幾百點的權利金可以忽略。"""
    est = []
    for k, d in closes.items():
        c, p = d.get("C") or 0.0, d.get("P") or 0.0
        if c >= min_price and p < min_price:
            est.append(k + c)
        elif p >= min_price and c < min_price:
            est.append(k - p)
    if len(est) < 3:
        return None
    est.sort()
    med = est[len(est) // 2]
    return med if sum(1 for x in est if abs(x - med) <= tol) >= 3 else None


def reduce_month(text: str) -> list[dict[str, Any]]:
    """一個月的選擇權行情（CSV 文字）→ 近價鏈的列（FIELDS）。"""
    recs: list[dict[str, Any]] = []
    settle_pairs: dict[tuple[str, str], dict[float, dict[str, float]]] = {}
    close_pairs: dict[tuple[str, str], dict[float, dict[str, float]]] = {}
    exp_date: dict[tuple[str, str], Optional[date]] = {}
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        code = (r.get("到期月份(週別)") or "").strip()
        if (r.get("契約") or "").strip() != "TXO" or not (MONTHLY.fullmatch(code) or WEEKLY.fullmatch(code)):
            continue
        side = {"買權": "C", "賣權": "P"}.get((r.get("買賣權") or "").strip())
        sess = SESSION.get((r.get("交易時段") or "").strip())
        strike = _num(r.get("履約價", ""))
        if side is None or sess is None or not strike:
            continue
        day = _iso(r["交易日期"])
        key = (day, code)
        if key not in exp_date:
            exp_date[key] = parse_expiry_date(r.get("契約到期日", ""), code)
        rec = {"date": day, "expiry": code, "session": sess, "right": side, "strike": strike, "settle": _num(r.get("結算價", "")),
               "close": _num(r.get("收盤價", "")), "bid": _num(r.get("最後最佳買價", "")), "ask": _num(r.get("最後最佳賣價", "")),
               "volume": _num(r.get("成交量", "")), "oi": _num(r.get("未沖銷契約數", ""))}
        recs.append(rec)
        if sess == "G" and rec["settle"] is not None and rec["settle"] > 0:
            settle_pairs.setdefault(key, {}).setdefault(strike, {})[side] = rec["settle"]
        if sess == "G" and rec["close"] is not None and rec["close"] > 0:
            close_pairs.setdefault(key, {}).setdefault(strike, {})[side] = rec["close"]
    fwd: dict[tuple[str, str], Optional[float]] = {}
    for key in set(settle_pairs) | set(close_pairs):
        ed = exp_date.get(key)
        # 到期當天結算價整欄是 0、買賣權平價失效：F 改成用價內選擇權的最後成交價反推的最終結算價（settle 欄位維持 0，到期價值請用 max(±(F−K), 0)）
        fwd[key] = final_settlement(close_pairs.get(key, {})) if ed is not None and ed.isoformat() == key[0] else parity_forward(settle_pairs.get(key, {}))
    # 每天：月選擇權只留最近的 MONTHLY_KEEP 個（到期日 >= 今天）；週選擇權留距到期 <= WEEKLY_MAX_DAYS 天的
    keep: dict[tuple[str, str], str] = {}
    by_day: dict[str, list[tuple[date, str]]] = {}
    for (day, code), ed in exp_date.items():
        if ed is not None and fwd.get((day, code)) and ed >= date.fromisoformat(day):
            by_day.setdefault(day, []).append((ed, code))
    for day, lst in by_day.items():
        d0 = date.fromisoformat(day)
        monthly = sorted(x for x in lst if MONTHLY.fullmatch(x[1]))[:MONTHLY_KEEP]
        for ed, code in monthly:
            keep[(day, code)] = "M"
        for ed, code in lst:
            if WEEKLY.fullmatch(code) and (ed - d0).days <= WEEKLY_MAX_DAYS:
                keep[(day, code)] = "W"
    out = []
    for rec in recs:
        key = (rec["date"], rec["expiry"])
        kind = keep.get(key)
        if kind is None:
            continue
        F = fwd[key]
        if abs(rec["strike"] - F) > F * (MONTHLY_BAND if kind == "M" else WEEKLY_BAND):
            continue
        out.append({**rec, "kind": kind, "expiry_date": exp_date[key].isoformat(), "F": round(F, 2)})
    out.sort(key=lambda x: (x["date"], x["kind"], x["expiry"], x["session"], x["right"], x["strike"]))
    return out


# ── 下載與讀取 ────────────────────────────────────────────────────

def months(start_year: int, today: date) -> list[tuple[str, date, date]]:
    out = []
    for y in range(start_year, today.year + 1):
        for m in range(1, 13):
            a = date(y, m, 1)
            if a > today:
                break
            b = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
            out.append((f"chain_{y}_{m:02d}.csv.gz", a, min(b, today)))
    return out


def write_rows(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(FIELDS)
    n = 0
    for r in rows:
        w.writerow([_fmt(r[k]) if isinstance(r[k], float) or r[k] is None else r[k] for k in FIELDS])
        n += 1
    tmp = path.with_suffix(path.suffix + ".part")
    with gzip.open(tmp, "wt", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())
    tmp.replace(path)                                    # 寫到一半被中斷不會留下半個檔
    return n


def fetch(directory: Path = DEFAULT_DIR, start_year: int = START_YEAR, today: Optional[date] = None, pause: float = 1.0,
          opener: Callable[..., Any] = urllib.request.urlopen, log: Callable[[str], None] = print) -> dict[str, int]:
    """每個月下載一次原始行情、整理成近價鏈存成 chain_YYYY_MM.csv.gz；已經有而且那個月已結束的不重抓。"""
    today = today or date.today()
    out = directory / "opt"
    out.mkdir(parents=True, exist_ok=True)
    got = {"downloaded": 0, "skipped": 0, "failed": 0, "rows": 0}
    for name, a, b in months(start_year, today):
        f = out / name
        if f.exists() and f.stat().st_size > 0 and b < today:
            got["skipped"] += 1
            continue
        body = urllib.parse.urlencode({"down_type": "1", "commodity_id": "TXO", "commodity_id2": "",
                                       "queryStartDate": f"{a:%Y/%m/%d}", "queryEndDate": f"{b:%Y/%m/%d}"}).encode()
        try:
            with opener(urllib.request.Request(OPT_URL, data=body), timeout=180) as resp:
                text = resp.read().decode("cp950", errors="replace")
            if not text.lstrip("﻿").startswith(OPT_HEADER):
                raise ValueError("不是預期的 CSV（可能是錯誤頁）")
            rows = reduce_month(text)
            if not rows:
                raise ValueError("這個月整理不出任何一列（休市整月？或格式變了）")
            got["rows"] += write_rows(f, rows)
            got["downloaded"] += 1
            log(f"  {name}：{len(rows)} 列")
        except Exception as e:                                                        # noqa: BLE001
            got["failed"] += 1
            print(f"下載失敗 {name}：{e}", file=sys.stderr)
        time.sleep(pause)
    return got


def iter_rows(directory: Path = DEFAULT_DIR, start: str = "", end: str = "9999-12-31") -> Iterator[dict[str, Any]]:
    """依日期順序讀回整理好的列（數字欄位已轉成 float，空的是 None）。start／end 是含頭含尾的日期字串。"""
    for f in sorted((directory / "opt").glob("chain_*.csv.gz")):
        with gzip.open(f, "rt", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                if r["date"] < start or r["date"] > end:
                    continue
                for k in ("strike", "F", "settle", "close", "bid", "ask", "volume", "oi"):
                    r[k] = float(r[k]) if r[k] != "" else None
                yield r


def status(directory: Path = DEFAULT_DIR) -> dict[str, Any]:
    files = sorted((directory / "opt").glob("chain_*.csv.gz"))
    size = sum(f.stat().st_size for f in files)
    return {"files": len(files), "first": files[0].name if files else None, "last": files[-1].name if files else None, "mb": round(size / 1e6, 1)}


# ── 短天期（週選研究用，update.md 發現 71）─────────────────────────
# 2025-12 以前的原始檔沒有「契約到期日」，上面的近價鏈因此沒有週選擇權。到期日與最終結算價改用期交所〈最後結算價—指數選擇權〉
# 這張官方表（每個到期代碼一列：最後結算日、代碼、最後結算價，含週三與週五週選），短天期的鏈另外存在 data/taifex/optw/。

def parse_fsp(text: str) -> list[tuple[str, str, float]]:
    """〈最後結算價—指數選擇權〉查詢結果（HTML，只勾臺指選擇權）→ [(最後結算日 ISO, 到期代碼, 最後結算價)]，依日期排序。
    期交所的表有的列少了 </TR>，兩組擠在同一列，所以依序找「日期、代碼、數字」三格一組。"""
    if "TXO" not in text:
        raise ValueError("不是臺指選擇權的最後結算價表")
    cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", text, re.S | re.I)]
    out: set[tuple[str, str, float]] = set()
    i = 0
    while i + 2 < len(cells):
        d, code, px = cells[i], cells[i + 1], cells[i + 2].replace(",", "")
        if re.fullmatch(r"\d{4}/\d{2}/\d{2}", d) and (MONTHLY.fullmatch(code) or WEEKLY.fullmatch(code)) and re.fullmatch(r"\d+(\.\d+)?", px):
            if float(px) > 0:
                out.add((d.replace("/", "-"), code, float(px)))
            i += 3
        else:
            i += 1
    return sorted(out)


def fetch_fsp(directory: Path = DEFAULT_DIR, start_year: int = SHORT_START[0], today: Optional[date] = None,
              opener: Callable[..., Any] = urllib.request.urlopen) -> int:
    """下載臺指選擇權全部的最後結算價（一次查整段）存成 data/taifex/opt_fsp.csv；回傳列數。"""
    today = today or date.today()
    body = urllib.parse.urlencode({"commodityIds": "2", "_all": "on", "start_year": str(start_year), "start_month": "01",
                                   "end_year": str(today.year), "end_month": f"{today.month:02d}", "button": "送出查詢"}).encode()
    with opener(urllib.request.Request(FSP_URL, data=body, headers={"User-Agent": "Mozilla/5.0"}), timeout=180) as resp:
        rows = parse_fsp(resp.read().decode("utf-8", errors="replace"))
    if len(rows) < 12:
        raise ValueError(f"最後結算價只有 {len(rows)} 列（查詢失敗？）")
    path = directory / FSP_FILE
    tmp = path.with_suffix(".part")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(("date", "code", "fsp"))
        w.writerows((d, c, _fmt(v)) for d, c, v in rows)
    tmp.replace(path)
    return len(rows)


def load_fsp(directory: Path = DEFAULT_DIR) -> dict[str, tuple[date, float]]:
    """到期代碼 → (最後結算日, 最後結算價)。"""
    with open(directory / FSP_FILE, encoding="utf-8", newline="") as f:
        return {r["code"]: (date.fromisoformat(r["date"]), float(r["fsp"])) for r in csv.DictReader(f)}


def code_kind(code: str) -> str:
    """'202610' 月選 M；'202610W2' 週三週選 W；'202610F3' 週五週選 F。"""
    return "M" if MONTHLY.fullmatch(code) else code[6]


def reduce_short(text: str, fsp: dict[str, tuple[date, float]]) -> list[dict[str, Any]]:
    """一個月的原始行情 → 短天期的列：到期代碼在最後結算價表裡、距到期 0～SHORT_MAX_DAYS 個日曆天、一般時段、履約價在遠期價 ± SHORT_BAND 內。
    遠期價：到期前用當天結算價的買賣權平價；到期當天直接用最後結算價（官方值，不再反推）。"""
    recs: list[dict[str, Any]] = []
    pairs: dict[tuple[str, str], dict[float, dict[str, float]]] = {}
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        code = (r.get("到期月份(週別)") or "").strip()
        if (r.get("契約") or "").strip() != "TXO" or code not in fsp or SESSION.get((r.get("交易時段") or "").strip()) != "G":
            continue
        side = {"買權": "C", "賣權": "P"}.get((r.get("買賣權") or "").strip())
        strike = _num(r.get("履約價", ""))
        if side is None or not strike:
            continue
        day = _iso(r["交易日期"])
        dte = (fsp[code][0] - date.fromisoformat(day)).days
        if not 0 <= dte <= SHORT_MAX_DAYS:
            continue
        rec = {"date": day, "expiry": code, "session": "G", "right": side, "strike": strike, "settle": _num(r.get("結算價", "")),
               "close": _num(r.get("收盤價", "")), "bid": _num(r.get("最後最佳買價", "")), "ask": _num(r.get("最後最佳賣價", "")),
               "volume": _num(r.get("成交量", "")), "oi": _num(r.get("未沖銷契約數", ""))}
        recs.append(rec)
        if rec["settle"] is not None and rec["settle"] > 0:
            pairs.setdefault((day, code), {}).setdefault(strike, {})[side] = rec["settle"]
    fwd: dict[tuple[str, str], Optional[float]] = {}
    out = []
    for rec in recs:
        key = (rec["date"], rec["expiry"])
        if key not in fwd:
            ed, px = fsp[rec["expiry"]]
            fwd[key] = px if ed.isoformat() == rec["date"] else parity_forward(pairs.get(key, {}))
        F = fwd[key]
        if not F or abs(rec["strike"] - F) > F * SHORT_BAND:
            continue
        out.append({**rec, "kind": code_kind(rec["expiry"]), "expiry_date": fsp[rec["expiry"]][0].isoformat(), "F": round(F, 2)})
    out.sort(key=lambda x: (x["date"], x["expiry"], x["right"], x["strike"]))
    return out


def fetch_short(directory: Path = DEFAULT_DIR, today: Optional[date] = None, pause: float = 1.0,
                opener: Callable[..., Any] = urllib.request.urlopen, log: Callable[[str], None] = print) -> dict[str, int]:
    """每個月下載一次原始行情、整理成短天期的列存成 optw/short_YYYY_MM.csv.gz（要先有 opt_fsp.csv）；已經有而且那個月已結束的不重抓。"""
    today = today or date.today()
    fsp = load_fsp(directory)
    out = directory / SHORT_DIR
    out.mkdir(parents=True, exist_ok=True)
    got = {"downloaded": 0, "skipped": 0, "failed": 0, "rows": 0}
    for name, a, b in months(SHORT_START[0], today):
        if (a.year, a.month) < SHORT_START:
            continue
        f = out / name.replace("chain_", "short_")
        if f.exists() and f.stat().st_size > 0 and b < today:
            got["skipped"] += 1
            continue
        body = urllib.parse.urlencode({"down_type": "1", "commodity_id": "TXO", "commodity_id2": "",
                                       "queryStartDate": f"{a:%Y/%m/%d}", "queryEndDate": f"{b:%Y/%m/%d}"}).encode()
        try:
            with opener(urllib.request.Request(OPT_URL, data=body), timeout=180) as resp:
                text = resp.read().decode("cp950", errors="replace")
            if not text.lstrip("﻿").startswith(OPT_HEADER):
                raise ValueError("不是預期的 CSV（可能是錯誤頁）")
            rows = reduce_short(text, fsp)
            if not rows:
                raise ValueError("這個月整理不出任何一列")
            got["rows"] += write_rows(f, rows)
            got["downloaded"] += 1
            log(f"  {f.name}：{len(rows)} 列")
        except Exception as e:                                                        # noqa: BLE001
            got["failed"] += 1
            print(f"下載失敗 {f.name}：{e}", file=sys.stderr)
        time.sleep(pause)
    return got


def iter_short(directory: Path = DEFAULT_DIR) -> Iterator[dict[str, Any]]:
    """依日期順序讀回短天期的列（數字欄位轉成 float，空的是 None）。"""
    for f in sorted((directory / SHORT_DIR).glob("short_*.csv.gz")):
        with gzip.open(f, "rt", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                for k in ("strike", "F", "settle", "close", "bid", "ask", "volume", "oi"):
                    r[k] = float(r[k]) if r[k] != "" else None
                yield r


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.option_history", description="期交所選擇權近價鏈歷史（回測用）")
    ap.add_argument("what", choices=("fetch", "status", "fetch-short"))
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--start-year", type=int, default=START_YEAR)
    a = ap.parse_args(argv)
    d = Path(a.dir)
    if a.what == "status":
        print(status(d))
        return 0
    if a.what == "fetch-short":
        print(f"最後結算價 {fetch_fsp(d)} 列 → {d / FSP_FILE}")
        r = fetch_short(d)
        print(f"短天期：下載 {r['downloaded']} 個月（{r['rows']:,} 列）、略過 {r['skipped']} 個月、失敗 {r['failed']} 個月 → {d / SHORT_DIR}")
        return 0 if not r["failed"] else 1
    r = fetch(d, a.start_year)
    print(f"下載 {r['downloaded']} 個月（{r['rows']:,} 列）、略過 {r['skipped']} 個月、失敗 {r['failed']} 個月 → {d / 'opt'}")
    return 0 if not r["failed"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
