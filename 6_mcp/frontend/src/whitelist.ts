// The approved symbol universe the trader may open new BUY positions in —
// fetched once in main.ts (it's static for the life of the process; the
// backend's /api/whitelist reads symbol_whitelist.yaml, which only changes
// on a redeploy). Highlights which symbols are currently held, so a
// HOLD/no-signal on a whitelisted-but-never-traded symbol reads differently
// from one that's already a position.

import type { Holding, WhitelistEntry } from "./api";

export class WhitelistView {
  private host: HTMLElement;

  constructor(host: HTMLElement) {
    this.host = host;
    host.classList.add("whitelist");
  }

  render(entries: WhitelistEntry[], holdings: Holding[]): void {
    this.host.innerHTML = "";
    if (entries.length === 0) return;
    const held = new Set(holdings.map((h) => h.symbol));
    for (const e of entries) {
      const chip = document.createElement("span");
      chip.className = "whitelist-chip";
      chip.dataset.held = held.has(e.symbol) ? "true" : "false";
      chip.textContent = e.symbol;
      chip.title = `${e.symbol}${e.sector ? " · " + e.sector : ""}${held.has(e.symbol) ? " · held" : ""}`;
      this.host.append(chip);
    }
  }
}
