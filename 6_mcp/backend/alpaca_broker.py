"""
Alpaca broker integration.

Provides real market order execution and live account/position state via the
Alpaca Trading API (alpaca-py SDK). This talks to a SINGLE Alpaca account —
Alpaca's personal Trading API does not expose multiple named sub-accounts;
that only exists in Alpaca's separate Broker API product for platforms
managing many end-customers.

Because there is only one real account, per-trader (Warren/George/Ray/Cathie)
balances and holdings continue to be tracked as VIRTUAL LEDGERS in the local
database (see accounts.py), while actual order execution, fills, and cash
all flow through this one Alpaca account.

Setup:
    pip install alpaca-py

Docs:
    https://docs.alpaca.markets/docs/getting-started
    https://docs.alpaca.markets/docs/paper-trading
"""

import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

load_dotenv(override=True)

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
# Keep this true until you are confident in the wiring. Paper trading uses
# real market data and real order mechanics, but fake money.
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").strip().lower() == "true"

_trading_client = None
_data_client = None


def _require_keys():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise RuntimeError(
            "Set ALPACA_API_KEY and ALPACA_SECRET_KEY in your .env file. "
            "Get these from https://app.alpaca.markets/paper/dashboard/overview "
            "(paper keys) or the live dashboard (live keys)."
        )


def trading_client() -> TradingClient:
    """Lazily-created singleton so importing this module doesn't require keys."""
    global _trading_client
    if _trading_client is None:
        _require_keys()
        _trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
    return _trading_client


def data_client() -> StockHistoricalDataClient:
    global _data_client
    if _data_client is None:
        _require_keys()
        _data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    return _data_client


def get_account_info() -> dict:
    """The single real Alpaca account's cash, buying power, and equity."""
    account = trading_client().get_account()
    return {
        "account_number": account.account_number,
        "status": str(account.status),
        "cash": float(account.cash),
        "buying_power": float(account.buying_power),
        "portfolio_value": float(account.portfolio_value),
        "equity": float(account.equity),
        "pattern_day_trader": account.pattern_day_trader,
    }


def get_real_positions() -> dict[str, int]:
    """Actual positions currently held in the real Alpaca account, aggregated
    across all symbols. Used as a sanity check against the sum of all
    traders' virtual holdings for the same symbol."""
    positions = trading_client().get_all_positions()
    return {p.symbol: int(float(p.qty)) for p in positions}


def get_latest_price(symbol: str) -> float:
    """Midpoint of the latest real bid/ask for a symbol."""
    request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
    quote = data_client().get_stock_latest_quote(request)[symbol]
    bid, ask = quote.bid_price, quote.ask_price
    if bid and ask:
        return round((bid + ask) / 2, 4)
    return float(ask or bid)


def submit_market_order(symbol: str, quantity: int, side: str) -> dict:
    """Submit a real market order. side: 'buy' or 'sell'.

    Market orders fill quickly during market hours but the fill price can
    differ from the quote you saw a moment earlier — that's normal slippage,
    not a bug.
    """
    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
    order_request = MarketOrderRequest(
        symbol=symbol,
        qty=quantity,
        side=order_side,
        time_in_force=TimeInForce.DAY,
    )
    order = trading_client().submit_order(order_request)
    return {
        "id": str(order.id),
        "symbol": order.symbol,
        "qty": float(order.qty),
        "side": order.side.value,
        "status": str(order.status.value),
        "submitted_at": str(order.submitted_at),
    }


def get_order(order_id: str) -> dict:
    """Poll an order's status/fill details after submission."""
    order = trading_client().get_order_by_id(order_id)
    return {
        "id": str(order.id),
        "status": str(order.status.value),
        "filled_qty": float(order.filled_qty or 0),
        "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else None,
    }


def is_market_open() -> bool:
    clock = trading_client().get_clock()
    return clock.is_open