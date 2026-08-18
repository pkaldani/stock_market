// Client for the trading floor HTTP API. All paths are relative; in dev the Vite
// proxy forwards /api to the FastAPI backend, so the browser sees one origin.

export interface Holding {
  symbol: string;
  quantity: number;
  price: number;
  avg_cost: number;
  market_value: number;
  unrealized_pnl: number;
  // Real Alpaca asset metadata; null when unavailable (e.g. Alpaca creds
  // missing or the lookup failed) rather than absent, so callers can tell
  // "unknown" from "not held".
  exchange: string | null;
  asset_class: string | null;
  tradable: boolean | null;
  fractionable: boolean | null;
  shortable: boolean | null;
}

export interface Transaction {
  symbol: string;
  quantity: number;
  price: number;
  timestamp: string;
  rationale: string;
}

export interface TimePoint {
  datetime: string;
  value: number;
}

// Mirrors the full backend payload; the dashboard renders a subset of these fields.
export interface TraderDetail {
  name: string;
  model_name: string;
  balance: number;
  strategy: string;
  portfolio_value: number;
  pnl: number;
  holdings: Holding[];
  transactions: Transaction[];
  time_series: TimePoint[];
}

export interface LogRow {
  datetime: string;
  type: string;
  message: string;
  color: string;
}

export interface MarketInfo {
  source: "massive" | "simulator";
  is_market_open: boolean;
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} failed: ${r.status}`);
  return r.json() as Promise<T>;
}

export function getTrader(): Promise<TraderDetail> {
  return get("/api/trader");
}

export function getTraderLogs(lastN = 13): Promise<LogRow[]> {
  return get(`/api/trader/logs?last_n=${lastN}`);
}

export function getMarket(): Promise<MarketInfo> {
  return get("/api/market");
}
