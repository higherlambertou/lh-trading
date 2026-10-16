"use client";

import { useCallback, useEffect, useState } from "react";
import { api, apiErrorDetail, ORDER_PLACED_EVENT } from "@/lib/api";
import {
  NOTE_MAX, TREND_OPTIONS, flowText, friendlyLabelError, hhmm, manualRows, statusText, trendText,
  type EntryRow, type Trend,
} from "@/lib/entryLabel";

// 手動單的趨勢判斷標記（update.md 發現 47）：進場時憑直覺填「多／空／盤整／無明確判斷」，之後才能回頭檢驗直覺準不準。
// 不擋、不延遲任何下單；可以進場後任何時候補填，但出場之後才補的會被標成「事後」（結果已知，可能被事後記憶影響）。
// 沒有任何策略或風控讀這個欄位；只新增紀錄、不改委託。

export function LabelRow({ row, busy, onPick }: { row: EntryRow; busy: boolean; onPick: (trend: Trend, note: string) => void }) {
  const [note, setNote] = useState(row.trend_note ?? "");
  const buy = row.direction === "long";
  const lots = row.filled_lots || row.order_lots || 0;
  const price = row.entry_price_avg ?? row.entry_price;
  return (
    <div className="rounded-lg bg-[#0d0d14] px-3 py-2">
      <div className="flex items-baseline gap-2 flex-wrap text-xs">
        <span className="font-mono text-[#7070a0]">{hhmm(row.entry_time)}</span>
        <span className={`font-semibold ${buy ? "text-[#00e676]" : "text-[#ff1744]"}`}>{buy ? "多" : "空"} {row.contract ?? ""}</span>
        <span className="font-mono text-[#e0e0f0]">{lots} 口{price != null ? ` @ ${price.toLocaleString()}` : ""}</span>
        <span className="text-[#7070a0]">{statusText(row)}</span>
      </div>
      <div className="mt-1.5 flex items-center gap-1 flex-wrap">
        {TREND_OPTIONS.map((o) => (
          <button key={o.id} disabled={busy} onClick={() => onPick(o.id, note)}
            aria-pressed={row.trend_label === o.id}
            className={`text-[11px] px-2 py-0.5 rounded border disabled:opacity-50 ${
              row.trend_label === o.id ? "border-[#3b82f6] bg-[#3b82f6]/15 text-[#e0e0f0]" : "border-[#1e1e3a] text-[#7070a0] hover:border-[#3b82f6]"}`}>{o.text}</button>
        ))}
        {row.label_after_exit && <span className="text-[10px] text-[#ffc107]" title="出場之後才補填，結果已知，可能被事後記憶影響">事後標記</span>}
      </div>
      <div className="mt-1.5 flex items-center gap-1">
        <input value={note} maxLength={NOTE_MAX} onChange={(e) => setNote(e.target.value)} placeholder="備註（選填）"
          className="flex-1 min-w-0 bg-[#141420] border border-[#1e1e3a] rounded px-2 py-0.5 text-[11px] text-[#e0e0f0] outline-none focus:border-[#3b82f6]" />
        <button disabled={busy || !row.trend_label || note === (row.trend_note ?? "")} onClick={() => row.trend_label && onPick(row.trend_label, note)}
          className="text-[11px] px-2 py-0.5 rounded border border-[#1e1e3a] text-[#e0e0f0] hover:border-[#3b82f6] disabled:opacity-40 whitespace-nowrap">存備註</button>
      </div>
      <div className="mt-1 text-[10px] leading-snug text-[#404060]">進場時內外盤：{flowText(row)}</div>
    </div>
  );
}

export function LabelList({ rows, busyId, onPick }: { rows: EntryRow[]; busyId: string | null; onPick: (r: EntryRow, trend: Trend, note: string) => void }) {
  if (rows.length === 0) return <div className="text-xs text-[#7070a0]">最近 3 天沒有手動進場。</div>;
  return (
    <div className="space-y-1.5">
      {rows.map((r) => <LabelRow key={r.trade_id} row={r} busy={busyId === r.trade_id} onPick={(t, n) => onPick(r, t, n)} />)}
    </div>
  );
}

export default function EntryLabelCard() {
  const [rows, setRows] = useState<EntryRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setRows(await api.tradelog.entries(3, 40)); setErr(null); }
    catch (e) { setErr(friendlyLabelError(apiErrorDetail(e))); }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 15000);                  // 只讀本機資料庫
    const onPlaced = () => setTimeout(load, 3000);        // 下單後等成交回報寫進紀錄再抓
    window.addEventListener(ORDER_PLACED_EVENT, onPlaced);
    return () => { clearInterval(id); window.removeEventListener(ORDER_PLACED_EVENT, onPlaced); };
  }, [load]);

  const pick = useCallback(async (r: EntryRow, trend: Trend, note: string) => {
    setBusyId(r.trade_id);
    try { await api.tradelog.label(r.trade_id, trend, note.trim()); setErr(null); await load(); }
    catch (e) { setErr(friendlyLabelError(apiErrorDetail(e))); }
    setBusyId(null);
  }, [load]);

  const list = manualRows(rows);
  const unlabeled = list.filter((r) => r.trend_label == null).length;
  return (
    <div className="bg-[#141420] rounded-xl border border-[#1e1e3a] p-5 flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <h2 className="text-[11px] text-[#7070a0] uppercase tracking-widest">進場判斷標記</h2>
        {unlabeled > 0 && <span className="text-[10px] text-[#ffc107]">{unlabeled} 筆還沒標</span>}
      </div>
      <div className="text-[10px] leading-snug text-[#7070a0]">
        進場時憑直覺標一下：你覺得現在是 {TREND_OPTIONS.map((o) => trendText(o.id)).join("／")}？只是記錄，不影響下單。出場後才補的會標成「事後」。
      </div>
      {err && <div className="text-xs text-[#ffc107]">{err}</div>}
      {rows && <LabelList rows={list} busyId={busyId} onPick={pick} />}
    </div>
  );
}
