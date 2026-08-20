"""
Alpaca broker integration.

Provides real market order execution and live account/position state via the
Alpaca Trading API (alpaca-py SDK). This talks to a SINGLE Alpaca account —
Alpaca's personal Trading API does not expose multiple named sub-accounts;
that only exists in Alpaca's separate Broker API product for platforms
managing many end-customers.

There is exactly one trader and no virtual ledger: `Account.balance`/
`Account.holdings` (accounts.py) are live reads of this one real account's
cash and positions, not a separately tracked local copy.

Setup:
    pip install alpaca-py

Docs:
    https://docs.alpaca.markets/docs/getting-started
    https://docs.alpaca.markets/docs/paper-trading
"""

import os
import time
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


_asset_cache: dict[str, dict] = {}


def get_asset_info(symbol: str) -> dict:
    """Real Alpaca asset metadata for a symbol: exchange, asset class, and
    tradability flags. Cached per symbol for the life of the process — unlike
    price/quote data, this is effectively static."""
    if symbol not in _asset_cache:
        asset = trading_client().get_asset(symbol)
        _asset_cache[symbol] = {
            "symbol": asset.symbol,
            "name": asset.name,
            "exchange": str(asset.exchange),
            "asset_class": str(asset.asset_class),
            "status": str(asset.status),
            "tradable": asset.tradable,
            "fractionable": asset.fractionable,
            "shortable": asset.shortable,
        }
    return _asset_cache[symbol]


def get_real_positions() -> dict[str, int]:
    """Actual positions currently held in the real Alpaca account, aggregated
    across all symbols. This IS the trader's holdings — there is no separate
    virtual ledger of positions anymore."""
    positions = trading_client().get_all_positions()
    return {p.symbol: int(float(p.qty)) for p in positions}


def get_real_positions_detail() -> list[dict]:
    """Real Alpaca positions with quantity, cost basis, current price, and
    unrealized P&L as reported directly by Alpaca (not recomputed locally
    from a transaction log, which wouldn't know about positions opened
    outside this app)."""
    positions = trading_client().get_all_positions()
    return [
        {
            "symbol": p.symbol,
            "quantity": int(float(p.qty)),
            "avg_entry_price": float(p.avg_entry_price),
            "current_price": float(p.current_price) if p.current_price else None,
            "market_value": float(p.market_value) if p.market_value else 0.0,
            "unrealized_pl": float(p.unrealized_pl) if p.unrealized_pl else 0.0,
        }
        for p in positions
    ]


# Unlike `_asset_cache` above (static metadata, cached forever), account
# balance/positions change with every fill, so this cache is time-based: a
# few minutes old is fine for a dashboard display, and it keeps the read-only
# API from hitting Alpaca on every ~6s frontend poll.
_ACCOUNT_SNAPSHOT_TTL_SECONDS = 300
_account_snapshot_cache: dict = {}


def get_real_account_snapshot(cache_key: int | None = None) -> dict:
    """Real Alpaca account info plus detailed positions, cached for a few
    minutes. Meant for read-only display (the dashboard), not for the order
    safety checks or live balance/holdings in accounts.py, which call
    get_account_info()/get_real_positions() directly so they always see
    fresh state before placing an order.

    `cache_key` is an opaque marker — api.py passes the local transaction
    count from accounts.db — that busts the cache early if it differs from
    what was true when the cache was last filled. A plain TTL alone can't
    tell when a trade happened: buy_shares/sell_shares run in whichever
    process placed the order (the trading_floor engine for the LLM trader,
    this api.py process for a manual trade), so an in-process "clear the
    cache now" call only ever helps the manual case. The transaction count
    is read from the one thing every process already shares — accounts.db —
    so this catches both.
    """
    now = time.monotonic()
    cached = _account_snapshot_cache.get("data")
    cached_at = _account_snapshot_cache.get("at", 0.0)
    cached_key = _account_snapshot_cache.get("key")
    fresh_enough = cached is not None and now - cached_at < _ACCOUNT_SNAPSHOT_TTL_SECONDS
    same_key = cache_key is None or cache_key == cached_key
    if fresh_enough and same_key:
        return cached

    snapshot = {**get_account_info(), "positions": get_real_positions_detail()}
    _account_snapshot_cache["data"] = snapshot
    _account_snapshot_cache["at"] = now
    _account_snapshot_cache["key"] = cache_key
    return snapshot


def get_fill_activities() -> list[dict]:
    """Every real order fill on this account, oldest first, for its entire
    history — not just orders placed by this app (the account may have prior
    manual activity). alpaca-py has no dedicated wrapper for the account
    activities endpoint, so this calls it directly via the base client and
    paginates through Alpaca's 100-per-page limit via `page_token`."""
    client = trading_client()
    activities: list[dict] = []
    page_token = None
    while True:
        params = {"direction": "asc", "page_size": 100}
        if page_token:
            params["page_token"] = page_token
        batch = client.get("/account/activities/FILL", params)
        if not batch:
            break
        activities.extend(batch)
        if len(batch) < 100:
            break
        page_token = batch[-1]["id"]
    return [
        {
            "id": a["id"],
            "symbol": a["symbol"],
            "side": a["side"],
            "qty": float(a["qty"]),
            "price": float(a["price"]),
            "transaction_time": a["transaction_time"],
            "order_id": a["order_id"],
        }
        for a in activities
    ]


# Same rationale as _ACCOUNT_SNAPSHOT_TTL_SECONDS: this walks the account's
# entire fill history on every call, so it's cached rather than recomputed
# on every dashboard poll.
_REALIZED_PNL_TTL_SECONDS = 300
_realized_pnl_cache: dict = {}


def get_realized_pnl() -> dict:
    """Realized P&L via FIFO cost-basis matching over the real account's full
    fill history: each buy opens a cost-basis lot, each sell consumes the
    oldest open lot(s) first. Returns individual closed trades (for a
    trade-by-trade ledger) plus per-symbol and total rollups.

    A sell quantity left over after every open lot for that symbol is
    exhausted (e.g. a short, a position opened before the earliest fill
    Alpaca reports, or a corporate action like a split changing share counts
    Alpaca's raw fill history doesn't reconcile) has no cost basis to compute
    against — that leftover quantity is excluded from closed_trades/by_symbol/
    total rather than guessed at, and surfaced instead in
    `unmatched_sell_qty_by_symbol` so a caller can tell the totals are
    incomplete for that symbol rather than silently trusting an understated
    number.
    """
    now = time.monotonic()
    cached = _realized_pnl_cache.get("data")
    cached_at = _realized_pnl_cache.get("at", 0.0)
    if cached is not None and now - cached_at < _REALIZED_PNL_TTL_SECONDS:
        return cached

    open_lots: dict[str, list[list]] = {}  # symbol -> [[qty, price, time], ...], oldest first
    closed_trades: list[dict] = []
    unmatched_sell_qty: dict[str, float] = {}
    for fill in get_fill_activities():
        symbol, side, qty, price = fill["symbol"], fill["side"], fill["qty"], fill["price"]
        lots = open_lots.setdefault(symbol, [])
        if side == "buy":
            lots.append([qty, price, fill["transaction_time"]])
            continue

        remaining = qty
        while remaining > 1e-9 and lots:
            lot_qty, lot_price, lot_time = lots[0]
            matched = min(lot_qty, remaining)
            closed_trades.append({
                "symbol": symbol,
                "quantity": matched,
                "buy_price": lot_price,
                "sell_price": price,
                "buy_time": lot_time,
                "sell_time": fill["transaction_time"],
                "realized_pnl": (price - lot_price) * matched,
            })
            remaining -= matched
            if lot_qty - matched <= 1e-9:
                lots.pop(0)
            else:
                lots[0][0] = lot_qty - matched
        if remaining > 1e-9:
            unmatched_sell_qty[symbol] = unmatched_sell_qty.get(symbol, 0.0) + remaining

    by_symbol: dict[str, float] = {}
    for trade in closed_trades:
        by_symbol[trade["symbol"]] = by_symbol.get(trade["symbol"], 0.0) + trade["realized_pnl"]

    result = {
        "closed_trades": closed_trades,
        "by_symbol": by_symbol,
        "total": sum(by_symbol.values()),
        "unmatched_sell_qty_by_symbol": unmatched_sell_qty,
    }
    _realized_pnl_cache["data"] = result
    _realized_pnl_cache["at"] = now
    return result


def get_quote(symbol: str) -> dict:
    """Latest real bid/ask quote for a symbol, plus the derived midpoint and
    the spread as a fraction of the midpoint. `get_latest_price` is a thin
    wrapper around this; accounts.py also uses `spread_pct` directly for a
    pre-submission wide-spread warning, since market orders can fill well
    away from the midpoint when the spread is wide (thin liquidity)."""
    request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
    quotes = data_client().get_stock_latest_quote(request)
    if symbol not in quotes:
        raise ValueError(f"No live quote available for '{symbol}' — check it's a valid, tradable symbol.")
    quote = quotes[symbol]
    bid, ask = quote.bid_price, quote.ask_price
    if bid and ask:
        mid = round((bid + ask) / 2, 4)
        spread_pct = (ask - bid) / mid if mid else None
    else:
        mid = float(ask or bid)
        spread_pct = None  # only one side quoted — nothing to compute a spread from
    return {"bid": bid, "ask": ask, "mid": mid, "spread_pct": spread_pct}


def get_latest_price(symbol: str) -> float:
    """Midpoint of the latest real bid/ask for a symbol."""
    return get_quote(symbol)["mid"]


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