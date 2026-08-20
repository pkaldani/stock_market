// Client for the trading floor HTTP API. All paths are relative; in dev the Vite
// proxy forwards /api to the FastAPI backend, so the browser sees one origin.

export interface Holding {
  symbol: string;
  quantity: number;
  // Alpaca's current_price for the position; null on the rare occasion
  // Alpaca doesn't report one (e.g. brand-new fill not yet priced).
  price: number | null;
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
  // "agent" (the LLM trader) or "manual" (placed via the trade page).
  source: "agent" | "manual";
  // Alpaca order status at the time this row was last written. A non-
  // terminal status (see isPendingOrder below) means quantity/price are
  // still the pre-fill estimate recorded by buy_shares/sell_shares — the
  // real values land once a later order reconciles it
  // (accounts.py's reconcile_pending_transactions), which only runs as a
  // side effect of the NEXT buy/sell call on any symbol, so a pending row
  // can persist on screen for a while with no trade activity to trigger it.
  order_status: string;
}

// Mirrors accounts.py's _TERMINAL_ORDER_STATUSES. Anything else (new,
// accepted, pending_new, partially_filled, ...) hasn't resolved yet, so the
// transaction's quantity/price shown for it is still provisional.
const TERMINAL_ORDER_STATUSES = new Set([
  "filled", "canceled", "expired", "rejected", "done_for_day",
  "replaced", "stopped", "suspended", "calculated",
]);

export function isPendingOrder(t: Transaction): boolean {
  return !!t.order_status && !TERMINAL_ORDER_STATUSES.has(t.order_status);
}

// Compact realized-P&L/win-rate summary — see Account.realized_pnl_summary
// in accounts.py. null fields mean there aren't enough closed trades yet to
// compute them (e.g. a fresh account).
export interface RealizedPnlSummary {
  total_realized_pnl: number;
  closed_trade_count: number;
  win_rate_pct: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  best_trade_pnl: number | null;
  worst_trade_pnl: number | null;
  // Symbols where FIFO matching had a sell quantity it couldn't pair against
  // a known open lot (e.g. a split, or a position opened before Alpaca's
  // earliest reported fill) — the numbers above are understated, not wrong-
  // but-complete, for any symbol listed here.
  incomplete_for_symbols: string[];
}

export interface TimePoint {
  datetime: string;
  value: number;
}

// The real Alpaca paper account's own status/buying power. `balance` and
// `portfolio_value` on TraderDetail already come from this same account, so
// this only carries the fields not shown elsewhere (buying power, status).
export interface RealAccountInfo {
  account_number: string;
  status: string;
  cash: number;
  buying_power: number;
  portfolio_value: number;
  equity: number;
  pattern_day_trader: boolean;
}

// Mirrors the full backend payload; the dashboard renders a subset of these fields.
// balance/portfolio_value/pnl/real_account are all null together when the real
// Alpaca account couldn't be reached (missing creds, rate limit, network hiccup)
// — there is no separate virtual ledger to fall back to.
export interface TraderDetail {
  name: string;
  model_name: string;
  balance: number | null;
  strategy: string;
  portfolio_value: number | null;
  pnl: number | null;
  // Computed independently of the account snapshot above (its own try/except
  // server-side), so this can be present even when real_account is null, or
  // vice versa.
  realized_pnl_summary: RealizedPnlSummary | null;
  holdings: Holding[];
  transactions: Transaction[];
  time_series: TimePoint[];
  real_account: RealAccountInfo | null;
}

export interface LogRow {
  datetime: string;
  type: string;
  message: string;
  color: string;
}

export interface MarketInfo {
  // backend/api.py's /api/market always reports "yfinance" (market.py is
  // pure yfinance, no simulated fallback); "simulator" is kept here only in
  // case a future data-source change actually introduces one.
  source: "yfinance" | "simulator";
  is_market_open: boolean;
}

export interface ClosedTrade {
  symbol: string;
  quantity: number;
  buy_price: number;
  sell_price: number;
  buy_time: string;
  sell_time: string;
  realized_pnl: number;
}

export interface RealizedPnl {
  closed_trades: ClosedTrade[];
  by_symbol: Record<string, number>;
  total: number;
}

export interface PriceQuote {
  symbol: string;
  price: number;
}

// The approved symbol universe (see backend/symbol_whitelist.yaml) — static
// for the life of the process, fetched once rather than polled.
export interface WhitelistEntry {
  symbol: string;
  sector: string | null;
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} failed: ${r.status}`);
  return r.json() as Promise<T>;
}

// FastAPI's HTTPException responses are `{"detail": "..."}`; surface that
// message rather than a bare status code, since the trade form shows it
// directly to the user (e.g. "Insufficient real buying power...").
async function post<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const payload = await r.json().catch(() => null);
    const detail = payload?.detail;
    const message = typeof detail === "string" ? detail : JSON.stringify(detail ?? r.status);
    throw new Error(message);
  }
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

export function getRealizedPnl(): Promise<RealizedPnl> {
  return get("/api/trader/realized-pnl");
}

export function getPrice(symbol: string): Promise<PriceQuote> {
  return get(`/api/price/${encodeURIComponent(symbol)}`);
}

export function getWhitelist(): Promise<WhitelistEntry[]> {
  return get("/api/whitelist");
}

export function buyShares(symbol: string, quantity: number, rationale?: string): Promise<TraderDetail> {
  return post("/api/trader/buy", { symbol, quantity, ...(rationale ? { rationale } : {}) });
}

export function sellShares(symbol: string, quantity: number, rationale?: string): Promise<TraderDetail> {
  return post("/api/trader/sell", { symbol, quantity, ...(rationale ? { rationale } : {}) });
}
