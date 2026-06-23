from __future__ import annotations

import logging

import pandas as pd

from strategy.base import Signal, Strategy

log = logging.getLogger(__name__)


class MACDCrossover(Strategy):
    """Buy on MACD bullish crossover (histogram flips positive), sell on bearish (flips negative)."""

    def __init__(self, cfg: dict) -> None:
        s = cfg["strategy"]
        self.fast = int(s.get("macd_fast", 12))
        self.slow = int(s.get("macd_slow", 26))
        self.signal_period = int(s.get("macd_signal_period", 9))
        self.bar_interval: str = s["bar_interval"]
        self.lookback_days: int = s["lookback_days"]
        self.watchlist: list[str] = cfg["watchlist"]

    async def generate_signals(self, broker) -> list[Signal]:
        signals: list[Signal] = []
        self.last_metrics: dict = {
            sym: {"rsi": None, "price": None, "signal": None, "macd_hist": None, "bb_pct_b": None}
            for sym in self.watchlist
        }

        historicals = await broker.get_historicals(self.watchlist, self.bar_interval, self.lookback_days)
        quotes = await broker.get_quotes(self.watchlist)
        min_bars = self.slow + self.signal_period + 2

        for symbol in self.watchlist:
            bars = historicals.get(symbol, [])
            if not bars:
                continue

            closes = pd.to_numeric(
                pd.Series([b["close_price"] for b in bars]), errors="coerce"
            ).dropna().reset_index(drop=True)

            price = float(quotes.get(symbol, {}).get("last_trade_price") or 0)
            rsi_val = Strategy._rsi(closes, 14)

            if rsi_val is not None:
                self.last_metrics[symbol]["rsi"] = round(rsi_val, 2)
            if price > 0:
                self.last_metrics[symbol]["price"] = price

            if len(closes) < min_bars:
                log.warning("%s: only %d bars for MACD, need %d", symbol, len(closes), min_bars)
                continue

            ema_fast = closes.ewm(span=self.fast, adjust=False).mean()
            ema_slow = closes.ewm(span=self.slow, adjust=False).mean()
            macd_line = ema_fast - ema_slow
            signal_line = macd_line.ewm(span=self.signal_period, adjust=False).mean()
            histogram = macd_line - signal_line

            curr_h = float(histogram.iloc[-1])
            prev_h = float(histogram.iloc[-2])
            self.last_metrics[symbol]["macd_hist"] = round(curr_h, 6)

            if price <= 0:
                continue

            sig: str | None = None
            if prev_h < 0 and curr_h > 0:
                sig = "buy"
                signals.append(Signal(symbol=symbol, side="buy", price=price, rsi=rsi_val or 0.0,
                                      reason=f"MACD bullish crossover hist={curr_h:.4f}"))
            elif prev_h > 0 and curr_h < 0:
                sig = "sell"
                signals.append(Signal(symbol=symbol, side="sell", price=price, rsi=rsi_val or 0.0,
                                      reason=f"MACD bearish crossover hist={curr_h:.4f}"))
            self.last_metrics[symbol]["signal"] = sig

        return signals
