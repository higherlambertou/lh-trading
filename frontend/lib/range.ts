// 機率範圍帶（後端 core/range_suggest.py、GET /api/market/range；update.md 發現 48）的型別與顯示用的純函式。
// 欄位名稱要和後端一致（tests/test_frontend_range.py 用後端現算的資料渲染，欄位改了沒跟著改會被抓到）。
// 只說「會走多遠」，不說往哪個方向；不是進場訊號。

export interface RangeSide {
  d: number;       // 距離現價多少點
  lo: number;
  hi: number;
}

export interface RangeLevel {
  p: number;                          // 層級：50／80／95（%）
  term: RangeSide | null;             // 落點範圍：時間到時價格落在這裡的機率＝p
  path: RangeSide | null;             // 期間範圍：這段時間內最高／最低都沒超出的機率＝p
  unstable_term: boolean;             // 前後半差太多，或樣本外驗證沒通過
  unstable_path: boolean;
}

export interface RangeBand {
  minutes: number;
  noise: number;                      // σ√T（點）
  few: boolean;                       // 實測樣本少（夜盤）
  levels: RangeLevel[];
  bucket?: string;                    // 到收盤：用的是哪一組表
}

export interface RangeCal {
  generated: string | null;
  day_sessions: number | null;
  from: string | null;
  to: string | null;
  night_sessions: number | null;
  split: string | null;
  holdout_ok: number | null;
  holdout_total: number | null;
  levels: number[];
}

export interface RangeData {
  as_of: number;
  price: number | null;
  price_ts: number | null;
  price_age_sec: number | null;
  stale: boolean;
  session: "day" | "night";
  sigma: number | null;
  n_changes: number;
  calibrated: boolean;
  remaining: number | null;           // 日盤：距收盤幾分鐘
  bands: Record<string, RangeBand>;   // "5"｜"15"｜"30"｜"60"｜"close"
  note: string | null;
  cal: RangeCal | null;
}

export type Kind = "term" | "path";
export const KINDS: { id: Kind; text: string; hint: string }[] = [
  { id: "term", text: "落點", hint: "時間到時，價格落在這個範圍內的機率＝層級" },
  { id: "path", text: "期間", hint: "這段時間內最高、最低都沒超出這個範圍的機率＝層級（停損停利碰不碰到看這個）" },
];

export const BAND_KEYS = ["5", "15", "30", "60", "close"] as const;
export type BandKey = (typeof BAND_KEYS)[number];
export const isBandKey = (x: unknown): x is BandKey => (BAND_KEYS as readonly unknown[]).includes(x);

export const bandText = (key: string): string => (key === "close" ? "到收盤" : `${key} 分`);

/** 該持有時間的範圍；沒有（沒實測表、沒波動、不在日盤等）回 null。 */
export const pickBand = (data: RangeData | null | undefined, key: string): RangeBand | null => data?.bands?.[key] ?? null;

/** 範圍的價位：下緣往下取整、上緣往上取整（整數點，寧可寬不要窄）。 */
export const edge = (v: number, which: "lo" | "hi"): number => (which === "lo" ? Math.floor(v) : Math.ceil(v));

export const fmt = (n: number): string => n.toLocaleString("zh-TW", { maximumFractionDigits: 1 });

/** 一個層級、一種範圍的文字：「29,997 ~ 30,005（±3.3）」；沒有就「—」。 */
export function sideText(s: RangeSide | null | undefined): string {
  if (!s) return "—";
  return `${fmt(edge(s.lo, "lo"))} ~ ${fmt(edge(s.hi, "hi"))}（±${fmt(s.d)}）`;
}

export interface BandLine {
  price: number;
  title: string;
  p: number;
  upper: boolean;
  unstable: boolean;
}

/** 畫在圖上的線：每個層級上下各一條，用精確價位；缺值的層級略過。title 例：「80%·15分」，不穩的後面加「?」。 */
export function bandLines(data: RangeData | null | undefined, key: string, kind: Kind, levels: readonly number[] = [50, 80, 95]): BandLine[] {
  const b = pickBand(data, key);
  if (!b) return [];
  const out: BandLine[] = [];
  const tag = key === "close" ? "收盤" : `${key}分`;
  for (const lv of b.levels) {
    const s = lv[kind];
    if (!s || !levels.includes(lv.p)) continue;
    const unstable = kind === "term" ? lv.unstable_term : lv.unstable_path;
    const title = `${lv.p}%·${tag}${unstable ? "?" : ""}`;
    out.push({ price: s.hi, title, p: lv.p, upper: true, unstable });
    out.push({ price: s.lo, title, p: lv.p, upper: false, unstable });
  }
  return out;
}

/** 價格帶資料有多舊的白話。 */
export function ageText(sec: number | null | undefined): string {
  if (sec == null) return "";
  if (sec < 90) return "剛剛";
  return sec < 3600 ? `${Math.round(sec / 60)} 分鐘前` : `${Math.round(sec / 3600)} 小時前`;
}

export const friendlyRangeError = (msg: string): string => (/^Not Found$/i.test(msg) ? "後端還沒有這個 API（要重啟後端才有）" : msg);

/** 「到收盤」是否可用（日盤、剩餘 15~270 分鐘、有表）。 */
export const hasClose = (data: RangeData | null | undefined): boolean => !!data?.bands?.close;

/** 驗證摘要：「樣本外驗證 44／48 格通過」；沒有就空字串。 */
export function holdoutText(cal: RangeCal | null | undefined): string {
  return cal && cal.holdout_total ? `樣本外驗證（${cal.split} 以前算表、以後量涵蓋率）${cal.holdout_ok}／${cal.holdout_total} 格通過` : "";
}
