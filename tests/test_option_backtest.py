"""選擇權買方回測（core/option_backtest.py，update.md 發現 30 B）。

測什麼：
- 價格假設與成本：手算得出答案的小例子（買進用賣價、賣出用買價、不合理報價退回結算價 ±2%、手續費與稅）。
- 出場規則 X0~X3：手造的週期，每條規則的賣出日、賣出價都手算；「隔天才賣、不偷看觸發當天的價格」「順延」「到期日當天」。
  再用一份**獨立重寫的狀態機**（結構完全不同）對 400 個隨機週期逐條比對。
- 週期怎麼切：用 Black-76 合成的整條鏈（和期交所檔案同樣的欄位），進場日、履約價、買進價、到期損益都和測試自己手算的一致；
  訊號只用進場日前一個交易日以前的資料——把進場日之後的價格改掉，訊號一模一樣（金絲雀），改前一天則會變（對照組）。
- 統計：手算的 t 值與 MDE；植入已知現象要偵測得到（正向與負向）、沒有現象時假陽性率要低。
"""
from __future__ import annotations

import json
import math
import random
import statistics
from datetime import date, datetime, time, timedelta
from statistics import NormalDist

import pytest

import core.option_backtest as ob
import core.option_history as oh
from core.iv_monitor import black76, expiry_of
from core.option_backtest import Cycle, Exp, Leg, Q


@pytest.fixture(autouse=True)
def _fast_bootstrap(monkeypatch):
    monkeypatch.setattr(ob, "BOOT", 200)


def buy(px): return px + 0.4 + 0.001 * px                 # noqa: E704  買進：賣價 + 手續費 0.4 點 + 稅 0.1%
def sell(px): return px - 0.4 - 0.001 * px                # noqa: E704  賣出
def expire(v): return v * 0.999                           # noqa: E704  到期現金結算：只收稅


# ── 價格假設與成本 ────────────────────────────────────────────────

def test_mark_prefers_settle_then_close():
    assert ob.mark(Q(100.0, 90.0, None, None)) == 100.0
    assert ob.mark(Q(0.0, 90.0, None, None)) == 90.0                  # 到期日結算價整欄是 0
    assert ob.mark(Q(None, 90.0, None, None)) == 90.0
    assert ob.mark(Q(None, None, 5.0, 6.0)) is None and ob.mark(Q(0.0, 0.0, 1.0, 2.0)) is None and ob.mark(None) is None


def test_ask_is_used_only_when_reasonable():
    assert ob.ask_px(Q(100.0, None, 98.0, 103.0)) == 103.0
    assert ob.ask_px(Q(100.0, None, 85.0, 90.0)) == 90.0              # 邊界 0.9 倍含
    assert ob.ask_px(Q(100.0, None, 98.0, 125.0)) == 125.0            # 邊界 1.25 倍含
    for bad in (Q(100.0, None, 85.0, 89.9), Q(100.0, None, 98.0, 125.1), Q(100.0, None, 98.0, None), Q(100.0, None, 98.0, 0.0),
                Q(100.0, None, 110.0, 105.0)):                        # 太低、太高、沒有、0、賣價低於買價
        assert ob.ask_px(bad) == pytest.approx(102.0)                 # 結算價 × 1.02
    assert ob.ask_px(Q(100.0, None, None, 105.0)) == 105.0 and ob.ask_px(Q(100.0, None, 0.0, 105.0)) == 105.0   # 沒買價就不比
    assert ob.ask_px(Q(0.0, 80.0, 79.0, 81.0)) == 81.0                # 沒結算價就用收盤價當基準
    assert ob.ask_px(Q(None, None, 1.0, 2.0)) is None


def test_bid_is_used_only_when_reasonable():
    assert ob.bid_px(Q(100.0, None, 97.0, 103.0)) == 97.0
    assert ob.bid_px(Q(100.0, None, 75.0, 103.0)) == 75.0             # 邊界 0.75 倍含
    assert ob.bid_px(Q(100.0, None, 110.0, 120.0)) == 110.0           # 邊界 1.1 倍含
    for bad in (Q(100.0, None, 74.9, 103.0), Q(100.0, None, 110.1, 120.0), Q(100.0, None, None, 103.0), Q(100.0, None, 0.0, 103.0)):
        assert ob.bid_px(bad) == pytest.approx(98.0)                  # 結算價 × 0.98
    assert ob.bid_px(Q(None, None, 1.0, 2.0)) is None


def test_costs_fee_and_tax():
    assert ob.buy_cost(100.0) == pytest.approx(100.5)                 # 100 + 0.4 + 0.1
    assert ob.sell_proceeds(100.0) == pytest.approx(99.5)
    assert ob.settle_proceeds(100.0) == pytest.approx(99.9)           # 到期結算沒有手續費、只收稅
    assert ob.settle_proceeds(0.0) == 0.0
    assert ob.intrinsic((10000.0, "C"), 10300.0) == 300.0 and ob.intrinsic((10000.0, "C"), 9900.0) == 0.0
    assert ob.intrinsic((10000.0, "P"), 9700.0) == 300.0 and ob.intrinsic((10000.0, "P"), 10100.0) == 0.0


def test_thresholds_are_the_preregistered_ones():
    assert ob.Z_CLAIM == pytest.approx(2.69, abs=0.001) and ob.N_TESTS == 7 and ob.Z_HINT == 2.0
    assert (ob.TP_MULT, ob.LEGOUT_RATIO, ob.TIME_STOP_DAYS, ob.SPIKE, ob.IV_LOW_PCT) == (1.5, 0.5, 7, 0.025, 20.0)
    assert (ob.PCT_WINDOW, ob.RV_DAYS, ob.SPIKE_DAYS, ob.STRANGLE_DELTA) == (250, 20, 5, 0.25)
    assert ob.ASK_OK == (0.9, 1.25) and ob.BID_OK == (0.75, 1.1) and ob.HAIRCUT == 0.02
    assert ob.FEE_PTS == pytest.approx(0.4) and ob.TAX == 0.001


# ── 出場規則（手造的週期）─────────────────────────────────────────

DAYS = [date(2020, 1, 2), date(2020, 1, 3), date(2020, 1, 6), date(2020, 1, 7), date(2020, 1, 8),
        date(2020, 1, 9), date(2020, 1, 10), date(2020, 1, 13), date(2020, 1, 14)]
EXPIRY = date(2020, 1, 15)                                            # 距到期 ≤ 7 個日曆天的第一個交易日是 1/8
K = 10000.0
CALL, PUT = (K, "C"), (K, "P")


def flat(settle=100.0, bid=98.0, ask=100.0):
    return Q(settle, None, bid, ask)


def cycle(final_F, daily=None, entry=None):
    """跨式 K=10000，進場兩腳賣價各 100 → 付出 2 × 100.5 = 201、權利金 200。daily：{日期: (買權 Q, 賣權 Q)}，Q 為 None＝那天那一腳沒報價；沒給的日子兩腳都是平的 100。"""
    daily = daily or {}
    call, put = Leg(entry or flat()), Leg(entry or flat())
    for d in DAYS:
        c, p = daily.get(d, (flat(), flat()))
        if c is not None:
            call.daily.append((d, c))
        if p is not None:
            put.daily.append((d, p))
    for leg in (call, put):
        leg.daily.append((EXPIRY, Q(0.0, 5.0, None, None)))            # 到期日那筆的結算價是 0：不能被當成價值
    return Cycle("main", "202001", EXPIRY, date(2019, 12, 18), K, {CALL: call, PUT: put}, (CALL, PUT), final_F=final_F)


def test_entry_cost_and_hold_to_expiry():
    r = ob.simulate(cycle(10300.0))
    assert r["outlay"] == pytest.approx(201.0) and r["premium"] == pytest.approx(200.0)
    assert r["X0"]["pnl"] == pytest.approx(expire(300) - 2 * buy(100))        # 98.7
    assert r["X0"]["roi"] == pytest.approx(98.7 / 201) and r["X0"]["exit"] == "到期"
    assert ob.simulate(cycle(10000.0))["X0"]["pnl"] == pytest.approx(-201.0)    # 平平：兩腳都歸零，最大虧損＝付出的全部
    assert ob.simulate(cycle(9600.0))["X0"]["pnl"] == pytest.approx(expire(400) - 201)   # 賺在賣權
    assert ob.simulate(cycle(10300.0))["free"] == pytest.approx(300 - 200)      # 無摩擦：結算價進場、不收稅


def test_x1_sells_on_first_day_within_7_calendar_days_at_that_days_bid():
    daily = {date(2020, 1, 7): (flat(100, 90, 100), flat(100, 90, 100)),         # 距到期 8 天：不是這天
             date(2020, 1, 8): (flat(100, 80, 100), flat(100, 80, 100)),         # 距到期 7 天：就是這天，買價 80
             date(2020, 1, 9): (flat(100, 70, 100), flat(100, 70, 100))}         # 70 < 0.75 × 100：就算被選到也會退回 98
    r = ob.simulate(cycle(10300.0, daily))
    assert r["X1"]["pnl"] == pytest.approx(2 * sell(80) - 201)
    assert r["X1"]["exit"] == "2020-01-08"
    assert r["X2"]["pnl"] == r["X3"]["pnl"] == r["X0"]["pnl"]                    # 沒觸發就是持有到期


def test_x3_sells_the_loser_the_day_after_and_holds_the_other():
    daily = {date(2020, 1, 3): (flat(), Q(40.0, None, 38.0, 42.0)),              # 賣權剩 40%（≤ 50%）→ 觸發
             date(2020, 1, 6): (flat(), Q(35.0, None, 33.0, 37.0))}              # 隔天收盤照買價 33 賣
    r = ob.simulate(cycle(10300.0, daily))
    assert r["X3"]["pnl"] == pytest.approx(sell(33) + expire(300) - 201)          # 131.267；用觸發當天的買價 38 就會是別的數字
    assert r["X3"]["exit"] == "2020-01-06"
    assert r["X2"]["pnl"] == pytest.approx(r["X0"]["pnl"])                       # 價值總和只有 140，沒到 300


def test_x3_does_not_peek_when_price_recovers_the_next_day():
    daily = {date(2020, 1, 3): (flat(), Q(40.0, None, 38.0, 42.0)),
             date(2020, 1, 6): (flat(), Q(80.0, None, 78.0, 82.0))}               # 隔天回到 80：賣的還是這個價
    r = ob.simulate(cycle(10300.0, daily))
    assert r["X3"]["pnl"] == pytest.approx(sell(78) + expire(300) - 201)


def test_x3_needs_two_legs_and_the_frictionless_baseline_uses_the_settle():
    c = cycle(10300.0, {date(2020, 1, 3): (Q(30.0, None, 29.0, 31.0), flat())})
    assert ob.simulate(c, [CALL])["X3"]["exit"] == "到期"                  # 單腳沒有「另一腳」可留，不平
    assert ob.simulate(c)["X3"]["exit"] != "到期"
    r = ob.simulate(cycle(10300.0, entry=Q(100.0, None, 98.0, 103.0)))
    assert r["premium"] == pytest.approx(206.0) and r["free"] == pytest.approx(300 - 200)      # 無摩擦＝結算價進場，不是賣價


def test_x3_exactly_at_the_ratio_triggers_and_just_above_does_not():
    at = ob.simulate(cycle(10300.0, {date(2020, 1, 3): (flat(), Q(50.0, None, 49.0, 51.0))}))
    assert at["X3"]["exit"] != "到期"
    above = ob.simulate(cycle(10300.0, {date(2020, 1, 3): (flat(), Q(50.01, None, 49.0, 51.0))}))
    assert above["X3"]["exit"] == "到期"


def test_x2_take_profit_sells_everything_the_day_after():
    daily = {date(2020, 1, 6): (Q(320.0, None, 318.0, 322.0), Q(20.0, None, 19.0, 21.0)),     # 340 ≥ 1.5 × 200 → 觸發
             date(2020, 1, 7): (Q(310.0, None, 305.0, 315.0), Q(18.0, None, 17.0, 19.0))}
    r = ob.simulate(cycle(10250.0, daily))
    assert r["X2"]["pnl"] == pytest.approx(sell(305) + sell(17) - 201)             # 119.878
    assert r["X2"]["exit"] == "2020-01-07"
    # 同一天賣權剩 20%：X3 也觸發，賣掉賣權、留買權到期
    assert r["X3"]["pnl"] == pytest.approx(sell(17) + expire(250) - 201)           # 65.333
    assert r["X0"]["pnl"] == pytest.approx(expire(250) - 201)
    assert r["X1"]["pnl"] == pytest.approx(2 * sell(98) - 201)                    # 1/8 的平價 98


def test_x2_threshold_is_inclusive_at_1_5_times_the_premium():
    below = {date(2020, 1, 6): (Q(299.0, None, 297.0, 301.0), Q(0.0, 0.9, None, None))}      # 299 + 0.9（賣權沒結算價，用收盤價）＝ 299.9 < 300
    assert ob.simulate(cycle(10250.0, below))["X2"]["exit"] == "到期"
    at = {date(2020, 1, 6): (Q(299.0, None, 297.0, 301.0), Q(0.0, 1.0, None, None))}         # 剛好 300 ＝ 1.5 × 200 → 觸發
    assert ob.simulate(cycle(10250.0, at))["X2"]["exit"] == "2020-01-07"


def test_trigger_on_the_last_day_before_expiry_settles_at_expiry_value():
    daily = {date(2020, 1, 14): (Q(400.0, None, 395.0, 405.0), Q(10.0, None, 9.0, 11.0))}     # 410 ≥ 300；賣權剩 10%
    r = ob.simulate(cycle(10200.0, daily))
    assert r["X2"]["exit"] == "到期" and r["X2"]["pnl"] == pytest.approx(r["X0"]["pnl"])      # 隔天就是到期日：以到期價值結算
    assert r["X3"]["exit"] == "到期" and r["X3"]["pnl"] == pytest.approx(r["X0"]["pnl"])
    assert r["delayed"] == 0


def test_sale_is_pushed_back_when_a_leg_has_no_quote_the_next_day():
    daily = {date(2020, 1, 3): (Q(320.0, None, 318.0, 322.0), Q(20.0, None, 19.0, 21.0)),     # X2 觸發
             date(2020, 1, 6): (None, Q(18.0, None, 17.0, 19.0)),                              # 隔天買權沒報價
             date(2020, 1, 7): (Q(300.0, None, 295.0, 305.0), Q(15.0, None, 14.0, 16.0))}
    r = ob.simulate(cycle(10250.0, daily))
    assert r["X2"]["pnl"] == pytest.approx(sell(295) + sell(14) - 201) and r["X2"]["exit"] == "2020-01-07"
    assert r["X3"]["pnl"] == pytest.approx(sell(17) + expire(250) - 201) and r["X3"]["exit"] == "2020-01-06"   # 只賣賣權：不需要買權的報價
    assert r["delayed"] == 1


def test_no_quote_for_the_rest_of_the_cycle_means_hold_to_expiry():
    gone = {d: (None, flat()) for d in DAYS[2:]}
    gone[date(2020, 1, 3)] = (Q(320.0, None, 318.0, 322.0), Q(20.0, None, 19.0, 21.0))
    r = ob.simulate(cycle(10250.0, gone))
    assert r["X2"]["exit"] == "到期" and r["X2"]["pnl"] == pytest.approx(r["X0"]["pnl"])
    assert r["X1"]["exit"] == "到期"
    assert r["delayed"] == 2                                          # X1 與 X2 各有一次賣不掉（X3 只賣賣權，賣得掉）


def test_a_day_with_a_missing_mark_cannot_trigger():
    daily = {date(2020, 1, 3): (None, Q(10.0, None, 9.0, 11.0))}       # 買權那天沒資料，沒辦法判斷總和或比例
    r = ob.simulate(cycle(10250.0, daily))
    assert r["X3"]["exit"] == "到期" and r["X2"]["exit"] == "到期"


def test_simulate_gives_none_without_entry_price_or_final_settlement():
    assert ob.simulate(cycle(None)) is None
    assert ob.simulate(cycle(10100.0, entry=Q(None, None, None, None))) is None
    c = cycle(10100.0)
    c.legs[PUT].entry = Q(None, None, 3.0, 4.0)
    assert ob.simulate(c) is None


def test_simulate_on_other_legs_strangle():
    wings = ((K + 200, "C"), (K - 200, "P"))
    c = cycle(10450.0)
    for w in wings:
        c.legs[w] = Leg(Q(40.0, None, 38.0, 40.0), [(d, Q(40.0, None, 38.0, 40.0)) for d in DAYS])
    r = ob.simulate(c, wings)
    assert r["outlay"] == pytest.approx(2 * buy(40)) and r["premium"] == pytest.approx(80.0)
    assert r["X0"]["pnl"] == pytest.approx(expire(250) - 2 * buy(40))              # 只有買權價內 250
    assert r["X1"]["pnl"] == pytest.approx(2 * sell(38) - 2 * buy(40))


def test_fixed_half_spread_ignores_the_quotes_and_prices_off_the_settle():
    wide = Q(100.0, None, 40.0, 100.0)                                # 收盤的最後最佳買價很寬（40），照登記規則賣出會退回 98
    daily = {d: (wide, wide) for d in DAYS}
    r = ob.simulate(cycle(10300.0, daily), half_spread=0.015)
    assert r["outlay"] == pytest.approx(2 * buy(101.5)) and r["premium"] == pytest.approx(203.0)
    assert r["X0"]["pnl"] == pytest.approx(expire(300) - 2 * buy(101.5))
    assert r["X1"]["pnl"] == pytest.approx(2 * sell(98.5) - 2 * buy(101.5))     # 結算價 100 × (1 − 1.5%)，不看報價
    registered = ob.simulate(cycle(10300.0, daily))
    assert registered["X1"]["pnl"] == pytest.approx(2 * sell(98.0) - 201)       # 40 < 0.75 × 100：不可信 → 結算價 × 0.98
    assert ob.simulate(cycle(10300.0, entry=Q(None, None, 1.0, 2.0)), half_spread=0.015) is None


# ── 獨立重寫的狀態機：隨機週期逐條比對 ─────────────────────────────

def brute(c, rule):
    """逐日走、持有部位、掛單隔天成交的狀態機；和 simulate 用 index 往前找的寫法完全不同。回傳這條規則的總損益。"""
    keys = list(c.straddle)
    ask = {k: ob.ask_px(c.legs[k].entry) for k in keys}
    outlay, prem = sum(ob.buy_cost(a) for a in ask.values()), sum(ask.values())
    quotes = {k: dict(c.legs[k].daily) for k in keys}
    days = sorted({d for q in quotes.values() for d in q if d < c.expiry_date})
    st = {"held": set(keys), "cash": -outlay, "pending": None}
    fired = False

    def try_sell(d):
        bids = [ob.bid_px(quotes[k][d]) if d in quotes[k] else None for k in st["pending"]]
        if None not in bids:
            st["cash"] += sum(ob.sell_proceeds(b) for b in bids)
            st["held"] -= set(st["pending"])
            st["pending"] = None

    for d in days:
        if st["pending"]:
            try_sell(d)
        if fired:
            continue
        marks = {k: ob.mark(quotes[k].get(d)) for k in keys}
        if rule == "X1" and (c.expiry_date - d).days <= 7:
            fired, st["pending"] = True, list(st["held"])
            try_sell(d)                                               # X1 當天收盤就賣
        elif rule == "X2" and None not in marks.values() and sum(marks.values()) >= 1.5 * prem:
            fired, st["pending"] = True, list(st["held"])             # 隔天才賣
        elif rule == "X3" and None not in marks.values():
            loser = min(keys, key=lambda k: marks[k] / ask[k])
            if marks[loser] / ask[loser] <= 0.5:
                fired, st["pending"] = True, [loser]
    return st["cash"] + sum(ob.settle_proceeds(ob.intrinsic(k, c.final_F)) for k in st["held"])


def random_cycle(rng):
    n = rng.randint(0, 15)
    days = [EXPIRY - timedelta(days=n - i) for i in range(n)]

    def rand_q():
        if rng.random() < 0.12:
            return None
        s = rng.choice([None, rng.uniform(5, 400), rng.uniform(5, 400), rng.uniform(5, 400)])
        close = rng.choice([None, rng.uniform(5, 400)]) if s is None else None
        base = s if s is not None else (close or 100.0)
        bid = rng.choice([base * rng.uniform(0.8, 1.05), base * rng.uniform(0.8, 1.05), None, 0.0, base * rng.uniform(1.3, 2.0)])
        ask = rng.choice([base * rng.uniform(0.95, 1.2), base * rng.uniform(0.95, 1.2), None, 0.0, base * rng.uniform(0.3, 0.8)])
        return Q(s, close, bid, ask)

    call, put = Leg(Q(rng.uniform(30, 300), None, None, None)), Leg(Q(rng.uniform(30, 300), None, None, None))
    for leg in (call, put):
        leg.entry = Q(leg.entry.settle, None, leg.entry.settle * 0.98, leg.entry.settle * rng.choice([1.0, 1.02, 1.5]))
        for d in days:
            q = rand_q()
            if q is not None:
                leg.daily.append((d, q))
        leg.daily.append((EXPIRY, Q(0.0, 5.0, None, None)))
    return Cycle("main", "202001", EXPIRY, EXPIRY - timedelta(days=n + 1), K, {CALL: call, PUT: put}, (CALL, PUT), final_F=K + rng.uniform(-500, 500))


def test_simulate_matches_an_independent_state_machine_on_random_cycles():
    rng = random.Random(20201013)
    fired = {"X1": 0, "X2": 0, "X3": 0}
    for _ in range(400):
        c = random_cycle(rng)
        r = ob.simulate(c)
        assert r is not None
        for rule in ob.RULES:
            assert r[rule]["pnl"] == pytest.approx(brute(c, rule), abs=1e-9), (rule, c)
        for rule in fired:
            fired[rule] += r[rule]["exit"] != "到期"
    assert all(v >= 30 for v in fired.values()), fired                # 隨機資料真的有觸發各條規則，比對才有意義


# ── 統計 ──────────────────────────────────────────────────────────

def test_one_sample_t_by_hand():
    r = ob.one_sample_t([1, 2, 3, 4, 5])
    se = math.sqrt(2.5) / math.sqrt(5)
    assert r["n"] == 5 and r["mean"] == 3 and r["se"] == pytest.approx(se) and r["t"] == pytest.approx(3 / se)
    assert r["mde"] == pytest.approx((ob.Z_CLAIM + ob.Z_POWER) * se)
    assert math.isnan(ob.one_sample_t([1, 2])["t"]) and math.isnan(ob.one_sample_t([2, 2, 2, 2])["t"])      # 太少、沒有變化


def test_welch_by_hand_and_direction():
    a, b = [1, 2, 3, 4, 5], [2, 3, 4, 5, 6, 7]
    r = ob.welch(a, b)
    se = math.sqrt(2.5 / 5 + 3.5 / 6)
    assert r["diff"] == pytest.approx(-1.5) and r["se"] == pytest.approx(se) and r["t"] == pytest.approx(-1.5 / se)
    assert r["mde"] == pytest.approx((ob.Z_CLAIM + ob.Z_POWER) * se)
    assert ob.welch(b, a)["t"] == pytest.approx(1.5 / se)                      # a − b：對調就反號
    assert math.isnan(ob.welch([1, 2], b)["t"]) and ob.welch([1, 2], b)["diff"] is None


def test_verdict_thresholds_and_sign():
    assert ob.verdict(ob.Z_CLAIM + 1e-9) == "過關" and ob.verdict(-ob.Z_CLAIM - 1e-9) == "過關"
    assert ob.verdict(ob.Z_CLAIM) == "過關" and ob.verdict(-ob.Z_CLAIM) == "過關"                     # 剛好等於門檻算過關
    assert ob.verdict(ob.Z_CLAIM - 0.01) == "線索" and ob.verdict(-2.0) == "線索" and ob.verdict(1.99) == "沒有"
    assert ob.verdict(float("nan")) == "無法檢定"


def test_describe_is_deterministic_and_correct():
    x = [-3.0, -1.0, 0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0]
    d = ob.describe(x)
    assert d["n"] == 10 and d["mean"] == pytest.approx(5.2) and d["median"] == 5.0 and d["win"] == 0.7
    assert d["p05"] == x[0] and d["p95"] == x[9]
    assert d["boot_lo"] < d["mean"] < d["boot_hi"]
    assert ob.describe(x) == d and ob.describe(x, seed=8) != d
    assert ob.describe([])["n"] == 0 and ob.describe([])["mean"] is None
    one = ob.describe([3.0])
    assert one["mean"] == 3.0 and one["sd"] is None and one["boot_lo"] == one["boot_hi"] == 3.0


def test_describe_percentiles_on_twenty_values():
    x = [float(i) for i in range(1, 21)]
    d = ob.describe(x)
    assert (d["p05"], d["p95"]) == (2.0, 20.0)                            # int(0.05 × 20)＝1 → 第 2 小；int(0.95 × 20)＝19 → 最大


def test_terciles_take_the_lowest_and_highest_third():
    items = [(v, v * 10) for v in (9, 1, 5, 3, 7, 2, 8, 4, 6, 10)]          # n=10 → 各 3 個
    low, high = ob.split_terciles(items)
    assert sorted(low) == [10, 20, 30] and sorted(high) == [80, 90, 100]
    assert ob.split_terciles(items[:2]) == ([], [])


# ── 訊號（純函式）──────────────────────────────────────────────────

def test_iv_percentile_window_excludes_the_latest_and_older_than_250():
    hist = [1000.0] * 10 + [50.0] * 250 + [60.0]
    assert ob.entry_signals(hist, [])["iv_pct"] == 100.0                      # 最新 60 比前 250 筆（都是 50）全高；更早的 1000 不算
    assert ob.entry_signals([1000.0] * 10 + [50.0] * 250 + [10.0], [])["iv_pct"] == 0.0
    mixed = [1000.0] * 10 + [float(i) for i in range(250)] + [100.0]
    assert ob.entry_signals(mixed, [])["iv_pct"] == pytest.approx(100 * 100 / 250)   # 前 250 筆裡嚴格小於 100 的有 100 筆
    assert ob.entry_signals([50.0] * 250, [])["iv_pct"] is None                  # 要 > 250 筆
    assert ob.entry_signals([], [])["iv"] is None and ob.entry_signals([], [])["iv_pct"] is None


def test_realized_vol_and_spike_signals():
    rets = [0.01, -0.01] * 10
    s = ob.entry_signals([20.0], rets)
    rv = statistics.stdev(rets) * math.sqrt(252) * 100
    assert s["rv"] == pytest.approx(rv) and s["ivrv"] == pytest.approx(20.0 / rv)
    assert ob.entry_signals([20.0], rets[-19:])["rv"] is None                    # 要 20 筆
    old_big = [0.08] + [0.001] * 5
    assert ob.entry_signals([20.0], old_big)["spike"] == 0.001                   # 只看最近 5 筆
    assert ob.entry_signals([20.0], [0.01, -0.03, 0.02, 0.0, 0.005])["spike"] == pytest.approx(0.03)
    assert ob.entry_signals([20.0], [0.01] * 4)["spike"] is None
    assert ob.entry_signals([], rets)["ivrv"] is None


# ── 合成的鏈 ──────────────────────────────────────────────────────

START, END = date(2019, 1, 2), date(2020, 3, 31)


def weekdays(a, b):
    d = a
    while d <= b:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def month_expiry(y, m):
    return expiry_of(f"{y}{m:02d}").date()


def S(d):
    t = d.toordinal() - START.toordinal()
    return 10000.0 + 300 * math.sin(2 * math.pi * t / 45) + 100 * math.sin(2 * math.pi * t / 11) + 2.0 * t


def SIG(d):
    return 0.18 + 0.06 * math.sin(d.toordinal() / 30.0)


def T_years(d, e):
    return (datetime.combine(e, time(13, 30)) - datetime.combine(d, time(13, 45))).total_seconds() / (365 * 86400)


def px(s, k, d, e, right):
    return round(black76(s, k, T_years(d, e), SIG(d), 0.0, right == "C"), 1)


def nearest_expiries(d, n=2):
    out, y, m = [], d.year, d.month
    while len(out) < n:
        e = month_expiry(y, m)
        if e >= d:
            out.append(e)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def gen_rows(start, end, path=S, drop=()):
    """期交所近價鏈的列（欄位與 option_history.iter_rows 一致）：每天最近 2 個月選擇權、履約價間隔 100（F ± 12%）；到期日結算價整欄 0。"""
    rows = []
    for d in weekdays(start, end):
        s = path(d)
        for e in nearest_expiries(d):
            code = f"{e:%Y%m}"
            if (d, code) in drop:
                continue
            base = {"date": d.isoformat(), "kind": "M", "expiry": code, "expiry_date": e.isoformat(), "session": "G", "F": s, "volume": 1.0, "oi": 1.0}
            k0 = round(s / 100) * 100
            for i in range(-12, 13):
                for right in ("C", "P"):
                    k = float(k0 + 100 * i)
                    if d == e:                                        # 到期日：結算價 0、收盤價是最後成交價、沒有最佳買賣價
                        itm = max(s - k, 0.0) if right == "C" else max(k - s, 0.0)
                        rows.append({**base, "right": right, "strike": k, "settle": 0.0, "close": max(itm, 0.1), "bid": None, "ask": None})
                    else:
                        p = px(s, k, d, e, right)
                        rows.append({**base, "right": right, "strike": k, "settle": p, "close": p, "bid": round(p * 0.99, 1), "ask": round(p * 1.01, 1)})
    return rows


def walk(rows, rules=("main", "dte10")):
    w = ob.Walker(rules)
    for day, exps in ob.iter_days(rows):
        w.feed(day, exps)
    return w


@pytest.fixture(scope="module")
def world():
    rows = gen_rows(START, END)
    return rows, walk(rows)


def next_weekday(d):
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


MAIN_EXPIRIES = [month_expiry(2019, m) for m in range(2, 13)] + [month_expiry(2020, m) for m in (1, 2, 3)]


def test_iter_days_groups_by_date_and_keeps_only_regular_session_monthlies():
    rows = gen_rows(date(2019, 3, 4), date(2019, 3, 5))
    extra = [{**rows[0], "kind": "W", "expiry": "201903W2"}, {**rows[0], "session": "A"}]
    days = list(ob.iter_days(extra + rows))
    assert [d for d, _ in days] == [date(2019, 3, 4), date(2019, 3, 5)]
    for d, exps in days:
        assert sorted(exps) == ["201903", "201904"] and all(e.expiry_date >= d for e in exps.values())
        assert exps["201903"].F == S(d) and len(exps["201903"].rows) == 50
    assert list(ob.iter_days([])) == []
    with pytest.raises(ValueError, match="沒有依日期排序"):
        list(ob.iter_days(rows[-1:] + rows[:1]))


def test_atm_iv_recovers_the_volatility_the_prices_were_built_from():
    d, e = date(2020, 1, 2), date(2020, 2, 19)
    x = Exp("202002", e, 10000.0)
    T = ob.years_to(d, e)
    for i in range(-8, 9):
        for right in ("C", "P"):
            p = black76(10000.0, 10000.0 + 100 * i, T, 0.22, 0.0, right == "C")
            x.rows[(10000.0 + 100 * i, right)] = Q(p, p, p, p)
    iv, F = ob.atm_iv(d, x)
    assert iv == pytest.approx(22.0, abs=0.01) and F == pytest.approx(10000.0, abs=0.1)
    assert ob.years_to(date(2020, 1, 15) - timedelta(days=30), date(2020, 1, 15)) * 365 == pytest.approx(30 - 15 / 1440)   # 13:45 到到期日 13:30
    assert ob.atm_iv(e, x) is None                                     # 到期日當天 T ≤ 0
    thin = Exp("202002", e, 10000.0, {k: v for k, v in list(x.rows.items())[:4]})
    assert ob.atm_iv(d, thin) is None                                  # 買賣權成對的履約價不到 3 檔
    for sigma in (2.0, 0.01):                                          # IV 200%、1%：超出合理範圍
        y = Exp("202002", e, 10000.0)
        for i in range(-8, 9):
            for right in ("C", "P"):
                p = black76(10000.0, 10000.0 + 100 * i, T, sigma, 0.0, right == "C")
                y.rows[(10000.0 + 100 * i, right)] = Q(p, p, p, p)
        assert ob.atm_iv(d, y) is None


def test_daily_iv_uses_nearest_expiry_with_at_least_7_days_left():
    d = date(2020, 1, 8)
    near, far = Exp("202001", date(2020, 1, 15), 10000.0), Exp("202002", date(2020, 2, 19), 10000.0)
    for x, sigma in ((near, 0.40), (far, 0.20)):
        T = ob.years_to(d, x.expiry_date)
        for i in range(-8, 9):
            for right in ("C", "P"):
                p = black76(10000.0, 10000.0 + 100 * i, T, sigma, 0.0, right == "C")
                x.rows[(10000.0 + 100 * i, right)] = Q(p, p, p, p)
    # 1/8 13:45 → 1/15 13:30 差 7 天少 15 分鐘：不夠 7 天（和 vrp_study 同定義），所以用遠月
    assert ob.daily_iv(d, {"202001": near, "202002": far}) == pytest.approx(20.0, abs=0.01)
    assert ob.daily_iv(date(2020, 1, 7), {"202001": near, "202002": far}) is not None
    assert ob.daily_iv(date(2020, 1, 14), {"202001": near}) is None


def test_day_return_uses_the_same_expiry_to_avoid_roll_gaps():
    a, b = Exp("201912", date(2019, 12, 18), 100.0), Exp("202001", date(2020, 1, 15), 101.0)
    a2, b2 = Exp("201912", date(2019, 12, 18), 102.0), Exp("202001", date(2020, 1, 15), 103.5)
    day = date(2019, 12, 18)
    assert ob.day_return({"201912": a, "202001": b}, {"201912": a2, "202001": b2}, day) == pytest.approx(math.log(102 / 100))     # 最近的共同月
    # 12 月合約今天不在了（資料缺）：用 1 月合約自己的前後比，不拿 12 月前一天去比 1 月今天（那是換月跳空）
    assert ob.day_return({"201912": a, "202001": b}, {"202001": b2}, day) == pytest.approx(math.log(103.5 / 101))
    assert ob.day_return({"201912": a}, {"202001": b2}, day) is None
    assert ob.day_return({"201912": Exp("201912", date(2019, 12, 18), 0.0)}, {"201912": a2}, day) is None


def test_pick_straddle_closest_strike_with_both_legs():
    e = Exp("202002", date(2020, 2, 19), 10040.0)
    for k in (9900.0, 10000.0, 10100.0):
        for right in ("C", "P"):
            e.rows[(k, right)] = Q(100.0, None, 98.0, 101.0)
    assert ob.pick_straddle(e) == ((10000.0, "C"), (10000.0, "P"))
    del e.rows[(10000.0, "C")]                                          # 10000 沒有買權：退到下一個最近的
    assert ob.pick_straddle(e) == ((10100.0, "C"), (10100.0, "P"))
    e.rows[(10100.0, "P")] = Q(None, None, None, None)                    # 10100 的賣權算不出價
    assert ob.pick_straddle(e) == ((9900.0, "C"), (9900.0, "P"))
    e.F = 10050.0
    f = Exp("202002", date(2020, 2, 19), 10050.0, {(k, r): Q(100.0, None, 98.0, 101.0) for k in (10000.0, 10100.0) for r in ("C", "P")})
    assert ob.pick_straddle(f) == ((10000.0, "C"), (10000.0, "P"))      # 平手取較低的
    assert ob.pick_straddle(Exp("202002", date(2020, 2, 19), 10050.0)) is None


def test_pick_strangle_wings_have_delta_closest_to_quarter():
    d, e = date(2020, 1, 2), date(2020, 2, 19)
    x = Exp("202002", e, 10000.0)
    T, sigma = ob.years_to(d, e), 0.20
    for i in range(-8, 9):
        for right in ("C", "P"):
            p = black76(10000.0, 10000.0 + 100 * i, T, sigma, 0.0, right == "C")
            x.rows[(10000.0 + 100 * i, right)] = Q(p, p, p * 0.99, p * 1.01)
    iv = ob.atm_iv(d, x)[0] / 100                                       # 挑翼用的是從鏈上反推的 IV

    def delta(k, right):
        d1 = (math.log(10000.0 / k) + 0.5 * iv * iv * T) / (iv * math.sqrt(T))
        return NormalDist().cdf(d1) if right == "C" else NormalDist().cdf(d1) - 1.0

    best_c = min((k for (k, r) in x.rows if r == "C" and k > 10000.0), key=lambda k: abs(abs(delta(k, "C")) - 0.25))
    best_p = min((k for (k, r) in x.rows if r == "P" and k < 10000.0), key=lambda k: abs(abs(delta(k, "P")) - 0.25))
    assert ob.pick_strangle(d, x) == ((best_c, "C"), (best_p, "P"))
    assert best_c > 10000.0 > best_p and abs(abs(delta(best_c, "C")) - 0.25) < 0.1
    only_calls = Exp("202002", e, 10000.0, {k: v for k, v in x.rows.items() if k[1] == "C"})
    assert ob.pick_strangle(d, only_calls) is None


def test_iter_days_applies_expiry_fix_and_records_what_changed():
    rows = gen_rows(date(2019, 3, 4), date(2019, 3, 5))
    changed = {}
    days = list(ob.iter_days(rows, {"201903": date(2019, 3, 25), "201904": date(2019, 4, 17)}, changed))      # 201904 的更正值和檔案裡一樣：不算更正
    for _, exps in days:
        assert exps["201903"].expiry_date == date(2019, 3, 25) and exps["201904"].expiry_date == date(2019, 4, 17)
    assert changed == {"201903": (date(2019, 3, 20), date(2019, 3, 25))}
    assert next(iter(ob.iter_days(rows)))[1]["201903"].expiry_date == date(2019, 3, 20)


def test_expiry_fixes_only_use_contracts_that_have_ended():
    last = {"201001": "2010-01-20", "202609": "2026-09-16", "202610": "2026-10-12", "202611": "2026-10-12"}      # TX 資料最後一天是 10/12
    assert ob.expiry_fixes(last) == {"201001": date(2010, 1, 20), "202609": date(2026, 9, 16)}                # 還在交易的兩個，最後成交日只是資料的最後一天
    assert ob.expiry_fixes({}) == {}


TX_HEAD = ("交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,"
           "歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,價差對單式委託成交量")


def tx_line(day, code, close, vol=100, session="一般"):
    return f"{day.replace('-', '/')},TX,{code},{close},{close},{close},{close},0,0,{vol},0,1,-,-,-,-,,{session},0"


def write_tx(path, lines):
    path.write_bytes(("\n".join([TX_HEAD] + lines) + "\n").encode("cp950"))


def test_tx_expiry_data_reads_last_trading_day_and_close(tmp_path):
    write_tx(tmp_path / "tx_2019_06.csv", [
        tx_line("2019-06-18", "201906", 10100), tx_line("2019-06-19", "201906", 10234.5), tx_line("2019-06-19", "201907", 10240),
        tx_line("2019-06-20", "201907", 10250), tx_line("2019-06-20", "201906", 10300, vol=0),            # 沒成交量：不算
        tx_line("2019-06-21", "201906", 10400, session="盤後"),                                            # 盤後：不算
        tx_line("2019-06-21", "201906/201907", 10), tx_line("2019-06-19", "201909", 10300)])              # 價差單：不算
    last, close = ob.tx_expiry_data(tmp_path)
    assert last["201906"] == "2019-06-19" and last["201907"] == "2019-06-20" and last["201909"] == "2019-06-19"
    assert close[("201906", "2019-06-19")] == 10234.5 and ("201906", "2019-06-20") not in close and "201906/201907" not in last
    assert ob.tx_expiry_data(tmp_path / "nothing") == ({}, {})


def dropped_world(gap=date(2019, 6, 19)):
    return walk(gen_rows(date(2019, 3, 1), date(2019, 9, 30), drop={(gap, "201906")}), rules=("main",))


def test_rescue_takes_the_final_settlement_from_tx():
    w = dropped_world()
    cyc = w.dropped[0]
    assert cyc.expiry == "201906" and ob.simulate(cyc) is None
    assert ob.rescue(w, {"201906": "2019-06-19"}, {("201906", "2019-06-19"): 10234.5}) == 1
    assert w.dropped == [] and cyc in w.done and cyc.rescued and cyc.final_F == 10234.5 and cyc.expiry_date == date(2019, 6, 19)
    k = cyc.straddle[0][0]
    r = ob.simulate(cyc)
    assert r["X0"]["pnl"] == pytest.approx(expire(abs(10234.5 - k)) - r["outlay"])
    assert [c.expiry_date for c in w.done] == sorted(c.expiry_date for c in w.done)             # 補完後照到期日排序


def test_rescue_uses_the_last_tx_day_when_the_expiry_was_shifted():
    w = dropped_world()
    assert ob.rescue(w, {"201906": "2019-06-24"}, {("201906", "2019-06-24"): 10111.0}) == 1
    c = next(c for c in w.done if c.expiry == "201906")
    assert c.expiry_date == date(2019, 6, 24) and c.final_F == 10111.0


def test_rescue_leaves_the_cycle_dropped_without_usable_tx():
    for last, close in (({}, {}), ({"201906": "2019-06-19"}, {}), ({"201906": "2019-03-04"}, {("201906", "2019-03-04"): 10000.0})):     # 沒資料、沒收盤價、最後成交日在進場之前
        w = dropped_world()
        assert ob.rescue(w, last, close) == 0 and len(w.dropped) == 1 and not any(c.rescued for c in w.done)


def test_expiry_fix_makes_the_next_cycle_wait_for_the_real_expiry():
    rows = gen_rows(date(2019, 3, 1), date(2019, 9, 30))
    fix = {"201906": date(2019, 6, 24)}                                       # 6 月合約其實 6/24 才到期，但檔案只有到 6/19 的資料（假日順延、被截掉）
    w = ob.Walker(("main",))
    for day, exps in ob.iter_days(rows, fix, w.fixed):
        w.feed(day, exps)
    assert w.fixed == {"201906": (date(2019, 6, 19), date(2019, 6, 24))}
    nxt = next(c for c in w.done if c.expiry == "201907")
    assert nxt.entry_day == date(2019, 6, 25)                                 # 6/20 起 6 月合約還沒到期：不能進
    assert [c.expiry for c in w.dropped] == ["201906"]                        # 6/24 沒有資料 → 先丟棄，之後用 TX 補
    assert ob.rescue(w, {"201906": "2019-06-24"}, {("201906", "2019-06-24"): 10050.0}) == 1


def test_sensitivity_is_descriptive_and_uses_the_fixed_spread(world):
    _, w = world
    cs = [c for c in w.done if c.rule == "main"]
    sv = ob.sensitivity(cs)
    assert sv["half_spread"] == 0.015 and sv["n"] == len(cs)
    manual = [ob.simulate(c, half_spread=0.015) for c in cs]
    assert sv["T1"]["mean"] == pytest.approx(statistics.mean(m["X0"]["roi"] for m in manual))
    assert sv["T2"]["mean"] == pytest.approx(statistics.mean(m["X1"]["roi"] - m["X0"]["roi"] for m in manual))
    assert "verdict" not in sv["T1"]


# ── 週期怎麼切（合成的整條鏈）──────────────────────────────────────

def test_main_cycles_enter_the_day_after_the_previous_expiry_and_match_hand_computation(world):
    _, w = world
    main = sorted((c for c in w.done if c.rule == "main"), key=lambda c: c.expiry_date)
    assert [c.expiry_date for c in main] == MAIN_EXPIRIES                  # 2019-01 到期的那個因為起點已經在到期前兩週，不算
    prev = [month_expiry(2019, 1)] + MAIN_EXPIRIES[:-1]
    for c, p in zip(main, prev):
        d0 = next_weekday(p)
        assert c.entry_day == d0 and 27 <= (c.expiry_date - d0).days <= 34      # 相鄰兩個第三個週三差 4 或 5 週，進場在前一個到期日的隔天
        assert c.F_entry == S(d0) and c.final_F == S(c.expiry_date)
        k = float(round(S(d0) / 100) * 100)
        assert c.straddle == ((k, "C"), (k, "P"))
        ask = [round(px(S(d0), k, d0, c.expiry_date, r) * 1.01, 1) for r in ("C", "P")]
        r = ob.simulate(c)
        assert r["premium"] == pytest.approx(sum(ask))
        assert r["outlay"] == pytest.approx(sum(buy(a) for a in ask))
        payoff = max(S(c.expiry_date) - k, 0.0) + max(k - S(c.expiry_date), 0.0)
        assert r["X0"]["pnl"] == pytest.approx(expire(payoff) - sum(buy(a) for a in ask))
        # 每個週期的每天資料都有收到：進場後到到期日（含）的每個交易日
        assert [x for x, _ in c.legs[c.straddle[0]].daily] == [x for x in weekdays(d0, c.expiry_date) if x > d0]
    assert [c.expiry for c in w.open if c.rule == "main"] == ["202004"] and not any(c.expiry == "202004" for c in w.done)


def test_dte10_cycles_enter_on_the_first_day_within_10_calendar_days(world):
    _, w = world
    cs = sorted((c for c in w.done if c.rule == "dte10"), key=lambda c: c.expiry_date)
    assert [c.expiry_date for c in cs] == [month_expiry(2019, 1)] + MAIN_EXPIRIES
    for c in cs:
        d0 = next(d for d in weekdays(c.expiry_date - timedelta(days=30), c.expiry_date) if (c.expiry_date - d).days <= 10)
        assert c.entry_day == d0
    assert len({(c.rule, c.expiry) for c in w.done}) == len(w.done)


def test_signals_match_hand_computation(world):
    _, w = world
    c = next(c for c in w.done if c.rule == "main" and c.entry_day == date(2020, 1, 16))
    p = date(2020, 1, 15)                                               # 進場日的前一個交易日
    dates = sorted(w.iv_by_date)
    i = dates.index(p.isoformat())
    prior = [w.iv_by_date[x] for x in dates[i - 250:i]]
    latest = w.iv_by_date[p.isoformat()]
    assert c.signals["iv"] == latest
    assert c.signals["iv_pct"] == pytest.approx(sum(v < latest for v in prior) / 250 * 100)
    wd = list(weekdays(START, END))
    rets = {d: math.log(S(d) / S(wd[j - 1])) for j, d in enumerate(wd) if j}
    j = wd.index(p)
    last20 = [rets[x] for x in wd[j - 19:j + 1]]
    assert c.signals["rv"] == pytest.approx(statistics.stdev(last20) * math.sqrt(252) * 100)
    assert c.signals["ivrv"] == pytest.approx(latest / c.signals["rv"])
    assert c.signals["spike"] == pytest.approx(max(abs(rets[x]) for x in wd[j - 4:j + 1]))


def test_signals_never_see_the_entry_day_or_later(world):
    rows, w = world
    entry = date(2020, 1, 16)
    base = next(c for c in w.done if c.rule == "main" and c.entry_day == entry)

    def jumped(from_day):
        return walk(gen_rows(START, END, lambda d: S(d) * (1.05 if d >= from_day else 1.0)))

    later = next(c for c in jumped(entry).done if c.rule == "main" and c.entry_day == entry)       # 進場日起全部漲 5%
    assert later.F_entry == pytest.approx(base.F_entry * 1.05)                                   # 進場價確實變了
    assert later.signals == base.signals                                                          # 但訊號一模一樣
    earlier = next(c for c in jumped(entry - timedelta(days=1)).done if c.rule == "main" and c.entry_day == entry)   # 前一天起漲 5%
    assert earlier.signals["spike"] == pytest.approx(math.log(1.05), abs=0.03) and earlier.signals["spike"] > base.signals["spike"] + 0.02   # 對照組：前一天的資料看得到


def test_missing_expiry_day_drops_the_cycle_and_the_next_one_still_waits_for_the_day_after():
    gap = date(2019, 6, 19)
    assert gap == month_expiry(2019, 6)
    w = walk(gen_rows(date(2019, 3, 1), date(2019, 9, 30), drop={(gap, "201906")}), rules=("main",))
    assert [c.expiry for c in w.dropped] == ["201906"]
    assert "201906" not in [c.expiry for c in w.done] and "201907" in [c.expiry for c in w.done]
    nxt = next(c for c in w.done if c.expiry == "201907")
    assert nxt.entry_day == date(2019, 6, 20)                           # 不是 6/19（那天最近的合約變成 7 月，但 6 月合約的到期日還沒過）


def test_first_day_without_a_usable_straddle_delays_entry_but_not_into_the_expiry_week():
    start, end = date(2019, 5, 10), date(2019, 7, 31)                     # 5/15 是 5 月合約的到期日：起點要早於它，才看得到「前一個到期日」
    kill = {"2019-05-16", "2019-05-17"}                                   # 到期後前兩天把 6 月合約的買權全部清掉：挑不出跨式
    rows = [r for r in gen_rows(start, end) if not (r["date"] in kill and r["expiry"] == "201906" and r["right"] == "C")]
    c = next(c for c in walk(rows, rules=("main",)).done if c.expiry == "201906")
    assert c.entry_day == date(2019, 5, 20)
    rows2 = [r for r in gen_rows(start, end) if not (r["date"] <= "2019-06-07" and r["expiry"] == "201906" and r["right"] == "C")]
    assert "201906" not in [c.expiry for c in walk(rows2, rules=("main",)).done]       # 6/10 起距到期 < 10 天：整個週期放棄，不拖到到期週才進
    rows3 = [r for r in gen_rows(start, end) if not (r["date"] <= "2019-06-06" and r["expiry"] == "201906" and r["right"] == "C")]
    c3 = next(c for c in walk(rows3, rules=("main",)).done if c.expiry == "201906")
    assert c3.entry_day == date(2019, 6, 7) and (c3.expiry_date - c3.entry_day).days == 12      # 距到期 12 天還可以進


def test_data_start_is_not_a_cycle_but_dte10_does_not_need_a_previous_expiry(world):
    _, w = world
    assert "201901" not in {c.expiry for c in w.done if c.rule == "main"}          # 起點在 1/2、前一個到期日（12 月）沒看到
    assert "201901" in {c.expiry for c in w.done if c.rule == "dte10"}


# ── 整體統計（合成的週期）─────────────────────────────────────────

def test_run_tests_t1_matches_hand_computation():
    finals = [10300.0, 10050.0, 9800.0, 10000.0, 10450.0, 9900.0, 10120.0, 9600.0]
    cycles = [cycle(f) for f in finals]
    res = ob.run_tests(cycles)
    roi = [(expire(abs(f - K)) - 201) / 201 for f in finals]
    se = statistics.stdev(roi) / math.sqrt(len(roi))
    assert res["tests"]["T1"]["mean"] == pytest.approx(statistics.mean(roi))
    assert res["tests"]["T1"]["t"] == pytest.approx(statistics.mean(roi) / se)
    assert res["tests"]["T1"]["mde"] == pytest.approx((ob.Z_CLAIM + ob.Z_POWER) * se)
    # T2：X1(1/8 全賣，買價 98) − X0
    diffs = [(2 * sell(98) - 201) / 201 - r for r in roi]
    assert res["tests"]["T2"]["mean"] == pytest.approx(statistics.mean(diffs))
    # 沒有任何週期觸發 X2／X3：差全是 0 → 無法檢定
    assert res["tests"]["T3"]["verdict"] == res["tests"]["T4"]["verdict"] == "無法檢定"
    assert res["tests"]["T5"]["verdict"] == res["tests"]["T6"]["verdict"] == res["tests"]["T7"]["verdict"] == "無法檢定"   # 沒有訊號
    d = res["describe"]
    assert d["cycles"] == 8 and d["entry_dte"] == {"min": 28, "median": 28, "max": 28} and d["first"] == d["last"] == "2019-12-18"
    assert d["since2016"]["n"] == 8 and d["since2016"]["x0_pts_mean"] == pytest.approx(statistics.mean(expire(abs(f - K)) - 201 for f in finals))
    assert d["since2016"]["free_pts_mean"] == pytest.approx(statistics.mean(abs(f - K) - 200 for f in finals))
    assert d["by_year_x0"] == {"2019": {"n": 8, "mean": pytest.approx(statistics.mean(roi))}}
    assert d["early_exits"] == {"X1": 8, "X2": 0, "X3": 0} and d["strangle_x0"]["n"] == 0
    assert ob.run_tests([])["describe"]["cycles"] == 0 and "沒有可用" in ob.format_report(ob.run_tests([]))


def test_run_tests_skips_unpriceable_cycles_and_signals_split_the_groups():
    cs = []
    for i, f in enumerate([10300.0, 9700.0, 10100.0, 10000.0, 10500.0, 9500.0, 10200.0, 9900.0, 10050.0]):
        c = cycle(f)
        c.signals = {"iv_pct": 10.0 if i < 4 else 50.0, "ivrv": float(i), "spike": 0.03 if i % 3 == 0 else 0.01}
        cs.append(c)
    bad = cycle(None)
    bad.signals = {"iv_pct": 5.0}
    res = ob.run_tests(cs + [bad])
    assert res["describe"]["cycles"] == 9                                 # 缺最終結算價的週期不算
    roi = [(expire(abs(c.final_F - K)) - 201) / 201 for c in cs]
    t5 = res["tests"]["T5"]
    assert (t5["n_a"], t5["n_b"]) == (4, 5) and t5["diff"] == pytest.approx(statistics.mean(roi[:4]) - statistics.mean(roi[4:]))
    t6 = res["tests"]["T6"]                                               # ivrv 0..8：最低 3 個是 0,1,2；最高 3 個是 6,7,8
    assert (t6["n_a"], t6["n_b"]) == (3, 3) and t6["diff"] == pytest.approx(statistics.mean(roi[:3]) - statistics.mean(roi[6:]))
    t7 = res["tests"]["T7"]
    on, off = [roi[i] for i in (0, 3, 6)], [roi[i] for i in (1, 2, 4, 5, 7, 8)]
    assert (t7["n_a"], t7["n_b"]) == (3, 6) and t7["diff"] == pytest.approx(statistics.mean(on) - statistics.mean(off))


def test_signal_thresholds_are_inclusive():
    cs = []
    for i in range(6):
        c = cycle(10000.0 + 50 * (i + 1) * (-1) ** i)
        c.signals = {"iv_pct": 20.0 if i < 3 else 20.01, "ivrv": None, "spike": 0.025 if i < 3 else 0.0249}
        cs.append(c)
    t = ob.run_tests(cs)["tests"]
    assert (t["T5"]["n_a"], t["T5"]["n_b"]) == (3, 3) and (t["T7"]["n_a"], t["T7"]["n_b"]) == (3, 3)      # ≤ 20%、≥ 2.5% 都含邊界


def test_since_2016_starts_at_the_first_of_2016():
    a, b = cycle(10300.0), cycle(10300.0)
    a.entry_day, b.entry_day = date(2015, 12, 31), date(2016, 1, 1)
    assert ob.run_tests([a, b])["describe"]["since2016"]["n"] == 1


def test_strangle_stats_use_the_wings_of_each_cycle(world):
    _, w = world
    main = [c for c in w.done if c.rule == "main"]
    d = ob.run_tests(main)["describe"]["strangle_x0"]
    manual = [ob.simulate(c, c.strangle)["X0"]["roi"] for c in main]
    assert d["n"] == len(main) and d["mean"] == pytest.approx(statistics.mean(manual))
    assert all(c.strangle[0][1] == "C" and c.strangle[1][1] == "P" and c.strangle[0][0] > c.F_entry > c.strangle[1][0] for c in main)


def exp_with_straddle(expiry_date, F=10000.0):
    e = Exp("202002", expiry_date, F)
    for k in (9900.0, 10000.0, 10100.0):
        for right in ("C", "P"):
            e.rows[(k, right)] = Q(100.0, None, 99.0, 101.0)
    return e


def test_dte10_rule_boundaries_are_ten_calendar_days_and_never_the_expiry_day():
    expiry = date(2020, 2, 19)
    w = ob.Walker(("dte10",))
    w.feed(expiry - timedelta(days=11), {"202002": exp_with_straddle(expiry)})
    assert w.open == []                                                       # 還有 11 天：太早
    w.feed(expiry - timedelta(days=10), {"202002": exp_with_straddle(expiry)})
    assert [c.entry_day for c in w.open] == [expiry - timedelta(days=10)]     # 剛好 10 天：進場
    w2 = ob.Walker(("dte10",))
    w2.feed(expiry, {"202002": exp_with_straddle(expiry)})
    assert w2.open == []                                                      # 到期日當天不進場


def test_signals_missing_are_excluded_from_both_groups():
    cs = []
    for i in range(10):
        c = cycle(10000.0 + 100 * i)
        c.signals = {"iv_pct": None if i < 3 else (10.0 if i < 6 else 90.0), "ivrv": None, "spike": None}
        cs.append(c)
    t = ob.run_tests(cs)["tests"]
    assert (t["T5"]["n_a"], t["T5"]["n_b"]) == (3, 4)
    assert (t["T6"]["n_a"], t["T6"]["n_b"]) == (0, 0) and (t["T7"]["n_a"], t["T7"]["n_b"]) == (0, 0)


# ── 植入已知現象、沒有現象時的假陽性率 ──────────────────────────────

def gbm_cycle(rng, idx, mult, signals, n=30, iv=0.20, half_spread=0.005):
    """一個公平定價的跨式週期：選擇權用 Black-76（隱含波動 iv）報價，標的的真實波動是 mult × iv 的鞅（mult=1 就是公平賭局）。"""
    entry = date(2010, 1, 1) + timedelta(days=40 * idx)
    F, dt = 10000.0, 1 / 365

    def q(f, T, right):
        p = black76(f, K, T, iv, 0.0, right == "C")
        return Q(p, p, p * (1 - half_spread), p * (1 + half_spread))

    legs = {CALL: Leg(q(F, n * dt, "C")), PUT: Leg(q(F, n * dt, "P"))}
    v = mult * iv
    for i in range(1, n + 1):
        F *= math.exp(-0.5 * v * v * dt + v * math.sqrt(dt) * rng.gauss(0, 1))
        day = entry + timedelta(days=i)
        for key, right in ((CALL, "C"), (PUT, "P")):
            legs[key].daily.append((day, q(F, (n - i) * dt, right) if i < n else Q(0.0, 5.0, None, None)))
    return Cycle("main", f"G{idx}", entry + timedelta(days=n), entry, 10000.0, legs, (CALL, PUT), signals=signals, final_F=F)


def world_cycles(seed, n, mult_of):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        sig = {"iv_pct": rng.uniform(0, 100), "ivrv": rng.uniform(0.6, 1.6), "spike": rng.choice([0.005, 0.03, 0.005])}
        out.append(gbm_cycle(rng, i, mult_of(sig), sig))
    return out


def test_nothing_is_flagged_often_when_there_is_nothing_to_find():
    flagged = 0
    for seed in range(12):
        res = ob.run_tests(world_cycles(seed, 150, lambda s: 1.0))
        flagged += sum(v["verdict"] == "過關" for v in res["tests"].values())
    assert flagged <= 3, flagged                                          # 12 次 × 7 個檢定，每個 0.7% → 預期不到 1 個


@pytest.mark.parametrize("mult_of,key,sign", [
    (lambda s: 2.0 if s["iv_pct"] <= 20 else 1.0, "T5", +1),
    (lambda s: 2.0 if s["ivrv"] < 0.93 else 1.0, "T6", +1),
    (lambda s: 2.0 if s["spike"] >= 0.025 else 1.0, "T7", +1),
    (lambda s: 0.4 if s["spike"] >= 0.025 else 1.0, "T7", -1)])
def test_planted_effects_are_detected_with_the_right_sign(mult_of, key, sign):
    tests = ob.run_tests(world_cycles(7, 300, mult_of))["tests"]
    assert tests[key]["verdict"] == "過關", tests[key]
    assert tests[key]["diff"] * sign > 0.2 and tests[key]["t"] * sign > 0
    for other in ("T5", "T6", "T7"):                                      # 訊號之間互相獨立：沒被植入的不該跟著亮
        if other != key:
            assert abs(tests[other]["t"]) < ob.Z_CLAIM, (other, tests[other])


def test_fair_world_has_roughly_zero_expected_return_before_costs():
    cs = world_cycles(3, 400, lambda s: 1.0)
    free = [ob.simulate(c)["free"] / ob.simulate(c)["premium"] for c in cs]
    se = statistics.stdev(free) / math.sqrt(len(free))
    assert abs(statistics.mean(free)) < 3 * se                              # 鞅：無摩擦的期望報酬是 0
    ours = [ob.simulate(c)["X0"]["roi"] for c in cs]
    assert statistics.mean(ours) < statistics.mean(free)                    # 成本讓它更低


# ── 報表與 CLI ────────────────────────────────────────────────────

def test_format_report_has_every_registered_test_and_rule(world):
    _, w = world
    main = [c for c in w.done if c.rule == "main"]
    res = ob.run_tests(main)
    out = ob.format_report(res, ob.describe_rule([c for c in w.done if c.rule == "dte10"]), dropped=2)
    for k in ("T1", "T2", "T3", "T4", "T5", "T6", "T7"):
        assert f"  {k} " in out
    for r in ob.RULES:
        assert ob.RULE_TEXT[r] in out
    assert "丟棄 2 個" in out and "次要" in out and "對照先前示意" in out


def test_clean_replaces_nan_for_json():
    assert ob._clean({"a": float("nan"), "b": [1.0, float("nan")], "c": (2, {"d": float("nan")})}) == {"a": None, "b": [1.0, None], "c": [2, {"d": None}]}


def test_cli_sanity_and_run_on_a_small_store(tmp_path, capsys):
    rows = gen_rows(START, END, drop={(month_expiry(2019, 6), "201906")})          # 6 月合約到期日的資料缺 → 算不出最終結算價
    w0 = walk(rows)
    (tmp_path / "opt").mkdir()
    by_month = {}
    for r in rows:
        by_month.setdefault(r["date"][:7], []).append(r)
    for ym, rs in by_month.items():
        oh.write_rows(tmp_path / "opt" / f"chain_{ym[:4]}_{ym[5:]}.csv.gz", rs)
    # TX：每個合約最後一天的收盤＝合成的標的；6 月合約補在 6/24（假日順延）；資料延續到 2020-03-31 之後，所以都算「已結束」
    (tmp_path / "tx").mkdir()
    lines = [tx_line(month_expiry(y, m).isoformat(), f"{y}{m:02d}", round(S(month_expiry(y, m)), 1)) for y in (2019, 2020) for m in range(1, 13)
             if f"{y}{m:02d}" not in ("201906",) and month_expiry(y, m) <= END]
    lines += [tx_line("2019-06-24", "201906", 10050.0), tx_line("2020-03-31", "202004", 9000.0)]
    write_tx(tmp_path / "tx" / "tx_2019_01.csv", lines)
    # IV 對帳：造一份和這裡幾乎一樣的 vrp_study 檔（差 0.001）
    (tmp_path / "vrp").mkdir()
    import csv
    from core.vrp_study import IV_FIELDS
    with open(tmp_path / "vrp" / "iv_2019_09.csv", "w", encoding="utf-8", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=IV_FIELDS, lineterminator="\n")
        wr.writeheader()
        for d, v in sorted(w0.iv_by_date.items()):
            if d.startswith("2019-09"):
                wr.writerow({"date": d, "expiry": "201910", "expiry_date": "2019-10-16", "T_days": 20.0, "F": 10000.0, "K": 10000.0, "iv": round(v + 0.001, 4)})
    assert ob.main(["sanity", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "[main] 完成 13 個、丟棄 1 個" in out and "[dte10] 完成 14 個、丟棄 1 個" in out
    assert "201906 2019-06-19→2019-06-24" in out and "TX 補得起來的：1 個" in out and "補完後：主要規則完成 14 個、仍丟棄 0 個" in out
    assert "IV 對帳" in out and "共同 21 天" in out and "差的最大 0.001" in out and "進場腿收盤最後最佳買賣價的價差" in out
    j = tmp_path / "out.json"
    assert ob.main(["run", "--dir", str(tmp_path), "--json", str(j)]) == 0
    out = capsys.readouterr().out
    assert "週期 14 個" in out and "其中 1 個的到期日與最終結算價用 TX 最後成交價補" in out and "T7" in out and "敏感度" in out
    data = json.loads(j.read_text(encoding="utf-8"))
    assert data["main"]["describe"]["cycles"] == 14 and data["main"]["describe"]["rescued"] == 1 and data["secondary"]["n"] > 0 and data["dropped"] == 0
