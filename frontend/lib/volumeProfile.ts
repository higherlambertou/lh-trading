// 價量分布（update.md 發現 77）：走勢圖右側「每個價位的成交量」與價位線。純函式、不碰 DOM；畫在圖上的部分在 lib/profilePrimitive.ts。
// 資料來自 GET /api/market/profile（後端把逐筆成交依價格彙總成每 1 點一列，只讀本機資料庫、不對券商發查詢）。只顯示、不下單。

export interface ProfileRow {
  price: number;       // 這一列的下緣（格寬 row_bin 點，目前是 1 點）
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
  pbin: number;        // POC 與價值區用的格寬（點）
  row_bin: number;     // rows 的格寬（點）
  complete: boolean;   // 時段已經收完
  rows: ProfileRow[];  // 由低價到高價
  total: number;
  buy: number;
  sell: number;
  other: number;
  poc: number | null;  // 量最大那一格（pbin 點一格）的中心
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

// 配色：深色底上柔和一點（飽和的紅綠會和 K 棒搶戲）；台股慣例外盤＝紅、內盤＝綠
export const BUY_COLOR = "#e0636a";
export const SELL_COLOR = "#3fb58a";
export const POC_COLOR = "#ffd54a";
export const VA_COLOR = "#7fd6f5";
export const ALPHA_OUT = 0.42;           // 價值區外的格子（和圖背景混出來的實心色，不是半透明：半透明會透出格線，直條上多出幾條細線）
export const ALPHA_IN = 0.74;            // 價值區裡的格子
export const PANE_BG = "#141420";        // 圖的背景色（components/PriceChart.tsx 的 layout.background）
export const POLL_MS = 15000;
export const MAX_FRAC = 0.22;            // 最寬的那一格占圖寬的比例
export const MAX_PX = 200;
export const MIN_ROW_PX = 7;             // 每一格至少這麼高（再細就變成一排髮絲；7 像素以上才像一座山）
export const PAD_FRAC = 0.36;            // 圖右邊留白（占顯示根數的比例）：價量分布和它的標籤畫在這塊空白裡，不蓋住 K 棒
export const LABEL_GAP = 6;              // 標籤和價量分布、和最新一根 K 棒之間至少隔幾像素
export const NICE_STEPS = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000];

/** 前景色以 alpha 疊在背景色上的實心色（"#rrggbb" → "rgb(r,g,b)"）。 */
export function blend(fg: string, alpha: number, bg: string = PANE_BG): string {
  const rgb = (h: string) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const f = rgb(fg), b = rgb(bg);
  return `rgb(${f.map((v, i) => Math.round(v * alpha + b[i] * (1 - alpha))).join(", ")})`;
}

export const BAR_COLORS = {
  in: { buy: blend(BUY_COLOR, ALPHA_IN), sell: blend(SELL_COLOR, ALPHA_IN) },        // 價值區裡：濃
  out: { buy: blend(BUY_COLOR, ALPHA_OUT), sell: blend(SELL_COLOR, ALPHA_OUT) },     // 價值區外：淡
};

/** 合併成幾點一格：依「每點幾像素」挑，讓每一格至少 minPx 高；從 NICE_STEPS 裡挑夠用的最小的一個。算不出來（沒有座標）回 1。 */
export function niceStep(pxPerPoint: number, minPx: number = MIN_ROW_PX): number {
  if (!(pxPerPoint > 0)) return 1;
  const need = minPx / pxPerPoint;
  return NICE_STEPS.find((s) => s >= need) ?? NICE_STEPS[NICE_STEPS.length - 1];
}

/** 把細的列合併成 step 點一格（下緣是 step 的倍數）。step 小於等於 1 就原樣。 */
export function rebin(rows: ProfileRow[], step: number): ProfileRow[] {
  if (!(step > 1)) return rows;
  const acc = new Map<number, ProfileRow>();
  for (const r of rows) {
    const b = Math.floor(r.price / step) * step;
    const a = acc.get(b) ?? { price: b, buy: 0, sell: 0, other: 0, total: 0 };
    a.buy += r.buy; a.sell += r.sell; a.other += r.other; a.total += r.total;
    acc.set(b, a);
  }
  return [...acc.values()].sort((x, y) => x.price - y.price);
}

export interface Rect {
  top: number;         // 像素：這一格的上緣（價格高的那邊）
  height: number;      // 至少 1 像素
  width: number;       // 這一格總寬（畫面上看得到的最長那格 ＝ maxWidth）
  sell: number;        // 各段寬度（內盤＋外盤 ＝ width；判定不出內外盤的量不畫，只占說明列的總數）
  buy: number;
  inValue: boolean;    // 在價值區裡
  poc: boolean;        // 量最大的一格（POC）
}

/**
 * 把每一格換成畫面上的長方形。priceToY＝圖的價格軸（價格 → 像素；不在畫面上回 null，那一格就不畫）。
 * 寬度（內盤＋外盤）依「畫面上看得到的格子」裡最長的一格正規化（和 TradingView 的 VPVR 一樣：放大縮小時永遠看得清楚），最寬 maxWidth；
 * height（畫面高度）有給的話，完全在畫面外的格子不算也不畫。
 * poc（外框）標在「整段裡最長的那一格」（格子比後端算 POC 的格寬粗時，後端 POC 那一格不一定是最長的；一樣長取離後端 POC 最近的）。
 */
export function profileBars(
  rows: ProfileRow[], step: number, priceToY: (p: number) => number | null, maxWidth: number,
  lv: { poc: number | null; vah: number | null; val: number | null } = { poc: null, vah: null, val: null }, height?: number,
): Rect[] {
  if (!(maxWidth > 0) || !(step > 0)) return [];
  const len = (r: ProfileRow) => r.buy + r.sell;
  const dist = (r: ProfileRow) => (lv.poc == null ? 0 : Math.abs(r.price + step / 2 - lv.poc));
  let peak: ProfileRow | null = null;
  for (const r of rows) {
    if (peak == null || len(r) > len(peak) || (len(r) === len(peak) && dist(r) < dist(peak))) peak = r;
  }
  const geo: { r: ProfileRow; top: number; h: number }[] = [];
  for (const r of rows) {
    if (!(len(r) > 0)) continue;
    const a = priceToY(r.price + step), b = priceToY(r.price);
    if (a == null || b == null || !Number.isFinite(a) || !Number.isFinite(b)) continue;
    const top = Math.min(a, b), h = Math.max(Math.abs(b - a), 1);
    if (height != null && (top + h < 0 || top > height)) continue;
    geo.push({ r, top, h });
  }
  const max = geo.reduce((m, g) => Math.max(m, len(g.r)), 0);
  if (!(max > 0)) return [];
  return geo.map(({ r, top, h }) => {
    const k = maxWidth / max, width = len(r) * k;
    return {
      top, height: h, width, sell: r.sell * k, buy: r.buy * k,
      inValue: lv.val != null && lv.vah != null && r.price + step / 2 >= lv.val && r.price + step / 2 <= lv.vah,          // 格子的中心落在價值區裡（價值區邊緣切到半格時，不會一半淡一半濃）
      poc: r === peak,
    };
  });
}

export interface LevelLine {
  price: number;
  title: string;
  color: string;
  style: number;       // 0 實線、2 虛線
}

/** 圖上的價位線：前一個日盤的 POC／價值區上下緣（虛線）＋現在時段的 POC（實線）。夜盤時「前一個日盤」就是同一天剛收的日盤，標「日」，否則標「昨」。 */
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

/** 標籤的垂直位置：每個標籤想放在 ys[i]，但相鄰的要隔至少 gap 像素（價位很近時不會疊在一起）；先壓進 minY 以下、再由上往下推開，超過 maxY 再由下往上推回來（放不下那麼多時寧可疊在一起，也不跑出畫面）。回傳和 ys 同樣順序。 */
export function layoutLabels(ys: number[], gap: number, minY: number, maxY: number): number[] {
  const order = ys.map((y, i) => i).sort((a, b) => ys[a] - ys[b] || a - b);
  const pos = order.map((i) => Math.max(ys[i], minY));
  for (let k = 1; k < pos.length; k++) pos[k] = Math.max(pos[k], pos[k - 1] + gap);
  if (pos.length && pos[pos.length - 1] > maxY) {
    pos[pos.length - 1] = maxY;
    for (let k = pos.length - 2; k >= 0; k--) pos[k] = Math.min(pos[k], pos[k + 1] - gap);
  }
  const out = new Array<number>(ys.length);
  order.forEach((i, k) => { out[i] = Math.max(pos[k], minY); });
  return out;
}

/** 價位線標籤的樣式：room＝價量分布左邊到最新一根 K 棒之間的像素。放得下「名稱＋價格」就全放；只放得下名稱就只放名稱；再窄（手機）就不放，數字看上方說明列。 */
export function labelMode(room: number, fullW: number, shortW: number): "full" | "short" | "none" {
  return room >= fullW ? "full" : room >= shortW ? "short" : "none";          // NaN、負數、0 都比較不過 → none
}

export const priceText = (x: number): string => Math.round(x).toLocaleString("en-US");

const num = (x: number | null | undefined) => (x == null ? "—" : priceText(x));
const pct = (x: number, total: number) => (total > 0 ? `${Math.round((x / total) * 100)}%` : "—");

/** 說明列（一行）：時段、POC、價值區、內外盤占比，以及前一個日盤的價位；沒有資料時寫原因。 */
export function captionText(p: ProfileData | null, err: string | null = null): string {
  if (err) return err;
  if (!p) return "價量分布載入中…";
  if (!p.rows.length) return p.note || `${p.label ?? "這個時段"}還沒有成交`;
  let s = `價量分布 ${p.label}${p.complete ? "" : "（進行中）"}：POC ${num(p.poc)}　價值區 ${num(p.val)}～${num(p.vah)}　外盤 ${pct(p.buy, p.total)}／內盤 ${pct(p.sell, p.total)}`;
  if (p.prev) {
    s += `｜${p.prev.label}：POC ${num(p.prev.poc)}　價值區 ${num(p.prev.val)}～${num(p.prev.vah)}`;
    if (p.prev.note) s += `（${p.prev.note}）`;
  } else {
    s += "｜找不到前一個日盤";
  }
  return s;
}

/** 滑過說明列時顯示的完整解釋。 */
export const HELP_TEXT = "POC＝成交最多的價位；價值區＝涵蓋 70% 成交量的區間（VAH 上緣、VAL 下緣）。紅＝外盤（買方主動）、綠＝內盤（賣方主動）。"
  + "只顯示成交落在哪些價位，不是進場訊號；歷史檢驗（2020 年起 1,458 天）沒有發現前一日的 POC／價值區有磁吸或擋住的預測力。";
