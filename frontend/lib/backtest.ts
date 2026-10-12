// 回測面板用的型別與純函式（沒有 React、沒有 fetch，方便測試）。
// 資料格式對應後端 core/backtest.py 的 analyze() 與 api/routes_backtest.py。

export interface BacktestRequest {
  strategy: string;
  cost: number;
  split: number;
  null: number;
  walk_forward: string | null;
  top: number;
  source: "bars" | "flow";
  session: "day" | "night" | "all";
}

export interface BtWindow {
  days: number; trades: number; perday: number; sd: number; t: number;
  pertrade: number; gross_pertrade: number; winrate: number;
}
export interface BtRow { label: string; is: BtWindow; oos: BtWindow; all: BtWindow }
export interface BtWalkForward {
  days: number; perday: number; t: number; trades: number; positive_windows: number; windows: number;
  distinct_picks: number; train: number; test: number; picks: [string, string][];
}
export interface BtNull { best_is_t: number; oos_t_of_best: number; best_all_t: number; wf_t?: number; wf_perday?: number }
export interface BacktestReport {
  strategy: string; combos: number; days: number; first: string; last: string;
  split: { is_days: number; oos_days: number; oos_from: string };
  cost: number; median_gross_pertrade: number; median_net_pertrade: number;
  positive_all: number; is_positive: number; is_positive_oos_positive: number;
  best_all_t: number; best_is_t: number; mde_perday: number;
  rows: BtRow[]; walk_forward?: BtWalkForward; null?: BtNull[];
}
export interface BacktestJob {
  id: string; status: "running" | "done" | "failed" | "cancelled"; request: BacktestRequest;
  started_at: number; elapsed_sec: number; progress: { done: number; total: number };
  error: string; log_tail: string[]; has_reports: boolean; reports?: BacktestReport[] | null;
}
export interface BacktestInfo {
  bars: { code: string; days: number; first: string | null; last: string | null };
  flow: { code: string; minutes: number; first_ts: number | null; last_ts: number | null };
  workers: number; timeout_sec: number; error?: string;
}

// ── 表單 ──────────────────────────────────────────────────────────

export const BT_STRATEGIES: { id: string; label: string }[] = [
  { id: "orb", label: "orb（開盤區間突破）" },
  { id: "vwap_revert", label: "vwap_revert（VWAP 回歸）" },
  { id: "ma_cross", label: "ma_cross（均線交叉，分鐘版）" },
  { id: "breakout", label: "breakout（區間突破，分鐘版）" },
  { id: "rsi", label: "rsi（分鐘版）" },
  { id: "bollinger", label: "bollinger（分鐘版）" },
  { id: "momentum", label: "momentum（分鐘版）" },
  { id: "all", label: "全部策略（較久）" },
];

export interface BtForm {
  strategy: string; cost: string; split: string; nullN: string;
  wf: boolean; wfTrain: string; wfTest: string;
  source: "bars" | "flow"; session: "day" | "night" | "all"; top: string;
}

export const DEFAULT_FORM: BtForm = {
  strategy: "orb", cost: "4", split: "0.6", nullN: "0", wf: false, wfTrain: "120", wfTest: "20",
  source: "bars", session: "day", top: "3",
};

const num = (s: string): number => (s.trim() === "" ? NaN : Number(s));
const isInt = (x: number) => Number.isFinite(x) && Number.isInteger(x);

export function buildRequest(f: BtForm): { ok: true; req: BacktestRequest } | { ok: false; error: string } {
  const cost = num(f.cost), split = num(f.split), nul = num(f.nullN), top = num(f.top);
  if (!Number.isFinite(cost) || cost < 0 || cost > 50) return { ok: false, error: "每筆成本要在 0～50 點之間" };
  if (!Number.isFinite(split) || split < 0.3 || split > 0.9) return { ok: false, error: "樣本內比例要在 0.3～0.9 之間" };
  if (!isInt(nul) || nul < 0 || nul > 12) return { ok: false, error: "打亂資料對照的組數要是 0～12 的整數" };
  if (!isInt(top) || top < 1 || top > 10) return { ok: false, error: "顯示前幾名要是 1～10 的整數" };
  let wf: string | null = null;
  if (f.wf) {
    const a = num(f.wfTrain), b = num(f.wfTest);
    if (!isInt(a) || a < 20 || a > 400) return { ok: false, error: "滾動式驗證的訓練天數要是 20～400 的整數" };
    if (!isInt(b) || b < 5 || b > 100) return { ok: false, error: "滾動式驗證的測試天數要是 5～100 的整數" };
    if (b >= a) return { ok: false, error: "測試天數要小於訓練天數" };
    wf = `${a},${b}`;
  }
  if (f.source === "bars" && f.session !== "day") return { ok: false, error: "日盤 K 棒資料只有日盤；要夜盤請改用 TMF 成交彙總" };
  return { ok: true, req: { strategy: f.strategy, cost, split, null: nul, walk_forward: wf, top, source: f.source, session: f.session } };
}

// ── 顯示 ──────────────────────────────────────────────────────────

export const fmtPts = (x: number | null | undefined, d = 1): string =>
  x == null || !Number.isFinite(x) ? "—" : `${x >= 0 ? "+" : ""}${x.toFixed(d)}`;
export const fmtT = (x: number | null | undefined): string => fmtPts(x, 2);
export const fmtPct = (x: number | null | undefined): string => (x == null || !Number.isFinite(x) ? "—" : `${Math.round(x * 100)}%`);

export type VerdictLevel = "green" | "gray" | "red";

/** 結論只看「滾動式驗證」（每次只用過去的天數挑參數、再跑接下來的天數）——樣本內最佳本來就會被高估，不能拿來下結論。
 *  刻意保守：綠色也只是「有一點線索」，不是上線依據。 */
export function verdict(r: BacktestReport): { level: VerdictLevel; text: string } {
  const wf = r.walk_forward;
  if (!wf) return { level: "gray", text: "還沒做滾動式驗證——勾選「滾動式驗證」後重跑，先別下結論" };
  const nulls = (r.null ?? []).map((x) => x.wf_t).filter((x): x is number => typeof x === "number");
  const head = `滾動式驗證樣本外 ${fmtPts(wf.perday)} 點/天（t=${fmtT(wf.t)}，${wf.days} 天）`;
  const nullTxt = nulls.length ? `；打亂資料（沒有技巧）t 介於 ${fmtT(Math.min(...nulls))} ~ ${fmtT(Math.max(...nulls))}` : "（沒做打亂資料對照）";
  if (wf.t <= -2) return { level: "red", text: `${head}${nullTxt}：樣本外顯著為負——用過去挑出的參數去交易，平均是虧的` };
  const beatsNull = nulls.length > 0 && wf.t > Math.max(...nulls);
  if (wf.t >= 2 && wf.perday > 0 && beatsNull) {
    return { level: "green", text: `${head}${nullTxt}：有一點線索，但這不是上線依據，要用新的資料再驗證一次` };
  }
  return { level: "gray", text: `${head}${nullTxt}：看不出比亂數強` };
}

export function topRows(r: BacktestReport, n: number): BtRow[] {
  return [...r.rows].sort((a, b) => b.is.t - a.is.t).slice(0, n);
}
