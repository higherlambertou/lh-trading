// 依波動的建議停損停利（後端 core/stop_suggest.py、GET /api/market/suggest；update.md 發現 33）的型別與顯示用的純函式。
// 欄位名稱要和後端一致（tests/test_frontend_stop_suggest.py 用後端現算的資料渲染，欄位改了沒跟著改會被抓到）。

export const HORIZONS = [5, 15, 30, 60] as const;
export type Horizon = (typeof HORIZONS)[number];

export interface SuggestRow {
  name: string;
  k_tp: number; k_sl: number;
  tp: number; sl: number;
  ratio: number;                 // 停利÷停損
  p0: number;                    // 沒有技巧、持有到碰到一邊為止的勝率＝停損÷(停利＋停損)
  need: number;                  // 要比基準多贏（機率差）才打平成本＝成本÷(停利＋停損)
  loss_twd: number;              // 單口停損虧多少元
  loss_pct: number | null;       // 占權益 %
  max_lots: number | null;       // 單筆風險上限下最多幾口
  far_tp: boolean;               // 停利比今天預期剩餘振幅的上緣還遠
  touch_sl?: number;             // 停損被雜訊掃到的機率（實測）
  touch_unstable?: boolean;
  win_unstable?: boolean;
  within?: { tp: number; sl: number; none: number };    // T 分鐘內停利先到／停損先到／都沒到（沒有技巧時，實測）
}

export interface Suggest {
  contract: string;
  session: "day" | "night";
  horizon: number;
  sigma: number | null;          // 點／分鐘
  n_changes: number;
  noise?: number;                // σ√T
  cost_pts: number;
  risk_pct: number;
  point_value: number;
  equity: number | null;
  calibrated: boolean;
  cal?: { sessions: number | null; n: number; few: boolean; from: string | null; to: string | null } | null;
  today?: { r20: number | null; early: number | null; done: number | null; expect_lo: number | null; expect_mid: number | null; expect_hi: number | null } | null;
  rows: SuggestRow[];
  note?: string;
  as_of: number;
}

const ok = (x: number | null | undefined): x is number => x != null && Number.isFinite(x);

// 0.6024 → 「60%」；缺值 → 「—」
export const pct = (x: number | null | undefined, digits = 0): string => (ok(x) ? `${(x * 100).toFixed(digits)}%` : "—");

export const money = (x: number | null | undefined): string => (ok(x) ? Math.round(x).toLocaleString("en-US") : "—");

// 0.0488 → 「4.9」（個百分點）
export const needPts = (need: number | null | undefined): string => (ok(need) ? (need * 100).toFixed(1) : "—");

// 單筆風險限額下最多幾口
export function lotsText(r: SuggestRow, riskPct: number): string {
  if (r.max_lots == null) return "";
  return r.max_lots >= 1 ? `${riskPct}% 風險最多 ${r.max_lots} 口` : `1 口就超過 ${riskPct}% 風險`;
}

export const sessionText = (s: string): string => (s === "night" ? "夜盤" : "日盤");

// 校準資料的說明：幾個時段、幾筆、樣本少
export function calText(c: Suggest["cal"], session: string): string {
  if (!c) return "";
  return `${c.sessions ?? "?"} 個${sessionText(session)}時段、${c.n.toLocaleString("en-US")} 筆${c.few ? "（樣本少）" : ""}`;
}

// 後端是舊版（沒有這個 API）時 FastAPI 回 404 的 "Not Found"
export const friendlySuggestError = (msg: string): string => (/^Not Found$/i.test(msg) ? "後端還沒有這個 API（要重啟後端才有）" : msg);
