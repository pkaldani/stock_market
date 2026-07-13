from pydantic import BaseModel
import json
from dotenv import load_dotenv
from datetime import datetime
from . import alpaca_broker
from .database import write_account, read_account, write_log

load_dotenv(override=True)

# Starting virtual cash allocated to EACH trader out of the one real Alpaca
# account. Four traders x this amount should be <= your real account's cash,
# or you're implicitly letting them share/overlap buying power.
INITIAL_BALANCE = 10_000.0


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
    def get(cls, name: str):
        fields = read_account(name.lower())
        if not fields:
            fields = {
                "name": name.lower(),
                "balance": INITIAL_BALANCE,
                "strategy": "",
                "holdings": {},
                "transactions": [],
                "portfolio_value_time_series": []
            }
            write_account(name, fields)
        return cls(**fields)

    def save(self):
        write_account(self.name.lower(), self.model_dump())

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
    # Cross-trader safety check: since all traders share ONE real Alpaca
    # account, before letting trader X sell N shares of SYMBOL we confirm
    # the real account actually still holds at least N shares that aren't
    # already spoken for by other traders' virtual holdings of the same
    # symbol booked after X's. This prevents two traders from both trying
    # to sell shares that, virtually, they each believe they own.
    # ------------------------------------------------------------------
    @staticmethod
    def _real_position_is_sufficient(symbol: str, quantity: int) -> bool:
        real_positions = alpaca_broker.get_real_positions()
        return real_positions.get(symbol, 0) >= quantity

    def buy_shares(self, symbol: str, quantity: int, rationale: str) -> str:
        """ Buy shares via a REAL Alpaca market order, tracked against this
        trader's virtual cash ledger. """
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
        if self.holdings.get(symbol, 0) < quantity:
            raise ValueError(f"Cannot sell {quantity} shares of {symbol}. Not enough virtual shares held.")

        if not self._real_position_is_sufficient(symbol, quantity):
            raise ValueError(
                f"Real Alpaca account does not currently hold {quantity} shares of {symbol} "
                f"(may already be sold by another trader sharing this account)."
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