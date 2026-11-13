"""手機版與下單動線（update.md 2026-11-10）：頁首行情條在窄螢幕要換行、交易分頁的面板順序、盤中即時在交易分頁只占一欄且手機預設收起。
2026-11-12 的直覺性修正：下單表單不被參考卡切開、方向不預選、送出鈕貼底、空手精簡、說明開關、表格可橫捲、夜盤的日盤振幅標「已收」。
靜態檢查（沒有 DOM 測試工具）；渲染的部分在 tests/frontend/（daytrade_check、protection_check、ui_check、stop_suggest_check、size_check、precheck_check）。"""
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


# ── 2026-11-12：下單面板 ───────────────────────────────────────────────

def test_order_panel_form_is_not_split_by_reference_cards_and_has_no_default_side():
    src = _read("OrderPanel.tsx")
    assert 'const [action, setAction] = useState<Action | null>(null);' in src                 # 方向預設不選
    assert "const noSide = action == null;" in src and "busy || noSide ||" in src                 # 沒選就不能送
    assert "if (!action) return;\n    if (needsArm()) return;" in src                               # 期貨送單前再擋一次
    assert 'if (!action || !deliveryMonth || strike === "" || !limitPrice) return;' in src        # 選擇權也是
    # 順序：方向鈕 → 欄位 → 口數提示 → 今日判斷 → 送出鈕 → 下單前檢查 → 建議停損停利
    idx = [src.index(k) for k in ("{/* 買 / 賣 */}", "{/* 欄位 */}", "<SizeHint ", "<TodayHint />", "{/* 送出", "<PrecheckCard />", "<StopSuggestCard ")]
    assert idx == sorted(idx), idx
    assert src.count("<PrecheckCard />") == 1 and src.count("<StopSuggestCard ") == 1
    # 不用 sticky：貼底的鈕在卡片剛捲進畫面時會蓋在標題與欄位上（試過、截圖看得到）
    assert 'className="sticky' not in src and "sticky bottom" not in src.split("{/* 送出")[1].split("</div>")[0].split("*/}")[1]


def test_today_hint_is_display_only():
    src = _read("TodayHint.tsx")
    for bad in ("placeOrder", "api.order", "fetch(", 'method: "POST"', "setStopLoss", "setAction"):
        assert bad not in src, bad
    assert "api.market.state()" in src and "只提醒" in src


# ── 2026-11-12：空手精簡、兩欄不互撐、行情順序 ─────────────────────────

def test_position_and_trades_panels_are_compact_when_flat():
    pos = _read("PositionPanel.tsx")
    assert "margin && positions.length === 0 && posAge >= 0 && posAge <= posLimit(cadence) && (" in pos       # 只有「確定空手」才縮成一行
    assert "目前無持倉</span>" in pos and "今日已實現 <Num value={totalRealizedPnl} />" in pos
    assert 'className="grid grid-cols-2 sm:grid-cols-3 gap-2 mb-4"' in pos                                  # 五格排三欄，不會剩一格孤單
    trades = _read("TradesPanel.tsx")
    assert '{watches.length === 0 && <span className="text-xs text-[#404060]">無監控中的部位</span>}' in trades
    assert '{trades.length === 0 && <span className="text-xs text-[#404060]">今日無委託紀錄</span>}' in trades
    assert "text-center py-3" not in trades                                                                   # 不再是置中一大塊
    tabs = _read("DashboardTabs.tsx")
    assert "<div className={`${GRID} lg:items-start`}>" in tabs                                               # 交易分頁兩欄各自照內容高度


def test_quote_bar_keeps_a_fixed_order():
    src = _read("QuoteBar.tsx")
    assert 'const CODE_ORDER = ["TMF", "TXF", "MXF"];' in src
    assert ".sort((a, b) => codeRank(a) - codeRank(b))" in src


# ── 2026-11-12：說明開關、表格可橫捲、夜盤標示、手機版的短說明 ─────────────

HELP_FILES = ["PriceChart.tsx", "LiveStatePanel.tsx", "IntradayCards.tsx", "StopSuggestCard.tsx", "RangeSection.tsx", "SizeHint.tsx", "PrecheckCard.tsx",
              "EntryLabelCard.tsx", "ScenarioCard.tsx", "MarketStatePanel.tsx", "PremarketPanel.tsx", "IntelPanel.tsx", "RiskPanel.tsx", "BacktestPanel.tsx"]


def test_help_toggle_hides_explanations_everywhere():
    css = (FRONT / "app" / "globals.css").read_text(encoding="utf-8")
    assert 'html:not([data-help="on"]) .help { display: none !important; }' in css
    layout = (FRONT / "app" / "layout.tsx").read_text(encoding="utf-8")
    assert "localStorage.getItem('help:on')==='1'" in layout and 'suppressHydrationWarning' in layout
    assert "<HelpToggle />" in _read("DashboardTabs.tsx")
    for name in HELP_FILES:
        src = _read(name)
        assert re.search(r'className=(?:"help[ "]|\{`help )', src), name                                      # 每個有說明段落的面板至少收了一段
    # 收進說明的只能是說明：數字、警告、錯誤不能被藏起來
    for name in HELP_FILES:
        for m in re.finditer(r'className="help[^"]*"[^>]*>([^<{]*)', _read(name)):
            assert "更新失敗" not in m.group(1) and "⚠" not in m.group(1), (name, m.group(1))


def test_tables_scroll_with_a_hint_on_phones():
    css = (FRONT / "app" / "globals.css").read_text(encoding="utf-8")
    assert ".scroll-x {" in css and "background-attachment: local, local, scroll, scroll;" in css
    for name, n in (("MarketStatePanel.tsx", 1), ("IntelPanel.tsx", 4), ("PremarketPanel.tsx", 2), ("TradesPanel.tsx", 1), ("PositionPanel.tsx", 1)):
        assert _read(name).count('className="scroll-x"') == n, (name, _read(name).count('className="scroll-x"'))
    msp = _read("MarketStatePanel.tsx")
    assert 'const NOTE_COLS = ["判斷依據", "備註"];' in msp and msp.count('"hidden sm:table-cell') == 2             # 兩個輸入欄在窄螢幕藏起來（表頭與儲存格）
    assert "const [expanded, setExpanded] = useState<string | null>(null);" in msp and '<tr className="sm:hidden">' in msp   # 點一列才展開
    intel = _read("IntelPanel.tsx")
    assert intel.count("hidden sm:table-cell") == 2 and '<div className="sm:hidden text-[10px] font-mono"' in intel   # 日期放到名稱下面


def test_live_state_marks_day_range_as_closed_at_night_and_chart_caption_is_short_on_phones():
    live = _read("LiveStatePanel.tsx")
    assert '{st.in_session ? "日盤振幅" : "今日日盤振幅（已收）"}' in live
    assert "<Chip color={st.in_session ? rangeColor : MUTED} text={st.range_label} />" in live
    chart = _read("PriceChart.tsx")
    assert '<span className="sm:hidden">{captionShort(profile)}</span><span className="hidden sm:inline">{captionText(profile)}</span>' in chart
    assert "收 ${fmt(last.c)}  開 ${fmt(last.o)}" in chart and "收 ${fmt(d.close)}  開 ${fmt(d.open)}" in chart        # 收盤放前面，窄螢幕截掉的是量
    prof = (FRONT / "lib" / "volumeProfile.ts").read_text(encoding="utf-8")
    assert "export function captionShort(" in prof


def test_position_panel_shows_the_position_rows_before_the_numbers():
    """2026-11-13：持倉列在五格數字與已實現明細的上面（有部位時最先要看的是部位本身）；渲染順序在 protection_check.js。"""
    src = _read("PositionPanel.tsx")
    idx = [src.index(k) for k in ("{/* 空手（部位資料是新的", "{/* 持倉列：有部位時最先看到", "{/* Margin grid", "<ScenarioCard ", "{/* Realized PnL detail */}")]
    assert idx == sorted(idx), idx
    assert '<div className="space-y-2 mb-4">' in src
