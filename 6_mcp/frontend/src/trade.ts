// The manual trade page: buy any symbol, sell current holdings, and see order
// history + realized P&L. Places REAL market orders against the paper account
// via the same Account.buy_shares/sell_shares the LLM trader uses (tagged
// source="manual" server-side) — every submit is a two-step confirm, not a
// single click, since it's a real (if paper) financial transaction.

import { buyShares, getPrice, getRealizedPnl, getTrader, isPendingOrder, sellShares } from "./api";
import type { ClosedTrade, Holding, RealizedPnl, TraderDetail, Transaction } from "./api";

function formatMoney(n: number): string {
  return n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
}

function formatSignedMoney(n: number): string {
  const sign = n >= 0 ? "+" : "-";
  return `${sign}${formatMoney(Math.abs(n))}`;
}

function dateOf(stamp: string): string {
  // Local "YYYY-MM-DD HH:MM:SS" (transactions) or ISO (Alpaca activities).
  return stamp.replace("T", " ").replace("Z", "").slice(0, 16);
}

export class TradePage {
  private root: HTMLElement;
  private detail: TraderDetail | null = null;
  private lastQuote: { symbol: string; price: number } | null = null;

  constructor(root: HTMLElement) {
    this.root = root;
    this.root.innerHTML = `
      <div class="trade-header">
        <h2>Trade</h2>
        <span class="trade-sub">Real market orders against the Alpaca paper account</span>
      </div>
      <div class="trade-grid">
        <section class="trade-card">
          <h3>Buy</h3>
          <form class="buy-form">
            <label>Symbol<input name="symbol" placeholder="AAPL" autocomplete="off" required /></label>
            <label>Quantity<input name="quantity" type="number" min="1" step="1" placeholder="1" required /></label>
            <div class="trade-preview"></div>
            <div class="trade-confirm-row" hidden>
              <button type="button" class="btn btn-cancel">Cancel</button>
              <button type="submit" class="btn btn-confirm-buy">Confirm buy</button>
            </div>
            <button type="button" class="btn btn-buy">Buy</button>
          </form>
          <div class="trade-message"></div>
        </section>

        <section class="trade-card">
          <h3>Holdings</h3>
          <div class="holdings-table"></div>
        </section>

        <section class="trade-card">
          <h3>Order history</h3>
          <div class="order-history"></div>
        </section>

        <section class="trade-card">
          <h3>Realized P&amp;L</h3>
          <div class="pnl-summary"></div>
          <div class="pnl-ledger"></div>
        </section>
      </div>
    `;

    const form = this.root.querySelector(".buy-form") as HTMLFormElement;
    const symbolInput = form.querySelector('[name="symbol"]') as HTMLInputElement;
    const quantityInput = form.querySelector('[name="quantity"]') as HTMLInputElement;
    const buyBtn = form.querySelector(".btn-buy") as HTMLButtonElement;
    const cancelBtn = form.querySelector(".btn-cancel") as HTMLButtonElement;
    const confirmRow = form.querySelector(".trade-confirm-row") as HTMLElement;

    const refreshPreview = () => this.updateBuyPreview(symbolInput.value, quantityInput.value);
    symbolInput.addEventListener("blur", refreshPreview);
    quantityInput.addEventListener("input", refreshPreview);

    buyBtn.addEventListener("click", () => {
      if (!symbolInput.reportValidity() || !quantityInput.reportValidity()) return;
      buyBtn.hidden = true;
      confirmRow.hidden = false;
    });
    cancelBtn.addEventListener("click", () => {
      confirmRow.hidden = true;
      buyBtn.hidden = false;
    });
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      void this.submitBuy(symbolInput.value.trim().toUpperCase(), Number(quantityInput.value));
      confirmRow.hidden = true;
      buyBtn.hidden = false;
    });
  }

  async activate(): Promise<void> {
    await Promise.all([this.refreshTrader(), this.refreshRealizedPnl()]);
  }

  private async refreshTrader(): Promise<void> {
    try {
      this.detail = await getTrader();
      this.renderHoldings(this.detail.holdings);
      this.renderHistory(this.detail.transactions);
    } catch (err) {
      console.error("trade page: failed to load trader state", err);
    }
  }

  private async refreshRealizedPnl(): Promise<void> {
    try {
      this.renderPnl(await getRealizedPnl());
    } catch (err) {
      console.error("trade page: failed to load realized P&L", err);
      this.root.querySelector(".pnl-summary")!.textContent = "Realized P&L unavailable";
    }
  }

  private async updateBuyPreview(symbol: string, quantityStr: string): Promise<void> {
    const preview = this.root.querySelector(".trade-preview") as HTMLElement;
    symbol = symbol.trim().toUpperCase();
    const quantity = Number(quantityStr);
    if (!symbol) {
      preview.textContent = "";
      return;
    }
    if (this.lastQuote?.symbol !== symbol) {
      try {
        this.lastQuote = await getPrice(symbol);
      } catch {
        this.lastQuote = null;
        preview.textContent = `No live quote for ${symbol}`;
        return;
      }
    }
    if (this.lastQuote && quantity > 0) {
      preview.textContent = `~${formatMoney(this.lastQuote.price)}/share · est. ${formatMoney(this.lastQuote.price * quantity)}`;
    } else if (this.lastQuote) {
      preview.textContent = `~${formatMoney(this.lastQuote.price)}/share`;
    }
  }

  private async submitBuy(symbol: string, quantity: number): Promise<void> {
    if (!symbol || !Number.isFinite(quantity) || quantity <= 0) return;
    this.showMessage(`Placing order: buy ${quantity} ${symbol}…`, "pending");
    try {
      this.detail = await buyShares(symbol, quantity);
      this.showMessage(`Bought ${quantity} ${symbol}.`, "success");
      this.renderHoldings(this.detail.holdings);
      this.renderHistory(this.detail.transactions);
      void this.refreshRealizedPnl();
    } catch (err) {
      this.showMessage(err instanceof Error ? err.message : String(err), "error");
    }
  }

  private async submitSell(symbol: string, quantity: number): Promise<void> {
    this.showMessage(`Placing order: sell ${quantity} ${symbol}…`, "pending");
    try {
      this.detail = await sellShares(symbol, quantity);
      this.showMessage(`Sold ${quantity} ${symbol}.`, "success");
      this.renderHoldings(this.detail.holdings);
      this.renderHistory(this.detail.transactions);
      void this.refreshRealizedPnl();
    } catch (err) {
      this.showMessage(err instanceof Error ? err.message : String(err), "error");
    }
  }

  private showMessage(text: string, kind: "success" | "error" | "pending"): void {
    const el = this.root.querySelector(".trade-message") as HTMLElement;
    el.textContent = text;
    el.dataset.kind = kind;
  }

  private renderHoldings(holdings: Holding[]): void {
    const host = this.root.querySelector(".holdings-table") as HTMLElement;
    host.innerHTML = "";
    if (holdings.length === 0) {
      host.innerHTML = `<div class="trade-empty">No holdings</div>`;
      return;
    }
    for (const h of holdings) {
      const row = document.createElement("div");
      row.className = "holding-row";
      row.innerHTML = `
        <span class="holding-symbol">${h.symbol}</span>
        <span class="holding-qty">${h.quantity}</span>
        <span class="holding-value">${formatMoney(h.market_value)}</span>
        <span class="holding-pnl" data-trend="${h.unrealized_pnl >= 0 ? "up" : "down"}">${formatSignedMoney(h.unrealized_pnl)}</span>
      `;
      const sellWrap = document.createElement("span");
      sellWrap.className = "holding-sell";
      const qtyInput = document.createElement("input");
      qtyInput.type = "number";
      qtyInput.min = "1";
      qtyInput.max = String(h.quantity);
      qtyInput.step = "1";
      qtyInput.value = String(h.quantity);
      qtyInput.className = "holding-sell-qty";
      const sellBtn = document.createElement("button");
      sellBtn.type = "button";
      sellBtn.className = "btn btn-sell";
      sellBtn.textContent = "Sell";
      sellWrap.append(qtyInput, sellBtn);
      row.append(sellWrap);

      sellBtn.addEventListener("click", () => {
        if (sellBtn.textContent === "Sell") {
          sellBtn.textContent = "Confirm?";
          sellBtn.classList.add("btn-confirm-sell");
          const reset = () => {
            sellBtn.textContent = "Sell";
            sellBtn.classList.remove("btn-confirm-sell");
          };
          const onBlur = () => {
            reset();
            sellBtn.removeEventListener("blur", onBlur);
          };
          sellBtn.addEventListener("blur", onBlur, { once: true });
          return;
        }
        const qty = Number(qtyInput.value);
        if (!Number.isFinite(qty) || qty <= 0) return;
        void this.submitSell(h.symbol, qty);
      });

      host.append(row);
    }
  }

  private renderHistory(transactions: Transaction[]): void {
    const host = this.root.querySelector(".order-history") as HTMLElement;
    host.innerHTML = "";
    if (transactions.length === 0) {
      host.innerHTML = `<div class="trade-empty">No orders yet</div>`;
      return;
    }
    for (const t of transactions.slice(-25).reverse()) {
      const row = document.createElement("div");
      row.className = "history-row";
      const pending = isPendingOrder(t);
      row.dataset.pending = String(pending);
      row.title = [t.rationale, pending ? `order still ${t.order_status} — quantity/price shown are provisional` : ""]
        .filter(Boolean)
        .join(" — ");
      const side = t.quantity >= 0 ? "buy" : "sell";
      row.innerHTML = `
        <span class="txn-date">${dateOf(t.timestamp)}</span>
        <span class="txn-side" data-side="${side}">${side.toUpperCase()}</span>
        <span class="txn-detail">${Math.abs(t.quantity)} ${t.symbol} @ ${formatMoney(t.price)}${pending ? " (pending)" : ""}</span>
        <span class="txn-source" data-source="${t.source}">${t.source}</span>
      `;
      host.append(row);
    }
  }

  private renderPnl(pnl: RealizedPnl): void {
    const summary = this.root.querySelector(".pnl-summary") as HTMLElement;
    summary.innerHTML = `
      <span class="pnl-total" data-trend="${pnl.total >= 0 ? "up" : "down"}">${formatSignedMoney(pnl.total)}</span>
      <span class="pnl-total-label">total realized</span>
    `;

    const ledger = this.root.querySelector(".pnl-ledger") as HTMLElement;
    ledger.innerHTML = "";
    if (pnl.closed_trades.length === 0) {
      ledger.innerHTML = `<div class="trade-empty">No closed trades yet</div>`;
      return;
    }
    const trades: ClosedTrade[] = [...pnl.closed_trades].reverse().slice(0, 25);
    for (const t of trades) {
      const row = document.createElement("div");
      row.className = "history-row";
      row.innerHTML = `
        <span class="txn-date">${dateOf(t.sell_time)}</span>
        <span class="txn-detail">${t.quantity} ${t.symbol} · ${formatMoney(t.buy_price)} → ${formatMoney(t.sell_price)}</span>
        <span class="pnl-row-value" data-trend="${t.realized_pnl >= 0 ? "up" : "down"}">${formatSignedMoney(t.realized_pnl)}</span>
      `;
      ledger.append(row);
    }
  }
}
