from mcp.server.fastmcp import FastMCP
from .accounts import Account

mcp = FastMCP("accounts_server")

@mcp.tool()
async def get_balance() -> float:
    """Get the trader's cash balance."""
    return Account.get().balance

@mcp.tool()
async def get_holdings() -> dict[str, int]:
    """Get the trader's current holdings."""
    return Account.get().holdings

@mcp.tool()
async def buy_shares(symbol: str, quantity: int, rationale: str) -> float:
    """Buy shares of a stock.

    Args:
        symbol: The symbol of the stock
        quantity: The quantity of shares to buy
        rationale: The rationale for the purchase and fit with the account's strategy
    """
    return Account.get().buy_shares(symbol, quantity, rationale)


@mcp.tool()
async def sell_shares(symbol: str, quantity: int, rationale: str) -> float:
    """Sell shares of a stock.

    Args:
        symbol: The symbol of the stock
        quantity: The quantity of shares to sell
        rationale: The rationale for the sale and fit with the account's strategy
    """
    return Account.get().sell_shares(symbol, quantity, rationale)

@mcp.tool()
async def get_realized_pnl() -> dict:
    """Get realized profit/loss from closed trades (FIFO-matched): a full
    trade-by-trade ledger plus per-symbol and total rollups. Your account
    context already includes a compact realized_pnl_summary (win rate,
    average win/loss, best/worst trade) on every run — use this tool when you
    want the full closed-trade detail behind that summary, e.g. to review
    which past trades actually worked before updating your strategy."""
    return Account.get().get_realized_pnl()

@mcp.tool()
async def change_strategy(strategy: str) -> str:
    """At your discretion, if you choose to, call this to change your investment strategy for the future.

    Args:
        strategy: The new strategy for the account
    """
    return Account.get().change_strategy(strategy)

@mcp.resource("accounts://accounts_server")
async def read_account_resource() -> str:
    return Account.get().report()

@mcp.resource("accounts://strategy")
async def read_strategy_resource() -> str:
    return Account.get().get_strategy()

if __name__ == "__main__":
    mcp.run(transport='stdio')
