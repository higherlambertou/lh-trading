"""夜盤振幅／成交值／漲跌家數 → 日盤振幅（發現 64）：讀檔（合約選法、夜盤同合約、價差與週合約不算）、證交所取最早一版、日期對齊（嚴格早於、過期不用）、
判定規則、植入有資訊的世界要過關、無關的世界不過、偷看金絲雀、不接進交易路徑。"""
from __future__ import annotations

import ast
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from core import range_predictors_study as st

ROOT = Path(st.__file__).resolve().parent.parent
HEADER = "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,價差對單式委託成交量"


def row(d, code, h, l, vol, sess):
    return f"{d},TX,{code}  ,100,{h},{l},101,1,1%,{vol},101,10,100,101,200,50,,{sess},"


# ── 讀檔 ─────────────────────────────────────────────────────────

def test_load_tx_picks_the_most_active_month_and_its_own_night(tmp_path):
    lines = [HEADER,
             row("2026/10/01", "202610", 110, 90, 5000, "一般"), row("2026/10/01", "202611", 130, 80, 100, "一般"),
             row("2026/10/01", "202610", 105, 95, 900, "盤後"), row("2026/10/01", "202611", 140, 60, 50, "盤後"),
             row("2026/10/01", "202610/202611", 3, 1, 9999, "一般"),          # 價差：不算
             row("2026/10/01", "202610W1", 500, 1, 99999, "一般"),            # 週合約：不算
             row("2026/10/02", "202610", 120, 100, 10, "一般"), row("2026/10/02", "202611", 125, 99, 20, "一般"),
             row("2026/10/02", "202610", 115, 101, 30, "盤後"),               # 成交量最大的是 11 月，11 月沒有夜盤 → 缺
             "2026/10/05,TX,202610  ,-,-,-,-,-,-,0,-,-,-,-,-,-,,一般,"]       # 沒成交：不算
    (tmp_path / "tx_2026_10.csv").write_bytes(("\n".join(lines) + "\n").encode("cp950"))
    tx = st.load_tx(tmp_path)
    assert list(tx) == ["2026-10-01", "2026-10-02"]
    assert tx["2026-10-01"] == st.TxDay("202610", 110.0, 90.0, 105.0, 95.0)
    assert tx["2026-10-02"] == st.TxDay("202611", 125.0, 99.0, None, None)


def test_load_twse_takes_the_earliest_revision(tmp_path):
    p = tmp_path / "intel.db"
    with sqlite3.connect(p) as c:
        c.execute("CREATE TABLE observations(source TEXT, series TEXT, event_date TEXT, value REAL, revision INTEGER)")
        c.executemany("INSERT INTO observations VALUES (?,?,?,?,?)", [
            ("twse", "market.turnover_twd", "2026-10-20", 798.0, 0), ("twse", "market.turnover_twd", "2026-10-20", 803.0, 1),   # 隔天修正：不用
            ("twse", "breadth.stock.up", "2026-10-20", 269.0, 0), ("twse", "breadth.stock.down", "2026-10-20", 700.0, 0),
            ("twse", "market.turnover_twd", "2026-10-21", 862.0, 0), ("twse", "taiex.close", "2026-10-21", 1.0, 0),
            ("other", "market.turnover_twd", "2026-10-21", 1.0, 0), ("twse", "breadth.stock.up", "2026-10-21", None, 0),
        ])
    tw = st.load_twse(p)
    assert tw == {"2026-10-20": {"turnover": 798.0, "up": 269.0, "down": 700.0}, "2026-10-21": {"turnover": 862.0}}


# ── 日期對齊 ─────────────────────────────────────────────────────

def bdays(start: date, n: int) -> list[str]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def test_build_alignment():
    days = bdays(date(2017, 4, 3), 40)
    tx = {d: st.TxDay("201705", 100.0 + i, 100.0 - i, 50.0 + i, 50.0) for i, d in enumerate(days)}
    tx[days[-1]] = st.TxDay("201705", 200.0, 100.0, None, None)                # 最後一天沒有夜盤
    iv = {d: {"iv": 10.0 + i, "F": 1000.0} for i, d in enumerate(days)}
    tw = {d: {"turnover": float(np.exp(i)), "up": 600.0, "down": 200.0 + i} for i, d in enumerate(days)}
    del tw[days[30]]                                                            # 證交所少一天
    b = st.build(tx, iv, tw, first=days[25])
    idx = {d: k for k, d in enumerate(b["dates"])}
    assert b["dates"][0] == days[25]
    k = idx[days[26]]
    assert b["y"][k] == pytest.approx(np.log(2 * 26))
    assert b["lnR"][k] == pytest.approx(np.log(np.mean([2 * i for i in range(6, 26)])))    # 前 20 天、不含當天
    assert b["ln_prev"][k] == pytest.approx(np.log(2 * 25))
    assert b["x_iv"][k] == pytest.approx(np.log((10 + 25) / 100 * 1000))                    # 嚴格早於當天
    assert b["N"][k] == pytest.approx(np.log(26))                                            # 當天標記的盤後＝開盤前那個夜盤
    assert b["U"][k] == pytest.approx(25 - np.mean(range(5, 25)))                           # 前一天 ln 成交值 − 再之前 20 天的平均
    assert b["B"][k] == pytest.approx((600 - 225) / (600 + 225))
    assert b["U_same"][k] == pytest.approx(26 - np.mean(range(6, 26)))
    assert b["B_same"][k] == pytest.approx((600 - 226) / (600 + 226))
    k31 = idx[days[31]]                                                         # 前一天（days[30]）證交所缺 → 用再前一天（仍在 4 天內）
    assert b["B"][k31] == pytest.approx((600 - 229) / (600 + 229))
    assert np.isnan(b["U_same"][idx[days[30]]])
    assert np.isnan(b["N"][idx[days[-1]]])


def test_stale_inputs_are_not_used():
    days = bdays(date(2017, 4, 3), 30)
    gap = date.fromisoformat(days[-1]) + timedelta(days=10)
    days2 = days + [gap.isoformat()]
    tx = {d: st.TxDay("201705", 110.0, 100.0, 105.0, 100.0) for d in days2}
    iv = {d: {"iv": 20.0, "F": 1000.0} for d in days}
    tw = {d: {"turnover": 1.0, "up": 1.0, "down": 2.0} for d in days}
    b = st.build(tx, iv, tw, first=days[-1])
    assert np.isfinite(b["x_iv"][0]) and np.isfinite(b["B"][0])
    assert np.isnan(b["x_iv"][-1]) and np.isnan(b["U"][-1]) and np.isnan(b["B"][-1])      # 前一筆在 10 天前：過期不用


# ── 判定規則 ─────────────────────────────────────────────────────

def test_passes_rule():
    good = {"explore": {"dm": 2.2, "reduction": 0.03}, "holdout": {"dm": 1.7, "reduction": 0.03}, "coef": 0.1}
    assert st.passes(good)
    for k, v in (("explore", {"dm": 2.1, "reduction": 0.03}), ("holdout", {"dm": 1.6, "reduction": 0.03}), ("explore", {"dm": 3.0, "reduction": 0.019}),
                 ("holdout", {"dm": 3.0, "reduction": 0.019}), ("coef", -0.1), ("holdout", {"dm": None, "reduction": None})):
        assert not st.passes({**good, k: v}), (k, v)
    assert not st.passes(None)
    assert st.Z_EXPLORE == pytest.approx(2.128, abs=1e-3)


# ── 植入已知現象／無關／偷看 ─────────────────────────────────────

def world(seed=0, n=2450, has=(), concurrent=False):
    """lv_t＝0.9·lv_{t−1}＋a_t；日盤振幅＝exp(lv_t＋雜訊)；IV_t 反映 lv_t（收盤後）。has 裡的項目帶有 a_t（開盤前就知道的新衝擊）；
    concurrent＝成交值與漲跌家數反映『當天』的 lv（偷看用）。"""
    rng = np.random.default_rng(seed)
    days = bdays(date(2017, 3, 1), n)
    a = rng.normal(0, 0.3, n)
    lv = np.zeros(n)
    for t in range(1, n):
        lv[t] = 0.9 * lv[t - 1] + a[t]
    day_r = 200 * np.exp(lv + rng.normal(0, 0.25, n))
    tx, iv, tw = {}, {}, {}
    for t, d in enumerate(days):
        night = 0.9 * lv[t - 1] + a[t] if "N" in has and t > 0 else rng.normal(0, 0.5)
        nr = 100 * np.exp(night + rng.normal(0, 0.15))
        tx[d] = st.TxDay("201712", 10000 + day_r[t] / 2, 10000 - day_r[t] / 2, 10000 + nr / 2, 10000 - nr / 2)
        iv[d] = {"iv": float(20 * np.exp(lv[t] + rng.normal(0, 0.05))), "F": 10000.0}
        nxt = a[t + 1] if t + 1 < n else 0.0                                     # 證交所第 t 天的數字被拿來預測第 t+1 天
        u_sig = lv[t] if concurrent else (nxt if "U" in has else 0.0)
        b_sig = lv[t] if concurrent else (nxt if "B" in has else 0.0)
        q = 0.5 + 0.49 / (1 + np.exp(-3 * (b_sig + rng.normal(0, 0.15 if ("B" in has or concurrent) else 1.0))))    # 一面倒程度＝2q−1，隨訊號單調變大
        tw[d] = {"turnover": float(1e11 * np.exp(u_sig + rng.normal(0, 0.15 if ("U" in has or concurrent) else 1.0))), "up": float(1000 * q), "down": float(1000 * (1 - q))}
    return tx, iv, tw


@pytest.mark.parametrize("k", ["N", "U", "B"])
def test_planted_information_passes_only_its_own_test(k):
    r = st.run(*world(seed=1, has=(k,)))
    assert r["verdict"][k] == "過關", r["tests"][k]
    assert all(r["verdict"][o] == "沒過" for o in st.TESTS if o != k)


@pytest.mark.parametrize("seed", [2, 3, 4, 5])
def test_unrelated_inputs_never_pass(seed):
    r = st.run(*world(seed=seed))
    assert all(v == "沒過" for v in r["verdict"].values()), {k: (v["explore"]["dm"], v["holdout"]["dm"]) for k, v in r["tests"].items()}


def test_same_day_canary():
    r = st.run(*world(seed=6, concurrent=True))
    for t in ("U", "B"):
        c = r["canary"][t]
        assert c["same_day_reduction"] > c["prev_reduction"] and c["suspect"] is False


def test_report_renders():
    r = st.run(*world(seed=7, has=("N",)))
    text = st.format_report(r)
    assert "夜盤振幅" in text and "金絲雀" in text and "NaN" not in text


def test_too_few_days():
    tx, iv, tw = world(seed=8, n=200)
    r = st.run(tx, iv, tw)
    assert r["verdict"] == {t: "沒有樣本" for t in st.TESTS}
    assert "夜盤振幅" in st.format_report(r)


def test_not_imported_by_trading_modules():
    for d in ("strategies", "api", "core"):
        for p in (ROOT / d).rglob("*.py"):
            if p.name in ("range_predictors_study.py", "night_range_study.py"):     # night_range_study＝另一個離線研究，借用同一個巢狀模型比較（發現 67）
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
            names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            names += [a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names]
            assert "range_predictors_study" not in " ".join(names), p


def test_description_survives_a_limit_locked_day():
    """整天鎖漲跌停＝高低差 0：那天沒有 y、隔天沒有 ln(前一天振幅)；檢定照跑，描述用有值的日子，不能變成 nan 或被跳過。"""
    tx, iv, tw = world(seed=9, has=("N",))
    d = sorted(tx)[1500]
    t = tx[d]
    tx[d] = st.TxDay(t.contract, t.h, t.h, t.nh, t.nl)
    r = st.run(tx, iv, tw)
    pr = r["describe"]["prev_range_in_base"]
    assert pr["n"] == r["n_joint"] - 1
    assert all(np.isfinite(c[k]) for c in r["describe"]["corr"].values() for k in ("lnR", "x_iv", "ln_prev"))
    assert "nan" not in st.format_report(r)
