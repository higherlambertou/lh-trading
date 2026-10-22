"""夜盤振幅（發現 67）：讀檔（日盤與夜盤各自取成交量最大的月合約）、日期對齊（前一個交易日、前 20 個夜盤、偷看用的隔天 IV）、
判定規則、植入有資訊的世界要過關、無關的世界不過、偷看金絲雀、不接進交易路徑。"""
from __future__ import annotations

import ast
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from core import night_range_study as st

ROOT = Path(st.__file__).resolve().parent.parent
HEADER = "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,價差對單式委託成交量"


def row(d, code, h, l, vol, sess):
    return f"{d},TX,{code}  ,100,{h},{l},101,1,1%,{vol},101,10,100,101,200,50,,{sess},"


def test_load_tx_picks_the_most_active_contract_per_session(tmp_path):
    lines = [HEADER,
             row("2026/10/01", "202610", 110, 90, 5000, "一般"), row("2026/10/01", "202611", 130, 80, 100, "一般"),
             row("2026/10/01", "202610", 105, 95, 100, "盤後"), row("2026/10/01", "202611", 140, 60, 900, "盤後"),        # 夜盤：11 月成交量大
             row("2026/10/01", "202610/202611", 3, 1, 9999, "盤後"), row("2026/10/01", "202610W1", 500, 1, 99999, "盤後")]
    (tmp_path / "tx_2026_10.csv").write_bytes(("\n".join(lines) + "\n").encode("cp950"))
    day, night = st.load_tx(tmp_path)
    assert day == {"2026-10-01": (110.0, 90.0)} and night == {"2026-10-01": (140.0, 60.0)}


def bdays(start: date, n: int) -> list[str]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def test_build_alignment():
    ds = bdays(date(2017, 5, 1), 30)
    day = {d: (1000.0 + 2 * i, 1000.0) for i, d in enumerate(ds)}                # 日盤高低差 2i
    night = {d: (500.0 + i + 1, 500.0) for i, d in enumerate(ds)}               # 夜盤高低差 i+1
    iv = {d: {"iv": 10.0 + i, "F": 1000.0} for i, d in enumerate(ds)}
    b = st.build(day, night, iv, first=ds[22])
    k = list(b["dates"]).index(ds[25])
    assert b["prev_day"][k] == ds[24]
    assert b["y"][k] == pytest.approx(np.log(26))
    assert b["lnR"][k] == pytest.approx(np.log(np.mean([i + 1 for i in range(5, 25)])))     # 前 20 個夜盤（不含今晚）
    assert b["x_iv"][k] == pytest.approx(np.log((10 + 24) / 100 * 1000))                       # 前一個交易日的 IV
    assert b["ln_day"][k] == pytest.approx(np.log(2 * 24))
    assert b["x_iv_after"][k] == pytest.approx(np.log((10 + 25) / 100 * 1000))                 # 夜盤之後那天（偷看）
    gap = dict(day)
    del gap[ds[24]]                                                                             # 前一個交易日沒有日盤資料 → 往前找最近的
    b2 = st.build(gap, night, iv, first=ds[22])
    assert b2["prev_day"][list(b2["dates"]).index(ds[25])] == ds[23]


def test_passes_rule():
    good = {"explore": {"dm": 2.0, "reduction": 0.03}, "holdout": {"dm": 1.7, "reduction": 0.03}, "coef": 0.1}
    assert st.passes(good)
    for k, v in (("explore", {"dm": 1.95, "reduction": 0.03}), ("holdout", {"dm": 1.6, "reduction": 0.03}), ("explore", {"dm": 3.0, "reduction": 0.019}),
                 ("holdout", {"dm": 3.0, "reduction": 0.01}), ("coef", -0.1), ("holdout", {"dm": None, "reduction": None})):
        assert not st.passes({**good, k: v}), (k, v)
    assert not st.passes(None)


def world(seed=0, n=2450, kind="null"):
    """每個交易日 t：先有夜盤 t（盤後列日期＝t），再有日盤 t。kind：
    t1＝前一天收盤的 IV 已經知道今晚的衝擊（日盤與夜盤無關）；t2＝夜盤延續前一個日盤的波動（IV 無關）；
    concurrent＝IV 反映『當天』（夜盤之後）的波動，含這個夜盤（偷看用）；null＝全部無關。"""
    rng = np.random.default_rng(seed)
    ds = bdays(date(2017, 3, 1), n)
    a = rng.normal(0, 0.4, n)                                                   # 夜盤的新衝擊
    b = rng.normal(0, 0.4, n)                                                   # 日盤的
    day, night, iv = {}, {}, {}
    lv_day_prev = 0.0
    for t, d in enumerate(ds):
        lv_n = (lv_day_prev + 0.15 * a[t]) if kind == "t2" else a[t]
        lv_d = b[t] + (0.5 * lv_n if kind == "concurrent" else 0.0)
        nr = 100 * np.exp(lv_n + rng.normal(0, 0.1))
        dr = 200 * np.exp(lv_d + rng.normal(0, 0.1))
        night[d] = (10000 + nr / 2, 10000 - nr / 2)
        day[d] = (10000 + dr / 2, 10000 - dr / 2)
        if kind == "t1":
            sig = a[t + 1] if t + 1 < n else 0.0                                # 今天收盤的 IV 知道今晚（標記為 t+1 的夜盤）的衝擊
        elif kind == "concurrent":
            sig = lv_n                                                          # 當天的 IV 含這天之前那個夜盤
        else:
            sig = rng.normal(0, 0.4)
        iv[d] = {"iv": float(20 * np.exp(sig + rng.normal(0, 0.05))), "F": 10000.0}
        lv_day_prev = lv_d
    return day, night, iv


def test_planted_iv_passes_t1_only():
    r = st.run(*world(seed=1, kind="t1"))
    assert r["verdict"] == {"T1": "過關", "T2": "沒過"}, {k: (v["explore"], v["holdout"]) for k, v in r["tests"].items()}
    assert r["describe"]["display_model"]["terms"] == ["const", "lnR", "x_iv"]


def test_planted_day_range_passes_t2_only():
    r = st.run(*world(seed=2, kind="t2"))
    assert r["verdict"] == {"T1": "沒過", "T2": "過關"}, {k: (v["explore"], v["holdout"]) for k, v in r["tests"].items()}
    dm = r["describe"]["display_model"]
    assert dm["terms"] == ["const", "lnR", "ln_day"] and dm["q16"] < dm["q50"] < dm["q84"]


@pytest.mark.parametrize("seed", [3, 4, 5, 6])
def test_unrelated_inputs_never_pass(seed):
    r = st.run(*world(seed=seed, kind="null"))
    assert r["verdict"] == {"T1": "沒過", "T2": "沒過"}
    assert r["describe"]["display_model"]["terms"] == ["const", "lnR"]


def test_canary_after_iv_beats_prev_iv_when_it_contains_the_night():
    r = st.run(*world(seed=7, kind="concurrent"))
    c = r["canary"]
    assert c["after_reduction"] > c["prev_reduction"] and c["suspect"] is False


def test_report_and_too_few():
    r = st.run(*world(seed=8, kind="t1"))
    text = st.format_report(r)
    assert "T1 前一個交易日的 IV" in text and "金絲雀" in text and "nan" not in text
    r2 = st.run(*world(seed=9, n=200))
    assert r2["verdict"] == {"T1": "沒有樣本", "T2": "沒有樣本"} and "夜盤振幅" in st.format_report(r2)


def test_not_imported_by_trading_modules():
    for d in ("strategies", "api", "core"):
        for p in (ROOT / d).rglob("*.py"):
            if p.name in ("night_range_study.py", "night_range.py"):              # night_range＝只顯示的模組，借用同一份資料定義（發現 67）
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            names = [x.name for n in ast.walk(tree) if isinstance(n, ast.Import) for x in n.names]
            names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            names += [x.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for x in n.names]
            assert "night_range_study" not in " ".join(names), p
