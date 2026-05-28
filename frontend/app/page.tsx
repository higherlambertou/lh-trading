import { Activity } from "lucide-react";
import StrategyPanel from "@/components/StrategyPanel";
import PositionPanel from "@/components/PositionPanel";
import OrderPanel from "@/components/OrderPanel";
import TradesPanel from "@/components/TradesPanel";

export default function Page() {
  return (
    <div className="min-h-screen">
      {/* ── Header ─────────────────────────────────────────────── */}
      <header className="border-b border-[#1e1e3a] px-6 py-3 flex items-center gap-3">
        <Activity size={18} className="text-[#3b82f6]" />
        <span className="font-semibold tracking-wide">LH Trading</span>
        <span className="text-xs text-[#7070a0]">台指期貨</span>
        <span className="ml-auto text-xs font-mono text-[#ffc107] bg-[#ffc107]/10 px-2 py-0.5 rounded border border-[#ffc107]/20">
          SIMULATION
        </span>
      </header>

      {/* ── 2×2 Grid ───────────────────────────────────────────── */}
      <main className="p-4 grid grid-cols-1 lg:grid-cols-2 gap-4 max-w-[1400px] mx-auto">
        <StrategyPanel />
        <PositionPanel />
        <OrderPanel />
        <TradesPanel />
      </main>
    </div>
  );
}
