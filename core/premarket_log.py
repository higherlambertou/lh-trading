"""盤前跨市場資訊紀錄（update.md 發現 31；需求文件《盤前跨市場資訊紀錄與驗證》v1）。

每個台股交易日日盤開盤前（T＝PREMARKET_TIME，預設 08:30）記一筆「隔夜」快照：台指期夜盤報酬（基準）、費半 SOX、台積電 ADR（TSM）、VIX。
**只記錄，不影響任何交易決策**：沒有任何策略或風控模組讀這些欄位（tests/test_premarket_log.py 有靜態檢查）；寫入失敗只警示（ERROR log、計數、錯誤表、備援檔），不擋任何事。

規則（對應需求文件的必做項目）：
- 固定時間 T：只用 T 之前已知的資料；資料本身的時間戳晚於 T 的一律拒絕。每個欄位記資料時間戳（source_timestamp）與取得時間（fetched_at），都是 UTC epoch 秒，顯示（CSV）轉 Asia/Taipei。
- 美股收盤時間用 zoneinfo 算（冬令 16:00 ET＝台北隔日 05:00、夏令 04:00；提早收盤日 13:00 ET）；週一盤前用上週五；美股休市日該欄位記 null＋原因、**不沿用前一天**。
- 缺值、來源失敗、過期、異常值（超出設定範圍）一律 null＋原因；不補預設值、不插補；異常值只標記（原始數字留在 detail）。
- 每個台股交易日恰好一筆（休市日不產生）；只新增不修改（資料庫觸發器擋 UPDATE／DELETE）；更正＝新增一筆指向原紀錄；作廢（例如颱風假）＝新增一筆 void。
- 後端 T 之後才啟動時最晚補到 13:45（資料仍只用 T 之前的，標 late_seconds）。

資料來源（update.md 發現 31）：Yahoo 對這台機器回 429，預設改用 Nasdaq 公開 API（TSM、SOX）＋Cboe 官方 CSV（VIX）；Yahoo 轉接器預設關（PREMARKET_SOURCES 可排進去，沒有對真實 Yahoo 驗證過）。
都是免費公開端點、不是契約授權：只存檔供個人研究、不轉發。

環境變數：PREMARKET_LOG=false 關閉；PREMARKET_TIME（預設 08:30）；PREMARKET_SOURCES（預設 intel,nasdaq,cboe：先讀 lh-intel 已經收好的資料，沒有或還沒更新才直接打 Nasdaq／Cboe）；PREMARKET_RANGE_{NIGHT,SOX,TSM,VIX}="下限,上限"（異常值範圍）。
用法：python -m core.premarket_log show|run|export|status|void|correct   （show 只算不寫）
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
import os
import sqlite3
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional, Sequence
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

NY, TPE, UTC = ZoneInfo("America/New_York"), ZoneInfo("Asia/Taipei"), timezone.utc
ROOT = Path(__file__).resolve().parent.parent
TICKS_PATH = ROOT / "data" / "ticks.db"
CACHE_DIR = ROOT / "data" / "premarket"
ERROR_FILE = CACHE_DIR / "premarket_errors.jsonl"
SCHEMA_VERSION = 1
LATEST_HHMM = (13, 45)               # 這之後不再補寫（日盤已經收了）
RETRY_WINDOW_MIN = 15                # T 之後這麼久內，來源失敗的欄位會重試；過了就帶著 null 寫入
MIN_BARS = 22                        # 美股欄位要有的日線筆數（20 個報酬的標準差 ＋ 最新一個報酬）
BREAKER_SEC = 6 * 3600               # 來源回 429 之後這麼久不再打
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def enabled() -> bool:
    return os.getenv("PREMARKET_LOG", "true").strip().lower() not in ("0", "false", "no", "off")


def snapshot_time() -> dtime:
    raw = os.getenv("PREMARKET_TIME", "08:30").strip()
    try:
        h, m = raw.split(":")
        return dtime(int(h), int(m))
    except ValueError:
        return dtime(8, 30)


def t_of(d: date) -> datetime:
    """交易日 d 的取樣時間 T（Asia/Taipei）。"""
    return datetime.combine(d, snapshot_time(), tzinfo=TPE)


def latest_of(d: date) -> datetime:
    return datetime.combine(d, dtime(*LATEST_HHMM), tzinfo=TPE)


# ── 美股日曆（NYSE）──────────────────────────────────────────────

SPECIAL_CLOSURES = {date(2018, 12, 5): "全國哀悼日（老布希）", date(2025, 1, 9): "全國哀悼日（卡特）"}


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """某月第 n 個星期幾（0＝週一）；n＝−1 是最後一個。"""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(days=7 * (n - 1))
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def easter(year: int) -> date:
    a, (b, c) = year % 19, divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month, day = divmod(h + ll - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date, saturday: bool = True) -> Optional[date]:
    """假日落在週六提前到週五、落在週日順延到週一（元旦落在週六不提前）。"""
    if d.weekday() == 5:
        return d - timedelta(days=1) if saturday else None
    return d + timedelta(days=1) if d.weekday() == 6 else d


@lru_cache(maxsize=64)
def nyse_holidays(year: int) -> dict[date, str]:
    h: dict[date, str] = {}

    def add(d: Optional[date], name: str) -> None:
        if d is not None and d.year == year:
            h[d] = name

    add(_observed(date(year, 1, 1), saturday=False), "元旦")
    add(nth_weekday(year, 1, 0, 3), "馬丁路德金恩日")
    add(nth_weekday(year, 2, 0, 3), "總統日")
    add(easter(year) - timedelta(days=2), "耶穌受難日")
    add(nth_weekday(year, 5, 0, -1), "陣亡將士紀念日")
    if year >= 2022:
        add(_observed(date(year, 6, 19)), "六月節（Juneteenth）")
    add(_observed(date(year, 7, 4)), "獨立紀念日")
    add(nth_weekday(year, 9, 0, 1), "勞動節")
    add(nth_weekday(year, 11, 3, 4), "感恩節")
    add(_observed(date(year, 12, 25)), "聖誕節")
    h.update({d: n for d, n in SPECIAL_CLOSURES.items() if d.year == year})
    return h


@lru_cache(maxsize=64)
def nyse_early_closes(year: int) -> dict[date, str]:
    """13:00 ET 提早收盤：感恩節隔天、7/3（週一～週四，且 7/4 是平日）、12/24（週一～週四）。"""
    out = {nth_weekday(year, 11, 3, 4) + timedelta(days=1): "感恩節隔天"}
    j3, d24 = date(year, 7, 3), date(year, 12, 24)
    if j3.weekday() < 4:
        out[j3] = "獨立紀念日前一天"
    if d24.weekday() < 4:
        out[d24] = "平安夜"
    return out


def us_session_info(d: date) -> tuple[str, Optional[str]]:
    """('open'|'early'|'closed', 說明)。"""
    if d.weekday() >= 5:
        return "closed", "週末"
    hol = nyse_holidays(d.year)
    if d in hol:
        return "closed", hol[d]
    early = nyse_early_closes(d.year)
    return ("early", early[d]) if d in early else ("open", None)


def us_close_utc(d: date, extra_minutes: int = 0) -> datetime:
    """美股 d 那天的收盤時間（UTC）；提早收盤日 13:00 ET；休市日用標準的 16:00 ET。extra_minutes：VIX 比股市晚 15 分鐘定價。"""
    hh = 13 if us_session_info(d)[0] == "early" else 16
    return (datetime(d.year, d.month, d.day, hh, 0, tzinfo=NY) + timedelta(minutes=extra_minutes)).astimezone(UTC)


def latest_us_session(t: datetime) -> date:
    """t 時點已經收盤的最近一個美股『平日』（休市日也回傳，由呼叫端用 us_session_info 判斷、記 null，不往前找）。"""
    t_utc = t.astimezone(UTC)
    d = t_utc.astimezone(NY).date()
    for _ in range(10):
        if d.weekday() < 5 and us_close_utc(d) <= t_utc:
            return d
        d -= timedelta(days=1)
    raise ValueError(f"找不到 {t} 之前的美股平日")


# ── 台股日曆（證交所公開的休市日）──────────────────────────────────

TWSE_URL = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"
OPEN_MARKERS = ("開始交易日", "最後交易日")           # 『國曆新年開始交易日』『農曆春節前最後交易日』是交易日，其餘（含只辦理結算）都不交易


class SourceError(Exception):
    """kind：rate_limited／http／network／parse／empty。"""

    def __init__(self, kind: str, msg: str = "") -> None:
        super().__init__(f"{kind}: {msg}" if msg else kind)
        self.kind = kind


def http_get(url: str, headers: Optional[dict[str, str]] = None, timeout: float = 25.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, text/csv, */*", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise SourceError("rate_limited" if e.code == 429 else "http", f"HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SourceError("network", str(e)[:120]) from e


def parse_twse_holidays(text: str) -> dict[date, str]:
    """證交所休市日曆 JSON（民國日期 '1150101'）→ {日期: 名稱}，只留不交易的日子。"""
    out: dict[date, str] = {}
    for r in json.loads(text):
        raw = str(r.get("Date", "")).strip()
        name = str(r.get("Name", "")).strip()
        if len(raw) != 7 or not raw.isdigit() or any(name.endswith(m) for m in OPEN_MARKERS):
            continue
        out[date(int(raw[:3]) + 1911, int(raw[3:5]), int(raw[5:7]))] = name
    return out


class TwCalendar:
    """台股交易日：週末不交易；證交所的休市日曆（快取一週）；查不到（連不上、那一年沒公布）就退回『平日』並標 fallback_weekday。
    颱風假這類臨時休市事先不知道，事後用 void 補一筆作廢紀錄。"""

    def __init__(self, cache_path: Optional[Path] = None, http: Callable[..., str] = http_get, now: Callable[[], float] = time.time, ttl_days: float = 7.0) -> None:
        self.cache_path = cache_path if cache_path is not None else CACHE_DIR / "tw_holidays.json"
        self.http, self.now, self.ttl = http, now, ttl_days * 86400
        self._mem: Optional[dict[str, Any]] = None
        self._retry_at = 0.0                                   # 抓失敗之後，這個時間之前不再重試

    def _cache(self) -> dict[str, Any]:
        if self._mem is None:
            try:
                self._mem = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._mem = {}
        return self._mem

    def _refresh(self) -> None:
        text = self.http(TWSE_URL)
        closed = parse_twse_holidays(text)
        years = sorted({str(d.year) for d in closed})
        data = {"fetched_at": self.now(), "years": years, "closed": {d.isoformat(): n for d, n in sorted(closed.items())}}
        self._mem = data
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            logger.warning("台股休市日曆快取寫不進去：%r", e)

    def _closed(self, year: int) -> tuple[Optional[dict[str, str]], str]:
        c = self._cache()
        stale = not c or self.now() - c.get("fetched_at", 0) > self.ttl or str(year) not in c.get("years", [])
        src = "twse"                                          # 一週內抓過的（這次抓的或快取）都算 twse
        if stale and self.now() >= self._retry_at:
            try:
                self._refresh()
                c = self._cache()
            except (SourceError, ValueError) as e:
                logger.warning("台股休市日曆取不到（%s），10 分鐘後再試", e)
                self._retry_at = self.now() + 600
                src = "twse_stale_cache"                      # 抓不到、用過期的快取
        elif stale:
            src = "twse_stale_cache"
        if str(year) not in c.get("years", []):
            return None, "fallback_weekday"
        return c["closed"], src

    def check(self, d: date) -> dict[str, Any]:
        if d.weekday() >= 5:
            return {"open": False, "reason": "週末", "source": "weekend"}
        closed, src = self._closed(d.year)
        if closed is None:
            return {"open": True, "reason": None, "source": src}
        if d.isoformat() in closed:
            return {"open": False, "reason": closed[d.isoformat()], "source": src}
        return {"open": True, "reason": None, "source": src}

    def previous_open(self, d: date) -> date:
        x = d - timedelta(days=1)
        for _ in range(20):
            if self.check(x)["open"]:
                return x
            x -= timedelta(days=1)
        raise ValueError(f"{d} 之前 20 天都休市？")


# ── 外部來源：日線收盤 ───────────────────────────────────────────

class Bar(NamedTuple):
    date: date
    close: float
    source: str


def _us_date(s: str) -> date:
    m, d, y = s.strip().split("/")
    return date(int(y), int(m), int(d))


def parse_nasdaq(text: str) -> list[Bar]:
    """api.nasdaq.com/api/quote/{symbol}/historical：日期 'MM/DD/YYYY'、收盤 '$444.35' 或 '12,236.79'。"""
    try:
        rows = (((json.loads(text) or {}).get("data") or {}).get("tradesTable") or {}).get("rows") or []
        out = [Bar(_us_date(r["date"]), float(str(r["close"]).replace("$", "").replace(",", "")), "nasdaq") for r in rows]
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise SourceError("parse", f"nasdaq：{type(e).__name__}") from e
    if not out:
        raise SourceError("empty", "nasdaq 沒有資料列")
    return sorted(out)


def parse_cboe_csv(text: str) -> list[Bar]:
    """cdn.cboe.com 的 VIX_History.csv：DATE,OPEN,HIGH,LOW,CLOSE，日期 'MM/DD/YYYY'。"""
    try:
        out = [Bar(_us_date(r["DATE"]), float(r["CLOSE"]), "cboe") for r in csv.DictReader(io.StringIO(text.lstrip("﻿")))]
    except (ValueError, KeyError, TypeError) as e:
        raise SourceError("parse", f"cboe：{type(e).__name__}") from e
    if not out:
        raise SourceError("empty", "cboe 沒有資料列")
    return sorted(out)


def parse_yahoo_chart(text: str) -> list[Bar]:
    """query1.finance.yahoo.com/v8/finance/chart：日線時間戳是開盤時間，換成交易所當地日期。沒有對真實 Yahoo 驗證過（這台機器被 429）。"""
    try:
        res = json.loads(text)["chart"]["result"][0]
        tz = ZoneInfo(res.get("meta", {}).get("exchangeTimezoneName") or "America/New_York")
        out = [Bar(datetime.fromtimestamp(t, tz).date(), float(c), "yahoo") for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]) if c is not None]
    except (ValueError, KeyError, TypeError, IndexError) as e:
        raise SourceError("parse", f"yahoo：{type(e).__name__}") from e
    if not out:
        raise SourceError("empty", "yahoo 沒有資料列")
    return sorted(out)


def fetch_nasdaq(symbol: str, assetclass: str, since: date, http: Callable[..., str] = http_get, today: Optional[date] = None) -> list[Bar]:
    today = today or datetime.now(UTC).date() + timedelta(days=1)
    url = (f"https://api.nasdaq.com/api/quote/{symbol}/historical?assetclass={assetclass}"
           f"&fromdate={since:%Y-%m-%d}&limit=9999&todate={today:%Y-%m-%d}")
    return [b for b in parse_nasdaq(http(url)) if b.date >= since]


def fetch_cboe_vix(since: date, http: Callable[..., str] = http_get) -> list[Bar]:
    return [b for b in parse_cboe_csv(http("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv")) if b.date >= since]


def fetch_yahoo(symbol: str, since: date, http: Callable[..., str] = http_get) -> list[Bar]:
    days = max(10, (datetime.now(UTC).date() - since).days + 5)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range={days}d&interval=1d"
    return [b for b in parse_yahoo_chart(http(url)) if b.date >= since]


def _intel_path():
    from core import intel_reader
    return intel_reader.default_path()


INTEL_SERIES = {"sox": ("nasdaq", "SOX.close"), "tsm": ("nasdaq", "TSM.close"), "vix": ("cboe", "VIX.close")}


def last_us_weekday(now: Optional[datetime] = None) -> date:
    """台北現在 → 美股最近一個『應該已經收盤』的交易日（只看週一～五，不看美國假日；假日會被當成『還沒更新』而改問下一個來源，無害）。"""
    t = (now or datetime.now(UTC)).astimezone(ZoneInfo("America/New_York"))
    d = t.date() if t.hour >= 17 else t.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def fetch_intel(key: str, since: date, http: Any = None, path: Any = None, now: Optional[datetime] = None) -> list[Bar]:
    """從 lh-intel 的 intel.db（唯讀）讀費半、台積電 ADR、VIX 的日收盤——它已經在收同一批資料（Nasdaq、Cboe），不必再自己打一次。
    只用『現在已經抓到』的列（fetched_at ≤ 現在）；最新一筆比美股最近的交易日舊＝還沒更新，丟 SourceError('stale') 讓下一個來源（直接打 Nasdaq／Cboe）接手。"""
    import sqlite3
    from contextlib import closing
    from core import intel_reader
    p = path or _intel_path()
    if not p.exists():
        raise SourceError("unavailable", "找不到 lh-intel 資料庫")
    src, series = INTEL_SERIES[key]
    t = (now or datetime.now(UTC)).timestamp()
    try:
        with closing(intel_reader.connect_ro(p)) as conn:
            rows = intel_reader.latest_points(conn, src, series, t, 60)
    except sqlite3.Error as e:
        raise SourceError("unavailable", f"讀不到 lh-intel（{type(e).__name__}）") from e
    bars = sorted(Bar(date.fromisoformat(r["event_date"]), float(r["value"]), "intel") for r in rows if r["value"] is not None and r["event_date"] >= since.isoformat())
    if not bars:
        raise SourceError("empty", "lh-intel 沒有資料列")
    want = last_us_weekday(now)
    if bars[-1].date < want:
        raise SourceError("stale", f"lh-intel 最新只到 {bars[-1].date}（要 {want}）")
    return bars


# 特徵 → {來源名稱: (since, http) → bars}
SOURCES: dict[str, dict[str, Callable[..., list[Bar]]]] = {
    "sox": {"intel": lambda since, http: fetch_intel("sox", since), "nasdaq": lambda since, http: fetch_nasdaq("SOX", "index", since, http), "yahoo": lambda since, http: fetch_yahoo("^SOX", since, http)},
    "tsm": {"intel": lambda since, http: fetch_intel("tsm", since), "nasdaq": lambda since, http: fetch_nasdaq("TSM", "stocks", since, http), "yahoo": lambda since, http: fetch_yahoo("TSM", since, http)},
    "vix": {"intel": lambda since, http: fetch_intel("vix", since), "cboe": lambda since, http: fetch_cboe_vix(since, http), "yahoo": lambda since, http: fetch_yahoo("^VIX", since, http)},
}
_breaker: dict[str, tuple[float, str]] = {}                     # 來源 → (暫停到什麼時候, 原本的失敗原因)


def source_order() -> list[str]:
    return [s.strip().lower() for s in os.getenv("PREMARKET_SOURCES", "intel,nasdaq,cboe").split(",") if s.strip()]


def fetch_bars(key: str, since: date, http: Callable[..., str] = http_get, order: Optional[Sequence[str]] = None,
               now: Callable[[], float] = time.time) -> tuple[list[Bar], str, list[str]]:
    """依序試這個特徵可用的來源，回傳 (日線, 來源名稱, 失敗紀錄)；全部失敗回傳 ([], '', 失敗紀錄)。來源回 429 後暫停使用一陣子，不去加重被擋的情況。"""
    fails: list[str] = []
    for name in (order if order is not None else source_order()):
        fn = SOURCES.get(key, {}).get(name)
        if fn is None:
            continue
        hold = _breaker.get(name)
        if hold and hold[0] > now():
            fails.append(f"{name}: 暫停使用（{hold[1]}）")
            continue
        try:
            bars = fn(since, http)
            return bars, name, fails
        except SourceError as e:
            fails.append(f"{name}: {e}")
            if e.kind == "rate_limited":
                _breaker[name] = (now() + BREAKER_SEC, str(e))
    return [], "", fails


# ── 特徵的定義 ───────────────────────────────────────────────────

@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    kind: str                      # 'return'：最近一個美股交易日的對數報酬；'level'：收盤值
    default_range: tuple[float, float]
    close_extra_min: int = 0       # VIX 比股市晚 15 分鐘定價


# 異常值範圍是擋「資料錯誤」（小數點、單位）用的，不是擋真實的崩盤：費半 2017~2026 年有兩天真的超過 ±15%（2020-03-16 −17.3%、2025-04-09 +17.2%），所以放在 ±25%
FEATURES = (Feature("sox", "費半 SOX", "return", (-0.25, 0.25)),
            Feature("tsm", "台積電 ADR（TSM）", "return", (-0.25, 0.25)),
            Feature("vix", "VIX", "level", (5.0, 120.0), 15))
NIGHT_RANGE = (-0.08, 0.08)
FEATURE_KEYS = ("txf_night",) + tuple(f.key for f in FEATURES)


def value_range(key: str, default: tuple[float, float]) -> tuple[float, float]:
    raw = os.getenv(f"PREMARKET_RANGE_{key.upper()}", "").strip()
    try:
        lo, hi = (float(x) for x in raw.split(","))
        return (lo, hi) if lo < hi else default
    except ValueError:
        return default


def _field(value: Optional[float] = None, *, source: str = "", source_timestamp: Optional[float] = None, fetched_at: Optional[float] = None,
           missing_reason: Optional[str] = None, flag: Optional[str] = None, detail: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """每個欄位的固定形狀（需求文件的資料結構）。"""
    if value is not None and not math.isfinite(value):
        value, missing_reason = None, missing_reason or "non_finite"
    return {"value": value, "source": source, "source_timestamp": source_timestamp, "fetched_at": fetched_at,
            "missing_reason": missing_reason, "flag": flag, "detail": detail or {}}


def _range_check(f: dict[str, Any], value: float, lo: float, hi: float) -> dict[str, Any]:
    """異常值只標記：value 記 null、原始數字留在 detail.raw_value，不修正。"""
    if lo <= value <= hi:
        f["value"] = value
        return f
    f["value"], f["flag"], f["missing_reason"] = None, "out_of_range", f"out_of_range: {value:.6g} 不在 [{lo:g}, {hi:g}]"
    f["detail"]["raw_value"] = value
    return f


def compute_us_feature(f: Feature, bars: Sequence[Bar], session: date, t_utc: datetime, fetched_at: Optional[float] = None,
                       ranges: Optional[dict[str, tuple[float, float]]] = None) -> dict[str, Any]:
    """bars（單一來源、依日期排序）→ 欄位。收盤時間晚於 T 的日線一律丟掉（資料洩漏防線）；找不到 session 那天（過期或缺）、歷史不夠都是 null＋原因。"""
    # 資料洩漏防線：收盤時間晚於 T 的日線丟掉。美股休市日的列也丟掉（Cboe 的 VIX 歷史從 2022 年起在美股休市日也有環球交易時段算出來的值，不是收盤值）
    usable = [b for b in bars if us_session_info(b.date)[0] != "closed" and us_close_utc(b.date, f.close_extra_min) <= t_utc]
    out = _field(source=bars[0].source if bars else "", fetched_at=fetched_at,
                 detail={"leaked_rejected": sum(1 for b in bars if us_close_utc(b.date, f.close_extra_min) > t_utc),
                         "non_trading_rejected": sum(1 for b in bars if us_session_info(b.date)[0] == "closed")})
    idx = next((i for i, b in enumerate(usable) if b.date == session), None)
    if idx is None:
        out["missing_reason"] = f"stale_or_missing: 最新資料 {usable[-1].date if usable else '無'}，預期 {session}"
        return out
    if idx < MIN_BARS - 1:
        out["missing_reason"] = f"insufficient_history: {session} 之前只有 {idx} 筆"
        return out
    c = [b.close for b in usable[: idx + 1]]
    if min(c[idx - 21: idx + 1]) <= 0:
        out["missing_reason"] = "bad_price: 收盤價 ≤ 0"
        return out
    rets = [math.log(c[i] / c[i - 1]) for i in range(idx - 20, idx)]
    ret = math.log(c[idx] / c[idx - 1])
    out["source_timestamp"] = us_close_utc(session, f.close_extra_min).timestamp()
    out["detail"].update(bar_date=session.isoformat(), close=c[idx], prev_close=c[idx - 1], ret=ret, sigma20=statistics.stdev(rets),
                         rel20=math.log(c[idx] / statistics.mean(c[idx - 20: idx])), n_hist=idx)
    lo, hi = ranges[f.key] if ranges and f.key in ranges else value_range(f.key, f.default_range)           # ranges：歷史預檢用固定範圍，不受環境變數影響
    return _range_check(out, ret if f.kind == "return" else c[idx], lo, hi)


# ── 台指期夜盤報酬（本機 ticks.db）───────────────────────────────

def txf_night_feature(ticks_path: Path, prev_day: date, t_utc: datetime, fetched_at: Optional[float] = None) -> dict[str, Any]:
    """夜盤（前一個交易日 15:00 ～ 隔天 05:00）收盤 ÷ 前一個交易日日盤收盤（13:45）− 1，同一個合約（夜盤成交量最大的）。
    只用 ts ≤ T 的成交；夜盤或日盤最後一筆離收盤太遠（後端沒開著）就是 null。"""
    out = _field(source="ticks.db", fetched_at=fetched_at)
    n_start = datetime.combine(prev_day, dtime(15, 0), tzinfo=TPE)
    n_end = datetime.combine(prev_day + timedelta(days=1), dtime(5, 0), tzinfo=TPE)
    d_start, d_end = datetime.combine(prev_day, dtime(8, 45), tzinfo=TPE), datetime.combine(prev_day, dtime(13, 45), tzinfo=TPE)
    if t_utc < n_end:
        out["missing_reason"] = "night_not_finished: T 在夜盤收盤之前"
        return out
    try:
        with closing(sqlite3.connect(f"file:{ticks_path}?mode=ro", uri=True, timeout=5)) as con:
            night = con.execute("SELECT code, ts, price, volume FROM ticks WHERE code LIKE 'TXF%' AND ts>=? AND ts<=? ORDER BY ts",
                                (n_start.timestamp(), min(n_end, t_utc).timestamp())).fetchall()
            if not night:
                out["missing_reason"] = "no_ticks: 夜盤沒有 TXF 成交紀錄（後端那段時間沒開？）"
                return out
            vol: dict[str, int] = {}
            for code, _, _, v in night:
                vol[code] = vol.get(code, 0) + int(v)
            code = max(vol, key=lambda k: vol[k])
            rows = [r for r in night if r[0] == code]
            prev = con.execute("SELECT ts, price FROM ticks WHERE code=? AND ts>=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                               (code, d_start.timestamp(), d_end.timestamp())).fetchone()
    except sqlite3.Error as e:
        out["missing_reason"] = f"ticks_db_unavailable: {type(e).__name__}: {str(e)[:80]}"
        return out
    if prev is None:
        out["missing_reason"] = f"no_ticks: {code} 在 {prev_day} 日盤沒有成交紀錄"
        return out
    last_ts, night_close = rows[-1][1], rows[-1][2]
    if last_ts < n_end.timestamp() - 300 or prev[0] < d_end.timestamp() - 300:
        out["missing_reason"] = "incomplete_session: 夜盤或前一日日盤最後一筆離收盤超過 5 分鐘"
        out["detail"].update(night_last=datetime.fromtimestamp(last_ts, TPE).isoformat(), prev_day_last=datetime.fromtimestamp(prev[0], TPE).isoformat())
        return out
    out["source_timestamp"] = last_ts
    out["detail"].update(contract=code, night_close=night_close, prev_close=prev[1], night_open=rows[0][2], night_ticks=len(rows),
                         night_open_to_close_return=night_close / rows[0][2] - 1 if rows[0][2] else None, prev_day=prev_day.isoformat())
    if prev[1] <= 0:
        out["missing_reason"] = "bad_price: 前一日收盤 ≤ 0"
        return out
    lo, hi = value_range("night", NIGHT_RANGE)
    return _range_check(out, night_close / prev[1] - 1, lo, hi)


# ── 組快照 ───────────────────────────────────────────────────────

def _retryable(f: dict[str, Any]) -> bool:
    r = f.get("missing_reason") or ""
    return r.startswith(("source_error", "stale_or_missing", "ticks_db_unavailable"))


US_KEYS = tuple(f.key for f in FEATURES)               # 外部來源的美股欄位；夜盤報酬是本機資料，不在這裡補
BACKFILL_NOTE = "自動補值（來源較晚更新）"
BACKFILL_SEC = 600.0                                   # 每 10 分鐘掃一次最近的紀錄
BACKFILL_DAYS = 5                                      # 掃最近幾天


def build_snapshot(d: date, *, now: datetime, cal: TwCalendar, ticks_path: Path = TICKS_PATH, http: Callable[..., str] = http_get,
                   order: Optional[Sequence[str]] = None) -> dict[str, Any]:
    """交易日 d 的盤前快照（不寫入）。回傳的 retryable＝來源失敗、值得稍後重試的欄位。"""
    T = t_of(d)
    t_utc = T.astimezone(UTC)
    now_ts = now.timestamp()
    w = latest_us_session(T)
    state, us_note = us_session_info(w)
    prev_day = cal.previous_open(d)
    feats: dict[str, dict[str, Any]] = {"txf_night": txf_night_feature(ticks_path, prev_day, t_utc, now_ts)}
    for f in FEATURES:
        if state == "closed":
            feats[f.key] = _field(missing_reason=f"us_closed: {w} {us_note}", fetched_at=now_ts)
            continue
        bars, src, fails = fetch_bars(f.key, w - timedelta(days=70), http, order)
        if not bars:
            feats[f.key] = _field(missing_reason="source_error: " + ("；".join(fails) or "沒有可用的來源"), fetched_at=now_ts)
            continue
        feats[f.key] = compute_us_feature(f, bars, w, t_utc, now_ts)
        feats[f.key]["detail"]["failed_sources"] = fails
    tw = cal.check(d)
    return {"trading_date": d.isoformat(), "snapshot_time_utc": T.timestamp(), "fetched_at_utc": now_ts, "late_seconds": max(0.0, now_ts - T.timestamp()),
            "features": feats,
            "calendar": {"tw_closed": not tw["open"], "tw_reason": tw["reason"], "tw_source": tw["source"], "us_closed": state == "closed",
                         "us_session_date": w.isoformat(), "us_early_close": state == "early", "prev_tw_trading_day": prev_day.isoformat(),
                         "notes": us_note or ""},
            "retryable": [k for k, v in feats.items() if _retryable(v)]}


# ── 儲存（只新增，不改寫）─────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS premarket_snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trading_date TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('snapshot','correction','void')),
  corrects_id INTEGER,
  schema_version INTEGER NOT NULL,
  snapshot_time_utc REAL NOT NULL,
  fetched_at_utc REAL NOT NULL,
  late_seconds REAL NOT NULL DEFAULT 0,
  writer TEXT,
  features_json TEXT NOT NULL,
  calendar_json TEXT NOT NULL,
  note TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ux_premarket_one ON premarket_snapshots(trading_date) WHERE kind='snapshot';
CREATE INDEX IF NOT EXISTS ix_premarket_date ON premarket_snapshots(trading_date, id);
CREATE TABLE IF NOT EXISTS premarket_errors(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, where_ TEXT, trading_date TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS premarket_graveyard(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, study TEXT NOT NULL, family TEXT, feature_set TEXT, target TEXT,
  params_json TEXT, n INTEGER, stat_json TEXT, verdict TEXT, note TEXT);
CREATE TRIGGER IF NOT EXISTS premarket_snapshots_no_update BEFORE UPDATE ON premarket_snapshots BEGIN SELECT RAISE(ABORT, 'premarket_snapshots 只新增，不修改'); END;
CREATE TRIGGER IF NOT EXISTS premarket_snapshots_no_delete BEFORE DELETE ON premarket_snapshots BEGIN SELECT RAISE(ABORT, 'premarket_snapshots 只新增，不刪除'); END;
CREATE TRIGGER IF NOT EXISTS premarket_graveyard_no_update BEFORE UPDATE ON premarket_graveyard BEGIN SELECT RAISE(ABORT, 'premarket_graveyard 只新增，不修改'); END;
CREATE TRIGGER IF NOT EXISTS premarket_graveyard_no_delete BEFORE DELETE ON premarket_graveyard BEGIN SELECT RAISE(ABORT, 'premarket_graveyard 只新增，不刪除'); END;
"""


def _dumps(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False, sort_keys=True, allow_nan=False)


class Alerts:
    """寫入失敗不能阻擋交易，但也不能靜默：ERROR log、累計次數、保留最近一筆。"""

    def __init__(self) -> None:
        self.count = 0
        self.last: Optional[dict[str, Any]] = None

    def fire(self, where: str, error: str, trading_date: str = "") -> None:
        self.count += 1
        self.last = {"ts": time.time(), "where": where, "trading_date": trading_date, "error": error[:300]}
        logger.error("PREMARKET ALERT [%s] %s：%s（盤前跨市場資訊紀錄失敗，不影響交易）", where, trading_date, error)


alerts = Alerts()


class PremarketStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._ready = False
        self._lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        if not self._ready:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(SCHEMA)
                conn.commit()
                self._ready = True
        return conn

    def has_snapshot(self, d: date) -> bool:
        with closing(self._conn()) as c:
            return c.execute("SELECT 1 FROM premarket_snapshots WHERE trading_date=? AND kind='snapshot'", (d.isoformat(),)).fetchone() is not None

    def write_snapshot(self, rec: dict[str, Any], writer: str = "") -> Optional[int]:
        """寫入一筆原始快照；同一天已經有了就回傳 None（冪等，不重複也不遺失）。"""
        with closing(self._conn()) as c:
            cur = c.execute("INSERT OR IGNORE INTO premarket_snapshots(trading_date, kind, corrects_id, schema_version, snapshot_time_utc, fetched_at_utc, "
                            "late_seconds, writer, features_json, calendar_json, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (rec["trading_date"], "snapshot", None, SCHEMA_VERSION, rec["snapshot_time_utc"], rec["fetched_at_utc"], rec["late_seconds"], writer,
                             _dumps(rec["features"]), _dumps(rec["calendar"]), None))
            c.commit()
            return cur.lastrowid if cur.rowcount else None

    def _original(self, c: sqlite3.Connection, d: date) -> sqlite3.Row:
        row = c.execute("SELECT * FROM premarket_snapshots WHERE trading_date=? AND kind='snapshot'", (d.isoformat(),)).fetchone()
        if row is None:
            raise ValueError(f"{d} 沒有原始快照，沒辦法更正／作廢")
        return row

    def add_correction(self, d: date, patch: dict[str, dict[str, Any]], reason: str, writer: str = "") -> int:
        """更正：新增一筆 correction（指向原紀錄），patch＝{特徵: {欄位: 新值}}；原紀錄不動。
        以「目前的有效內容」（最新一筆更正，沒有更正就是原始快照）為底再套 patch——連續更正（手動、或自動補值分幾次）才會累積，不會把前一次的丟掉。"""
        with closing(self._conn()) as c:
            orig = self._original(c, d)
            last = c.execute("SELECT features_json FROM premarket_snapshots WHERE trading_date=? AND kind='correction' ORDER BY id DESC LIMIT 1", (d.isoformat(),)).fetchone()
            feats = json.loads((last or orig)["features_json"])
            for k, fields in patch.items():
                feats.setdefault(k, {}).update(fields)
            cur = c.execute("INSERT INTO premarket_snapshots(trading_date, kind, corrects_id, schema_version, snapshot_time_utc, fetched_at_utc, late_seconds, writer, "
                            "features_json, calendar_json, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (d.isoformat(), "correction", orig["id"], SCHEMA_VERSION, orig["snapshot_time_utc"], time.time(), 0, writer, _dumps(feats),
                             orig["calendar_json"], reason))
            c.commit()
            return int(cur.lastrowid)

    def add_void(self, d: date, reason: str, writer: str = "") -> int:
        """作廢：這一天其實休市（颱風假之類）。新增一筆 void 指向原紀錄；分析時排除。"""
        with closing(self._conn()) as c:
            orig = self._original(c, d)
            cur = c.execute("INSERT INTO premarket_snapshots(trading_date, kind, corrects_id, schema_version, snapshot_time_utc, fetched_at_utc, late_seconds, writer, "
                            "features_json, calendar_json, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (d.isoformat(), "void", orig["id"], SCHEMA_VERSION, orig["snapshot_time_utc"], time.time(), 0, writer, "{}", orig["calendar_json"], reason))
            c.commit()
            return int(cur.lastrowid)

    def raw_rows(self, since: Optional[date] = None) -> list[dict[str, Any]]:
        with closing(self._conn()) as c:
            sql, args = "SELECT * FROM premarket_snapshots", []
            if since:
                sql, args = sql + " WHERE trading_date>=?", [since.isoformat()]
            return [dict(r) for r in c.execute(sql + " ORDER BY trading_date, id", args).fetchall()]

    def effective(self, since: Optional[date] = None) -> list[dict[str, Any]]:
        """每個交易日一列：原始快照，若有更正就用最新一筆更正的內容；有 void 的標 voided。新 → 舊。"""
        by: dict[str, dict[str, Any]] = {}
        for r in self.raw_rows(since):
            cur = by.setdefault(r["trading_date"], {"trading_date": r["trading_date"], "voided": False, "corrected": False, "void_reason": None, "correction_note": None})
            if r["kind"] == "void":
                cur["voided"], cur["void_reason"] = True, r["note"]
                continue
            cur.update(features=json.loads(r["features_json"]), calendar=json.loads(r["calendar_json"]))
            if r["kind"] == "snapshot":
                cur.update(snapshot_time_utc=r["snapshot_time_utc"], fetched_at_utc=r["fetched_at_utc"], late_seconds=r["late_seconds"], writer=r["writer"], id=r["id"],
                           schema_version=r["schema_version"], corrected_at_utc=None)
            else:                                            # 更正：內容換成最新一筆更正，取得時間、寫入者仍是原始快照的
                cur.update(corrected=True, correction_note=r["note"], corrected_at_utc=r["fetched_at_utc"])
        return sorted((v for v in by.values() if "features" in v), key=lambda x: x["trading_date"], reverse=True)

    def log_error(self, where: str, error: str, trading_date: str = "") -> None:
        with closing(self._conn()) as c:
            c.execute("INSERT INTO premarket_errors(ts, where_, trading_date, error) VALUES (?,?,?,?)", (time.time(), where, trading_date, error[:500]))
            c.commit()

    def add_graveyard(self, study: str, *, family: str = "", feature_set: str = "", target: str = "", params: Optional[dict[str, Any]] = None, n: Optional[int] = None,
                      stat: Optional[dict[str, Any]] = None, verdict: str = "", note: str = "") -> int:
        """墳場紀錄：試過的每一個特徵／目標／參數組合都記，包含沒通過的（修正多重檢定用）。"""
        with closing(self._conn()) as c:
            cur = c.execute("INSERT INTO premarket_graveyard(ts, study, family, feature_set, target, params_json, n, stat_json, verdict, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (time.time(), study, family, feature_set, target, _dumps(params or {}), n, _dumps(stat or {}), verdict, note))
            c.commit()
            return int(cur.lastrowid)

    def missed_dates(self, limit: int = 10) -> list[str]:
        with closing(self._conn()) as c:
            return [r[0] for r in c.execute("SELECT DISTINCT trading_date FROM premarket_errors WHERE where_='missed' ORDER BY trading_date DESC LIMIT ?", (limit,)).fetchall()]

    def status(self) -> dict[str, Any]:
        with closing(self._conn()) as c:
            n = c.execute("SELECT COUNT(*) FROM premarket_snapshots WHERE kind='snapshot'").fetchone()[0]
            corr = c.execute("SELECT COUNT(*) FROM premarket_snapshots WHERE kind='correction'").fetchone()[0]
            void = c.execute("SELECT COUNT(*) FROM premarket_snapshots WHERE kind='void'").fetchone()[0]
            errs = c.execute("SELECT COUNT(*) FROM premarket_errors").fetchone()[0]
            grave = c.execute("SELECT COUNT(*) FROM premarket_graveyard").fetchone()[0]
        return {"snapshots": n, "corrections": corr, "voids": void, "errors_logged": errs, "graveyard": grave}


_default_store: Optional[PremarketStore] = None


def default_store() -> PremarketStore:
    global _default_store
    if _default_store is None:
        from core.trade_log import trade_log           # 和成交紀錄放在同一個資料庫（有每日備份）
        _default_store = PremarketStore(trade_log.path)
    return _default_store


def _record_error(store: Optional[PremarketStore], where: str, error: str, trading_date: str = "") -> None:
    """通報 ＋ 寫錯誤表；錯誤表也寫不進去（資料庫壞了）就寫備援檔。任何一步失敗都不往外丟。"""
    alerts.fire(where, error, trading_date)
    try:
        if store is None:
            raise RuntimeError("沒有 store")
        store.log_error(where, error, trading_date)
    except Exception:  # noqa: BLE001
        try:
            ERROR_FILE.parent.mkdir(parents=True, exist_ok=True)
            with ERROR_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "where": where, "trading_date": trading_date, "error": error[:500]}, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            logger.exception("PREMARKET 備援檔也寫不進去")


# ── 執行 ─────────────────────────────────────────────────────────

def _record_missed(store: Optional[PremarketStore], d: date) -> None:
    """這個台股交易日後端在 13:45 之後才啟動、沒機會記：留一筆紀錄與警告（不算『失敗警示』：重新部署本來就會碰到）。"""
    msg = f"{d} 後端在 {LATEST_HHMM[0]}:{LATEST_HHMM[1]} 之後才啟動，這天沒有盤前快照"
    logger.warning("盤前跨市場資訊：%s", msg)
    try:
        if store is not None:
            store.log_error("missed", msg, d.isoformat())
    except Exception:  # noqa: BLE001
        logger.exception("missed 紀錄寫不進去（已忽略）")


def run_once(d: date, *, now: Optional[datetime] = None, store: Optional[PremarketStore] = None, cal: Optional[TwCalendar] = None,
             ticks_path: Path = TICKS_PATH, http: Callable[..., str] = http_get, order: Optional[Sequence[str]] = None,
             writer: str = "", force: bool = False) -> dict[str, Any]:
    """嘗試記錄交易日 d 的快照。status：exists／too_early／too_late／skipped（休市）／retry（來源失敗、稍後再試）／written。
    force（只給測試與手動）略過時間窗。"""
    now = now or datetime.now(TPE)
    store = store or default_store()
    if store.has_snapshot(d):
        return {"status": "exists"}
    cal = cal or TwCalendar()
    info = cal.check(d)
    if not info["open"]:                                     # 休市日：不管幾點都不記，也不算「錯過」（週末或假日重啟不該誤報）
        return {"status": "skipped", "reason": info["reason"], "source": info["source"]}
    T = t_of(d)
    if not force and now < T:
        return {"status": "too_early"}
    if not force and now > latest_of(d):
        return {"status": "too_late"}
    rec = build_snapshot(d, now=now, cal=cal, ticks_path=ticks_path, http=http, order=order)
    retry = rec.pop("retryable")
    if retry and not force and now < T + timedelta(minutes=RETRY_WINDOW_MIN):
        return {"status": "retry", "retryable": retry}
    rid = store.write_snapshot(rec, writer)
    missing = {k: v["missing_reason"] for k, v in rec["features"].items() if v["value"] is None}
    return {"status": "written" if rid else "exists", "id": rid, "missing": missing, "late_seconds": rec["late_seconds"]}


def run_guarded(d: date, **kw: Any) -> dict[str, Any]:
    """run_once 的外殼：任何例外都變成警示，不往外丟（交易路徑絕不能被這個拖累）。"""
    try:
        return run_once(d, **kw)
    except Exception as e:  # noqa: BLE001
        _record_error(kw.get("store") or _default_store, "run", f"{type(e).__name__}: {e}", d.isoformat())
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


async def premarket_log_loop() -> None:
    """背景：T 之後（到 13:45 為止）每天記一筆；來源失敗的欄位 T 後 15 分鐘內每 2 分鐘重試。"""
    import asyncio
    if not enabled():
        logger.info("盤前跨市場資訊紀錄已關閉（PREMARKET_LOG=false）")
        return
    try:
        from core.trade_log import current_mode
        mode = current_mode()
    except Exception:  # noqa: BLE001
        mode = ""
    logger.info("盤前跨市場資訊紀錄已啟動：每個台股交易日 %s 記一筆 → trade_log.db（來源 %s）", snapshot_time().strftime("%H:%M"), ",".join(source_order()))
    done: set[date] = set()
    wait = 30.0
    next_backfill = 0.0                                   # 啟動後第一圈就掃一次（今天的紀錄若是空值的，重啟後馬上補）
    while True:
        await asyncio.sleep(wait)
        wait = 30.0
        try:
            now = datetime.now(TPE)
            if time.time() >= next_backfill:
                next_backfill = time.time() + BACKFILL_SEC
                await asyncio.to_thread(_backfill_tick, now)
            d = now.date()
            if d in done or now < t_of(d):
                continue
            res = await asyncio.to_thread(run_guarded, d, writer=mode)
            if res["status"] in ("written", "exists", "skipped", "too_late"):
                done.add(d)
                if res["status"] == "written":
                    logger.info("盤前快照 %s 已寫入（缺值：%s）", d, res.get("missing") or "無")
                elif res["status"] == "too_late":
                    await asyncio.to_thread(_record_missed, _default_store, d)
            elif res["status"] == "retry":
                wait = 120.0
                logger.info("盤前快照 %s：%s 取不到，2 分鐘後重試", d, res.get("retryable"))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("盤前跨市場資訊紀錄迴圈例外（已忽略）：%r", e)


# ── 匯出與狀態 ───────────────────────────────────────────────────

def _tpe(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(ts, TPE).isoformat(timespec="seconds") if ts else ""


CSV_EXTRA = {"txf_night": ("contract", "night_close", "prev_close", "night_open_to_close_return"),
             "sox": ("close", "prev_close", "sigma20", "rel20", "bar_date"), "tsm": ("close", "prev_close", "sigma20", "rel20", "bar_date"),
             "vix": ("close", "prev_close", "rel20", "bar_date")}


def csv_columns() -> list[str]:
    cols = ["trading_date", "snapshot_time", "fetched_at", "late_seconds", "writer", "corrected", "corrected_at", "voided", "void_reason", "tw_source", "us_closed", "us_session_date",
            "us_early_close", "calendar_notes"]
    for k in FEATURE_KEYS:
        cols += [f"{k}_{x}" for x in ("value", "source", "source_timestamp", "fetched_at", "missing_reason", "flag")] + [f"{k}_{x}" for x in CSV_EXTRA[k]]
    return cols


def to_csv(rows: Sequence[dict[str, Any]]) -> str:
    """時間一律 Asia/Taipei（+08:00）；缺值留空（不寫 0）。"""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=csv_columns(), lineterminator="\n")
    w.writeheader()
    for r in rows:
        cal = r["calendar"]
        line: dict[str, Any] = {"trading_date": r["trading_date"], "snapshot_time": _tpe(r["snapshot_time_utc"]), "fetched_at": _tpe(r["fetched_at_utc"]),
                                "late_seconds": round(r["late_seconds"], 1), "writer": r["writer"] or "", "corrected": int(r["corrected"]),
                                "corrected_at": _tpe(r.get("corrected_at_utc")), "voided": int(r["voided"]),
                                "void_reason": r["void_reason"] or "", "tw_source": cal.get("tw_source", ""), "us_closed": int(bool(cal.get("us_closed"))),
                                "us_session_date": cal.get("us_session_date", ""), "us_early_close": int(bool(cal.get("us_early_close"))), "calendar_notes": cal.get("notes", "")}
        for k in FEATURE_KEYS:
            f = r["features"].get(k) or {}
            line.update({f"{k}_value": "" if f.get("value") is None else f["value"], f"{k}_source": f.get("source") or "",
                         f"{k}_source_timestamp": _tpe(f.get("source_timestamp")), f"{k}_fetched_at": _tpe(f.get("fetched_at")),
                         f"{k}_missing_reason": f.get("missing_reason") or "", f"{k}_flag": f.get("flag") or ""})
            for x in CSV_EXTRA[k]:
                v = (f.get("detail") or {}).get(x)
                line[f"{k}_{x}"] = "" if v is None else v
        w.writerow(line)
    return buf.getvalue()


def next_snapshot(store: PremarketStore, cal: TwCalendar, now: datetime) -> Optional[dict[str, Any]]:
    """下一筆預計什麼時候記：今天還沒記而且還在 13:45 之前（含已經過了 T 的補記）就是今天，否則是下一個台股交易日的 T。"""
    d = now.date()
    for i in range(15):
        day = d + timedelta(days=i)
        if not cal.check(day)["open"]:
            continue
        if i == 0 and (now > latest_of(day) or store.has_snapshot(day)):
            continue
        return {"trading_date": day.isoformat(), "time": t_of(day).isoformat(timespec="minutes"), "late": i == 0 and now >= t_of(day)}
    return None


def status(store: Optional[PremarketStore] = None, today: Optional[date] = None, cal: Optional[TwCalendar] = None, now: Optional[datetime] = None) -> dict[str, Any]:
    store = store or default_store()
    now = now or (datetime.combine(today, dtime(0, 0), tzinfo=TPE) if today else datetime.now(TPE))
    today = today or now.date()
    out: dict[str, Any] = {"enabled": enabled(), "snapshot_time": snapshot_time().strftime("%H:%M"), "sources": source_order(), "alerts": alerts.count,
                           "last_alert": alerts.last, "today": today.isoformat(), "missed_dates": store.missed_dates(), **store.status()}
    rows = store.effective()
    out["today_recorded"] = bool(rows) and any(r["trading_date"] == today.isoformat() for r in rows)
    valid = [r for r in rows if not r["voided"]]                          # 作廢的那天不算「最新」（面板也是這個定義）
    out["latest"] = ({"trading_date": valid[0]["trading_date"], "missing": {k: v["missing_reason"] for k, v in valid[0]["features"].items() if v["value"] is None}} if valid else None)
    try:
        out["next_snapshot"] = next_snapshot(store, cal or TwCalendar(), now)
    except Exception as e:  # noqa: BLE001
        out["next_snapshot"] = None
        logger.warning("算不出下一筆快照的時間：%r", e)
    return out


# ── 事後補值（update.md 發現 35）──────────────────────────────────
# 美股收盤是台北 04:00，但 Nasdaq 公開 API 與 Cboe 的 CSV 要到 08:46 之後（實測：12:16 已有）才有前一天的資料，
# 而快照在 T＋15 分鐘（08:45）就得寫。來源後來有了就補一筆 correction：美股收盤本來就在 T 之前（資料時間戳不會晚於 T），
# 所以補進去的值和 T 當下「已經存在」的資訊一致；補值的時間記在 detail.backfilled_at、更正說明寫「自動補值」，原始那筆（空值）一個字不動。

def pending_backfill(store: "PremarketStore", today: date, days: int = BACKFILL_DAYS) -> list[date]:
    """最近 days 天裡，美股欄位是「來源失敗／資料還沒更新」造成的空值的日子（休市、異常值、本機沒夜盤不算——那些補不回來）。"""
    out = []
    for r in store.effective(today - timedelta(days=days)):
        if r["voided"]:
            continue
        if any((r["features"].get(k) or {}).get("value") is None and _retryable(r["features"].get(k) or {}) for k in US_KEYS):
            out.append(date.fromisoformat(r["trading_date"]))
    return out


def backfill_once(d: date, *, now: Optional[datetime] = None, store: Optional["PremarketStore"] = None, cal: Optional[TwCalendar] = None,
                  ticks_path: Path = TICKS_PATH, http: Callable[..., str] = http_get, order: Optional[Sequence[str]] = None) -> dict[str, Any]:
    """d 這天的美股欄位還缺的，現在有了就補一筆 correction（只補它們；其他欄位不動）。status：no_record／nothing_to_do／still_missing／filled。"""
    now = now or datetime.now(TPE)
    store = store or default_store()
    eff = next((r for r in store.effective(d - timedelta(days=1)) if r["trading_date"] == d.isoformat()), None)
    if eff is None or eff["voided"]:
        return {"status": "no_record"}
    todo = [k for k in US_KEYS if (eff["features"].get(k) or {}).get("value") is None and _retryable(eff["features"].get(k) or {})]
    if not todo:
        return {"status": "nothing_to_do"}
    rec = build_snapshot(d, now=now, cal=cal or TwCalendar(), ticks_path=ticks_path, http=http, order=order)
    filled = {k: rec["features"][k] for k in todo if rec["features"][k].get("value") is not None}
    if not filled:
        return {"status": "still_missing", "missing": todo}
    for f in filled.values():
        f["detail"] = {**(f.get("detail") or {}), "backfilled": True, "backfilled_at": now.timestamp()}
    store.add_correction(d, filled, BACKFILL_NOTE, writer="backfill")      # add_correction 以目前的有效內容為底，所以只要帶這次補到的欄位
    return {"status": "filled", "filled": sorted(filled), "missing": [k for k in todo if k not in filled]}


def backfill_pass(now: Optional[datetime] = None, *, store: Optional["PremarketStore"] = None, **kw: Any) -> list[dict[str, Any]]:
    """掃最近的紀錄、能補的都補。任何例外只記 log 與 premarket_errors（不算失敗警示），不影響其他日子。"""
    now = now or datetime.now(TPE)
    store = store or default_store()
    out = []
    for d in pending_backfill(store, now.date()):
        try:
            res = backfill_once(d, now=now, store=store, **kw)
        except Exception as e:  # noqa: BLE001
            logger.warning("盤前快照 %s 補值失敗（下次再試）：%r", d, e)
            try:
                store.log_error("backfill", f"{type(e).__name__}: {e}", d.isoformat())
            except Exception:  # noqa: BLE001
                pass
            continue
        if res["status"] == "filled":
            logger.info("盤前快照 %s 補值：%s（來源較晚更新，現在才有）%s", d, "、".join(res["filled"]), f"；仍缺 {res['missing']}" if res["missing"] else "")
        out.append({"date": d.isoformat(), **res})
    return out


def _backfill_tick(now: datetime) -> list[dict[str, Any]]:
    """背景迴圈每 10 分鐘呼叫一次（獨立成函式，測試可以換掉）。"""
    return backfill_pass(now)


# ── 預覽（只算不寫）─────────────────────────────────────────────

PREVIEW_TTL = 60.0
_preview_cache: dict[date, tuple[float, dict[str, Any]]] = {}


def default_preview_date(cal: TwCalendar, now: datetime) -> date:
    """預覽預設看哪一天：最近一個『T 已經過了』的台股交易日（今天 T 之前就看前一個交易日），這樣美股與夜盤都已經有資料、看得到真實的值。"""
    d = now.date()
    for _ in range(15):
        if cal.check(d)["open"] and now >= t_of(d):
            return d
        d -= timedelta(days=1)
    raise ValueError("找不到最近的台股交易日")


def preview(d: Optional[date] = None, *, now: Optional[datetime] = None, store: Optional[PremarketStore] = None, cal: Optional[TwCalendar] = None,
            ticks_path: Path = TICKS_PATH, http: Callable[..., str] = http_get, order: Optional[Sequence[str]] = None, use_cache: bool = True) -> dict[str, Any]:
    """某個交易日（預設最近一個 T 已過的）的快照『會記成什麼樣子』，不寫入。每次會去抓 Nasdaq／Cboe／證交所，所以 60 秒內重複呼叫用快取。"""
    now = now or datetime.now(TPE)
    store = store or default_store()
    cal = cal or TwCalendar()
    d = d or default_preview_date(cal, now)
    hit = _preview_cache.get(d)
    if use_cache and hit and time.time() - hit[0] < PREVIEW_TTL:
        return hit[1]
    rec = build_snapshot(d, now=now, cal=cal, ticks_path=ticks_path, http=http, order=order)
    rec.pop("retryable", None)
    out = {**rec, "dry_run": True, "recorded": store.has_snapshot(d), "snapshot_time": t_of(d).isoformat(timespec="minutes")}
    _preview_cache[d] = (time.time(), out)
    return out


# ── CLI ──────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    ap = argparse.ArgumentParser(prog="python -m core.premarket_log", description="盤前跨市場資訊紀錄")
    ap.add_argument("what", choices=("show", "run", "export", "status", "void", "correct", "backfill"))
    ap.add_argument("date", nargs="?", help="YYYY-MM-DD（show／run／void／correct 用；預設今天）")
    ap.add_argument("--reason", default="")
    ap.add_argument("--patch", default="", help='correct：JSON，例如 \'{"sox": {"value": 0.01}}\'')
    ap.add_argument("--days", type=int, default=3650)
    a = ap.parse_args(argv)
    d = date.fromisoformat(a.date) if a.date else datetime.now(TPE).date()
    if a.what == "show":
        rec = build_snapshot(d, now=datetime.now(TPE), cal=TwCalendar())
        rec.pop("retryable", None)
        print(json.dumps(rec, ensure_ascii=False, indent=1, default=str))
        return 0
    store = default_store()
    if a.what == "run":
        print(json.dumps(run_guarded(d, store=store, force=False), ensure_ascii=False, default=str))
    elif a.what == "export":
        sys.stdout.write("﻿" + to_csv(store.effective(date.today() - timedelta(days=a.days))))
    elif a.what == "status":
        print(json.dumps(status(store), ensure_ascii=False, indent=1, default=str))
    elif a.what == "void":
        print(store.add_void(d, a.reason or "手動作廢"))
    elif a.what == "backfill":
        print(json.dumps(backfill_once(d, store=store) if a.date else backfill_pass(store=store), ensure_ascii=False, default=str))
    elif a.what == "correct":
        print(store.add_correction(d, json.loads(a.patch), a.reason or "手動更正"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
