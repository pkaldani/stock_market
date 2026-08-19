import yaml
from functools import lru_cache
from pathlib import Path

_PATH = Path(__file__).parent / "symbol_whitelist.yaml"


@lru_cache(maxsize=1)
def get_symbol_whitelist() -> frozenset[str]:
    with open(_PATH) as f:
        data = yaml.safe_load(f)
    return frozenset(symbol.upper() for symbol in data["symbols"])


def is_symbol_allowed(symbol: str) -> bool:
    return symbol.upper() in get_symbol_whitelist()
