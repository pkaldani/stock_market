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

# Starting virtual cash for the trader, tracked against the one real Alpaca
# account's actual cash/buying power. Should be <= the real account's cash.
INITIAL_BALANCE = 10_000.0

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

    def total(self) -> float:
        return self.quantity * self.price

    def __repr__(self):
        return f"{abs(self.quantity)} shares of {self.symbol} at {self.price} each."


class Account(BaseModel):
    name: str
    balance: float
    strategy: str
    holdings: dict[str, int]
    transactions: list[Transaction]
    portfolio_value_time_series: list[tuple[str, float]]

    @classmethod
    def get(cls) -> "Account":
        fields = read_account(TRADER_NAME)
        if not fields:
            fields = {
                "name": TRADER_NAME,
                "balance": INITIAL_BALANCE,
                "strategy": "",
                "holdings": {},
                "transactions": [],
                "portfolio_value_time_series": []
            }
            write_account(TRADER_NAME, fields)
        return cls(**fields)

    def save(self):
        write_account(TRADER_NAME, self.model_dump())

    def reset(self, strategy: str = ""):
        self.balance = INITIAL_BALANCE
        self.strategy = strategy
        self.holdings = {}
        self.transactions = []
        self.portfolio_value_time_series = []
        self.save()

    def deposit(self, amount: float):
        """ Deposit funds into the virtual ledger (does not move real money). """
        if amount <= 0:
            raise ValueError("Deposit amount must be positive.")
        self.balance += amount
        self.save()

    def withdraw(self, amount: float):
        """ Withdraw funds from the virtual ledger, ensuring it doesn't go negative. """
        if amount > self.balance:
            raise ValueError("Insufficient funds for withdrawal.")
        self.balance -= amount
        self.save()

    # ------------------------------------------------------------------
    # Sanity check: the virtual ledger and the real Alpaca account should
    # agree, but they're updated independently (fills, manual trades placed
    # outside this app, etc. can make them drift). Before selling, confirm
    # the real account actually still holds at least this many shares
    # rather than trusting the virtual ledger blindly.
    # ------------------------------------------------------------------
    @staticmethod
    def _real_position_is_sufficient(symbol: str, quantity: int) -> bool:
        real_positions = alpaca_broker.get_real_positions()
        return real_positions.get(symbol, 0) >= quantity

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

    def buy_shares(self, symbol: str, quantity: int, rationale: str) -> str:
        """ Buy shares via a REAL Alpaca market order, tracked against this
        trader's virtual cash ledger. """
        self._check_hold_period(symbol)
        price = alpaca_broker.get_latest_price(symbol)
        estimated_cost = price * quantity

        if estimated_cost > self.balance:
            raise ValueError("Insufficient virtual balance to buy shares.")

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

        total_cost = fill_price * quantity
        self.holdings[symbol] = self.holdings.get(symbol, 0) + quantity
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        transaction = Transaction(
            symbol=symbol, quantity=quantity, price=fill_price, timestamp=timestamp,
            rationale=rationale, order_id=order["id"], order_status=order["status"],
        )
        self.transactions.append(transaction)
        self.balance -= total_cost
        self.save()
        write_log(self.name, "account", f"BUY {quantity} {symbol} (order {order['id']}, status={order['status']})")
        return "Order submitted. Latest details:\n" + self.report()

    def sell_shares(self, symbol: str, quantity: int, rationale: str) -> str:
        """ Sell shares via a REAL Alpaca market order, tracked against this
        trader's virtual holdings ledger. """
        self._check_hold_period(symbol)
        if self.holdings.get(symbol, 0) < quantity:
            raise ValueError(f"Cannot sell {quantity} shares of {symbol}. Not enough virtual shares held.")

        if not self._real_position_is_sufficient(symbol, quantity):
            raise ValueError(
                f"Real Alpaca account does not currently hold {quantity} shares of {symbol} "
                f"(the virtual ledger and the real account have drifted apart)."
            )

        price = alpaca_broker.get_latest_price(symbol)
        order = alpaca_broker.submit_market_order(symbol, quantity, "sell")

        fill_price = price
        filled = alpaca_broker.get_order(order["id"])
        if filled["filled_avg_price"]:
            fill_price = filled["filled_avg_price"]

        total_proceeds = fill_price * quantity
        self.holdings[symbol] -= quantity
        if self.holdings[symbol] == 0:
            del self.holdings[symbol]

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        transaction = Transaction(
            symbol=symbol, quantity=-quantity, price=fill_price, timestamp=timestamp,
            rationale=rationale, order_id=order["id"], order_status=order["status"],
        )
        self.transactions.append(transaction)
        self.balance += total_proceeds
        self.save()
        write_log(self.name, "account", f"SELL {quantity} {symbol} (order {order['id']}, status={order['status']})")
        return "Order submitted. Latest details:\n" + self.report()

    def calculate_portfolio_value(self):
        """ Virtual cash + current market value of this trader's virtual holdings. """
        total_value = self.balance
        for symbol, quantity in self.holdings.items():
            total_value += alpaca_broker.get_latest_price(symbol) * quantity
        return total_value

    def calculate_profit_loss(self, portfolio_value: float):
        initial_spend = sum(transaction.total() for transaction in self.transactions)
        return portfolio_value - initial_spend - self.balance

    def get_holdings(self):
        return self.holdings

    def list_transactions(self):
        return [transaction.model_dump() for transaction in self.transactions]

    def report(self) -> str:
        """ Return a json string representing the account.  """
        portfolio_value = self.calculate_portfolio_value()
        self.portfolio_value_time_series.append((datetime.now().strftime("%Y-%m-%d %H:%M:%S"), portfolio_value))
        self.save()
        pnl = self.calculate_profit_loss(portfolio_value)
        data = self.model_dump()
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