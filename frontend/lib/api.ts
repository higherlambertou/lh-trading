const PROD_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8002/api";
const SIM_URL  = process.env.NEXT_PUBLIC_SIM_URL  ?? "http://localhost:8003/api";

export function getBase(): string {
  if (typeof window !== "undefined" && localStorage.getItem("trading_mode") === "sim") {
    return SIM_URL;
  }
  return PROD_URL;
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
