// 選擇權買方試算（〈分析〉分頁的〈選擇權試算〉）的型別、表單驗證與格式。沒有 React、沒有網路。
// 測試：tests/frontend/options_check.js（由 tests/test_frontend_options.py 執行）。後端見 api/routes_options.py、core/options_calc.py。

export type OptKind = "straddle" | "strangle" | "call" | "put";
export type OptSelect = "delta" | "move";

export const KIND_TEXT: Record<OptKind, string> = { straddle: "買跨式", strangle: "買勒式", call: "買 Call", put: "買 Put" };
export const KIND_HINT: Record<OptKind, string> = {
  straddle: "同一履約價同時買 Call 與 Put（不賭方向、賭會大動）",
  strangle: "買較遠的價外 Call 與價外 Put（比跨式便宜、要動更多才回本）",
  call: "只買 Call（看多；情境二：事件驅動單向）",
  put: "只買 Put（看空；情境二：事件驅動單向）",
};

// 資料來源：即時（向券商一次查整個到期月的鏈）或手動（自己給遠期價、IV、天數，用 Black-76 造模擬鏈，休市也能用）
export interface OptSource {
  manual: boolean;
  deliveryMonth: string;
  mF: string; mIv: string; mDays: string; mSpread: string;
}

export interface OptForm {
  kind: OptKind;
  select: OptSelect;
  delta: string;        // 目標 |delta|
  k: string;            // 履約價距離 ＝ k × 預期波動
  movePts: string;      // 你預期的波動點數；空白＝市場隱含的 1 個標準差
  lots: string;
  budget: string;       // 元，可空白
  holdHours: string;
}

export const DEFAULT_SOURCE: OptSource = { manual: false, deliveryMonth: "", mF: "", mIv: "", mDays: "", mSpread: "2" };
export const DEFAULT_FORM: OptForm = { kind: "straddle", select: "delta", delta: "0.25", k: "0.5", movePts: "", lots: "1", budget: "", holdHours: "6" };
// 切換結構時的預設 delta：勒式的兩翼 0.25；單向 0.40
export const defaultDelta = (kind: OptKind): string => (kind === "strangle" ? "0.25" : "0.40");

export interface ManualBody { F: number; iv: number; days: number; spread_pct: number }
export interface CalcBody {
  kind: OptKind; category: string; delivery_month?: string; select: OptSelect; delta: number; k: number; move_pts?: number;
  lots: number; budget_twd?: number; hold_hours: number; manual?: ManualBody;
}
export type Built<T> = { ok: true; body: T } | { ok: false; error: string };

const num = (s: string): number => (s.trim() === "" ? NaN : Number(s));

export function buildManual(src: OptSource): Built<ManualBody> {
  const F = num(src.mF), iv = num(src.mIv), days = num(src.mDays), sp = num(src.mSpread || "2");
  if (!(F > 0)) return { ok: false, error: "手動模式：遠期價要大於 0" };
  if (!(iv > 0 && iv < 300)) return { ok: false, error: "手動模式：ATM IV（%）要大於 0、小於 300" };
  if (!(days > 0 && days <= 800)) return { ok: false, error: "手動模式：距到期天數要大於 0" };
  if (!(sp >= 0 && sp <= 100)) return { ok: false, error: "手動模式：模擬價差（%）要在 0～100" };
  return { ok: true, body: { F, iv, days, spread_pct: sp / 100 } };
}

export function buildCalcRequest(f: OptForm, src: OptSource): Built<CalcBody> {
  const delta = num(f.delta), k = num(f.k), lots = num(f.lots), hold = num(f.holdHours || "0");
  const move = f.movePts.trim() === "" ? undefined : num(f.movePts), budget = f.budget.trim() === "" ? undefined : num(f.budget);
  if (f.kind !== "straddle") {
    if (f.select === "delta" && !(delta > 0 && delta < 1)) return { ok: false, error: "Delta 要在 0～1 之間（例如 0.25）" };
    if (f.select === "move" && !(k >= -3 && k <= 5)) return { ok: false, error: "距離倍數要在 −3～5 之間（例如 0.5）" };
  }
  if (move !== undefined && !(move > 0)) return { ok: false, error: "預期波動點數要大於 0（或留空用市場隱含的）" };
  if (!(Number.isInteger(lots) && lots >= 1 && lots <= 1000)) return { ok: false, error: "組數要是 1～1000 的整數" };
  if (budget !== undefined && !(budget > 0)) return { ok: false, error: "預算要大於 0（或留空）" };
  if (!(hold >= 0 && hold <= 720)) return { ok: false, error: "持有時間（小時）要在 0～720" };
  const body: CalcBody = { kind: f.kind, category: "TXO", select: f.select, delta: Number.isFinite(delta) ? delta : 0.25, k: Number.isFinite(k) ? k : 0.5,
    lots, hold_hours: hold };
  if (move !== undefined) body.move_pts = move;
  if (budget !== undefined) body.budget_twd = budget;
  if (src.manual) {
    const m = buildManual(src);
    if (!m.ok) return m;
    body.manual = m.body;
  } else {
    if (!src.deliveryMonth) return { ok: false, error: "請選到期月，或改用手動模式" };
    body.delivery_month = src.deliveryMonth;
  }
  return { ok: true, body };
}

// ── 平倉試算 ──────────────────────────────────────────────────────

export interface CloseRow { right: "C" | "P"; strike: string; qty: string; entry: string; bid: string; ask: string }
export const EMPTY_ROW: CloseRow = { right: "C", strike: "", qty: "1", entry: "", bid: "", ask: "" };
export interface CloseBody {
  legs: { right: "C" | "P"; strike: number; qty: number; entry: number; bid?: number; ask?: number }[];
  category: string; delivery_month?: string; move_pts?: number; hold_hours: number; manual?: ManualBody;
}

export function buildCloseRequest(rows: CloseRow[], src: OptSource, movePts: string, holdHours: string): Built<CloseBody> {
  const legs: CloseBody["legs"] = [];
  for (const [i, r] of rows.entries()) {
    const strike = num(r.strike), qty = num(r.qty), entry = num(r.entry);
    const tag = `第 ${i + 1} 腳`;
    if (!(strike > 0)) return { ok: false, error: `${tag}：履約價要大於 0` };
    if (!(Number.isInteger(qty) && qty >= 1 && qty <= 1000)) return { ok: false, error: `${tag}：口數要是 1～1000 的整數` };
    if (!(entry >= 0)) return { ok: false, error: `${tag}：買進價要填（點，不能是負的）` };
    const leg: CloseBody["legs"][number] = { right: r.right, strike, qty, entry };
    if (r.bid.trim() !== "") { const b = num(r.bid); if (!(b >= 0)) return { ok: false, error: `${tag}：目前買價不能是負的` }; leg.bid = b; }
    if (r.ask.trim() !== "") { const a = num(r.ask); if (!(a >= 0)) return { ok: false, error: `${tag}：目前賣價不能是負的` }; leg.ask = a; }
    legs.push(leg);
  }
  if (!legs.length) return { ok: false, error: "至少要有一腳" };
  const move = movePts.trim() === "" ? undefined : num(movePts), hold = num(holdHours || "0");
  if (move !== undefined && !(move > 0)) return { ok: false, error: "預期波動點數要大於 0（或留空）" };
  if (!(hold >= 0 && hold <= 720)) return { ok: false, error: "持有時間（小時）要在 0～720" };
  const body: CloseBody = { legs, category: "TXO", hold_hours: hold };
  if (move !== undefined) body.move_pts = move;
  if (src.manual) {
    const m = buildManual(src);
    if (!m.ok) return m;
    body.manual = m.body;
  } else {
    if (!src.deliveryMonth) return { ok: false, error: "請選到期月，或改用手動模式" };
    body.delivery_month = src.deliveryMonth;
  }
  return { ok: true, body };
}

// 把試算結果的各腳帶到平倉試算（買進價＝試算時用的進場價）
export function rowsFromCalc(r: OptCalc): CloseRow[] {
  return r.legs.map((l) => ({ right: l.right, strike: String(l.strike), qty: String(l.qty * (r.cost.lots || 1)), entry: String(l.entry ?? ""), bid: "", ask: "" }));
}

// ── 後端回應的型別 ────────────────────────────────────────────────

export interface OptLeg {
  right: "C" | "P"; strike: number; qty: number; bid: number | null; ask: number | null; mid: number | null; entry: number | null;
  iv: number | null; delta: number | null; spread: number | null; spread_pct: number | null;
}
export interface OptBE { level: number | null; dist_pts: number | null; dist_pct: number; dist_em: number | null }
export interface OptGreeks {
  delta: number; gamma: number; theta_day_pts: number; vega_pts: number; delta_twd: number; theta_day_twd: number; vega_twd: number;
}
export interface OptHedge {
  net_delta: number; delta_twd_per_pt: number; tmf_lots: number; tmf_lots_exact: number; residual_delta: number; residual_twd_per_pt: number;
  gamma: number; theta_day_twd: number; be_rms_pts: number | null; implied_rms_pts: number | null; realized_rms_pts: number | null; ratio: number | null;
  cost_per_hedge_twd: number; cost_pts: number;
}
export interface OptScen {
  moves: { mult: number; pts: number; pct: number }[];
  time_cols: { label: string; days: number }[];
  pnl_pts: (number | null)[][]; pnl_twd: number[][];
  iv: { after_days: number; shocks: number[]; pnl_pts: (number | null)[][]; pnl_twd: number[][] };
}
export interface OptMeta { source: "live" | "manual"; category: string | null; delivery_month: string | null; expiry: string | null; quote_ts: number | null; as_of: number }
export interface OptCalc extends OptMeta {
  kind: OptKind; F: number | null; days: number; atm_iv: number | null;
  legs: OptLeg[];
  cost: { pts: number | null; mid_pts: number | null; slippage_pts: number | null; exit_spread_pts: number | null; per_lot_twd: number; lots: number;
          total_twd: number; budget_twd: number | null; max_lots: number | null };
  max_loss: { pts: number | null; twd: number };
  expected_move: { sigma_pts: number | null; straddle_pts: number | null; ref_pts: number | null; ref_source: "user" | "implied" };
  breakeven: OptBE[];
  prob: { lognormal: number | null; hist: { p: number; n: number; h_days: number; abs_move_q: { "50": number; "75": number; "90": number } } | null };
  greeks: OptGreeks;
  hedge: OptHedge;
  scenarios: OptScen;
  warnings: string[];
}
export interface OptCloseLeg {
  right: "C" | "P"; strike: number; qty: number; entry: number; bid: number | null; ask: number | null; mid: number | null; iv: number | null;
  pnl_mid_pts: number | null; pnl_bid_pts: number | null; role: "loser" | "winner" | null;
}
export interface OptCloseAction {
  id: string; label: string; closed: number[];
  realized_pts: number | null; realized_twd: number; proceeds_pts: number | null; net_basis_pts: number | null;
  max_loss_pts: number | null; max_loss_twd: number; remaining_now_pts: number | null; need_ratio: number | null;
  breakeven: OptBE[]; greeks: OptGreeks | null; hedge: OptHedge | null; scenarios: OptScen | null;
}
export interface OptClose extends OptMeta {
  F: number | null; days: number; iv: number | null; em_sigma_pts: number | null; ref_pts: number | null; entry_total_pts: number | null;
  legs: OptCloseLeg[]; actions: OptCloseAction[]; warnings: string[];
}

export interface OptChain extends OptMeta {
  F: number; days: number; atm_iv_pct: number | null; em_sigma_pts: number; straddle_pts: number;
  rows: { strike: number; call: { bid: number; ask: number; mid: number; iv: number | null; delta: number | null; spread_pct: number | null } | null; put: { bid: number; ask: number; mid: number; iv: number | null; delta: number | null; spread_pct: number | null } | null }[];
}

// 預設的到期月：第一個「距到期 ≥ 7 天」的月份（和後端 IV 監控同一個規則，避開到期週的 Gamma）；都不到就取第一個。
// 月選擇權到期日＝該月第三個週三 13:30（遇假日順延的情形忽略）。
export function thirdWednesday(ym: string): Date | null {
  if (!/^\d{6}$/.test(ym)) return null;
  const y = Number(ym.slice(0, 4)), m = Number(ym.slice(4, 6)) - 1;
  const first = new Date(y, m, 1);
  const offset = (3 - first.getDay() + 7) % 7;          // 第一個週三（getDay：週日 0、週三 3）
  return new Date(y, m, 1 + offset + 14, 13, 30);
}
export function defaultMonth(expiries: string[], now: Date = new Date(), minDays = 7): string {
  const sorted = [...expiries].sort();
  const ok = sorted.find((e) => { const d = thirdWednesday(e); return d != null && (d.getTime() - now.getTime()) / 86400000 >= minDays; });
  return ok ?? sorted[0] ?? "";
}

// ── 格式 ─────────────────────────────────────────────────────────

export const MINUS = "−";
export function pts(x: number | null | undefined, digits = 0): string {
  if (x == null || !Number.isFinite(x)) return "—";
  return x.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
export function spts(x: number | null | undefined, digits = 0): string {
  if (x == null || !Number.isFinite(x)) return "—";
  const r = Number(x.toFixed(digits));
  return r === 0 ? (0).toFixed(digits) : `${r > 0 ? "+" : MINUS}${pts(Math.abs(r), digits)}`;
}
export const twd = (x: number | null | undefined): string => (x == null || !Number.isFinite(x) ? "—" : `${x < 0 ? MINUS : ""}${Math.abs(Math.round(x)).toLocaleString("en-US")} 元`);
export const stwd = (x: number | null | undefined): string => (x == null || !Number.isFinite(x) ? "—" : `${x > 0 ? "+" : x < 0 ? MINUS : ""}${Math.abs(Math.round(x)).toLocaleString("en-US")}`);
export const pct1 = (x: number | null | undefined, d = 1): string => (x == null || !Number.isFinite(x) ? "—" : `${(x * 100).toFixed(d)}%`);
export const spct = (x: number | null | undefined, d = 2): string => (x == null || !Number.isFinite(x) ? "—" : `${x > 0 ? "+" : x < 0 ? MINUS : ""}${Math.abs(x * 100).toFixed(d)}%`);
export const sigma = (x: number | null | undefined): string => (x == null || !Number.isFinite(x) ? "—" : `${x > 0 ? "+" : x < 0 ? MINUS : ""}${Math.abs(x).toFixed(2)}σ`);
export const rightText = (r: "C" | "P"): string => (r === "C" ? "買權 Call" : "賣權 Put");
export const pnlColor = (x: number | null | undefined): string => (x == null || x === 0 ? "#7070a0" : x > 0 ? "#00e676" : "#ff1744");

// 微台對沖的建議文字：「賣出 4 口微台可中和（剩 +200 元/點）」；0 口＝不用對沖
export function hedgeText(h: OptHedge): string {
  const n = h.tmf_lots;
  if (n === 0) return `淨 Delta ${h.net_delta.toFixed(2)}（約 ${stwd(h.delta_twd_per_pt)} 元/點）：不用對沖`;
  return `淨 Delta ${h.net_delta.toFixed(2)}（約 ${stwd(h.delta_twd_per_pt)} 元/點）→ ${n > 0 ? "買進" : "賣出"} ${Math.abs(n)} 口微台可中和，剩 ${stwd(h.residual_twd_per_pt)} 元/點`;
}

// 損益兩平一行字：「上 50,556（+1,267 點、+2.57%、+0.82σ）」
export function beText(b: OptBE): string {
  const side = b.dist_pts != null && b.dist_pts < 0 ? "下" : "上";
  return `${side} ${pts(b.level)}（${spts(b.dist_pts)} 點、${spct(b.dist_pct)}、${sigma(b.dist_em)}）`;
}
