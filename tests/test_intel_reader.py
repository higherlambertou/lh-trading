"""〈資訊〉面板的資料來源（core/intel_reader.py）：唯讀讀 lh-intel 的 intel.db。重點是「當時已知」的規則、換算與缺值、不丟例外、沒有任何交易模組讀它。"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import core.intel_reader as ir
from intel_fixture import event, headline, make_db, obs, run

TZ = ir.TAIPEI
NOW = datetime(2026, 10, 19, 20, 0, tzinfo=TZ).timestamp()                 # 週一晚上 8 點（台北）
TODAY = date(2026, 10, 19)
H = 3600.0


@pytest.fixture
def db(tmp_path):
    c = make_db(tmp_path / "intel.db")
    yield c
    c.close()


def conn_of(c) -> sqlite3.Connection:
    c2 = ir.connect_ro(Path(c.execute("PRAGMA database_list").fetchone()[2]))
    return c2


# ── 唯讀與可見性 ────────────────────────────────────────────────

def test_the_connection_is_read_only(db, tmp_path):
    ro = ir.connect_ro(tmp_path / "intel.db")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        ro.execute("INSERT INTO schema_meta VALUES('x','y')")
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("DELETE FROM observations")
    assert ro.execute("SELECT value FROM schema_meta").fetchone()[0] == "1"


def test_latest_points_only_use_what_was_known_at_t(db, tmp_path):
    obs(db, "fred", "DGS10", "2026-10-15", 5.10, NOW - 50 * H)
    obs(db, "fred", "DGS10", "2026-10-16", 5.20, NOW - 30 * H)
    obs(db, "fred", "DGS10", "2026-10-16", 5.25, NOW - 5 * H, revision=1)                      # 修正：5 小時前才知道
    obs(db, "fred", "DGS10", "2026-10-19", 5.40, NOW + 5 * H)                                  # 未來才抓到的：現在看不到
    ro = ir.connect_ro(tmp_path / "intel.db")
    now = ir.latest_points(ro, "fred", "DGS10", NOW, 5)
    assert [(r["event_date"], r["value"]) for r in now] == [("2026-10-16", 5.25), ("2026-10-15", 5.10)]
    earlier = ir.latest_points(ro, "fred", "DGS10", NOW - 10 * H, 5)
    assert [(r["event_date"], r["value"]) for r in earlier] == [("2026-10-16", 5.20), ("2026-10-15", 5.10)]                     # 5 小時前還不知道修正
    assert [r["event_date"] for r in ir.latest_points(ro, "fred", "DGS10", NOW + 10 * H, 1)] == ["2026-10-19"]                  # n 限制日期數
    assert ir.latest_points(ro, "fred", "NOPE", NOW) == [] and ir.latest_points(ro, "other", "DGS10", NOW) == []


def test_the_panel_shows_backfilled_rows_because_they_are_known_now_but_never_rows_fetched_after_t(db, tmp_path):
    """顯示「現在」：回補列現在確實已經知道了，要顯示（面板要的是目前最新的資料）；但 fetched_at 比 t 晚的一律不顯示。
    研究／決策用的 as_of 規則（回補列預設不可見）在 lh-intel/intel/read.py，不是這個模組。"""
    obs(db, "fred", "S", "2026-10-14", 1.0, NOW - 1 * H, backfilled=1)
    obs(db, "fred", "S", "2026-10-15", 2.0, NOW - 1 * H, backfilled=1, published_at=NOW - 20 * H)
    obs(db, "fred", "S", "2026-10-16", 3.0, NOW + 2 * H, backfilled=1)                          # 還沒抓到
    ro = ir.connect_ro(tmp_path / "intel.db")
    assert [(r["event_date"], r["value"]) for r in ir.latest_points(ro, "fred", "S", NOW, 5)] == [("2026-10-15", 2.0), ("2026-10-14", 1.0)]
    assert [(r["event_date"], r["value"]) for r in ir.latest_points(ro, "fred", "S", NOW - 2 * H, 5)] == []                              # t 比抓到的時間早：什麼都沒有


# ── 全球市場 ────────────────────────────────────────────────────

def test_markets_give_change_percent_age_and_handle_missing_series(db, tmp_path):
    obs(db, "cboe", "VIX.close", "2026-10-15", 16.46, NOW - 30 * H)
    obs(db, "cboe", "VIX.close", "2026-10-16", 19.99, NOW - 20 * H)
    obs(db, "fred", "DGS10", "2026-10-14", 5.19, NOW - 60 * H)
    obs(db, "fred", "DGS10", "2026-10-15", 5.10, NOW - 40 * H)
    obs(db, "nasdaq", "TSM.close", "2026-10-16", 412.61, NOW - 20 * H)                          # 只有一天：沒有前一天可比
    m = {x["key"]: x for x in ir.markets(ir.connect_ro(tmp_path / "intel.db"), NOW, TODAY)}
    v = m["VIX"]
    assert (v["value"], v["prev"], v["chg"], v["date"], v["prev_date"], v["age_days"], v["kind"], v["unit"]) == (19.99, 16.46, 3.53, "2026-10-16", "2026-10-15", 3, "price", "points")
    assert v["chg_pct"] == round(3.53 / 16.46 * 100, 3)
    y = m["US10Y"]
    assert (y["kind"], y["chg"], y["unit"]) == ("rate", -0.09, "pct") and y["chg_pct"] == round(-0.09 / 5.19 * 100, 3)               # 殖利率類型標 rate（畫面用「點」不用百分比）
    t = m["TSM"]
    assert t["value"] == 412.61 and t["prev"] is None and t["chg"] is None and t["chg_pct"] is None
    s = m["SOX"]
    assert s["value"] is None and s["date"] is None and "chg" not in s                           # 完全沒有資料的序列：只有標籤
    assert [x["key"] for x in ir.markets(ir.connect_ro(tmp_path / "intel.db"), NOW, TODAY)] == [k[0] for k in ir.MARKETS]


def test_a_missing_value_is_shown_as_missing_not_zero(db, tmp_path):
    obs(db, "yahoo", "HSI.close", "2026-10-16", None, NOW - 10 * H)
    h = {x["key"]: x for x in ir.markets(ir.connect_ro(tmp_path / "intel.db"), NOW, TODAY)}["HSI"]
    assert h["value"] is None and h["date"] == "2026-10-16" and h["chg"] is None


# ── 籌碼 ────────────────────────────────────────────────────────

def seed_fut(db, d, fetched, tx_foreign=(1000, 5000), dealer=(300, 200)):
    for who, (lg, sh) in (("foreign", tx_foreign), ("dealer", dealer)):
        obs(db, "taifex", f"futinst.TX.{who}.long_oi_lots", d, lg, fetched)
        obs(db, "taifex", f"futinst.TX.{who}.short_oi_lots", d, sh, fetched)
        obs(db, "taifex", f"futinst.TX.{who}.net_oi_lots", d, lg - sh, fetched)


def test_futures_chips_show_net_change_long_short_and_skip_missing_identities(db, tmp_path):
    seed_fut(db, "2026-10-15", NOW - 30 * H, (900, 5200), (250, 220))
    seed_fut(db, "2026-10-16", NOW - 6 * H, (1000, 5000), (300, 200))
    c = ir.chips(ir.connect_ro(tmp_path / "intel.db"), NOW, TODAY)
    (tx,) = c["futures"]
    assert tx["commodity"] == "TX" and tx["date"] == "2026-10-16" and [r["who"] for r in tx["rows"]] == ["foreign", "dealer"]                  # 投信沒有資料就不列；MTX／TMF 沒資料整組不列
    f = tx["rows"][0]
    assert (f["net_oi"], f["chg"], f["long_oi"], f["short_oi"], f["label"]) == (-4000, -4000 - (-4300), 1000, 5000, "外資")
    assert tx["rows"][1]["chg"] == 100 - 30


def test_options_and_pcr(db, tmp_path):
    for d, f in (("2026-10-15", NOW - 30 * H), ("2026-10-16", NOW - 6 * H)):
        k = 1 if d.endswith("15") else 2
        obs(db, "taifex", "optinst.TXO.CALL.foreign.net_oi_lots", d, -169 * k, f)
        obs(db, "taifex", "optinst.TXO.PUT.foreign.net_oi_lots", d, 5820 * k, f)
        obs(db, "taifex", "pcr.oi_ratio_pct", d, 60.0 * k, f)
        obs(db, "taifex", "pcr.vol_ratio_pct", d, 80.0 * k, f)
    c = ir.chips(ir.connect_ro(tmp_path / "intel.db"), NOW, TODAY)
    o = c["options"]
    assert o["date"] == "2026-10-16" and [r["who"] for r in o["rows"]] == ["foreign"] and o["rows"][0]["call_net_oi"] == -338 and o["rows"][0]["call_chg"] == -169 and o["rows"][0]["put_chg"] == 5820
    assert c["pcr"] == {"date": "2026-10-16", "oi_ratio": 120.0, "oi_ratio_prev": 60.0, "vol_ratio": 160.0, "vol_ratio_prev": 80.0}


def seed_inst(db, d, fetched, f, t, ds, dh, total):
    for s, v in (("foreign", f), ("trust", t), ("dealer_self", ds), ("dealer_hedge", dh), ("total", total)):
        obs(db, "twse", f"inst.{s}.net", d, v * 1e8, fetched)


def test_institutional_net_is_converted_to_100_million_ntd_with_a_five_day_sum(db, tmp_path):
    days = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19"]
    for i, d in enumerate(days):
        seed_inst(db, d, NOW - (10 - i) * H, -100 - i, 50 + i, -10, -20 - i, -80 - 3 * i)
    inst = ir.chips(ir.connect_ro(tmp_path / "intel.db"), NOW, TODAY)["inst"]
    assert inst["date"] == "2026-10-19"
    r = {x["who"]: x for x in inst["rows"]}
    assert r["foreign"]["net"] == -105.0 and r["foreign"]["days5"] == 5 and r["foreign"]["net5"] == -(101 + 102 + 103 + 104 + 105)                # 今天 −105 億；最近 5 個交易日合計（不含 10/12）
    assert r["dealer"]["net"] == -10 + -25 and r["dealer"]["label"] == "自營商" and r["trust"]["net"] == 55.0 and r["total"]["net"] == -95.0


def test_old_format_dealer_row_and_missing_components(db, tmp_path):
    obs(db, "twse", "inst.foreign.net", "2026-10-16", -50e8, NOW - 5 * H)
    obs(db, "twse", "inst.dealer.net", "2026-10-16", -7e8, NOW - 5 * H)                          # 舊格式：自營商一列
    r = {x["who"]: x for x in ir.chips(ir.connect_ro(tmp_path / "intel.db"), NOW, TODAY)["inst"]["rows"]}
    assert r["dealer"]["net"] == -7.0 and r["trust"]["net"] is None and r["total"]["net"] is None                                           # 沒有的就是 None，不是 0
    assert ir.chips(ir.connect_ro(tmp_path / "intel.db"), NOW, TODAY)["inst"]["rows"][2]["net5"] == -7.0
    obs(db, "twse", "inst.dealer_self.net", "2026-10-16", -3e8, NOW - 5 * H)                     # 新格式只有一半（缺避險）：整個自營商是 None，不拿一半當全部
    assert {x["who"]: x for x in ir.chips(ir.connect_ro(tmp_path / "intel.db"), NOW, TODAY)["inst"]["rows"]}["dealer"]["net"] is None


def test_margin_converts_units_and_is_marked_provisional(db, tmp_path):
    obs(db, "twse", "margin.financing_amt.today_balance", "2026-10-15", 646_415_871, NOW - 30 * H)       # 仟元
    obs(db, "twse", "margin.financing_amt.today_balance", "2026-10-16", 645_594_048, NOW - 6 * H)
    obs(db, "twse", "margin.short_lots.today_balance", "2026-10-15", 229_150, NOW - 30 * H)
    obs(db, "twse", "margin.short_lots.today_balance", "2026-10-16", 233_602, NOW - 6 * H)
    m = ir.chips(ir.connect_ro(tmp_path / "intel.db"), NOW, TODAY)["margin"]
    assert m == {"date": "2026-10-16", "financing_100m": 6455.9, "financing_chg_100m": -8.2, "short_lots": 233_602, "short_chg": 4452, "provisional": True}
    assert ir.chips(ir.connect_ro(tmp_path / "intel.db"), NOW - 20 * H, TODAY)["margin"]["financing_chg_100m"] is None                      # 只有一天
    assert ir.chips(ir.connect_ro(tmp_path / "intel.db"), NOW - 100 * H, TODAY)["margin"] is None


def test_chips_are_empty_not_broken_without_any_data(db, tmp_path):
    c = ir.chips(ir.connect_ro(tmp_path / "intel.db"), NOW, TODAY)
    assert c == {"futures": [], "options": None, "pcr": None, "inst": None, "margin": None}


# ── 事件 ────────────────────────────────────────────────────────

def test_upcoming_events_are_filtered_sorted_and_labelled(db, tmp_path):
    fomc = datetime(2026, 10, 29, 3, 0, tzinfo=TZ).timestamp()                                                # 台北 10/29 03:00（美東 10/28 14:00 EDT）
    event(db, "fomc-2026-10-28", "FOMC 利率決策", when=fomc, date="2026-10-28", importance=3, detail={"zh": "FOMC 利率決策"})
    event(db, "bls-1", "Consumer Price Index", when=NOW + 30 * H, date="2026-10-21", importance=3, source="bls", detail={"zh": "美國消費者物價指數 CPI"})
    event(db, "bls-2", "Real Earnings", when=NOW + 31 * H, date="2026-10-21", importance=1, source="bls")                  # 重要度 1：預設不列
    event(db, "fred-53-x", "Gross Domestic Product", when=None, date="2026-10-22", importance=3, source="fred", detail={"zh": "美國 GDP"})
    event(db, "bls-far", "Employment Situation", when=NOW + 20 * 86400, date="2026-11-08", importance=3, source="bls")     # 超過 14 天：不列
    event(db, "bls-old", "PPI", when=NOW - 10 * H, date="2026-10-19", importance=2, source="bls")                         # 10 小時前：超過 6 小時，不列
    event(db, "bls-justnow", "JOLTS", when=NOW - 2 * H, date="2026-10-19", importance=2, source="bls")                     # 2 小時前：列出並標 past
    event(db, "x-cancel", "取消的", when=NOW + 5 * H, date="2026-10-20", importance=3, status="cancelled")
    ev = ir.events(ir.connect_ro(tmp_path / "intel.db"), NOW, TODAY)
    assert [e["uid"] for e in ev["upcoming"]] == ["bls-justnow", "bls-1", "fred-53-x", "fomc-2026-10-28"]                    # 依時間排序；date-only 的排在它的日期位置
    first, cpi = ev["upcoming"][0], ev["upcoming"][1]
    assert first["past"] is True and first["minutes_to"] == -120 and cpi["past"] is False and cpi["minutes_to"] == 30 * 60
    assert cpi["title"] == "美國消費者物價指數 CPI" and cpi["title_en"] == "Consumer Price Index" and cpi["taipei_weekday"] == "三" and cpi["taipei_time"] == (datetime.fromtimestamp(NOW + 30 * H, TZ)).strftime("%m/%d %H:%M")
    gdp = ev["upcoming"][2]
    assert gdp["scheduled_time"] is None and gdp["taipei_time"] is None and gdp["minutes_to"] is None and gdp["title"] == "美國 GDP" and gdp["local_date"] == "2026-10-22"
    assert ev["upcoming"][3]["taipei_time"] == "10/29 03:00" and ev["upcoming"][3]["taipei_weekday"] == "四"
    assert [e["uid"] for e in ir.events(ir.connect_ro(tmp_path / "intel.db"), NOW, TODAY, min_importance=1)["upcoming"]][:3] == ["bls-justnow", "bls-1", "bls-2"]


def test_only_the_latest_visible_revision_of_an_event_is_used_and_future_knowledge_is_hidden(db, tmp_path):
    event(db, "bls-a", "CPI", when=NOW + 10 * H, date="2026-10-20", importance=3, source="bls", revision=0, fetched_at=NOW - 100 * H)
    event(db, "bls-a", "CPI", when=NOW + 12 * H, date="2026-10-20", importance=3, source="bls", revision=1, fetched_at=NOW - 2 * H)          # 改期
    event(db, "bls-a", "CPI", when=NOW + 99 * H, date="2026-10-23", importance=3, source="bls", revision=2, fetched_at=NOW + 1 * H)          # 未來才知道的改期
    ro = ir.connect_ro(tmp_path / "intel.db")
    items = ir.events(ro, NOW, TODAY)["upcoming"]
    assert [(e["uid"], e["minutes_to"]) for e in items] == [("bls-a", 12 * 60)]                                                                # 取已經抓到的最新版（rev 1）；未來才知道的改期（rev 2）不顯示


def test_closures_list_the_next_three_closed_days_only(db, tmp_path):
    for i, (d, closed) in enumerate((("2026-10-19", True), ("2026-10-23", False), ("2026-10-25", True), ("2026-10-26", True), ("2026-12-25", True), ("2026-12-28", False), ("2027-01-01", True), ("2027-02-11", True))):
        event(db, f"h{i}", f"休市{i}", date=d, importance=None, kind="holiday", source="twse_calendar", country="TW", detail={"closed": closed})
    c = ir.events(ir.connect_ro(tmp_path / "intel.db"), NOW, TODAY)["closures"]
    assert [x["date"] for x in c] == ["2026-10-19", "2026-10-26", "2026-12-25"] and c[0]["days_to"] == 0 and c[1]["days_to"] == 7 and c[1]["title"] == "休市3" and "2026-10-25" not in [x["date"] for x in c]       # 週末（10/25 是週日）本來就不開市，不列


# ── 新聞 ────────────────────────────────────────────────────────

def test_news_hides_duplicates_orders_by_publish_time_and_labels_sources(db, tmp_path):
    a = headline(db, "cna", "台股收跌", published_at=NOW - 3600, fetched_at=NOW - 3000)
    headline(db, "google_news", "台股收跌！", published_at=NOW - 3500, fetched_at=NOW - 2900, dup_of=a, publisher="經濟日報")           # 重複的
    headline(db, "google_news", "費半連八跌", published_at=NOW - 600, fetched_at=NOW - 500, publisher="理財周刊")
    headline(db, "google_news", "沒有發布時間", published_at=None, fetched_at=NOW - 100, publisher="UDN")
    headline(db, "cna", "未來才抓到的", published_at=NOW + 10, fetched_at=NOW + 20)
    n = ir.news(ir.connect_ro(tmp_path / "intel.db"), NOW)
    assert [x["title"] for x in n] == ["沒有發布時間", "費半連八跌", "台股收跌"]                                                                # 沒發布時間的用抓到的時間排；dup 與未來的不列
    assert [x["label"] for x in n] == ["UDN", "理財周刊", "中央社"] and n[1]["age_min"] == 10 and n[0]["time"] == NOW - 100
    assert len(ir.news(ir.connect_ro(tmp_path / "intel.db"), NOW, limit=2)) == 2


# ── 收集器狀態 ──────────────────────────────────────────────────

def test_collector_health_counts_consecutive_errors_and_hides_backfill_runs(db, tmp_path):
    for i, st in enumerate(["ok", "error", "error"]):
        run(db, "twse_inst", NOW - 1000 + i * 100, st)
    run(db, "twse_inst:backfill", NOW - 50, "error")
    run(db, "news_rss", NOW - 300, "ok")
    run(db, "news_rss", NOW - 100, "not_ready")
    run(db, "fomc_calendar", NOW - 10, None)
    (tmp_path / "heartbeat").write_text(f"{NOW - 45:.0f}", encoding="utf-8")
    c = ir.collectors(ir.connect_ro(tmp_path / "intel.db"), NOW, ir._heartbeat(tmp_path / "intel.db"))
    by = {x["name"]: x for x in c["items"]}
    assert set(by) == {"twse_inst", "news_rss", "fomc_calendar"} and by["twse_inst"]["fails"] == 2 and by["twse_inst"]["last_status"] == "error" and by["news_rss"]["fails"] == 0 and by["news_rss"]["last_status"] == "not_ready"
    assert by["news_rss"]["last_ok_age_sec"] == 299 and by["fomc_calendar"]["last_status"] is None and by["fomc_calendar"]["last_ok"] is None
    assert c["problems"] == ["twse_inst"] and c["heartbeat_age_sec"] == 45 and c["service_ok"] is True


def test_the_service_is_not_ok_without_a_recent_heartbeat(db, tmp_path):
    ro = ir.connect_ro(tmp_path / "intel.db")
    assert ir.collectors(ro, NOW, None)["service_ok"] is False and ir.collectors(ro, NOW, None)["heartbeat_age_sec"] is None
    assert ir.collectors(ro, NOW, NOW - 179)["service_ok"] is True and ir.collectors(ro, NOW, NOW - 180)["service_ok"] is False
    assert ir._heartbeat(tmp_path / "intel.db") is None
    (tmp_path / "heartbeat").write_text("garbage", encoding="utf-8")
    assert ir._heartbeat(tmp_path / "intel.db") is None


# ── summary()：永遠不丟例外 ──────────────────────────────────────

@pytest.fixture(autouse=True)
def _fresh_cache():
    ir._cache.update(key=None, at=0.0, data=None)
    yield
    ir._cache.update(key=None, at=0.0, data=None)


def test_summary_assembles_everything_for_a_seeded_database(db, tmp_path):
    obs(db, "cboe", "VIX.close", "2026-10-16", 19.99, NOW - 6 * H)
    seed_fut(db, "2026-10-16", NOW - 6 * H)
    event(db, "bls-1", "Consumer Price Index", when=NOW + 30 * H, date="2026-10-21", importance=3, source="bls")
    headline(db, "cna", "標題", published_at=NOW - 60, fetched_at=NOW - 50)
    run(db, "news_rss", NOW - 100, "ok")
    s = ir.summary(NOW, tmp_path / "intel.db")
    assert s["available"] is True and s["schema_version"] == 1 and s["db"] == "intel.db" and s["as_of"] == NOW
    assert set(s) == {"available", "as_of", "schema_version", "db", "events", "chips", "markets", "news", "collectors"}
    assert s["markets"][0]["value"] == 19.99 and s["chips"]["futures"][0]["commodity"] == "TX" and s["events"]["upcoming"][0]["uid"] == "bls-1" and s["news"][0]["title"] == "標題"
    json.dumps(s, allow_nan=False)                                                                            # 要能直接當 JSON 回給前端（沒有 NaN）


def test_summary_never_raises_and_says_why(tmp_path, monkeypatch):
    missing = ir.summary(NOW, tmp_path / "nope.db")
    assert missing["available"] is False and "找不到資料庫" in missing["reason"] and "INTEL_DB" in missing["reason"]
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a database" * 100)
    assert ir.summary(NOW, junk)["available"] is False and "讀取 intel.db 失敗" in ir.summary(NOW, junk)["reason"]
    empty = tmp_path / "empty.db"
    sqlite3.connect(str(empty)).close()
    assert ir.summary(NOW, empty)["available"] is False                                                        # 沒有 schema_meta
    make_db(tmp_path / "future.db", version=99).close()
    fut = ir.summary(NOW, tmp_path / "future.db")
    assert fut["available"] is False and "schema_version=99" in fut["reason"] and "更新" in fut["reason"]
    c = make_db(tmp_path / "ok.db")
    monkeypatch.setattr(ir, "events", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("壞了")))
    bad = ir.summary(NOW, tmp_path / "ok.db")
    assert bad["available"] is False and "RuntimeError" in bad["reason"] and "壞了" in bad["reason"]
    c.close()


def test_summary_is_cached_for_ten_seconds_but_failures_are_not(db, tmp_path):
    path = tmp_path / "intel.db"
    obs(db, "cboe", "VIX.close", "2026-10-16", 19.99, NOW - 6 * H)
    first = ir.summary(NOW, path)
    obs(db, "cboe", "VIX.close", "2026-10-19", 25.0, NOW - 1)
    assert ir.summary(NOW + 9, path) is first                                                                  # 10 秒內：同一份
    assert ir.summary(NOW + 10, path)["markets"][0]["value"] == 25.0                                           # 過了：重算
    ir._cache.update(key=None, at=0.0, data=None)
    assert ir.summary(NOW, tmp_path / "nope.db")["available"] is False and ir._cache["data"] is None
    assert ir.summary(NOW, path)["available"] is True


def test_default_path_follows_the_env_and_otherwise_the_sibling_repo(monkeypatch):
    monkeypatch.delenv("INTEL_DB", raising=False)
    assert ir.default_path() == ir.ROOT.parent / "lh-intel" / "data" / "intel.db"
    monkeypatch.setenv("INTEL_DB", "~/x/intel.db")
    assert ir.default_path() == Path("~/x/intel.db").expanduser()
    monkeypatch.setenv("INTEL_DB", "  ")
    assert ir.default_path() == ir.ROOT.parent / "lh-intel" / "data" / "intel.db"


# ── 沒有任何交易模組讀它 ─────────────────────────────────────────

def test_no_trading_module_reads_the_intel_reader():
    root = ir.ROOT
    for name in ("strategies", "core/broker.py", "core/shioaji_worker.py", "core/manual_monitor.py", "core/protection.py", "core/quote_hub.py", "core/trade_log.py",
                 "core/entry_log.py", "api/routes_order.py", "api/routes_strategy.py", "api/routes_position.py", "api/routes_quote.py"):
        p = root / name
        for f in (list(p.rglob("*.py")) if p.is_dir() else [p]):
            if f.exists():
                src = f.read_text(encoding="utf-8")
                assert "intel_reader" not in src and "intel.db" not in src, f
    for f in (root / "core").glob("*risk*.py"):
        assert "intel_reader" not in f.read_text(encoding="utf-8"), f


# ── 與 lh-intel 真正的 schema 與寫入函式對照（它在旁邊時）────────────────

LH_INTEL = ir.ROOT.parent / "lh-intel" / "intel" / "db.py"


@pytest.mark.skipif(not LH_INTEL.exists(), reason="lh-intel 不在旁邊")
def test_reader_works_on_a_database_written_by_the_real_lh_intel_code(tmp_path):
    spec = importlib.util.spec_from_file_location("lh_intel_db", LH_INTEL)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    path = tmp_path / "real.db"
    c = real.connect(path)
    f = NOW - 6 * H
    real.put_observation(c, source="cboe", series="VIX.close", event_date="2026-10-15", value=16.46, fetched_at=NOW - 30 * H)
    real.put_observation(c, source="cboe", series="VIX.close", event_date="2026-10-16", value=19.99, fetched_at=f)
    real.put_observation(c, source="cboe", series="VIX.close", event_date="2026-10-16", value=20.5, fetched_at=NOW - 1 * H)                  # 修正：新 revision
    real.put_observation(c, source="cboe", series="VIX.close", event_date="2026-10-09", value=1.0, fetched_at=NOW - 1 * H, backfilled=True)   # 回補
    real.put_event(c, uid="bls-x", source="bls", kind="macro", title="Consumer Price Index", status="scheduled", fetched_at=NOW - 100 * H, country="US", scheduled_time=NOW + 30 * H,
                   scheduled_date="2026-10-21", importance=3, detail={"zh": "美國 CPI"})
    real.put_news(c, source="cna", url="https://a.example/1", url_canonical="https://a.example/1", title="標題", fetched_at=NOW - 50, published_at=NOW - 60)
    rid = real.start_run(c, "news_rss", NOW - 100)
    real.finish_run(c, rid, NOW - 99, "ok", 1)
    c.commit()
    c.close()
    s = ir.summary(NOW, path)
    assert s["available"] is True and s["schema_version"] == real.SCHEMA_VERSION == ir.SUPPORTED_SCHEMA
    vix = s["markets"][0]
    assert (vix["value"], vix["prev"], vix["date"]) == (20.5, 16.46, "2026-10-16")                              # 取已抓到的最新修正（回補列也算已知，但不是最近兩天）
    assert s["events"]["upcoming"][0]["title"] == "美國 CPI" and s["news"][0]["title"] == "標題" and s["collectors"]["items"][0]["name"] == "news_rss"


@pytest.mark.skipif(not LH_INTEL.exists(), reason="lh-intel 不在旁邊")
def test_the_schema_copy_in_the_tests_has_every_column_the_real_schema_has(tmp_path):
    spec = importlib.util.spec_from_file_location("lh_intel_db2", LH_INTEL)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    r = real.connect(tmp_path / "real.db")
    mine = make_db(tmp_path / "mine.db")
    for t in ("observations", "events", "news", "collector_runs", "schema_meta"):
        theirs = {x[1] for x in r.execute(f"PRAGMA table_info({t})")}
        ours = {x[1] for x in mine.execute(f"PRAGMA table_info({t})")}
        assert ours == theirs, (t, ours ^ theirs)                                                              # 契約的欄位改了、這邊的複本沒跟著改，就會被抓到


def test_futures_chips_list_big_mini_and_micro_in_that_order(db, tmp_path):
    for code in ("TMF", "MTX", "TX"):                                                                       # 故意倒著種
        for s, v in (("long_oi_lots", 10), ("short_oi_lots", 4), ("net_oi_lots", 6)):
            obs(db, "taifex", f"futinst.{code}.foreign.{s}", "2026-10-16", v, NOW - 6 * H)
    f = ir.chips(ir.connect_ro(tmp_path / "intel.db"), NOW, TODAY)["futures"]
    assert [x["commodity"] for x in f] == ["TX", "MTX", "TMF"] and [x["label"] for x in f] == ["臺股期貨（大台）", "小型臺指", "微型臺指"]


def test_an_old_failure_before_a_success_does_not_count_as_consecutive(db, tmp_path):
    for i, st in enumerate(["error", "ok", "error"]):                                                       # 由舊到新：失敗、成功、失敗
        run(db, "c", NOW - 1000 + i * 100, st)
    by = {x["name"]: x for x in ir.collectors(ir.connect_ro(tmp_path / "intel.db"), NOW, None)["items"]}
    assert by["c"]["fails"] == 1 and by["c"]["last_ok_age_sec"] == 899                                       # 只有最後那一次；成功之前的不算


def test_an_unavailable_result_is_never_served_from_the_cache(tmp_path):
    path = tmp_path / "x.db"
    make_db(path, version=99).close()
    first = ir.summary(NOW, path)
    assert first["available"] is False and "schema_version=99" in first["reason"]
    path.unlink()
    make_db(path, version=1).close()                                                                         # 10 秒內換成可以讀的資料庫：不能還在回舊的「不可用」
    assert ir.summary(NOW + 1, path)["available"] is True
