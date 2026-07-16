## Run it

Three terminals, opened with the plus on the terminal panel.

First, start the API:

`cd 6_mcp`

`uv run uvicorn backend.api:app --port 8000`

FastAPI also serves interactive docs at http://localhost:8000/docs if you want to explore the endpoints yourself.

Next, start the frontend:

`cd 6_mcp/frontend`

`npm run dev`

Open http://localhost:5173. The four traders appear straight away, reading from the API. Try the theme toggle in the corner, and notice the market data badge in the sidebar.

Finally, start the trading floor engine and watch the traders come to life:

`cd 6_mcp`

`uv run -m backend.trading_floor`