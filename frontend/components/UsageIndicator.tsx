"use client";

import { useState, useCallback } from "react";
import { Gauge, RefreshCw } from "lucide-react";
import { api, Usage } from "@/lib/api";

function fmtBytes(n: number): string {
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(2)} GB`;
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${n} B`;
}

export default function UsageIndicator() {
  const [usage, setUsage] = useState<Usage | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setUsage(await api.position.usage());
      setErr(false);
    } catch {
      setErr(true);
    } finally {
      setBusy(false);
    }
  }, []);

  // 顏色：< 70% 綠、70~90% 黃、> 90% 紅
  const pct = usage?.percent ?? 0;
  const color = pct >= 90 ? "#ff1744" : pct >= 70 ? "#ffc107" : "#00e676";

  return (
    <button
      onClick={load}
      disabled={busy}
      className="flex items-center gap-2 text-xs px-2 py-1 rounded-lg border border-[#1e1e3a] hover:border-[#3b82f6]/50 transition-colors disabled:opacity-50"
      title={
        usage
          ? `已用 ${fmtBytes(usage.used_bytes)} / ${fmtBytes(usage.limit_bytes)}　剩餘 ${fmtBytes(
              usage.remaining_bytes
            )}　連線數 ${usage.connections}/5　（額度開盤日 08:00 重置）　點擊重新查詢`
          : "點擊查詢流量用量"
      }
    >
      <Gauge size={14} style={{ color: usage ? color : "#7070a0" }} />
      <span className="text-[#7070a0]">流量</span>

      {usage && !err ? (
        <>
          {/* 進度條 */}
          <div className="w-24 h-1.5 rounded-full bg-[#1e1e3a] overflow-hidden">
            <div
              className="h-full rounded-full transition-all"
              style={{ width: `${Math.min(pct, 100)}%`, backgroundColor: color }}
            />
          </div>
          <span className="font-mono" style={{ color }}>
            {pct.toFixed(0)}%
          </span>
          <span className="text-[#404060] font-mono hidden sm:inline">
            {fmtBytes(usage.used_bytes)} / {fmtBytes(usage.limit_bytes)}
          </span>
          <span
            className="font-mono"
            style={{ color: usage.connections >= 4 ? "#ff1744" : "#404060" }}
          >
            連線 {usage.connections}/5
          </span>
        </>
      ) : (
        <span className="text-[#404060]">{err ? "查詢失敗" : "點此查詢"}</span>
      )}

      <RefreshCw size={12} className={`text-[#7070a0] ${busy ? "animate-spin" : ""}`} />
    </button>
  );
}
