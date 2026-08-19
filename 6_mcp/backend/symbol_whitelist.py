import yaml
from functools import lru_cache
from pathlib import Path

_PATH = Path(__file__).parent / "symbol_whitelist.yaml"


@lru_cache(maxsize=1)
def _load() -> dict[str, str]:
    """symbol -> sector, keys upper-cased."""
    with open(_PATH) as f:
        data = yaml.safe_load(f)
    return {symbol.upper(): sector for symbol, sector in data["symbols"].items()}


def get_symbol_whitelist() -> frozenset[str]:
    return frozenset(_load().keys())


def get_symbol_sector(symbol: str) -> str | None:
    """The sector tag for a whitelisted symbol, or None if it isn't on the
    whitelist (e.g. a legacy holding). Backs Account.sector_exposure's
    sector-concentration check in accounts.py."""
    return _load().get(symbol.upper())


def is_symbol_allowed(symbol: str) -> bool:
    return symbol.upper() in get_symbol_whitelist()
