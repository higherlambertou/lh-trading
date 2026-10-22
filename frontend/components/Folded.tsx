"use client";

import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

/** localStorage 的 key；讀寫失敗（私密視窗、被擋）一律當作沒記過。 */
export const foldKey = (id: string) => `fold:${id}`;

export function readFold(id: string, fallback: boolean): boolean {
  try {
    const v = localStorage.getItem(foldKey(id));
    return v === "open" ? true : v === "closed" ? false : fallback;
  } catch {
    return fallback;
  }
}

function writeFold(id: string, open: boolean): void {
  try {
    localStorage.setItem(foldKey(id), open ? "open" : "closed");
  } catch {
    /* 記不住就算了 */
  }
}

/**
 * 已經驗證過沒有用（或只在模擬盤用）的面板：預設收起，標出為什麼，按一下才展開；展開／收起會記在這台瀏覽器。
 * keepMounted＝收起時仍掛著、只用 CSS 藏起來（面板有正在跑的狀態或表單，不能被洗掉）；
 * 否則收起時不掛載（例如回放面板一掛載就讓後端算排列檢定，沒在看就不該算）。
 */
export default function Folded({ id, title, why, defaultOpen = false, keepMounted = false, children }: {
  id: string; title: string; why: string; defaultOpen?: boolean; keepMounted?: boolean; children: React.ReactNode;
}) {
  const [open, setOpen] = useState<boolean | null>(null);     // null＝還沒讀到記錄；先照預設
  useEffect(() => setOpen(readFold(id, defaultOpen)), [id, defaultOpen]);
  const shown = open ?? defaultOpen;
  const toggle = () => {
    writeFold(id, !shown);
    setOpen(!shown);
  };
  return (
    <>
      <button
        type="button"
        onClick={toggle}
        aria-expanded={shown}
        className="lg:col-span-2 w-full text-left flex items-start gap-2 px-4 py-2.5 rounded-xl border border-dashed border-[#1e1e3a] bg-[#101018] hover:border-[#3b82f6]/40 transition-colors"
      >
        {shown ? <ChevronDown size={14} className="mt-0.5 shrink-0 text-[#7070a0]" /> : <ChevronRight size={14} className="mt-0.5 shrink-0 text-[#7070a0]" />}
        <span className="min-w-0">
          <span className="text-xs font-semibold text-[#7070a0] tracking-widest">{title}</span>
          <span className="ml-2 text-[11px] text-[#ffc107]/80">僅供參考</span>
          <span className="block text-[11px] text-[#505070] leading-relaxed">{why}{shown ? "" : "（按一下展開）"}</span>
        </span>
      </button>
      {keepMounted ? <div className={shown ? "contents" : "hidden"}>{children}</div> : shown ? children : null}
    </>
  );
}
