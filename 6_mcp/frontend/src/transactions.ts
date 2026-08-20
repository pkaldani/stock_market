// A trader's recent trades, newest first. Mirrors the activity log's compact style.

import { isPendingOrder, type Transaction } from "./api";

const MAX_ROWS = 12;

export class TransactionsView {
  private host: HTMLElement;

  constructor(host: HTMLElement) {
    this.host = host;
    host.classList.add("txns");
  }

  render(transactions: Transaction[]): void {
    this.host.innerHTML = "";
    if (transactions.length === 0) {
      const empty = document.createElement("div");
      empty.className = "txn-empty";
      empty.textContent = "No trades yet";
      this.host.append(empty);
      return;
    }
    for (const t of transactions.slice(-MAX_ROWS).reverse()) {
      const row = document.createElement("div");
      row.className = "txn-row";
      // The trader's own stated reasoning for this trade — surfaced as a
      // hover tooltip rather than an extra visible line, matching
      // heatmap.ts's tile.title pattern, since row space here is tight.
      const pending = isPendingOrder(t);
      row.dataset.pending = String(pending);
      row.title = [t.rationale, pending ? `order still ${t.order_status} — quantity/price shown are provisional` : ""]
        .filter(Boolean)
        .join(" — ");

      const date = document.createElement("span");
      date.className = "txn-date";
      date.textContent = dateOf(t.timestamp);

      const side = document.createElement("span");
      side.className = "txn-side";
      side.dataset.side = t.quantity >= 0 ? "buy" : "sell";
      side.textContent = t.quantity >= 0 ? "BUY" : "SELL";

      const detail = document.createElement("span");
      detail.className = "txn-detail";
      detail.textContent = `${Math.abs(t.quantity)} ${t.symbol} @ $${t.price.toFixed(2)}`;
      if (pending) detail.textContent += " (pending)";

      row.append(date, side, detail);
      this.host.append(row);
    }
  }
}

function dateOf(stamp: string): string {
  // "YYYY-MM-DD HH:MM:SS" -> "MM-DD"
  const parts = stamp.split(" ")[0].split("-");
  return parts.length === 3 ? `${parts[1]}-${parts[2]}` : stamp;
}
