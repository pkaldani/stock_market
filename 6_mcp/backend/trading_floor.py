from .traders import Trader
from .accounts import TRADER_NAME
import asyncio
from .tracers import LogTracer
from agents import add_trace_processor
from . import alpaca_broker
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


def _is_market_open() -> bool:
    """Real Alpaca clock (same source accounts.py's order-safety checks use),
    wrapped so a transient API/network failure can't crash the whole
    scheduler loop — this only gates whether to bother running at all, and
    buy_shares/sell_shares already handle a genuinely-closed market safely
    (order queues for next open) if this defaults wrong."""
    try:
        return alpaca_broker.is_market_open()
    except Exception as e:
        print(f"Could not determine market status ({e}) — proceeding with a scheduled run anyway")
        return True


async def run_every_n_minutes():
    add_trace_processor(LogTracer())
    trader = create_trader()
    while True:
        if RUN_EVEN_WHEN_MARKET_IS_CLOSED or _is_market_open():
            await trader.run()
        else:
            print("Market is closed, skipping run")
        await asyncio.sleep(RUN_EVERY_N_MINUTES * 60)


if __name__ == "__main__":
    print(f"Starting scheduler to run every {RUN_EVERY_N_MINUTES} minutes")
    asyncio.run(run_every_n_minutes())
