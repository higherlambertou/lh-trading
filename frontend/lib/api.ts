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

// ─── API client ───────────────────────────────────────────────────

export const api = {
  strategy: {
    list: () => req<StrategyInfo[]>("/strategy/"),
    start: (name: string, params: Record<string, number>) =>
      req(`/strategy/${name}/start`, {
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
