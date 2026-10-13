"""選擇權歷史買賣價差（core/option_spread_study.py，update.md 發現 30 A）：區間邊界、雙邊報價的定義、中位數手算、分組。"""
from __future__ import annotations

import pytest

import core.option_history as oh
import core.option_spread_study as ss


def row(strike=10000.0, bid=100.0, ask=102.0, *, F=10000.0, session="G", kind="M", date="2024-03-04"):
    return {"date": date, "kind": kind, "expiry": "202403", "expiry_date": "2024-03-20", "session": session, "right": "C", "strike": strike,
            "F": F, "settle": None, "close": None, "bid": bid, "ask": ask, "volume": 1.0, "oi": 1.0}


def test_bucket_boundaries_are_inclusive_and_beyond_5_percent_is_dropped():
    F = 10000.0
    assert [ss.bucket_of(k, F) for k in (10000, 10050, 9950, 10051, 10200, 9800, 10201, 10500, 9500, 10501, 9499)] == [0, 0, 0, 1, 1, 1, 2, 2, 2, None, None]
    assert ss.bucket_of(10000.0, 0.0) is None


def test_two_sided_needs_a_positive_bid_and_an_ask_not_below_it():
    assert ss.two_sided(100.0, 102.0) and ss.two_sided(100.0, 100.0)               # 價差 0 也算雙邊
    for bid, ask in ((0.0, 5.0), (None, 5.0), (100.0, None), (50.0, 40.0), (None, None), (-1.0, 3.0)):
        assert not ss.two_sided(bid, ask)


def test_medians_by_hand_and_one_sided_rows_only_count_in_the_denominator():
    rows = [row(10000, 100, 102), row(10040, 200, 210), row(9960, 300, 300),         # 雙邊：價差 2、10、0 點
            row(10000, 0, 5), row(10000, 50, 40), row(10000, None, None)]            # 單邊、交叉、沒有
    (r,) = ss.summarize(ss.collect(rows))
    assert (r["session"], r["kind"], r["bucket"], r["year"]) == ("G", "M", "≤0.5%", 2024)
    assert r["n"] == 6 and r["two_sided"] == 3 and r["share"] == pytest.approx(0.5)
    assert r["pts"] == 2.0                                                          # median(2, 10, 0)
    assert r["pct"] == pytest.approx(2 / 101)                                        # median(2/101, 10/205, 0)
    assert 10 / 205 > 2 / 101 > 0


def test_pct_uses_the_mid_price_and_even_counts_average_the_middle_two():
    rows = [row(10000, 90, 110), row(10000, 40, 60)]                                 # 價差 20 / 中價 100＝20%；20 / 50＝40%
    (r,) = ss.summarize(ss.collect(rows))
    assert r["pts"] == 20.0 and r["pct"] == pytest.approx(0.30)


def test_cells_are_split_by_session_kind_bucket_and_year():
    rows = [row(10000, 100, 102), row(10000, 100, 104, session="A"), row(10000, 100, 106, kind="W"), row(10000, 100, 108, date="2023-03-06"),
            row(10100, 100, 110), row(10300, 100, 112)]
    s = {(r["session"], r["kind"], r["bucket"], r["year"]): r for r in ss.summarize(ss.collect(rows))}
    assert set(s) == {("G", "M", "≤0.5%", 2024), ("A", "M", "≤0.5%", 2024), ("G", "W", "≤0.5%", 2024), ("G", "M", "≤0.5%", 2023),
                      ("G", "M", "0.5~2%", 2024), ("G", "M", "2~5%", 2024)}
    assert s[("G", "M", "≤0.5%", 2024)]["pts"] == 2.0 and s[("A", "M", "≤0.5%", 2024)]["pts"] == 4.0 and s[("G", "W", "≤0.5%", 2024)]["pts"] == 6.0
    assert s[("G", "M", "≤0.5%", 2023)]["pts"] == 8.0 and s[("G", "M", "0.5~2%", 2024)]["pts"] == 10.0 and s[("G", "M", "2~5%", 2024)]["pts"] == 12.0


def test_a_cell_with_no_two_sided_quote_reports_none_not_zero():
    (r,) = ss.summarize(ss.collect([row(10000, 0, 5), row(10000, None, 7)]))
    assert r["n"] == 2 and r["two_sided"] == 0 and r["share"] == 0.0 and r["pts"] is None and r["pct"] is None


def test_report_has_a_table_per_session_and_kind_and_dashes_for_missing_cells():
    rows = [row(10000, 100, 102), row(10100, 100, 110, session="A"), row(10000, 100, 102, date="2023-03-06")]
    out = ss.format_report(ss.summarize(ss.collect(rows)))
    assert "■ 一般時段（日盤）・月選擇權" in out and "■ 盤後（夜盤）・月選擇權" in out and "≤0.5%" in out and "0.5~2%" in out
    assert "2023" in out and "2024" in out and "—" in out
    assert "  2.0% /   2.0 點 / 100% (n=1)" in out                                   # 2/101＝1.98% → 2.0%；價差 2 點；唯一一檔、雙邊
    assert ss.format_report([]).startswith("選擇權歷史買賣價差")


def test_run_reads_the_stored_chain_files(tmp_path):
    (tmp_path / "opt").mkdir()
    oh.write_rows(tmp_path / "opt" / "chain_2024_03.csv.gz", [row(10000, 100, 102), row(10100, 100, 110)])
    oh.write_rows(tmp_path / "opt" / "chain_2023_03.csv.gz", [row(10000, 100, 104, date="2023-03-06")])
    res = ss.run(tmp_path)
    assert {(r["year"], r["bucket"]): r["pts"] for r in res} == {(2023, "≤0.5%"): 4.0, (2024, "≤0.5%"): 2.0, (2024, "0.5~2%"): 10.0}
