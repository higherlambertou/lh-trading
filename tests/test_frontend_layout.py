"""手機版與下單動線（update.md 2026-11-10）：頁首行情條在窄螢幕要換行、交易分頁的面板順序、盤中即時在交易分頁只占一欄且手機預設收起。
靜態檢查（沒有 DOM 測試工具）；盤中即時面板的渲染在 tests/frontend/intraday_check.js。"""
from __future__ import annotations

import re
from pathlib import Path

FRONT = Path(__file__).resolve().parent.parent / "frontend"


def _read(name: str) -> str:
    return (FRONT / "components" / name).read_text(encoding="utf-8")


def test_quote_bar_wraps_and_hides_detail_on_narrow_screens():
    src = _read("QuoteBar.tsx")
    assert re.search(r'<div className="flex flex-wrap items-center gap-x-4 gap-y-1">', src)          # 整條可以換到下一行
    assert 'className="hidden sm:inline font-mono text-[10px] tabular-nums text-[#404060]">' in src   # 高低只在 ≥640px
    assert 'className="hidden md:inline text-[10px] text-[#404060] tabular-nums">' in src            # 時間只在 ≥768px
    assert src.count("whitespace-nowrap") == 0 or "flex-wrap" in src


def test_trade_tab_order_puts_positions_and_orders_before_the_research_panels():
    src = _read("DashboardTabs.tsx")
    trade = src[src.index('tab === "trade" ? "space-y-4"'):src.index('tab === "market"')]
    order = [m.group(1) for m in re.finditer(r"<(PriceChart|PositionPanel|TradesPanel|OrderPanel|LiveStatePanel|StrategyPanel|EntryLabelCard)\b", trade)]
    assert order == ["PriceChart", "PositionPanel", "TradesPanel", "OrderPanel", "LiveStatePanel", "StrategyPanel", "EntryLabelCard"], order
    assert "<LiveStatePanel compact />" in trade
    # 市場分頁用完整版（橫跨兩欄）
    market = src[src.index('tab === "market"'):src.index('tab === "intel"')]
    assert "<LiveStatePanel />" in market


def test_live_state_panel_compact_is_one_column_and_folds_on_phones():
    src = _read("LiveStatePanel.tsx")
    assert 'const [open, setOpen] = useState(false);' in src
    assert '${compact ? "" : "lg:col-span-2 "}' in src
    assert 'const cols = compact ? "grid grid-cols-1 sm:grid-cols-2 gap-3" : "grid grid-cols-1 md:grid-cols-3 gap-3";' in src
    assert '<div className={compact && !open ? "hidden lg:block space-y-4" : "space-y-4"}>' in src
    assert 'className="lg:hidden flex items-center gap-0.5' in src and "aria-expanded={open}" in src
    assert "<IntradayCards st={st} cols={cols} />" in src
    cards = _read("IntradayCards.tsx")
    assert 'cols = "grid grid-cols-1 md:grid-cols-3 gap-3"' in cards and "<div className={cols}>" in cards     # 預設不變


def test_market_state_table_hides_strategy_columns_when_no_strategy_ran():
    src = _read("MarketStatePanel.tsx")
    assert "const showScalp = rows.some((r) => r.scalp_on || r.trades > 0);" in src
    assert '...(showScalp ? ["scalp", "結果"] : [])' in src
    assert src.count("{showScalp && ") == 2 and "colSpan={cols.length}" in src
    assert 'placeholder={iv?.value != null ? `現在 ${iv.value.toFixed(1)}` : "例：18.5"}' in src
    assert "market_bias" not in src


def test_no_code_jargon_in_user_facing_text():
    core = (FRONT.parent / "core" / "daily_summary.py").read_text(encoding="utf-8")
    assert "market_bias=自動" not in core
