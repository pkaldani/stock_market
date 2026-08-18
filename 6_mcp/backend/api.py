"""HTTP API over the trading floor, for a separate frontend to consume.

The Gradio dashboard in demo/ reads accounts.db in-process. This serves the same
data as JSON so a decoupled web frontend can render it. Everything here is
read-only; the trading floor writes the database out of band.

Run it from the 6_mcp directory so it shares the engine's accounts.db:

    uv run uvicorn backend.api:app --port 8000
"""

from fastapi import FastAPI

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


def average_cost(account: Account, symbol: str) -> float:
    """Average price paid across this symbol's buys, for per-holding profit."""
    spend = sum(t.price * t.quantity for t in account.transactions if t.symbol == symbol and t.quantity > 0)
    bought = sum(t.quantity for t in account.transactions if t.symbol == symbol and t.quantity > 0)
    return spend / bought if bought else 0.0


def holdings_detail(account: Account) -> list[dict]:
    """Current holdings enriched with price, market value, unrealised profit,
    and real Alpaca asset metadata (exchange, asset class, tradability)."""
    details = []
    for symbol, quantity in account.holdings.items():
        price = market.get_share_price(symbol)
        cost = average_cost(account, symbol)
        try:
            asset = alpaca_broker.get_asset_info(symbol)
        except Exception:
            # Missing Alpaca creds, a rate limit, or a network hiccup shouldn't
            # take down the whole read-only dashboard — just omit the fields.
            asset = {}
        details.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "price": price,
                "avg_cost": cost,
                "market_value": price * quantity,
                "unrealized_pnl": (price - cost) * quantity,
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
    """The trader's full state: value, profit, holdings, transactions and history."""
    account = Account.get()
    holdings = holdings_detail(account)
    portfolio_value = account.balance + sum(h["market_value"] for h in holdings)
    return {
        "name": TRADER_NAME,
        "model_name": MODEL_NAME,
        "balance": account.balance,
        "strategy": account.strategy,
        "portfolio_value": portfolio_value,
        "pnl": account.calculate_profit_loss(portfolio_value),
        "holdings": holdings,
        "transactions": account.list_transactions(),
        "time_series": [{"datetime": ts, "value": value} for ts, value in account.portfolio_value_time_series],
    }


@app.get("/api/trader/logs")
def get_trader_logs(last_n: int = 13) -> list[dict]:
    """Recent trace and account log lines, oldest first, with their panel colour."""
    rows = list(read_log(TRADER_NAME, last_n))
    return [
        {"datetime": ts, "type": kind, "message": message, "color": LOG_COLORS.get(kind, DEFAULT_LOG_COLOR)}
        for ts, kind, message in rows
    ]
