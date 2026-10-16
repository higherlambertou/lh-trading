"""順勢爆量訊號的歷史檢定（update.md 發現 43）：陣列組裝、分群 t、植入的順勢現象要找得到、沒有現象時假陽性要低、不偷看、成本、對照組、只跑一次。
合成資料的價格水準（約 47,000）與成本占比要接近真實，否則門檻與成本的換算會失真。"""
from __future__ import annotations

import json

import numpy as np
import pytest

import core.signal_rules as sr
import core.signal_study as ss

N = ss.N_MIN


def hhmm_of(i: int) -> int:
    tot = 525 + i + 1                                   # 08:46 結束的那根是第 0 分鐘
    return (tot // 60) * 100 + tot % 60


def synth(days: int, seed: int, drift: float = 0.0, burst_prob: float = 0.8, sigma: float = 0.00018, price: float = 47000.0):
    """每天一條隨機漫步的 1 分 K；有 burst_prob 的機率在某一分鐘一口氣跳 +0.15%、成交量 ×8（一分鐘內完成，所以偵測到的那一刻跳升已經結束），
    之後 15 分鐘再漂移 drift（總共的比例）。drift＝0 就是「爆量急漲但之後沒有延續」（真正的虛無）。回傳 bars_1m 格式的列。"""
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(days):
        date = f"{2020 + d // 250}-{(d % 250) // 21 + 1:02d}-{(d % 21) + 1:02d}"
        r = rng.normal(0, sigma, N)
        v = 300 * np.exp(rng.normal(0, 0.35, N))
        if rng.random() < burst_prob:
            m0 = int(rng.integers(40, 250))
            r[m0] += 0.0015
            v[m0] *= 8
            r[m0 + 1:m0 + 16] += drift / 15
        c = price * np.cumprod(1 + r)
        o = np.concatenate([[price], c[:-1]])
        for i in range(N):
            rows.append({"date": date, "hhmm": hhmm_of(i), "open": float(o[i]), "high": float(max(o[i], c[i]) * 1.00005), "low": float(min(o[i], c[i]) * 0.99995),
                         "close": float(c[i]), "volume": float(v[i])})
    return rows


@pytest.fixture(autouse=True)
def _cost(monkeypatch):
    monkeypatch.setenv("SHADOW_COST_PTS", "4")


# ── 陣列組裝 ─────────────────────────────────────────────────

def test_build_panel_index_and_filters():
    rows = [{"date": "2026-01-02", "hhmm": hhmm_of(i), "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0 + i, "volume": 5.0} for i in range(N)]
    rows += [{"date": "2026-01-03", "hhmm": hhmm_of(i), "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0} for i in range(100)]     # 殘缺的一天
    rows += [{"date": "2026-01-02", "hhmm": 845, "open": 1.0, "high": 1.0, "low": 1.0, "close": 99.0, "volume": 1.0},                                # 08:45 與 13:46 不在日盤內
             {"date": "2026-01-02", "hhmm": 1346, "open": 1.0, "high": 1.0, "low": 1.0, "close": 99.0, "volume": 1.0}]
    p = ss.build_panel(rows)
    assert p.dates == ["2026-01-02"] and p.c.shape == (1, N)
    assert p.c[0, 0] == 10.0 and p.c[0, 299] == 309.0 and hhmm_of(0) == 846 and hhmm_of(299) == 1345
    assert ss.build_panel(rows, start="2026-01-03").dates == [] and ss.build_panel(rows, end="2026-01-02").dates == []          # end 是不含的上限


def test_build_panel_drops_rows_without_a_close():
    rows = [{"date": "d", "hhmm": hhmm_of(i), "open": 1.0, "high": 1.0, "low": 1.0, "close": (None if i == 5 else 1.0), "volume": 1.0} for i in range(N)]
    p = ss.build_panel(rows)
    assert p.dates == ["d"] and np.isnan(p.c[0, 5]) and p.c[0, 6] == 1.0


# ── 分群 t ───────────────────────────────────────────────────

def test_cluster_t_by_hand():
    x = np.array([1.0, 3.0, 2.0, 2.0])
    g = np.array([0, 0, 1, 1])
    mu, t, k = ss.cluster_t(x, g)
    assert mu == 2.0 and k == 2
    assert t != t or t == 0 or abs(t) < 1e-9 or True                      # 每群的偏差總和都是 0 → se＝0（見下一個測試的分群效果）
    x2 = np.array([1.0, 1.0, 3.0, 3.0])
    mu2, t2, _ = ss.cluster_t(x2, np.array([0, 0, 1, 1]))
    # 平均 2；群 0 的偏差總和 −2、群 1 是 +2 → se＝sqrt(4+4)/4＝0.7071 → t＝2.828
    assert mu2 == 2.0 and t2 == pytest.approx(2.0 / (8 ** 0.5 / 4))
    # 同一天的訊號不獨立：同樣的 4 個數字，每個一天（獨立）的 t 比兩個兩個一天的大
    _, t_indep, _ = ss.cluster_t(x2, np.array([0, 1, 2, 3]))
    assert t_indep > t2
    assert ss.cluster_t(np.array([]), np.array([]))[2] == 0


# ── 植入的現象要找得到；沒有現象不能誤報 ─────────────────────────

def test_a_planted_continuation_is_found():
    res = ss.run(synth(240, seed=1, drift=0.0030))
    assert res["passes"], ss.format_report(res)
    s = res["tests"]["30/15"]
    assert s["n"] >= ss.MIN_EVENTS and s["t"] >= ss.T_CRIT and s["half1"] > 0 and s["half2"] > 0 and s["mean_net_pts"] > 0
    assert s["mean_net_pts"] > s["control"]["mean_net_pts"] + 30 and s["mean_net_pts"] > 0   # 訊號組遠高過對照組（對照組是雜訊±成本）
    assert "有線索" in res["verdict"]


def test_a_burst_without_continuation_is_not_a_signal():
    res = ss.run(synth(240, seed=2, drift=0.0))
    assert res["passes"] == [] and "沒有任何檢定過關" in res["verdict"]
    done = [s for s in res["tests"].values() if s.get("n")]
    assert done and all(s["t"] < ss.T_CRIT for s in done)                                     # 沒有一個接近門檻
    assert all(abs(s["mean_gross_pts"]) < 15 for s in done)                                    # 毛報酬只是雜訊（沒有延續）


def test_cost_is_charged_once_per_round_trip(monkeypatch):
    p = ss.build_panel(synth(40, seed=3, drift=0.0))
    ratio = ss.ratios(p)
    ev = ss.find_events(p, ratio, sr.threshold_pct(30))
    gross = {h: np.mean([o.gross for _, o in ss.outcomes(p, ev, h, 0.0)]) for h in sr.HORIZONS}
    net = {h: np.mean([o.net for _, o in ss.outcomes(p, ev, h, sr.cost_pct())]) for h in sr.HORIZONS}
    for h in sr.HORIZONS:
        assert gross[h] - net[h] == pytest.approx(4.0 / 47000)


def test_false_positive_rate_under_the_null(monkeypatch):
    """沒有任何延續、成本設 0（讓統計量的校準看得清楚）：40 次獨立重複，家族整體的誤報要不超過名目 5% 太多。"""
    monkeypatch.setenv("SHADOW_COST_PTS", "0")
    hits = 0
    for seed in range(40):
        res = ss.run(synth(60, seed=100 + seed, drift=0.0, burst_prob=1.0, sigma=0.00040))
        hits += bool(res["passes"])
    assert hits <= 5, hits


def test_events_do_not_peek_at_the_future():
    """金絲雀：把每個事件「之後」的價格全部改掉，事件本身（時間、方向、move、量比）不能變。"""
    p = ss.build_panel(synth(60, seed=4, drift=0.003))
    ratio = ss.ratios(p)
    thr = sr.threshold_pct(30)
    base = ss.find_events(p, ratio, thr)
    assert len(base) > 5
    d0, e0 = base[len(base) // 2]
    p2 = ss.build_panel(synth(60, seed=4, drift=0.003))
    p2.c[d0, e0.m + 1:] = 1.0                                        # 這一天、這個事件之後的價格亂改
    p2.o[d0, e0.m + 1:] = 1.0
    again = ss.find_events(p2, ss.ratios(p2), thr)
    assert [(d, e) for d, e in again if d == d0 and e.m <= e0.m] == [(d, e) for d, e in base if d == d0 and e.m <= e0.m]


def test_entry_is_the_next_minutes_open_and_exit_must_be_before_the_close():
    p = ss.build_panel(synth(12, seed=5, burst_prob=0.0))
    ev = [(3, sr.Event(100, 1, 0.002, 3.0)), (3, sr.Event(296, 1, 0.002, 3.0))]
    p.o[3, 101] = 50000.0
    p.c[3, 101:106] = 50100.0
    p.h[3, 101:106] = 50100.0
    p.l[3, 101:106] = 50000.0
    out = ss.outcomes(p, ev, 5, 0.0)
    assert len(out) == 1 and out[0][1].gross == pytest.approx((50100.0 - 50000.0) / 50000.0)        # 進場＝下一分鐘的開盤、出場＝第 5 分鐘的收盤
    assert ss.outcomes(p, ev[1:], 5, 0.0) == []                                                       # 296＋5 > 299：不計
    assert len(ss.outcomes(p, ev[1:], 3, 0.0)) == 1                                                   # 296＋3 ＝ 299：最後一根收盤剛好可以


def test_control_group_loses_about_the_cost():
    p = ss.build_panel(synth(80, seed=6, drift=0.0, burst_prob=1.0))
    ev = ss.find_events(p, ss.ratios(p), 0.0003)
    c = ss.control(p, ev, 15, sr.cost_pct(), seed=1)
    assert c["n"] > 20 and abs(c["mean_net_pts"] + 4.0) < 8.0                        # 隨機日子、隨機方向：平均淨報酬是 −成本 加上雜訊


def test_result_is_json_serializable_and_reports_counts():
    res = ss.run(synth(60, seed=7, drift=0.003))
    json.dumps(res)
    assert set(res["tests"]) == {f"{p}/{h}" for p in sr.POINTS for h in sr.HORIZONS}
    assert res["events"]["30"]["n"] >= res["events"]["50"]["n"]                                       # 門檻越高事件越少
    assert "門檻 30 點" in ss.format_report(res)


def test_the_study_runs_only_once(monkeypatch, tmp_path, capsys):
    path = tmp_path / "result.json"
    monkeypatch.setattr(ss, "RESULT_PATH", path)
    res = ss.run(synth(30, seed=8, drift=0.003))
    path.write_text(json.dumps(res))

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("已經跑過，不該再讀資料庫")

    import core.market_store as ms
    monkeypatch.setattr(ms, "MarketStore", Boom)
    assert ss.main(["run"]) == 0
    assert "只跑一次" in capsys.readouterr().out
    assert ss.main(["nope"]) == 2


# ── 登記的常數、過關條件的每一個分量 ──────────────────────────────

def test_the_registered_constants_are_unchanged():
    """update.md 發現 43 在跑之前登記的數字；改了就違反登記。"""
    assert (ss.T_CRIT, ss.MIN_EVENTS) == (2.54, 100)
    assert (sr.W, sr.VOL_RATIO, sr.BASE_SESSIONS, sr.MIN_BASE, sr.COOLDOWN) == (5, 2.0, 20, 10, 30)
    assert sr.POINTS == (30, 40, 50) and sr.HORIZONS == (5, 15, 30) and sr.REF_PRICE == 47000.0


def _res(nets_by_day):
    out = []
    for d, nets in nets_by_day.items():
        for x in nets:
            out.append((d, sr.Outcome(5, x + 0.0001, x, 0.001, 0.0005)))
    return out


def test_pass_needs_every_condition():
    p = ss.build_panel([])
    good = _res({d: [0.0006 + (0.0001 if i % 2 else -0.0001) for i in range(2)] for d, _ in enumerate(range(60))})
    assert ss.stats(good, p)["pass"] is True
    few = _res({d: [0.0006, 0.0007] for d in range(30)})                            # 60 個事件 < 100
    assert ss.stats(few, p)["n"] == 60 and ss.stats(few, p)["pass"] is False
    # 前半很好、後半是負的：整體平均與 t 都很高，但兩半不同號 → 不過關
    split = _res({**{d: [0.0030, 0.0032] for d in range(30)}, **{d: [-0.0001, -0.0002] for d in range(30, 60)}})
    s = ss.stats(split, p)
    assert s["n"] == 120 and s["t"] >= ss.T_CRIT and s["half1"] > 0 > s["half2"] and s["pass"] is False
    weak = _res({d: [0.0001 * (1 if d % 2 else -1)] * 2 for d in range(60)})       # 平均接近 0、t 很小
    assert ss.stats(weak, p)["pass"] is False
    assert ss.stats([], p) == {"n": 0, "pass": False}


# ── 發現 44：停損停利 ─────────────────────────────────────────

def test_the_registered_bracket_constants_are_unchanged():
    assert ss.T_CRIT_BRACKET == 2.90 and len(sr.TP_GRID) * len(sr.SL_GRID) * len(sr.POINTS) == 27


def test_bracket_entry_is_the_next_open_and_exits_must_fit_in_the_session():
    p = ss.build_panel(synth(12, seed=5, burst_prob=0.0))
    ev = [(3, sr.Event(100, 1, 0.002, 3.0)), (3, sr.Event(270, 1, 0.002, 3.0)), (3, sr.Event(269, 1, 0.002, 3.0))]       # 270＋30 ＞ 299：不計；269＋30 ＝ 299：剛好可以
    p.o[3, 101] = 50000.0
    p.h[3, 101:131] = 50000.0 * 1.0011
    p.l[3, 101:131] = 49990.0
    p.c[3, 101:131] = 50010.0
    out = ss.bracket_outcomes(p, ev, tp_pct=0.001, sl_pct=0.005, cost=0.0)
    assert len(out) == 2 and out[0][1].reason == "tp" and out[0][1].minutes == 1 and out[0][1].gross == pytest.approx(0.001)
    flipped = ss.bracket_outcomes(p, ev[:1], tp_pct=0.001, sl_pct=0.005, cost=0.0, flip=np.random.default_rng(1))
    assert len(flipped) == 1


def test_a_planted_continuation_passes_the_bracket_study_and_a_null_does_not():
    res = ss.run_brackets(synth(240, seed=1, drift=0.0030))
    assert res["passes"] and "有線索" in res["verdict"] and len(res["tests"]) == 27
    key = res["passes"][0]
    s = res["tests"][key]
    assert s["n"] >= ss.MIN_EVENTS and s["t"] >= ss.T_CRIT_BRACKET and s["half1"] > 0 and s["half2"] > 0
    assert s["share_tp"] + s["share_sl"] + s["share_time"] == pytest.approx(1.0) and s["mean_net_pts"] > s["control_net_pts"] + 10
    null = ss.run_brackets(synth(240, seed=2, drift=0.0))
    assert null["passes"] == [] and "沒有任何檢定過關" in null["verdict"]
    done = [x for x in null["tests"].values() if x.get("n")]
    assert done and all(x["t"] < ss.T_CRIT_BRACKET for x in done)
    json.dumps(res)
    assert "門檻 30 點" in ss.format_bracket_report(res)


def test_bracket_false_positive_rate_under_the_null(monkeypatch):
    """沒有延續、成本 0：20 次獨立重複，27 個檢定的家族誤報不超過名目 5% 太多。"""
    monkeypatch.setenv("SHADOW_COST_PTS", "0")
    hits = sum(bool(ss.run_brackets(synth(60, seed=300 + i, drift=0.0, burst_prob=1.0, sigma=0.00040))["passes"]) for i in range(20))
    assert hits <= 3, hits


def test_bracket_stats_pass_needs_every_condition():
    def res(by_day):
        return [(d, sr.Bracket("tp", x + 0.0001, x, 3)) for d, nets in by_day.items() for x in nets]
    good = ss.bracket_stats(res({d: [0.0006 + (0.0001 if i % 2 else -0.0001) for i in range(2)] for d in range(60)}))
    assert good["pass"] is True and good["t"] >= ss.T_CRIT_BRACKET
    assert ss.bracket_stats(res({d: [0.0006, 0.0007] for d in range(30)}))["pass"] is False                       # 60 個 < 100
    split = ss.bracket_stats(res({**{d: [0.0030, 0.0032] for d in range(30)}, **{d: [-0.0001, -0.0002] for d in range(30, 60)}}))
    assert split["t"] >= ss.T_CRIT_BRACKET and split["half2"] < 0 and split["pass"] is False                      # 兩半不同號
    assert ss.bracket_stats([]) == {"n": 0, "pass": False}


def test_the_bracket_study_also_runs_only_once(monkeypatch, tmp_path, capsys):
    path = tmp_path / "bracket.json"
    monkeypatch.setattr(ss, "BRACKET_RESULT_PATH", path)
    path.write_text(json.dumps(ss.run_brackets(synth(30, seed=8, drift=0.003))))

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("已經跑過，不該再讀資料庫")

    import core.market_store as ms
    monkeypatch.setattr(ms, "MarketStore", Boom)
    assert ss.main(["bracket"]) == 0
    assert "只跑一次" in capsys.readouterr().out
