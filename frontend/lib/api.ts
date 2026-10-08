// 後端位址依「載入此頁面的 host」自動推導，而不是寫死／烤進 bundle：
// 本機開 localhost:3002 → 打 localhost:8002；手機開 100.97.169.26:3002 → 打 100.97.169.26:8002。
// 一份 bundle 兩邊都正確，也避開「本機自連自己 Tailscale IP timeout」的問題。
// port 依模式：正式盤 8002、模擬盤 8003。
function apiBase(sim: boolean): string {
  const port = sim ? 8003 : 8002;
  const host = typeof window !== "undefined" ? window.location.hostname : "localhost";
  return `http://${host}:${port}/api`;
}

export function getBase(): string {
  return apiBase(isSimMode());
}

export function isSimMode(): boolean {
  return typeof window !== "undefined" && localStorage.getItem("trading_mode") === "sim";
}

export function setSimMode(sim: boolean): void {
  localStorage.setItem("trading_mode", sim ? "sim" : "prod");
}

// 下單成功後廣播此事件，讓委託/部位面板立即刷新（不必等下一輪輪詢）
export const ORDER_PLACED_EVENT = "lh:order-placed";
export function notifyOrderPlaced(): void {
  window.dispatchEvent(new Event(ORDER_PLACED_EVENT));
}

// 各合約每點價值（計算即時損益用）
export function pointValue(code: string): number {
  if (code.startsWith("TXF")) return 200;
  if (code.startsWith("MXF")) return 50;
  if (code.startsWith("TMF")) return 10;
  return 50; // TXO/週選等選擇權每點 50 元
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${getBase()}${path}`, init);
  if (!res.ok) {
    const msg = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status} — ${msg}`);
  }
  return res.json() as Promise<T>;
}

/** 把 req() 丟出的 `409 — {"detail":"…"}` 還原成後端給的那句說明（FastAPI 的錯誤主體是 JSON），不是 JSON 就原樣回傳。 */
export function apiErrorDetail(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e);
  const body = raw.replace(/^\d+ — /, "");
  try {
    const j = JSON.parse(body);
    if (j && typeof j.detail === "string") return j.detail;
  } catch { /* 不是 JSON */ }
  return body;
}

// ─── Types ────────────────────────────────────────────────────────

export interface ParamSchema {
  key: string;
  label: string;
  type: string;
  min?: number;
  max?: number;
}

export interface StrategyInfo {
  name: string;
  is_running: boolean;
  position: number;
  entry_price: number;
  last_price: number;
  unrealized_pnl: number;
  realized_pnl: number;
  errors: string[];
  events: string[];
  params: Record<string, number>;
  param_schema: ParamSchema[];
}

export interface Position {
  code: string;
  direction: string;
  quantity: number;
  price: number;
  last_price: number;
  pnl: number;
  margin_original: number;
}

export interface Margin {
  equity: number;
  equity_amount: number;
  margin_call: number;
  initial_margin: number;
  maintenance_margin: number;
}

export interface ProfitLoss {
  code: string;
  quantity: number;
  price: number;
  pnl: number;
  dseq: string;
  date: string;
}

export interface Usage {
  connections: number;
  used_bytes: number;
  limit_bytes: number;
  remaining_bytes: number;
  percent: number;
}

export interface Watch {
  id: string;
  contract: string;
  direction: "Buy" | "Sell";
  quantity: number;
  entry_price: number;
  stop_loss_pts: number;
  take_profit_pts: number;
  is_option?: boolean;
  match_code?: string;
  close_attempts?: number;     // 已送出幾次平倉單（>0 = 平倉中，等成交確認）
  close_gave_up?: boolean;     // 超過上限仍未平倉：不再自動重送，需人工處理
}

export interface Trade {
  id: string;
  action: string;
  price: number;
  deal_price: number;
  quantity: number;
  status: string;
  deal_quantity: number;
  order_time: string;
  deal_time: string;
}

export interface OrderRequest {
  action: "Buy" | "Sell";
  quantity: number;
  price?: number;
  price_type: "MKT" | "LMT";
  order_type: "ROD" | "IOC" | "FOK";
  octype: string;
  contract: "TMF" | "MXF" | "TXF";
  stop_loss_pts?: number;
  take_profit_pts?: number;
}

export interface OptionOrderRequest {
  delivery_month: string;
  strike: number;
  option_right: "C" | "P";
  category?: string;            // 預設 TXO
  action: "Buy" | "Sell";
  quantity: number;
  price: number;                // 權利金限價（必填）
  order_type: "ROD" | "IOC" | "FOK";
  stop_loss_pts?: number;
  take_profit_pts?: number;
  exit_buffer_pts?: number;
}

// ─── 市場狀態 ─────────────────────────────────────────────────────

export interface HurstInfo {
  value: number | null;       // 校準後 H（隨機漫步 = 0.5）
  raw: number | null;
  z: number | null;           // 與隨機漫步差幾個標準差
  se: number | null;          // 此窗口長度下純隨機漫步的 H 雜訊
  state: string;              // TREND | REVERT | RANDOM | UNCERTAIN
  label: string;
  window: number;
  last_bar: string;
  note: string;
  window_label?: string;      // 例：「60 根日K」「近 20 日 5 分K（1180 筆報酬）」
}

export interface IvInfo {
  state: string;              // LOW | NORMAL | HIGH | UNKNOWN
  label: string;
  percentile: number | null;
  history_n: number;
  min_history: number;
  value: number | null;       // ATM IV %
  source: string | null;      // manual | shioaji | csv
  as_of: string | null;
  note: string;
}

export interface MarketState {
  ready: boolean;
  mode: string;
  date?: string;
  phase?: string;             // early | pre | manual
  computed_at?: number;
  hurst?: HurstInfo;
  iv?: IvInfo;
  direction?: number;         // +1 偏多 / -1 偏空 / 0 中性
  direction_label?: string;
  state?: string;             // TREND | REVERT | UNCLEAR | IV_LOW | IV_HIGH
  state_label?: string;
  strategies?: string[];
  hint?: string;
  notes?: string[];
  config: {
    window: number; hurst_freq?: string; hurst_days?: number; trend_th: number; revert_th: number; min_z: number;
    iv_min_history: number; iv_auto: boolean; gate: string; pre_hhmm: number; post_hhmm: number;
  };
}

export interface JournalRow {
  date: string;
  phase: string | null;
  hurst: number | null;
  hurst_z: number | null;
  hurst_state: string | null;
  iv: number | null;
  iv_pct: number | null;
  iv_state: string | null;
  direction: number | null;
  market_state: string | null;
  strategy_hint: string | null;
  range_ratio: number | null;
  basis: string;
  notes: string;
  trades: number;
  pnl: number;
  scalp_on: boolean;
  result: string;             // 獲利 | 虧損 | 持平 | 未進場
}

export interface MarketStats {
  strategy: string;
  big_move: number;
  by_state: {
    state: string; days: number; wins: number; losses: number; win_rate: number | null;
    avg_win: number; avg_loss: number; payoff: number | null; total_pnl: number;
  }[];
  by_iv: {
    iv_state: string; days: number; big_move_days: number; big_move_rate: number; avg_range_ratio: number;
  }[];
}

// 盤中即時狀態（真實成交的外/內盤比例、日盤振幅、與盤前判斷是否同向；僅供顯示）
export interface FlowWindow {
  n: number;                  // 視窗內實際的成交筆數
  buy: number;
  sell: number;
  share: number | null;       // 外盤筆數占比（沒資料為 null，不是 0）
  vol_share: number | null;   // 外盤口數占比
  span_sec: number;           // 這個視窗涵蓋多少秒
  dir?: number;               // 買賣方向：+1 買方主動 / -1 賣方主動 / 0 中性（後端帶遲滯算好的；舊後端沒有這欄）
}

export interface LiveState {
  ready: boolean;
  as_of: number;
  prefix: string;             // 追蹤的合約（TMF）
  last: number | null;
  last_age: number | null;
  flow: Record<string, FlowWindow>;   // "20" | "100" | "300" 筆
  high: number | null;
  low: number | null;
  range: number | null;       // 今日日盤振幅（點）；非今日日盤為 null
  avg_range: number | null;   // 近 20 日日盤平均振幅
  range_ratio: number | null;
  range_label: string | null; // 大波動 | 正常 | 清淡
  session_day: string;
  since: string | null;       // 振幅累計起點（重啟後重新累計）
  partial: boolean;           // 起點晚於 08:50 → 振幅可能不完整
  in_session: boolean;
  pre: { date: string | null; state: string | null; hint: string | null; want: number; want_reason: string };
  flow_dir: number;           // +1 買方主動 / -1 賣方主動 / 0 中性
  coherence: number | null;   // +1 協調 / -1 矛盾 / 0 中性 / null 無法比較
  coherence_text: string;
  thresholds: { flow_up: number; flow_down: number; flow_margin?: number; big_move: number; quiet: number };
}

// 市場指標回放（歷史）＋統計驗證
export interface ReplayColor {
  kind: "trend" | "revert" | "neutral" | "none"; hue: number; sat: number; light: number; strength: number;
  iv_known: boolean; css: string;
}
export interface ReplayCell {
  i: number; start: string; trades: number; share: number | null;
  dir: number;                                   // +1 買方主動 / -1 賣方主動 / 0 中性（該格結束那一刻，帶遲滯）
  shape: "up" | "down" | "flat";
  coherence: number | null;                      // +1 協調 / -1 矛盾 / 0 力道中性 / null 沒有預期方向或資料不足
  up_min: number; down_min: number; close: number | null;
  fwd: number | null;                            // 之後到下一格結束的價格變動（點）
}
export interface ReplayDay {
  date: string; asof: string | null; hurst: number | null; hurst_z: number | null; hurst_state: string | null;
  direction: number | null; iv_pct: number | null; iv_state: string | null;
  want: number;                                  // 盤前預期方向 +1 / -1 / 0
  color: ReplayColor; open: number | null; close: number | null; move: number | null; cells: ReplayCell[];
}
export interface ReplayGroup { n: number; hit_rate: number | null; mean_move: number | null }
export interface ReplayValidation {
  eligible_cells: number; days_with_want: number; n_perm: number; min_group: number;
  coherent: ReplayGroup; contradictory: ReplayGroup; neutral: ReplayGroup;
  diff: number | null; p_value: number | null;
  cusum: null | { observed: (number | null)[]; lo: (number | null)[]; hi: (number | null)[]; end_outside: boolean };
  day_level: null | { days: number; hit_rate: number | null; null_mean: number | null; p_value: number | null };
  verdict: { level: "insufficient" | "none" | "significant" | "wrong_way"; text: string };
}
export interface Replay {
  params: Record<string, number>;
  blocks: { i: number; start: string }[];
  days: ReplayDay[];
  coverage: { days: number; with_hurst: number; with_iv: number; with_want: number };
  validation?: ReplayValidation;
}

// 破產機率驗證
export interface RuinPoint {
  capital: number;
  ruin_prob: number;                  // n_trades 筆內破產的比例（蒙地卡羅）
  lundberg: number;                   // 無限期破產機率上界
  formula: number | null;             // 賺賠對稱且無成本時的公式值
  median_first_ruin_trade: number | null;
  drawdown_p95: number;
  survivor_median_end: number | null;
  n_trades: number;
  n_paths: number;
}

export interface RuinReport {
  inputs: {
    capital: number; win_rate: number; win: number; loss: number; cost: number;
    n_trades: number; n_paths: number; ruin_level: number; source: string;
  };
  per_trade: {
    win: number; loss: number; cost: number; payoff: number | null;
    breakeven_win_rate: number; expectancy: number; expectancy_pct_of_capital: number | null;
    baseline_win_rate?: number;       // 無技巧基準勝率（隨機進場）＝ 停損 ÷ (停利 + 停損)
    edge_needed?: number;             // 兩平勝率 − 基準：進場至少要比隨機多出的優勢（0.025 ＝ 2.5 個百分點）
  };
  ruin: Record<string, RuinPoint>;    // "0.5x" | "1x" | "2x"
  capacity: { affordable_losses: number | null; expected_longest_losing_streak: number };
  grid: { win_rate: number; expectancy: number; "0.5x": number; "1x": number; "2x": number }[];
  history: null | {
    n: number; win_rate: number; avg_win: number; avg_loss: number; payoff: number | null;
    expectancy: number; total: number; worst_trade: number;
    longest_losing_streak: number; longest_losing_streak_loss: number;
  };
  history_used: boolean;
  history_note: string | null;
  defaults: { capital_from_equity: boolean; equity: number | null; win_rate_from_baseline?: boolean };
}

// ─── API client ───────────────────────────────────────────────────

const JSON_HEADERS = { "Content-Type": "application/json" };

export const api = {
  risk: {
    ruin: (q: Record<string, string | number | boolean>) =>
      req<RuinReport>(`/risk/ruin?${new URLSearchParams(Object.entries(q).map(([k, v]) => [k, String(v)]))}`),
  },
  market: {
    state: () => req<MarketState>("/market/state"),
    live: () => req<LiveState>("/market/live"),
    replay: (q: Record<string, string | number | boolean>) =>
      req<Replay>(`/market/replay?${new URLSearchParams(Object.entries(q).map(([k, v]) => [k, String(v)]))}`),
    // POST 的回應刻意標成 unknown：呼叫端送出後要重抓 state()，不可把回應直接當 MarketState 用
    refresh: (force = false) =>
      req<unknown>(`/market/refresh${force ? "?force=true" : ""}`, { method: "POST" }),
    setIv: (iv: number) =>
      req<unknown>("/market/iv", {
        method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ iv }),
      }),
    journal: (limit = 14) => req<JournalRow[]>(`/market/journal?limit=${limit}`),
    saveNote: (date: string, data: { basis?: string; notes?: string }) =>
      req(`/market/journal/${date}`, {
        method: "PATCH", headers: JSON_HEADERS, body: JSON.stringify(data),
      }),
    stats: (strategy = "scalp") => req<MarketStats>(`/market/stats?strategy=${strategy}`),
  },
  strategy: {
    list: () => req<StrategyInfo[]>("/strategy/"),
    start: (name: string, params: Record<string, number>) =>
      req<{ status: string; name: string; warning?: string }>(`/strategy/${name}/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ params }),
      }),
    // 持倉時後端預設回 409；force=true 才會停（停止＝不再檢查停損停利＋取消帳戶內所有未成交委託）
    stop: (name: string, force = false) =>
      req<{ status: string; name: string; warning?: string }>(`/strategy/${name}/stop${force ? "?force=true" : ""}`, { method: "POST" }),
  },
  health: () =>
    req<{ status: string; broker_connected: string }>("/health"),
  quote: {
    last: () => req<Record<string, number>>("/quote/last"),
  },
  position: {
    list: () => req<Position[]>("/position/"),
    margin: () => req<Margin>("/position/margin"),
    pnl: () => req<ProfitLoss[]>("/position/pnl"),
    usage: () => req<Usage>("/position/usage"),
    meta: () => req<{ updated_at: number; age_sec: number; positions_age_sec?: number; pnl_age_sec?: number }>("/position/meta"),
  },
  order: {
    place: (data: OrderRequest) =>
      req<{ trade_id: string; status: string; watch_id?: string }>("/order/place", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      }),
    optionExpiries: (category = "TXO") =>
      req<string[]>(`/order/option/expiries?category=${encodeURIComponent(category)}`),
    optionStrikes: (deliveryMonth: string, right: "C" | "P", category = "TXO") =>
      req<number[]>(
        `/order/option/strikes?delivery_month=${encodeURIComponent(deliveryMonth)}` +
          `&right=${right}&category=${encodeURIComponent(category)}`,
      ),
    optionQuote: (deliveryMonth: string, strike: number, right: "C" | "P", category = "TXO") =>
      req<{ code: string; close: number; bid: number; ask: number; total_volume: number }>(
        `/order/option/quote?delivery_month=${encodeURIComponent(deliveryMonth)}` +
          `&strike=${strike}&right=${right}&category=${encodeURIComponent(category)}`,
      ),
    placeOption: (data: OptionOrderRequest) =>
      req<{
        trade_id: string;
        status: string;
        code: string;
        limit_price: number;
        watch_id?: string;
      }>("/order/place_option", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      }),
    cancel: (tradeId: string) =>
      req(`/order/cancel/${tradeId}`, { method: "POST" }),
    trades: () => req<Trade[]>("/order/trades"),
    watches: () => req<Watch[]>("/order/watches"),
    updateWatch: (watchId: string, data: { stop_loss_pts?: number; take_profit_pts?: number }) =>
      req(`/order/watches/${watchId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      }),
    removeWatch: (watchId: string) =>
      req(`/order/watches/${watchId}`, { method: "DELETE" }),
  },
};
