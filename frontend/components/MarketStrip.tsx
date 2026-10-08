"use client";

import { useEffect, useState } from "react";
import { api, MarketState } from "@/lib/api";
import { Chip, STATE_TEXT } from "./MarketStatePanel";

// 交易頁頂端的精簡「今日判斷」：市場狀態面板搬到「市場」分頁之後，交易時仍看得到今天盤前的結論。
// 純顯示；詳細（Hurst、IV、日誌、手動輸入 IV）在「市場」分頁。取不到資料就靜默，不影響交易面板。
export default function MarketStrip() {
  const [st, setSt] = useState<MarketState | null>(null);

  useEffect(() => {
    const load = async () => {
      try { setSt(await api.market.state()); } catch { /* 靜默 */ }
    };
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, []);

  const today = new Date().toLocaleDateString("sv-SE");        // YYYY-MM-DD（當地日期）
  const h = st?.hurst, iv = st?.iv;

  return (
    <div className="flex items-center gap-x-3 gap-y-1 flex-wrap bg-[#141420] rounded-xl border border-[#1e1e3a] px-4 py-2.5 text-[11px]">
      <span className="text-[10px] text-[#7070a0] uppercase tracking-widest">今日判斷</span>
      {!st ? (
        <span className="text-[#404060]">載入中…</span>
      ) : !st.ready ? (
        <span className="text-[#7070a0]">尚未產生（每個交易日盤前自動計算）</span>
      ) : (
        <>
          <Chip code={st.state} text={STATE_TEXT[st.state ?? ""] ?? st.state ?? "—"} />
          <span className="text-[#7070a0]">{h?.label} + IV {iv?.label}</span>
          <span className="text-[#e0e0f0]">建議：{(st.strategies ?? []).length ? st.strategies!.join("、") : "不操作"}</span>
          <span className="text-[#7070a0]">日 K {st.direction_label}</span>
          {st.date && st.date !== today && <span className="text-[#ffc107]">非今日（{st.date}）</span>}
        </>
      )}
      <a href="#market" className="ml-auto text-[#3b82f6] hover:underline">詳情 →</a>
    </div>
  );
}
