import { Activity } from "lucide-react";
import ModeToggle from "@/components/ModeToggle";
import UsageIndicator from "@/components/UsageIndicator";
import StatusDot from "@/components/StatusDot";
import QuoteBar from "@/components/QuoteBar";
import DashboardTabs from "@/components/DashboardTabs";

export default function Page() {
  return (
    <div className="min-h-screen">
      {/* ── Header：左＝名稱與行情條；右＝模式、流量、連線狀態（窄螢幕會自動換行）──────── */}
      <header className="px-6 pt-3 pb-2 flex items-center gap-x-3 gap-y-2 flex-wrap">
        <Activity size={18} className="text-[#3b82f6]" />
        <span className="font-semibold tracking-wide">LH Trading</span>
        <span className="text-xs text-[#7070a0]">台指期貨</span>
        <QuoteBar />
        <div className="ml-auto flex items-center gap-3">
          <ModeToggle />
          <UsageIndicator />
          <StatusDot />
        </div>
      </header>

      {/* ── 分頁：交易／市場／分析（見 DashboardTabs）──────────────────── */}
      <DashboardTabs />
    </div>
  );
}
