// 臺指選擇權的到期序列（update.md 發現 70）：月選（TXO）＋週三週選（TX1/2/4/5）＋週五週選（TXU～TXZ）。
// 後端 GET /order/option/series 已依到期日排序；一個序列用「類別｜到期月」指定（週選的到期月不足以分辨，要帶類別）。
export interface OptionSeries {
  category: string;
  delivery_month: string;
  delivery_date: string;          // YYYY-MM-DD
  kind: "monthly" | "weekly_wed" | "weekly_fri" | "other";
  kind_text: string;
  days: number;
  label: string;                  // 例：「10/23（週五）週五週選・明天到期」
  n: number;
}

export const seriesKey = (s: { category: string; delivery_month: string }): string => `${s.category}|${s.delivery_month}`;

/** 「TXY|202610」→ { category: "TXY", deliveryMonth: "202610" }；舊格式（只有月份）當成月選。 */
export function parseSeriesKey(key: string): { category: string; deliveryMonth: string } {
  const [a, b] = String(key || "").split("|");
  return b !== undefined ? { category: a || "TXO", deliveryMonth: b } : { category: "TXO", deliveryMonth: a || "" };
}

/** 確認按鈕用的短名稱：「10/23 週五週選」「11/18 月選」——週選只寫月份會分不出是哪一個到期日（可能下錯合約）。 */
export function seriesShort(list: OptionSeries[], key: string): string {
  const s = list.find((x) => seriesKey(x) === key);
  if (!s) return parseSeriesKey(key).deliveryMonth;
  const [, m, d] = s.delivery_date.split("-");
  return `${Number(m)}/${Number(d)} ${s.kind_text}`;
}

/** 預設選哪個：最近、至少剩 minDays 天的月選（和以前一樣，不會一打開就是明天到期的週選）；沒有就最近的月選；再沒有就第一個。 */
export function defaultSeriesKey(list: OptionSeries[], minDays = 7): string {
  const monthly = list.filter((s) => s.kind === "monthly");
  const pick = monthly.find((s) => s.days >= minDays) ?? monthly[0] ?? list[0];
  return pick ? seriesKey(pick) : "";
}
