"""依預期振幅定口數（發現 61）：尾部比的定義、區塊重抽、植入可預測的波動要過關、IV 無關時 B 不過、波動不可預測時都不過、偷看金絲雀、不接進交易路徑。"""
from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pytest

from core import vol_sizing_study as st
from tests.test_iv_range_study import world

ROOT = Path(st.__file__).resolve().parent.parent


def test_tail_ratio_is_scale_free_and_flat_is_one():
    x = np.random.default_rng(0).lognormal(0, 0.5, 2000)
    assert st.tail_ratio(x) == pytest.approx(st.tail_ratio(7 * x))
    assert st.tail_ratio(np.full(100, 3.0)) == pytest.approx(1.0)
    assert st.tail_ratio(x) == pytest.approx(np.percentile(x, 99) / x.mean())


def test_block_ci_brackets_point_and_detects_a_flatter_series():
    rng = np.random.default_rng(1)
    b = rng.lognormal(0, 0.6, 800)
    a = b ** 0.3                                                  # 同一組日子、更平
    p, lo, hi = st.block_ci(a, b, np.random.default_rng(2), n_boot=300)
    assert lo <= p <= hi and hi < 0
    p2, lo2, hi2 = st.block_ci(b, b, np.random.default_rng(2), n_boot=50)
    assert p2 == pytest.approx(0) and lo2 == pytest.approx(0) and hi2 == pytest.approx(0)


def test_exposure_uses_lots_times_actual_range():
    s, iv = world(n=400, seed=3)
    b = st.build(s, iv)
    ex = st.exposures(b["dates"], b["range"], b["lnR"], b["x_prev"])
    ok = np.isfinite(b["range"]) & np.isfinite(b["lnR"]) & np.isfinite(b["x_prev"])
    actual = np.exp(b["range"][ok][st.WARM:])
    assert np.allclose(ex["fixed"], actual)
    assert np.allclose(ex["r20"], actual / np.exp(b["lnR"][ok][st.WARM:]))
    assert np.median(ex["lots_iv"]) == pytest.approx(1.0)


def iid_world(n=1500, seed=0):
    """每天波動獨立（沒有可預測的部分），IV 也無關。"""
    s, iv = world(n=n, seed=seed, kind="null")
    rng = np.random.default_rng(seed + 100)
    out = []
    for x in s:
        k = float(np.exp(rng.normal(0, 0.5)))
        m = x.op[0]
        out.append(type(x)(x.date, m + (x.hi - m) * k, m + (x.lo - m) * k, m + (x.op - m) * k, m + (x.cl - m) * k, x.vol))
    return out, iv


def test_planted_predictable_vol_passes_both():
    r = st.run(*world(n=1500, seed=4, kind="leading"), n_boot=400)
    assert r["verdict"] == {"A": "過關", "B": "過關"}            # （這個世界的前一天 IV 知道隔天的衝擊，比同一天還多資訊，所以不看金絲雀）


@pytest.mark.parametrize("seed", [5, 6, 7])
def test_unrelated_iv_never_passes_b(seed):
    r = st.run(*world(n=1500, seed=seed, kind="null"), n_boot=300)
    assert r["verdict"]["B"] == "沒過"


@pytest.mark.parametrize("seed", [8, 9])
def test_unpredictable_vol_passes_nothing(seed):
    r = st.run(*iid_world(n=1500, seed=seed), n_boot=300)
    assert r["verdict"] == {"A": "沒過", "B": "沒過"}


def test_same_day_canary_is_more_stable_than_prev_day():
    r = st.run(*world(n=1500, seed=10, kind="concurrent"), n_boot=100)
    assert r["canary"]["same_day_tail"] < r["canary"]["prev_tail"]


def test_passes_requires_both_segments_and_min_drop():
    ok = {"change": -0.2, "lo": -0.3, "hi": -0.1}
    r = {"tests": {"A": {"explore": ok, "holdout": ok}}}
    assert st.passes(r, "A")
    r["tests"]["A"]["holdout"] = {"change": -0.05, "lo": -0.1, "hi": -0.01}       # 下降不到 10%
    assert not st.passes(r, "A")
    r["tests"]["A"]["holdout"] = {"change": -0.2, "lo": -0.3, "hi": 0.01}          # 區間跨 0
    assert not st.passes(r, "A")
    r["tests"]["A"]["holdout"] = None
    assert not st.passes(r, "A")


def test_not_imported_by_trading_modules():
    for d in ("strategies", "api", "core"):
        for p in (ROOT / d).rglob("*.py"):
            if p.name == "vol_sizing_study.py":
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
            names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            names += [a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names]
            assert "vol_sizing_study" not in " ".join(names), p
