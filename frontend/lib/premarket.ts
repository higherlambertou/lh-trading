// 盤前跨市場快照（後端 core/premarket_log.py、api/routes_premarket.py）的型別與顯示用的純函式。
// 欄位名稱要和後端一致（tests/test_frontend_premarket.py 用後端現算的資料渲染，欄位改了沒跟著改會被抓到）。

export type PmKey = "txf_night" | "sox" | "tsm" | "vix";
export const PM_KEYS: PmKey[] = ["txf_night", "sox", "tsm", "vix"];
export const PM_LABEL: Record<PmKey, { name: string; short: string; hint: string }> = {
  txf_night: { name: "台指期夜盤", short: "夜盤", hint: "夜盤收盤 ÷ 前一個交易日日盤收盤 − 1（本機成交紀錄）" },
  sox: { name: "費半 SOX", short: "SOX", hint: "最近一個已收盤的美股交易日的報酬" },
  tsm: { name: "台積電 ADR", short: "TSM", hint: "最近一個已收盤的美股交易日的報酬" },
  vix: { name: "VIX", short: "VIX", hint: "最近一個已收盤的美股交易日的收盤值" },
};

export interface PmField {
  value: number | null;
  source: string;
  source_timestamp: number | null;     // 資料本身的時間（UTC epoch 秒）
  fetched_at: number | null;
  missing_reason: string | null;
  flag: string | null;
  detail: Record<string, number | string | boolean | null | undefined>;
}

export interface PmCalendar {
  tw_closed: boolean; tw_reason: string | null; tw_source: string;
  us_closed: boolean; us_session_date: string; us_early_close: boolean;
  prev_tw_trading_day: string; notes: string;
}

export interface PmSnapshot {
  trading_date: string;
  features: Record<string, PmField>;
  calendar: PmCalendar;
  snapshot_time_utc: number;
  fetched_at_utc: number;
  late_seconds: number;
  writer: string | null;
  voided: boolean;
  corrected: boolean;
  void_reason: string | null;
  correction_note: string | null;
}

export interface PmPreview {
  dry_run: boolean;
  recorded: boolean;
  trading_date: string;
  snapshot_time: string;
  snapshot_time_utc: number;
  fetched_at_utc: number;
  late_seconds: number;
  features: Record<string, PmField>;
  calendar: PmCalendar;
}

export interface PmStatus {
  enabled: boolean;
  snapshot_time: string;
  sources: string[];
  alerts: number;
  last_alert: { ts: number; where: string; trading_date: string; error: string } | null;
  today: string;
  today_recorded: boolean;
  missed_dates: string[];
  next_snapshot: { trading_date: string; time: string; late: boolean } | null;
  snapshots: number;
  corrections: number;
  voids: number;
  errors_logged: number;
  graveyard: number;
  latest: { trading_date: string; missing: Record<string, string | null> } | null;
}

const MINUS = "−";

// 報酬（小數）→ 「+1.23%」；null → 「—」
export function fmtPct(x: number | null | undefined, digits = 2): string {
  if (x == null || !Number.isFinite(x)) return "—";
  const r = Number((x * 100).toFixed(digits));
  return r === 0 ? `${(0).toFixed(digits)}%` : `${r > 0 ? "+" : MINUS}${Math.abs(r).toFixed(digits)}%`;
}

export function fmtLevel(x: number | null | undefined, digits = 2): string {
  return x == null || !Number.isFinite(x) ? "—" : x.toFixed(digits);
}

// 夜盤、費半、TSM 是報酬（百分比），VIX 是收盤值
export function fmtValue(key: string, f: PmField | undefined | null): string {
  if (!f || f.value == null) return "—";
  return key === "vix" ? fmtLevel(f.value) : fmtPct(f.value);
}

// 缺值原因（後端的 missing_reason）→ 白話；沒看懂的原樣顯示，不吞掉
export function reasonText(reason: string | null | undefined): string {
  if (!reason) return "";
  const [head, ...rest] = reason.split(": ");
  const tail = rest.join(": ");
  const table: Record<string, string> = {
    us_closed: "美股休市", source_error: "來源失敗", stale_or_missing: "資料還沒更新", insufficient_history: "歷史資料不足", out_of_range: "超出異常值範圍（只標記，沒有修正）",
    no_ticks: "本機沒有那段成交紀錄", incomplete_session: "夜盤或日盤的紀錄不完整（後端那段時間沒開？）", ticks_db_unavailable: "本機成交資料庫讀不到",
    night_not_finished: "夜盤還沒收盤", bad_price: "價格異常", non_finite: "數值異常",
  };
  const base = table[head];
  if (!base) return reason;
  return head === "us_closed" || head === "stale_or_missing" || head === "source_error" ? `${base}：${tail.slice(0, 80)}` : base;
}

const TPE_FMT = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Taipei", hourCycle: "h23", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });   // h23：午夜是 00（hour12:false 在部分瀏覽器會變 24）

// UTC epoch 秒 → 台北時間「MM-DD HH:mm」；時間一律用 Asia/Taipei，不跟著瀏覽器時區跑
export function tpeTime(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec)) return "—";
  const p = Object.fromEntries(TPE_FMT.formatToParts(new Date(sec * 1000)).map((x) => [x.type, x.value]));
  return `${p.month}-${p.day} ${p.hour}:${p.minute}`;
}

export const lateText = (s: number | null | undefined): string => (s != null && s >= 60 ? `補記（晚了 ${Math.round(s / 60)} 分鐘）` : "");

// 下一筆：「10-14 08:30」；今天補記的標示「（現在就會補記）」
export function nextText(n: PmStatus["next_snapshot"] | undefined): string {
  if (!n) return "—";
  const m = /^\d{4}-(\d{2})-(\d{2})T(\d{2}:\d{2})/.exec(n.time);
  return m ? `${m[1]}-${m[2]} ${m[3]}${n.late ? "（現在就會補記）" : ""}` : n.time;
}

// 這一筆用的美股是哪一天；休市時說明為什麼三個美股欄位是空的
export function usSessionText(c: PmCalendar | null | undefined): string {
  if (!c) return "—";
  if (c.us_closed) return `美股 ${c.us_session_date} 休市${c.notes ? `（${c.notes}）` : ""}，三個美股欄位留空`;
  return `用的美股是 ${c.us_session_date} 收盤${c.us_early_close ? "（提早收盤）" : ""}`;
}

// 台股日曆的來源（後端 TwCalendar.source）
export function twSourceText(src: string | null | undefined): string {
  if (src === "twse") return "證交所";
  if (src === "twse_stale_cache") return "證交所（舊快取，這次沒抓到新的）";
  if (src === "fallback_weekday") return "只依週一至週五推算（證交所日曆抓不到）";
  return src || "—";
}

// 這筆紀錄的更正是不是「自動補值」（來源比 T 晚更新，後來才補進去的；後端 BACKFILL_NOTE）
export const isBackfill = (r: Pick<PmSnapshot, "correction_note">): boolean => (r.correction_note ?? "").startsWith("自動補值");

// 這個欄位是事後補進去的嗎？是的話回傳補值的時間（UTC epoch 秒）
export function backfilledAt(f: PmField | undefined | null): number | null {
  const t = f?.detail?.backfilled_at;
  return f?.detail?.backfilled === true && typeof t === "number" && Number.isFinite(t) ? t : null;
}

// 一筆紀錄缺幾個欄位
export const missingCount = (features: Record<string, PmField>): number => PM_KEYS.filter((k) => features[k]?.value == null).length;

// 狀態晶片：[文字, 顏色]
export function statusChips(s: PmStatus): [string, string][] {
  const GREEN = "#00e676", MUTED = "#7070a0", YELLOW = "#ffc107";
  const out: [string, string][] = [[s.enabled ? "記錄中" : "已關閉", s.enabled ? GREEN : MUTED], [`T ${s.snapshot_time}`, MUTED], [`已記 ${s.snapshots} 筆`, s.snapshots > 0 ? GREEN : MUTED]];
  out.push([s.today_recorded ? "今天已記" : "今天還沒記", s.today_recorded ? GREEN : MUTED]);
  if (s.alerts > 0) out.push([`失敗警示 ${s.alerts}`, YELLOW]);
  if (s.missed_dates.length > 0) out.push([`錯過 ${s.missed_dates.length} 天`, YELLOW]);
  return out;
}
