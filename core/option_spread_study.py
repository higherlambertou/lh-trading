"""選擇權歷史買賣價差（update.md 發現 30 A；描述、不檢定）。

資料：core/option_history 整理好的近價鏈，每天每一檔一筆「收盤那一刻的最後最佳買賣價」。依
時段（一般／盤後）× 月／週選擇權 × 離遠期價的距離（≤0.5%、0.5~2%、2~5%）× 年份，列
價差中位數（點與占中價 %）與雙邊報價的比例。

誠實的限制：
- 這是「每天收盤那一刻」的最後最佳買賣價，不是盤中的價差：收盤後買價可能被撤、陳舊報價還掛著，所以近年的數字可能比盤中真實價差寬很多
  （回測 core/option_backtest 的資料檢查就看到進場腿的價差 2010~2019 年約 1%、2024~2026 年 7~15%，而 2026-10-12 夜盤實測 ATM 約 2%）。
  盤中各個時間點的價差由 core/option_spread_log 的取樣迴圈累積（需重啟後端才開始記），之後用它的 report 看。
- 只算「雙邊都有報價」（買價 > 0、賣價 ≥ 買價）的那些檔；單邊或交叉的不進中位數，只計入「雙邊報價占比」的分母。價差占中價用 (賣−買) ÷ ((賣+買)/2)。
- 盤後（夜盤）從 2017 年中才有資料；週選擇權只留距到期 ≤14 天、±6% 內。

用法：
    python -m core.option_spread_study run [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Optional

from core.option_history import DEFAULT_DIR, iter_rows

BUCKETS = (("≤0.5%", 0.005), ("0.5~2%", 0.02), ("2~5%", 0.05))        # |履約價 − 遠期價| ÷ 遠期價 的上界（含）
SESSION_TEXT = {"G": "一般時段（日盤）", "A": "盤後（夜盤）"}
KIND_TEXT = {"M": "月選擇權", "W": "週選擇權"}


def bucket_of(strike: float, F: float) -> Optional[int]:
    """離遠期價的距離落在哪個區間（0、1、2）；超過 5% 回 None。"""
    if F <= 0:
        return None
    d = abs(strike - F) / F
    for i, (_, hi) in enumerate(BUCKETS):
        if d <= hi:
            return i
    return None


def two_sided(bid: Optional[float], ask: Optional[float]) -> bool:
    return bid is not None and ask is not None and bid > 0 and ask >= bid


def collect(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, str, int, int], dict[str, Any]]:
    """{(時段, 月/週, 區間, 年): {'n': 列數, 'pts': [雙邊報價的價差點], 'pct': [占中價]}}。"""
    cells: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    for r in rows:
        b = bucket_of(r["strike"], r["F"])
        if b is None:
            continue
        cell = cells.setdefault((r["session"], r["kind"], b, int(r["date"][:4])), {"n": 0, "pts": [], "pct": []})
        cell["n"] += 1
        if two_sided(r["bid"], r["ask"]):
            cell["pts"].append(r["ask"] - r["bid"])
            cell["pct"].append((r["ask"] - r["bid"]) / ((r["ask"] + r["bid"]) / 2))
    return cells


def summarize(cells: dict[tuple[str, str, int, int], dict[str, Any]]) -> list[dict[str, Any]]:
    """每個格子：列數、雙邊報價的檔數與占比、價差中位數（點、%）。"""
    out = []
    for (session, kind, b, year), c in sorted(cells.items()):
        k = len(c["pts"])
        out.append({"session": session, "kind": kind, "bucket": BUCKETS[b][0], "year": year, "n": c["n"], "two_sided": k,
                    "share": k / c["n"] if c["n"] else None, "pts": median(c["pts"]) if k else None, "pct": median(c["pct"]) if k else None})
    return out


def format_report(summary: list[dict[str, Any]]) -> str:
    """每個 (時段, 月/週) 一張表：列＝年份，欄＝三個距離區間的（價差占中價中位數／價差點數中位數／雙邊報價占比）。"""
    lines = ["選擇權歷史買賣價差（每天收盤那一刻的最後最佳買賣價；描述、不檢定）", ""]
    groups: dict[tuple[str, str], dict[int, dict[str, dict[str, Any]]]] = {}
    for r in summary:
        groups.setdefault((r["session"], r["kind"]), {}).setdefault(r["year"], {})[r["bucket"]] = r
    for (session, kind), years in sorted(groups.items()):
        lines.append(f"■ {SESSION_TEXT[session]}・{KIND_TEXT[kind]}　每格：價差占中價 ／ 價差點數 ／ 雙邊報價占比（中位數）")
        lines.append("  年     " + "".join(f"{name:^29}" for name, _ in BUCKETS))
        for y, cols in sorted(years.items()):
            cells = []
            for name, _ in BUCKETS:
                r = cols.get(name)
                cells.append("—".center(29) if r is None or r["pct"] is None else f"{r['pct'] * 100:5.1f}% /{r['pts']:6.1f} 點 /{r['share'] * 100:4.0f}% (n={r['two_sided']})".ljust(29))
            lines.append(f"  {y}  " + "".join(cells))
        lines.append("")
    return "\n".join(lines).rstrip()


def run(directory: Path = DEFAULT_DIR) -> list[dict[str, Any]]:
    return summarize(collect(iter_rows(directory)))


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    ap = argparse.ArgumentParser(prog="python -m core.option_spread_study", description="選擇權歷史買賣價差（收盤的最後最佳買賣價）")
    ap.add_argument("what", choices=("run",))
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--json", help="把完整結果寫成 JSON")
    a = ap.parse_args(argv)
    res = run(Path(a.dir))
    print(format_report(res))
    if a.json:
        Path(a.json).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
