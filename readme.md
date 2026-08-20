# Traders

An AI agent that researches stocks on its own and trades them on a real brokerage account funded with
fake money.

## What is this?

Think of it as one virtual investor. On a schedule (once a day by default), it:

1. Looks at a fixed list of well-known stocks (Apple, Microsoft, Amazon, and other large, familiar
   companies).
2. Researches them — current price, financials, recent news, technical signals — and remembers what it
   learned last time.
3. Decides whether to buy, sell, or hold each one, following a strict value-investing checklist loosely
   inspired by Warren Buffett.
4. If it decides to trade, it places a real order through Alpaca's **paper trading** account — real
   prices and real order handling, just with fake money, so nothing here risks actual funds.

You can watch all of this happen on a live dashboard.

## How it works

Each run goes through four steps:

1. **Research** — A "Researcher" assistant searches the web for news, checks technical indicators (is
   the stock overbought/oversold, trending up or down, etc.), and checks its own memory for anything
   relevant it found in previous runs.
2. **Decide** — The agent works through a checklist: Does this company have a durable advantage over
   competitors? Is it priced with enough of a safety margin? Has anything changed since it first bought
   in? It ends every decision with one clear verdict — buy, sell, hold, or avoid — never a vague opinion.
3. **Act** — On a buy or sell, it places a real order. A series of built-in safety checks run first (see
   below); if any of them fail, the trade is blocked automatically and the agent has to fall back to
   holding instead.
4. **Report** — It logs what it did and why, so you can see the reasoning behind every trade afterward.

The agent's own investment "strategy" — a short paragraph describing its style — can evolve over time: it
looks back at how its past trades actually performed and is allowed to rewrite it.

## Safety guardrails

Because the agent places real orders — even if it's fake money — several limits are enforced in the
code itself, not just suggested to it in words:

- **Paper trading by default** — trades run against a practice account. Switching to real money takes a
  deliberate, separate step.
- **Approved stock list** — new positions can only be opened in a curated list of well-known, liquid
  stocks, not anything the agent stumbles across.
- **Position and sector limits** — a single stock, or a single industry, can't grow past a set share of
  the portfolio.
- **Cool-down period** — the same stock can't be bought and sold back-to-back within a set number of
  hours, to prevent day-trading style flip-flopping.
- **Daily order limit** — a hard cap on how many orders can go out in 24 hours, as a last-resort circuit
  breaker if something misbehaves.

## What you can see

The web dashboard shows:

- Current portfolio value and profit/loss — both "on paper" (unrealized) and "already cashed in"
  (realized).
- Current holdings, with live prices.
- The approved stock list, highlighting which ones are currently held.
- Recent trades, each with the reasoning behind it.
- A live activity log of what the agent is doing right now.

There's also a manual **Trade** page where a person can place their own buy/sell orders on the same
account — separate from, and clearly labeled apart from, the agent's own trades.

## Run it

Three terminals, opened with the plus on the terminal panel.

First, start the API:

`cd 6_mcp`

`uv run uvicorn backend.api:app --port 8000`

FastAPI also serves interactive docs at http://localhost:8000/docs if you want to explore the endpoints yourself.

Next, start the frontend:

`cd 6_mcp/frontend`

`npm run dev`

Open http://localhost:5173. The dashboard appears straight away, reading from the API. Try the theme
toggle in the corner, and notice the market data badge in the sidebar.

Finally, start the trading floor engine and watch the agent come to life:

`cd 6_mcp`

`uv run -m backend.trading_floor`

It can also run as a set of containers on Kubernetes (`scripts/deploy-kind.sh` for a local `kind`
cluster) — see `CLAUDE.md` for details.
