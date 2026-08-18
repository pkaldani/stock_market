"""HTTP API over the trading floor, for a separate frontend to consume.

The Gradio dashboard in demo/ reads accounts.db in-process. This serves the same
data as JSON so a decoupled web frontend can render it. Most of this is
read-only (the trading floor writes the database out of band); the
`/api/trader/buy` and `/api/trader/sell` endpoints are the one exception —
they let the dashboard's own trade page place real orders manually, through
the same `Account.buy_shares`/`sell_shares` (and their guardrails) the LLM
trader uses, tagged with `source="manual"` to distinguish them in history.

Run it from the 6_mcp directory so it shares the engine's accounts.db:

    uv run uvicorn backend.api:app --port 8000
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from backend import alpaca_broker, market
from backend.accounts import Account
from backend.database import read_log
from backend.trading_floor import TRADER_NAME, MODEL_NAME

# Mirrors the log colours in demo/ so the frontend reproduces the same panel.
LOG_COLORS = {
    "trace": "#87CEEB",
    "agent": "#00dddd",
    "function": "#00dd00",
    "generation": "#dddd00",
    "response": "#aa00dd",
    "account": "#dd0000",
}
DEFAULT_LOG_COLOR = "#87CEEB"

app = FastAPI(title="Trading Floor")


def holdings_detail(positions: list[dict]) -> list[dict]:
    """Real Alpaca positions enriched with real Alpaca asset metadata
    (exchange, asset class, tradability). Price, cost basis, and unrealized
    P&L come straight from Alpaca's own position data — not recomputed
    locally, since Alpaca knows about positions opened outside this app too."""
    details = []
    for pos in positions:
        try:
            asset = alpaca_broker.get_asset_info(pos["symbol"])
        except Exception:
            # Missing Alpaca creds, a rate limit, or a network hiccup shouldn't
            # take down the whole read-only dashboard — just omit the fields.
            asset = {}
        details.append(
            {
                "symbol": pos["symbol"],
                "quantity": pos["quantity"],
                "price": pos["current_price"],
                "avg_cost": pos["avg_entry_price"],
                "market_value": pos["market_value"],
                "unrealized_pnl": pos["unrealized_pl"],
                "exchange": asset.get("exchange"),
                "asset_class": asset.get("asset_class"),
                "tradable": asset.get("tradable"),
                "fractionable": asset.get("fractionable"),
                "shortable": asset.get("shortable"),
            }
        )
    return details


@app.get("/api/market")
def get_market() -> dict:
    """Which price source is live, and whether the market is open."""
    source = "massive" if market.massive_api_key else "simulator"
    return {"source": source, "is_market_open": market.is_market_open()}


@app.get("/api/trader")
def get_trader() -> dict:
    """The trader's full state: value, profit, holdings, transactions and history.

    Balance, holdings, and portfolio value all come from the real Alpaca
    account (there's no separate virtual ledger) via one cached snapshot
    (~5min TTL, see alpaca_broker.get_real_account_snapshot) so the ~6s
    frontend poll doesn't hit Alpaca's API on every request. `transactions`
    is this app's own order history, kept locally for rationale/audit — its
    count also doubles as the snapshot cache's invalidation key, since a
    trade can be placed by a different process (the trading_floor engine)
    than the one serving this request; accounts.db is the one thing every
    process shares, so a changed count busts the cache early even then.
    """
    account = Account.get()
    try:
        snapshot = alpaca_broker.get_real_account_snapshot(cache_key=len(account.transactions))
    except Exception:
        # Missing Alpaca creds, a rate limit, or a network hiccup shouldn't
        # crash the whole read-only dashboard — surface "unavailable" instead.
        snapshot = None

    if snapshot:
        positions = snapshot["positions"]
        holdings = holdings_detail(positions)
        balance = snapshot["cash"]
        portfolio_value = snapshot["portfolio_value"]
        pnl = sum(p["unrealized_pl"] for p in positions)
        real_account = {k: v for k, v in snapshot.items() if k != "positions"}
    else:
        holdings, balance, portfolio_value, pnl, real_account = [], None, None, None, None

    return {
        "name": TRADER_NAME,
        "model_name": MODEL_NAME,
        "balance": balance,
        "strategy": account.strategy,
        "portfolio_value": portfolio_value,
        "pnl": pnl,
        "holdings": holdings,
        "transactions": account.list_transactions(),
        "time_series": [{"datetime": ts, "value": value} for ts, value in account.portfolio_value_time_series],
        "real_account": real_account,
    }


@app.get("/api/trader/logs")
def get_trader_logs(last_n: int = 13) -> list[dict]:
    """Recent trace and account log lines, oldest first, with their panel colour."""
    rows = list(read_log(TRADER_NAME, last_n))
    return [
        {"datetime": ts, "type": kind, "message": message, "color": LOG_COLORS.get(kind, DEFAULT_LOG_COLOR)}
        for ts, kind, message in rows
    ]


@app.get("/api/trader/realized-pnl")
def get_realized_pnl() -> dict:
    """FIFO-matched realized P&L from the real account's full fill history:
    a trade-by-trade closed-lot ledger plus per-symbol/total rollups. See
    alpaca_broker.get_realized_pnl for the matching logic; cached ~5min
    there since it walks the account's entire order history."""
    try:
        return alpaca_broker.get_realized_pnl()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not compute realized P&L: {e}")


@app.get("/api/price/{symbol}")
def get_price(symbol: str) -> dict:
    """Live Alpaca quote for a symbol — used by the trade page's buy form to
    preview estimated cost before submitting an order."""
    symbol = symbol.upper()
    try:
        price = alpaca_broker.get_latest_price(symbol)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"No live price for {symbol}: {e}")
    return {"symbol": symbol, "price": price}


class TradeRequest(BaseModel):
    symbol: str
    quantity: int = Field(gt=0)
    rationale: str = "Manual trade via dashboard"


@app.post("/api/trader/buy")
def post_buy(req: TradeRequest) -> dict:
    """Place a real market buy order manually from the dashboard's trade
    page. Subject to the same guardrails as the LLM trader (hold period,
    real buying power) since it calls the same Account.buy_shares."""
    try:
        Account.get().buy_shares(req.symbol.upper(), req.quantity, req.rationale, source="manual")
    except Exception as e:
        # Guardrail violations raise ValueError; a bad symbol, a rejected
        # order, or an Alpaca API hiccup can raise other exception types
        # (KeyError for an unknown symbol, alpaca-py's own API error types,
        # etc.) — all of them mean "the order didn't go through," which the
        # trade page should show as a clean message, not a raw 500.
        raise HTTPException(status_code=400, detail=str(e) or type(e).__name__)
    return get_trader()


@app.post("/api/trader/sell")
def post_sell(req: TradeRequest) -> dict:
    """Place a real market sell order manually from the dashboard's trade
    page. Subject to the same guardrails as the LLM trader (hold period,
    real share availability) since it calls the same Account.sell_shares."""
    try:
        Account.get().sell_shares(req.symbol.upper(), req.quantity, req.rationale, source="manual")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e) or type(e).__name__)
    return get_trader()
