from pydantic import BaseModel
import json
import os
import time
from dotenv import load_dotenv
from datetime import datetime, timedelta
from . import alpaca_broker
from .database import write_account, read_account, write_log, account_transaction
from .symbol_whitelist import is_symbol_allowed, get_symbol_sector

load_dotenv(override=True)

# There's exactly one trader now, so this is the single SQLite/account key —
# not a persona name, just an identifier (Alpaca's personal Trading API has
# no human "name" field to pull one from).
TRADER_NAME = "pkaldani"

# Minimum time that must pass between transactions on the same symbol, in
# either direction, before another buy/sell of it is allowed. This is the
# day-trading guardrail: it blocks opening and closing (or re-opening) a
# position in the same ticker within one window.
MIN_HOLD_HOURS = float(os.getenv("MIN_HOLD_HOURS", "24"))

# Circuit breaker: hard cap on how many orders (buy+sell combined) this
# account can place within a rolling 24h window, independent of the
# per-symbol MIN_HOLD_HOURS guardrail above — that one only limits re-trading
# the SAME symbol; this bounds a runaway loop hitting many DIFFERENT symbols
# in one run. Generous enough not to interfere with normal use (the symbol
# whitelist has 20 names; a real run trades a handful at most) but caps the
# worst case from a malfunctioning run.
MAX_ORDERS_PER_24H = int(os.getenv("MAX_ORDERS_PER_24H", "15"))

# Position-sizing limits from the decision mandate (templates.py's Step 4),
# enforced here rather than left as prompt-only text the model could ignore
# or miscompute. Expressed as % of total portfolio value (cash + positions).
# MAX_POSITION_PCT mirrors templates.py's "never exceed [X]%... default to
# 10% if not specified"; MAX_SECTOR_PCT mirrors its "never exceed 30% of
# portfolio in a single sector".
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "10")) / 100
MAX_SECTOR_PCT = float(os.getenv("MAX_SECTOR_PCT", "30")) / 100

# Warn (don't block — market-order-only is a deliberate design choice) when
# the bid/ask spread is wide enough that a market order could fill well away
# from the quoted midpoint, a sign of thin liquidity.
WIDE_SPREAD_PCT_THRESHOLD = 0.02

# Order statuses Alpaca reports as final. Anything else (new, accepted,
# pending_new, partially_filled, ...) may still resolve further and is
# revisited by reconcile_pending_transactions().
_TERMINAL_ORDER_STATUSES = {
    "filled", "canceled", "expired", "rejected", "done_for_day",
    "replaced", "stopped", "suspended", "calculated",
}

# How hard to poll right after submitting an order before accepting whatever
# status is seen and falling back to reconcile_pending_transactions() later.
# Market orders during market hours typically fill within a second or two.
_FILL_POLL_ATTEMPTS = 4
_FILL_POLL_DELAY_SECONDS = 0.5


def _poll_for_fill(order_id: str) -> dict:
    """Poll an order a few times right after submission, so a fast round-trip
    doesn't record a stale pre-trade quote/quantity as if the order had
    already resolved. Not a guarantee — if the order is still open after
    this, the caller records it as provisional and
    reconcile_pending_transactions() revisits it on a later call."""
    filled = alpaca_broker.get_order(order_id)
    for _ in range(_FILL_POLL_ATTEMPTS - 1):
        if filled["status"] in _TERMINAL_ORDER_STATUSES:
            break
        time.sleep(_FILL_POLL_DELAY_SECONDS)
        filled = alpaca_broker.get_order(order_id)
    return filled


class Transaction(BaseModel):
    symbol: str
    quantity: int
    price: float
    timestamp: str
    rationale: str
    order_id: str = ""
    order_status: str = ""
    # "agent" (the LLM trader, via its MCP tools) or "manual" (placed by a
    # human through the dashboard's trade page). Defaults to "agent" so
    # existing rows written before this field existed still validate.
    source: str = "agent"

    def total(self) -> float:
        return self.quantity * self.price

    def __repr__(self):
        return f"{abs(self.quantity)} shares of {self.symbol} at {self.price} each."


class Account(BaseModel):
    """There's exactly one real Alpaca account behind this trader, so
    balance/holdings/portfolio value/P&L are not tracked separately here —
    they're live reads of that account (via alpaca_broker), always fresh at
    the point of use. What IS stored locally is this app's own state: the
    evolvable strategy text and its own log of the orders it placed (for
    rationale/history — Alpaca's positions may also include activity from
    outside this app, e.g. manual trades)."""

    name: str
    strategy: str
    transactions: list[Transaction]
    portfolio_value_time_series: list[tuple[str, float]]

    @property
    def balance(self) -> float:
        return alpaca_broker.get_account_info()["cash"]

    @property
    def holdings(self) -> dict[str, int]:
        return alpaca_broker.get_real_positions()

    @classmethod
    def get(cls) -> "Account":
        fields = read_account(TRADER_NAME)
        if not fields:
            fields = {
                "name": TRADER_NAME,
                "strategy": "",
                "transactions": [],
                "portfolio_value_time_series": []
            }
            write_account(TRADER_NAME, fields)
        return cls(**fields)

    def save(self):
        write_account(TRADER_NAME, self.model_dump())

    def reset(self, strategy: str = ""):
        """Reset this app's own state (strategy text, its order history).
        Does not touch the real Alpaca account — that money and those
        positions are real and can't be reset."""
        self.strategy = strategy
        self.transactions = []
        self.portfolio_value_time_series = []
        self.save()

    def _last_transaction_time(self, symbol: str) -> datetime | None:
        """Timestamp of the most recent buy or sell of this symbol, if any."""
        matches = [t for t in self.transactions if t.symbol == symbol]
        if not matches:
            return None
        return datetime.strptime(matches[-1].timestamp, "%Y-%m-%d %H:%M:%S")

    def _check_hold_period(self, symbol: str) -> None:
        """Block same-symbol round-trips within MIN_HOLD_HOURS (day-trading guardrail)."""
        last_trade = self._last_transaction_time(symbol)
        if last_trade and datetime.now() - last_trade < timedelta(hours=MIN_HOLD_HOURS):
            eligible_at = last_trade + timedelta(hours=MIN_HOLD_HOURS)
            raise ValueError(
                f"Cannot trade {symbol} again yet: last transaction was at "
                f"{last_trade.strftime('%Y-%m-%d %H:%M:%S')}, minimum hold period is "
                f"{MIN_HOLD_HOURS} hours (eligible again at {eligible_at.strftime('%Y-%m-%d %H:%M:%S')})."
            )

    def _check_symbol_allowed(self, symbol: str) -> None:
        """Block buying anything outside the approved symbol whitelist (buys only —
        selling an existing holding is always allowed, even for a symbol that's not,
        or no longer, on the list)."""
        if not is_symbol_allowed(symbol):
            raise ValueError(
                f"{symbol} is not on the approved symbol whitelist "
                f"(backend/symbol_whitelist.yaml) — buying it is not allowed."
            )

    def _check_daily_order_cap(self) -> None:
        """Circuit breaker against a runaway loop placing many orders across
        many different symbols in a short span — MIN_HOLD_HOURS only guards
        against re-trading the SAME symbol, not this."""
        cutoff = datetime.now() - timedelta(hours=24)
        recent = [
            t for t in self.transactions
            if datetime.strptime(t.timestamp, "%Y-%m-%d %H:%M:%S") >= cutoff
        ]
        if len(recent) >= MAX_ORDERS_PER_24H:
            raise ValueError(
                f"Daily order cap reached ({len(recent)}/{MAX_ORDERS_PER_24H} orders in the last "
                "24h) — refusing to place another order until the window rolls forward. This is a "
                "circuit breaker against a runaway loop, not a normal trading limit."
            )

    def _check_position_limits(self, symbol: str, order_value: float) -> None:
        """Block a BUY that would push either the single-position or the
        sector-concentration limit past its cap (MAX_POSITION_PCT /
        MAX_SECTOR_PCT above) — the code-level enforcement of the decision
        mandate's Step 4 limits (templates.py), which until now existed only
        as prompt text: nothing read the agent's final JSON to gate on it,
        and buy_shares had no check of its own. Sells are never checked here
        since they only reduce exposure. Skips the check entirely if
        portfolio_value is 0 (nothing to size a % against yet, e.g. a fresh
        account)."""
        portfolio_value = self.calculate_portfolio_value()
        if portfolio_value <= 0:
            return

        positions = alpaca_broker.get_real_positions_detail()
        existing_position_value = next(
            (p["market_value"] for p in positions if p["symbol"] == symbol), 0.0
        )
        position_pct = (existing_position_value + order_value) / portfolio_value
        if position_pct > MAX_POSITION_PCT:
            raise ValueError(
                f"BUY_BLOCKED_BY_LIMITS: this order would put {symbol} at {position_pct:.1%} of "
                f"portfolio value, exceeding the {MAX_POSITION_PCT:.0%} single-position limit."
            )

        sector = get_symbol_sector(symbol) or "Unclassified"
        sector_value = sum(
            p["market_value"] for p in positions
            if (get_symbol_sector(p["symbol"]) or "Unclassified") == sector
        )
        sector_pct = (sector_value + order_value) / portfolio_value
        if sector_pct > MAX_SECTOR_PCT:
            raise ValueError(
                f"BUY_BLOCKED_BY_LIMITS: this order would put the '{sector}' sector at "
                f"{sector_pct:.1%} of portfolio value, exceeding the {MAX_SECTOR_PCT:.0%} "
                f"sector-concentration limit."
            )

    def _append_transaction(self, transaction: Transaction) -> None:
        """Append one transaction via a locked, freshly-read read-modify-write
        cycle (database.account_transaction), then refresh self.transactions
        to match what was actually persisted. A plain
        self.transactions.append(...) + self.save() would race with a
        concurrent buy/sell call from the other process that can write this
        account (the trading-floor engine running the LLM trader, and
        api.py's manual-trade endpoints) and silently drop one of the two."""
        with account_transaction(self.name) as fields:
            fields["transactions"].append(transaction.model_dump())
            self.transactions = [Transaction(**t) for t in fields["transactions"]]

    def _append_portfolio_value_point(self, point: tuple[str, float]) -> None:
        """Same locked pattern as _append_transaction, for the portfolio
        value time series appended by report()."""
        with account_transaction(self.name) as fields:
            fields["portfolio_value_time_series"].append(list(point))
            self.portfolio_value_time_series = [tuple(x) for x in fields["portfolio_value_time_series"]]

    def reconcile_pending_transactions(self) -> int:
        """Revisit any locally-recorded transaction whose order hadn't
        reached a terminal status by the time buy_shares/sell_shares finished
        polling for it, and patch its quantity/price/status now that more
        time has passed. Real market orders normally resolve almost
        instantly, so this is a rare, cheap sweep — called at the top of
        buy_shares/sell_shares so stale entries don't linger. The Alpaca
        lookups happen before acquiring the write lock, so a slow network
        round-trip never blocks other writers."""
        pending = [
            t for t in self.transactions
            if t.order_id and t.order_status not in _TERMINAL_ORDER_STATUSES
        ]
        if not pending:
            return 0

        patches: dict[str, dict] = {}
        for t in pending:
            try:
                current = alpaca_broker.get_order(t.order_id)
            except Exception as e:
                write_log(self.name, "account", f"Could not reconcile order {t.order_id} for {t.symbol}: {e}")
                continue
            if current["status"] == t.order_status:
                continue
            patch = {"order_status": current["status"]}
            if current["status"] in _TERMINAL_ORDER_STATUSES:
                # Final answer: replace the provisional quantity/price with
                # the real outcome now that the order has fully resolved.
                # Sign is taken from the original (never-yet-patched)
                # quantity, which is always the correctly-signed requested
                # amount until this first — and only — quantity patch.
                was_buy = t.quantity >= 0
                filled_qty = int(current["filled_qty"]) if current["filled_qty"] else 0
                patch["quantity"] = filled_qty if was_buy else -filled_qty
                if current["filled_avg_price"]:
                    patch["price"] = current["filled_avg_price"]
            patches[t.order_id] = patch

        if not patches:
            return 0

        with account_transaction(self.name) as fields:
            transactions = [Transaction(**t) for t in fields["transactions"]]
            for t in transactions:
                if t.order_id in patches:
                    for key, value in patches[t.order_id].items():
                        setattr(t, key, value)
            fields["transactions"] = [t.model_dump() for t in transactions]
            self.transactions = transactions

        write_log(self.name, "account", f"Reconciled {len(patches)} pending transaction(s) against Alpaca order status")
        return len(patches)

    def buy_shares(self, symbol: str, quantity: int, rationale: str, source: str = "agent") -> str:
        """ Buy shares via a REAL Alpaca market order. """
        self.reconcile_pending_transactions()
        self._check_symbol_allowed(symbol)
        self._check_hold_period(symbol)
        self._check_daily_order_cap()

        quote = alpaca_broker.get_quote(symbol)
        price = quote["mid"]
        if quote["spread_pct"] is not None and quote["spread_pct"] > WIDE_SPREAD_PCT_THRESHOLD:
            write_log(
                self.name, "account",
                f"WARNING: wide bid/ask spread on {symbol} ({quote['spread_pct']:.2%}) — this market "
                f"order may fill well away from the ${price:.2f} midpoint used for sizing."
            )
        estimated_cost = price * quantity
        self._check_position_limits(symbol, estimated_cost)

        account_info = alpaca_broker.get_account_info()
        if estimated_cost > account_info["buying_power"]:
            raise ValueError(
                f"Insufficient real buying power (${account_info['buying_power']:.2f}) "
                f"in the Alpaca account to cover this order."
            )

        try:
            market_open = alpaca_broker.is_market_open()
        except Exception:
            # Informational only — never block a real order over this check failing.
            market_open = True
        if not market_open:
            write_log(
                self.name, "account",
                f"NOTE: market is currently closed — this BUY order for {symbol} will queue and fill "
                f"at the next session's open, not at the ${price:.2f} quote used for this estimate."
            )

        order = alpaca_broker.submit_market_order(symbol, quantity, "buy")
        filled = _poll_for_fill(order["id"])
        order_status = filled["status"]
        if order_status in _TERMINAL_ORDER_STATUSES:
            filled_qty = int(filled["filled_qty"]) if filled["filled_qty"] else 0
            recorded_qty = filled_qty
            fill_price = filled["filled_avg_price"] or price
        else:
            # Still not resolved after polling — record the requested
            # quantity as a provisional placeholder rather than silently
            # treating an open order as if it had already filled at this
            # estimate. reconcile_pending_transactions() patches this once
            # the order actually resolves.
            recorded_qty = quantity
            fill_price = price

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        transaction = Transaction(
            symbol=symbol, quantity=recorded_qty, price=fill_price, timestamp=timestamp,
            rationale=rationale, order_id=order["id"], order_status=order_status, source=source,
        )
        self._append_transaction(transaction)
        write_log(self.name, "account", f"BUY {recorded_qty} {symbol} (order {order['id']}, status={order_status}, source={source})")
        if order_status not in _TERMINAL_ORDER_STATUSES:
            write_log(self.name, "account", f"BUY order {order['id']} for {symbol} still {order_status} after polling — recorded provisionally, will reconcile on a later call")
        return "Order submitted. Latest details:\n" + self.report()

    def sell_shares(self, symbol: str, quantity: int, rationale: str, source: str = "agent") -> str:
        """ Sell shares via a REAL Alpaca market order. """
        self.reconcile_pending_transactions()
        self._check_hold_period(symbol)
        self._check_daily_order_cap()
        if self.holdings.get(symbol, 0) < quantity:
            raise ValueError(f"Cannot sell {quantity} shares of {symbol}. Alpaca account does not hold enough shares.")

        quote = alpaca_broker.get_quote(symbol)
        price = quote["mid"]
        if quote["spread_pct"] is not None and quote["spread_pct"] > WIDE_SPREAD_PCT_THRESHOLD:
            write_log(
                self.name, "account",
                f"WARNING: wide bid/ask spread on {symbol} ({quote['spread_pct']:.2%}) — this market "
                f"order may fill well away from the ${price:.2f} midpoint used for sizing."
            )

        try:
            market_open = alpaca_broker.is_market_open()
        except Exception:
            market_open = True
        if not market_open:
            write_log(
                self.name, "account",
                f"NOTE: market is currently closed — this SELL order for {symbol} will queue and fill "
                f"at the next session's open, not at the ${price:.2f} quote used for this estimate."
            )

        order = alpaca_broker.submit_market_order(symbol, quantity, "sell")
        filled = _poll_for_fill(order["id"])
        order_status = filled["status"]
        if order_status in _TERMINAL_ORDER_STATUSES:
            filled_qty = int(filled["filled_qty"]) if filled["filled_qty"] else 0
            recorded_qty = filled_qty
            fill_price = filled["filled_avg_price"] or price
        else:
            recorded_qty = quantity
            fill_price = price

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        transaction = Transaction(
            symbol=symbol, quantity=-recorded_qty, price=fill_price, timestamp=timestamp,
            rationale=rationale, order_id=order["id"], order_status=order_status, source=source,
        )
        self._append_transaction(transaction)
        write_log(self.name, "account", f"SELL {recorded_qty} {symbol} (order {order['id']}, status={order_status}, source={source})")
        if order_status not in _TERMINAL_ORDER_STATUSES:
            write_log(self.name, "account", f"SELL order {order['id']} for {symbol} still {order_status} after polling — recorded provisionally, will reconcile on a later call")
        return "Order submitted. Latest details:\n" + self.report()

    def calculate_portfolio_value(self) -> float:
        """ The real Alpaca account's total portfolio value (cash + positions). """
        return alpaca_broker.get_account_info()["portfolio_value"]

    def calculate_profit_loss(self) -> float:
        """ Unrealized P&L summed across all real Alpaca positions, as Alpaca itself computes it. """
        return sum(p["unrealized_pl"] for p in alpaca_broker.get_real_positions_detail())

    def get_realized_pnl(self) -> dict:
        """Full FIFO-matched realized P&L — closed-trade ledger plus per-symbol
        and total rollups. See alpaca_broker.get_realized_pnl for the matching
        logic; realized_pnl_summary() below is the compact version folded into
        report() automatically."""
        return alpaca_broker.get_realized_pnl()

    def realized_pnl_summary(self) -> dict:
        """Compact realized-P&L/win-rate summary, included in report() so
        every trade/rebalance cycle's account context carries the trader's
        own closed-trade track record — not just current unrealized P&L on
        still-open positions, which is all report() surfaced before this.
        Use get_realized_pnl() for the full trade-by-trade ledger behind
        these numbers.

        `incomplete_for_symbols` lists any symbol with a sell quantity that
        FIFO matching couldn't pair against a known open lot (see
        alpaca_broker.get_realized_pnl's unmatched_sell_qty_by_symbol) — for
        those symbols total_realized_pnl/win_rate_pct are understated, not
        wrong-but-complete, since the unmatched portion is excluded rather
        than guessed at."""
        data = alpaca_broker.get_realized_pnl()
        closed = data["closed_trades"]
        incomplete_for_symbols = sorted(data["unmatched_sell_qty_by_symbol"].keys())
        if not closed:
            return {
                "total_realized_pnl": 0.0, "closed_trade_count": 0, "win_rate_pct": None,
                "avg_win": None, "avg_loss": None, "best_trade_pnl": None, "worst_trade_pnl": None,
                "incomplete_for_symbols": incomplete_for_symbols,
            }
        pnls = [t["realized_pnl"] for t in closed]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        return {
            "total_realized_pnl": round(data["total"], 2),
            "closed_trade_count": len(closed),
            "win_rate_pct": round(100 * len(wins) / len(closed), 1),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
            "best_trade_pnl": round(max(pnls), 2),
            "worst_trade_pnl": round(min(pnls), 2),
            "incomplete_for_symbols": incomplete_for_symbols,
        }

    def sector_exposure(self) -> dict[str, float]:
        """Current portfolio exposure by sector, as a % of TOTAL portfolio
        value — cash + positions, calculate_portfolio_value()'s definition,
        the same denominator _check_position_limits uses to enforce
        MAX_SECTOR_PCT. (Previously this divided by invested capital only —
        sum of position market values — so sectors always summed to 100%
        regardless of cash on hand, silently inflating every reported %
        versus what the enforcement check and everyone else in this codebase
        means by "portfolio value".) Computed from live Alpaca position data
        cross-referenced against each symbol's sector tag in
        symbol_whitelist.yaml; a held symbol outside the whitelist (e.g. a
        legacy position) is grouped under 'Unclassified'. Percentages here
        no longer sum to 100% — the remainder is uninvested cash."""
        portfolio_value = self.calculate_portfolio_value()
        if portfolio_value <= 0:
            return {}
        positions = alpaca_broker.get_real_positions_detail()
        by_sector: dict[str, float] = {}
        for p in positions:
            sector = get_symbol_sector(p["symbol"]) or "Unclassified"
            by_sector[sector] = by_sector.get(sector, 0.0) + p["market_value"]
        return {sector: round(100 * value / portfolio_value, 1) for sector, value in by_sector.items()}

    def get_holdings(self):
        return self.holdings

    def list_transactions(self):
        return [transaction.model_dump() for transaction in self.transactions]

    def report(self) -> str:
        """ Return a json string representing the account.  """
        # reconcile_pending_transactions() otherwise only runs as a side
        # effect of the NEXT buy_shares/sell_shares call — a symbol with no
        # further trading activity could stay stuck showing a stale
        # provisional quantity/price/order_status indefinitely even after
        # the real order resolved. report() is called at the start of every
        # scheduled trader run (Trader.get_account_report(), regardless of
        # whether that run ends up trading anything) and after every
        # buy/sell, so hooking it here gives reconciliation a proactive
        # trigger instead of purely an incidental one. Cheap when nothing is
        # pending — reconcile_pending_transactions() early-returns without
        # any Alpaca calls in that (overwhelmingly common) case.
        self.reconcile_pending_transactions()
        portfolio_value = self.calculate_portfolio_value()
        self._append_portfolio_value_point((datetime.now().strftime("%Y-%m-%d %H:%M:%S"), portfolio_value))
        pnl = self.calculate_profit_loss()
        data = self.model_dump()
        data["balance"] = self.balance
        data["holdings"] = self.holdings
        data["total_portfolio_value"] = portfolio_value
        data["total_profit_loss"] = pnl
        data["realized_pnl_summary"] = self.realized_pnl_summary()
        data["sector_exposure_pct"] = self.sector_exposure()
        write_log(self.name, "account", "Retrieved account details")
        return json.dumps(data)

    def get_strategy(self) -> str:
        write_log(self.name, "account", "Retrieved strategy")
        return self.strategy

    def change_strategy(self, strategy: str) -> str:
        self.strategy = strategy
        self.save()
        write_log(self.name, "account", "Changed strategy")
        return "Changed strategy"
