"""盤前跨市場資訊紀錄（core/premarket_log.py，update.md 發現 31）：需求文件的驗收標準逐條對應。

1 每天恰好一筆、休市日不產生　2 時間洩漏（晚於 T 的資料必須被拒絕）　3 美股夏令／冬令切換（2026-03-08、11-01 前後）　4 週一用上週五
5 休市日 null＋原因、不沿用前一天　6 來源失敗／過期／異常 null＋原因、沒有預設值　7 啟用前後重播同一組行情、訂單完全相同　8 沒有策略或風控模組讀這些欄位
9 寫入失敗交易照常、有警示與錯誤紀錄　10 同一天重複觸發不重複寫入也不遺失　11 CSV 匯出、時區一致　12 缺值、休市、夏令、洩漏、重複、異常值、週一與假日後第一個交易日

外部來源（Nasdaq、Cboe、證交所）都換成替身；美股收盤價用手算得出來的合成序列，期望值由測試自己獨立算。
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import math
import random
import sqlite3
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.premarket_log as pm
from core.premarket_log import Bar, SourceError, TwCalendar

ROOT = Path(__file__).resolve().parent.parent


def tpe(y, m, d, hh=0, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=pm.TPE)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    pm._breaker.clear()
    pm._preview_cache.clear()
    pm.alerts.count, pm.alerts.last = 0, None
    pm._default_store = pm.PremarketStore(tmp_path / "default_trade_log.db")       # 預設的資料庫換成暫存的：任何沒傳 store 的呼叫都不能碰真的 data/trade_log.db（update.md 發現 35）
    monkeypatch.setattr(pm, "ERROR_FILE", tmp_path / "premarket_errors.jsonl")
    for k in ("PREMARKET_TIME", "PREMARKET_SOURCES", "PREMARKET_LOG", "PREMARKET_RANGE_NIGHT", "PREMARKET_RANGE_SOX", "PREMARKET_RANGE_TSM", "PREMARKET_RANGE_VIX"):
        monkeypatch.delenv(k, raising=False)


# ── 美股日曆與時區（驗收 3、4）────────────────────────────────────

def test_easter_and_the_2026_nyse_holidays_are_exact():
    assert [pm.easter(y) for y in (2024, 2025, 2026, 2027)] == [date(2024, 3, 31), date(2025, 4, 20), date(2026, 4, 5), date(2027, 3, 28)]
    got = pm.nyse_holidays(2026)
    assert sorted(got) == [date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3),
                           date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25)]
    assert pm.nth_weekday(2026, 5, 0, -1) == date(2026, 5, 25) and pm.nth_weekday(2026, 11, 3, 4) == date(2026, 11, 26) and pm.nth_weekday(2026, 1, 0, 3) == date(2026, 1, 19)


def test_observed_holidays_and_special_closures():
    assert date(2021, 12, 24) in pm.nyse_holidays(2021) and date(2022, 12, 26) in pm.nyse_holidays(2022)            # 聖誕節落在週六 → 週五；週日 → 週一
    assert date(2020, 7, 3) in pm.nyse_holidays(2020) and date(2021, 7, 5) in pm.nyse_holidays(2021)                # 7/4 落在週六／週日
    assert date(2021, 12, 31) not in pm.nyse_holidays(2021) and date(2022, 1, 1) not in pm.nyse_holidays(2022)      # 元旦落在週六：前一個週五照開
    assert date(2023, 1, 2) in pm.nyse_holidays(2023)                                                               # 元旦落在週日 → 週一
    assert date(2022, 6, 20) in pm.nyse_holidays(2022) and not any(d.month == 6 for d in pm.nyse_holidays(2021))   # Juneteenth 從 2022 年起
    assert date(2025, 1, 9) in pm.nyse_holidays(2025) and date(2018, 12, 5) in pm.nyse_holidays(2018)              # 全國哀悼日
    assert pm.us_session_info(date(2026, 3, 7)) == ("closed", "週末") and pm.us_session_info(date(2026, 7, 3)) == ("closed", "獨立紀念日")


def test_early_close_days():
    assert pm.nyse_early_closes(2026) == {date(2026, 11, 27): "感恩節隔天", date(2026, 12, 24): "平安夜"}           # 7/3 是補假、不是提早收盤
    assert date(2025, 7, 3) in pm.nyse_early_closes(2025) and date(2024, 7, 3) in pm.nyse_early_closes(2024) and date(2019, 7, 3) in pm.nyse_early_closes(2019)
    assert date(2022, 7, 1) not in pm.nyse_early_closes(2022) and date(2020, 7, 2) not in pm.nyse_early_closes(2020)    # 7/4 落在週一、週六：不提早
    assert date(2021, 12, 24) not in pm.nyse_early_closes(2021)                                                       # 那天是補假（休市）
    assert pm.us_session_info(date(2026, 11, 27)) == ("early", "感恩節隔天")
    assert pm.us_close_utc(date(2026, 11, 27)).astimezone(pm.NY).strftime("%H:%M") == "13:00"
    assert pm.us_close_utc(date(2026, 11, 27)).astimezone(pm.TPE).strftime("%m-%d %H:%M") == "11-28 02:00"


def test_us_close_in_taipei_follows_daylight_saving_time():
    """需求文件的例子：3/6（週五）收盤 → 台灣 3/7 05:00；3/9（週一）收盤 → 台灣 3/10 04:00。2026 夏令 3/8 開始、11/1 結束。"""
    f = lambda d, extra=0: pm.us_close_utc(d, extra).astimezone(pm.TPE).strftime("%m-%d %H:%M")      # noqa: E731
    assert f(date(2026, 3, 6)) == "03-07 05:00" and f(date(2026, 3, 9)) == "03-10 04:00"
    assert f(date(2026, 10, 30)) == "10-31 04:00" and f(date(2026, 11, 2)) == "11-03 05:00"           # 11/1 結束夏令：10/30 還是夏令、11/2 已是冬令
    assert f(date(2026, 3, 6), 15) == "03-07 05:15" and f(date(2026, 3, 9), 15) == "03-10 04:15"      # VIX 比股市晚 15 分鐘
    assert pm.us_close_utc(date(2026, 3, 6)).utcoffset() == timedelta(0)


def test_latest_us_session_monday_uses_friday_and_dst_weeks_are_right():
    f = lambda y, m, d, hh=8, mm=30: pm.latest_us_session(tpe(y, m, d, hh, mm))                         # noqa: E731
    assert f(2026, 3, 9) == date(2026, 3, 6) and f(2026, 3, 10) == date(2026, 3, 9)                    # 週一盤前用上週五；夏令第一週的週二用週一
    assert f(2026, 11, 2) == date(2026, 10, 30) and f(2026, 11, 3) == date(2026, 11, 2)                # 冬令前後
    assert f(2026, 10, 12) == date(2026, 10, 9)                                                         # 台股連假後的第一個交易日
    assert f(2026, 5, 26) == date(2026, 5, 25) and pm.us_session_info(f(2026, 5, 26))[0] == "closed"   # 美股休市日：回傳那天（呼叫端記 null），不往前找
    # T 比較早（04:30）時，夏令與冬令差一個小時：夏令週的週一收盤 04:00 已過、冬令的週一收盤 05:00 還沒到
    assert f(2026, 3, 10, 4, 30) == date(2026, 3, 9) and f(2026, 3, 3, 4, 30) == date(2026, 2, 27)
    assert f(2026, 3, 3, 5, 0) == date(2026, 3, 2) and f(2026, 3, 3, 4, 59) == date(2026, 2, 27)                       # 收盤時間剛好等於 T 算「已收盤」（冬令 16:00 ET ＝ 台北隔日 05:00）


def test_latest_us_session_against_a_brute_force_scan():
    rng = random.Random(7)
    for _ in range(400):
        t = datetime(2024, 1, 1, tzinfo=pm.UTC) + timedelta(minutes=rng.randrange(0, 60 * 24 * 365 * 3))
        w = pm.latest_us_session(t)
        assert w.weekday() < 5 and pm.us_close_utc(w) <= t
        later = w + timedelta(days=1)
        while later.weekday() >= 5:
            later += timedelta(days=1)
        assert pm.us_close_utc(later) > t                                                               # 下一個平日的收盤還沒到


# ── 台股日曆 ──────────────────────────────────────────────────────

def twse_json(extra=()):
    rows = [("1150101", "中華民國開國紀念日"), ("1150102", "國曆新年開始交易日"), ("1150211", "農曆春節前最後交易日"), ("1150212", "市場無交易，僅辦理結算交割作業"),
            ("1150213", "市場無交易，僅辦理結算交割作業")] + [(f"11502{d}", "農曆除夕及春節") for d in (15, 16, 17, 18, 19, 20)] + [
        ("1150223", "農曆春節後開始交易日"), ("1150227", "和平紀念日"), ("1150403", "兒童節及民族掃墓節"), ("1150501", "勞動節"), ("1150619", "端午節"),
        ("1150925", "中秋節"), ("1150928", "孔子誕辰紀念日/ 教師節"), ("1151009", "國慶日"), ("1151010", "國慶日"), ("1151225", "行憲紀念日")] + list(extra)
    return json.dumps([{"Name": n, "Date": d, "Weekday": "一", "Description": ""} for d, n in rows], ensure_ascii=False)


class Http:
    """替身：依網址回傳固定內容；記錄每個網址被叫了幾次。"""

    def __init__(self, routes=None):
        self.routes, self.calls, self.lock = dict(routes or {}), [], threading.Lock()

    def __call__(self, url, headers=None, timeout=25):
        with self.lock:
            self.calls.append(url)
        for needle, body in self.routes.items():
            if needle in url:
                if isinstance(body, Exception):
                    raise body
                return body(url) if callable(body) else body
        raise SourceError("http", f"沒有這個替身路由：{url}")

    def count(self, needle):
        return sum(needle in c for c in self.calls)


def test_parse_twse_holidays_roc_dates_and_trading_day_markers():
    got = pm.parse_twse_holidays(twse_json())
    assert date(2026, 1, 1) in got and date(2026, 10, 9) in got and date(2026, 2, 12) in got                          # 只辦理結算＝不交易
    assert date(2026, 1, 2) not in got and date(2026, 2, 11) not in got and date(2026, 2, 23) not in got               # 開始／最後交易日是交易日
    assert got[date(2026, 2, 20)] == "農曆除夕及春節" and len(got) == 18
    assert pm.parse_twse_holidays(json.dumps([{"Name": "x", "Date": "bad"}, {"Name": "y", "Date": "99"}])) == {}


def test_tw_calendar_open_closed_cache_and_fallback(tmp_path):
    http = Http({"holidaySchedule": twse_json()})
    cal = TwCalendar(tmp_path / "tw.json", http, now=lambda: 1_000.0)
    assert cal.check(date(2026, 10, 9)) == {"open": False, "reason": "國慶日", "source": "twse"}
    assert cal.check(date(2026, 10, 10)) == {"open": False, "reason": "週末", "source": "weekend"}
    assert cal.check(date(2026, 10, 12))["open"] and cal.check(date(2026, 2, 11))["open"] and not cal.check(date(2026, 2, 16))["open"]
    assert http.count("holidaySchedule") == 1                                                                              # 同一年只抓一次
    cal2 = TwCalendar(tmp_path / "tw.json", Http(), now=lambda: 2_000.0)                                                   # 重啟：用快取（不能連線也行）
    assert cal2.check(date(2026, 10, 9)) == {"open": False, "reason": "國慶日", "source": "twse"} and cal2.http.calls == []
    stale = TwCalendar(tmp_path / "tw.json", Http({"holidaySchedule": SourceError("network", "x")}), now=lambda: 1_000.0 + 8 * 86400)
    assert stale.check(date(2026, 10, 9)) == {"open": False, "reason": "國慶日", "source": "twse_stale_cache"}              # 過期又抓不到：還是用快取，標明是過期的，不退回平日
    nothing = TwCalendar(tmp_path / "other.json", Http({"holidaySchedule": SourceError("network", "x")}))
    assert nothing.check(date(2026, 10, 9)) == {"open": True, "reason": None, "source": "fallback_weekday"}                # 沒有任何資訊：退回平日、標明
    assert nothing.check(date(2027, 3, 3))["source"] == "fallback_weekday"                                                   # 那一年沒公布


def test_a_failing_holiday_calendar_is_retried_only_after_ten_minutes(tmp_path):
    good = TwCalendar(tmp_path / "tw.json", Http({"holidaySchedule": twse_json()}), now=lambda: 1_000.0)
    good.check(date(2026, 10, 9))                                                                                       # 先有一份快取
    clock = [1_000.0 + 8 * 86400]                                                                                       # 快取過期（超過 7 天）
    http = Http({"holidaySchedule": SourceError("network", "x")})
    cal = TwCalendar(tmp_path / "tw.json", http, now=lambda: clock[0])
    src = lambda: cal.check(date(2026, 10, 9))["source"]                                                                # noqa: E731
    assert src() == "twse_stale_cache" and http.count("holidaySchedule") == 1
    clock[0] += 599
    assert src() == "twse_stale_cache" and http.count("holidaySchedule") == 1                                           # 10 分鐘內不再打、仍標明是過期快取
    clock[0] += 2
    assert src() == "twse_stale_cache" and http.count("holidaySchedule") == 2                                           # 過了才再試一次
    clock[0] += 1
    cal.check(date(2026, 10, 9))
    assert http.count("holidaySchedule") == 2                                                                           # 又退避 10 分鐘
    http.routes["holidaySchedule"] = twse_json()
    clock[0] += 601
    assert src() == "twse" and http.count("holidaySchedule") == 3                                                       # 抓到了：恢復成 twse，不再是過期快取


def test_previous_open_skips_weekends_holidays_and_the_new_year_break(tmp_path):
    cal = TwCalendar(tmp_path / "tw.json", Http({"holidaySchedule": twse_json()}))
    assert cal.previous_open(date(2026, 10, 13)) == date(2026, 10, 12) and cal.previous_open(date(2026, 10, 12)) == date(2026, 10, 8)      # 10/9 補假
    assert cal.previous_open(date(2026, 2, 23)) == date(2026, 2, 11)                                                                         # 春節後第一個交易日
    assert cal.previous_open(date(2026, 3, 9)) == date(2026, 3, 6)                                                                           # 週一 → 上週五


# ── 外部來源的解析、失敗轉移、限流（驗收 6）────────────────────────

def nasdaq_json(rows):
    return json.dumps({"data": {"symbol": "X", "totalRecords": len(rows), "tradesTable": {"rows": [{"date": d, "close": c, "volume": "1"} for d, c in rows]}}})


def test_parse_nasdaq_cboe_and_yahoo():
    assert pm.parse_nasdaq(nasdaq_json([("10/12/2026", "$444.35"), ("10/09/2026", "$453.20")])) == [Bar(date(2026, 10, 9), 453.2, "nasdaq"), Bar(date(2026, 10, 12), 444.35, "nasdaq")]
    assert pm.parse_nasdaq(nasdaq_json([("10/12/2026", "12,236.79")]))[0].close == 12236.79
    for bad in ('{"data": null}', "not json", nasdaq_json([("13/45/2026", "1")]), nasdaq_json([("10/12/2026", "N/A")])):
        with pytest.raises(SourceError):
            pm.parse_nasdaq(bad)
    csv_text = "﻿DATE,OPEN,HIGH,LOW,CLOSE\n01/02/1990,17.24,17.24,17.24,17.24\n10/12/2026,15.9,16.0,15.1,15.21\n"
    assert pm.parse_cboe_csv(csv_text) == [Bar(date(1990, 1, 2), 17.24, "cboe"), Bar(date(2026, 10, 12), 15.21, "cboe")]
    with pytest.raises(SourceError):
        pm.parse_cboe_csv("DATE,OPEN\n")
    # Yahoo：日線時間戳是開盤時間（09:30 ET），換成交易所當地日期（冬令 14:30 UTC、夏令 13:30 UTC）；沒有成交價的列略過
    open_ts = lambda y, m, d: int(datetime(y, m, d, 9, 30, tzinfo=pm.NY).timestamp())      # noqa: E731  日線時間戳是 09:30 ET（冬令 14:30 UTC、夏令 13:30 UTC）
    y = json.dumps({"chart": {"result": [{"meta": {"exchangeTimezoneName": "America/New_York"}, "timestamp": [open_ts(2026, 3, 5), open_ts(2026, 3, 9), open_ts(2026, 3, 10)],
                                           "indicators": {"quote": [{"close": [100.5, None, 101.5]}]}}]}})
    got = pm.parse_yahoo_chart(y)
    assert [(b.date, b.close) for b in got] == [(date(2026, 3, 5), 100.5), (date(2026, 3, 10), 101.5)]
    nz = int(datetime(2026, 3, 10, 10, 0, tzinfo=ZoneInfo("Pacific/Auckland")).timestamp())                          # UTC 是 3/9 21:00
    z = json.dumps({"chart": {"result": [{"meta": {"exchangeTimezoneName": "Pacific/Auckland"}, "timestamp": [nz], "indicators": {"quote": [{"close": [7.5]}]}}]}})
    assert pm.parse_yahoo_chart(z) == [Bar(date(2026, 3, 10), 7.5, "yahoo")]                                          # 日期用交易所當地時區
    with pytest.raises(SourceError):
        pm.parse_yahoo_chart('{"chart": {"result": null}}')


def test_http_get_maps_errors_to_source_errors(monkeypatch):
    import urllib.error
    import urllib.request

    def boom(code):
        def f(req, timeout=None):
            raise urllib.error.HTTPError("u", code, "x", None, None)
        return f

    monkeypatch.setattr(urllib.request, "urlopen", boom(429))
    with pytest.raises(SourceError) as e:
        pm.http_get("http://x")
    assert e.value.kind == "rate_limited"
    monkeypatch.setattr(urllib.request, "urlopen", boom(500))
    with pytest.raises(SourceError) as e:
        pm.http_get("http://x")
    assert e.value.kind == "http"

    def net(req, timeout=None):
        raise urllib.error.URLError("dns")

    monkeypatch.setattr(urllib.request, "urlopen", net)
    with pytest.raises(SourceError) as e:
        pm.http_get("http://x")
    assert e.value.kind == "network"


def test_fetch_bars_fails_over_and_stops_hitting_a_rate_limited_source():
    series = nasdaq_json([("10/12/2026", "$10")])
    http = Http({"yahoo": SourceError("rate_limited", "HTTP 429"), "api.nasdaq.com": series})
    t = [1000.0]
    bars, src, fails = pm.fetch_bars("tsm", date(2026, 9, 1), http, order=["yahoo", "nasdaq"], now=lambda: t[0])
    assert src == "nasdaq" and bars[0].close == 10.0 and fails == ["yahoo: rate_limited: HTTP 429"]
    assert http.count("yahoo") == 1
    bars, src, fails = pm.fetch_bars("tsm", date(2026, 9, 1), http, order=["yahoo", "nasdaq"], now=lambda: t[0] + 60)
    assert http.count("yahoo") == 1 and "暫停使用" in fails[0]                                                   # 限流後不再打
    t[0] += pm.BREAKER_SEC + 1
    pm.fetch_bars("tsm", date(2026, 9, 1), http, order=["yahoo", "nasdaq"], now=lambda: t[0])
    assert http.count("yahoo") == 2                                                                              # 過了暫停期再試
    none, src, fails = pm.fetch_bars("vix", date(2026, 9, 1), Http(), order=["nasdaq"])                            # 這個特徵沒有 nasdaq：沒有可用來源
    assert none == [] and src == "" and fails == []
    http2 = Http({"api.nasdaq.com": SourceError("network", "timeout")})
    assert pm.fetch_bars("sox", date(2026, 9, 1), http2, order=["nasdaq"])[2] == ["nasdaq: network: timeout"]


def test_source_order_comes_from_the_environment(monkeypatch):
    assert pm.source_order() == ["nasdaq", "cboe"]
    monkeypatch.setenv("PREMARKET_SOURCES", " Yahoo , nasdaq,cboe ")
    assert pm.source_order() == ["yahoo", "nasdaq", "cboe"]


# ── 美股欄位的計算、洩漏、異常值（驗收 2、6）───────────────────────

def closes_for(key, start=date(2025, 10, 1), end=date(2026, 12, 31)):
    """合成的收盤價（只在美股開市日）：每個特徵有自己的形狀，報酬有大有小、沒有任何捷徑。"""
    out, i, d = [], 0, start
    base = {"sox": 12000.0, "tsm": 400.0, "vix": 16.0}[key]
    while d <= end:
        if pm.us_session_info(d)[0] != "closed":
            out.append(Bar(d, round(base * (1 + 0.0007 * i) * (1 + 0.012 * math.sin(i * 1.3 + len(key))), 4), "test"))
            i += 1
        d += timedelta(days=1)
    return out


def expected(series, session, kind):
    """獨立算一次：session 那天的對數報酬、σ20（它之前 20 個報酬）、VIX 相對近 20 日平均。"""
    c = [b.close for b in series if b.date <= session]
    i = len(c) - 1
    ret = math.log(c[i] / c[i - 1])
    sigma = statistics.stdev([math.log(c[j] / c[j - 1]) for j in range(i - 20, i)])
    rel = math.log(c[i] / (sum(c[i - 20:i]) / 20))
    return {"value": ret if kind == "return" else c[i], "sigma20": sigma, "rel20": rel, "ret": ret, "close": c[i], "prev_close": c[i - 1]}


SOX_F, TSM_F, VIX_F = pm.FEATURES


def test_compute_us_feature_matches_an_independent_calculation():
    T = tpe(2026, 10, 14, 8, 30).astimezone(pm.UTC)
    session = pm.latest_us_session(tpe(2026, 10, 14, 8, 30))
    assert session == date(2026, 10, 13)
    for f in pm.FEATURES:
        series = closes_for(f.key)
        out = pm.compute_us_feature(f, series, session, T, 123.0)
        want = expected(series, session, f.kind)
        assert out["missing_reason"] is None and out["flag"] is None
        assert out["value"] == pytest.approx(want["value"])
        for k in ("sigma20", "rel20", "ret", "close", "prev_close"):
            assert out["detail"][k] == pytest.approx(want[k]), (f.key, k)
        assert out["detail"]["bar_date"] == "2026-10-13" and out["fetched_at"] == 123.0 and out["source"] == "test"
        close = pm.us_close_utc(session, f.close_extra_min)
        assert out["source_timestamp"] == close.timestamp() and close <= T


def test_data_later_than_T_is_rejected_and_cannot_change_the_value():
    """驗收 2：在 T 之後才有的資料（今天的美股日線、隔天的、收盤時間晚於 T 的）一律拒絕；加進去不能改變任何一個數字。"""
    T = tpe(2026, 10, 14, 8, 30).astimezone(pm.UTC)
    session = date(2026, 10, 13)
    series = [b for b in closes_for("sox") if b.date <= session]
    base = pm.compute_us_feature(SOX_F, series, session, T)
    future = [Bar(date(2026, 10, 14), 99999.0, "test"), Bar(date(2026, 10, 15), 1.0, "test"), Bar(date(2026, 10, 16), 5.0, "test")]       # T 時美股 10/14 還沒收盤
    tainted = pm.compute_us_feature(SOX_F, series + future, session, T)
    assert tainted["detail"]["leaked_rejected"] == 3 and base["detail"]["leaked_rejected"] == 0
    assert {k: v for k, v in tainted.items() if k != "detail"} == {k: v for k, v in base.items() if k != "detail"}
    assert {k: v for k, v in tainted["detail"].items() if k != "leaked_rejected"} == {k: v for k, v in base["detail"].items() if k != "leaked_rejected"}
    assert pm.compute_us_feature(SOX_F, closes_for("sox"), session, T)["value"] == base["value"]                                           # 整條序列（含到年底的未來資料）也一樣
    # 要記的那一天本身在 T 之後才收盤（T 設得比較早）：不是『可用』，也不能拿來算
    early = tpe(2026, 10, 14, 4, 30).astimezone(pm.UTC)                  # 10 月是夏令：美股 10/13 16:00 收盤＝台北 10/14 04:00，04:30 已收
    assert pm.compute_us_feature(SOX_F, series, date(2026, 10, 13), early)["missing_reason"] is None
    too_early = tpe(2026, 10, 14, 3, 30).astimezone(pm.UTC)
    out = pm.compute_us_feature(SOX_F, series, date(2026, 10, 13), too_early)
    assert out["value"] is None and out["missing_reason"].startswith("stale_or_missing") and out["detail"]["leaked_rejected"] >= 1
    vix_series = closes_for("vix")                                         # VIX 比股市晚 15 分鐘：04:00 收的股市已經有、04:15 才定價的 VIX 04:10 還沒有
    t_0410 = tpe(2026, 10, 14, 4, 10).astimezone(pm.UTC)
    assert pm.compute_us_feature(SOX_F, closes_for("sox"), date(2026, 10, 13), t_0410)["value"] is not None
    assert pm.compute_us_feature(VIX_F, vix_series, date(2026, 10, 13), t_0410)["value"] is None


def test_stale_missing_and_short_history_are_null_with_a_reason():
    T = tpe(2026, 10, 14, 8, 30).astimezone(pm.UTC)
    series = closes_for("tsm")
    out = pm.compute_us_feature(TSM_F, [b for b in series if b.date <= date(2026, 10, 12)], date(2026, 10, 13), T)
    assert out["value"] is None and out["missing_reason"] == "stale_or_missing: 最新資料 2026-10-12，預期 2026-10-13" and out["flag"] is None
    assert pm.compute_us_feature(TSM_F, [], date(2026, 10, 13), T)["missing_reason"].startswith("stale_or_missing: 最新資料 無")
    short = [b for b in series if date(2026, 9, 10) <= b.date <= date(2026, 10, 13)]                       # 只有 ~23 筆？再砍
    out = pm.compute_us_feature(TSM_F, short[-21:], date(2026, 10, 13), T)
    assert out["value"] is None and out["missing_reason"].startswith("insufficient_history")
    assert pm.compute_us_feature(TSM_F, short[-22:], date(2026, 10, 13), T)["value"] is not None            # 剛好 22 筆就夠
    bad = [Bar(b.date, 0.0 if b.date == date(2026, 10, 12) else b.close, "test") for b in series]
    assert pm.compute_us_feature(TSM_F, bad, date(2026, 10, 13), T)["missing_reason"].startswith("bad_price")


def test_bars_on_us_holidays_are_ignored():
    """Cboe 的 VIX 歷史從 2022 年起在美股休市日也有值（環球交易時段）：不能拿來算報酬或平均。"""
    T = tpe(2026, 6, 2, 8, 30).astimezone(pm.UTC)
    session = date(2026, 6, 1)
    series = closes_for("vix")
    base = pm.compute_us_feature(VIX_F, series, session, T)
    bogus = Bar(date(2026, 5, 25), 99.0, "test")                                                           # 陣亡將士紀念日
    out = pm.compute_us_feature(VIX_F, sorted(series + [bogus], key=lambda b: b.date), session, T)
    assert out["detail"]["non_trading_rejected"] == 1 + base["detail"]["non_trading_rejected"]
    assert out["value"] == base["value"] and out["detail"]["rel20"] == pytest.approx(base["detail"]["rel20"]) and out["detail"]["sigma20"] == pytest.approx(base["detail"]["sigma20"])


def test_out_of_range_is_flagged_not_corrected(monkeypatch):
    T = tpe(2026, 10, 14, 8, 30).astimezone(pm.UTC)
    session = date(2026, 10, 13)
    series = closes_for("sox")
    jump = [Bar(b.date, b.close * 1.5 if b.date == session else b.close, "test") for b in series]            # 一天漲 50%：超出 ±25%
    out = pm.compute_us_feature(SOX_F, jump, session, T)
    assert out["value"] is None and out["flag"] == "out_of_range" and out["missing_reason"].startswith("out_of_range")
    i = next(k for k, b in enumerate(series) if b.date == session)
    assert out["detail"]["raw_value"] == pytest.approx(math.log(1.5 * series[i].close / series[i - 1].close))
    real_crash = [Bar(b.date, b.close * math.exp(-0.1731) if b.date == session else b.close, "test") for b in series]   # 2020-03-16 那種真實的崩盤日（−17.3%）不能被當成資料錯誤
    out = pm.compute_us_feature(SOX_F, real_crash, session, T)
    assert out["flag"] is None and out["value"] == pytest.approx(math.log(math.exp(-0.1731) * series[i].close / series[i - 1].close)) and pm.FEATURES[0].default_range == (-0.25, 0.25)
    vix = closes_for("vix")
    for level, ok in ((120.0, True), (120.0001, False), (5.0, True), (4.9999, False)):                        # VIX 範圍 [5, 120]：剛好等於邊界算正常（收盤值沒有對數的浮點誤差）
        series_v = [Bar(b.date, level if b.date == session else b.close, "test") for b in vix]
        assert (pm.compute_us_feature(VIX_F, series_v, session, T)["value"] is not None) == ok, level
    monkeypatch.setenv("PREMARKET_RANGE_SOX", "-0.5,0.5")                                                    # 使用者可以改範圍
    assert pm.compute_us_feature(SOX_F, jump, session, T)["value"] is not None
    monkeypatch.setenv("PREMARKET_RANGE_VIX", "20,30")
    out = pm.compute_us_feature(VIX_F, closes_for("vix"), session, T)
    assert out["value"] is None and out["flag"] == "out_of_range"
    monkeypatch.setenv("PREMARKET_RANGE_VIX", "garbage")                                                     # 亂填就用預設
    assert pm.value_range("vix", (5.0, 120.0)) == (5.0, 120.0)
    monkeypatch.setenv("PREMARKET_RANGE_VIX", "30,20")
    assert pm.value_range("vix", (5.0, 120.0)) == (5.0, 120.0)


# ── 台指期夜盤報酬（ticks.db）──────────────────────────────────────

def make_ticks(path, rows):
    con = sqlite3.connect(path)
    con.execute("DROP TABLE IF EXISTS ticks")
    con.execute("CREATE TABLE ticks(code TEXT NOT NULL, ts REAL NOT NULL, price REAL NOT NULL, volume INTEGER NOT NULL, tick_type INTEGER NOT NULL, total_volume INTEGER NOT NULL DEFAULT 0)")
    con.executemany("INSERT INTO ticks(code, ts, price, volume, tick_type) VALUES (?,?,?,?,?)", [(c, t.timestamp(), p, v, 1) for c, t, p, v in rows])
    con.commit()
    con.close()


def session_ticks(prev, code="TXFK6", day=(48000.0, 48100.0, 48200.0), night=(48300.0, 48250.0, 48150.0), vol=2, day_end=dtime(13, 44, 59), night_end=dtime(4, 59, 58)):
    """前一個交易日 prev 的日盤（08:45 起、最後一筆在 day_end）與夜盤（15:00 起、隔天最後一筆在 night_end）。"""
    d0, d1 = datetime.combine(prev, dtime(8, 45), tzinfo=pm.TPE), datetime.combine(prev, day_end, tzinfo=pm.TPE)
    n0, n1 = datetime.combine(prev, dtime(15, 0, 1), tzinfo=pm.TPE), datetime.combine(prev + timedelta(days=1), night_end, tzinfo=pm.TPE)
    rows = [(code, d0, day[0], vol), (code, d0 + timedelta(hours=2), day[1], vol), (code, d1, day[2], vol),
            (code, n0, night[0], vol), (code, n0 + timedelta(hours=6), night[1], vol), (code, n1, night[2], vol)]
    return rows


def test_txf_night_return_by_hand(tmp_path):
    db = tmp_path / "ticks.db"
    prev = date(2026, 10, 13)
    make_ticks(db, session_ticks(prev))
    T = tpe(2026, 10, 14, 8, 30).astimezone(pm.UTC)
    out = pm.txf_night_feature(db, prev, T, 5.0)
    assert out["missing_reason"] is None and out["source"] == "ticks.db" and out["fetched_at"] == 5.0
    assert out["value"] == pytest.approx(48150.0 / 48200.0 - 1)                                               # 夜盤收盤 ÷ 前一日日盤收盤 − 1
    d = out["detail"]
    assert (d["contract"], d["night_close"], d["prev_close"], d["night_open"], d["night_ticks"]) == ("TXFK6", 48150.0, 48200.0, 48300.0, 3)
    assert d["night_open_to_close_return"] == pytest.approx(48150.0 / 48300.0 - 1)
    assert out["source_timestamp"] == datetime.combine(date(2026, 10, 14), dtime(4, 59, 58), tzinfo=pm.TPE).timestamp()


def test_txf_night_uses_only_ticks_up_to_T_and_inside_the_session_window(tmp_path):
    """晚於 T、以及夜盤收盤（05:00）之後到 T 之前的成交（那是別的時段）都不能影響結果。"""
    db = tmp_path / "ticks.db"
    prev = date(2026, 10, 13)
    base = session_ticks(prev)
    extra = [("TXFK6", datetime.combine(date(2026, 10, 14), dtime(6, 0), tzinfo=pm.TPE), 1.0, 99), ("TXFK6", datetime.combine(date(2026, 10, 14), dtime(8, 29, 59), tzinfo=pm.TPE), 2.0, 99),
             ("TXFK6", datetime.combine(date(2026, 10, 14), dtime(9, 0), tzinfo=pm.TPE), 3.0, 99), ("TXFK6", datetime.combine(date(2026, 10, 14), dtime(13, 44), tzinfo=pm.TPE), 4.0, 99),
             ("TXFK6", datetime.combine(prev, dtime(14, 30), tzinfo=pm.TPE), 5.0, 99)]                                # 前一日 13:45~15:00 之間（沒有交易）也不算
    make_ticks(db, base + extra)
    T = tpe(2026, 10, 14, 8, 30).astimezone(pm.UTC)
    out = pm.txf_night_feature(db, prev, T)
    assert out["value"] == pytest.approx(48150.0 / 48200.0 - 1) and out["detail"]["night_ticks"] == 3
    only_base = tmp_path / "base.db"
    make_ticks(only_base, base)
    assert pm.txf_night_feature(only_base, prev, T)["value"] == out["value"]


def test_txf_night_picks_the_most_active_contract_and_the_same_contract_for_the_close(tmp_path):
    db = tmp_path / "ticks.db"
    prev = date(2026, 10, 13)
    rows = session_ticks(prev, "TXFK6", vol=5) + session_ticks(prev, "TXFL6", day=(48500.0, 48600.0, 48700.0), night=(48800.0, 48850.0, 48750.0), vol=1)
    make_ticks(db, rows)
    T = tpe(2026, 10, 14, 8, 30).astimezone(pm.UTC)
    out = pm.txf_night_feature(db, prev, T)
    assert out["detail"]["contract"] == "TXFK6" and out["value"] == pytest.approx(48150.0 / 48200.0 - 1)
    # 夜盤最活躍的合約，前一日日盤沒有成交：不拿別的合約的收盤來湊
    make_ticks(tmp_path / "x.db", [r for r in session_ticks(prev, "TXFK6", vol=5) if r[1].hour >= 15 or r[1].hour < 6] + session_ticks(prev, "TXFL6", vol=1))
    out = pm.txf_night_feature(tmp_path / "x.db", prev, T)
    assert out["value"] is None and out["missing_reason"].startswith("no_ticks: TXFK6")


def test_txf_night_missing_incomplete_or_out_of_range_is_null_with_a_reason(tmp_path):
    prev, T = date(2026, 10, 13), tpe(2026, 10, 14, 8, 30).astimezone(pm.UTC)
    assert pm.txf_night_feature(tmp_path / "missing.db", prev, T)["missing_reason"].startswith("ticks_db_unavailable")
    make_ticks(tmp_path / "empty.db", [])
    assert pm.txf_night_feature(tmp_path / "empty.db", prev, T)["missing_reason"].startswith("no_ticks")
    make_ticks(tmp_path / "late.db", session_ticks(prev, night_end=dtime(4, 20)))                                  # 夜盤最後一筆在 04:20：後端中途沒開
    out = pm.txf_night_feature(tmp_path / "late.db", prev, T)
    assert out["value"] is None and out["missing_reason"].startswith("incomplete_session") and out["detail"]["night_last"].endswith("04:20:00+08:00")
    make_ticks(tmp_path / "day.db", session_ticks(prev, day_end=dtime(13, 20)))
    assert pm.txf_night_feature(tmp_path / "day.db", prev, T)["missing_reason"].startswith("incomplete_session")
    make_ticks(tmp_path / "ok.db", session_ticks(prev))
    assert pm.txf_night_feature(tmp_path / "ok.db", prev, tpe(2026, 10, 14, 4, 0).astimezone(pm.UTC))["missing_reason"].startswith("night_not_finished")
    make_ticks(tmp_path / "wild.db", session_ticks(prev, night=(48300.0, 52000.0, 53000.0)))                       # 夜盤漲 10%：超出 ±8%
    out = pm.txf_night_feature(tmp_path / "wild.db", prev, T)
    assert out["value"] is None and out["flag"] == "out_of_range" and out["detail"]["raw_value"] == pytest.approx(53000.0 / 48200.0 - 1)


def test_txf_night_window_after_a_weekend_and_after_a_holiday(tmp_path):
    """週五的夜盤跨到週六 05:00（屬於下週一）；連假前一天（10/8 週四）的夜盤跨到 10/9 05:00（屬於 10/12）。"""
    for prev in (date(2026, 10, 2), date(2026, 10, 8)):                                                         # 週五、週四
        db = tmp_path / f"{prev}.db"
        make_ticks(db, session_ticks(prev))
        nxt = prev + timedelta(days=1)
        t = datetime.combine(prev + timedelta(days=3 if prev.weekday() == 4 else 4), dtime(8, 30), tzinfo=pm.TPE).astimezone(pm.UTC)
        out = pm.txf_night_feature(db, prev, t)
        assert out["value"] == pytest.approx(48150.0 / 48200.0 - 1)
        assert out["source_timestamp"] == datetime.combine(nxt, dtime(4, 59, 58), tzinfo=pm.TPE).timestamp()


# ── 整筆快照（驗收 1、4、5、6、10）─────────────────────────────────

class World:
    """一個完整的替身世界：證交所日曆、Nasdaq（TSM、SOX）、Cboe（VIX）、ticks.db、暫存資料庫。"""

    def __init__(self, tmp_path, *, prev=date(2026, 10, 13), extra_routes=None, ticks=True):
        self.series = {k: closes_for(k) for k in ("sox", "tsm", "vix")}
        self.series["vix"] = sorted(self.series["vix"] + [Bar(date(2026, 5, 25), 99.0, "test")], key=lambda b: b.date)      # 休市日的假資料
        routes = {"holidaySchedule": twse_json(), "/SOX/historical": self.nasdaq("sox"), "/TSM/historical": self.nasdaq("tsm"), "VIX_History.csv": self.cboe()}
        routes.update(extra_routes or {})
        self.http = Http(routes)
        self.cal = TwCalendar(tmp_path / "tw.json", self.http)
        self.db = tmp_path / "ticks.db"
        if ticks:
            make_ticks(self.db, session_ticks(prev))
        self.store = pm.PremarketStore(tmp_path / "trade_log.db")
        self.kw = dict(store=self.store, cal=self.cal, ticks_path=self.db, http=self.http)

    def nasdaq(self, key):
        fmt = (lambda c: f"${c:,.2f}") if key == "tsm" else (lambda c: f"{c:,.2f}")
        return nasdaq_json([(b.date.strftime("%m/%d/%Y"), fmt(b.close)) for b in reversed(self.series[key])])

    def cboe(self):
        return "DATE,OPEN,HIGH,LOW,CLOSE\n" + "\n".join(f"{b.date:%m/%d/%Y},1,1,1,{b.close}" for b in self.series["vix"]) + "\n"


def feature_values(row):
    return {k: row["features"][k]["value"] for k in pm.FEATURE_KEYS}


def test_a_full_snapshot_is_written_with_hand_computed_values(tmp_path):
    w = World(tmp_path)
    d = date(2026, 10, 14)
    res = pm.run_once(d, now=tpe(2026, 10, 14, 8, 31), writer="live", **w.kw)
    assert res["status"] == "written" and res["missing"] == {} and res["late_seconds"] == pytest.approx(60.0)
    (row,) = w.store.effective()
    assert row["trading_date"] == "2026-10-14" and row["writer"] == "live" and row["schema_version"] == 1 and not row["corrected"] and not row["voided"]
    # Nasdaq 回傳的收盤價有四捨五入到兩位小數，期望值用同樣的資料算
    for f in pm.FEATURES:
        rounded = [Bar(b.date, b.close if f.key == "vix" else float(f"{b.close:.2f}"), b.source) for b in w.series[f.key]]
        want = expected(rounded, date(2026, 10, 13), f.kind)
        got = row["features"][f.key]
        assert got["value"] == pytest.approx(want["value"], rel=1e-9) and got["detail"]["sigma20"] == pytest.approx(want["sigma20"], rel=1e-9)
        assert got["source"] == ("cboe" if f.key == "vix" else "nasdaq") and got["missing_reason"] is None
        assert got["source_timestamp"] == pm.us_close_utc(date(2026, 10, 13), f.close_extra_min).timestamp()
    assert row["features"]["txf_night"]["value"] == pytest.approx(48150.0 / 48200.0 - 1)
    assert row["calendar"] == {"tw_closed": False, "tw_reason": None, "tw_source": "twse", "us_closed": False, "us_session_date": "2026-10-13", "us_early_close": False,
                               "prev_tw_trading_day": "2026-10-13", "notes": ""}
    assert row["snapshot_time_utc"] == tpe(2026, 10, 14, 8, 30).timestamp() and row["fetched_at_utc"] == tpe(2026, 10, 14, 8, 31).timestamp()
    assert pm.status(w.store, d)["today_recorded"] and pm.status(w.store, d)["snapshots"] == 1
    assert not pm.status(w.store, date(2026, 10, 15))["today_recorded"] and pm.status(w.store, date(2026, 10, 15))["latest"]["trading_date"] == "2026-10-14"


def test_monday_uses_friday_and_daylight_saving_weeks(tmp_path):
    for d, prev, session in ((date(2026, 3, 9), date(2026, 3, 6), date(2026, 3, 6)), (date(2026, 3, 10), date(2026, 3, 9), date(2026, 3, 9)),
                             (date(2026, 11, 2), date(2026, 10, 30), date(2026, 10, 30)), (date(2026, 11, 3), date(2026, 11, 2), date(2026, 11, 2)),
                             (date(2026, 10, 12), date(2026, 10, 8), date(2026, 10, 9))):                      # 最後一個：台股連假（10/9 補假）後第一個交易日
        sub = tmp_path / str(d)
        sub.mkdir()
        w = World(sub, prev=prev)
        res = pm.run_once(d, now=datetime.combine(d, dtime(8, 30), tzinfo=pm.TPE), **w.kw)
        assert res["status"] == "written", (d, res)
        (row,) = w.store.effective()
        assert row["calendar"]["us_session_date"] == session.isoformat() and row["calendar"]["prev_tw_trading_day"] == prev.isoformat()
        assert row["features"]["sox"]["detail"]["bar_date"] == session.isoformat() and row["features"]["vix"]["detail"]["bar_date"] == session.isoformat()
        assert row["features"]["txf_night"]["detail"]["prev_day"] == prev.isoformat() and row["features"]["txf_night"]["value"] is not None


def test_us_holiday_gives_null_with_a_reason_and_never_carries_the_previous_value(tmp_path):
    """驗收 5：台股週二盤前，美股週一休市（陣亡將士紀念日）→ 三個美股欄位 null；不能拿上週五的值；也不去打來源。"""
    w = World(tmp_path, prev=date(2026, 5, 25))
    d = date(2026, 5, 26)
    res = pm.run_once(d, now=tpe(2026, 5, 26, 8, 30), **w.kw)
    assert res["status"] == "written"
    (row,) = w.store.effective()
    for k in ("sox", "tsm", "vix"):
        f = row["features"][k]
        assert f["value"] is None and f["missing_reason"] == "us_closed: 2026-05-25 陣亡將士紀念日" and f["source"] == "" and f["source_timestamp"] is None
    assert row["calendar"]["us_closed"] is True and row["calendar"]["us_session_date"] == "2026-05-25" and row["calendar"]["notes"] == "陣亡將士紀念日"
    assert row["features"]["txf_night"]["value"] is not None                                    # 台指期夜盤不受影響
    assert w.http.count("api.nasdaq.com") == 0 and w.http.count("cdn.cboe.com") == 0


def test_tw_closed_days_produce_no_snapshot(tmp_path):
    w = World(tmp_path)
    for d, reason in ((date(2026, 10, 9), "國慶日"), (date(2026, 2, 16), "農曆除夕及春節"), (date(2026, 10, 10), "週末"), (date(2026, 10, 11), "週末")):
        res = pm.run_once(d, now=datetime.combine(d, dtime(8, 30), tzinfo=pm.TPE), **w.kw)
        assert res["status"] == "skipped" and res["reason"] == reason
    assert w.store.effective() == [] and w.http.count("api.nasdaq.com") == 0


def test_source_failures_retry_inside_the_window_then_write_nulls_never_defaults(tmp_path):
    w = World(tmp_path, extra_routes={"/SOX/historical": SourceError("network", "timeout"), "VIX_History.csv": SourceError("rate_limited", "HTTP 429")})
    d = date(2026, 10, 14)
    res = pm.run_once(d, now=tpe(2026, 10, 14, 8, 31), **w.kw)
    assert res == {"status": "retry", "retryable": ["sox", "vix"]} and w.store.effective() == []                    # T 後 15 分鐘內：先不寫，稍後重試
    res = pm.run_once(d, now=tpe(2026, 10, 14, 8, 46), **w.kw)                                                       # 過了重試窗：帶著 null 寫入
    assert res["status"] == "written" and set(res["missing"]) == {"sox", "vix"}
    (row,) = w.store.effective()
    assert row["features"]["sox"]["value"] is None and row["features"]["sox"]["missing_reason"] == "source_error: nasdaq: network: timeout"
    assert row["features"]["vix"]["missing_reason"] == "source_error: cboe: 暫停使用（rate_limited: HTTP 429）"                  # 第一次被限流 → 之後暫停，原因不能丟
    assert row["features"]["tsm"]["value"] is not None and row["features"]["txf_night"]["value"] is not None            # 其他欄位不受影響
    assert row["late_seconds"] == pytest.approx(16 * 60)


def test_stale_source_is_null_not_yesterdays_value(tmp_path):
    w = World(tmp_path)
    w.series["tsm"] = [b for b in w.series["tsm"] if b.date <= date(2026, 10, 12)]                                  # Nasdaq 還沒更新昨天的收盤
    w.http.routes["/TSM/historical"] = w.nasdaq("tsm")
    res = pm.run_once(date(2026, 10, 14), now=tpe(2026, 10, 14, 8, 50), **w.kw)
    assert res["status"] == "written"
    (row,) = w.store.effective()
    assert row["features"]["tsm"]["value"] is None and row["features"]["tsm"]["missing_reason"] == "stale_or_missing: 最新資料 2026-10-12，預期 2026-10-13"


def test_ticks_gap_makes_only_the_night_return_null(tmp_path):
    w = World(tmp_path, ticks=False)
    assert pm.run_once(date(2026, 10, 14), now=tpe(2026, 10, 14, 8, 31), **w.kw) == {"status": "retry", "retryable": ["txf_night"]}
    pm.run_once(date(2026, 10, 14), now=tpe(2026, 10, 14, 8, 50), **w.kw)
    (row,) = w.store.effective()
    assert row["features"]["txf_night"]["value"] is None and row["features"]["txf_night"]["missing_reason"].startswith("ticks_db_unavailable")
    assert all(row["features"][k]["value"] is not None for k in ("sox", "tsm", "vix"))


def test_the_window_edges_are_inclusive(tmp_path):
    for hh, mm, ss, late in ((8, 30, 0, 0.0), (13, 45, 0, (5 * 3600 + 15 * 60))):                              # 剛好 T、剛好 13:45：都還在窗內
        (tmp_path / f"{hh}{mm}").mkdir()
        w = World(tmp_path / f"{hh}{mm}")
        res = pm.run_once(date(2026, 10, 14), now=tpe(2026, 10, 14, hh, mm, ss), **w.kw)
        assert res["status"] == "written" and res["late_seconds"] == pytest.approx(late)


def test_the_time_window_before_T_after_the_close_and_late_start(tmp_path):
    w = World(tmp_path)
    d = date(2026, 10, 14)
    assert pm.run_once(d, now=tpe(2026, 10, 14, 8, 29, 59), **w.kw)["status"] == "too_early"
    assert pm.run_once(d, now=tpe(2026, 10, 14, 13, 45, 1), **w.kw)["status"] == "too_late"
    assert w.store.effective() == [] and w.http.count("api.nasdaq.com") == 0 and w.http.count("cdn.cboe.com") == 0     # 窗外不去抓美股資料（只有日曆查過）
    res = pm.run_once(d, now=tpe(2026, 10, 14, 10, 12), **w.kw)                                                       # 後端 10:12 才啟動：補記，資料仍只用 T 之前的
    assert res["status"] == "written" and res["late_seconds"] == pytest.approx(102 * 60)
    (row,) = w.store.effective()
    assert row["late_seconds"] == pytest.approx(102 * 60) and row["snapshot_time_utc"] == tpe(2026, 10, 14, 8, 30).timestamp()


def test_late_snapshot_has_the_same_values_as_an_on_time_one(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    wa, wb = World(a), World(b)
    pm.run_once(date(2026, 10, 14), now=tpe(2026, 10, 14, 8, 30), **wa.kw)
    pm.run_once(date(2026, 10, 14), now=tpe(2026, 10, 14, 11, 0), **wb.kw)
    ra, rb = wa.store.effective()[0], wb.store.effective()[0]
    strip = lambda r: {k: {x: y for x, y in v.items() if x != "fetched_at"} for k, v in r["features"].items()}      # noqa: E731
    assert strip(ra) == strip(rb)


def test_data_after_T_in_every_source_cannot_change_a_snapshot(tmp_path):
    """驗收 2 的整體版：把 T 之後才會出現的資料（美股隔天的日線、T 之後的台指期成交）塞進所有來源，快照的數字完全一樣。"""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    clean, dirty = World(a), World(b)
    dirty.series["tsm"] = dirty.series["tsm"] + [Bar(date(2026, 10, 14), 1.0, "x"), Bar(date(2026, 10, 15), 2.0, "x")]
    dirty.series["sox"] = dirty.series["sox"] + [Bar(date(2026, 10, 14), 1.0, "x")]
    dirty.series["vix"] = dirty.series["vix"] + [Bar(date(2026, 10, 14), 1.0, "x")]
    dirty.http.routes.update({"/SOX/historical": dirty.nasdaq("sox"), "/TSM/historical": dirty.nasdaq("tsm"), "VIX_History.csv": dirty.cboe()})
    make_ticks(dirty.db, session_ticks(date(2026, 10, 13)) + [("TXFK6", tpe(2026, 10, 14, 8, 45, 1), 1.0, 9), ("TXFK6", tpe(2026, 10, 14, 9, 30), 2.0, 9)])
    now = tpe(2026, 10, 14, 8, 30)
    pm.run_once(date(2026, 10, 14), now=now, **clean.kw)
    pm.run_once(date(2026, 10, 14), now=now, **dirty.kw)
    strip = lambda r: {k: {x: y for x, y in v.items() if x not in ("detail",)} for k, v in r["features"].items()}   # noqa: E731
    assert strip(clean.store.effective()[0]) == strip(dirty.store.effective()[0])
    leaked = lambda w: w.store.effective()[0]["features"]["tsm"]["detail"]["leaked_rejected"]                                # noqa: E731
    assert leaked(dirty) == leaked(clean) + 2                                                                                # 多塞的兩筆被擋下


def test_running_twice_or_in_parallel_writes_exactly_one_complete_row(tmp_path):
    """驗收 10：冪等——重複觸發不重複寫入，同時觸發也不遺失。"""
    w = World(tmp_path)
    d, now = date(2026, 10, 14), tpe(2026, 10, 14, 8, 31)
    assert pm.run_once(d, now=now, **w.kw)["status"] == "written"
    assert pm.run_once(d, now=now, **w.kw)["status"] == "exists"
    calls = len(w.http.calls)
    assert pm.run_once(d, now=tpe(2026, 10, 14, 9, 0), **w.kw)["status"] == "exists" and len(w.http.calls) == calls            # 已經有了就不再去抓
    other = tmp_path / "par"
    other.mkdir()
    w2 = World(other)
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda _: pm.run_once(d, now=now, **w2.kw), range(8)))
    assert sorted(r["status"] for r in results).count("written") == 1 and {r["status"] for r in results} <= {"written", "exists"}
    rows = w2.store.raw_rows()
    assert len(rows) == 1 and rows[0]["kind"] == "snapshot"
    assert all(v is not None for v in feature_values(w2.store.effective()[0]).values())                                        # 寫進去的那一筆是完整的


def test_one_snapshot_per_trading_day_over_a_whole_week(tmp_path):
    w = World(tmp_path)
    for i in range(7):                                                                                                         # 10/12（週一）~ 10/18（週日）
        d = date(2026, 10, 12) + timedelta(days=i)
        pm.run_once(d, now=datetime.combine(d, dtime(8, 31), tzinfo=pm.TPE), **w.kw)
        pm.run_once(d, now=datetime.combine(d, dtime(8, 40), tzinfo=pm.TPE), **w.kw)
    assert sorted(r["trading_date"] for r in w.store.raw_rows()) == ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"]


# ── 只新增、不改寫（驗收：紀錄不可改寫）────────────────────────────

def written(tmp_path):
    w = World(tmp_path)
    pm.run_once(date(2026, 10, 14), now=tpe(2026, 10, 14, 8, 31), **w.kw)
    return w


def test_history_cannot_be_updated_or_deleted(tmp_path):
    w = written(tmp_path)
    con = sqlite3.connect(w.store.path, isolation_level=None)
    for sql in ("UPDATE premarket_snapshots SET features_json='{}'", "DELETE FROM premarket_snapshots", "UPDATE premarket_graveyard SET note='x'", "DELETE FROM premarket_graveyard"):
        w.store.add_graveyard("t")
        with pytest.raises(sqlite3.DatabaseError, match="只新增"):
            con.execute(sql)
    assert con.execute("SELECT COUNT(*) FROM premarket_snapshots").fetchone()[0] == 1
    con.close()


def test_a_correction_is_a_new_row_pointing_at_the_original(tmp_path):
    w = written(tmp_path)
    d = date(2026, 10, 14)
    (orig,) = w.store.raw_rows()
    cid = w.store.add_correction(d, {"sox": {"value": 0.0123, "missing_reason": None, "flag": None}}, "來源當天寫錯，Nasdaq 事後更正")
    rows = w.store.raw_rows()
    assert [r["kind"] for r in rows] == ["snapshot", "correction"] and rows[1]["corrects_id"] == orig["id"] and rows[1]["id"] == cid
    assert rows[0] == orig                                                                                                # 原紀錄一個字都沒動
    eff = w.store.effective()[0]
    assert eff["corrected"] and eff["correction_note"].startswith("來源當天") and eff["features"]["sox"]["value"] == 0.0123 and eff["features"]["tsm"]["value"] is not None
    assert json.loads(orig["features_json"])["sox"]["value"] != 0.0123
    with pytest.raises(ValueError):
        w.store.add_correction(date(2026, 10, 15), {}, "沒有這一天")
    assert pm.run_once(d, now=tpe(2026, 10, 14, 9, 0), **w.kw)["status"] == "exists"                                       # 更正不會讓冪等失效


def test_void_marks_the_day_without_touching_the_original(tmp_path):
    w = written(tmp_path)
    d = date(2026, 10, 14)
    vid = w.store.add_void(d, "颱風假，期交所休市")
    assert [r["kind"] for r in w.store.raw_rows()] == ["snapshot", "void"]
    eff = w.store.effective()[0]
    assert eff["voided"] and eff["void_reason"] == "颱風假，期交所休市" and eff["features"]["sox"]["value"] is not None and vid > 0
    assert w.store.status() == {"snapshots": 1, "corrections": 0, "voids": 1, "errors_logged": 0, "graveyard": 0}


def test_status_latest_is_the_newest_day_that_is_not_voided(tmp_path):
    w = written(tmp_path)                                                                                      # 10/14
    make_ticks(w.db, session_ticks(date(2026, 10, 14)))
    pm.run_once(date(2026, 10, 15), now=tpe(2026, 10, 15, 8, 31), **w.kw)
    now = tpe(2026, 10, 15, 9, 0)
    assert pm.status(w.store, cal=w.cal, now=now)["latest"]["trading_date"] == "2026-10-15"
    w.store.add_void(date(2026, 10, 15), "颱風假")
    assert pm.status(w.store, cal=w.cal, now=now)["latest"]["trading_date"] == "2026-10-14"                    # 作廢的 10/15 不算最新
    w.store.add_void(date(2026, 10, 14), "颱風假")
    s = pm.status(w.store, cal=w.cal, now=now)
    assert s["latest"] is None and s["snapshots"] == 2 and s["today_recorded"] is True                         # 都作廢了：沒有最新；但今天『有一筆（作廢的）』


# ── 寫入失敗不影響交易（驗收 9）────────────────────────────────────

def test_a_failing_store_raises_an_alert_and_never_an_exception(tmp_path, monkeypatch, caplog):
    w = World(tmp_path)

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(w.store, "write_snapshot", boom)
    with caplog.at_level("ERROR"):
        res = pm.run_guarded(date(2026, 10, 14), now=tpe(2026, 10, 14, 8, 31), **w.kw)
    assert res["status"] == "error" and "database is locked" in res["error"]
    assert pm.alerts.count == 1 and pm.alerts.last["where"] == "run" and "PREMARKET ALERT" in caplog.text and "不影響交易" in caplog.text
    assert pm.status(w.store)["errors_logged"] == 1                                                                         # 錯誤表裡有一筆
    assert not pm.ERROR_FILE.exists()                                                                                         # 錯誤表寫得進去，就不用備援檔


def test_when_even_the_error_table_fails_the_fallback_file_gets_the_error(tmp_path, monkeypatch):
    w = World(tmp_path)
    broken = pm.PremarketStore(tmp_path / "no_such_dir" / "x" / "db.sqlite")                                                 # 連資料庫都開不起來
    monkeypatch.setattr(pm.PremarketStore, "_conn", lambda self: (_ for _ in ()).throw(sqlite3.OperationalError("unable to open database file")))
    res = pm.run_guarded(date(2026, 10, 14), now=tpe(2026, 10, 14, 8, 31), **{**w.kw, "store": broken})
    assert res["status"] == "error" and pm.alerts.count == 1
    lines = pm.ERROR_FILE.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["where"] == "run" and "unable to open" in json.loads(lines[0])["error"]


def test_an_exception_inside_the_builder_is_contained(tmp_path, monkeypatch):
    w = World(tmp_path)
    monkeypatch.setattr(pm, "build_snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
    res = pm.run_guarded(date(2026, 10, 14), now=tpe(2026, 10, 14, 8, 31), **w.kw)
    assert res["status"] == "error" and pm.alerts.count == 1 and w.store.effective() == []


# ── 休市日不算錯過、日曆不被猛打、下一筆時間、預覽 ──────────────────

def test_closed_days_are_skipped_even_after_the_window_and_never_count_as_missed(tmp_path):
    """週末或假日 13:45 之後重啟，不能誤報『錯過』。"""
    w = World(tmp_path)
    for d in (date(2026, 10, 10), date(2026, 10, 11), date(2026, 10, 9)):                                      # 週六、週日、國慶補假
        res = pm.run_once(d, now=datetime.combine(d, dtime(16, 0), tzinfo=pm.TPE), **w.kw)
        assert res["status"] == "skipped", (d, res)
    assert w.store.status()["errors_logged"] == 0 and w.store.effective() == []
    d = date(2026, 10, 13)                                                                                       # 對照：交易日 13:45 之後才啟動 → 真的錯過
    assert pm.run_once(d, now=datetime.combine(d, dtime(16, 0), tzinfo=pm.TPE), **w.kw)["status"] == "too_late"


def test_tw_calendar_does_not_hammer_twse_when_the_fetch_fails(tmp_path):
    http, t = Http({"holidaySchedule": SourceError("network", "x")}), [1000.0]
    cal = TwCalendar(tmp_path / "tw.json", http, now=lambda: t[0])
    for _ in range(6):
        assert cal.check(date(2026, 10, 9))["source"] == "fallback_weekday"
    assert http.count("holidaySchedule") == 1                                                                    # 失敗之後 10 分鐘內不再打
    t[0] += 601
    cal.check(date(2026, 10, 9))
    assert http.count("holidaySchedule") == 2
    http.routes["holidaySchedule"] = twse_json()                                                                 # 恢復了：下一次重試就用得到
    t[0] += 601
    assert cal.check(date(2026, 10, 9)) == {"open": False, "reason": "國慶日", "source": "twse"}


def test_next_snapshot_is_today_until_the_window_closes_then_the_next_trading_day(tmp_path):
    w = World(tmp_path)
    nxt = lambda y, m, d, hh, mm: pm.next_snapshot(w.store, w.cal, tpe(y, m, d, hh, mm))                       # noqa: E731
    assert nxt(2026, 10, 14, 7, 0) == {"trading_date": "2026-10-14", "time": "2026-10-14T08:30+08:00", "late": False}
    assert nxt(2026, 10, 14, 10, 0) == {"trading_date": "2026-10-14", "time": "2026-10-14T08:30+08:00", "late": True}            # 已過 T、還在 13:45 前：補記
    assert nxt(2026, 10, 14, 16, 0)["trading_date"] == "2026-10-15"                                                              # 已經過了補記的時限
    assert nxt(2026, 10, 16, 16, 0)["trading_date"] == "2026-10-19"                                                              # 週五晚上 → 下週一
    assert nxt(2026, 10, 8, 16, 0)["trading_date"] == "2026-10-12"                                                               # 10/9 補假＋週末
    pm.run_once(date(2026, 10, 14), now=tpe(2026, 10, 14, 8, 31), **w.kw)
    assert nxt(2026, 10, 14, 10, 0)["trading_date"] == "2026-10-15"                                                              # 今天記過了


def test_status_has_the_next_snapshot_and_the_missed_days(tmp_path):
    w = World(tmp_path)
    w.store.log_error("missed", "x", "2026-10-13")
    w.store.log_error("missed", "y", "2026-10-13")
    s = pm.status(w.store, cal=w.cal, now=tpe(2026, 10, 14, 7, 0))
    assert s["missed_dates"] == ["2026-10-13"] and s["next_snapshot"]["trading_date"] == "2026-10-14" and s["alerts"] == 0 and s["snapshots"] == 0
    assert s["today"] == "2026-10-14" and s["today_recorded"] is False and s["latest"] is None


def test_missed_dates_are_newest_first_and_only_the_missed_ones(tmp_path):
    store = pm.PremarketStore(tmp_path / "t.db")
    store.log_error("missed", "x", "2026-10-13")
    store.log_error("missed", "x", "2026-10-14")
    store.log_error("run", "database is locked", "2026-10-15")                                                   # 一般的失敗不算「錯過」
    assert store.missed_dates() == ["2026-10-14", "2026-10-13"] and store.missed_dates(limit=1) == ["2026-10-14"]


def test_next_snapshot_edges_are_inclusive(tmp_path):
    w = World(tmp_path)
    at = lambda hh, mm, ss: pm.next_snapshot(w.store, w.cal, tpe(2026, 10, 14, hh, mm, ss))                      # noqa: E731
    assert at(8, 29, 59) == {"trading_date": "2026-10-14", "time": "2026-10-14T08:30+08:00", "late": False}
    assert at(8, 30, 0) == {"trading_date": "2026-10-14", "time": "2026-10-14T08:30+08:00", "late": True}        # 剛好 T：已經可以記了
    assert at(13, 45, 0)["trading_date"] == "2026-10-14" and at(13, 45, 1)["trading_date"] == "2026-10-15"        # 13:45:00 還補得到、13:45:01 不行


def test_default_preview_date_is_the_latest_trading_day_whose_T_has_passed(tmp_path):
    w = World(tmp_path)
    f = lambda y, m, d, hh, mm: pm.default_preview_date(w.cal, tpe(y, m, d, hh, mm))                            # noqa: E731
    assert f(2026, 10, 14, 7, 0) == date(2026, 10, 13) and f(2026, 10, 14, 8, 30) == date(2026, 10, 14) and f(2026, 10, 14, 23, 0) == date(2026, 10, 14)
    assert f(2026, 10, 10, 12, 0) == date(2026, 10, 8) and f(2026, 10, 12, 8, 0) == date(2026, 10, 8)          # 週六、連假後週一 T 之前 → 連假前最後一個交易日


def test_preview_is_a_dry_run_that_matches_what_would_be_recorded(tmp_path):
    w = World(tmp_path)
    now = tpe(2026, 10, 14, 9, 0)
    pv = pm.preview(date(2026, 10, 14), now=now, **w.kw)
    assert pv["dry_run"] is True and pv["recorded"] is False and pv["trading_date"] == "2026-10-14" and pv["snapshot_time"] == "2026-10-14T08:30+08:00"
    assert w.store.effective() == [] and w.store.raw_rows() == []                                              # 預覽不寫入
    assert all(pv["features"][k]["value"] is not None for k in pm.FEATURE_KEYS) and "retryable" not in pv
    pm.run_once(date(2026, 10, 14), now=now, **w.kw)
    real = w.store.effective()[0]
    strip = lambda feats: {k: {x: y for x, y in v.items() if x != "fetched_at"} for k, v in feats.items()}     # noqa: E731
    assert strip(pv["features"]) == strip(real["features"]) and pv["calendar"] == real["calendar"]               # 預覽的內容就是真的會記的內容
    assert pm.preview(date(2026, 10, 14), now=now, use_cache=False, **w.kw)["recorded"] is True


def test_preview_is_cached_for_a_minute_and_uses_the_default_day(tmp_path, monkeypatch):
    w = World(tmp_path)
    now = tpe(2026, 10, 14, 7, 0)                                                                              # T 之前 → 預設看 10/13
    a = pm.preview(now=now, **w.kw)
    assert a["trading_date"] == "2026-10-13" and a["features"]["sox"]["detail"]["bar_date"] == "2026-10-12"
    calls = len(w.http.calls)
    assert pm.preview(now=now, **w.kw) is a and len(w.http.calls) == calls                                      # 60 秒內不再去抓
    clock = [pm.time.time() + pm.PREVIEW_TTL + 1]
    monkeypatch.setattr(pm.time, "time", lambda: clock[0])
    assert pm.preview(now=now, **w.kw) is not a and len(w.http.calls) > calls                                   # 過了就重抓
    closed = pm.preview(date(2026, 10, 9), now=now, **w.kw)                                                      # 休市日也能預覽（日曆欄位會標休市）
    assert closed["calendar"]["tw_closed"] is True and closed["calendar"]["tw_reason"] == "國慶日"


# ── 背景迴圈 ──────────────────────────────────────────────────────

def drive_loop(monkeypatch, results, dates, hhmm=dtime(8, 31), backfill=None):
    """跑 premarket_log_loop 幾圈：run_guarded 依序回傳 results、日期依序是 dates、現在時間固定是 hhmm；回傳 (呼叫次數, 每次睡多久)。"""
    sleeps, calls = [], []
    it_res, it_date = iter(results), iter(dates)

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) > len(results):
            raise asyncio.CancelledError

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.combine(next(it_date), hhmm, tzinfo=pm.TPE)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(pm, "datetime", FakeDT)
    monkeypatch.setattr(pm, "run_guarded", lambda d, **kw: calls.append(d) or next(it_res))
    monkeypatch.setattr(pm, "_backfill_tick", backfill or (lambda now: []))                          # 測試不能碰真的資料庫與網路
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(pm.premarket_log_loop())
    return calls, sleeps


def test_loop_stops_after_a_day_is_done_and_retries_after_two_minutes(monkeypatch):
    d1, d2 = date(2026, 10, 14), date(2026, 10, 15)
    calls, sleeps = drive_loop(monkeypatch, [{"status": "retry", "retryable": ["sox"]}, {"status": "written"}, {"status": "skipped"}],
                               [d1, d1, d1, d1, d2])
    assert calls == [d1, d1, d1][:len(calls)] and calls[:2] == [d1, d1]
    assert sleeps[0] == 30.0 and sleeps[1] == 120.0 and sleeps[2] == 30.0                                       # retry 之後等 2 分鐘；之後回到 30 秒


def test_loop_does_not_call_again_for_a_finished_day(monkeypatch):
    d1 = date(2026, 10, 14)
    calls, _ = drive_loop(monkeypatch, [{"status": "written", "missing": {}}, {"status": "written"}, {"status": "written"}], [d1] * 6)
    assert calls == [d1]                                                                                           # 當天寫完就不再叫


def test_loop_records_a_missed_day_quietly_and_never_before_T(monkeypatch, tmp_path, caplog):
    """後端 13:45 之後才啟動：這天留一筆『錯過』紀錄與警告，但不算失敗警示（重新部署本來就會碰到）；T 之前根本不呼叫。"""
    d1 = date(2026, 10, 14)
    store = pm.PremarketStore(tmp_path / "t.db")
    monkeypatch.setattr(pm, "_default_store", store)
    with caplog.at_level("WARNING"):
        calls, _ = drive_loop(monkeypatch, [{"status": "too_late"}], [d1, d1, d1])
    assert calls == [d1] and pm.alerts.count == 0 and "沒有盤前快照" in caplog.text
    assert store.status()["errors_logged"] == 1 and store.status()["snapshots"] == 0
    with sqlite3.connect(store.path) as con:
        assert con.execute("SELECT where_, trading_date FROM premarket_errors").fetchall() == [("missed", "2026-10-14")]
    calls, _ = drive_loop(monkeypatch, [{"status": "written"}], [d1, d1], hhmm=dtime(8, 29))              # 08:29：還沒到 T
    assert calls == []


def test_loop_stays_alive_on_unexpected_exceptions(monkeypatch):
    n = {"i": 0}

    async def fake_sleep(s):
        n["i"] += 1
        if n["i"] > 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(pm, "run_guarded", lambda d, **kw: (_ for _ in ()).throw(RuntimeError("迴圈裡的未預期例外")))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(pm.premarket_log_loop())
    assert n["i"] == 4                                                                                             # 例外被吞掉、迴圈繼續活著


def test_loop_is_off_when_disabled(monkeypatch):
    monkeypatch.setenv("PREMARKET_LOG", "false")
    asyncio.run(asyncio.wait_for(pm.premarket_log_loop(), 1))                                                      # 直接結束、不進無窮迴圈
    assert not pm.enabled()


def test_snapshot_time_comes_from_the_environment(monkeypatch):
    assert pm.snapshot_time() == dtime(8, 30) and pm.t_of(date(2026, 10, 14)) == tpe(2026, 10, 14, 8, 30)
    monkeypatch.setenv("PREMARKET_TIME", "8:20")
    assert pm.snapshot_time() == dtime(8, 20) and pm.t_of(date(2026, 10, 14)) == tpe(2026, 10, 14, 8, 20)
    monkeypatch.setenv("PREMARKET_TIME", "亂填")
    assert pm.snapshot_time() == dtime(8, 30)


# ── CSV 匯出（驗收 11）────────────────────────────────────────────

def test_csv_has_fixed_columns_taipei_times_and_blanks_for_nulls(tmp_path):
    w = World(tmp_path, extra_routes={"VIX_History.csv": SourceError("network", "x")})
    pm.run_once(date(2026, 10, 14), now=tpe(2026, 10, 14, 8, 50), **w.kw)
    w.store.add_correction(date(2026, 10, 14), {"vix": {"value": 15.5, "missing_reason": None, "source": "手動"}}, "手動補")
    text = pm.to_csv(w.store.effective())
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0]) == pm.csv_columns() and len(rows) == 1
    r = rows[0]
    assert r["trading_date"] == "2026-10-14" and r["snapshot_time"] == "2026-10-14T08:30:00+08:00" and r["fetched_at"] == "2026-10-14T08:50:00+08:00"
    assert r["sox_source_timestamp"] == pm.us_close_utc(date(2026, 10, 13)).astimezone(pm.TPE).isoformat(timespec="seconds") and r["sox_source_timestamp"].endswith("+08:00")
    assert r["sox_source_timestamp"] == "2026-10-14T04:00:00+08:00"                                                           # 10 月是夏令：美東 16:00 ＝ 台北 04:00
    assert r["vix_value"] == "15.5" and r["vix_source"] == "手動" and r["corrected"] == "1" and r["vix_missing_reason"] == ""
    assert r["corrected_at"].endswith("+08:00") and r["corrected_at"] != r["fetched_at"]                                        # 更正的時間另外一欄，不蓋掉原始的取得時間
    assert float(r["sox_value"]) == pytest.approx(w.store.effective()[0]["features"]["sox"]["value"]) and r["tsm_missing_reason"] == ""
    assert r["txf_night_contract"] == "TXFK6" and float(r["txf_night_night_close"]) == 48150.0
    blank = pm.to_csv([{**w.store.effective()[0], "features": {k: {"value": None, "missing_reason": "x"} for k in pm.FEATURE_KEYS}}])
    row = next(csv.DictReader(io.StringIO(blank)))
    assert row["sox_value"] == "" and row["sox_missing_reason"] == "x" and "0" != row["sox_value"]                              # 缺值留空、不寫 0


# ── API ───────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    import api.routes_premarket as rp
    w = written(tmp_path)
    monkeypatch.setattr(pm, "_default_store", w.store)
    app = FastAPI()
    app.include_router(rp.router, prefix="/api/premarket")
    return TestClient(app), w, rp


def test_api_preview_and_status_extras(client, monkeypatch):
    c, w, rp = client
    seen = {}

    def fake_preview(d=None):
        seen["d"] = d
        return {"dry_run": True, "trading_date": "2026-10-13", "features": {}, "calendar": {}}

    monkeypatch.setattr(pm, "preview", fake_preview)
    assert c.get("/api/premarket/preview").json()["dry_run"] is True and seen["d"] is None
    assert c.get("/api/premarket/preview", params={"date": "2026-10-09"}).status_code == 200 and seen["d"] == date(2026, 10, 9)
    assert c.get("/api/premarket/preview", params={"date": "亂寫"}).status_code == 422
    monkeypatch.setattr(pm, "preview", lambda d=None: (_ for _ in ()).throw(RuntimeError("來源全掛")))
    r = c.get("/api/premarket/preview")
    assert r.status_code == 502 and "來源全掛" in r.json()["detail"]
    s = c.get("/api/premarket/status").json()
    assert "next_snapshot" in s and "missed_dates" in s and s["missed_dates"] == []


def test_api_status_snapshots_csv_and_run(client, monkeypatch):
    c, w, rp = client
    s = c.get("/api/premarket/status").json()
    assert s["enabled"] and s["snapshot_time"] == "08:30" and s["snapshots"] == 1 and s["sources"] == ["nasdaq", "cboe"] and s["alerts"] == 0
    rows = c.get("/api/premarket/snapshots", params={"days": 3650}).json()
    assert len(rows) == 1 and rows[0]["trading_date"] == "2026-10-14" and rows[0]["features"]["sox"]["value"] is not None
    r = c.get("/api/premarket/snapshots.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and r.content.startswith("﻿".encode("utf-8"))
    assert "trading_date" in r.text and "2026-10-14" in r.text and "premarket_snapshots.csv" in r.headers["content-disposition"]
    monkeypatch.setattr(pm, "run_guarded", lambda d, **kw: {"status": "exists"})
    assert c.post("/api/premarket/run").json() == {"status": "exists"}


# ── 沒有任何策略或風控模組讀這些欄位（驗收 8）────────────────────

FORBIDDEN = ("premarket", "txf_night", "sox_ret", "tsm_ret", "vix_level")


def test_no_strategy_or_risk_module_reads_the_premarket_fields():
    files = (sorted((ROOT / "strategies").glob("*.py")) + [ROOT / "api" / n for n in ("routes_strategy.py", "routes_order.py", "routes_risk.py", "routes_position.py", "routes_backtest.py")]
             + [ROOT / "core" / n for n in ("manual_monitor.py", "ruin.py", "broker_guard.py", "broker.py", "quote_hub.py", "backtest.py")])
    offenders = [f"{f.name}: {w}" for f in files for w in FORBIDDEN if w in f.read_text(encoding="utf-8").lower()]
    assert len(files) >= 16 and not offenders, offenders
    notify_only = ("daily_brief.py", "reminders.py")                        # 只通知的模組（發現 37）：reminders 只借休市日曆、daily_brief 只借夜盤報酬與日曆來組摘要訊息
    wired = [f.name for f in (ROOT / "core").glob("*.py") if "premarket_log" in f.read_text(encoding="utf-8")
             and f.name not in ("premarket_log.py", "premarket_study.py") + notify_only]
    assert wired == [], wired                                                                                        # core 裡也沒有別的模組 import 它（只有 main.py 與自己的路由）
    # 反方向：策略與風控模組不能 import 那兩個通知模組（否則盤前資料就繞一圈進到交易決策）
    leaks = [f.name for f in files if any(w in f.read_text(encoding="utf-8") for w in ("daily_brief", "core.reminders", "import reminders", "from core import reminders"))]
    assert leaks == [], leaks


def test_enabling_the_recorder_does_not_change_the_orders_a_strategy_sends(tmp_path, monkeypatch):
    """驗收 7：啟用前後重播同一組行情，送出的訂單完全相同——就算記錄器同時在寫入、或寫入失敗。"""
    import strategies.base as base
    from core.backtest import Combo, run_grid

    def mk(day, ohlc, start="08:45"):
        t0 = datetime.strptime(f"{day} {start}", "%Y-%m-%d %H:%M").timestamp()
        return [(t0 + 60 * i, float(o), float(h), float(l), float(c), 100) for i, (o, h, l, c) in enumerate(ohlc)]

    rng = random.Random(3)
    days = {}
    for i in range(6):
        day = (date(2026, 1, 5) + timedelta(days=i)).isoformat()
        p, bars = 100.0, [(105, 110, 100, 105)] * 5
        for _ in range(60):
            o = p
            p += rng.choice([-9, -4, 0, 5, 12])
            bars.append((o, max(o, p) + 2, min(o, p) - 2, p))
        days[day] = mk(day, bars)
    combo = Combo("orb", {"or_minutes": 5, "buffer_pts": 0}, {"stop_loss_pts": 10, "take_profit_pts": 15})

    def replay():
        sent = []
        orig = base.BaseStrategy.place_order

        async def spy(self, *a, **k):
            sent.append((a, tuple(sorted((x, repr(y)) for x, y in k.items()))))
            return await orig(self, *a, **k)

        monkeypatch.setattr(base.BaseStrategy, "place_order", spy)
        try:
            res = run_grid([combo], days, 4.0, workers=1)[0]
        finally:
            monkeypatch.setattr(base.BaseStrategy, "place_order", orig)
        return sent, res.net.tolist(), res.n.tolist(), res.wins.tolist()

    baseline = replay()
    assert len(baseline[0]) >= 6                                                                                      # 重播裡真的有送單，比對才有意義
    w = World(tmp_path)
    stop, written_rows = threading.Event(), []

    def hammer():                                                                                                     # 記錄器在另一個執行緒不停地寫入（每次換一個日期）
        i = 0
        while not stop.is_set():
            d = date(2026, 10, 14) + timedelta(days=i)
            res = pm.run_guarded(d, now=datetime.combine(d, dtime(8, 31), tzinfo=pm.TPE), force=True, **w.kw)
            written_rows.append(res["status"])
            i += 1

    th = threading.Thread(target=hammer)
    th.start()
    try:
        with_recorder = replay()
    finally:
        stop.set()
        th.join()
    assert written_rows and with_recorder == baseline
    monkeypatch.setattr(w.store, "write_snapshot", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))       # 記錄器壞掉
    assert pm.run_guarded(date(2026, 11, 20), now=tpe(2026, 11, 20, 8, 31), force=True, **w.kw)["status"] == "error"
    assert replay() == baseline


# ── 事後補值（update.md 發現 35）：來源比 T 晚更新 ───────────────────

def stale_world(tmp_path, last=date(2026, 10, 12)):
    """Nasdaq、Cboe 都還停在 last（還沒更新前一個美股交易日）。"""
    w = World(tmp_path)
    full = {k: list(v) for k, v in w.series.items()}
    for k in ("sox", "tsm", "vix"):
        w.series[k] = [b for b in full[k] if b.date <= last]
    w.http.routes["/SOX/historical"], w.http.routes["/TSM/historical"], w.http.routes["VIX_History.csv"] = w.nasdaq("sox"), w.nasdaq("tsm"), w.cboe()
    w.full = full
    return w


def publish(w, *keys):
    """來源更新了：這幾個欄位的完整資料上線。"""
    for k in keys:
        w.series[k] = list(w.full[k])
    w.http.routes["/SOX/historical"], w.http.routes["/TSM/historical"], w.http.routes["VIX_History.csv"] = w.nasdaq("sox"), w.nasdaq("tsm"), w.cboe()


def test_backfill_fills_what_the_sources_published_late_and_leaves_the_original_row_alone(tmp_path):
    w = stale_world(tmp_path)
    d = date(2026, 10, 14)
    assert pm.run_once(d, now=tpe(2026, 10, 14, 8, 46), **w.kw)["status"] == "written"
    (orig,) = w.store.raw_rows()
    eff = w.store.effective()[0]
    assert [eff["features"][k]["value"] for k in ("sox", "tsm", "vix")] == [None, None, None] and all(eff["features"][k]["missing_reason"].startswith("stale_or_missing") for k in ("sox", "tsm", "vix"))
    assert pm.pending_backfill(w.store, d) == [d]
    publish(w, "sox", "tsm", "vix")
    now = tpe(2026, 10, 14, 12, 16)
    res = pm.backfill_once(d, now=now, **w.kw)
    assert res == {"status": "filled", "filled": ["sox", "tsm", "vix"], "missing": []}
    rows = w.store.raw_rows()
    assert [r["kind"] for r in rows] == ["snapshot", "correction"] and rows[0] == orig and rows[1]["writer"] == "backfill" and rows[1]["corrects_id"] == orig["id"]    # 原始那筆一個字沒動
    eff = w.store.effective()[0]
    assert eff["corrected"] and eff["correction_note"] == pm.BACKFILL_NOTE == "自動補值（來源較晚更新）" and eff["fetched_at_utc"] == orig["fetched_at_utc"] and eff["late_seconds"] == orig["late_seconds"]
    for k in ("sox", "tsm", "vix"):
        f = eff["features"][k]
        assert f["value"] is not None and f["missing_reason"] is None and f["detail"]["backfilled"] is True and f["detail"]["backfilled_at"] == now.timestamp() and f["fetched_at"] == now.timestamp()
    assert eff["features"]["txf_night"]["value"] is not None and eff["features"]["txf_night"]["fetched_at"] != now.timestamp()          # 夜盤欄位沒被碰
    want = {k: expected(w.series[k] if k == "vix" else [Bar(b.date, float(f"{b.close:.2f}"), b.source) for b in w.series[k]], date(2026, 10, 13), f_.kind)["value"]
            for f_ in pm.FEATURES for k in (f_.key,)}
    assert [eff["features"][k]["value"] for k in ("sox", "tsm", "vix")] == [pytest.approx(want[k]) for k in ("sox", "tsm", "vix")]                    # 補進去的值和當天算的一樣
    assert pm.pending_backfill(w.store, d) == [] and pm.backfill_once(d, now=now, **w.kw) == {"status": "nothing_to_do"}                     # 補完就不再補


def test_backfill_in_steps_accumulates_and_never_loses_an_earlier_fill(tmp_path):
    w = stale_world(tmp_path)
    d = date(2026, 10, 14)
    pm.run_once(d, now=tpe(2026, 10, 14, 8, 46), **w.kw)
    publish(w, "tsm")                                                                                       # 先只有 Nasdaq 的 TSM 更新（費半、VIX 還沒）
    w.series["sox"] = [b for b in w.series["sox"] if b.date <= date(2026, 10, 12)]
    w.series["vix"] = [b for b in w.series["vix"] if b.date <= date(2026, 10, 12)]
    w.http.routes["/SOX/historical"], w.http.routes["VIX_History.csv"] = w.nasdaq("sox"), w.cboe()
    r1 = pm.backfill_once(d, now=tpe(2026, 10, 14, 9, 0), **w.kw)
    assert r1 == {"status": "filled", "filled": ["tsm"], "missing": ["sox", "vix"]}
    assert pm.pending_backfill(w.store, d) == [d]                                                           # 還有欄位沒補到：繼續等
    publish(w, "sox", "vix")
    r2 = pm.backfill_once(d, now=tpe(2026, 10, 14, 10, 30), **w.kw)
    assert r2 == {"status": "filled", "filled": ["sox", "vix"], "missing": []}
    eff = w.store.effective()[0]
    assert [eff["features"][k]["value"] is not None for k in ("sox", "tsm", "vix")] == [True, True, True]      # 第二次補值沒有把第一次的 TSM 弄丟
    assert eff["features"]["tsm"]["detail"]["backfilled_at"] == tpe(2026, 10, 14, 9, 0).timestamp()           # 各欄位記各自補到的時間
    assert eff["features"]["sox"]["detail"]["backfilled_at"] == tpe(2026, 10, 14, 10, 30).timestamp()
    assert [r["kind"] for r in w.store.raw_rows()] == ["snapshot", "correction", "correction"]


def test_successive_manual_corrections_accumulate_too(tmp_path):
    w = written(tmp_path)
    d = date(2026, 10, 14)
    w.store.add_correction(d, {"sox": {"value": 0.01}}, "第一次")
    w.store.add_correction(d, {"vix": {"value": 15.5}}, "第二次")
    eff = w.store.effective()[0]
    assert eff["features"]["sox"]["value"] == 0.01 and eff["features"]["vix"]["value"] == 15.5 and eff["correction_note"] == "第二次"     # 以前第二次會把第一次的蓋掉


def test_backfill_when_the_sources_are_still_late_writes_nothing(tmp_path):
    w = stale_world(tmp_path)
    d = date(2026, 10, 14)
    pm.run_once(d, now=tpe(2026, 10, 14, 8, 46), **w.kw)
    res = pm.backfill_once(d, now=tpe(2026, 10, 14, 9, 0), **w.kw)
    assert res == {"status": "still_missing", "missing": ["sox", "tsm", "vix"]} and [r["kind"] for r in w.store.raw_rows()] == ["snapshot"]


def test_backfill_ignores_days_without_a_record_and_voided_days(tmp_path):
    w = stale_world(tmp_path)
    d = date(2026, 10, 14)
    assert pm.backfill_once(d, **w.kw)["status"] == "no_record"
    pm.run_once(d, now=tpe(2026, 10, 14, 8, 46), **w.kw)
    w.store.add_void(d, "颱風假")
    assert pm.pending_backfill(w.store, d) == [] and pm.backfill_once(d, **w.kw)["status"] == "no_record"


def test_only_retryable_us_gaps_are_backfilled(tmp_path):
    w = World(tmp_path, ticks=False)                                                                         # 夜盤報酬缺（沒有本機成交）：補不回來
    d = date(2026, 10, 14)
    pm.run_once(d, now=tpe(2026, 10, 14, 8, 50), **w.kw)
    assert w.store.effective()[0]["features"]["txf_night"]["value"] is None
    assert pm.pending_backfill(w.store, d) == [] and pm.backfill_once(d, **w.kw)["status"] == "nothing_to_do"          # 美股欄位都有值：什麼都不補
    w2 = World(tmp_path / "us_closed", prev=date(2026, 5, 25)) if (tmp_path / "us_closed").mkdir() is None else None
    pm.run_once(date(2026, 5, 26), now=tpe(2026, 5, 26, 8, 50), **w2.kw)                                      # 美股休市：欄位是 us_closed，不是來源慢
    assert pm.pending_backfill(w2.store, date(2026, 5, 26)) == []
    assert pm.backfill_once(date(2026, 5, 26), **w2.kw) == {"status": "nothing_to_do"} and [r["kind"] for r in w2.store.raw_rows()] == ["snapshot"]   # 休市的空值補不回來：連試都不試


def test_pending_backfill_only_looks_at_recent_days(tmp_path):
    w = stale_world(tmp_path)
    pm.run_once(date(2026, 10, 14), now=tpe(2026, 10, 14, 8, 46), **w.kw)
    assert pm.pending_backfill(w.store, date(2026, 10, 14)) == [date(2026, 10, 14)]
    assert pm.BACKFILL_DAYS == 5
    assert pm.pending_backfill(w.store, date(2026, 10, 14) + timedelta(days=5)) == [date(2026, 10, 14)]
    assert pm.pending_backfill(w.store, date(2026, 10, 14) + timedelta(days=6)) == []                             # 太久以前的不再試


def test_backfill_pass_keeps_going_after_a_failure_and_logs_it(tmp_path, caplog):
    w = stale_world(tmp_path, last=date(2026, 10, 9))                                                       # 來源停在 10-09：10-13（要 10-12）、10-14（要 10-13）兩天都缺
    for day, prev in ((date(2026, 10, 13), date(2026, 10, 12)), (date(2026, 10, 14), date(2026, 10, 13))):
        make_ticks(w.db, session_ticks(prev))
        pm.run_once(day, now=datetime.combine(day, dtime(8, 46), tzinfo=pm.TPE), **w.kw)
    publish(w, "sox", "tsm", "vix")
    real = pm.backfill_once

    def flaky(d, **kw):
        if d == date(2026, 10, 14):
            raise RuntimeError("來源暫時壞掉")
        return real(d, **kw)

    pm_backfill = pm.backfill_once
    try:
        pm.backfill_once = flaky
        with caplog.at_level("WARNING"):
            out = pm.backfill_pass(tpe(2026, 10, 14, 12, 0), store=w.store, cal=w.cal, ticks_path=w.db, http=w.http)
    finally:
        pm.backfill_once = pm_backfill
    assert [o["date"] for o in out] == ["2026-10-13"] and out[0]["status"] == "filled" and "補值失敗" in caplog.text
    con = sqlite3.connect(w.store.path)
    try:
        assert con.execute("SELECT where_, trading_date, error FROM premarket_errors").fetchall() == [("backfill", "2026-10-14", "RuntimeError: 來源暫時壞掉")]
    finally:
        con.close()
    assert pm.alerts.count == 0                                                                                # 記進 premarket_errors，不算失敗警示


def test_the_loop_runs_a_backfill_pass_at_start_and_then_every_ten_minutes(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(pm.time, "time", lambda: clock[0])
    seen = []
    d1 = date(2026, 10, 14)

    def tick(now):
        seen.append(now)
        return []

    n = {"i": 0}

    async def fake_sleep(s):
        n["i"] += 1
        clock[0] += 200.0                                                                                  # 每圈過 200 秒
        if n["i"] > 6:
            raise asyncio.CancelledError

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.combine(d1, dtime(7, 0), tzinfo=pm.TPE)                                        # 7:00：還沒到 T，主要邏輯什麼都不做

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(pm, "datetime", FakeDT)
    monkeypatch.setattr(pm, "_backfill_tick", tick)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(pm.premarket_log_loop())
    # 時鐘每圈 +200 秒：第 1 圈（t＝1200）掃一次，之後要等 600 秒＝第 4 圈（t＝1800）才再掃；第 2、3、5、6 圈不掃
    assert len(seen) == 2 and all(isinstance(x, datetime) for x in seen)


def test_the_loop_survives_a_failing_backfill_tick(monkeypatch, caplog):
    def boom(now):
        raise RuntimeError("補值壞了")

    d1 = date(2026, 10, 14)
    with caplog.at_level("ERROR"):
        calls, _ = drive_loop(monkeypatch, [{"status": "written"}], [d1, d1, d1], backfill=boom)
    assert "盤前跨市場資訊紀錄迴圈例外" in caplog.text                                                        # 例外被迴圈接住（不會讓整個迴圈死掉）


def test_three_successive_corrections_all_accumulate(tmp_path):
    w = written(tmp_path)
    d = date(2026, 10, 14)
    w.store.add_correction(d, {"sox": {"value": 0.01}}, "一")
    w.store.add_correction(d, {"vix": {"value": 15.5}}, "二")
    w.store.add_correction(d, {"tsm": {"value": -0.02}}, "三")
    eff = w.store.effective()[0]
    assert (eff["features"]["sox"]["value"], eff["features"]["vix"]["value"], eff["features"]["tsm"]["value"]) == (0.01, 15.5, -0.02) and eff["correction_note"] == "三"
    assert [r["kind"] for r in w.store.raw_rows()] == ["snapshot", "correction", "correction", "correction"]
