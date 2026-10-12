"""worker 的報價萃取（_extract_quote）：核心欄位不變、漲跌改讀 price_chg、五檔與交易所累計欄位是「附加」的。

背景（2026-10-12 夜盤用模擬盤實測）：Quote 事件本來每筆都帶五檔、交易所累計內外盤、avg_price、現貨指數，
worker 以前只轉發一部分；而 `change_price` 這個欄位在 shioaji 1.5.x 根本不存在（真正叫 price_chg），畫面的漲跌因此永遠是 0。
下面的兩筆事件是當時實測抓到的原樣（TMFJ6，一筆成交、一筆報價更新）。
"""
from decimal import Decimal
from types import SimpleNamespace

from core.shioaji_worker import _extract_quote

TRADE = dict(
    code="TMFJ6", open=49310.0, close=49290.0, high=49420.0, low=49280.0, avg_price=49353.320758, underlying_price=49301.96,
    price_chg=-55.0, pct_chg=-0.11146, volume=1, total_volume=16885, tick_type=2, simtrade=False,
    bid_side_total_vol=8198, ask_side_total_vol=8374, bid_side_total_cnt=12399, ask_side_total_cnt=11781,
    bid_price=[49290.0, 49289.0, 49288.0, 49287.0, 49286.0], ask_price=[49293.0, 49294.0, 49295.0, 49296.0, 49297.0],
    bid_volume=[5, 2, 7, 7, 9], ask_volume=[7, 6, 17, 14, 8], diff_bid_vol=[-1, 0, 0, 0, 0], diff_ask_vol=[0, 0, 0, 0, 0],
    ts=1_760_258_410_622_000_000)
UPDATE = dict(TRADE, volume=0, total_volume=16884, ask_side_total_vol=8373, diff_bid_vol=[-1, 0, 0, 0, 0])


def q(**kw):
    return SimpleNamespace(**kw)


CORE = {"code", "close", "open", "high", "low", "volume", "total_volume", "change_price", "tick_type", "simtrade", "ts"}


def test_real_trade_event_keeps_core_fields_and_adds_the_rest():
    d = _extract_quote(q(**TRADE))
    assert d["code"] == "TMFJ6" and d["close"] == 49290.0 and d["volume"] == 1 and d["total_volume"] == 16885
    assert d["tick_type"] == 2 and d["simtrade"] is False and d["high"] == 49420.0 and d["low"] == 49280.0
    assert abs(d["ts"] - 1_760_258_410.622) < 1e-6
    assert d["change_price"] == -55.0                                   # 以前永遠是 0
    assert d["avg_price"] == 49353.320758 and d["underlying_price"] == 49301.96
    assert d["bid_side_total_vol"] == 8198 and d["ask_side_total_vol"] == 8374
    assert d["book"] == {"bp": [49290.0, 49289.0, 49288.0, 49287.0, 49286.0], "ap": [49293.0, 49294.0, 49295.0, 49296.0, 49297.0],
                         "bv": [5, 2, 7, 7, 9], "av": [7, 6, 17, 14, 8], "dbv": -1, "dav": 0}


def test_non_trade_update_event_is_extracted_the_same_way():
    d = _extract_quote(q(**UPDATE))
    assert d["volume"] == 0 and d["ask_side_total_vol"] == 8373 and d["book"]["bv"] == [5, 2, 7, 7, 9]


def test_change_price_prefers_price_chg_then_falls_back():
    base = dict(code="TMFJ6", close=100.0)
    assert _extract_quote(q(**base, price_chg=-3.0, diff_price=9.0, change_price=7.0))["change_price"] == -3.0
    assert _extract_quote(q(**base, diff_price=9.0, change_price=7.0))["change_price"] == 9.0
    assert _extract_quote(q(**base, change_price=7.0))["change_price"] == 7.0          # 舊版 SDK
    assert _extract_quote(q(**base))["change_price"] == 0.0
    assert _extract_quote(q(**base, price_chg=0.0))["change_price"] == 0.0              # 平盤不是「沒有」


def test_quote_without_extra_fields_looks_exactly_like_before():
    d = _extract_quote(q(code="TMFJ6", close=100.0, open=99.0, high=101.0, low=98.0, volume=2, total_volume=10, tick_type=1, ts=None))
    assert set(d) == CORE and d["close"] == 100.0 and d["volume"] == 2


def test_invalid_close_or_code_still_returns_empty():
    assert _extract_quote(q(code="TMFJ6", close=None)) == {}
    assert _extract_quote(q(code="TMFJ6", close="abc")) == {}


def test_decimal_and_numeric_strings_are_converted_to_plain_python():
    d = _extract_quote(q(code="TMFJ6", close=Decimal("100.5"), avg_price=Decimal("100.25"), underlying_price="101.5",
                         bid_side_total_vol=Decimal("12"), ask_side_total_vol="13",
                         bid_price=[Decimal("100"), Decimal("99")], ask_price=[Decimal("101")], bid_volume=[3, 4], ask_volume=[Decimal("5")]))
    assert d["close"] == 100.5 and d["avg_price"] == 100.25 and d["underlying_price"] == 101.5
    assert d["bid_side_total_vol"] == 12 and d["ask_side_total_vol"] == 13
    assert d["book"]["bp"] == [100.0, 99.0] and all(type(x) is float for x in d["book"]["bp"])
    assert d["book"]["av"] == [5] and all(type(x) is int for x in d["book"]["bv"] + d["book"]["av"])
    assert d["book"]["dbv"] is None and d["book"]["dav"] is None          # 沒有 diff 欄位 → None，不是 0


def test_unusable_extra_fields_are_dropped_but_never_break_the_core():
    d = _extract_quote(q(code="TMFJ6", close=100.0, avg_price="n/a", underlying_price=0, bid_side_total_vol="abc", ask_side_total_vol=None,
                         bid_price=[100.0, None, 98.0], ask_price=[101.0], bid_volume=[1, 2], ask_volume=[1]))
    assert d["close"] == 100.0
    assert "avg_price" not in d and "underlying_price" not in d            # 0／亂碼不當成有值
    assert "bid_side_total_vol" not in d and "ask_side_total_vol" not in d
    assert "book" not in d                                                  # 買價有一檔是 None → 整個五檔放棄，不要錯位


def test_book_needs_all_four_columns():
    d = _extract_quote(q(code="TMFJ6", close=100.0, bid_price=[100.0], ask_price=[101.0], bid_volume=[1]))
    assert "book" not in d
    d = _extract_quote(q(code="TMFJ6", close=100.0, bid_price=[], ask_price=[101.0], bid_volume=[1], ask_volume=[1]))
    assert "book" not in d


class Hostile:
    """取五檔／累計欄位就丟例外的 SDK 物件（Rust 包裝物件取屬性可能丟任何事）。"""
    code, close, open, high, low = "TMFJ6", 100.0, 99.0, 101.0, 98.0
    volume, total_volume, tick_type, simtrade, ts = 1, 5, 1, False, None

    @property
    def bid_price(self):
        raise RuntimeError("boom")

    @property
    def avg_price(self):
        raise RuntimeError("boom")

    @property
    def price_chg(self):
        raise RuntimeError("boom")

    @property
    def bid_side_total_vol(self):
        raise RuntimeError("boom")


def test_hostile_attributes_never_take_down_the_core_fields():
    d = _extract_quote(Hostile())
    assert d["close"] == 100.0 and d["code"] == "TMFJ6" and d["volume"] == 1 and d["change_price"] == 0.0
    assert not ({"book", "avg_price", "bid_side_total_vol"} & set(d))


def test_book_lists_are_copies_not_views_of_the_sdk_object():
    src = q(**TRADE)
    d = _extract_quote(src)
    d["book"]["bp"][0] = -1.0
    assert src.bid_price[0] == 49290.0


def test_levels_are_truncated_to_five():
    six = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    d = _extract_quote(q(code="TMFJ6", close=100.0, bid_price=six, ask_price=six, bid_volume=[1] * 6, ask_volume=[2] * 6))
    assert d["book"]["bp"] == six[:5] and len(d["book"]["av"]) == 5


def test_zero_cumulative_volume_is_kept_but_zero_prices_are_not():
    d = _extract_quote(q(code="TMFJ6", close=100.0, bid_side_total_vol=0, ask_side_total_vol=0, avg_price=0.0))
    assert d["bid_side_total_vol"] == 0 and d["ask_side_total_vol"] == 0 and "avg_price" not in d      # 時段剛開始：累計 0 是合法值


def test_even_an_unexpected_failure_inside_the_extras_cannot_drop_the_quote(monkeypatch):
    import core.shioaji_worker as w
    monkeypatch.setattr(w, "_extract_book", lambda quote: (_ for _ in ()).throw(MemoryError("boom")))
    d = w._extract_quote(q(code="TMFJ6", close=100.0, volume=1))
    assert d["close"] == 100.0 and d["volume"] == 1 and "book" not in d
