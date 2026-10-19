"use client";

import { useEffect, useState } from "react";
import { api, apiErrorDetail } from "@/lib/api";
import { friendlySizeError, lotsLine, openRiskLine, perLotLine, qtyLine, warnLines, REASON_TEXT, type Size } from "@/lib/size";

// 口數提示（只顯示，不擋單；update.md 發現 51）：依你填的停損點數與單筆風險上限算「最多幾口」，並扣掉手動停損監控裡已佔用的風險。
// 口數仍是你自己填；這裡只是讓你在按下送出前看到「這樣的風險是權益的幾 %」。
export function SizeBody({ s }: { s: Size }) {
  const over = s.qty_over === true;
  const noLots = s.lots == null || s.lots < 1;
  return (
    <div className="space-y-0.5">
      <div className={`text-xs ${s.lots == null ? "text-[#7070a0]" : noLots ? "text-[#ff1744]" : "text-[#e0e0f0]"}`}>{lotsLine(s)}</div>
      {perLotLine(s) && <div className="text-[10px] text-[#7070a0]">{perLotLine(s)}</div>}
      {qtyLine(s) && <div className={`text-[11px] ${over ? "text-[#ffc107]" : "text-[#7070a0]"}`}>{over ? "⚠ " : ""}{qtyLine(s)}</div>}
      {s.lots != null && <div className="text-[10px] text-[#7070a0]">{openRiskLine(s)}</div>}
      {warnLines(s).map((w) => <div key={w} className="text-[10px] text-[#ffc107]">{w}</div>)}
    </div>
  );
}

export default function SizeHint({ contract, stopLoss, quantity }: { contract: string; stopLoss: string; quantity: number }) {
  const [data, setData] = useState<Size | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const stop = Number(stopLoss);
  const has = stopLoss !== "" && Number.isFinite(stop) && stop > 0;

  useEffect(() => {
    if (!has) { setData(null); setErr(null); return; }
    let cancelled = false;
    const load = async () => {
      try {
        const r = await api.risk.size(contract, stop, quantity >= 1 ? Math.floor(quantity) : undefined);
        if (!cancelled) { setData(r); setErr(null); }
      } catch (e) { if (!cancelled) setErr(friendlySizeError(apiErrorDetail(e))); }
    };
    const t = setTimeout(load, 400);                       // 打字時別每個字都問
    const id = setInterval(load, 15000);                   // 權益與已佔用風險會變；只讀後端記憶體
    return () => { cancelled = true; clearTimeout(t); clearInterval(id); };
  }, [has, contract, stop, quantity]);

  return (
    <div className="mb-3 rounded-lg border border-[#1e1e3a] px-3 py-2">
      <div className="text-[11px] text-[#7070a0] uppercase tracking-widest mb-1">口數提示（只提醒，不擋單）</div>
      {!has && <div className="text-[11px] text-[#7070a0]">{REASON_TEXT.stop_missing}</div>}
      {has && err && !data && <div className="text-xs text-[#ffc107]">{err}</div>}
      {has && data && <SizeBody s={data} />}
      {has && err && data && <div className="text-[10px] text-[#ffc107]">更新失敗：{err}</div>}
      <div className="mt-1 text-[10px] leading-snug text-[#404060]">風險＝到停損的距離（含滑價緩衝）；假設停損會成交，跳空或極端行情可能更差。</div>
    </div>
  );
}
