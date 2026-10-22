// 下單前檢查（後端 core/precheck.py、GET /api/market/precheck；update.md 發現 60）的型別與顯示用的純函式。只顯示、不擋單。
export interface PrecheckEvent { kind: "CPI" | "NFP" | "FOMC"; label: string; when: string; time: number; note: string }
export interface Precheck {
  day_range: { expect?: { lo: number; mid: number; hi: number }; r20?: number; vix?: number; vix_date?: string; note?: string };
  iv: { value: number | null; percentile: number | null; label: string; history_n: number; note: string } | null;
  events: PrecheckEvent[] | null; events_note: string | null; event_hours: number; as_of: number;
}

const n0 = (x: number | null | undefined): string => (x == null || !Number.isFinite(x) ? "—" : Math.round(x).toLocaleString("en-US"));

export function rangeLine(p: Precheck): string {
  const d = p.day_range;
  if (!d.expect) return `今天預期日盤振幅：${d.note ?? "還沒有"}`;
  return `今天預期日盤振幅約 ${n0(d.expect.mid)} 點（68% 區間 ${n0(d.expect.lo)}～${n0(d.expect.hi)}；近 20 日均 ${n0(d.r20)}）`;
}

export function ivLine(p: Precheck): string {
  const iv = p.iv;
  if (!iv || iv.value == null) return "隱含波動：還沒有今天的數字";
  if (iv.percentile == null) return `隱含波動 ${iv.value.toFixed(1)}%（${iv.label}）`;
  return `隱含波動 ${iv.value.toFixed(1)}%，在過去一年是第 ${Math.round(iv.percentile)} 百分位（${iv.label}）`;
}

export function eventLines(p: Precheck): string[] {
  if (p.events == null) return [`重要事件：${p.events_note ?? "讀不到"}`];
  if (p.events.length === 0) return [`${p.event_hours} 小時內沒有美國重要事件（CPI、非農、FOMC）`];
  return p.events.map((e) => `${e.when} ${e.label}：${e.note}`);
}

export const ivHigh = (p: Precheck): boolean => p.iv?.percentile != null && p.iv.percentile >= 80;
