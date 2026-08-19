"""
Stock Market MCP Server
========================
Exposes MCP tools for:
  - get_share_price              : tiered live/near-live price lookup (yfinance)
  - is_market_open               : whether the US market is currently open
  - get_current_price            : latest quote for a ticker
  - get_historical_data          : OHLCV history for a period/interval
  - optimize_indicator_parameters: per-ticker backtested indicator settings
  - get_technical_analysis       : full indicator suite (SMA/EMA, RSI, MACD,
                                    Bollinger Bands, Stochastic, ADX, ATR, OBV,
                                    VWAP), with parameters auto-optimized per
                                    ticker by default (optimize=True)
  - get_full_report              : price + indicators + plain-English summary

Run (stdio transport, default for local MCP clients / Claude Desktop config):
    python stock_mcp_server.py

Requirements (see requirements.txt):
    pip install mcp yfinance pandas numpy
"""

from __future__ import annotations

from itertools import product

import numpy as np
import pandas as pd
import yfinance as yf
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("stock-analysis")


# --------------------------------------------------------------------------
# Data fetch helpers
# --------------------------------------------------------------------------

_history_cache: dict[tuple[str, str, str], pd.DataFrame] = {}


def _fetch_history(ticker: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
    """Download OHLCV data via yfinance and normalize the dataframe.

    Cached in-process per (ticker, period, interval) for the lifetime of this
    MCP server subprocess (one per Researcher run — see mcp_servers.py), since
    nothing downstream mutates the returned DataFrame. get_full_report calls
    get_technical_analysis which calls this, and a Researcher pass often also
    calls optimize_indicator_parameters for the same ticker/window in the same
    cycle — without this, each of those re-hits yfinance for identical data.
    Mirrors the existing per-symbol cache in asset_server.py's get_asset_info.
    """
    cache_key = (ticker, period, interval)
    cached = _history_cache.get(cache_key)
    if cached is not None:
        return cached

    tk = yf.Ticker(ticker)
    df = tk.history(period=period, interval=interval, auto_adjust=False)
    if df.empty:
        raise ValueError(f"No data returned for ticker '{ticker}' "
                          f"(period={period}, interval={interval}). Check the symbol.")
    df = df.rename(columns=str.title)  # Open, High, Low, Close, Volume
    df.index.name = "Date"
    _history_cache[cache_key] = df
    return df


def _df_to_records(df: pd.DataFrame, decimals: int = 4) -> list[dict]:
    out = df.copy()
    out = out.round(decimals)
    out.index = out.index.strftime("%Y-%m-%d %H:%M:%S")
    out = out.reset_index()
    return out.to_dict(orient="records")


# --------------------------------------------------------------------------
# Tiered live price lookup (yfinance only — no simulator, no third-party
# market-data provider). Mirrors the "try progressively less-live sources,
# remember whichever tier last worked" pattern: yfinance doesn't expose a
# tick-level last-trade endpoint the way a paid feed does, so the tiers here
# go from "live intraday snapshot" down to "last completed session's close".
# --------------------------------------------------------------------------

def _last_trade(ticker_obj: yf.Ticker) -> float:
    """Most current price available: fast_info's live last_price field."""
    price = ticker_obj.fast_info.get("last_price") if isinstance(ticker_obj.fast_info, dict) \
        else getattr(ticker_obj.fast_info, "last_price", None)
    if price is None:
        raise ValueError("No last_price in fast_info")
    return float(price)


def _snapshot(ticker_obj: yf.Ticker) -> float:
    """Near-live snapshot: close of the most recent 1-minute intraday bar."""
    hist = ticker_obj.history(period="1d", interval="1m")
    if hist.empty:
        raise ValueError("No intraday snapshot data available")
    return float(hist["Close"].iloc[-1])


def _previous_close(ticker_obj: yf.Ticker) -> float:
    """Last completed session's close price."""
    price = ticker_obj.fast_info.get("previous_close") if isinstance(ticker_obj.fast_info, dict) \
        else getattr(ticker_obj.fast_info, "previous_close", None)
    if price is None:
        raise ValueError("No previous_close in fast_info")
    return float(price)


# Best (most live) price first, prior close last. Some tickers/exchanges
# don't populate the live fields, so we remember the first tier that worked
# for the current process and start there next time instead of retrying
# tiers that just failed.
price_methods = [_last_trade, _snapshot, _previous_close]
plan_tier = 0


@mcp.tool()
def get_share_price(symbol: str) -> float:
    """
    Return the current price for a symbol using yfinance, trying
    progressively less-live sources (live last price -> intraday snapshot ->
    previous close) until one succeeds. Real market data only — no simulated
    or synthetic prices are ever returned; if yfinance has nothing for the
    symbol this raises an error instead of making a number up.

    Args:
        symbol: Stock symbol, e.g. "AAPL".
    """
    global plan_tier
    ticker_obj = yf.Ticker(symbol)
    last_err: Exception | None = None
    for tier in range(plan_tier, len(price_methods)):
        try:
            price = price_methods[tier](ticker_obj)
            plan_tier = tier
            return price
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"No yfinance price available for '{symbol}': {last_err}")


@mcp.tool()
def is_market_open() -> bool:
    """
    Whether the US stock market is currently open, per yfinance's market
    status endpoint. Reflects real exchange state — there is no simulated
    fallback; if the status can't be retrieved this raises rather than
    guessing.
    """
    status = yf.Market("US").status
    state = status.get("status") if isinstance(status, dict) else None
    if state is None:
        raise RuntimeError("Could not determine market status from yfinance")
    return str(state).lower() == "open"


# --------------------------------------------------------------------------
# Indicator calculations (pure pandas/numpy, no extra TA dependency needed)
# --------------------------------------------------------------------------

def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(series: pd.Series, window: int = 20, num_std: float = 2.0):
    mid = sma(series, window)
    std = series.rolling(window=window, min_periods=window).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def stochastic_oscillator(df: pd.DataFrame, k_window: int = 14, d_window: int = 3):
    low_min = df["Low"].rolling(window=k_window, min_periods=k_window).min()
    high_max = df["High"].rolling(window=k_window, min_periods=k_window).max()
    percent_k = 100 * (df["Close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    percent_d = percent_k.rolling(window=d_window, min_periods=d_window).mean()
    return percent_k, percent_d


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift(1)
    ranges = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    tr = true_range(df)
    return tr.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()


def adx(df: pd.DataFrame, window: int = 14):
    up_move = df["High"].diff()
    down_move = -df["Low"].diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = true_range(df)
    atr_val = tr.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()

    plus_dm_s = pd.Series(plus_dm, index=df.index).ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    minus_dm_s = pd.Series(minus_dm, index=df.index).ewm(alpha=1 / window, adjust=False, min_periods=window).mean()

    plus_di = 100 * (plus_dm_s / atr_val.replace(0, np.nan))
    minus_di = 100 * (minus_dm_s / atr_val.replace(0, np.nan))

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val = dx.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    return adx_val, plus_di, minus_di


def obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["Close"].diff()).fillna(0)
    return (direction * df["Volume"]).cumsum()


def vwap(df: pd.DataFrame) -> pd.Series:
    """Volume-weighted average price, reset at the start of each session
    (calendar day) rather than accumulated since the start of the fetched
    window. A VWAP that never resets isn't the standard intraday "fair value"
    anchor traders mean by the term — it's a long-run average price that
    silently drifts further from the current price the longer `period` is,
    which is exactly what happens for this project's default daily-bar,
    multi-month/year windows."""
    typical_price = (df["High"] + df["Low"] + df["Close"]) / 3
    vol_price = typical_price * df["Volume"]
    session = df.index.date
    cum_vol = df["Volume"].groupby(session).cumsum()
    cum_vol_price = vol_price.groupby(session).cumsum()
    return cum_vol_price / cum_vol.replace(0, np.nan)


STATIC_DEFAULT_PARAMS = {
    "sma_fast": 20, "sma_slow": 50, "sma_trend": 200,
    "ema_fast": 12, "ema_slow": 26,
    "rsi_window": 14, "rsi_low": 30, "rsi_high": 70,
    "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
    "bb_window": 20, "bb_std": 2.0,
    "stoch_k": 14, "stoch_d": 3,
    "atr_window": 14, "adx_window": 14,
}


def build_indicators_dynamic(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    """Compute the full indicator suite using a supplied parameter set (either
    the static defaults or the output of optimize_all_parameters)."""
    out = df.copy()

    out[f"SMA_{params['sma_fast']}"] = sma(out["Close"], params["sma_fast"])
    out[f"SMA_{params['sma_slow']}"] = sma(out["Close"], params["sma_slow"])
    out[f"SMA_{params['sma_trend']}"] = sma(out["Close"], params["sma_trend"])
    out[f"EMA_{params['ema_fast']}"] = ema(out["Close"], params["ema_fast"])
    out[f"EMA_{params['ema_slow']}"] = ema(out["Close"], params["ema_slow"])

    out[f"RSI_{params['rsi_window']}"] = rsi(out["Close"], params["rsi_window"])

    macd_line, signal_line, hist = macd(
        out["Close"], params["macd_fast"], params["macd_slow"], params["macd_signal"]
    )
    out["MACD"] = macd_line
    out["MACD_Signal"] = signal_line
    out["MACD_Hist"] = hist

    bb_upper, bb_mid, bb_lower = bollinger_bands(out["Close"], params["bb_window"], params["bb_std"])
    out["BB_Upper"] = bb_upper
    out["BB_Middle"] = bb_mid
    out["BB_Lower"] = bb_lower

    k, d = stochastic_oscillator(out, params["stoch_k"], params["stoch_d"])
    out["Stoch_%K"] = k
    out["Stoch_%D"] = d

    out[f"ATR_{params['atr_window']}"] = atr(out, params["atr_window"])
    adx_val, plus_di, minus_di = adx(out, params["adx_window"])
    out[f"ADX_{params['adx_window']}"] = adx_val
    out["Plus_DI"] = plus_di
    out["Minus_DI"] = minus_di

    out["OBV"] = obv(out)
    out["VWAP"] = vwap(out)

    return out


def summarize_signals_dynamic(latest: pd.Series, params: dict) -> dict:
    """Produce human-readable interpretations from the latest indicator row,
    using whichever parameter set (static or optimized) generated it."""
    signals = {}

    sma_fast_col = f"SMA_{params['sma_fast']}"
    sma_slow_col = f"SMA_{params['sma_slow']}"
    sma_trend_col = f"SMA_{params['sma_trend']}"
    rsi_col = f"RSI_{params['rsi_window']}"
    adx_col = f"ADX_{params['adx_window']}"

    if pd.notna(latest.get(sma_fast_col)) and pd.notna(latest.get(sma_slow_col)):
        signals["ma_crossover"] = (
            f"Bullish (SMA{params['sma_fast']} > SMA{params['sma_slow']})"
            if latest[sma_fast_col] > latest[sma_slow_col]
            else f"Bearish (SMA{params['sma_fast']} < SMA{params['sma_slow']})"
        )

    if pd.notna(latest.get(sma_trend_col)):
        signals["long_term_trend"] = (
            f"Price above SMA{params['sma_trend']} — bullish bias"
            if latest["Close"] > latest[sma_trend_col]
            else f"Price below SMA{params['sma_trend']} — bearish bias"
        )

    if pd.notna(latest.get(rsi_col)):
        rsi_v = latest[rsi_col]
        low_th, high_th = params["rsi_low"], params["rsi_high"]
        if rsi_v >= high_th:
            signals["rsi"] = f"Overbought ({rsi_v:.1f}, threshold {high_th})"
        elif rsi_v <= low_th:
            signals["rsi"] = f"Oversold ({rsi_v:.1f}, threshold {low_th})"
        else:
            signals["rsi"] = f"Neutral ({rsi_v:.1f})"

    if pd.notna(latest.get("MACD")) and pd.notna(latest.get("MACD_Signal")):
        signals["macd"] = (
            "Bullish (MACD above signal line)"
            if latest["MACD"] > latest["MACD_Signal"]
            else "Bearish (MACD below signal line)"
        )

    if pd.notna(latest.get("BB_Upper")) and pd.notna(latest.get("BB_Lower")):
        close = latest["Close"]
        if close >= latest["BB_Upper"]:
            signals["bollinger"] = "Price at/above upper band (potentially overbought)"
        elif close <= latest["BB_Lower"]:
            signals["bollinger"] = "Price at/below lower band (potentially oversold)"
        else:
            signals["bollinger"] = "Price within bands"

    if pd.notna(latest.get("Stoch_%K")):
        k_v = latest["Stoch_%K"]
        if k_v >= 80:
            signals["stochastic"] = f"Overbought ({k_v:.1f})"
        elif k_v <= 20:
            signals["stochastic"] = f"Oversold ({k_v:.1f})"
        else:
            signals["stochastic"] = f"Neutral ({k_v:.1f})"

    if pd.notna(latest.get(adx_col)):
        adx_v = latest[adx_col]
        if adx_v >= 25:
            strength = "Strong trend"
        elif adx_v >= 20:
            strength = "Developing trend"
        else:
            strength = "Weak/No trend"
        signals["adx"] = f"{strength} (ADX{params['adx_window']} = {adx_v:.1f})"

    return signals


# --------------------------------------------------------------------------
# Per-ticker parameter optimization
#
# For each indicator family, we grid-search a set of "reasonable" candidate
# windows/thresholds, turn each candidate into a simple long/flat trading
# signal, backtest it against the ticker's own historical daily returns, and
# keep whichever candidate produced the best Sharpe-like risk-adjusted return.
# This is in-sample optimization — a diagnostic tool for "what rhythm has
# this stock historically respected", not a guarantee of future edge.
# --------------------------------------------------------------------------

DEFAULT_GRID = {
    "sma_fast": [5, 10, 15, 20, 25, 30],
    "sma_slow": [40, 50, 75, 100, 150, 200],
    "sma_trend": [50, 100, 150, 200, 250],
    "ema_fast": [5, 8, 10, 12, 15, 20],
    "ema_slow": [20, 26, 30, 40, 50],
    "rsi_window": [7, 9, 14, 21, 28],
    "rsi_thresholds": [(20, 80), (25, 75), (30, 70)],
    "macd_fast": [8, 12, 16],
    "macd_slow": [21, 26, 34],
    "macd_signal": [5, 9, 12],
    "bb_window": [10, 15, 20, 30],
    "bb_std": [1.5, 2.0, 2.5],
    "stoch_k": [5, 9, 14, 21],
    "stoch_d": [3, 5],
}


# Bars per year implied by each yfinance interval, used to annualize a Sharpe-
# like score correctly regardless of bar frequency. 6.5 trading hours/day and
# 252 trading days/year are the standard US-equity conventions.
BARS_PER_YEAR = {
    "1m": 98280.0, "2m": 49140.0, "5m": 19656.0, "15m": 6552.0, "30m": 3276.0,
    "60m": 1638.0, "90m": 1092.0, "1h": 1638.0,
    "1d": 252.0, "5d": 50.4, "1wk": 52.0, "1mo": 12.0, "3mo": 4.0,
}

DEFAULT_COST_BPS = 5.0  # flat round-trip cost assumed per position flip, in basis points


def _annualization_factor(interval: str) -> float:
    """Bars-per-year for `interval`, defaulting to daily (252) for any
    interval not in BARS_PER_YEAR."""
    return BARS_PER_YEAR.get(interval, 252.0)


def _apply_cost(position: pd.Series, daily_ret: pd.Series, cost_bps: float = DEFAULT_COST_BPS) -> pd.Series:
    """Strategy return net of a flat per-flip transaction cost (spread/
    slippage proxy), so the grid search doesn't reward high-turnover
    parameter sets (e.g. tight RSI/Bollinger thresholds) that only look good
    in a frictionless backtest."""
    turnover = position.diff().abs().fillna(0)
    cost = turnover * (cost_bps / 10000)
    return position * daily_ret - cost


def _sharpe(returns: pd.Series, interval: str = "1d") -> float:
    """Annualized Sharpe-like score (mean/std of per-bar strategy returns),
    annualized using the bar frequency implied by `interval` rather than
    always assuming daily bars — see BARS_PER_YEAR."""
    clean = returns.dropna()
    std = clean.std(ddof=0)
    if len(clean) < 20 or std == 0 or pd.isna(std):
        return -np.inf
    return float((clean.mean() / std) * np.sqrt(_annualization_factor(interval)))


def optimize_ma_crossover(close: pd.Series, ma_func, fast_candidates, slow_candidates,
                           interval: str = "1d") -> dict:
    """Best (fast, slow) pair for a moving-average crossover long/flat strategy."""
    daily_ret = close.pct_change()
    best = {"fast": fast_candidates[0], "slow": slow_candidates[-1], "sharpe": -np.inf}
    for fast, slow in product(fast_candidates, slow_candidates):
        if fast >= slow:
            continue
        fast_ma = ma_func(close, fast)
        slow_ma = ma_func(close, slow)
        position = (fast_ma > slow_ma).astype(int).shift(1).fillna(0)
        score = _sharpe(_apply_cost(position, daily_ret), interval)
        if score > best["sharpe"]:
            best = {"fast": fast, "slow": slow, "sharpe": score}
    return best


def optimize_single_ma_trend(close: pd.Series, window_candidates, interval: str = "1d") -> dict:
    """Best single MA window for a price-vs-MA trend-following strategy."""
    daily_ret = close.pct_change()
    best = {"window": window_candidates[0], "sharpe": -np.inf}
    for window in window_candidates:
        ma_line = sma(close, window)
        position = (close > ma_line).astype(int).shift(1).fillna(0)
        score = _sharpe(_apply_cost(position, daily_ret), interval)
        if score > best["sharpe"]:
            best = {"window": window, "sharpe": score}
    return best


def _mean_reversion_position(daily_ret: pd.Series, indicator: pd.Series, low_th: float, high_th: float) -> pd.Series:
    """Enter long when indicator drops below low_th, exit when it rises above
    high_th, hold in between (typical oscillator mean-reversion logic)."""
    raw = pd.Series(np.nan, index=indicator.index)
    raw[indicator < low_th] = 1
    raw[indicator > high_th] = 0
    return raw.ffill().fillna(0).shift(1).fillna(0)


def optimize_rsi(close: pd.Series, window_candidates, threshold_pairs, interval: str = "1d") -> dict:
    daily_ret = close.pct_change()
    best = {"window": window_candidates[0], "low_th": 30, "high_th": 70, "sharpe": -np.inf}
    for window in window_candidates:
        r = rsi(close, window)
        for low_th, high_th in threshold_pairs:
            position = _mean_reversion_position(daily_ret, r, low_th, high_th)
            score = _sharpe(_apply_cost(position, daily_ret), interval)
            if score > best["sharpe"]:
                best = {"window": window, "low_th": low_th, "high_th": high_th, "sharpe": score}
    return best


def optimize_macd(close: pd.Series, fast_candidates, slow_candidates, signal_candidates,
                   interval: str = "1d") -> dict:
    daily_ret = close.pct_change()
    best = {"fast": 12, "slow": 26, "signal": 9, "sharpe": -np.inf}
    for fast, slow, sig in product(fast_candidates, slow_candidates, signal_candidates):
        if fast >= slow:
            continue
        macd_line, signal_line, _ = macd(close, fast, slow, sig)
        position = (macd_line > signal_line).astype(int).shift(1).fillna(0)
        score = _sharpe(_apply_cost(position, daily_ret), interval)
        if score > best["sharpe"]:
            best = {"fast": fast, "slow": slow, "signal": sig, "sharpe": score}
    return best


def optimize_bollinger(df: pd.DataFrame, window_candidates, std_candidates, interval: str = "1d") -> dict:
    close = df["Close"]
    daily_ret = close.pct_change()
    best = {"window": 20, "num_std": 2.0, "sharpe": -np.inf}
    for window, num_std in product(window_candidates, std_candidates):
        upper, _, lower = bollinger_bands(close, window, num_std)
        raw = pd.Series(np.nan, index=close.index)
        raw[close < lower] = 1
        raw[close > upper] = 0
        position = raw.ffill().fillna(0).shift(1).fillna(0)
        score = _sharpe(_apply_cost(position, daily_ret), interval)
        if score > best["sharpe"]:
            best = {"window": window, "num_std": num_std, "sharpe": score}
    return best


def optimize_stochastic(df: pd.DataFrame, k_candidates, d_candidates, low_th: float = 20, high_th: float = 80,
                         interval: str = "1d") -> dict:
    close = df["Close"]
    daily_ret = close.pct_change()
    best = {"k_window": 14, "d_window": 3, "sharpe": -np.inf}
    for k_w, d_w in product(k_candidates, d_candidates):
        k, _ = stochastic_oscillator(df, k_w, d_w)
        position = _mean_reversion_position(daily_ret, k, low_th, high_th)
        score = _sharpe(_apply_cost(position, daily_ret), interval)
        if score > best["sharpe"]:
            best = {"k_window": k_w, "d_window": d_w, "sharpe": score}
    return best


def estimate_dominant_cycle(close: pd.Series, min_lag: int = 7, max_lag: int = 30) -> int:
    """Pick the lag (in bars) with the strongest return autocorrelation, used
    as the window for volatility indicators (ATR/ADX) that don't map cleanly
    onto a long/flat backtest."""
    returns = close.pct_change().dropna()
    best_lag, best_corr = 14, -np.inf
    for lag in range(min_lag, max_lag + 1):
        if len(returns) <= lag + 5:
            continue
        corr = returns.autocorr(lag=lag)
        if pd.notna(corr) and corr > best_corr:
            best_corr = corr
            best_lag = lag
    return best_lag


def _walk_forward_splits(n: int, n_splits: int = 4, min_train: int = 60, min_test: int = 15):
    """Yield (train_end, test_start, test_end) index tuples for anchored
    walk-forward folds: each fold trains on everything up to that point and
    tests on the next unseen block."""
    usable = n - min_train
    if usable < min_test:
        return []
    fold_size = max(usable // n_splits, min_test)
    splits = []
    train_end = min_train
    while train_end + min_test <= n and len(splits) < n_splits:
        test_end = min(train_end + fold_size, n)
        if test_end - train_end >= min_test:
            splits.append((train_end, train_end, test_end))
        train_end = test_end
    return splits


def walk_forward_validate(df: pd.DataFrame, n_splits: int = 4, grid: dict | None = None,
                           interval: str = "1d") -> dict | None:
    """
    Anchored walk-forward validation — the honest counterpart to
    optimize_all_parameters(). For each fold: optimize every indicator family
    using ONLY data up to that fold's start (never touching the test block),
    then score those exact chosen parameters on the following unseen block.
    Averaging the out-of-sample score across folds gives a realistic estimate
    of whether "optimizing per ticker" actually generalizes for this stock,
    versus just curve-fitting to its price history.

    Returns None if there isn't enough history for at least one valid fold.
    """
    grid = grid or DEFAULT_GRID
    close = df["Close"]
    daily_ret = close.pct_change()
    n = len(df)

    splits = _walk_forward_splits(n, n_splits=n_splits)
    if not splits:
        return None

    fold_scores = {
        "sma_crossover": [], "sma_trend": [], "ema_crossover": [],
        "rsi": [], "macd": [], "bollinger": [], "stochastic": [],
    }

    for train_end, test_start, test_end in splits:
        train_df = df.iloc[:train_end]
        train_close = train_df["Close"]
        test_idx = slice(test_start, test_end)

        sma_cross = optimize_ma_crossover(train_close, sma, grid["sma_fast"], grid["sma_slow"], interval=interval)
        sma_trend = optimize_single_ma_trend(train_close, grid["sma_trend"], interval=interval)
        ema_cross = optimize_ma_crossover(train_close, ema, grid["ema_fast"], grid["ema_slow"], interval=interval)
        rsi_best = optimize_rsi(train_close, grid["rsi_window"], grid["rsi_thresholds"], interval=interval)
        macd_best = optimize_macd(train_close, grid["macd_fast"], grid["macd_slow"], grid["macd_signal"],
                                   interval=interval)
        bb_best = optimize_bollinger(train_df, grid["bb_window"], grid["bb_std"], interval=interval)
        stoch_best = optimize_stochastic(train_df, grid["stoch_k"], grid["stoch_d"], interval=interval)

        # Score each fold-chosen parameter set on the held-out block only.
        # Indicators are computed on the full series (so rolling windows have
        # proper lookback) but the cost-adjusted Sharpe score only ever looks
        # at test_idx. _apply_cost is computed over the full series first so a
        # position change right at the fold boundary still gets charged.
        fast_ma, slow_ma_ = sma(close, sma_cross["fast"]), sma(close, sma_cross["slow"])
        pos = (fast_ma > slow_ma_).astype(int).shift(1).fillna(0)
        fold_scores["sma_crossover"].append(_sharpe(_apply_cost(pos, daily_ret).iloc[test_idx], interval))

        ma_line = sma(close, sma_trend["window"])
        pos = (close > ma_line).astype(int).shift(1).fillna(0)
        fold_scores["sma_trend"].append(_sharpe(_apply_cost(pos, daily_ret).iloc[test_idx], interval))

        fast_ema, slow_ema = ema(close, ema_cross["fast"]), ema(close, ema_cross["slow"])
        pos = (fast_ema > slow_ema).astype(int).shift(1).fillna(0)
        fold_scores["ema_crossover"].append(_sharpe(_apply_cost(pos, daily_ret).iloc[test_idx], interval))

        r = rsi(close, rsi_best["window"])
        pos = _mean_reversion_position(daily_ret, r, rsi_best["low_th"], rsi_best["high_th"])
        fold_scores["rsi"].append(_sharpe(_apply_cost(pos, daily_ret).iloc[test_idx], interval))

        macd_line, signal_line, _ = macd(close, macd_best["fast"], macd_best["slow"], macd_best["signal"])
        pos = (macd_line > signal_line).astype(int).shift(1).fillna(0)
        fold_scores["macd"].append(_sharpe(_apply_cost(pos, daily_ret).iloc[test_idx], interval))

        upper, _, lower = bollinger_bands(close, bb_best["window"], bb_best["num_std"])
        raw = pd.Series(np.nan, index=close.index)
        raw[close < lower] = 1
        raw[close > upper] = 0
        pos = raw.ffill().fillna(0).shift(1).fillna(0)
        fold_scores["bollinger"].append(_sharpe(_apply_cost(pos, daily_ret).iloc[test_idx], interval))

        k, _ = stochastic_oscillator(df, stoch_best["k_window"], stoch_best["d_window"])
        pos = _mean_reversion_position(daily_ret, k, 20, 80)
        fold_scores["stochastic"].append(_sharpe(_apply_cost(pos, daily_ret).iloc[test_idx], interval))

    summary = {}
    for family, scores in fold_scores.items():
        valid = [s for s in scores if np.isfinite(s)]
        summary[family] = {
            "out_of_sample_sharpe_mean": round(float(np.mean(valid)), 3) if valid else None,
            "out_of_sample_sharpe_by_fold": [round(s, 3) if np.isfinite(s) else None for s in scores],
            "folds_used": len(valid),
        }
    return summary


def build_honest_assessment(backtest_scores: dict, wf_summary: dict | None) -> dict:
    """Compare in-sample (full-history) Sharpe against out-of-sample
    (walk-forward) Sharpe per family, and flag likely overfitting."""
    if wf_summary is None:
        return {
            "available": False,
            "reason": "Not enough history for walk-forward validation (need "
                      "roughly 150+ bars for a meaningful test). Results above "
                      "are in-sample only — treat them cautiously.",
        }

    family_to_insample_key = {
        "sma_crossover": "sma_crossover_sharpe",
        "sma_trend": "sma_trend_sharpe",
        "ema_crossover": "ema_crossover_sharpe",
        "rsi": "rsi_sharpe",
        "macd": "macd_sharpe",
        "bollinger": "bollinger_sharpe",
        "stochastic": "stochastic_sharpe",
    }

    per_family = {}
    for family, in_sample_key in family_to_insample_key.items():
        in_sample = backtest_scores.get(in_sample_key)
        oos = wf_summary.get(family, {}).get("out_of_sample_sharpe_mean")
        verdict = "insufficient_data"
        if in_sample is not None and oos is not None:
            gap = round(in_sample - oos, 3)
            if oos > 0.2:
                verdict = "holds_up_out_of_sample"
            elif oos > -0.2:
                verdict = "weak_or_inconclusive"
            else:
                verdict = "likely_overfit"
            per_family[family] = {
                "in_sample_sharpe": in_sample,
                "out_of_sample_sharpe": oos,
                "gap": gap,
                "verdict": verdict,
            }
        else:
            per_family[family] = {
                "in_sample_sharpe": in_sample,
                "out_of_sample_sharpe": oos,
                "verdict": verdict,
            }

    return {
        "available": True,
        "per_family": per_family,
        "how_to_read": (
            "'holds_up_out_of_sample' = the optimized setting kept working on data "
            "it never saw during optimization — some real signal here. "
            "'weak_or_inconclusive' = roughly flat out-of-sample, no strong edge either way. "
            "'likely_overfit' = performed well in-sample but lost money out-of-sample — "
            "the 'optimal' parameter was mostly fitting noise, don't trust it as-is."
        ),
    }


def optimize_all_parameters(df: pd.DataFrame, grid: dict | None = None, interval: str = "1d") -> dict:
    """Run the full optimization sweep and return chosen params + their
    backtested scores."""
    if len(df) < 60:
        raise ValueError(
            "Need at least ~60 bars of history to reliably optimize indicator "
            "parameters. Use a longer 'period' (e.g. '1y' or '2y')."
        )

    grid = grid or DEFAULT_GRID
    close = df["Close"]

    sma_cross = optimize_ma_crossover(close, sma, grid["sma_fast"], grid["sma_slow"], interval=interval)
    sma_trend = optimize_single_ma_trend(close, grid["sma_trend"], interval=interval)
    ema_cross = optimize_ma_crossover(close, ema, grid["ema_fast"], grid["ema_slow"], interval=interval)
    rsi_best = optimize_rsi(close, grid["rsi_window"], grid["rsi_thresholds"], interval=interval)
    macd_best = optimize_macd(close, grid["macd_fast"], grid["macd_slow"], grid["macd_signal"], interval=interval)
    bb_best = optimize_bollinger(df, grid["bb_window"], grid["bb_std"], interval=interval)
    stoch_best = optimize_stochastic(df, grid["stoch_k"], grid["stoch_d"], interval=interval)

    # ATR/ADX windows are NOT walk-forward validated the way the 7 families
    # above are (see walk_forward_validate/build_honest_assessment — neither
    # covers them), and an autocorrelation-picked window on daily-return noise
    # is not a reliable per-ticker "optimization". Rather than presenting an
    # unvalidated window with the same "optimized" framing as the validated
    # families, use the conventional fixed default (matches
    # STATIC_DEFAULT_PARAMS) and report the autocorrelation estimate
    # separately, labeled as informational only.
    cycle_window = estimate_dominant_cycle(close)

    params = {
        "sma_fast": sma_cross["fast"],
        "sma_slow": sma_cross["slow"],
        "sma_trend": sma_trend["window"],
        "ema_fast": ema_cross["fast"],
        "ema_slow": ema_cross["slow"],
        "rsi_window": rsi_best["window"],
        "rsi_low": rsi_best["low_th"],
        "rsi_high": rsi_best["high_th"],
        "macd_fast": macd_best["fast"],
        "macd_slow": macd_best["slow"],
        "macd_signal": macd_best["signal"],
        "bb_window": bb_best["window"],
        "bb_std": bb_best["num_std"],
        "stoch_k": stoch_best["k_window"],
        "stoch_d": stoch_best["d_window"],
        "atr_window": STATIC_DEFAULT_PARAMS["atr_window"],
        "adx_window": STATIC_DEFAULT_PARAMS["adx_window"],
    }

    backtest_scores = {
        "sma_crossover_sharpe": round(sma_cross["sharpe"], 3),
        "sma_trend_sharpe": round(sma_trend["sharpe"], 3),
        "ema_crossover_sharpe": round(ema_cross["sharpe"], 3),
        "rsi_sharpe": round(rsi_best["sharpe"], 3),
        "macd_sharpe": round(macd_best["sharpe"], 3),
        "bollinger_sharpe": round(bb_best["sharpe"], 3),
        "stochastic_sharpe": round(stoch_best["sharpe"], 3),
        "dominant_cycle_length_days_informational_not_validated": cycle_window,
    }

    return {"params": params, "backtest_scores": backtest_scores}


# --------------------------------------------------------------------------
# MCP tools
# --------------------------------------------------------------------------

@mcp.tool()
def get_current_price(ticker: str) -> dict:
    """
    Get the current/latest price and basic quote info for a stock ticker.

    Args:
        ticker: Stock symbol, e.g. "AAPL", "MSFT", "TSLA".
    """
    tk = yf.Ticker(ticker)
    info = tk.fast_info  # lightweight, fast quote data

    last_price = getattr(info, "last_price", None)
    if last_price is None:
        # fast_info's last_price is sometimes empty (illiquid names, pre/post
        # market gaps). Fall back to the same tiered lookup get_share_price
        # uses (live -> intraday snapshot -> previous close) instead of
        # returning a quote with a silently missing price — get_share_price
        # raises if every tier fails, so that discipline carries through here
        # rather than this tool quietly handing back a None.
        last_price = get_share_price(ticker)

    result = {
        "ticker": ticker.upper(),
        "last_price": last_price,
        "previous_close": getattr(info, "previous_close", None),
        "open": getattr(info, "open", None),
        "day_high": getattr(info, "day_high", None),
        "day_low": getattr(info, "day_low", None),
        "year_high": getattr(info, "year_high", None),
        "year_low": getattr(info, "year_low", None),
        "volume": getattr(info, "last_volume", None),
        "market_cap": getattr(info, "market_cap", None),
        "currency": getattr(info, "currency", None),
        "exchange": getattr(info, "exchange", None),
    }

    if result["last_price"] is not None and result["previous_close"]:
        change = result["last_price"] - result["previous_close"]
        pct = (change / result["previous_close"]) * 100
        result["change"] = round(change, 4)
        result["change_percent"] = round(pct, 2)

    return result


@mcp.tool()
def get_historical_data(ticker: str, period: str = "6mo", interval: str = "1d") -> dict:
    """
    Get historical OHLCV (Open/High/Low/Close/Volume) data for a stock.

    Args:
        ticker: Stock symbol, e.g. "AAPL".
        period: One of 1d,5d,1mo,3mo,6mo,1y,2y,5y,10y,ytd,max. Default "6mo".
        interval: One of 1m,2m,5m,15m,30m,60m,90m,1h,1d,5d,1wk,1mo,3mo.
                  Intraday intervals (<1d) are only available for short periods
                  (e.g. last 60 days). Default "1d".
    """
    df = _fetch_history(ticker, period, interval)
    return {
        "ticker": ticker.upper(),
        "period": period,
        "interval": interval,
        "rows": len(df),
        "start_date": df.index[0].strftime("%Y-%m-%d %H:%M:%S"),
        "end_date": df.index[-1].strftime("%Y-%m-%d %H:%M:%S"),
        "data": _df_to_records(df[["Open", "High", "Low", "Close", "Volume"]]),
    }


@mcp.tool()
def optimize_indicator_parameters(ticker: str, period: str = "2y", interval: str = "1d",
                                   validate: bool = True) -> dict:
    """
    Backtest a grid of common parameter choices for each indicator family
    (SMA/EMA crossover windows, RSI window+thresholds, MACD periods, Bollinger
    window+width, Stochastic %K/%D windows, ATR/ADX window) against this
    ticker's own historical price data, and return whichever parameters
    produced the best risk-adjusted (Sharpe-like) return for a simple
    long/flat strategy built on each indicator.

    Use a longer period (>= 1y, ideally 2y+) for a meaningful optimization —
    short histories don't give the grid search enough signal to distinguish
    good parameters from noise.

    Args:
        ticker: Stock symbol, e.g. "AAPL".
        period: History window used for backtesting. Default "2y".
        interval: Bar interval. Default "1d".
        validate: If True (default), also runs anchored walk-forward
                  validation — re-optimizing on earlier data only and testing
                  on later unseen data — and flags which indicator families'
                  "optimal" settings actually hold up out-of-sample versus
                  which ones are likely overfit to noise. This is the honest
                  check on the in-sample numbers above; recommended to leave on.
    """
    df = _fetch_history(ticker, period, interval)
    optimized = optimize_all_parameters(df, interval=interval)

    result = {
        "ticker": ticker.upper(),
        "period": period,
        "interval": interval,
        "bars_used": len(df),
        "optimized_parameters": optimized["params"],
        "in_sample_backtest_scores": optimized["backtest_scores"],
        "note": (
            "'in_sample_backtest_scores' were chosen by backtesting simple long/flat "
            "strategies (net of an assumed "
            f"{DEFAULT_COST_BPS:.0f}bps cost per position change) for each indicator on "
            "this ticker's own price history and picking the highest-Sharpe combination. "
            "This is in-sample optimization (no out-of-sample validation on its own) — "
            "treat it as 'what rhythm has this stock historically respected', not a "
            "guarantee of future performance. atr_window/adx_window are NOT part of this "
            "optimization (they use the conventional default, 14) since ATR/ADX don't map "
            "onto a long/flat backtest the other families do — see "
            "'dominant_cycle_length_days_informational_not_validated' for an unvalidated "
            "autocorrelation-based estimate, not used as the actual window."
        ),
    }

    if validate:
        wf_summary = walk_forward_validate(df, interval=interval)
        result["walk_forward_out_of_sample_scores"] = wf_summary
        result["honest_assessment"] = build_honest_assessment(optimized["backtest_scores"], wf_summary)

    return result


@mcp.tool()
def get_technical_analysis(ticker: str, period: str = "1y", interval: str = "1d",
                            optimize: bool = True, include_series: bool = False) -> dict:
    """
    Compute technical-analysis indicators for a stock: SMA/EMA crossover pair,
    long-term trend SMA, RSI, MACD, Bollinger Bands, Stochastic Oscillator,
    ATR, ADX (+DI/-DI), OBV, VWAP.

    Args:
        ticker: Stock symbol, e.g. "AAPL".
        period: History window. Default "1y". Use "2y" or longer when
                optimize=True for a more reliable per-ticker optimization.
        interval: Bar interval, e.g. "1d", "1wk". Default "1d".
        optimize: If True (default), indicator windows/thresholds are chosen
                  per-ticker by backtesting a grid of candidates on this
                  ticker's own price history (same logic as
                  optimize_indicator_parameters). If False, uses fixed
                  conventional defaults (SMA 20/50/200, EMA 12/26, RSI 14,
                  MACD 12/26/9, Bollinger 20/2, Stochastic 14/3, ATR/ADX 14).
        include_series: If True, also returns the full indicator time series
                         (can be large). Default False (latest values + signals only).
    """
    df = _fetch_history(ticker, period, interval)

    backtest_scores = None
    if optimize:
        optimized = optimize_all_parameters(df, interval=interval)
        params = optimized["params"]
        backtest_scores = optimized["backtest_scores"]
    else:
        params = STATIC_DEFAULT_PARAMS

    indicators_df = build_indicators_dynamic(df, params)
    latest = indicators_df.iloc[-1]

    latest_values = {
        col: (None if pd.isna(latest[col]) else round(float(latest[col]), 4))
        for col in indicators_df.columns
    }

    result = {
        "ticker": ticker.upper(),
        "period": period,
        "interval": interval,
        "as_of": indicators_df.index[-1].strftime("%Y-%m-%d %H:%M:%S"),
        "optimized": optimize,
        "parameters_used": params,
        "latest_indicator_values": latest_values,
        "signals": summarize_signals_dynamic(latest, params),
    }
    if backtest_scores is not None:
        result["backtest_scores"] = backtest_scores

    if include_series:
        result["series"] = _df_to_records(indicators_df)

    return result


@mcp.tool()
def get_full_report(ticker: str, period: str = "1y", interval: str = "1d", optimize: bool = True) -> dict:
    """
    Combined report: current quote + latest technical indicators (optionally
    per-ticker optimized) + plain-English signal summary. Good default tool
    for "analyze this stock".

    Args:
        ticker: Stock symbol, e.g. "AAPL".
        period: History window for indicator calculation. Default "1y".
        interval: Bar interval. Default "1d".
        optimize: If True (default), indicator parameters are optimized per-
                  ticker via backtesting. If False, uses conventional fixed
                  defaults.
    """
    quote = get_current_price(ticker)
    ta = get_technical_analysis(ticker, period=period, interval=interval,
                                 optimize=optimize, include_series=False)

    result = {
        "ticker": ticker.upper(),
        "quote": quote,
        "as_of": ta["as_of"],
        "optimized": ta["optimized"],
        "parameters_used": ta["parameters_used"],
        "indicators": ta["latest_indicator_values"],
        "signals": ta["signals"],
    }
    if "backtest_scores" in ta:
        result["backtest_scores"] = ta["backtest_scores"]
    return result


if __name__ == "__main__":
    mcp.run(transport="stdio")