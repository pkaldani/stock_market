from pydantic import BaseModel
import json
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta
from . import alpaca_broker
from .database import write_account, read_account, write_log

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

    def buy_shares(self, symbol: str, quantity: int, rationale: str, source: str = "agent") -> str:
        """ Buy shares via a REAL Alpaca market order. """
        self._check_hold_period(symbol)
        price = alpaca_broker.get_latest_price(symbol)
        estimated_cost = price * quantity

        account_info = alpaca_broker.get_account_info()
        if estimated_cost > account_info["buying_power"]:
            raise ValueError(
                f"Insufficient real buying power (${account_info['buying_power']:.2f}) "
                f"in the Alpaca account to cover this order."
            )

        order = alpaca_broker.submit_market_order(symbol, quantity, "buy")

        # Use the estimate for immediate bookkeeping; true up with the actual
        # fill once it's available (market orders usually fill within seconds
        # during market hours).
        fill_price = price
        filled = alpaca_broker.get_order(order["id"])
        if filled["filled_avg_price"]:
            fill_price = filled["filled_avg_price"]

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        transaction = Transaction(
            symbol=symbol, quantity=quantity, price=fill_price, timestamp=timestamp,
            rationale=rationale, order_id=order["id"], order_status=order["status"], source=source,
        )
        self.transactions.append(transaction)
        self.save()
        write_log(self.name, "account", f"BUY {quantity} {symbol} (order {order['id']}, status={order['status']}, source={source})")
        return "Order submitted. Latest details:\n" + self.report()

    def sell_shares(self, symbol: str, quantity: int, rationale: str, source: str = "agent") -> str:
        """ Sell shares via a REAL Alpaca market order. """
        self._check_hold_period(symbol)
        if self.holdings.get(symbol, 0) < quantity:
            raise ValueError(f"Cannot sell {quantity} shares of {symbol}. Alpaca account does not hold enough shares.")

        price = alpaca_broker.get_latest_price(symbol)
        order = alpaca_broker.submit_market_order(symbol, quantity, "sell")

        fill_price = price
        filled = alpaca_broker.get_order(order["id"])
        if filled["filled_avg_price"]:
            fill_price = filled["filled_avg_price"]

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        transaction = Transaction(
            symbol=symbol, quantity=-quantity, price=fill_price, timestamp=timestamp,
            rationale=rationale, order_id=order["id"], order_status=order["status"], source=source,
        )
        self.transactions.append(transaction)
        self.save()
        write_log(self.name, "account", f"SELL {quantity} {symbol} (order {order['id']}, status={order['status']}, source={source})")
        return "Order submitted. Latest details:\n" + self.report()

    def calculate_portfolio_value(self) -> float:
        """ The real Alpaca account's total portfolio value (cash + positions). """
        return alpaca_broker.get_account_info()["portfolio_value"]

    def calculate_profit_loss(self) -> float:
        """ Unrealized P&L summed across all real Alpaca positions, as Alpaca itself computes it. """
        return sum(p["unrealized_pl"] for p in alpaca_broker.get_real_positions_detail())

    def get_holdings(self):
        return self.holdings

    def list_transactions(self):
        return [transaction.model_dump() for transaction in self.transactions]

    def report(self) -> str:
        """ Return a json string representing the account.  """
        portfolio_value = self.calculate_portfolio_value()
        self.portfolio_value_time_series.append((datetime.now().strftime("%Y-%m-%d %H:%M:%S"), portfolio_value))
        self.save()
        pnl = self.calculate_profit_loss()
        data = self.model_dump()
        data["balance"] = self.balance
        data["holdings"] = self.holdings
        data["total_portfolio_value"] = portfolio_value
        data["total_profit_loss"] = pnl
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
