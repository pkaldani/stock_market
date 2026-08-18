from .traders import Trader
from .accounts import TRADER_NAME
import asyncio
from .tracers import LogTracer
from agents import add_trace_processor
from .market import is_market_open
from dotenv import load_dotenv
import os

load_dotenv(override=True)

RUN_EVERY_N_MINUTES = int(os.getenv("RUN_EVERY_N_MINUTES", "1440"))
RUN_EVEN_WHEN_MARKET_IS_CLOSED = (
    os.getenv("RUN_EVEN_WHEN_MARKET_IS_CLOSED", "false").strip().lower() == "true"
)
MODEL_NAME = os.getenv("TRADER_MODEL_NAME", "gpt-5.4-mini")


def create_trader() -> Trader:
    return Trader(TRADER_NAME, MODEL_NAME)


async def run_every_n_minutes():
    add_trace_processor(LogTracer())
    trader = create_trader()
    while True:
        if RUN_EVEN_WHEN_MARKET_IS_CLOSED or is_market_open():
            await trader.run()
        else:
            print("Market is closed, skipping run")
        await asyncio.sleep(RUN_EVERY_N_MINUTES * 60)


if __name__ == "__main__":
    print(f"Starting scheduler to run every {RUN_EVERY_N_MINUTES} minutes")
    asyncio.run(run_every_n_minutes())
