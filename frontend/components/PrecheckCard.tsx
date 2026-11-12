"use client";

import { useEffect, useState } from "react";
import { api, apiErrorDetail } from "@/lib/api";
import { attention, eventLines, hitsLine, ivHigh, ivLine, nextDayLine, nightLine, rangeLine, showDayLine, summaryLine, type Precheck } from "@/lib/precheck";

// 下單前檢查（只顯示、不擋單；update.md 發現 60）：今天預期會走多遠、隱含波動高不高、今晚有沒有重大事件。
// 13:45 以後多了今晚夜盤與下一個日盤的預期振幅（發現 67）。
// 2026-11-12 起放在送出鈕下面、預設收成一行摘要（IV 百分位、事件、預期振幅），點開才是完整的幾行；開關記在這台瀏覽器。
const OPEN_KEY = "fold:precheck";

export function PrecheckBody({ p }: { p: Precheck }) {
  const night = nightLine(p), next = nextDayLine(p), hits = hitsLine(p);
  return (
    <div className="space-y-0.5 text-[11px] leading-snug">
      {showDayLine(p) && <div className="text-[#e0e0f0]">{rangeLine(p)}</div>}
      {night && <div className="text-[#e0e0f0]">🌙 {night}</div>}
      {next && <div className="text-[#a0a0c0]">{next}</div>}
      <div style={{ color: ivHigh(p) ? "#ffc107" : "#7070a0" }}>{ivLine(p)}</div>
      {eventLines(p).map((l, i) => <div key={i} style={{ color: p.events && p.events.length ? "#ffc107" : "#7070a0" }}>{p.events && p.events.length ? "📅 " : ""}{l}</div>)}
      {hits && <div className="help text-[#505070]">{hits}</div>}
    </div>
  );
}

export default function PrecheckCard() {
  const [p, setP] = useState<Precheck | null>(null);
  const [err, setErr] = useState("");
  const [open, setOpen] = useState(false);
  useEffect(() => {
    try { if (localStorage.getItem(OPEN_KEY) === "open") setOpen(true); } catch { /* 沒有 localStorage 就維持收起 */ }
  }, []);
  const toggle = () => setOpen((o) => { try { localStorage.setItem(OPEN_KEY, o ? "closed" : "open"); } catch { /* 忽略 */ } return !o; });
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
    <div className="rounded-lg border border-[#1e1e3a] px-3 py-2">
      <button type="button" onClick={toggle} aria-expanded={open} className="w-full text-left flex items-baseline gap-2 flex-wrap hover:text-[#e0e0f0]">
        <span className="text-[11px] text-[#7070a0] uppercase tracking-widest whitespace-nowrap">{open ? "▾" : "▸"} 下單前檢查</span>
        {!open && (
          <span className="text-[11px] leading-snug min-w-0" style={{ color: p && attention(p) ? "#ffc107" : "#a0a0c0" }}>
            {p ? summaryLine(p) : (err || "載入中…")}
          </span>
        )}
      </button>
      {open && (
        <div className="mt-1.5">
          {p ? <PrecheckBody p={p} /> : <div className="text-[11px] text-[#7070a0]">{err || "載入中…"}</div>}
          {p && err && <div className="text-[10px] text-[#ffc107]">更新失敗：{err}</div>}
        </div>
      )}
    </div>
  );
}
