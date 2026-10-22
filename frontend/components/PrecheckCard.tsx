"use client";

import { useEffect, useState } from "react";
import { api, apiErrorDetail } from "@/lib/api";
import { eventLines, ivHigh, ivLine, nextDayLine, nightLine, rangeLine, showDayLine, type Precheck } from "@/lib/precheck";

// 下單前檢查（只顯示、不擋單；update.md 發現 60）：今天預期會走多遠、隱含波動高不高、今晚有沒有重大事件——集中在下單面板最上面。
// 13:45 以後多了今晚夜盤與下一個日盤的預期振幅（發現 67）。
export function PrecheckBody({ p }: { p: Precheck }) {
  const night = nightLine(p), next = nextDayLine(p);
  return (
    <div className="space-y-0.5 text-[11px] leading-snug">
      {showDayLine(p) && <div className="text-[#e0e0f0]">{rangeLine(p)}</div>}
      {night && <div className="text-[#e0e0f0]">🌙 {night}</div>}
      {next && <div className="text-[#a0a0c0]">{next}</div>}
      <div style={{ color: ivHigh(p) ? "#ffc107" : "#7070a0" }}>{ivLine(p)}</div>
      {eventLines(p).map((l, i) => <div key={i} style={{ color: p.events && p.events.length ? "#ffc107" : "#7070a0" }}>{p.events && p.events.length ? "📅 " : ""}{l}</div>)}
    </div>
  );
}

export default function PrecheckCard() {
  const [p, setP] = useState<Precheck | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try { const r = await api.market.precheck(); if (alive) { setP(r); setErr(""); } }
      catch (e) { if (alive) setErr(apiErrorDetail(e).trim() === "Not Found" ? "後端還沒有這個 API（要重啟後端才有）" : apiErrorDetail(e)); }
    };
    load();
    const id = setInterval(load, 60000);
    return () => { alive = false; clearInterval(id); };
  }, []);
  return (
    <div className="mb-3 rounded-lg border border-[#1e1e3a] px-3 py-2">
      <div className="text-[11px] text-[#7070a0] uppercase tracking-widest mb-1">下單前檢查（只提醒）</div>
      {p ? <PrecheckBody p={p} /> : <div className="text-[11px] text-[#7070a0]">{err || "載入中…"}</div>}
      {p && err && <div className="text-[10px] text-[#ffc107]">更新失敗：{err}</div>}
    </div>
  );
}
