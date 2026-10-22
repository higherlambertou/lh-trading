// 系統健康（後端 core/system_health.py、GET /api/system/health；update.md 發現 66）的型別與顯示用的純函式。只顯示。
export type HealthStatus = "ok" | "warn" | "bad" | "off";
export interface HealthItem { key: string; title: string; status: HealthStatus; text: string; detail?: string | null }
export interface SystemHealth { as_of: number; mode: string; overall: HealthStatus; items: HealthItem[] }

export const STATUS_COLOR: Record<HealthStatus, string> = { ok: "#00e676", warn: "#ffc107", bad: "#ff1744", off: "#7070a0" };
export const STATUS_TEXT: Record<HealthStatus, string> = { ok: "正常", warn: "注意", bad: "異常", off: "沒有資料" };

/** 標題列的一句話：「全部正常」或「2 項要注意：資料庫備份、期交所資料自動更新」。 */
export function summaryLine(h: SystemHealth): string {
  const bad = h.items.filter((i) => i.status === "bad"), warn = h.items.filter((i) => i.status === "warn");
  if (!bad.length && !warn.length) return "全部正常";
  const parts = [];
  if (bad.length) parts.push(`${bad.length} 項異常：${bad.map((i) => i.title).join("、")}`);
  if (warn.length) parts.push(`${warn.length} 項要注意：${warn.map((i) => i.title).join("、")}`);
  return parts.join("；");
}
