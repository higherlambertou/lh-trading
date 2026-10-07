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
    window: number; trend_th: number; revert_th: number; min_z: number;
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

// ─── API client ───────────────────────────────────────────────────

const JSON_HEADERS = { "Content-Type": "application/json" };

export const api = {
  market: {
    state: () => req<MarketState>("/market/state"),
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
    stop: (name: string) =>
      req(`/strategy/${name}/stop`, { method: "POST" }),
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
    meta: () => req<{ updated_at: number; age_sec: number }>("/position/meta"),
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
