# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

"Traders" — a single autonomous LLM trading agent (account name `pkaldani`, a fixed identifier, not a
persona — Alpaca's personal Trading API has no human "name" field to pull one from) that researches the
market via MCP tools and places real orders against an Alpaca **paper trading** account, under a strict
Buffett-style decision mandate that ends every run in one structured JSON verdict. Balance, holdings,
and portfolio value are all live reads of that one real Alpaca account — there's no separate virtual
ledger; SQLite only stores the trader's own evolvable strategy text and its own order history. A FastAPI
backend exposes mostly-read-only JSON (plus manual buy/sell endpoints for the dashboard's own trade page)
for a TypeScript frontend dashboard, and a Gradio dashboard (`demo/`) offers an alternative in-process
view.

All active code lives under `6_mcp/`. The repo's `.gitignore` has entries for `1_foundations` through
`5_agent_frameworks`, remnants of a multi-week course structure — those directories don't exist here.

## Running it

Everything is run from inside `6_mcp/` (or `6_mcp/frontend/` for the UI), across three long-running
processes:

```bash
cd 6_mcp
uv run uvicorn backend.api:app --port 8000     # HTTP API (FastAPI, docs at /docs)

cd 6_mcp/frontend
npm run dev                                     # Vite dev server at :5173, proxies /api -> :8000

cd 6_mcp
uv run -m backend.trading_floor                 # The engine: runs traders on a schedule
```

Other entry points:
- `uv run -m backend.reset` — reset the trader's own local state (strategy text, order history — not
  the real Alpaca account, which can't be reset) and reseed its strategy text (defined in `reset.py`).
- `uv run python demo/util.py` / Gradio UI: launched via `demo/ui.py`'s `create_ui()` — this reads
  `accounts.db` in-process rather than over HTTP.
- Frontend build/typecheck: `npm run build` (runs `tsc` then `vite build`) from `6_mcp/frontend/`.

There is no test suite, linter, or formatter configured in this repo (no pytest/ruff/eslint config
present) — don't invent commands for these.

## Environment variables

Loaded via `.env` at the repo root (`load_dotenv(override=True)` in each module that needs it).
Required for real trading: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER` (default `"true"` —
keep it that way until deliberately going live; see `backend/setup_guide.md`).

Optional, feature-gating: `TRADER_MODEL_NAME` (default `gpt-5.4-mini` — the model the trader runs on),
`DEEPSEEK_API_KEY`, `GOOGLE_API_KEY`, `GROK_API_KEY`, `OPENROUTER_API_KEY` (only relevant if
`TRADER_MODEL_NAME` is pointed at one of those providers — see `get_model()` in `traders.py`),
`TAVILY_API_KEY` (researcher web search), `PUSHOVER_USER`/`PUSHOVER_TOKEN` (push notifications),
`RUN_EVERY_N_MINUTES` (default `1440` — daily; deliberately long so the trader isn't re-deciding on
every tick), `RUN_EVEN_WHEN_MARKET_IS_CLOSED` (default false — the scheduler otherwise skips runs while
markets are closed), `MIN_HOLD_HOURS` (default `24` — minimum time between transactions on the same
symbol, in either direction, enforced in `Account.buy_shares`/`sell_shares`; this is the code-level
day-trading guardrail, on top of the scheduler interval).

## Architecture

### The trading loop (`backend/trading_floor.py` -> `backend/traders.py`)
`trading_floor.py` builds a single `Trader` (name `TRADER_NAME` = `"pkaldani"`, from `accounts.py`) and
loops forever, running it every `RUN_EVERY_N_MINUTES` (default daily), alternating between a "new
trades" pass and a "rebalance only" pass (`Trader.do_trade` flips after every run — see
`templates.trade_message` vs `rebalance_message`).

Each `Trader.run()` (`traders.py`) spins up its own MCP server subprocesses for the duration of one run
(via `AsyncExitStack`, torn down afterward), builds an `agents.Agent` from the OpenAI Agents SDK, and
calls `Runner.run(...)`. The trader's tool surface is composed from two groups of MCP servers, both
defined in `mcp_servers.py`:
- **Trader MCP servers** (direct tools): the accounts server, push-notification server, and market
  price-lookup server.
- **Researcher MCP servers**: wrapped as a single `Researcher` sub-agent tool (not exposed directly),
  giving it Fetch, Tavily search, a knowledge-graph memory server (`mcp-memory-libsql`, persisted at
  `6_mcp/memory/<name>.db`), a filtered subset of the technical analysis tools from `backend/market.py`,
  and `backend/asset_server.py`'s `get_asset_info` tool — real Alpaca asset metadata (exchange, asset
  class, status, tradable/fractionable/shortable) via `alpaca_broker.get_asset_info()`, so the
  researcher can sanity-check a candidate is an active, tradable US equity before advising on it.
  `get_asset_info` results are cached per symbol in-process since this metadata is effectively static;
  `backend/api.py`'s `/api/trader` endpoint reuses the same cached lookup to enrich each holding for
  the frontend, swallowing lookup failures (missing Alpaca creds, rate limits) so the read-only
  dashboard stays up even if that enrichment fails.

Instructions live in `backend/templates.py`'s `trader_instructions()` (no-arg — there's only one
trader): a Buffett-style decision mandate with a moat/quality gate, DCF-based valuation and margin-of-
safety thresholds, a thesis-break check for existing positions, and conviction-based position sizing,
ending in a **strict JSON object** (ticker/decision/confidence/sizing/etc.) as the agent's entire final
reply — no prose outside it. `trade_message`/`rebalance_message` reinforce that JSON-only requirement
per run. The trader's separate strategy prose (a short Buffett-flavored paragraph, distinct from the
instructions above) is seeded via `reset.py` and can be rewritten by the agent itself at runtime through
the `change_strategy` tool — it's meant to evolve.

### Money: one real account, no virtual ledger (`backend/accounts.py`, `backend/alpaca_broker.py`)
Alpaca's Trading API has no concept of sub-accounts, so `Account` (a pydantic model, always keyed by the
single `TRADER_NAME` constant, persisted via `database.py` into `accounts.db`) stores only its own local
state — `strategy` and `transactions` (this app's own order history, for rationale/audit) — while
`balance` and `holdings` are `@property`s that call `alpaca_broker.get_account_info()`/
`get_real_positions()` live, every time they're read. `buy_shares`/`sell_shares` place **real market
orders** through `alpaca_broker.py` against the configured Alpaca account; before any sell, `holdings`
(itself a live Alpaca read) is checked against the requested quantity, so there's nothing to drift out of
sync. `backend/api.py`'s `/api/trader` endpoint reads a 5-minute TTL-cached snapshot
(`alpaca_broker.get_real_account_snapshot()`) instead of hitting Alpaca on every ~6s frontend poll — the
order-path code above never uses that cache, so it always acts on fresh state. `buy_shares`/`sell_shares`
also start with `_check_hold_period`, which raises if the same symbol was last traded (buy or sell)
within `MIN_HOLD_HOURS` — the code-level day-trading guardrail requested alongside the longer scheduler
interval. `buy_shares` additionally starts with `_check_symbol_allowed`, which raises if the symbol
isn't in `backend/symbol_whitelist.yaml` (loaded/cached by `backend/symbol_whitelist.py`) — a curated,
hand-edited list of tickers the trader is allowed to open new positions in, also surfaced in
`templates.trader_instructions()` so the agent doesn't waste Researcher turns on tickers it can't buy.
This restriction is buys-only: `sell_shares` has no such check, so an existing holding outside the list
(e.g. a legacy position from before the whitelist existed) can always be exited. See
`backend/setup_guide.md` for the full writeup of this design and its sharp edges.

Every account/MCP tool (`get_balance`, `buy_shares`, `change_strategy`, the `accounts://...` resources,
`accounts_client.py`) takes **no name argument** — `Account.get()` always resolves to `TRADER_NAME`.
Don't reintroduce a `name` parameter here without also updating every call site.

### Market/technical-analysis data (`backend/market.py`)
`market.py` is both an importable module (used by `market_server.py` for the simple
`lookup_share_price` trader tool — `api.py` now prices holdings from Alpaca's own position data instead,
see the Money section above) and its own MCP server
(`mcp = FastMCP("stock-analysis")`, run via `market_analysis_params` in `mcp_servers.py`) exposing
`get_current_price`, `get_historical_data`, `optimize_indicator_parameters`, `get_technical_analysis`,
and `get_full_report` to the researcher sub-agent. Price lookups are tiered (live last price -> 1m
intraday snapshot -> previous close, via yfinance) with no simulated fallback — it raises rather than
fabricating a price. `market_simulator.py` (deterministic pseudo-random prices from a ticker+timestamp
seed) and `backend/stock_mcp_server.py` (an earlier, near-duplicate of `market.py` without the
price/is_market_open tools) both exist in the tree but nothing currently imports or runs them — treat
`market.py` as the live implementation before assuming either of those is in the loop.

Known inconsistency: `api.py`'s `/api/market` endpoint reads `market.massive_api_key`, an attribute
`market.py` no longer defines (it's pure-yfinance now) — that endpoint will raise `AttributeError` as
the code currently stands.

### HTTP API (`backend/api.py`) and frontend (`frontend/src/`)
Most of `api.py` is read-only (it reads `accounts.db` and live Alpaca/price data, the trading floor
process writes the local database out of band, so the API and engine only share state through SQLite —
run `api.py` from `6_mcp/` so it resolves the same `accounts.db`, a relative path, see `database.py`).
The exception is `POST /api/trader/buy`/`/api/trader/sell`, which place real market orders through the
same `Account.buy_shares`/`sell_shares` (and their guardrails) the LLM trader uses, tagged
`source="manual"` on the resulting `Transaction` to distinguish them from the agent's own trades.
`GET /api/trader/realized-pnl` exposes FIFO-matched realized P&L (closed-trade ledger plus per-symbol/
total rollups) computed from the real account's full fill history via
`alpaca_broker.get_realized_pnl()`; `GET /api/price/{symbol}` is a live quote lookup used by the trade
page's buy-cost preview.

Frontend (`main.ts` as the entry point) is a two-view SPA with no router: a sidebar nav toggles between
the `#panels` dashboard (`hidden` attribute) and `#trade-page` (`trade.ts`) — note that `[hidden]` only
works on an element if no author CSS rule also sets that element's `display` (author styles beat the
UA's `[hidden]{display:none}` regardless of specificity), which is why `.panels[hidden]`/
`.trade-page[hidden]` overrides exist in `styles.css`. The dashboard view polls `/api/trader`,
`/api/trader/logs`, and `/api/market` on independent intervals (`DATA_POLL_MS`, `LOG_POLL_MS`) and
re-renders one panel (`panel.ts`, `chart.ts`, `heatmap.ts`, `log.ts`, `transactions.ts`) — there's no
websocket/push channel and no multi-trader leaderboard/ranking UI (that was removed along with the
other three traders). The trade view (`trade.ts`) fetches on activation and after every order rather
than polling continuously: a buy form with a live price preview, a holdings table with a
sell-per-position action, order history, and the realized P&L ledger — every buy/sell is a two-step
confirm (click once to reveal Confirm/Cancel) since these are real orders, not a simulation.
`LOG_COLORS` in `api.py` and the `mapper` in `demo/ui.py` must be kept in sync since both are
independently mirroring the same log-type -> colour scheme for their respective UIs.

### Tracing/logging (`backend/tracers.py`, `backend/database.py`)
`LogTracer` is registered as an OpenAI Agents SDK trace processor (`add_trace_processor` in
`trading_floor.py`) and writes every trace/span start and end into the `logs` SQLite table, keyed by
trader name recovered from the trace ID (`make_trace_id` encodes the name into the ID; see
`LogTracer.get_name`'s parsing of it). This is what both frontends' "activity log" panels read via
`database.read_log`.
