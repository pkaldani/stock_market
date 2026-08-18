import os
from pathlib import Path
from dotenv import load_dotenv
from agents.mcp import MCPServerStdio, create_static_tool_filter


load_dotenv(override=True)

PROJECT_DIR = str(Path(__file__).resolve().parent.parent)
tavily_env = {"TAVILY_API_KEY": os.getenv("TAVILY_API_KEY")}
TIMEOUT = 120

# The market data server for the trader: our own yfinance-backed MCP server
# (backend.market). Real market data only
market_params = {"command": "uv", "args": ["run", "-m", "backend.market_server"], "cwd": PROJECT_DIR}


def trader_mcp_servers() -> list[MCPServerStdio]:
    """The trader's MCP servers: our Accounts server, Push Notification and Market data."""
    params = [
        {"command": "uv", "args": ["run", "-m", "backend.accounts_server"], "cwd": PROJECT_DIR},
        {"command": "uv", "args": ["run", "-m", "backend.push_server"], "cwd": PROJECT_DIR},
        market_params,
    ]
    return [MCPServerStdio(p, client_session_timeout_seconds=TIMEOUT) for p in params]


market_analysis_params = {"command": "uv", "args": ["run", "-m", "backend.market"], "cwd": PROJECT_DIR}
asset_params = {"command": "uv", "args": ["run", "-m", "backend.asset_server"], "cwd": PROJECT_DIR}


def researcher_mcp_servers(name: str) -> list[MCPServerStdio]:
    """The researcher's MCP servers: Fetch, Tavily web search, Memory, technical
    analysis, and real Alpaca asset metadata."""
    fetch = MCPServerStdio(
        # mcp-server-fetch's latest release imports the old `McpError` name, which the
        # `mcp` SDK renamed to `MCPError` — pin below that rename so `uvx` doesn't
        # resolve an incompatible combination.
        {"command": "uvx", "args": ["--with", "mcp<1.28", "mcp-server-fetch"]},
        client_session_timeout_seconds=TIMEOUT,
    )
    search = MCPServerStdio(
        {"command": "npx", "args": ["-y", "tavily-mcp@latest"], "env": tavily_env},
        client_session_timeout_seconds=TIMEOUT,
        tool_filter=create_static_tool_filter(allowed_tool_names=["tavily_search"]),
    )
    memory = MCPServerStdio(
        {
            "command": "npx",
            "args": ["-y", "mcp-memory-libsql"],
            "env": {"LIBSQL_URL": f"file:./memory/{name}.db"},
        },
        client_session_timeout_seconds=TIMEOUT,
    )
    analysis = MCPServerStdio(
        market_analysis_params,
        client_session_timeout_seconds=TIMEOUT,
        tool_filter=create_static_tool_filter(
            allowed_tool_names=[
                "get_current_price",
                "get_historical_data",
                "optimize_indicator_parameters",
                "get_technical_analysis",
                "get_full_report",
            ]
        ),
    )
    assets = MCPServerStdio(asset_params, client_session_timeout_seconds=TIMEOUT)
    return [fetch, search, memory, analysis, assets]