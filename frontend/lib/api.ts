const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init);
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

export interface Trade {
  id: string;
  action: string;
  price: number;
  quantity: number;
  status: string;
  deal_quantity: number;
}

export interface OrderRequest {
  action: "Buy" | "Sell";
  quantity: number;
  price?: number;
  price_type: "MKT" | "LMT";
  order_type: "ROD" | "IOC" | "FOK";
  octype: string;
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
  },
  order: {
    place: (data: OrderRequest) =>
      req<{ trade_id: string; status: string }>("/order/place", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      }),
    cancel: (tradeId: string) =>
      req(`/order/cancel/${tradeId}`, { method: "POST" }),
    trades: () => req<Trade[]>("/order/trades"),
  },
};
