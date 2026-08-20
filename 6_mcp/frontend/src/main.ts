// App entry point: build the trader panel, then poll the backend for portfolio
// data and activity logs. The trading floor runs on its own; this only reads.

import { getMarket, getTrader, getTraderLogs, getWhitelist } from "./api";
import { TraderPanel } from "./panel";
import { TraderState } from "./state";
import { initTheme } from "./theme";
import { TradePage } from "./trade";

const DATA_POLL_MS = 6000;
const LOG_POLL_MS = 2000;

initTheme(document.getElementById("btn-theme") as HTMLButtonElement);

const panelHost = document.getElementById("panels")!;
let state: TraderState;
let panel: TraderPanel;

const tradePageHost = document.getElementById("trade-page") as HTMLElement;
const navDashboard = document.getElementById("nav-dashboard") as HTMLButtonElement;
const navTrade = document.getElementById("nav-trade") as HTMLButtonElement;
let tradePage: TradePage | null = null;

function showDashboard(): void {
  panelHost.hidden = false;
  tradePageHost.hidden = true;
  navDashboard.dataset.active = "true";
  navTrade.dataset.active = "false";
}

function showTrade(): void {
  panelHost.hidden = true;
  tradePageHost.hidden = false;
  navDashboard.dataset.active = "false";
  navTrade.dataset.active = "true";
  if (!tradePage) tradePage = new TradePage(tradePageHost);
  void tradePage.activate();
}

navDashboard.addEventListener("click", showDashboard);
navTrade.addEventListener("click", showTrade);

async function loadMarket(): Promise<void> {
  try {
    const market = await getMarket();
    const badge = document.getElementById("market-badge")!;
    badge.dataset.source = market.source;
    // backend/api.py's /api/market only ever reports "yfinance" now (market.py
    // is pure yfinance, no simulated fallback) — this used to check for a
    // "massive" source from an earlier data-provider design that no longer
    // exists, so it always fell through to "Simulated" and mislabeled real
    // live data.
    document.getElementById("market-source")!.textContent =
      market.source === "yfinance" ? "Live market" : "Simulated";
    document.getElementById("market-status")!.textContent = market.is_market_open
      ? "Market open"
      : "Market closed";
  } catch (err) {
    console.error("market fetch failed", err);
  }
}

async function buildPanel(): Promise<void> {
  const detail = await getTrader();
  state = new TraderState(detail);
  panel = new TraderPanel(state);
  panelHost.append(panel.root);
  panel.mount();
}

async function pollData(): Promise<void> {
  try {
    state.recordDetail(await getTrader());
    panel.update();
  } catch (err) {
    console.error("data fetch failed", err);
  }
}

async function pollLogs(): Promise<void> {
  try {
    panel.renderLogs(await getTraderLogs());
  } catch (err) {
    console.error("log fetch failed", err);
  }
}

async function loadWhitelist(): Promise<void> {
  try {
    panel.setWhitelist(await getWhitelist());
  } catch (err) {
    console.error("whitelist fetch failed", err);
  }
}

async function main(): Promise<void> {
  await loadMarket();
  await buildPanel();
  await loadWhitelist();
  await pollData();
  await pollLogs();
  setInterval(pollData, DATA_POLL_MS);
  setInterval(pollLogs, LOG_POLL_MS);
}

main().catch((err) => console.error("startup failed", err));
