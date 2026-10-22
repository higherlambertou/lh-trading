// 下單前檢查（後端 core/precheck.py、GET /api/market/precheck；update.md 發現 60）的型別與顯示用的純函式。只顯示、不擋單。
export interface PrecheckEvent { kind: "CPI" | "NFP" | "FOMC"; label: string; when: string; time: number; note: string }
export interface Precheck {
  day_range: { expect?: { lo: number; mid: number; hi: number }; r20?: number; vix?: number; vix_date?: string; note?: string };
  iv: { value: number | null; percentile: number | null; label: string; history_n: number; note: string } | null;
  events: PrecheckEvent[] | null; events_note: string | null; event_hours: number; as_of: number;
  // 13:45～05:00：今晚夜盤（發現 67）與下一個日盤（發現 55 的模型）的預期振幅；其他時段 null
  evening?: {
    prev_day?: string; error?: string;
    night?: { expect?: { lo: number; mid: number; hi: number }; r20n?: number; vix?: number; vix_date?: string; day_range?: number; note?: string };
    next_day?: { expect?: { lo: number; mid: number; hi: number }; r20?: number; vix?: number; vix_date?: string; note?: string } | null;
  } | null;
}

const n0 = (x: number | null | undefined): string => (x == null || !Number.isFinite(x) ? "—" : Math.round(x).toLocaleString("en-US"));

export function rangeLine(p: Precheck): string {
  const d = p.day_range;
  if (!d.expect) return `今天預期日盤振幅：${d.note ?? "還沒有"}`;
  return `今天預期日盤振幅約 ${n0(d.expect.mid)} 點（68% 區間 ${n0(d.expect.lo)}～${n0(d.expect.hi)}；近 20 日均 ${n0(d.r20)}）`;
}

const md = (d?: string): string => (d ? `${Number(d.slice(5, 7))}/${Number(d.slice(8, 10))}` : "");

/** 夜盤那一行：「10/22 晚上的夜盤預期振幅約 899 點（…）」；不在 13:45～05:00 就沒有（null）。 */
export function nightLine(p: Precheck): string | null {
  const e = p.evening;
  if (!e) return null;
  if (e.error || !e.night) return "夜盤預期振幅：計算失敗（細節在後端 log）";
  const n = e.night;
  const who = e.prev_day ? `${md(e.prev_day)} 晚上的夜盤` : "夜盤";
  if (!n.expect) return `${who}預期振幅：${n.note ?? "還沒有"}`;
  return `${who}預期振幅約 ${n0(n.expect.mid)} 點（68% 區間 ${n0(n.expect.lo)}～${n0(n.expect.hi)}；近 20 個夜盤平均 ${n0(n.r20n)}）`;
}

/** 下一個日盤那一行（13:45～24:00）；沒有就 null。 */
export function nextDayLine(p: Precheck): string | null {
  const d = p.evening?.next_day;
  if (!d) return null;
  if (!d.expect) return `下一個日盤預期振幅：${d.note ?? "還沒有"}`;
  return `下一個日盤預期振幅約 ${n0(d.expect.mid)} 點（68% 區間 ${n0(d.expect.lo)}～${n0(d.expect.hi)}；近 20 日均 ${n0(d.r20)}）`;
}

/** 傍晚以後「今天的日盤」那一行如果只是「還沒有」就不顯示（換成夜盤與下一個日盤）。 */
export const showDayLine = (p: Precheck): boolean => !p.evening || !!p.day_range.expect;

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
