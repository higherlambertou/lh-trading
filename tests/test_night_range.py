"""夜盤預期振幅的顯示（發現 67）：哪個夜盤、輸入不齊就寫原因、公式、最近 20 個夜盤只取今晚以前的、模型檔的讀取與快取、全期擬合；
下單前檢查的傍晚區塊（今晚夜盤＋下一個日盤）與日盤歷史的快取。不接進交易路徑。"""
from __future__ import annotations

import json
import math
from datetime import datetime

import numpy as np
import pytest

from core import night_range as nr
from core import precheck as pc
from core.daytrade import TAIPEI
from core.intraday_context import SESSION_MIN, Session


def tpe(*a):
    return datetime(*a, tzinfo=TAIPEI)


MODEL = {"beta": [-3.4, 0.2, 0.8, 0.15], "q16": -0.4, "q50": 0.0, "q84": 0.4, "n": 2000,
         "recent_nights": [[f"2026-10-{d:02d}", 500.0 + d] for d in range(1, 23)] + [["2026-10-23", 99999.0]]}


@pytest.mark.parametrize("when,expect", [
    (tpe(2026, 10, 22, 13, 44), None), (tpe(2026, 10, 22, 13, 45), "2026-10-22"), (tpe(2026, 10, 22, 23, 59), "2026-10-22"),
    (tpe(2026, 10, 23, 4, 59), "2026-10-22"), (tpe(2026, 10, 23, 5, 0), None), (tpe(2026, 10, 23, 10, 0), None),
    (tpe(2026, 10, 24, 1, 0), "2026-10-23"), (tpe(2026, 10, 24, 14, 0), None), (tpe(2026, 10, 26, 1, 0), None),      # 週六凌晨＝週五的夜盤；週六下午、週一凌晨沒有
])
def test_prev_trading_day(when, expect):
    assert nr.prev_trading_day(when) == expect


def test_for_tonight_formula_and_recent_window():
    r = nr.for_tonight(MODEL, "2026-10-22", 1120.0, 46272.0, ("2026-10-22", 30.31))
    recent = [500.0 + d for d in range(3, 23)]                                           # 10/03～10/22（不含 10/23，那是今晚之後的）
    mu = -3.4 + 0.2 * math.log(np.mean(recent)) + 0.8 * math.log(30.31 * nr.RATIO / 100 * 46272.0) + 0.15 * math.log(1120.0)
    assert r["r20n"] == pytest.approx(np.mean(recent), abs=0.05)
    assert r["expect"]["mid"] == pytest.approx(math.exp(mu), abs=0.05)
    assert r["expect"]["lo"] == pytest.approx(math.exp(mu - 0.4), abs=0.05) and r["expect"]["hi"] == pytest.approx(math.exp(mu + 0.4), abs=0.05)
    assert r["vix_date"] == "2026-10-22" and r["day_range"] == 1120.0 and nr.RATIO == 0.93


def test_for_tonight_explains_missing_inputs():
    assert "還沒算好" in nr.for_tonight(None, "2026-10-22", 1120.0, 46272.0, ("2026-10-22", 30.0))["note"]
    assert "還沒有 2026-10-22 的日盤資料" in nr.for_tonight(MODEL, "2026-10-22", None, 46272.0, ("2026-10-22", 30.0))["note"]
    assert "等 10-22 收盤的臺指選擇權波動率指數" in nr.for_tonight(MODEL, "2026-10-22", 1120.0, 46272.0, ("2026-10-21", 28.0))["note"]   # 只有昨天的：不湊
    assert "等 10-22" in nr.for_tonight(MODEL, "2026-10-22", 1120.0, 46272.0, None)["note"]
    short = {**MODEL, "recent_nights": MODEL["recent_nights"][:10]}
    assert "不完整" in nr.for_tonight(short, "2026-10-22", 1120.0, 46272.0, ("2026-10-22", 30.0))["note"]


def test_load_model_reads_validates_and_caches(tmp_path, monkeypatch):
    p = tmp_path / "m.json"
    monkeypatch.setattr(nr, "_cache", {"mtime": None, "model": None})
    assert nr.load_model(p) is None
    p.write_text(json.dumps(MODEL))
    assert nr.load_model(p)["n"] == 2000
    p.write_text(json.dumps({"beta": [1, 2]}))                                          # 壞掉的檔：不用
    import os
    os.utime(p, (1, 1))
    assert nr.load_model(p) is None


def test_fit_model_uses_the_study_definitions(monkeypatch):
    from core import night_range_study as st
    from tests.test_night_range_study import world
    day, night, iv = world(seed=2, kind="t2")
    monkeypatch.setattr(st, "load_tx", lambda d: (day, night))
    monkeypatch.setattr("core.vrp_study.load_iv", lambda d: iv)
    m = nr.fit_model()
    assert m["terms"] == ["const", "lnR", "x_iv", "ln_day"] and len(m["beta"]) == 4 and m["q16"] < m["q50"] < m["q84"]
    assert len(m["recent_nights"]) == nr.KEEP_RECENT and m["recent_nights"][-1][0] == sorted(night)[-1]
    assert m["beta"][3] > 0.3                                                            # 這個世界的夜盤延續前一個日盤


# ── 下單前檢查的傍晚區塊 ─────────────────────────────────────────

def sess(d, rng=1000.0, close=46000.0):
    hi = np.full(SESSION_MIN, close + rng / 2)
    lo = np.full(SESSION_MIN, close - rng / 2)
    c = np.full(SESSION_MIN, close)
    return Session(d, hi, lo, c, c, np.full(SESSION_MIN, 10.0))


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setattr(pc, "_sess", {"prev": None, "at": 0.0, "sessions": None})


def test_evening_block(fresh):
    calls = []
    loader = lambda st, before: calls.append(before) or [sess("2026-10-21"), sess("2026-10-22", 1120.0, 46272.0)]       # noqa: E731
    vix = lambda target: (("2026-10-22", 30.31), None)                                   # noqa: E731
    now = tpe(2026, 10, 22, 17, 0).timestamp()
    e = pc.evening(now, None, loader=loader, vix_fn=vix, model_fn=lambda: MODEL)
    assert e["prev_day"] == "2026-10-22" and e["night"]["expect"]["mid"] > 0 and calls == ["2026-10-23"]
    assert e["night"]["day_range"] == 1120.0
    assert "note" in e["next_day"] or "expect" in e["next_day"]                          # 下一個日盤：歷史太短時寫原因
    assert pc.evening(tpe(2026, 10, 22, 10, 0).timestamp(), None, loader=loader, vix_fn=vix, model_fn=lambda: MODEL) is None     # 日盤中沒有
    late = pc.evening(tpe(2026, 10, 23, 1, 0).timestamp(), None, loader=loader, vix_fn=vix, model_fn=lambda: MODEL)
    assert late["prev_day"] == "2026-10-22" and late["next_day"] is None                 # 過了午夜：下一個日盤由「今天」那一行顯示
    assert len(calls) == 1                                                               # 已經有 10/22 的日盤：整晚沿用，不重讀


def test_evening_waits_for_todays_vix_and_session(fresh):
    calls = []
    loader = lambda st, before: calls.append(before) or [sess("2026-10-21")]            # noqa: E731  今天的日盤還沒同步
    old_vix = lambda target: (("2026-10-21", 28.0), None)                               # noqa: E731
    t0 = tpe(2026, 10, 22, 14, 0).timestamp()
    e = pc.evening(t0, None, loader=loader, vix_fn=old_vix, model_fn=lambda: MODEL)
    assert "還沒有 2026-10-22 的日盤資料" in e["night"]["note"] and "還沒有 2026-10-22 的日盤資料" in e["next_day"]["note"]
    pc.evening(t0 + 60, None, loader=loader, vix_fn=old_vix, model_fn=lambda: MODEL)
    assert len(calls) == 1                                                               # 10 分鐘內不重讀
    loader2 = lambda st, before: calls.append(before) or [sess("2026-10-21"), sess("2026-10-22")]       # noqa: E731
    e2 = pc.evening(t0 + pc.SESSIONS_RETRY_SEC + 1, None, loader=loader2, vix_fn=old_vix, model_fn=lambda: MODEL)
    assert len(calls) == 2 and "等 10-22 收盤" in e2["night"]["note"] and "等 10-22 收盤" in e2["next_day"]["note"]


def test_fetch_evening_never_raises(fresh, monkeypatch):
    monkeypatch.setattr(pc, "evening", lambda now, store: (_ for _ in ()).throw(RuntimeError("x")))
    assert pc.fetch_evening(0.0, None) == {"error": "RuntimeError"}
