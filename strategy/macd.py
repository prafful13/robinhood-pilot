from __future__ import annotations

import pandas as pd

from strategy.base import Signal, Strategy


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
        historicals = await broker.get_historicals(self.watchlist, self.bar_interval, self.lookback_days)
        quotes = await broker.get_quotes(self.watchlist)

        for symbol in self.watchlist:
            bars = historicals.get(symbol, [])
            price = float(quotes.get(symbol, {}).get("last_trade_price", 0))
            if price <= 0 or len(bars) < self.slow + self.signal_period + 2:
                continue
            sig = self._compute(symbol, bars, price)
            if sig:
                signals.append(sig)

        return signals

    def _compute(self, symbol: str, bars: list[dict], price: float) -> Signal | None:
        closes = pd.to_numeric(
            pd.Series([b["close_price"] for b in bars]), errors="coerce"
        ).dropna().reset_index(drop=True)

        ema_fast = closes.ewm(span=self.fast, adjust=False).mean()
        ema_slow = closes.ewm(span=self.slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=self.signal_period, adjust=False).mean()
        histogram = macd_line - signal_line

        curr_h = float(histogram.iloc[-1])
        prev_h = float(histogram.iloc[-2])

        if prev_h < 0 and curr_h > 0:
            return Signal(symbol=symbol, side="buy", price=price, rsi=curr_h,
                          reason=f"MACD bullish crossover hist={curr_h:.4f}")
        if prev_h > 0 and curr_h < 0:
            return Signal(symbol=symbol, side="sell", price=price, rsi=curr_h,
                          reason=f"MACD bearish crossover hist={curr_h:.4f}")
        return None
