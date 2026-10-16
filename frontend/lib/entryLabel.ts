// 手動單的「趨勢判斷」標記（後端 core/entry_log.py、GET /api/tradelog/entries、POST /api/tradelog/entries/{id}/label；update.md 發現 47）。
// 只事後記錄、不擋也不延遲下單；欄位名稱要和後端一致（tests/test_frontend_entry_label.py 用後端現算的資料渲染）。

export type Trend = "multi" | "short" | "range" | "none";

export const TREND_OPTIONS: { id: Trend; text: string }[] = [
  { id: "multi", text: "多" },
  { id: "short", text: "空" },
  { id: "range", text: "盤整" },
  { id: "none", text: "無明確判斷" },
];

export const NOTE_MAX = 200;

export interface EntryRow {
  trade_id: string;
  contract: string | null;
  order_source: "manual" | "strategy";
  strategy_name: string | null;       // 手動單是 null
  direction: "long" | "short" | null;  // 後端寫的是 long／short，不是 Buy／Sell
  entry_time: string | null;           // ISO（台北時間）
  entry_price: number | null;
  entry_price_avg: number | null;
  order_lots: number | null;
  filled_lots: number | null;
  status: "open" | "partial" | "closed";
  pnl_points: number | null;
  flow_inside: number | null;
  flow_outside: number | null;
  flow_net_diff: number | null;
  flow_ratio: number | null;
  flow_accum_start: string | null;
  flow_missing_reason: string | null;
  trend_label: Trend | null;
  trend_note: string | null;
  label_entered_at: string | null;
  label_after_exit: boolean | null;
  label_missing_reason: "not_entered" | "strategy_order" | null;
}

/** 手動單才能標：已經有標籤，或沒標但原因是「還沒標」（策略單的原因是 strategy_order）。 */
export const isManual = (r: EntryRow): boolean => r.trend_label != null || r.label_missing_reason === "not_entered";

/** 最近的手動進場（後端已是新 → 舊），最多 max 筆。 */
export const manualRows = (rows: EntryRow[] | null | undefined, max = 8): EntryRow[] => (rows || []).filter(isManual).slice(0, max);

export const trendText = (t: string | null | undefined): string => TREND_OPTIONS.find((o) => o.id === t)?.text ?? "—";

/** "2026-10-16T09:12:03+08:00" → "09:12"；壞掉就原樣回傳。 */
export const hhmm = (iso: string | null | undefined): string => {
  const m = /T(\d{2}:\d{2})/.exec(iso || "");
  return m ? m[1] : iso || "—";
};

const signed = (n: number) => (n > 0 ? "+" : "") + n.toLocaleString();

/** 進場當下的內外盤摘要（外盤占比＝面板上的比例；差＝外盤−內盤）；沒記到就回後端給的原因，絕不顯示 0。 */
export function flowText(r: EntryRow): string {
  if (r.flow_ratio == null || r.flow_net_diff == null) return r.flow_missing_reason || "沒有記到內外盤";
  return `外盤 ${(r.flow_ratio * 100).toFixed(1)}%・外−內 ${signed(r.flow_net_diff)} 口${r.flow_accum_start ? `（${r.flow_accum_start}）` : ""}`;
}

export const statusText = (r: EntryRow): string =>
  r.status === "closed" ? `已平${r.pnl_points != null ? ` ${signed(Math.round(r.pnl_points * 10) / 10)} 點` : ""}` : r.status === "partial" ? "部分平倉" : "持有中";

export const friendlyLabelError = (msg: string): string =>
  /^Not Found$/i.test(msg) ? "後端還沒有這個 API（要重啟後端才有）" : msg;
