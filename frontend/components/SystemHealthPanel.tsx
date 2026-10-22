"use client";

import { useEffect, useState } from "react";
import { Activity } from "lucide-react";
import { api, apiErrorDetail } from "@/lib/api";
import { STATUS_COLOR, STATUS_TEXT, summaryLine, type SystemHealth } from "@/lib/systemHealth";

// 系統健康（只顯示；update.md 發現 66）：券商連線、自動重啟、備份與雲端副本、期交所資料更新、lh-intel、磁碟與 log——以前要問人或下指令才看得到。
export function SystemHealthBody({ h }: { h: SystemHealth }) {
  return (
    <div className="space-y-1.5">
      <div className="text-xs" style={{ color: STATUS_COLOR[h.overall] }}>{summaryLine(h)}</div>
      {h.items.map((i) => (
        <div key={i.key} className="flex items-start gap-2 text-[11px] leading-snug" title={i.detail ?? undefined}>
          <span className="mt-1 inline-block w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: STATUS_COLOR[i.status] }} aria-label={STATUS_TEXT[i.status]} />
          <span className="min-w-0">
            <span className="text-[#a0a0c0]">{i.title}</span>
            <span className="text-[#e0e0f0]">　{i.text}</span>
          </span>
        </div>
      ))}
    </div>
  );
}

export default function SystemHealthPanel() {
  const [h, setH] = useState<SystemHealth | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try { const r = await api.system.health(); if (alive) { setH(r); setErr(""); } }
      catch (e) { if (alive) setErr(apiErrorDetail(e).trim() === "Not Found" ? "後端還沒有這個 API（要重啟後端才有）" : apiErrorDetail(e)); }
    };
    load();
    const id = setInterval(load, 60000);
    return () => { alive = false; clearInterval(id); };
  }, []);
  return (
    <div className="lg:col-span-2 bg-[#141420] rounded-xl border border-[#1e1e3a] p-5 space-y-3">
      <div className="flex items-center gap-2">
        <Activity size={14} className="text-[#00e676]" />
        <h2 className="text-xs font-semibold text-[#7070a0] uppercase tracking-widest">系統健康</h2>
        <span className="ml-auto text-[10px] text-[#404060]">{err ? "更新失敗" : "每分鐘更新・滑過去看細節"}</span>
      </div>
      {h ? <SystemHealthBody h={h} /> : <div className="text-[11px] text-[#7070a0]">{err || "載入中…"}</div>}
      {h && err && <div className="text-[10px] text-[#ffc107]">更新失敗：{err}</div>}
    </div>
  );
}
