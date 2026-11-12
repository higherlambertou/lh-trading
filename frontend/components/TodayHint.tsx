"use client";

import { useEffect, useState } from "react";
import { api, MarketState } from "@/lib/api";
import { STATE_TEXT } from "./MarketStatePanel";

// 下單面板裡的「今日判斷」一行：盤前算出來的結論（頁首那條的精簡版），放在送出鈕上面，下單前不用捲回頁首。
// 純提醒：不擋單、不改任何欄位；取不到就不顯示。
const COLOR: Record<string, string> = { TREND: "#00e676", REVERT: "#3b82f6", IV_HIGH: "#ffc107", IV_LOW: "#a855f7" };

export function hintText(st: MarketState): string {
  const act = (st.strategies ?? []).length ? st.strategies!.join("、") : "不操作";
  return `${STATE_TEXT[st.state ?? ""] ?? st.state ?? "—"} · 建議${act}${st.direction_label ? ` · 日 K ${st.direction_label}` : ""}`;
}

export default function TodayHint() {
  const [st, setSt] = useState<MarketState | null>(null);
  useEffect(() => {
    let alive = true;
    const load = async () => { try { const s = await api.market.state(); if (alive) setSt(s); } catch { /* 靜默 */ } };
    load();
    const id = setInterval(load, 60000);
    return () => { alive = false; clearInterval(id); };
  }, []);
  if (!st?.ready) return null;
  return (
    <div className="mb-3 flex items-baseline gap-2 text-[11px]">
      <span className="text-[#7070a0] whitespace-nowrap">今日判斷</span>
      <span style={{ color: COLOR[st.state ?? ""] ?? "#a0a0c0" }}>{hintText(st)}</span>
      <span className="text-[#404060] whitespace-nowrap">只提醒</span>
    </div>
  );
}
