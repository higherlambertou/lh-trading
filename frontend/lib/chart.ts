// 走勢圖的純邏輯（update.md 發現 42）：合併 K 棒、換週期、均線、VWAP、場次水位、持倉／停損線。
// 不碰 DOM、不碰 lightweight-charts，所以可以用 node 直接測（tests/frontend/chart_check.js）。
import type { Position, Watch } from "./api";

export interface Bar {
  t: number;   // 分鐘起點（epoch 秒）
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;   // 成交口數
  b: number;   // 外盤口數（買方主動）
  s: number;   // 內盤口數（賣方主動）
  n: number;   // 成交筆數
}

export interface BarsResponse {
  contract: string;
  interval: number;
  now: number;
  bars: Bar[];
  live: Bar | null;
}

/** lightweight-charts 的時間軸一律當 UTC 顯示；加 8 小時就是台北時間。 */
export const TZ_OFFSET = 8 * 3600;

/** 兩根 K 棒相隔超過這麼久就算不同場次（日盤收 13:45、夜盤 15:00 開，中間 75 分鐘；盤中不會有這麼久的空檔）。 */
export const SESSION_GAP_SEC = 3600;

/** 把新抓到的 K 棒合進既有的（同一分鐘以新的為準）；live 是進行中那一根，同一分鐘就取代、較新就補上。回傳依時間排序的新陣列。 */
export function mergeBars(base: Bar[], incoming: Bar[], live: Bar | null): Bar[] {
  const m = new Map<number, Bar>();
  for (const b of base) m.set(b.t, b);
  for (const b of incoming) m.set(b.t, b);
  if (live) m.set(live.t, live);
  return Array.from(m.values()).sort((a, b) => a.t - b.t);
}

/** 兩份 K 棒陣列內容一樣嗎（長度、頭尾與最後三根的每個欄位）；沒變就不必重畫。 */
export function sameBars(a: Bar[], b: Bar[]): boolean {
  if (a.length !== b.length) return false;
  const k = (x: Bar) => `${x.t}|${x.o}|${x.h}|${x.l}|${x.c}|${x.v}|${x.b}|${x.s}|${x.n}`;
  const n = a.length;
  if (n === 0) return true;
  if (a[0].t !== b[0].t) return false;
  for (let i = Math.max(0, n - 3); i < n; i++) if (k(a[i]) !== k(b[i])) return false;
  return true;
}

/** 1 分 K 合成 N 分 K（桶＝epoch 秒除以 N 分鐘取整；日盤 08:45、夜盤 15:00 都是 5、15 的倍數，不會跨場次）。 */
export function aggregate(bars: Bar[], minutes: number): Bar[] {
  if (minutes <= 1) return bars;
  const size = minutes * 60;
  const out: Bar[] = [];
  for (const b of bars) {
    const t = Math.floor(b.t / size) * size;
    const last = out[out.length - 1];
    if (last && last.t === t) {
      last.h = Math.max(last.h, b.h);
      last.l = Math.min(last.l, b.l);
      last.c = b.c;
      last.v += b.v; last.b += b.b; last.s += b.s; last.n += b.n;
    } else {
      out.push({ ...b, t });
    }
  }
  return out;
}

/** 簡單移動平均；前 n−1 根是 null。 */
export function sma(closes: number[], n: number): (number | null)[] {
  const out: (number | null)[] = [];
  let sum = 0;
  for (let i = 0; i < closes.length; i++) {
    sum += closes[i];
    if (i >= n) sum -= closes[i - n];
    out.push(i >= n - 1 ? sum / n : null);
  }
  return out;
}

/** 依時間空檔切成場次。 */
export function splitSessions(bars: Bar[], gapSec: number = SESSION_GAP_SEC): Bar[][] {
  const out: Bar[][] = [];
  for (const b of bars) {
    const cur = out[out.length - 1];
    if (cur && b.t - cur[cur.length - 1].t < gapSec) cur.push(b);
    else out.push([b]);
  }
  return out;
}

/** 每個場次內累計的 VWAP（用 (高+低+收)/3 當代表價、成交口數加權——是用 1 分 K 估的，不是交易所的 VWAP）；沒有成交量的棒沿用前一個值。 */
export function vwap(bars: Bar[], gapSec: number = SESSION_GAP_SEC): (number | null)[] {
  const out: (number | null)[] = [];
  let pv = 0, vol = 0, prevT = -Infinity;
  for (const b of bars) {
    if (b.t - prevT >= gapSec) { pv = 0; vol = 0; }
    prevT = b.t;
    pv += ((b.h + b.l + b.c) / 3) * b.v;
    vol += b.v;
    out.push(vol > 0 ? pv / vol : null);
  }
  return out;
}

export interface Levels {
  prevClose: number | null;   // 上一個場次的最後一個收盤
  open: number | null;        // 目前場次第一根的開盤
  high: number | null;        // 目前場次的最高
  low: number | null;
}

/** 目前（最後一個）場次的開、高、低，以及上一個場次的收盤。 */
export function sessionLevels(bars: Bar[], gapSec: number = SESSION_GAP_SEC): Levels {
  const s = splitSessions(bars, gapSec);
  const cur = s[s.length - 1];
  if (!cur) return { prevClose: null, open: null, high: null, low: null };
  const prev = s[s.length - 2];
  return {
    prevClose: prev ? prev[prev.length - 1].c : null,
    open: cur[0].o,
    high: Math.max(...cur.map((b) => b.h)),
    low: Math.min(...cur.map((b) => b.l)),
  };
}

export interface PriceLine {
  price: number;
  title: string;
  kind: "entry" | "stop" | "take";
}

/** 期貨持倉（前綴 TMF…）的均價線，以及對應的停損／停利價（用手動停損監看登記的進場價與點數，與後端監看的算法相同）。選擇權不畫。 */
export function positionLines(positions: Position[] | null | undefined, watches: Watch[] | null | undefined, prefix = "TMF"): PriceLine[] {
  const out: PriceLine[] = [];
  for (const p of positions || []) {
    if (!(p.quantity > 0) || !p.code.startsWith(prefix) || !(p.price > 0)) continue;
    const buy = p.direction === "Buy";
    out.push({ price: p.price, kind: "entry", title: `持倉 ${buy ? "多" : "空"} ${p.quantity} 口` });
    for (const w of watches || []) {
      if (w.is_option || w.direction !== p.direction) continue;
      const mc = w.match_code || w.contract || "";
      if (!mc || !p.code.startsWith(mc)) continue;
      const d = buy ? 1 : -1;
      if (w.stop_loss_pts > 0) out.push({ price: w.entry_price - d * w.stop_loss_pts, kind: "stop", title: `停損 ${w.stop_loss_pts} 點` });
      if (w.take_profit_pts > 0) out.push({ price: w.entry_price + d * w.take_profit_pts, kind: "take", title: `停利 ${w.take_profit_pts} 點` });
      break;
    }
  }
  return out;
}

export const MA_PERIODS = [5, 20, 60] as const;
export const INTERVALS = [1, 5, 15] as const;
export type Interval = (typeof INTERVALS)[number];

export interface Toggles {
  ma5: boolean;
  ma20: boolean;
  ma60: boolean;
  vwap: boolean;
  levels: boolean;
  position: boolean;
}

export const DEFAULT_TOGGLES: Toggles = { ma5: false, ma20: true, ma60: false, vwap: true, levels: true, position: true };

/** 從 localStorage 讀回來的字串還原成設定；壞掉或缺欄位的用預設。 */
export function parseToggles(raw: string | null): { interval: Interval; toggles: Toggles } {
  const base = { interval: 1 as Interval, toggles: { ...DEFAULT_TOGGLES } };
  if (!raw) return base;
  try {
    const o = JSON.parse(raw);
    const interval = (INTERVALS as readonly number[]).includes(o?.interval) ? (o.interval as Interval) : 1;
    const toggles = { ...DEFAULT_TOGGLES };
    for (const k of Object.keys(DEFAULT_TOGGLES) as (keyof Toggles)[]) {
      if (typeof o?.toggles?.[k] === "boolean") toggles[k] = o.toggles[k];
    }
    return { interval, toggles };
  } catch {
    return base;
  }
}
