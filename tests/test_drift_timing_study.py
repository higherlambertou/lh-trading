"""漲幅發生在什麼時候（core/drift_timing_study.py，update.md 發現 72）：讀檔、同合約的報酬、月底月初的日子、植入／無關的世界。"""
from __future__ import annotations

import ast
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from core import drift_timing_study as st

ROOT = Path(__file__).resolve().parent.parent
HEADER = "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,價差對單式委託成交量"


def row(d, code, o, c, vol, sess="一般"):
    return f"{d},TX,{code}  ,{o},999,1,{c},1,1%,{vol},101,10,100,101,200,50,,{sess},"


def _nm(c):
    y, m = int(c[:4]), int(c[4:])
    return f"{y + (m == 12)}{m % 12 + 1:02d}"


def _pm(c):
    y, m = int(c[:4]), int(c[4:])
    return f"{y - (m == 1)}{(m - 2) % 12 + 1:02d}"


def world(seed=0, n=1500, mu_o=0.0, mu_d=0.0, tom=0.0, start=date(2003, 1, 2), night_from="2017-05-16"):
    rng = np.random.default_rng(seed)
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5 and rng.random() > 0.03:                    # 偶爾休市
            days.append(d.isoformat())
        d += timedelta(days=1)
    flags = st.tom_flags(days)
    out, c = {}, 10000.0
    for i, x in enumerate(days):
        o = mu_o + 0.008 * rng.standard_normal()
        dd = mu_d + 0.009 * rng.standard_normal() + (tom if flags[i] else 0.0)
        op = c * np.exp(o) if i else c
        cl = op * np.exp(dd)
        code = x[:4] + x[5:7]
        no, nc = {}, {}
        ks = (_pm(code), code, _nm(code))                                            # 上個月的合約在月初還在（真實的近月在月中才到期）
        if i and x >= night_from:
            no, nc = {k: c * np.exp(0.1 * o) for k in ks}, {k: c * np.exp(0.8 * o) for k in ks}
        out[x] = st.Day(code, {k: op for k in ks}, {k: cl for k in ks}, no, nc)
        c = cl
    return out


# ── 讀檔與定義 ────────────────────────────────────────────────────

def test_load_tx_day_and_night_rows(tmp_path):
    lines = [HEADER, row("2026/10/01", "202610", 100, 101, 5000), row("2026/10/01", "202611", 102, 103, 100),
             row("2026/10/01", "202610", 99, 100.5, 900, "盤後"),                    # 盤後列的日期＝它之後那個日盤
             row("2026/10/01", "202610/202611", 3, 1, 9999), row("2026/10/01", "202610W1", 500, 1, 99999),
             "2026/10/02,TX,202610  ,-,-,-,-,-,-,0,-,-,-,-,-,-,,一般,",                     # 沒成交：不算
             row("2026/10/02", "202611", 104, 105, 20)]
    (tmp_path / "tx_2026_10.csv").write_bytes(("\n".join(lines) + "\n").encode("cp950"))
    tx = st.load_tx(tmp_path)
    assert tx["2026-10-01"] == st.Day("202610", {"202610": 100.0, "202611": 102.0}, {"202610": 101.0, "202611": 103.0}, {"202610": 99.0}, {"202610": 100.5})
    assert tx["2026-10-02"] == st.Day("202611", {"202611": 104.0}, {"202611": 105.0}, {}, {})


def test_returns_use_the_same_contract_and_split_the_night():
    days = {"2026-10-20": st.Day("202610", {"202610": 100.0, "202611": 120.0}, {"202610": 101.0, "202611": 121.0}, {}, {}),
            "2026-10-21": st.Day("202611", {"202611": 123.0}, {"202611": 125.0}, {"202611": 122.0}, {"202611": 124.0}),   # 換月：用 11 月自己的前一天 121
            "2026-10-22": st.Day("202612", {"202612": 130.0}, {"202612": 131.0}, {}, {})}                               # 前一天沒有 12 月：略過
    b = st.build(days)
    assert list(b["date"]) == ["2026-10-21"]
    assert b["O"][0] == pytest.approx(np.log(123 / 121)) and b["D"][0] == pytest.approx(np.log(125 / 123)) and b["R"][0] == pytest.approx(np.log(125 / 121))
    assert (b["g1"][0], b["night"][0], b["g2"][0]) == pytest.approx((np.log(122 / 121), np.log(124 / 122), np.log(123 / 124)))
    assert b["g1"][0] + b["night"][0] + b["g2"][0] == pytest.approx(b["O"][0]) and b["wd"][0] == 2


def test_turn_of_month_days():
    dates = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-29", "2026-09-30",
             "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-30", "2026-11-02"]          # 10/1 休市
    assert st.tom_flags(dates) == [True, True, True, False, False, True, True, True, True, False, True, True]
    assert st.tom_flags(["2026-10-07", "2026-10-08"]) == [True, True]                                # 資料中途開始：當成月初（描述用）
    assert st.tom_flags([]) == []


def test_tom_trade_buys_the_day_before_month_end_and_sells_the_third_day():
    days = {d: st.Day("202610", {"202610": p}, {"202610": p}, {}, {}) for d, p in
            (("2026-09-28", 100.0), ("2026-09-29", 101.0), ("2026-09-30", 102.0), ("2026-10-02", 103.0), ("2026-10-05", 104.0), ("2026-10-06", 106.0), ("2026-10-07", 107.0))}
    tt = st.tom_trades(days)
    assert tt == [{"buy": "2026-09-29", "sell": "2026-10-06", "ret": pytest.approx(np.log(106 / 101) - st.COST)}]
    assert st.COST == pytest.approx(4 / 47000)
    days["2026-10-06"] = st.Day("202611", {"202610": 106.0, "202611": 150.0}, {"202610": 106.0, "202611": 150.0}, {}, {})   # 賣出那天近月換了：仍用買進的合約
    assert st.tom_trades(days)[0]["ret"] == pytest.approx(np.log(106 / 101) - st.COST)


def test_checks_need_both_halves():
    x = np.r_[np.full(300, -0.001), np.full(300, 0.004)] + np.random.default_rng(1).standard_normal(600) * 0.002
    r = st.mean_check(x)
    assert r["t"] > st.Z and r["first"] < 0 and not r["pass"]
    assert st.mean_check(np.abs(x) + 0.001)["pass"]
    assert st.Z == pytest.approx(2.128, abs=1e-3)
    R = np.random.default_rng(2).standard_normal(400) * 0.01
    tom = (np.arange(400) % 5 == 0).astype(float)
    R2 = R + tom * np.r_[np.full(200, -0.01), np.full(200, 0.02)]
    t3 = st.tom_check(R2, tom)
    assert t3["t"] > st.Z and t3["first"] < 0 < t3["second"] and not t3["pass"]


# ── 合成世界 ──────────────────────────────────────────────────────

def test_planted_overnight_drift_passes_t1_t2_but_not_t3():
    r = st.run(world(seed=1, n=3000, mu_o=0.0008, mu_d=-0.0002, start=date(2010, 1, 4)))
    assert r["verdict"]["T1"] == "過關" and r["verdict"]["T2"] == "過關" and r["verdict"]["T3"] == "沒過", (r["T1"], r["T2"], r["T3"])
    assert r["describe"]["night_split"]["n"] > 0 and r["describe"]["pts"]["O"] > 0


def test_planted_turn_of_month_passes_t3():
    r = st.run(world(seed=2, n=3000, tom=0.003))
    assert r["verdict"]["T3"] == "過關" and r["verdict"]["T1"] == "沒過", r["T3"]
    assert r["describe"]["tom_days"]["R_tom"] > r["describe"]["tom_days"]["R_other"] and r["describe"]["tom_trade"]["n"] > 100


def test_null_world_false_positive_rate_is_small():
    fp = 0
    sims = 100
    for s in range(sims):
        r = st.run(world(seed=100 + s, n=1200))
        fp += any(r[k]["pass"] for k in ("T1", "T2", "T3"))
    assert fp / sims <= 0.06, fp
    zero_cost = st.run(world(seed=5, n=2000, mu_o=st.COST))                          # 過夜剛好賺到成本：T2 不該過
    assert not zero_cost["T2"]["pass"]


def test_tests_use_the_registered_definitions():
    days = world(seed=6, n=800, mu_o=0.0003)
    r, b = st.run(days), st.build(days)
    assert r["T1"]["t"] == pytest.approx(st.mean_test(b["O"] - b["D"], 10)["t"]) and r["T1"]["mean"] == pytest.approx(np.mean(b["O"] - b["D"]))
    assert r["T2"]["mean"] == pytest.approx(np.mean(b["O"]) - 4 / 47000) and r["T2"]["t"] == pytest.approx(st.mean_test(b["O"] - 4 / 47000, 10)["t"])
    assert r["T3"]["t"] == pytest.approx(st.slope_test(b["R"], b["tom"], 10)["t"])
    assert st.mean_test(b["O"] - b["D"], 10)["t"] != pytest.approx(st.mean_test(b["O"] - b["D"], 0)["t"])


def test_report_and_too_few():
    text = st.format_report(st.run(world(seed=3, n=2500, mu_o=0.0005, start=date(2012, 1, 2))))
    assert "T1" in text and "月底月初" in text and "夜盤" in text and "nan" not in text and "星期" in text
    small = st.run(world(seed=4, n=10))
    assert small["verdict"] == {"T1": "沒有樣本", "T2": "沒有樣本", "T3": "沒有樣本"} and "樣本不夠" in st.format_report(small)


def test_not_imported_by_trading_modules():
    for d in ("strategies", "api", "core"):
        for p in (ROOT / d).rglob("*.py"):
            if p.name in ("drift_timing_study.py", "night_reversal_study.py"):                  # night_reversal_study＝另一個離線研究，沿用同一份資料定義（發現 73）
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            names = [x.name for n in ast.walk(tree) if isinstance(n, ast.Import) for x in n.names]
            names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            names += [x.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for x in n.names]
            assert "drift_timing_study" not in " ".join(names), p
