// 口數試算（後端 core/position_size.py、GET /api/risk/size；update.md 發現 51）的型別與顯示用的純函式。只顯示、不擋任何委託。
// 欄位名稱要和後端一致（tests/test_frontend_size.py 用後端現算的資料渲染，欄位改了沒跟著改會被抓到）。

export interface SizeCap { name: "single" | "total" | "cap"; lots: number; budget_twd: number | null }
export interface SizeOpenRisk {
  twd: number; pct: number | null; covered_lots: number;
  uncovered: { code: string; direction: string; quantity: number }[];
  options: { code: string; direction: string; quantity: number }[];
}
export interface Size {
  contract: string;
  lots: number | null;                  // null＝不給口數（看 reason）；0＝算出不足 1 口
  reason: "contract_unknown" | "equity_missing" | "equity_stale" | "stop_missing" | "below_one" | null;
  equity: number | null;
  stop_pts: number | null;
  slip_pts: number;
  risk_pct: number;
  total_risk_pct: number;
  point_value: number | null;
  open_risk_twd: number;
  caps: SizeCap[];
  binding: SizeCap["name"] | null;
  qty: number | null;
  eff_stop?: number;
  risk_per_lot?: number;
  risk_per_lot_pct?: number;
  need_equity_single?: number;
  max_stop_for_one?: number;
  qty_risk_twd?: number;
  qty_risk_pct?: number;
  qty_over?: boolean;
  qty_over_by?: SizeCap["name"][];
  open_risk: SizeOpenRisk;
  lot_cap: number | null;
  as_of: number;
}

const ok = (x: number | null | undefined): x is number => x != null && Number.isFinite(x);

export const money = (x: number | null | undefined): string => (ok(x) ? Math.round(x).toLocaleString("en-US") : "—");

// 已經是百分比的數字（0.6 → 「0.6%」）；不是 0~1 的比例
export const pctText = (x: number | null | undefined, digits = 2): string => (ok(x) ? `${x.toFixed(digits).replace(/\.?0+$/, "")}%` : "—");

export const REASON_TEXT: Record<string, string> = {
  contract_unknown: "合約不認得",
  equity_missing: "權益還沒取得，無法算口數",
  equity_stale: "權益資料已過期（超過 5 分鐘沒更新），不給口數",
  stop_missing: "填停損點數後，這裡會顯示依風險上限最多幾口",
};

export function bindingText(s: Size): string {
  switch (s.binding) {
    case "single": return `單筆風險上限 ${pctText(s.risk_pct)}`;
    case "total": return `總風險上限 ${pctText(s.total_risk_pct)} 的餘額`;
    case "cap": return `單筆口數上限 ${s.lot_cap ?? "—"} 口`;
    default: return "";
  }
}

export const capName = (n: SizeCap["name"], s: Size): string =>
  n === "single" ? `單筆風險 ${pctText(s.risk_pct)}` : n === "total" ? `總風險餘額` : `口數上限 ${s.lot_cap ?? "—"}`;

/** 主要那一行：不給口數的原因；不足 1 口要什麼才夠；否則是最多幾口與哪個上限在管。 */
export function lotsLine(s: Size): string {
  if (s.lots == null) return REASON_TEXT[s.reason ?? ""] ?? "無法計算";
  if (s.lots < 1) {
    const bits: string[] = [];
    if (ok(s.max_stop_for_one) && s.max_stop_for_one > 0) bits.push(`停損縮到 ${s.max_stop_for_one} 點以內`);
    if (ok(s.need_equity_single)) bits.push(`或權益到 ${money(s.need_equity_single)} 元（單筆風險 ${pctText(s.risk_pct)}）`);
    return `算出不足 1 口（${bindingText(s)}）${bits.length ? `：要 1 口需${bits.join("，")}` : ""}`;
  }
  return `最多 ${s.lots} 口（${bindingText(s)}）`;
}

/** 每口風險那一行（含滑價緩衝）。 */
export function perLotLine(s: Size): string {
  if (!ok(s.risk_per_lot) || !ok(s.stop_pts)) return "";
  return `停損 ${s.stop_pts} 點＋緩衝 ${s.slip_pts} 點，每口約 ${money(s.risk_per_lot)} 元（${pctText(s.risk_per_lot_pct)} 權益）`;
}

/** 你填的口數對照上限；沒填口數或不給口數就是空字串。 */
export function qtyLine(s: Size): string {
  if (s.qty == null || !ok(s.qty_risk_twd) || s.lots == null) return "";
  const base = `你填 ${s.qty} 口：風險約 ${money(s.qty_risk_twd)} 元（${pctText(s.qty_risk_pct)} 權益）`;
  return s.qty_over ? `${base}，超過上限（${(s.qty_over_by ?? []).map((n) => capName(n, s)).join("、")}）` : `${base}，在上限內`;
}

export function openRiskLine(s: Size): string {
  const o = s.open_risk;
  const base = `已佔用風險 ${money(o.twd)} 元（${pctText(o.pct)} 權益，上限 ${pctText(s.total_risk_pct)}）`;
  return o.covered_lots > 0 || o.twd > 0 ? `${base}，${o.covered_lots} 口有登記停損` : base;
}

export function warnLines(s: Size): string[] {
  const out: string[] = [];
  const un = s.open_risk.uncovered.reduce((a, u) => a + u.quantity, 0);
  if (un > 0) out.push(`另有 ${un} 口期貨沒有登記停損（${s.open_risk.uncovered.map((u) => u.code).join("、")}），風險未計入——總風險只是下限`);
  const op = s.open_risk.options.reduce((a, u) => a + u.quantity, 0);
  if (op > 0) out.push(`選擇權部位 ${op} 口不計入（買方最大虧損＝權利金）`);
  return out;
}

export const friendlySizeError = (msg: string): string =>
  msg.trim() === "Not Found" ? "後端還沒有這個 API（要重啟後端才有）" : msg;
