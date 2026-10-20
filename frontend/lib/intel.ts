// 〈資訊〉面板（後端 core/intel_reader.py、GET /api/intel/summary）的型別與顯示用的純函式。資料來自另一個 repo（lh-intel）的 intel.db，唯讀、只顯示、不影響交易。
// 欄位名稱要和後端一致（tests/test_frontend_intel.py 用後端現算的資料渲染，欄位改了沒跟著改會被抓到）。

export interface IntelMarket {
  key: string;
  label: string;
  source: string;
  unit: string;                       // points／usd／pct／twd／index
  kind: "price" | "rate";             // rate＝殖利率（變動用 bp）
  value: number | null;
  prev?: number | null;
  chg?: number | null;
  chg_pct?: number | null;
  date: string | null;                // 該市場當地的交易日
  prev_date?: string | null;
  age_days?: number;
}

export interface IntelEvent {
  uid: string;
  source: string;
  country: string | null;
  title: string;
  title_en: string;
  importance: number;
  scheduled_time: number | null;
  taipei_time: string | null;         // "MM/DD HH:MM"（台北）
  taipei_weekday: string | null;
  scheduled_date: string;             // 事件所在國家當地日期
  local_date: string;
  minutes_to: number | null;
  past: boolean;
}

export interface FutRow { who: string; label: string; date: string; net_oi: number | null; chg: number | null; long_oi: number | null; short_oi: number | null }
export interface OptRow { who: string; label: string; date: string; call_net_oi: number | null; call_chg: number | null; put_net_oi: number | null; put_chg: number | null }
export interface InstRow { who: string; label: string; net: number | null; net5: number | null; days5: number }

export interface IntelChips {
  futures: { commodity: string; label: string; date: string; rows: FutRow[] }[];
  options: { date: string; rows: OptRow[] } | null;
  pcr: { date: string; oi_ratio: number | null; oi_ratio_prev: number | null; vol_ratio: number | null; vol_ratio_prev: number | null } | null;
  inst: { date: string; rows: InstRow[] } | null;
  margin: { date: string; financing_100m: number | null; financing_chg_100m: number | null; short_lots: number | null; short_chg: number | null; provisional: boolean } | null;
  market: { date: string | null; turnover_100m: number | null; turnover_ratio20: number | null; turnover_days: number | null; breadth_date: string | null;
            up: number | null; down: number | null; flat: number | null; limit_up: number | null; limit_down: number | null } | null;
}

export interface IntelNews { title: string; url: string; source: string; label: string; time: number; taipei_time: string; age_min: number }
export interface IntelCollector { name: string; last_ok: number | null; last_ok_age_sec: number | null; last_status: string | null; fails: number }

export interface IntelSummary {
  available: boolean;
  reason?: string;
  as_of: number;
  schema_version?: number;
  db?: string;
  events?: { upcoming: IntelEvent[]; closures: { date: string; title: string; days_to: number }[] };
  chips?: IntelChips;
  markets?: IntelMarket[];
  news?: IntelNews[];
  collectors?: { heartbeat_age_sec: number | null; service_ok: boolean; items: IntelCollector[]; problems: string[] };
}

// 台灣習慣：漲＝紅、跌＝綠（與報價列、走勢圖一致）
export const UP = "#ff5252", DOWN = "#00e676", MUTED = "#7070a0", WARN = "#ffc107";

export function fmt(n: number | null | undefined, nd = 2): string {
  return n == null || !Number.isFinite(n) ? "—" : n.toLocaleString("zh-TW", { minimumFractionDigits: nd, maximumFractionDigits: nd });
}

export function signed(n: number | null | undefined, nd = 2): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const s = fmt(Math.abs(n), nd);
  return n > 0 ? `+${s}` : n < 0 ? `−${s}` : s;
}

export const upDown = (n: number | null | undefined): string => (n == null || n === 0 ? MUTED : n > 0 ? UP : DOWN);

export function marketValue(m: IntelMarket): string {
  if (m.value == null) return "—";
  if (m.unit === "usd") return `$${fmt(m.value)}`;
  if (m.unit === "pct") return `${fmt(m.value)}%`;
  return fmt(m.value);
}

/** 與前一個交易日比：價格類「+3.53（+21.45%）」，殖利率類「−9 bp」。沒有前一天就「—」。 */
export function marketChange(m: IntelMarket): string {
  if (m.chg == null) return "—";
  if (m.kind === "rate") return `${signed(m.chg * 100, 0)} bp`;
  return m.chg_pct == null ? signed(m.chg) : `${signed(m.chg)}（${signed(m.chg_pct)}%）`;
}

/** "10/16" ＋（資料超過 3 天沒更新時）「・5 天前」；沒有日期是「沒有資料」。 */
export function marketDate(m: IntelMarket): string {
  if (!m.date) return "沒有資料";
  const d = m.date.slice(5).replace("-", "/");
  return m.age_days != null && m.age_days > 3 ? `${d}・${m.age_days} 天前` : d;
}

export const stars = (n: number): string => "★".repeat(Math.max(0, Math.min(3, n)));

/** 事件倒數：有時間的用台北時間算；只有日期的寫「當天」。 */
export function countdown(e: IntelEvent): string {
  const m = e.minutes_to;
  if (m == null) return "（只知道日期）";
  if (m < 0) return m > -180 ? `已過 ${Math.abs(m)} 分鐘` : "已過";
  if (m < 60) return `${m} 分鐘後`;
  if (m < 48 * 60) return `${Math.floor(m / 60)} 小時後`;
  return `${Math.floor(m / 1440)} 天後`;
}

export const eventWhen = (e: IntelEvent): string => (e.taipei_time ? `${e.taipei_time}（${e.taipei_weekday}）` : `${e.local_date.slice(5).replace("-", "/")}（美東日期）`);

export function newsAge(min: number): string {
  if (min < 1) return "剛剛";
  if (min < 60) return `${min} 分鐘前`;
  return min < 48 * 60 ? `${Math.floor(min / 60)} 小時前` : `${Math.floor(min / 1440)} 天前`;
}

export type Light = "ok" | "warn" | "bad";
export const LIGHT_COLOR: Record<Light, string> = { ok: "#00e676", warn: WARN, bad: "#ff1744" };

/** 收集器的燈：最近一次失敗＝紅；從沒成功過＝黃；其他綠。 */
export function collectorLight(c: IntelCollector): Light {
  if (c.fails >= 1) return "bad";
  return c.last_ok == null ? "warn" : "ok";
}

/** 整個資訊系統的燈：沒有心跳（服務沒在跑）＝紅；有收集器失敗＝黃；否則綠。 */
export function systemLight(c: NonNullable<IntelSummary["collectors"]>): Light {
  if (!c.service_ok) return "bad";
  return c.problems.length ? "warn" : "ok";
}

export function systemText(c: NonNullable<IntelSummary["collectors"]>): string {
  if (c.heartbeat_age_sec == null) return "資訊服務沒有心跳（沒在跑？）";
  if (!c.service_ok) return `資訊服務 ${Math.round(c.heartbeat_age_sec / 60)} 分鐘沒有心跳`;
  return c.problems.length ? `資訊服務在跑，但 ${c.problems.join("、")} 最近失敗` : "資訊服務正常";
}

export function ageText(sec: number | null | undefined): string {
  if (sec == null) return "從沒成功";
  if (sec < 90) return "剛剛";
  if (sec < 3600) return `${Math.round(sec / 60)} 分鐘前`;
  return sec < 172800 ? `${Math.round(sec / 3600)} 小時前` : `${Math.round(sec / 86400)} 天前`;
}

export const friendlyIntelError = (msg: string): string => (/^Not Found$/i.test(msg) ? "後端還沒有這個 API（〈資訊〉是新功能，要重啟後端才有）" : msg);
