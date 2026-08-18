from mcp.server.fastmcp import FastMCP
from . import alpaca_broker

mcp = FastMCP("asset_server")


@mcp.tool()
async def get_asset_info(symbol: str) -> dict:
    """Look up real Alpaca asset metadata for a symbol: exchange, asset class,
    trading status, and whether it's tradable/fractionable/shortable.

    Use this to sanity-check a candidate before recommending it — e.g. confirm
    it's an active, tradable US equity on a real exchange rather than
    inactive, OTC, or a non-equity asset class.

    Args:
        symbol: the symbol of the stock
    """
    return alpaca_broker.get_asset_info(symbol)


if __name__ == "__main__":
    mcp.run(transport='stdio')
