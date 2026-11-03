// 價量分布（update.md 發現 77）：走勢圖右側「每個價位的成交量」與價位線。純函式、不碰 DOM；畫在圖上的部分在 lib/profilePrimitive.ts。
// 資料來自 GET /api/market/profile（後端把逐筆成交依價格彙總，只讀本機資料庫、不對券商發查詢）。只顯示、不下單。

export interface ProfileRow {
  price: number;       // 這一格的下緣（格寬 pbin 點）
  buy: number;         // 外盤口數（買方主動）
  sell: number;        // 內盤口數（賣方主動）
  other: number;       // 無法判定（開盤那一筆之類）
  total: number;
}

export interface ProfilePrev {
  date: string;
  session: string;
  label: string;       // 例：「11/2 日盤」
  code: string;
  note: string | null; // 換月時說明價位可能差一個價差
  poc: number | null;
  vah: number | null;
  val: number | null;
  total: number;
  high: number | null;
  low: number | null;
  last: number | null;
}

export interface ProfileData {
  code: string | null;
  prefix: string;
  session: string;     // day／night
  date: string | null;
  label: string | null;
  pbin: number;
  complete: boolean;   // 時段已經收完
  rows: ProfileRow[];  // 由低價到高價
  total: number;
  buy: number;
  sell: number;
  other: number;
  poc: number | null;  // 量最大那一格的中心
  vah: number | null;  // 價值區（涵蓋 70% 成交量）的上緣
  val: number | null;  // 價值區的下緣
  open: number | null;
  high: number | null;
  low: number | null;
  last: number | null;
  prev: ProfilePrev | null;   // 前一個已收完的日盤
  note: string | null;        // 沒有資料時的原因
  as_of: number;
}

export const BUY_COLOR = "#ff5a5f";      // 外盤（買方主動）：紅，台股慣例（漲＝紅）
export const SELL_COLOR = "#2ecc71";     // 內盤（賣方主動）：綠
export const OTHER_COLOR = "#8a8aa8";    // 無法判定
export const POC_COLOR = "#ffd400";
export const VA_COLOR = "#7fe0ff";
export const POLL_MS = 15000;
export const MAX_FRAC = 0.3;             // 最寬的那一格占圖寬的比例（太寬會蓋住 K 棒）
export const MAX_PX = 220;

export interface Rect {
  top: number;         // 像素：這一格的上緣（價格高的那邊）
  height: number;      // 至少 1 像素
  width: number;       // 這一格總寬（量最大的那格 ＝ maxWidth）
  sell: number;        // 各段寬度（加起來 ＝ width）
  buy: number;
  other: number;
  inValue: boolean;    // 在價值區裡
  poc: boolean;        // POC 在這一格
}

/** 把每一格換成畫面上的長方形。priceToY＝圖的價格軸（價格 → 像素；不在畫面上回 null，那一格就不畫）。寬度依量占最大那一格的比例，最寬 maxWidth。 */
export function profileBars(
  rows: ProfileRow[], pbin: number, priceToY: (p: number) => number | null, maxWidth: number,
  lv: { poc: number | null; vah: number | null; val: number | null } = { poc: null, vah: null, val: null },
): Rect[] {
  const max = rows.reduce((m, r) => Math.max(m, r.total), 0);
  if (!(max > 0) || !(maxWidth > 0) || !(pbin > 0)) return [];
  const out: Rect[] = [];
  for (const r of rows) {
    if (!(r.total > 0)) continue;
    const a = priceToY(r.price + pbin), b = priceToY(r.price);
    if (a == null || b == null || !Number.isFinite(a) || !Number.isFinite(b)) continue;
    const width = (r.total / max) * maxWidth, k = width / r.total;
    out.push({
      top: Math.min(a, b), height: Math.max(Math.abs(b - a), 1), width,
      sell: r.sell * k, buy: r.buy * k, other: r.other * k,
      inValue: lv.val != null && lv.vah != null && r.price >= lv.val && r.price + pbin <= lv.vah,
      poc: lv.poc != null && lv.poc >= r.price && lv.poc < r.price + pbin,
    });
  }
  return out;
}

export interface LevelLine {
  price: number;
  title: string;
  color: string;
  style: number;       // lightweight-charts 的 LineStyle：0 實線、2 虛線
}

/** 圖上的價位線：前一個日盤的 POC／價值區上下緣（虛線）＋現在這個時段的 POC（實線）。夜盤時「前一個日盤」就是同一天剛收的日盤，標「日」，否則標「昨」。 */
export function levelLines(p: ProfileData | null): LevelLine[] {
  if (!p) return [];
  const out: LevelLine[] = [];
  const add = (price: number | null | undefined, title: string, color: string, style: number) => {
    if (price != null && Number.isFinite(price) && price > 0) out.push({ price, title, color, style });
  };
  if (p.prev) {
    const tag = p.prev.date === p.date ? "日" : "昨";
    add(p.prev.poc, `${tag}POC`, POC_COLOR, 2);
    add(p.prev.vah, `${tag}VAH`, VA_COLOR, 2);
    add(p.prev.val, `${tag}VAL`, VA_COLOR, 2);
  }
  add(p.poc, `${p.session === "night" ? "夜" : "今"}POC`, POC_COLOR, 0);
  return out;
}

const num = (x: number | null | undefined) => (x == null ? "—" : Math.round(x).toLocaleString("en-US"));
const pct = (x: number, total: number) => (total > 0 ? `${Math.round((x / total) * 100)}%` : "—");

/** 圖下面那一行說明：時段、POC、價值區、內外盤占比，以及前一個日盤的價位；沒有資料時寫原因。 */
export function captionText(p: ProfileData | null, err: string | null = null): string {
  if (err) return err;
  if (!p) return "價量分布載入中…";
  if (!p.rows.length) return p.note || `${p.label ?? "這個時段"}還沒有成交`;
  let s = `價量分布 ${p.label}${p.complete ? "" : "（進行中）"}，每格 ${p.pbin} 點：POC ${num(p.poc)}　價值區 ${num(p.val)}～${num(p.vah)}　總量 ${num(p.total)} 口（外盤 ${pct(p.buy, p.total)}／內盤 ${pct(p.sell, p.total)}）`;
  if (p.prev) {
    s += `｜${p.prev.label}：POC ${num(p.prev.poc)}　價值區 ${num(p.prev.val)}～${num(p.prev.vah)}`;
    if (p.prev.note) s += `（${p.prev.note}）`;
  } else {
    s += "｜找不到前一個日盤";
  }
  return s;
}
