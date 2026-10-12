"""core/backtest.py：回測工具。

重點不是「策略賺不賺」，而是工具本身的算法對不對——這些數字會被拿來決定能不能上線：
成本有沒有扣、停損停利誰先、收盤有沒有平倉、樣本內外有沒有切對、滾動式驗證有沒有偷看未來、
打亂資料是不是真的沒有規律、參數名稱打錯會不會被靜默忽略。全部用手算得出答案的合成資料。"""
from __future__ import annotations

import json
import logging
import random
from datetime import date, datetime, timedelta

import numpy as np
import pytest

import core.backtest as bt
from core.backtest import (Combo, Result, build_strategy, expand_grid, load_days, load_grid_file, main, preset_grid,
                           run_grid, split_index, summarize, surrogate_days, t_stat, walk_forward, window_stats)
from core.market_store import MarketStore


def mk(date, ohlc, vol=100, start="08:45"):
    t0 = datetime.strptime(f"{date} {start}", "%Y-%m-%d %H:%M").timestamp()
    return [(t0 + 60 * i, float(o), float(h), float(l), float(c), vol) for i, (o, h, l, c) in enumerate(ohlc)]


def flat(prices):
    return [(p, p, p, p) for p in prices]


RANGE = [(105, 110, 100, 105)] * 5             # orb：前 5 分鐘的開盤區間 100~110
ORB = dict(strategy="orb", params={"or_minutes": 5, "buffer_pts": 0})


def orb_day(*after, sl=10, tp=0):
    """區間 100~110，第 6 根收 120 突破 → 做多 @120，之後的 K 棒由呼叫端給。"""
    combo = Combo("orb", {"or_minutes": 5, "buffer_pts": 0}, {"stop_loss_pts": sl, "take_profit_pts": tp})
    bars = mk("2026-01-05", RANGE + [(115, 121, 114, 120)] + list(after))
    return combo, {"2026-01-05": bars}


def run1(combo, days, cost=4.0):
    return run_grid([combo], days, cost, workers=1)[0]


# ── 引擎：手算得出答案的情境 ─────────────────────────────────────────

def test_a_stop_out_loses_exactly_the_stop_distance_plus_cost():
    combo, days = orb_day((118, 119, 108, 112), *flat([112, 112, 112]))
    r = run1(combo, days)
    assert (r.n[0], r.net[0], r.wins[0]) == (1, -10.0 - 4.0, 0)             # 做多 @120、停損 110
    assert run1(combo, days, cost=0).net[0] == -10.0


def test_take_profit_fills_at_the_level():
    combo, days = orb_day((121, 140, 119, 130), *flat([130, 130]), sl=10, tp=15)
    r = run1(combo, days)
    assert (r.n[0], r.net[0], r.wins[0]) == (1, 15.0 - 4.0, 1)              # 停利價 135，不是這根 K 棒的高點 140


def test_when_both_levels_are_touched_in_one_bar_the_stop_comes_first():
    combo, days = orb_day((120, 140, 100, 110), *flat([110, 110]), sl=10, tp=15)
    assert run1(combo, days, cost=0).net[0] == -10.0


def test_an_open_position_is_closed_at_the_last_close():
    combo, days = orb_day(*flat([125, 130]), sl=0)                          # 沒有停損停利，也沒有反向訊號
    r = run1(combo, days, cost=0)
    assert (r.n[0], r.net[0]) == (1, 10.0)                                  # 120 → 130


def test_a_quiet_day_has_no_trades():
    combo = Combo("orb", {"or_minutes": 5, "buffer_pts": 0}, {"stop_loss_pts": 10})
    r = run1(combo, {"2026-01-05": mk("2026-01-05", RANGE + flat([105, 104, 106]))})
    assert (r.n[0], r.net[0]) == (0, 0.0)


def test_the_minute_version_of_a_tick_strategy_is_driven_by_bar_closes():
    """ma_cross（快 1、慢 3）：收盤 10,10,10,12,14,12,10,8,6 → 空@10、反手多@12（-2）、反手空@12（0）、收盤平 @6（+6）。"""
    combo = Combo("ma_cross", {"short_period": 1, "long_period": 3}, {"stop_loss_pts": 0, "take_profit_pts": 0})
    days = {"2026-01-05": mk("2026-01-05", flat([10, 10, 10, 12, 14, 12, 10, 8, 6]))}
    r = run1(combo, days)
    assert (r.n[0], r.net[0], r.wins[0]) == (3, (-2 + 0 + 6) - 3 * 4.0, 1)
    assert run1(combo, days, cost=0).net[0] == 4.0


def test_each_day_starts_fresh():
    combo, d1 = orb_day((118, 119, 108, 112), *flat([112, 112]))
    bars2 = mk("2026-01-06", RANGE + [(115, 121, 114, 120)] + [(118, 119, 108, 112)] + flat([112, 112]))
    r = run1(combo, {**d1, "2026-01-06": bars2})
    assert list(r.n) == [1, 1] and list(r.net) == [-14.0, -14.0]            # 第二天不會帶著第一天的區間、旗標或部位


def late_day(breakout_at, *after, after_at="13:40", sl=10):
    """區間照常在 08:45 形成；突破發生在 breakout_at，之後的 K 棒從 after_at 開始。"""
    combo = Combo("orb", {"or_minutes": 5, "buffer_pts": 0}, {"stop_loss_pts": sl})
    bars = mk("2026-01-05", RANGE) + mk("2026-01-05", [(115, 121, 114, 120)], start=breakout_at)
    if after:
        bars += mk("2026-01-05", list(after), start=after_at)
    return combo, {"2026-01-05": bars}


def test_the_real_risk_checks_still_apply_to_entries():
    """回測走真的 place_order：orb 預設可開倉到 13:30。13:20 的突破會進場（對照組），13:40 的突破不會。"""
    combo, days = late_day("13:20", (120, 121, 119, 120))
    assert run1(combo, days).n[0] == 1
    combo, days = late_day("13:40", (120, 121, 119, 120))
    assert run1(combo, days).n[0] == 0


def test_a_stop_still_works_after_the_trade_window_closed():
    """update.md 發現 24 的回歸：過了可開倉時段（13:30），停損照樣出得去。"""
    combo, days = late_day("13:20", (118, 119, 100, 105), (105, 105, 105, 105))
    r = run1(combo, days, cost=0)
    assert (r.n[0], r.net[0]) == (1, -10.0)                    # 13:20 進場 @120，13:40 碰到停損價 110


def test_patches_are_undone_after_a_run():
    import strategies.base as base
    from core.broker import broker
    prev = logging.root.manager.disable
    logging.disable(logging.NOTSET)                          # 先設成已知狀態（前面的測試若沒還原，不能讓這個測試跟著誤判）
    try:
        before = (broker.place_order, base.BaseStrategy._margin_ok)
        combo, days = orb_day((118, 119, 108, 112))
        run1(combo, days)
        assert (broker.place_order, base.BaseStrategy._margin_ok) == before
        assert logging.root.manager.disable == logging.NOTSET            # 回測時關掉的 logging 要還原
        logging.disable(logging.WARNING)                                  # 原本就有人關過的，要還原成他設的，不是 NOTSET
        run1(combo, days)
        assert logging.root.manager.disable == logging.WARNING
    finally:
        logging.disable(prev)


def test_parallel_and_serial_give_the_same_numbers():
    rng = random.Random(3)
    days = {f"2026-01-{d:02d}": mk(f"2026-01-{d:02d}", flat([100 + sum(rng.choice((-2, -1, 1, 2)) for _ in range(i)) for i in range(60)]))
            for d in range(5, 9)}
    combos = expand_grid("ma_cross", [{"short_period": s, "long_period": l} for s, l in ((2, 5), (3, 8), (5, 12), (4, 20))], {"stop_loss_pts": [0]})
    a = run_grid(combos, days, 4.0, workers=1)
    b = run_grid(combos, days, 4.0, workers=2)
    for x, y in zip(a, b):
        assert x.combo.label == y.combo.label and np.array_equal(x.net, y.net) and np.array_equal(x.n, y.n)


def test_no_data_is_an_error_not_an_empty_report():
    with pytest.raises(ValueError):
        run_grid([Combo("orb")], {}, 4.0)


# ── 參數組 ────────────────────────────────────────────────────────

def test_expand_grid_takes_the_product_of_axes_and_accepts_explicit_alternatives():
    assert len(expand_grid("orb", {"or_minutes": [15, 30], "buffer_pts": [5, 20, 40]}, {"stop_loss_pts": [50, 100]})) == 12
    alts = expand_grid("ma_cross", [{"short_period": 3, "long_period": 10}, {"short_period": 5, "long_period": 20}], {"stop_loss_pts": [0, 100]})
    assert len(alts) == 4 and alts[0].params == {"short_period": 3, "long_period": 10}
    assert len(expand_grid("orb", {"or_minutes": 15})) == 1                      # 純量當一個值


def test_a_misspelt_parameter_is_rejected_instead_of_silently_ignored():
    with pytest.raises(ValueError, match="lookbak"):
        expand_grid("breakout", {"lookbak": [10]})
    with pytest.raises(ValueError, match="no_such_attr"):
        expand_grid("breakout", {"lookback": [10]}, {"no_such_attr": [1]})
    with pytest.raises(ValueError, match="scalp"):
        expand_grid("scalp")


def test_every_parameter_actually_reaches_the_strategy():
    s = build_strategy(Combo("orb", {"or_minutes": 30, "buffer_pts": 7}, {"stop_loss_pts": 66, "atr_trail_mult": 2.5, "take_profit_pts": 0}))
    assert (s.or_minutes, s.buffer_pts, s.stop_loss_pts, s.atr_trail_mult) == (30, 7, 66, 2.5)
    s = build_strategy(Combo("ma_cross", {"short_period": 2, "long_period": 9, "stop_loss_pts": 7}))      # 基本參數寫在 params 也要生效
    assert (s.short_period, s.long_period, s.stop_loss_pts) == (2, 9, 7)
    s = build_strategy(Combo("vwap_revert", {"dev_pts": 90, "exit_ratio": 0.5}, {"atr_trail_mult": 3.0, "atr_period": 20}))
    assert (s.dev_pts, s.exit_ratio, s.atr_trail_mult, s.atr_period) == (90, 0.5, 3.0, 20)


def test_presets_exist_for_every_strategy_with_a_sane_size():
    sizes = {n: len(preset_grid(n)) for n in bt.STRATEGIES}
    assert sizes == {"ma_cross": 12, "breakout": 10, "rsi": 12, "bollinger": 12, "momentum": 24, "orb": 36, "vwap_revert": 48}


@pytest.mark.parametrize("name", bt.STRATEGIES)
def test_every_strategy_runs_on_a_small_day_without_errors(name):
    rng = random.Random(1)
    px, ohlc = 100.0, []
    for _ in range(120):
        o = px
        px += rng.choice((-3, -1, 1, 3))
        ohlc.append((o, max(o, px) + 1, min(o, px) - 1, px))
    r = run_grid(preset_grid(name)[:3], {"2026-01-05": mk("2026-01-05", ohlc)}, 4.0)
    assert len(r) == 3 and all(len(x.n) == 1 for x in r)


def test_grid_file_formats(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"strategy": "orb", "params": {"or_minutes": [15, 30]}, "base": {"stop_loss_pts": [50, 100]}}), encoding="utf-8")
    assert len(load_grid_file(str(p))) == 4
    p.write_text(json.dumps([{"strategy": "orb", "params": {"or_minutes": [15]}},
                             {"strategy": "rsi", "params": [{"period": 14, "oversold": 30, "overbought": 70}]}]), encoding="utf-8")
    assert [c.strategy for c in load_grid_file(str(p))] == ["orb", "rsi"]
    p.write_text(json.dumps({"params": {}}), encoding="utf-8")
    with pytest.raises(KeyError):
        load_grid_file(str(p))


# ── 資料 ──────────────────────────────────────────────────────────

def test_bars_1m_hhmm_is_the_end_of_the_bar(tmp_path):
    ms = MarketStore(tmp_path / "ms.db")
    ms.upsert_bars_1m("TXF", [
        {"date": "2026-01-05", "hhmm": 846, "open": 100, "high": 102, "low": 99, "close": 101, "volume": 10},
        {"date": "2026-01-05", "hhmm": 1345, "open": 101, "high": 103, "low": 100, "close": 102, "volume": 20},
        {"date": "2026-01-06", "hhmm": 846, "open": 102, "high": 104, "low": 101, "close": 103, "volume": 30}])
    days = load_days(str(tmp_path / "ms.db"))
    assert list(days) == ["2026-01-05", "2026-01-06"]
    first, last = days["2026-01-05"]
    assert datetime.fromtimestamp(first[0]) == datetime(2026, 1, 5, 8, 45)      # 08:46 結束的那根，起點是 08:45
    assert datetime.fromtimestamp(last[0]) == datetime(2026, 1, 5, 13, 44)
    assert first[1:] == (100.0, 102.0, 99.0, 101.0, 10)
    assert list(load_days(str(tmp_path / "ms.db"), start="2026-01-06")) == ["2026-01-06"]
    assert list(load_days(str(tmp_path / "ms.db"), end="2026-01-05")) == ["2026-01-05"]
    with pytest.raises(ValueError, match="只有日盤"):
        load_days(str(tmp_path / "ms.db"), session="night")
    with pytest.raises(ValueError):
        load_days(str(tmp_path / "ms.db"), source="nope")


def test_flow_1m_is_split_into_day_and_night_sessions(tmp_path):
    ms = MarketStore(tmp_path / "ms.db")

    def row(dt, price):
        return {"ts": int(dt.timestamp()), "buy_n": 1, "sell_n": 1, "unk_n": 0, "buy_vol": 2, "sell_vol": 3, "unk_vol": 1,
                "open": price, "high": price + 1, "low": price - 1, "close": price}

    rows = [row(datetime(2026, 1, 5, 8, 45 + i), 100 + i) for i in range(3)]                                  # 日盤
    rows += [row(datetime(2026, 1, 5, 15, i), 200 + i) for i in range(3)]                                      # 夜盤前段
    rows += [row(datetime(2026, 1, 5, 23, 58), 300), row(datetime(2026, 1, 5, 23, 59), 301),
             row(datetime(2026, 1, 6, 0, 0), 302), row(datetime(2026, 1, 6, 0, 1), 303)]                        # 相隔 > 30 分鐘的另一段，跨午夜
    ms.upsert_flow_1m("TMF", rows)
    db = str(tmp_path / "ms.db")
    everything = load_days(db, source="flow", session="all")
    assert list(everything) == ["2026-01-05D", "2026-01-05N", "2026-01-05N2"]                                  # 同一天切出兩段夜盤，key 不會互相蓋掉
    assert [len(everything[k]) for k in everything] == [3, 3, 4]                                               # 跨午夜的那段是連續一段
    assert list(load_days(db, source="flow", session="day")) == ["2026-01-05D"]
    assert list(load_days(db, source="flow", session="night")) == ["2026-01-05N", "2026-01-05N2"]
    assert everything["2026-01-05D"][0][5] == 6                                                                # 成交量 = 買＋賣＋不明
    assert list(load_days(db, source="flow", session="all", start="2026-01-06")) == []                          # 日期看的是時段開始那天
    assert list(load_days(db, source="flow", session="all", end="2026-01-05")) == list(everything)


# ── 打亂資料 ──────────────────────────────────────────────────────

def make_days(n=6, bars=40, seed=1):
    rng = random.Random(seed)
    days = {}
    for k in range(n):
        d = (date(2026, 1, 5) + timedelta(days=k)).isoformat()
        px, ohlc = 100.0 + k, []
        for _ in range(bars):
            o = px
            px += rng.choice((-3, -1, 1, 3, 5, -5))
            ohlc.append((o, max(o, px) + rng.choice((0, 1, 2)), min(o, px) - rng.choice((0, 1, 2)), px))
        days[d] = [(t, o, h, l, c, 10 + i) for i, (t, o, h, l, c, v) in enumerate(mk(d, ohlc))]
    return days


def test_surrogate_keeps_the_shape_of_the_data_but_destroys_the_order():
    days = make_days()
    sd = surrogate_days(days, 7)
    assert list(sd) == list(days)
    for d in days:
        a, b = days[d], sd[d]
        assert len(a) == len(b) and [x[0] for x in a] == [x[0] for x in b] and [x[5] for x in a] == [x[5] for x in b]   # 時間與成交量不動
        assert a[0][1] == b[0][1]                                                                                        # 開盤價不動
        assert all(h >= max(o, c) - 1e-9 and l <= min(o, c) + 1e-9 for _, o, h, l, c, _ in b)                            # K 棒仍然合理
        assert [x[4] for x in a] != [x[4] for x in b]                                                                    # 真的打亂了
        assert abs(np.std([x[4] - x[1] for x in a]) - np.std([x[4] - x[1] for x in b])) < 1e-6                           # 單根 K 棒的實體分布不變
    assert surrogate_days(days, 7) == sd and surrogate_days(days, 8) != sd                                              # 同種子可重現


# ── 統計 ──────────────────────────────────────────────────────────

def test_t_stat_and_friends():
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    assert t_stat(x) == pytest.approx(3.0 / (np.std(x, ddof=1) / np.sqrt(5)))
    assert t_stat(np.array([2.0, 2.0, 2.0])) == 0.0 and t_stat(np.array([1.0])) == 0.0
    assert [split_index(n, 0.6) for n in (2, 10, 100, 267)] == [1, 6, 60, 160]
    assert split_index(10, 0.0) == 1 and split_index(10, 1.0) == 9                     # 兩邊至少各一天
    assert bt.mde(100.0, 100) == pytest.approx(28.0)


def res(label, net, n=None):
    net = np.array(net, float)
    n = np.ones(len(net)) if n is None else np.array(n, float)
    return Result(Combo("orb", {}, {"stop_loss_pts": label}), [f"d{i}" for i in range(len(net))], n, net, (net > 0).astype(float))


def test_window_stats_and_summary_split_by_time():
    r = res(1, [10, 10, 10, 10, -20, -20])                    # 前 4 天賺、後 2 天賠
    rows = summarize([r], 4 / 6, cost=4.0)
    assert rows[0].is_["perday"] == 10.0 and rows[0].oos["perday"] == -20.0
    s = window_stats(r, 0, 6, cost=4.0)
    assert s["trades"] == 6 and s["perday"] == 0.0 and s["pertrade"] == 0.0                   # 40 - 40
    assert s["gross_pertrade"] == pytest.approx(4.0) and s["winrate"] == pytest.approx(4 / 6)   # 還原成本：每筆 +4


def test_walk_forward_picks_with_the_past_only():
    """A 在訓練期賺、在接著的測試期賠（B 永遠 0）。若偷看測試期就會挑 B（0）；只看過去會挑 A 然後賠。"""
    n = 60
    a = ([5.0] * 20 + [-5.0] * 10) * 2                        # 每 30 天：前 20 天 +5、後 10 天 -5
    b = [0.001 * ((i % 3) - 1) for i in range(n)]
    wf = walk_forward([res(1, a), res(2, b)], train=20, test=10, metric="mean")
    assert wf["windows"] == 4 and wf["days"] == 40
    assert wf["perday"] < 0 and wf["distinct_picks"] >= 1
    # 第一個視窗：訓練期是前 20 天（A 全是 +5）→ 挑 A；測試期 20~30 天 A 是 -5
    assert wf["picks"][0][1].endswith("stop_loss_pts=1")


def test_walk_forward_follows_the_better_combo_when_it_stays_better():
    n = 60
    good = [1.0 + 0.1 * np.sin(i) for i in range(n)]
    bad = [-1.0 + 0.1 * np.cos(i) for i in range(n)]
    wf = walk_forward([res(1, bad), res(2, good)], train=20, test=10)
    assert wf["perday"] == pytest.approx(np.mean(good[20:]), abs=1e-9) and wf["distinct_picks"] == 1 and wf["positive_windows"] == wf["windows"]


def test_walk_forward_rejects_impossible_windows():
    with pytest.raises(ValueError):
        walk_forward([res(1, [1.0] * 30)], train=25, test=10)
    with pytest.raises(ValueError):
        walk_forward([res(1, [1.0] * 30)], train=3, test=2)


def test_null_calibration_runs_the_same_pipeline_on_shuffled_data():
    days = make_days(n=10)
    combos = expand_grid("ma_cross", [{"short_period": 2, "long_period": 6}, {"short_period": 3, "long_period": 9}], {"stop_loss_pts": [0]})
    out = bt.null_calibration(combos, days, 4.0, 2, 0.6, walk=(5, 2), workers=1)
    assert len(out) == 2 and {"best_is_t", "oos_t_of_best", "best_all_t", "wf_t", "wf_perday"} <= set(out[0])
    again = bt.null_calibration(combos, days, 4.0, 2, 0.6, walk=(5, 2), workers=1)
    assert out == again                                                                  # 固定種子，結果可重現


# ── 命令列 ────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    ms = MarketStore(tmp_path / "ms.db")
    rows = []
    for d, bars in make_days(n=40, bars=60).items():
        for i, (ts, o, h, l, c, v) in enumerate(bars):
            end = datetime.fromtimestamp(ts + 60)
            rows.append({"date": d, "hhmm": end.hour * 100 + end.minute, "open": o, "high": h, "low": l, "close": c, "volume": v})
    ms.upsert_bars_1m("TXF", rows)
    return str(tmp_path / "ms.db")


def test_cli_prints_a_report_and_writes_json(db, tmp_path, capsys):
    out = tmp_path / "out.json"
    code = main(["--db", db, "--strategy", "ma_cross", "--workers", "1", "--top", "2", "--null", "1", "--walk-forward", "20,5", "--json", str(out)])
    text = capsys.readouterr().out
    assert code == 0 and "ma_cross：12 組參數，40 天" in text and "偵測力" in text and "滾動式驗證" in text and "沒有技巧的對照" in text
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data) == 1 and data[0]["strategy"] == "ma_cross" and len(data[0]["rows"]) == 12 and "walk_forward" in data[0] and len(data[0]["null"]) == 1


def test_cli_with_a_custom_grid_and_cost(db, tmp_path, capsys):
    g = tmp_path / "g.json"
    g.write_text(json.dumps({"strategy": "breakout", "params": {"lookback": [5, 10]}, "base": {"stop_loss_pts": [0]}}), encoding="utf-8")
    assert main(["--db", db, "--grid", str(g), "--cost", "0", "--workers", "1"]) == 0
    text = capsys.readouterr().out
    assert "breakout：2 組參數" in text and "每筆成本 0 點" in text


@pytest.mark.parametrize("argv,message", [
    (["--strategy", "nope"], "沒有內建網格"),
    (["--strategy", "scalp"], "沒有內建網格"),
    (["--walk-forward", "20"], "TRAIN,TEST"),
    (["--strategy", "ma_cross", "--walk-forward", "100,50"], "walk-forward 視窗不合理"),
    (["--grid", "/no/such/file.json"], "No such file"),
    (["--strategy", "ma_cross", "--start", "2030-01-01"], "沒有 K 棒"),
])
def test_cli_errors_exit_with_2_and_say_why(db, capsys, argv, message):
    assert main(["--db", db, "--workers", "1", *argv]) == 2
    assert message in capsys.readouterr().err


# ── 波動度標準化（--scaled）：點數型參數改成「近 N 日平均日內振幅 R 的倍數」──────────────────

def ranged_days(ranges, bars_per_day=8):
    """每天一個指定的日內振幅（高－低）：第一根 K 棒的高低點撐出振幅，其餘貼著收盤。"""
    days = {}
    for k, r in enumerate(ranges):
        d = (date(2026, 1, 5) + timedelta(days=k)).isoformat()
        ohlc = [(100, 100 + r, 100, 100)] + [(100, 100, 100, 100)] * (bars_per_day - 1)
        days[d] = mk(d, ohlc)
    return days


def sim_day(combo, bars, ref=None):
    import asyncio
    with bt.patched():
        return asyncio.run(bt.simulate_day(combo, bars, ref))


def test_reference_range_is_the_mean_of_the_previous_sessions_only():
    days = ranged_days([10, 20, 30, 40, 50])
    assert bt.ref_ranges(days, 3) == [None, None, None, 20.0, 30.0]            # 第 4 天＝前 3 天（10,20,30）的平均，不含自己
    # 沒有偷看未來／自己：把最後一天的振幅改得很大，它自己和前面所有天的參考振幅都不變
    changed = ranged_days([10, 20, 30, 40, 5000])
    assert bt.ref_ranges(changed, 3) == bt.ref_ranges(days, 3)
    # 前面的天改了，後面的才會跟著變
    assert bt.ref_ranges(ranged_days([10, 20, 31, 40, 50]), 3)[3] != 20.0


def test_scaled_points_rounds_keeps_zero_as_off_and_never_rounds_a_real_stop_to_zero():
    assert bt.scaled_points(0, 500) == 0
    assert bt.scaled_points(0.2, 500) == 100 and bt.scaled_points(0.161, 620) == 100
    assert bt.scaled_points(0.001, 100) == 1                                  # 0.1 點四捨五入成 0 就等於沒有停損，這比設定錯誤還糟


def test_build_strategy_resolves_multiples_for_attributes_and_strategy_params():
    c = Combo("orb", {"or_minutes": 15}, {"take_profit_pts": 0}, {"stop_loss_pts": 0.2, "buffer_pts": 0.01})
    s = build_strategy(c, 500.0)
    assert s.stop_loss_pts == 100 and s.params["buffer_pts"] == 5 and s.params["or_minutes"] == 15 and s.take_profit_pts == 0
    s2 = build_strategy(c, 250.0)
    assert s2.stop_loss_pts == 50 and s2.params["buffer_pts"] == 2           # 振幅小一半 → 停損與緩衝跟著小一半（2.5 四捨五入成 2）
    with pytest.raises(ValueError, match="warmup"):
        build_strategy(c, None)
    assert build_strategy(Combo("orb", {"or_minutes": 15}, {"stop_loss_pts": 80})).stop_loss_pts == 80      # 沒有倍數的組合完全不受影響


def test_a_scaled_stop_follows_the_reference_range_in_a_simulated_day():
    # 做多 @120；下一根最低 80、收 105（收在開盤區間 100~110 裡面，不會觸發 orb 的反向進場）
    bars = mk("2026-01-05", RANGE + [(115, 121, 114, 120)] + [(118, 119, 80, 105)] + flat([105, 105]))
    combo = Combo("orb", {"or_minutes": 5, "buffer_pts": 0}, {"take_profit_pts": 0}, {"stop_loss_pts": 0.1})
    assert sim_day(combo, bars, 100.0) == [-10.0]                             # R=100 → 停損 10 點（110）
    assert sim_day(combo, bars, 200.0) == [-20.0]                             # R=200 → 停損 20 點（100）
    assert sim_day(combo, bars, 600.0) == [-15.0]                             # R=600 → 停損 60 點（60）碰不到 → 收盤 105 平倉
    with pytest.raises(ValueError):
        sim_day(combo, bars, None)


def test_scaled_grids_have_the_same_shape_as_the_fixed_grids_and_agree_at_the_median():
    ref = 500.0
    for name in bt.STRATEGIES:
        fixed, scaled = preset_grid(name), preset_grid(name, scaled_ref=ref)
        assert len(fixed) == len(scaled)
        keys = bt.POINT_KEYS.get(name, bt.DEFAULT_POINT_KEYS)
        for f, c in zip(fixed, scaled):
            at_median = build_strategy(c, ref)
            baseline = build_strategy(f)
            for k in keys:
                fv = {**f.params, **f.base}.get(k)
                if fv is None:
                    continue
                got = getattr(at_median, k) if hasattr(at_median, k) and k in bt.BASE_KEYS else at_median.params[k]
                assert got == fv, (name, k, fv, got)                                     # 在 R 的中位數那一天，標準化版＝固定版
                assert (fv == 0) == (k not in c.scaled)                                  # 0（停用）維持 0，不會被換成倍數
            for k in set({**f.params, **f.base}) - set(keys):
                assert {**c.params, **c.base}[k] == {**f.params, **f.base}[k]          # 週期、比例、atr_trail_mult 不動
            assert at_median.params == baseline.params and at_median.stop_loss_pts == baseline.stop_loss_pts
    assert any(c.scaled for c in preset_grid("orb", scaled_ref=ref)) and not any(c.scaled for c in preset_grid("orb"))
    with pytest.raises(ValueError):
        preset_grid("orb", scaled_ref=0)


def test_warmup_drops_the_same_days_for_every_combo_and_leaves_fixed_results_unchanged():
    days = make_days(n=8, bars=30)
    fixed = Combo("ma_cross", {"short_period": 2, "long_period": 6}, {"stop_loss_pts": 20})
    full = run1(fixed, days)
    cut = run_grid([fixed], days, 4.0, workers=1, warmup=3)[0]
    assert cut.days == list(days)[3:] and len(cut.net) == 5
    assert list(cut.net) == list(full.net[3:]) and list(cut.n) == list(full.n[3:])      # 每天用全新的策略物件，所以固定版的成績只是少了前幾天
    for w in (-1, 8, 99):
        with pytest.raises(ValueError, match="warmup"):
            run_grid([fixed], days, 4.0, workers=1, warmup=w)


def test_a_scaled_combo_without_enough_history_is_refused_with_the_fix_in_the_message():
    days = ranged_days([100] * 6)
    c = Combo("breakout", {"lookback": 5}, {}, {"stop_loss_pts": 0.2})
    with pytest.raises(ValueError, match="--warmup 3"):
        bt._run_combo(c, days, 4.0, warmup=0, scale_days=3)
    r = bt._run_combo(c, days, 4.0, warmup=3, scale_days=3)                             # 夠了就能跑
    assert len(r.days) == 3


def test_each_day_is_resolved_with_the_range_of_the_days_before_it(monkeypatch):
    ranges = [100] * 20 + [200, 400, 800, 100000]
    days = ranged_days(ranges)
    seen = []
    real = bt.build_strategy
    monkeypatch.setattr(bt, "build_strategy", lambda combo, ref=None: seen.append(ref) or real(combo, ref))
    c = Combo("breakout", {"lookback": 5}, {}, {"stop_loss_pts": 0.5})
    with bt.patched():
        bt._run_combo(c, days, 4.0, warmup=20, scale_days=20)
    # 第 21~24 天：前 20 天的平均（每一天往前滑一格）；最後一天那個 100000 的大振幅不能影響它自己
    expect = [float(np.mean(ranges[i - 20:i])) for i in range(20, 24)]
    assert seen == expect and seen[-1] < 1000


def test_analyze_reports_the_trimmed_days_and_says_when_the_grid_is_scaled():
    days = make_days(n=12, bars=30)
    fixed = [Combo("ma_cross", {"short_period": 2, "long_period": 6}, {"stop_loss_pts": 20})]
    scaled = [Combo("ma_cross", {"short_period": 2, "long_period": 6}, {}, {"stop_loss_pts": 0.2})]
    a = bt.analyze("ma_cross", fixed, days, 4.0, 0.6, 0, None, 1, 3, 3)
    b = bt.analyze("ma_cross", scaled, days, 4.0, 0.6, 0, None, 1, 3, 3)
    keys = list(days)
    assert (a["days"], a["first"], a["warmup"], a["scaled"]) == (9, keys[3], 3, False)
    assert (b["days"], b["first"], b["warmup"], b["scaled"]) == (9, keys[3], 3, True)
    assert "前 3 天只用來算參考振幅" in bt.format_report(b) and "振幅倍數" in bt.format_report(b) and "振幅倍數" not in bt.format_report(a)
    assert "0.2R" in b["rows"][0]["label"]


@pytest.mark.parametrize("scaled,message", [
    ({"nope": 0.1}, "沒有可以用振幅倍數設定的參數"), ({"stop_loss_pts": -1}, "0 以上"), ({"stop_loss_pts": True}, "0 以上"),
    ({"stop_loss_pts": float("nan")}, "0 以上"), ({"stop_loss_pts": "0.1"}, "0 以上")])
def test_bad_scaled_parameters_are_refused(scaled, message):
    with pytest.raises(ValueError, match=message):
        bt.validate_combo(Combo("breakout", {}, {}, scaled))


def test_the_same_parameter_cannot_be_both_fixed_and_scaled():
    with pytest.raises(ValueError, match="只留一個"):
        bt.validate_combo(Combo("breakout", {}, {"stop_loss_pts": 100}, {"stop_loss_pts": 0.2}))


def test_a_grid_file_can_use_multiples(tmp_path):
    g = tmp_path / "g.json"
    g.write_text(json.dumps({"strategy": "orb", "params": {"or_minutes": [15]}, "base": {"take_profit_pts": [0]},
                             "scaled": {"stop_loss_pts": [0.1, 0.2], "buffer_pts": [0.01]}}), encoding="utf-8")
    combos = load_grid_file(str(g))
    assert [c.scaled for c in combos] == [{"stop_loss_pts": 0.1, "buffer_pts": 0.01}, {"stop_loss_pts": 0.2, "buffer_pts": 0.01}]


def test_cli_scaled_uses_a_warmup_prints_the_reference_and_runs_the_scaled_grid(db, capsys):
    assert main(["--db", db, "--strategy", "breakout", "--scaled", "--workers", "1", "--scale-days", "5"]) == 0
    text = capsys.readouterr().out
    assert "標準化：參考振幅 R" in text and "前 5 天不納入成績" in text
    assert "breakout（點數型參數＝振幅倍數）：10 組參數，35 天" in text         # 40 天 − 5 天 warmup
    assert main(["--db", db, "--strategy", "breakout", "--workers", "1", "--warmup", "5"]) == 0
    text = capsys.readouterr().out
    assert "振幅倍數" not in text and "breakout：10 組參數，35 天" in text           # 固定版用同樣的天數


def test_which_parameters_get_scaled_is_pinned_down_per_strategy():
    """哪些參數被當成「點數型」是這項檢驗的核心設計（寫進 update.md 發現 27），不能被悄悄改掉。"""
    expect = {"orb": {"buffer_pts", "stop_loss_pts"}, "vwap_revert": {"dev_pts", "stop_loss_pts"}}
    for name in bt.STRATEGIES:
        got = {k for c in preset_grid(name, scaled_ref=500.0) for k in c.scaled}
        assert got == expect.get(name, {"stop_loss_pts"}), name
    # 沒有被換成倍數的軸：週期、比例、atr_trail_mult（本來就是 ATR 的倍數）
    orb = preset_grid("orb", scaled_ref=500.0)
    assert {c.base["atr_trail_mult"] for c in orb} == {0.0, 3.0} and {c.params["or_minutes"] for c in orb} == {15, 30, 60}
    assert {c.scaled["stop_loss_pts"] for c in orb} == {0.1, 0.2, 0.4} and {c.scaled["buffer_pts"] for c in orb} == {0.01, 0.04}
    assert {c.params["exit_ratio"] for c in preset_grid("vwap_revert", scaled_ref=500.0)} == {0.25, 0.5}
    # 沒有停損的組合（stop_loss_pts=0）保持沒有停損
    ma = preset_grid("ma_cross", scaled_ref=500.0)
    assert sum(1 for c in ma if c.base.get("stop_loss_pts") == 0 and not c.scaled) == 6 and sum(1 for c in ma if c.scaled == {"stop_loss_pts": 0.2}) == 6
    assert all("stop_loss_pts" not in c.base for c in ma if c.scaled)                  # 換成倍數的那一軸，不會同時留著固定值


def test_a_scaled_attribute_that_is_not_a_strategy_parameter_is_set_on_the_object_not_silently_dropped():
    s = build_strategy(Combo("breakout", {}, {}, {"point_value": 0.01}), 500.0)           # 不是 BASE_KEYS、也不是策略參數的屬性
    assert s.point_value == 5


def test_parallel_workers_give_the_same_answer_as_serial_with_warmup_and_multiples():
    days = make_days(n=14, bars=30)
    combos = [Combo("breakout", {"lookback": lb}, {}, {"stop_loss_pts": m}) for lb in (5, 10) for m in (0.5, 1.0)]
    serial = run_grid(combos, days, 4.0, workers=1, warmup=5, scale_days=5)
    para = run_grid(combos, days, 4.0, workers=2, warmup=5, scale_days=5)
    assert [r.days for r in para] == [r.days for r in serial] and all(len(r.days) == 9 for r in para)
    assert all(np.array_equal(a.net, b.net) and np.array_equal(a.n, b.n) for a, b in zip(serial, para))


def test_the_no_skill_comparison_uses_the_same_warmup_and_multiples():
    days = make_days(n=14, bars=30)
    combos = [Combo("breakout", {"lookback": 5}, {}, {"stop_loss_pts": 0.5}), Combo("breakout", {"lookback": 10}, {}, {"stop_loss_pts": 0.5})]
    a = bt.analyze("breakout", combos, days, 4.0, 0.6, 2, (5, 2), 1, 5, 5)
    assert a["days"] == 9 and len(a["null"]) == 2 and "wf_t" in a["null"][0] and a["walk_forward"]["days"] == 4


def test_cli_custom_grid_with_multiples_gets_a_warmup_automatically(db, tmp_path, capsys):
    g = tmp_path / "g.json"
    g.write_text(json.dumps({"strategy": "breakout", "params": {"lookback": [5, 10]}, "scaled": {"stop_loss_pts": [0.3]}}), encoding="utf-8")
    assert main(["--db", db, "--grid", str(g), "--workers", "1", "--scale-days", "5"]) == 0
    text = capsys.readouterr().out
    assert "breakout（點數型參數＝振幅倍數）：2 組參數，35 天" in text and "前 5 天只用來算參考振幅" in text
