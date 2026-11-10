// 持倉情境表（只顯示、不擋單；update.md 2026-11-10）：現有期貨持倉在價格漲跌若干點時，損益、權益與離追繳還有多遠。純函式、不碰 DOM 與下單。
// 假設（畫面上也會寫）：權益數是「現在」的值；各合約同點數漲跌（台指期三種的價差短時間內幾乎不變）；維持保證金不變；不含手續費與滑價；選擇權部位不在內。
// 做法：用「現在到情境的價差」算增減（權益 ＋ 持倉每點價值 × 漲跌點數），不依賴權益數有沒有含未實現損益。
import { pointValue, type Margin, type Position } from "@/lib/api";

export const MOVES = [-300, -200, -100, -50, 50, 100, 200, 300];
export const NEAR_RATIO = 1.25;                      // 權益低於維持保證金的 1.25 倍＝接近追繳

export const isFuture = (code: string): boolean => /^(TMF|MXF|TXF)/.test(code);

/** 台指期三種（微台、小台、大台）的持倉；選擇權與數量 0 的不算。 */
export const futures = (positions: Position[]): Position[] => positions.filter((p) => isFuture(p.code) && p.quantity > 0);

/** 有沒有不在情境表裡的持倉（選擇權）。 */
export const hasOthers = (positions: Position[]): boolean => positions.some((p) => !isFuture(p.code) && p.quantity > 0);

/** 每漲 1 點的損益（元）：多方為正、空方為負，各合約加總。0＝多空相抵。 */
export function exposure(positions: Position[]): number {
  return futures(positions).reduce((s, p) => s + (p.direction === "Buy" ? 1 : -1) * p.quantity * pointValue(p.code), 0);
}

export type Status = "ok" | "near" | "call" | "zero";

export interface Row {
  move: number;          // 漲跌點數（現價起算）
  pnl: number;           // 相對現在的損益增減（元）
  equity: number;        // 情境下的權益（元）
  pct: number | null;    // 損益增減占現在權益的百分比；權益 ≤ 0 算不出來
  status: Status;
}

export function statusOf(equity: number, maintenance: number): Status {
  if (equity <= 0) return "zero";
  if (equity < maintenance) return "call";                  // 沒有維持保證金（0）時這兩行都不會成立
  if (equity < maintenance * NEAR_RATIO) return "near";
  return "ok";
}

export function rows(exp: number, equity: number, maintenance: number, moves: number[] = MOVES): Row[] {
  return moves.map((move) => {
    const pnl = exp * move, eq = equity + pnl;
    return { move, pnl, equity: eq, pct: equity > 0 ? (pnl / equity) * 100 : null, status: statusOf(eq, maintenance) };
  });
}

export interface Reach {
  dir: 1 | -1 | 0;                 // 對你不利的價格方向：多方 −1（跌）、空方 +1（漲）、0＝多空相抵
  toCall: number | null;           // 再走幾點會被追繳（權益掉到維持保證金以下）；已經在追繳線以下＝0；沒有維持保證金資料＝null
  toZero: number | null;           // 再走幾點權益歸零；權益已經 ≤ 0＝0
}

export function reach(exp: number, equity: number, maintenance: number): Reach {
  if (exp === 0) return { dir: 0, toCall: null, toZero: null };
  const per = Math.abs(exp);
  return {
    dir: exp > 0 ? -1 : 1,
    toCall: maintenance > 0 ? Math.max(0, Math.floor((equity - maintenance) / per)) : null,
    toZero: Math.max(0, Math.floor(equity / per)),
  };
}

export const money = (x: number): string => Math.round(x).toLocaleString("en-US");
const signed = (x: number): string => (x > 0 ? "+" : x < 0 ? "−" : "") + money(Math.abs(x));
export const pctText = (x: number | null): string => (x == null ? "—" : `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x).toFixed(1)}%`);
export const STATUS_TEXT: Record<Status, string> = { ok: "", near: "接近追繳", call: "追繳", zero: "權益歸零" };

/** 一行摘要（不用展開就看得到）：每 100 點的損益、離追繳還有多遠。不能算（沒權益、沒有期貨持倉）回 null。 */
export function summaryLines(positions: Position[], margin: Margin | null): string[] | null {
  const fs = futures(positions);
  if (!fs.length || !margin || !Number.isFinite(margin.equity)) return null;
  const exp = exposure(positions), eq = margin.equity, mt = margin.maintenance_margin;
  if (exp === 0) return ["多空相抵：價格漲跌對權益沒有影響（假設各合約同點數漲跌）"];
  if (eq <= 0) return [`權益已經 ${money(eq)} 元（≤ 0）`];
  const r = reach(exp, eq, mt), word = r.dir < 0 ? "跌" : "漲", lose = Math.abs(exp) * 100;
  const out = [`價格每${word} 100 點：損益 −${money(lose)} 元（占權益 ${((lose / eq) * 100).toFixed(1)}%）`];
  if (r.toCall != null) out.push(r.toCall === 0 ? `權益已經低於維持保證金 ${money(mt)} 元（追繳線）` : `再${word}約 ${money(r.toCall)} 點，權益掉到維持保證金 ${money(mt)} 元（追繳）`);
  else out.push("維持保證金還沒取得，算不出離追繳多遠");
  if (r.toZero != null) out.push(`再${word}約 ${money(r.toZero)} 點，權益歸零`);
  return out;
}

export const FOOT_TEXT = "假設權益數是現在的值、各合約同點數漲跌、維持保證金不變，不含手續費與滑價；只是試算，不影響下單。";
export const OPTION_NOTE = "選擇權部位不在這張表裡。";
export { signed };
