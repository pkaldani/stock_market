import mcp
from pathlib import Path
from mcp.client.stdio import stdio_client
from mcp import StdioServerParameters

params = StdioServerParameters(
    command="uv",
    args=["run", "-m", "backend.accounts_server"],
    cwd=str(Path(__file__).resolve().parent.parent),
    env=None,
)

async def read_accounts_resource():
    async with stdio_client(params) as streams:
        async with mcp.ClientSession(*streams) as session:
            await session.initialize()
            result = await session.read_resource("accounts://accounts_server")
            return result.contents[0].text

async def read_strategy_resource():
    async with stdio_client(params) as streams:
        async with mcp.ClientSession(*streams) as session:
            await session.initialize()
            result = await session.read_resource("accounts://strategy")
            return result.contents[0].text
