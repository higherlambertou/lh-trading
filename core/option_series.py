"""臺指選擇權的到期序列（update.md 發現 70）：月選＋週三週選＋週五週選。純函式，只整理清單、不碰下單。

永豐 SDK 把每一種放在不同的類別（`api.Contracts.Options.<類別>`）：
  月選 TXO（每月第三個週三到期）；週三週選 TX1／TX2／TX4／TX5（第幾個週三；第三週就是月選）；週五週選 TXU～TXZ（依週別輪替）。
同一個類別裡的「到期月」（delivery_month，例如 202610）不足以分辨週選，到期日要看合約的 delivery_date；
系統用（類別, 到期月）指定一個序列——後端的下單、報價、履約價、選擇權鏈、試算與停損監控本來就帶 category，只是以前畫面固定用 TXO。"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable, Optional

CATEGORIES = ("TXO", "TX1", "TX2", "TX4", "TX5", "TXU", "TXV", "TXW", "TXX", "TXY", "TXZ")
_WD = "一二三四五六日"


def kind_of(category: str) -> str:
    c = str(category or "").upper()
    if c == "TXO":
        return "monthly"
    if re.fullmatch(r"TX[0-9]", c):
        return "weekly_wed"
    if re.fullmatch(r"TX[U-Z]", c):
        return "weekly_fri"
    return "other"


KIND_TEXT = {"monthly": "月選", "weekly_wed": "週三週選", "weekly_fri": "週五週選", "other": "其他"}


def parse_date(raw: Any) -> Optional[date]:
    """'2026/10/23'、'2026-10-23'、'20261023' → date；認不得 None。"""
    s = re.sub(r"[^0-9]", "", str(raw or ""))[:8]
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def label(category: str, delivery_month: str, d: date, today: date) -> str:
    """畫面用的一行（選單很窄，要短）：「10/23（五）週選・明天到期」「11/18（三）月選・剩 27 天」——星期已經寫在括號裡，週三／週五不用再說一次。"""
    k = kind_of(category)
    head = f"{d.month}/{d.day}（{_WD[d.weekday()]}）{'月選' if k == 'monthly' else '週選' if k.startswith('weekly') else KIND_TEXT[k]}"
    days = (d - today).days
    tail = "今天到期" if days == 0 else "明天到期" if days == 1 else f"剩 {days} 天"
    return f"{head}・{tail}"


def build(rows: Iterable[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """worker 給的 [{category, delivery_month, delivery_date, n}] → 畫面用的序列（已到期的丟掉），依到期日、再依月選優先排序。"""
    out = []
    for r in rows:
        d = parse_date(r.get("delivery_date"))
        cat, dm = str(r.get("category") or ""), str(r.get("delivery_month") or "")
        if d is None or not cat or not dm or d < today:
            continue
        k = kind_of(cat)
        out.append({"category": cat, "delivery_month": dm, "delivery_date": d.isoformat(), "kind": k, "kind_text": KIND_TEXT[k],
                    "days": (d - today).days, "label": label(cat, dm, d, today), "n": int(r.get("n") or 0)})
    order = {"monthly": 0, "weekly_wed": 1, "weekly_fri": 2, "other": 3}
    return sorted(out, key=lambda s: (s["delivery_date"], order[s["kind"]], s["category"]))
