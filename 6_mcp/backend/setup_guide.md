# Connecting the trading floor to Alpaca (paper trading)

## 1. Get your paper API keys
1. Log into https://app.alpaca.markets
2. Switch to the **Paper Trading** dashboard (toggle top-left or go directly to
   https://app.alpaca.markets/paper/dashboard/overview).
3. Click **View API Keys** (or **Generate New Key** if you haven't yet).
4. Copy the **Key ID** and **Secret Key** — the secret is only shown once.

Paper trading uses real, live market data and real order/matching mechanics,
but the account is funded with fake money (default $100,000). It's the right
place to validate this integration before ever touching a live account.

## 2. Install the SDK
```bash
pip install alpaca-py --break-system-packages
```
(or add `alpaca-py` to your project's `pyproject.toml` / `requirements.txt`
and run your normal install command, e.g. `uv add alpaca-py`)

## 3. Add credentials
Copy `.env.example` to `.env` in your project root and fill in the values:
```
ALPACA_API_KEY=your_paper_api_key_id
ALPACA_SECRET_KEY=your_paper_secret_key
ALPACA_PAPER=true
```

## 4. Drop in the new files
- Add `alpaca_broker.py` to your `backend/` package (next to `market.py`).
- Replace your existing `backend/accounts.py` with the updated version here.

Everything else — `accounts_server.py`, `traders.py`, `templates.py`,
`trading_floor.py` — works unchanged, because the public interface of
`Account` (`buy_shares`, `sell_shares`, `report`, etc.) didn't change, only
what happens inside it.

## 5. Sanity-check the connection
```python
from backend import alpaca_broker

print(alpaca_broker.get_account_info())
print(alpaca_broker.is_market_open())
print(alpaca_broker.get_latest_price("AAPL"))
```
You should see your paper account's cash/buying power, whether the market is
open right now, and a live AAPL quote.

## 6. Important: one real account, no virtual ledger
Alpaca's personal Trading API gives you **one account**, and the trader's
`balance`, `holdings`, and portfolio value are all live reads of that real
account (via `alpaca_broker.get_account_info()`/`get_real_positions()`) —
there's no separate SQLite-tracked starting balance to keep in sync with it.

- Every buy/sell places a **real market order** against your Alpaca paper
  account; `balance`/`holdings` reflect the result immediately since they're
  read live, not bookkept locally.
- The only thing kept in local SQLite is this app's own state: the evolvable
  strategy text and its own order history (rationale, timestamps) — useful
  for audit/display, not for balance math.
- Before any sell, the code checks the real Alpaca account actually holds
  enough shares of that symbol right now.

## 7. Going live later
When you're ready:
1. Generate **live** API keys from https://app.alpaca.markets/dashboard/overview
   (separate from paper keys — Alpaca requires your live account to be funded
   and approved first).
2. Update `.env`: swap in the live key/secret, set `ALPACA_PAPER=false`.
3. Nothing else changes in code — but treat this as placing real orders with
   real money based on autonomous LLM decisions. Consider adding a manual
   approval step or a hard daily loss limit before removing the training
   wheels entirely.