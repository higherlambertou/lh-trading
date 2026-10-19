"""〈資訊〉面板的資料：**唯讀**讀另一個 repo（lh-intel）的 intel.db（籌碼、總經事件、全球市場、新聞、收集器狀態）。只供顯示。

鐵則（lh-intel 的 SCHEMA.md 契約）
  - **唯讀**開啟（`file:…?mode=ro`），絕不寫入；找不到、打不開、版本比認得的新、讀到一半出錯，一律回 `{"available": False, "reason": …}`，**不丟例外、不影響交易**。
  - **這裡是「顯示現在」，不是「重播過去」**：只用 `fetched_at ≤ t` 的列（t＝現在），多版取最新版；**事後回補的列（`backfilled=1`）也照樣顯示**——它們現在確實已經知道了，面板要的是「目前最新的資料」。
    ⚠ lh-intel 的 `as_of` 規則（回補列預設不可見，除非 `published_at` 明確且 ≤ t）是給**分析與決策**用的，防止偷看未來；任何拿這些資料做研究、回測、進決策的程式都必須照那個規則（lh-intel/intel/read.py），**不能用這個模組**。
    `tests/test_intel_reader.py` 有跟 lh-intel 真正 schema 與寫入函式對照的測試（lh-intel 在旁邊時）。
  - **沒有任何策略、風控、下單、券商模組可以讀它**（`tests/test_intel_reader.py` 靜態檢查）；要讓它進決策之前，必須先登記驗證標準、用保留期驗證一次。
  - 日期的意義：籌碼的 event_date 是台北交易日；全球市場的 event_date 是「該市場當地的交易日」（美股收盤是台北隔天早上）；事件的 scheduled_date 是事件所在國家當地日期。
資料庫位置：環境變數 `INTEL_DB`，沒設就用同一層目錄的 `../lh-intel/data/intel.db`。"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
TAIPEI = ZoneInfo("Asia/Taipei")
SUPPORTED_SCHEMA = 1
CACHE_SEC = 10.0

OBS_VISIBLE = "fetched_at<=:t"                                           # 顯示「現在」：fetched_at 不晚於 t 的都算（含事後回補的）；研究用的 as_of 規則更嚴，見檔頭

# (代號, 名稱, source, series, 單位, 類型)；類型 rate＝殖利率（變動用「點」不用百分比）
MARKETS = (
    ("VIX", "VIX 恐慌指數", "cboe", "VIX.close", "points", "price"),
    ("SOX", "費城半導體", "nasdaq", "SOX.close", "points", "price"),
    ("TSM", "台積電 ADR", "nasdaq", "TSM.close", "usd", "price"),
    ("SPX", "S&P 500", "fred", "SP500", "points", "price"),
    ("NDX", "那斯達克綜合", "fred", "NASDAQCOM", "points", "price"),
    ("US10Y", "美國 10 年公債殖利率", "fred", "DGS10", "pct", "rate"),
    ("US2Y", "美國 2 年公債殖利率", "fred", "DGS2", "pct", "rate"),
    ("USDTWD", "美元兌台幣", "fred", "DEXTAUS", "twd", "price"),
    ("DXY", "美元指數（廣義）", "fred", "DTWEXBGS", "index", "price"),
    ("WTI", "WTI 原油", "fred", "DCOILWTICO", "usd", "price"),
    ("N225", "日經 225", "yahoo", "N225.close", "points", "price"),
    ("HSI", "恒生指數", "yahoo", "HSI.close", "points", "price"),
    ("KS11", "KOSPI", "yahoo", "KS11.close", "points", "price"),
)
FUT_COMMODITIES = (("TX", "臺股期貨（大台）"), ("MTX", "小型臺指"), ("TMF", "微型臺指"))
WHO = (("foreign", "外資"), ("dealer", "自營商"), ("trust", "投信"))
SOURCE_LABEL = {"cna": "中央社"}

_cache: dict[str, Any] = {"key": None, "at": 0.0, "data": None}


def default_path() -> Path:
    env = os.getenv("INTEL_DB", "").strip()
    return Path(env).expanduser() if env else ROOT.parent / "lh-intel" / "data" / "intel.db"


def connect_ro(path: Path) -> sqlite3.Connection:
    """唯讀連線（SQLite 的 mode=ro）：寫入一律失敗。資料庫是 WAL 模式，lh-intel 寫入時不會擋住讀取。"""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def latest_points(conn: sqlite3.Connection, source: str, series: str, t: float, n: int = 2) -> list[sqlite3.Row]:
    """某條日序列「在 t 時已經抓到」的最近 n 個不同日期，每個日期取已抓到的最新版，新 → 舊。（顯示用；研究要用 lh-intel 的 as_of。）"""
    return conn.execute(
        f"SELECT o.* FROM observations o JOIN (SELECT event_date AS d, max(revision) AS r FROM observations WHERE source=:s AND series=:se AND event_date IS NOT NULL AND {OBS_VISIBLE} "
        "GROUP BY event_date ORDER BY event_date DESC LIMIT :n) m ON o.event_date=m.d AND o.revision=m.r WHERE o.source=:s AND o.series=:se ORDER BY o.event_date DESC",
        {"s": source, "se": series, "t": t, "n": n}).fetchall()


def _vals(rows: list[sqlite3.Row]) -> list[tuple[str, Optional[float]]]:
    return [(r["event_date"], r["value"]) for r in rows]


def _round(x: Optional[float], nd: int = 2) -> Optional[float]:
    return None if x is None else round(x, nd)


def _delta(cur: Optional[float], prev: Optional[float]) -> Optional[float]:
    return None if cur is None or prev is None else cur - prev


# ── 各區塊 ───────────────────────────────────────────────────────

def markets(conn: sqlite3.Connection, t: float, today: date) -> list[dict[str, Any]]:
    out = []
    for key, label, source, series, unit, kind in MARKETS:
        pts = _vals(latest_points(conn, source, series, t, 2))
        if not pts:
            out.append({"key": key, "label": label, "source": source, "unit": unit, "kind": kind, "value": None, "date": None})
            continue
        d, v = pts[0]
        prev = pts[1][1] if len(pts) > 1 else None
        chg = _delta(v, prev)
        out.append({"key": key, "label": label, "source": source, "unit": unit, "kind": kind, "value": _round(v, 4), "prev": _round(prev, 4), "chg": _round(chg, 4),
                    "chg_pct": None if chg is None or not prev else _round(chg / prev * 100, 3), "date": d, "prev_date": pts[1][0] if len(pts) > 1 else None,
                    "age_days": (today - date.fromisoformat(d)).days})
    return out


def chips(conn: sqlite3.Connection, t: float, today: date) -> dict[str, Any]:
    out: dict[str, Any] = {"futures": [], "options": None, "pcr": None, "inst": None, "margin": None}
    for code, label in FUT_COMMODITIES:
        rows, day = [], None
        for who, who_label in WHO:
            pts = _vals(latest_points(conn, "taifex", f"futinst.{code}.{who}.net_oi_lots", t, 2))
            if not pts:
                continue
            day = day or pts[0][0]
            longs = _vals(latest_points(conn, "taifex", f"futinst.{code}.{who}.long_oi_lots", t, 1))
            shorts = _vals(latest_points(conn, "taifex", f"futinst.{code}.{who}.short_oi_lots", t, 1))
            rows.append({"who": who, "label": who_label, "date": pts[0][0], "net_oi": pts[0][1], "chg": _delta(pts[0][1], pts[1][1] if len(pts) > 1 else None),
                         "long_oi": longs[0][1] if longs else None, "short_oi": shorts[0][1] if shorts else None})
        if rows:
            out["futures"].append({"commodity": code, "label": label, "date": day, "rows": rows})
    orows, oday = [], None
    for who, who_label in WHO:
        c = _vals(latest_points(conn, "taifex", f"optinst.TXO.CALL.{who}.net_oi_lots", t, 2))
        p = _vals(latest_points(conn, "taifex", f"optinst.TXO.PUT.{who}.net_oi_lots", t, 2))
        if c and p:
            oday = oday or c[0][0]
            orows.append({"who": who, "label": who_label, "date": c[0][0], "call_net_oi": c[0][1], "call_chg": _delta(c[0][1], c[1][1] if len(c) > 1 else None),
                          "put_net_oi": p[0][1], "put_chg": _delta(p[0][1], p[1][1] if len(p) > 1 else None)})
    if orows:
        out["options"] = {"date": oday, "rows": orows}
    oi, vol = _vals(latest_points(conn, "taifex", "pcr.oi_ratio_pct", t, 2)), _vals(latest_points(conn, "taifex", "pcr.vol_ratio_pct", t, 2))
    if oi:
        out["pcr"] = {"date": oi[0][0], "oi_ratio": oi[0][1], "oi_ratio_prev": oi[1][1] if len(oi) > 1 else None,
                      "vol_ratio": vol[0][1] if vol else None, "vol_ratio_prev": vol[1][1] if len(vol) > 1 else None}
    out["inst"] = _inst(conn, t)
    out["margin"] = _margin(conn, t)
    return out


def _inst(conn: sqlite3.Connection, t: float) -> Optional[dict[str, Any]]:
    """證交所三大法人買賣超（億元）：今天與最近 5 個交易日合計。自營商＝自行買賣＋避險（2017 年後的格式；舊格式是單一列）。"""
    def net(series: str) -> list[tuple[str, Optional[float]]]:
        return _vals(latest_points(conn, "twse", series, t, 5))
    parts = {"foreign": net("inst.foreign.net"), "trust": net("inst.trust.net"), "dealer_self": net("inst.dealer_self.net"), "dealer_hedge": net("inst.dealer_hedge.net"),
             "dealer": net("inst.dealer.net"), "total": net("inst.total.net")}
    if not parts["foreign"]:
        return None
    day = parts["foreign"][0][0]

    def at(series: str, d: str) -> Optional[float]:
        return next((v for dd, v in parts[series] if dd == d), None)

    dates = [d for d, _ in parts["foreign"]]
    rows = []
    for key, label, comps in (("foreign", "外資", ("foreign",)), ("trust", "投信", ("trust",)), ("dealer", "自營商", ("dealer_self", "dealer_hedge")), ("total", "三大法人合計", ("total",))):
        def one(d: str) -> Optional[float]:
            vals = [at(c, d) for c in comps]
            if all(v is None for v in vals) and key == "dealer":
                vals = [at("dealer", d)]                                       # 舊格式的單一列
            return None if any(v is None for v in vals) else sum(vals) / 1e8
        today_v = one(day)
        five = [one(d) for d in dates]
        rows.append({"who": key, "label": label, "net": _round(today_v, 1), "net5": None if any(v is None for v in five) else _round(sum(five), 1), "days5": len(five)})
    return {"date": day, "rows": rows}


def _margin(conn: sqlite3.Connection, t: float) -> Optional[dict[str, Any]]:
    """融資融券。⚠ 今日餘額是暫定值，隔一個交易日的前日餘額才是最終（來源自己這麼說）：這裡顯示暫定值並標明。"""
    amt = _vals(latest_points(conn, "twse", "margin.financing_amt.today_balance", t, 2))
    sh = _vals(latest_points(conn, "twse", "margin.short_lots.today_balance", t, 2))
    if not amt:
        return None
    d_amt = _delta(amt[0][1], amt[1][1]) if len(amt) > 1 else None
    return {"date": amt[0][0], "financing_100m": _round(None if amt[0][1] is None else amt[0][1] / 1e5, 1),                       # 仟元 → 億元
            "financing_chg_100m": _round(None if d_amt is None else d_amt / 1e5, 1),
            "short_lots": sh[0][1] if sh else None, "short_chg": _delta(sh[0][1], sh[1][1]) if len(sh) > 1 else None, "provisional": True}


def events(conn: sqlite3.Connection, t: float, today: date, min_importance: int = 2, ahead_days: int = 14, behind_hours: int = 6) -> dict[str, Any]:
    ev_visible = "fetched_at<=:t"
    rows = conn.execute(
        f"SELECT e.* FROM events e JOIN (SELECT uid, max(revision) AS r FROM events WHERE {ev_visible} GROUP BY uid) m ON e.uid=m.uid AND e.revision=m.r "
        "WHERE e.kind='macro' AND e.status!='cancelled' AND e.importance>=:imp AND ((e.scheduled_time IS NOT NULL AND e.scheduled_time BETWEEN :lo AND :hi) "
        "OR (e.scheduled_time IS NULL AND e.scheduled_date BETWEEN :d0 AND :d1)) ORDER BY coalesce(e.scheduled_time, 0), e.scheduled_date, e.uid",
        {"t": t, "imp": min_importance, "lo": t - behind_hours * 3600, "hi": t + ahead_days * 86400,
         "d0": (today - timedelta(days=1)).isoformat(), "d1": (today + timedelta(days=ahead_days)).isoformat()}).fetchall()
    items = []
    for r in rows:
        det = json.loads(r["detail_json"]) if r["detail_json"] else {}
        when = None if r["scheduled_time"] is None else datetime.fromtimestamp(r["scheduled_time"], TAIPEI)
        items.append({"uid": r["uid"], "source": r["source"], "country": r["country"], "title": det.get("zh") or r["title"], "title_en": r["title"], "importance": r["importance"],
                      "scheduled_time": r["scheduled_time"], "taipei_time": None if when is None else when.strftime("%m/%d %H:%M"), "taipei_weekday": None if when is None else "一二三四五六日"[when.weekday()],
                      "scheduled_date": r["scheduled_date"], "local_date": r["scheduled_date"], "minutes_to": None if r["scheduled_time"] is None else round((r["scheduled_time"] - t) / 60),
                      "past": r["scheduled_time"] is not None and r["scheduled_time"] < t})
    items.sort(key=lambda x: (x["scheduled_time"] if x["scheduled_time"] is not None else datetime.fromisoformat(x["scheduled_date"]).replace(tzinfo=TAIPEI).timestamp(), -x["importance"]))
    hol = conn.execute(
        f"SELECT e.* FROM events e JOIN (SELECT uid, max(revision) AS r FROM events WHERE {ev_visible} GROUP BY uid) m ON e.uid=m.uid AND e.revision=m.r "
        "WHERE e.kind='holiday' AND e.scheduled_date>=:d0 ORDER BY e.scheduled_date LIMIT 12", {"t": t, "d0": today.isoformat()}).fetchall()
    closures = [{"date": h["scheduled_date"], "title": h["title"], "days_to": (date.fromisoformat(h["scheduled_date"]) - today).days}
                for h in hol if (json.loads(h["detail_json"]) if h["detail_json"] else {}).get("closed") and date.fromisoformat(h["scheduled_date"]).weekday() < 5][:3]      # 週末本來就不開市，不列
    return {"upcoming": items, "closures": closures}


def news(conn: sqlite3.Connection, t: float, limit: int = 15) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM news WHERE fetched_at<=:t AND dup_of IS NULL ORDER BY coalesce(published_at, fetched_at) DESC, id DESC LIMIT :n", {"t": t, "n": limit}).fetchall()
    out = []
    for r in rows:
        det = json.loads(r["detail_json"]) if r["detail_json"] else {}
        ts = r["published_at"] or r["fetched_at"]
        out.append({"title": r["title"], "url": r["url"], "source": r["source"], "label": SOURCE_LABEL.get(r["source"]) or det.get("publisher") or r["source"],
                    "time": ts, "taipei_time": datetime.fromtimestamp(ts, TAIPEI).strftime("%m/%d %H:%M"), "age_min": round((t - ts) / 60)})
    return out


def collectors(conn: sqlite3.Connection, t: float, heartbeat: Optional[float]) -> dict[str, Any]:
    names = [r[0] for r in conn.execute("SELECT DISTINCT collector FROM collector_runs WHERE collector NOT LIKE '%:backfill' ORDER BY collector")]
    out = []
    for n in names:
        runs = conn.execute("SELECT started_at, finished_at, status FROM collector_runs WHERE collector=? ORDER BY id DESC LIMIT 60", (n,)).fetchall()
        done = [r for r in runs if r["status"] is not None]
        last_ok = max((r["finished_at"] for r in done if r["status"] == "ok"), default=None)
        fails = 0
        for r in done:
            if r["status"] == "ok":
                break
            if r["status"] == "error":
                fails += 1
        out.append({"name": n, "last_ok": last_ok, "last_ok_age_sec": None if last_ok is None else round(t - last_ok), "last_status": done[0]["status"] if done else None, "fails": fails})
    age = None if heartbeat is None else round(t - heartbeat)
    return {"heartbeat_age_sec": age, "service_ok": age is not None and age < 180, "items": out,
            "problems": [c["name"] for c in out if c["fails"] >= 1]}


# ── 組起來 ───────────────────────────────────────────────────────

def _heartbeat(path: Path) -> Optional[float]:
    try:
        return float((path.parent / "heartbeat").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def build_summary(conn: sqlite3.Connection, path: Path, now: float) -> dict[str, Any]:
    ver = int(conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0])
    if ver > SUPPORTED_SCHEMA:
        return {"available": False, "reason": f"intel.db 的 schema_version={ver} 比這邊認得的（{SUPPORTED_SCHEMA}）新，請先更新交易系統的讀取模組", "as_of": now}
    today = datetime.fromtimestamp(now, TAIPEI).date()
    return {"available": True, "as_of": now, "schema_version": ver, "db": path.name, "events": events(conn, now, today), "chips": chips(conn, now, today),
            "markets": markets(conn, now, today), "news": news(conn, now), "collectors": collectors(conn, now, _heartbeat(path))}


def summary(now: Optional[float] = None, path: Optional[Path] = None) -> dict[str, Any]:
    """面板要的全部資料。**永遠不丟例外**：資料庫不在、被鎖太久、版本不對、讀到一半出錯＝available False＋原因。10 秒快取（多個畫面同時開不會重複查）。"""
    now = time.time() if now is None else now
    path = path or default_path()
    key = f"{path}"
    if _cache["key"] == key and now - _cache["at"] < CACHE_SEC and _cache["data"] is not None and _cache["data"].get("available"):
        return _cache["data"]
    if not path.exists():
        return {"available": False, "reason": f"找不到資料庫 {path}（lh-intel 沒裝？INTEL_DB 設錯？）", "as_of": now}
    try:
        with closing(connect_ro(path)) as conn:
            data = build_summary(conn, path, now)
    except sqlite3.Error as e:
        return {"available": False, "reason": f"讀取 intel.db 失敗：{type(e).__name__}: {e}", "as_of": now}
    except Exception as e:  # noqa: BLE001
        logger.warning("資訊面板資料組裝失敗", exc_info=True)
        return {"available": False, "reason": f"資訊面板資料組裝失敗：{type(e).__name__}: {e}", "as_of": now}
    _cache.update(key=key, at=now, data=data)
    return data
