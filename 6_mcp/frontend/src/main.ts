// App entry point: build the trader panel, then poll the backend for portfolio
// data and activity logs. The trading floor runs on its own; this only reads.

import { getMarket, getTrader, getTraderLogs } from "./api";
import { TraderPanel } from "./panel";
import { TraderState } from "./state";
import { initTheme } from "./theme";

const DATA_POLL_MS = 6000;
const LOG_POLL_MS = 2000;

initTheme(document.getElementById("btn-theme") as HTMLButtonElement);

const panelHost = document.getElementById("panels")!;
let state: TraderState;
let panel: TraderPanel;

async function loadMarket(): Promise<void> {
  try {
    const market = await getMarket();
    const badge = document.getElementById("market-badge")!;
    badge.dataset.source = market.source;
    document.getElementById("market-source")!.textContent =
      market.source === "massive" ? "Live market" : "Simulated";
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

async function main(): Promise<void> {
  await loadMarket();
  await buildPanel();
  await pollData();
  await pollLogs();
  setInterval(pollData, DATA_POLL_MS);
  setInterval(pollLogs, LOG_POLL_MS);
}

main().catch((err) => console.error("startup failed", err));
