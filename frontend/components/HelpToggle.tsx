"use client";

import { useEffect, useState } from "react";
import { Info } from "lucide-react";

export const HELP_KEY = "help:on";

/** 現在說明文字是開著的嗎（看 <html data-help>；app/layout.tsx 在載入時從 localStorage 設好）。 */
export function helpOn(): boolean {
  return typeof document !== "undefined" && document.documentElement.dataset.help === "on";
}

export function setHelp(on: boolean): void {
  if (on) document.documentElement.dataset.help = "on";
  else delete document.documentElement.dataset.help;
  try { localStorage.setItem(HELP_KEY, on ? "1" : "0"); } catch { /* 記不住就只管這一次 */ }
}

/**
 * 全站說明文字的開關：每張卡下面那些方法、免責、資料來源的灰字（class="help"）預設收起，
 * 看過就不用每次都看；想知道怎麼算的再打開。只是顯示／隱藏，不動任何資料。
 */
export default function HelpToggle() {
  const [on, setOn] = useState(false);
  useEffect(() => setOn(helpOn()), []);
  const toggle = () => { const next = !on; setHelp(next); setOn(next); };
  return (
    <button type="button" onClick={toggle} aria-pressed={on} title="顯示／隱藏各卡片下面的說明文字（怎麼算、資料來源、限制）"
      className={`ml-auto self-center flex items-center gap-1 px-2 py-1 rounded border text-[11px] transition-colors ${
        on ? "border-[#3b82f6]/60 text-[#93c5fd] bg-[#3b82f6]/10" : "border-[#1e1e3a] text-[#7070a0] hover:text-[#e0e0f0]"}`}>
      <Info size={12} />
      說明{on ? "：開" : "：關"}
    </button>
  );
}
