"use client";

import { useEffect, useState } from "react";
import PositionPanel from "./PositionPanel";
import StrategyPanel from "./StrategyPanel";
import OrderPanel from "./OrderPanel";
import TradesPanel from "./TradesPanel";
import MarketStrip from "./MarketStrip";
import MarketStatePanel from "./MarketStatePanel";
import LiveStatePanel from "./LiveStatePanel";
import RiskPanel from "./RiskPanel";
import IndicatorReplayPanel from "./IndicatorReplayPanel";

type Tab = "trade" | "market" | "analysis";

const TABS: { id: Tab; label: string; hint: string }[] = [
  { id: "trade", label: "交易", hint: "部位、策略、委託、成交" },
  { id: "market", label: "市場", hint: "盤前判斷、盤中買賣力道" },
  { id: "analysis", label: "分析", hint: "破產機率驗證、市場指標回放（只讀歷史資料，不影響交易）" },
];

const isTab = (x: string): x is Tab => TABS.some((t) => t.id === x);
const GRID = "grid grid-cols-1 lg:grid-cols-2 gap-4";

/**
 * 三個分頁，用網址的 # 記住在哪一頁（可加書籤；切換正式盤／模擬盤會 reload，也會留在同一頁）。
 *
 * 掛載策略（為什麼不是三頁都直接掛著）：
 *  - 交易：永遠掛著，只用 CSS 隱藏。委託面板有表單輸入與「3 秒內再點一次」的確認狀態，切分頁不能把它洗掉。
 *  - 市場：只在目前這一頁時掛載。兩個面板各自在輪詢（3 秒／15 秒），看不到時沒必要打後端。
 *  - 分析：第一次點進去才掛載，之後保留（隱藏）。風險與回放載入時會讓後端算一次蒙地卡羅／排列檢定，
 *    沒在看就不該算；而且它們不輪詢，保留下來可以留住你填的參數與結果。
 */
export default function DashboardTabs() {
  const [tab, setTab] = useState<Tab | null>(null);          // null＝還沒讀到網址；先不掛載，避免先掛錯的分頁再切換
  const [analysisOpened, setAnalysisOpened] = useState(false);

  useEffect(() => {
    const read = () => {
      const h = window.location.hash.slice(1);
      setTab(isTab(h) ? h : "trade");
    };
    read();
    window.addEventListener("hashchange", read);
    return () => window.removeEventListener("hashchange", read);
  }, []);

  useEffect(() => {
    if (tab === "analysis") setAnalysisOpened(true);
  }, [tab]);

  return (
    <>
      <nav role="tablist" aria-label="儀表板分頁" className="px-4 max-w-[1400px] mx-auto flex gap-1 border-b border-[#1e1e3a]">
        {TABS.map((t) => (
          <a
            key={t.id}
            href={`#${t.id}`}
            role="tab"
            aria-selected={tab === t.id}
            title={t.hint}
            className={`px-4 py-2 text-sm -mb-px border-b-2 transition-colors ${
              tab === t.id
                ? "border-[#3b82f6] text-[#e0e0f0] font-semibold"
                : "border-transparent text-[#7070a0] hover:text-[#e0e0f0]"
            }`}
          >{t.label}</a>
        ))}
      </nav>

      <main className="p-4 max-w-[1400px] mx-auto">
        {tab && (
          <>
            <div className={tab === "trade" ? "space-y-4" : "hidden"}>
              <MarketStrip />
              <div className={GRID}>
                <PositionPanel />
                <StrategyPanel />
                <OrderPanel />
                <TradesPanel />
              </div>
            </div>

            {tab === "market" && (
              <div className={GRID}>
                <MarketStatePanel />
                <LiveStatePanel />
              </div>
            )}

            {(tab === "analysis" || analysisOpened) && (
              <div className={tab === "analysis" ? GRID : "hidden"}>
                <RiskPanel />
                <IndicatorReplayPanel />
              </div>
            )}
          </>
        )}
      </main>
    </>
  );
}
