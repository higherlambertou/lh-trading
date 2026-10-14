"""依波動的建議停損停利（core/stop_suggest.py、GET /api/market/suggest，update.md 發現 33）。"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time

import numpy as np
import pytest

import api.routes_market as rm
import core.stop_calibration as sc
import core.stop_suggest as ss


# ── σ：和校準用的定義完全一致 ─────────────────────────────────────

def test_sigma_of_alternating_closes_is_hand_computable():
    closes = [100.0 + (i % 2) for i in range(31)]                      # 30 個變動：+1、−1、…
    sigma, n = ss.sigma_from_closes(closes)
    assert n == 30 and sigma == pytest.approx(math.sqrt(30 / 29))


def test_sigma_uses_only_the_last_31_closes_and_needs_25_valid_changes():
    closes = [5000.0] * 20 + [100.0 + (i % 2) for i in range(31)]      # 前面的不算
    assert ss.sigma_from_closes(closes)[0] == pytest.approx(math.sqrt(30 / 29))
    bad = [100.0 + (i % 2) for i in range(31)]
    for i in (3, 9, 15, 21, 27):                                       # 五個孤立缺值＝10 個無效變動 → 20 個 < 25
        bad[i] = None
    assert ss.sigma_from_closes(bad) == (None, 20)
    ok = [100.0 + (i % 2) for i in range(31)]
    ok[5] = ok[20] = None                                              # 兩個缺值＝4 個無效 → 26 個
    s, n = ss.sigma_from_closes(ok)
    assert n == 26 and s is not None
    assert ss.sigma_from_closes([]) == (None, 0) and ss.sigma_from_closes([float("nan")] * 31) == (None, 0)


def test_sigma_agrees_with_the_calibration_definition_on_random_data():
    rng = np.random.default_rng(4)
    cl = 20000.0 + np.cumsum(rng.standard_normal(200) * 3)
    cl[rng.random(200) < 0.04] = np.nan
    ref = sc.trailing_sigma(cl)
    for t in range(30, 200, 7):
        got, _ = ss.sigma_from_closes(cl[t - 30:t + 1].tolist())
        assert (got is None and np.isnan(ref[t])) or got == pytest.approx(ref[t])


# ── 從 flow_1m 取最近 31 個已收完的分鐘 ───────────────────────────

def test_closes_from_flow_skips_the_unfinished_minute_and_marks_gaps():
    m = 6000                                                           # 現在是第 6000 分鐘的第 30 秒
    now = m * 60 + 30
    rows = [{"ts": mm * 60, "close": float(mm)} for mm in (5969, 5970, 5990, 5997, 5999, 6000)]      # 6000＝還沒收完；5998 沒成交
    got = ss.closes_from_flow(rows, now)
    assert len(got) == 31
    assert got[30] == 5999.0 and got[29] is None and got[28] == 5997.0 and got[0] == 5969.0 and got[1] == 5970.0
    assert 6000.0 not in got and got[10] is None and got[21] == 5990.0               # 5979 那分鐘沒成交；5990 在第 21 格


def test_closes_from_flow_with_no_rows_is_all_gaps():
    assert ss.closes_from_flow([], 123456.0) == [None] * 31


# ── 其他小東西 ───────────────────────────────────────────────────

def test_session_of_boundaries():
    mon, sat = 0, 5
    assert ss.session_of(8, 44, mon) == "night" and ss.session_of(8, 45, mon) == "day" and ss.session_of(13, 44, mon) == "day"
    assert ss.session_of(13, 45, mon) == "night" and ss.session_of(10, 0, sat) == "night" and ss.session_of(10, 0, 6) == "night" and ss.session_of(23, 0, 4) == "night"


def test_env_parameters_fall_back_on_nonsense(monkeypatch):
    for v, want in (("abc", 4.0), ("-1", 4.0), ("2.5", 2.5), ("0", 0.0)):
        monkeypatch.setenv("SUGGEST_COST_PTS", v)
        assert ss.cost_pts() == want
    monkeypatch.delenv("SUGGEST_COST_PTS")
    assert ss.cost_pts() == 4.0
    for v, want in (("0", 1.0), ("150", 1.0), ("x", 1.0), ("2", 2.0), ("0.5", 0.5)):
        monkeypatch.setenv("SUGGEST_RISK_PCT", v)
        assert ss.risk_pct() == want


def test_load_table_handles_missing_corrupt_wrong_version_and_reloads(tmp_path):
    p = tmp_path / "t.json"
    assert ss.load_table(p) is None
    p.write_text("{壞", encoding="utf-8")
    assert ss.load_table(p) is None
    p.write_text(json.dumps({"version": 2, "day": {}, "night": {}}), encoding="utf-8")
    assert ss.load_table(p) is None
    p.write_text(json.dumps({"version": 1, "day": {}, "night": {}, "tag": "a"}), encoding="utf-8")
    assert ss.load_table(p)["tag"] == "a"
    p.write_text(json.dumps({"version": 1, "day": {}, "night": {}, "tag": "b"}), encoding="utf-8")
    os.utime(p, (time.time() + 5, time.time() + 5))                    # mtime 變了就重讀
    assert ss.load_table(p)["tag"] == "b"


# ── build：用手算得出來的表 ───────────────────────────────────────

KS, KS_TP, KS_SL = [0.5, 1.0, 1.5, 2.0, 3.0], [0.5, 1.0, 1.5, 2.0, 3.0], [1.0, 1.5, 2.0, 3.0]


def cell_table(unstable_win=None):
    """每一格都是不一樣的數字，才看得出索引有沒有對錯：touch[k]＝0.50+0.01·k 的位置；win[i][j]＝0.10·(i+1)+0.01·(j+1)；unres[i][j]＝0.20+0.01·i+0.001·j。"""
    th = {"n": 1000, "days": 10, "few": False, "touch": [0.51, 0.52, 0.53, 0.54, 0.55], "touch_se": [0.0] * 5, "gauss": [0.0] * 5,
          "win": [[0.10 * (i + 1) + 0.01 * (j + 1) for j in range(4)] for i in range(5)],
          "unres": [[0.20 + 0.01 * i + 0.001 * j for j in range(4)] for i in range(5)],
          "unstable_touch": [False, False, False, True, False], "unstable_win": unstable_win or [[False] * 4 for _ in range(5)]}
    sess = {"sessions": 10, "from": "a", "to": "b", "table": {"15": th}}
    return {"version": 1, "ks": KS, "ks_tp": KS_TP, "ks_sl": KS_SL, "day": sess, "night": {**sess, "few": True}}


def test_build_arithmetic_with_hand_computed_numbers(monkeypatch):
    monkeypatch.delenv("SUGGEST_COST_PTS", raising=False)
    monkeypatch.delenv("SUGGEST_RISK_PCT", raising=False)
    out = ss.build(10.0, 30, session="day", horizon=15, contract="TMF", equity=50_000.0, table=cell_table())
    unit = 10 * math.sqrt(15)                                          # 38.7298
    assert out["noise"] == 38.7 and out["sigma"] == 10.0 and out["cost_pts"] == 4.0 and out["risk_pct"] == 1.0 and out["point_value"] == 10.0
    hi, mid, agg = out["rows"]
    assert (hi["name"], mid["name"], agg["name"]) == ("高勝率型", "平衡", "高賺賠比型")
    assert (hi["tp"], hi["sl"]) == (19, 77) and (mid["tp"], mid["sl"]) == (39, 58) and (agg["tp"], agg["sl"]) == (58, 39)     # round(0.5·38.73)＝19、round(2·38.73)＝77…
    assert hi["p0"] == pytest.approx(77 / 96, abs=1e-4) and mid["p0"] == pytest.approx(58 / 97, abs=1e-4) and agg["p0"] == pytest.approx(39 / 97, abs=1e-4)
    assert hi["need"] == pytest.approx(4 / 96, abs=1e-4) and mid["need"] == pytest.approx(4 / 97, abs=1e-4)                   # 成本 ÷ (停利＋停損)
    assert (hi["loss_twd"], mid["loss_twd"], agg["loss_twd"]) == (770, 580, 390)                                               # 點數 × 10 元
    assert (hi["loss_pct"], mid["loss_pct"], agg["loss_pct"]) == (1.54, 1.16, 0.78)
    assert (hi["max_lots"], mid["max_lots"], agg["max_lots"]) == (0, 0, 1)                                                     # 500 元風險預算 ÷ 每口虧損，往下取整
    assert hi["ratio"] == pytest.approx(19 / 77, abs=0.01) and agg["ratio"] == pytest.approx(58 / 39, abs=0.01)
    assert unit - 0.05 < out["noise"] < unit + 0.05


def test_build_reads_the_right_table_cells(monkeypatch):
    monkeypatch.delenv("SUGGEST_COST_PTS", raising=False)
    out = ss.build(10.0, 30, session="day", horizon=15, table=cell_table())
    hi, mid, agg = out["rows"]
    # 高勝率型：k_tp＝0.5（i＝0）、k_sl＝2.0（j＝2）；停損掃到機率看 k＝2.0 那一格（索引 3）
    assert hi["touch_sl"] == 0.54 and hi["touch_unstable"] is True
    win, un = 0.10 + 0.03, 0.20 + 0.002
    assert hi["within"] == {"tp": round(win * (1 - un), 4), "sl": round((1 - win) * (1 - un), 4), "none": round(un, 4)}
    # 平衡：k_tp＝1.0（i＝1）、k_sl＝1.5（j＝1）；touch 索引 2
    assert mid["touch_sl"] == 0.53 and mid["touch_unstable"] is False
    win, un = 0.20 + 0.02, 0.21 + 0.001
    assert mid["within"] == {"tp": round(win * (1 - un), 4), "sl": round((1 - win) * (1 - un), 4), "none": round(un, 4)}
    # 高賺賠比型：k_tp＝1.5（i＝2）、k_sl＝1.0（j＝0）；touch 索引 1
    assert agg["touch_sl"] == 0.52
    win, un = 0.30 + 0.01, 0.22
    assert agg["within"]["tp"] == round(win * (1 - un), 4) and agg["within"]["none"] == round(un, 4)
    for r in out["rows"]:
        assert sum(r["within"].values()) == pytest.approx(1.0, abs=2e-4)
    assert out["cal"] == {"sessions": 10, "n": 1000, "few": False, "from": "a", "to": "b"}


def test_build_flags_unstable_win_cells_and_few_samples():
    uw = [[False] * 4 for _ in range(5)]
    uw[1][1] = True                                                    # 平衡（i＝1、j＝1）
    out = ss.build(10.0, 30, session="day", horizon=15, table=cell_table(uw))
    assert [r["win_unstable"] for r in out["rows"]] == [False, True, False]
    night = ss.build(10.0, 30, session="night", horizon=15, table=cell_table())
    assert night["cal"]["few"] is True                                 # 夜盤一律標樣本少


def test_build_without_a_table_gives_numbers_but_no_calibrated_fields():
    out = ss.build(10.0, 30, session="day", horizon=15, table=None)
    assert out["calibrated"] is False and out["cal"] is None
    for r in out["rows"]:
        assert "touch_sl" not in r and "within" not in r and r["p0"] > 0 and r["tp"] > 0
    odd = ss.build(10.0, 30, session="day", horizon=10, table=cell_table())               # 表裡沒有這個持有時間
    assert odd["calibrated"] is True and odd["cal"] is None and "touch_sl" not in odd["rows"][0]


def test_build_scales_with_sigma_horizon_and_contract():
    a = ss.build(10.0, 30, session="day", horizon=15, contract="TMF", equity=100_000.0, table=None)
    b = ss.build(20.0, 30, session="day", horizon=15, contract="TMF", equity=100_000.0, table=None)
    assert [r["sl"] for r in b["rows"]] == [round(2 * r["sl"]) for r in a["rows"]] or all(abs(rb["sl"] - 2 * ra["sl"]) <= 1 for ra, rb in zip(a["rows"], b["rows"]))
    c = ss.build(10.0, 30, session="day", horizon=60, table=None)
    assert all(abs(rc["sl"] - 2 * ra["sl"]) <= 1 for ra, rc in zip(a["rows"], c["rows"]))     # T 變 4 倍 → 範圍 2 倍
    tx = ss.build(10.0, 30, session="day", horizon=15, contract="txf", equity=1_000_000.0, table=None)
    assert tx["contract"] == "TXF" and tx["point_value"] == 200.0 and tx["rows"][0]["loss_twd"] == tx["rows"][0]["sl"] * 200


def test_total_distance_is_the_same_for_all_three_presets_so_the_break_even_gap_is_too():
    out = ss.build(17.6, 30, session="day", horizon=15, table=None)
    sums = [r["tp"] + r["sl"] for r in out["rows"]]
    assert max(sums) - min(sums) <= 1 and max(r["need"] for r in out["rows"]) - min(r["need"] for r in out["rows"]) < 0.001


def test_a_longer_horizon_dilutes_the_cost():
    need = [ss.build(17.6, 30, session="day", horizon=T, table=None)["rows"][1]["need"] for T in ss.HORIZONS]
    assert need == sorted(need, reverse=True) and need[0] > 2 * need[-1]


def test_build_flags_a_target_beyond_the_expected_remaining_range():
    today = {"r20": 700.0, "done": 300.0, "expect_lo": 80.0, "expect_mid": 120.0, "expect_hi": 50.0}
    out = ss.build(10.0, 30, session="day", horizon=15, table=None, today=today)
    assert [r["far_tp"] for r in out["rows"]] == [False, False, True]                       # 停利 19、39、58 vs 上緣 50
    assert all(r["far_tp"] is False for r in ss.build(10.0, 30, session="day", horizon=15, table=None)["rows"])


def test_build_without_equity_has_no_risk_numbers():
    out = ss.build(10.0, 30, session="day", horizon=15, table=None, equity=None)
    assert all(r["loss_pct"] is None and r["max_lots"] is None and r["loss_twd"] > 0 for r in out["rows"])


def test_build_says_why_when_there_is_no_sigma():
    for sigma in (None, 0.0):
        out = ss.build(sigma, 12, session="day", horizon=15, table=cell_table())
        assert out["rows"] == [] and out["sigma"] in (None, 0.0) and "成交太少" in out["note"] and "12" in out["note"] and "noise" not in out


def test_risk_budget_follows_the_env(monkeypatch):
    monkeypatch.setenv("SUGGEST_RISK_PCT", "2")
    monkeypatch.setenv("SUGGEST_COST_PTS", "6")
    out = ss.build(10.0, 30, session="day", horizon=15, equity=50_000.0, table=None)
    assert out["risk_pct"] == 2.0 and out["cost_pts"] == 6.0
    assert out["rows"][0]["max_lots"] == int(1000 // 770) == 1 and out["rows"][0]["need"] == pytest.approx(6 / 96, abs=1e-4)


# ── suggest_now、今天的尺度 ───────────────────────────────────────

@pytest.fixture
def taipei(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("需要 POSIX 的 tzset")
    monkeypatch.setenv("TZ", "Asia/Taipei")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def at(y, mo, d, hh, mm, ss=30):
    return time.mktime((y, mo, d, hh, mm, ss, 0, 0, -1))


def flow(now_ts, n=31):
    base = int(now_ts // 60) * 60
    return [{"ts": base - 60 * i, "close": 20000.0 + (i % 2)} for i in range(1, n + 1)]


def test_suggest_now_picks_session_from_the_clock_and_attaches_today_only_in_the_day(taipei):
    ctx = {"r20": 700.0, "proj": {"early": 150.0, "done": 90.0, "expect": {"lo": 100.0, "mid": 160.0, "hi": 240.0}}}
    wed_day, wed_night = at(2026, 10, 14, 10, 15), at(2026, 10, 14, 20, 0)
    d = ss.suggest_now(flow(wed_day), wed_day, contract="TMF", horizon=15, equity=50_000.0, table=None, ctx=ctx)
    assert d["session"] == "day" and d["today"] == {"r20": 700.0, "early": 150.0, "done": 90.0, "expect_lo": 100.0, "expect_mid": 160.0, "expect_hi": 240.0}
    assert d["as_of"] == wed_day and d["sigma"] == pytest.approx(math.sqrt(30 / 29), abs=0.01) and len(d["rows"]) == 3
    n = ss.suggest_now(flow(wed_night), wed_night, contract="TMF", horizon=15, equity=50_000.0, table=None, ctx=ctx)
    assert n["session"] == "night" and n["today"] is None
    sat = at(2026, 10, 17, 10, 0)
    assert ss.suggest_now(flow(sat), sat, contract="TMF", horizon=15, equity=None, table=None)["session"] == "night"


def test_suggest_now_without_recent_trades_says_so(taipei):
    now = at(2026, 10, 14, 10, 15)
    out = ss.suggest_now([], now, contract="TMF", horizon=5, equity=None, table=None)
    assert out["rows"] == [] and "成交太少" in out["note"] and out["n_changes"] == 0


def test_today_from_ctx():
    assert ss.today_from_ctx(None) is None and ss.today_from_ctx({}) is None and ss.today_from_ctx({"proj_note": "09:15 之後才有"}) is None
    assert ss.today_from_ctx({"r20": 1.0, "proj": {"early": 2.0, "done": 3.0, "expect": {"lo": 4.0, "mid": 5.0, "hi": 6.0}}}) == \
        {"r20": 1.0, "early": 2.0, "done": 3.0, "expect_lo": 4.0, "expect_mid": 5.0, "expect_hi": 6.0}


# ── 真正的校準表（core/stop_calibration.json）──────────────────────

def test_the_shipped_table_is_sane():
    t = ss.load_table()
    assert t is not None and t["version"] == 1 and t["horizons"] == [5, 15, 30, 60] and t["sigma"]["window"] == 30
    assert t["day"]["sessions"] > 1000 and t["night"]["sessions"] >= 30 and t["night"]["few"] is True
    for sess in ("day", "night"):
        for T, th in t[sess]["table"].items():
            touch = th["touch"]
            assert all(0 < x < 1 for x in touch) and touch == sorted(touch, reverse=True)           # 距離越遠越不容易碰到
            assert all(0 <= u <= 1 for row in th["unres"] for u in row)
            for row in th["win"]:                                                                    # 停損越遠（欄越右）勝率越高
                assert row == sorted(row)
            for j in range(4):                                                                       # 停利越遠（列越下）勝率越低
                col = [th["win"][i][j] for i in range(5)]
                assert col == sorted(col, reverse=True)
            assert th["win"][1][0] == pytest.approx(0.5, abs=0.02) and th["win"][4][3] == pytest.approx(0.5, abs=0.02)   # 停利＝停損 → 一半一半
    d15 = t["day"]["table"]["15"]
    assert 0.10 < d15["touch"][2] < 0.16 and d15["touch"][4] > 0.005                                # 1.5σ 約 13%；3σ 實測約 1%（常態公式只有 0.3%）
    assert not any(d15["unstable_touch"]) and d15["n"] > 50_000


# ── API ─────────────────────────────────────────────────────────

class FakeStore:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def flow_1m(self, code, since_ts=None, until_ts=None):
        self.calls.append((code, since_ts))
        return self.rows


def test_route_validates_and_assembles(monkeypatch, taipei):
    now = at(2026, 10, 14, 10, 15)
    monkeypatch.setattr(time, "time", lambda: now)
    store = FakeStore(flow(now))
    monkeypatch.setattr(rm.market_state, "store", store)

    async def live():
        return {"ctx": {"r20": 700.0, "proj": {"early": 150.0, "done": 90.0, "expect": {"lo": 1.0, "mid": 2.0, "hi": 3.0}}}}

    monkeypatch.setattr(rm.market_state, "live_snapshot", live)
    monkeypatch.setitem(rm.position_cache, "margin", {"equity": 52318, "initial_margin": 35050})
    out = asyncio.run(rm.get_suggest("tmf", 15))
    assert store.calls == [("TMF", now - 3600)] and out["contract"] == "TMF" and out["equity"] == 52318.0 and out["horizon"] == 15
    assert out["session"] == "day" and out["today"]["expect_hi"] == 3.0 and len(out["rows"]) == 3 and out["calibrated"] is True
    assert out["rows"][1]["touch_sl"] is not None                                                   # 真的校準表
    with pytest.raises(rm.HTTPException) as e1:
        asyncio.run(rm.get_suggest("ABC", 15))
    assert e1.value.status_code == 422
    with pytest.raises(rm.HTTPException) as e2:
        asyncio.run(rm.get_suggest("TMF", 10))
    assert e2.value.status_code == 422 and "5, 15, 30, 60" in e2.value.detail


def test_route_survives_a_broken_live_snapshot_and_missing_margin(monkeypatch, taipei):
    now = at(2026, 10, 14, 10, 15)
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setattr(rm.market_state, "store", FakeStore(flow(now)))

    async def boom():
        raise RuntimeError("壞了")

    monkeypatch.setattr(rm.market_state, "live_snapshot", boom)
    monkeypatch.setitem(rm.position_cache, "margin", None)
    out = asyncio.run(rm.get_suggest("MXF", 30))
    assert out["today"] is None and out["equity"] is None and out["contract"] == "MXF" and out["rows"][0]["loss_pct"] is None


# ── 補：變異測試抓到的邊界 ───────────────────────────────────────

def test_exactly_25_valid_changes_is_enough_and_24_is_not():
    ok = [100.0 + (i % 2) for i in range(31)]
    ok[5] = None                                                       # 一個孤立缺值＝2 個無效變動
    ok[15] = ok[16] = None                                             # 連續兩個缺值＝3 個無效變動 → 30 − 5 ＝ 25 個有效
    s, n = ss.sigma_from_closes(ok)
    assert n == 25 and s is not None
    bad = [100.0 + (i % 2) for i in range(31)]
    bad[5] = bad[6] = bad[15] = bad[16] = None                         # 兩段各 3 個無效 → 24 個有效
    assert ss.sigma_from_closes(bad) == (None, 24)


def test_a_target_exactly_at_the_upper_edge_is_not_far():
    today = {"r20": 700.0, "done": 300.0, "expect_lo": 10.0, "expect_mid": 20.0, "expect_hi": 39.0}
    out = ss.build(10.0, 30, session="day", horizon=15, table=None, today=today)
    assert [r["tp"] for r in out["rows"]] == [19, 39, 58] and [r["far_tp"] for r in out["rows"]] == [False, False, True]    # 39 剛好等於上緣：不算太遠
