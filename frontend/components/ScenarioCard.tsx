"use client";

import { useState } from "react";
import { pointValue, type Margin, type Position } from "@/lib/api";
import { exposure, futures, hasOthers, rows, summaryLines, signed, pctText, money, FOOT_TEXT, OPTION_NOTE, STATUS_TEXT, type Status } from "@/lib/scenario";

// 持倉情境表（只顯示、不擋單）：現有期貨持倉在價格漲跌時的損益、權益、離追繳多遠。一行摘要常駐，點「展開」看整張表。
const STATUS_COLOR: Record<Status, string> = { ok: "text-[#7070a0]", near: "text-[#ffc107]", call: "text-[#ff1744]", zero: "text-[#ff1744] font-semibold" };

export default function ScenarioCard({ positions, last, margin, stale = false }: { positions: Position[]; last: Record<string, number>; margin: Margin | null; stale?: boolean }) {
  const [open, setOpen] = useState(false);
  const lines = summaryLines(positions, margin);
  if (!lines || !margin) return null;
  const exp = exposure(positions);
  const eq = margin.equity, mt = margin.maintenance_margin;
  const fs = futures(positions);
  const big = [...fs].sort((a, b) => b.quantity * pointValue(b.code) - a.quantity * pointValue(a.code))[0];          // 每點金額最大的那筆
  const ref = big ? (last[big.code] ?? big.last_price) : null;               // 價位欄用最大那筆持倉的現價
  const table = exp !== 0 && eq > 0 ? rows(exp, eq, mt) : [];
  return (
    <div className="mt-4 rounded-lg border border-[#1e1e3a] px-3 py-2" data-testid="scenario">
      <div className="flex items-center gap-2">
        <div className="text-[11px] text-[#7070a0] uppercase tracking-widest">情境表（只提醒，不擋單）</div>
        {table.length > 0 && (
          <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open} className="ml-auto text-[11px] text-[#7070a0] hover:text-[#e0e0f0]">{open ? "收起" : "展開"}</button>
        )}
      </div>
      <div className="mt-1 space-y-0.5">
        {lines.map((l, i) => <div key={i} className={`text-xs ${i === 0 ? "text-[#e0e0f0]" : "text-[#a0a0c0]"}`}>{l}</div>)}
      </div>
      {stale && <div className="mt-1 text-[10px] text-[#ffc107]">權益資料不是最新的，數字可能有落差。</div>}
      {open && table.length > 0 && (
        <div className="mt-2 overflow-x-auto">
          <table className="w-full text-[11px] font-mono">
            <thead>
              <tr className="text-[#404060] border-b border-[#1e1e3a]">
                <th className="text-left pb-1 font-normal">漲跌</th>
                {ref != null && ref > 0 && <th className="text-right pb-1 font-normal">{big?.code}</th>}
                <th className="text-right pb-1 font-normal">損益</th>
                <th className="text-right pb-1 font-normal">權益</th>
                <th className="text-right pb-1 font-normal"> </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#0d0d14]">
              {table.map((r) => (
                <tr key={r.move}>
                  <td className="py-1 text-[#e0e0f0]">{signed(r.move)}</td>
                  {ref != null && ref > 0 && <td className="text-right text-[#7070a0]">{money(ref + r.move)}</td>}
                  <td className={`text-right ${r.pnl > 0 ? "text-[#00e676]" : "text-[#ff1744]"}`}>{signed(r.pnl)}<span className="text-[#404060]"> {pctText(r.pct)}</span></td>
                  <td className="text-right text-[#e0e0f0]">{money(r.equity)}</td>
                  <td className={`text-right ${STATUS_COLOR[r.status]}`}>{STATUS_TEXT[r.status]}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="help mt-1 text-[10px] leading-snug text-[#404060]">{FOOT_TEXT}{hasOthers(positions) ? OPTION_NOTE : ""}</div>
    </div>
  );
}
