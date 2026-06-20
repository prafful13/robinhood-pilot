from __future__ import annotations

import pandas as pd

from strategy.base import Signal, Strategy


class BollingerBands(Strategy):
    """Buy when price closes below lower band (N σ), sell when above upper band."""

    def __init__(self, cfg: dict) -> None:
        s = cfg["strategy"]
        self.period = int(s.get("bb_period", 20))
        self.num_std = float(s.get("bb_std_dev", 2.0))
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
            if price <= 0 or len(bars) < self.period + 2:
                continue
            sig = self._compute(symbol, bars, price)
            if sig:
                signals.append(sig)

        return signals

    def _compute(self, symbol: str, bars: list[dict], price: float) -> Signal | None:
        closes = pd.to_numeric(
            pd.Series([b["close_price"] for b in bars]), errors="coerce"
        ).dropna().reset_index(drop=True)

        middle = closes.rolling(self.period).mean()
        std = closes.rolling(self.period).std()
        upper = (middle + self.num_std * std).iloc[-1]
        lower = (middle - self.num_std * std).iloc[-1]
        mid = middle.iloc[-1]

        if pd.isna(lower) or (upper - lower) == 0:
            return None

        # Normalized position: 0 = middle, ±1 = band edges, beyond ±1 = outside bands
        band_pos = (price - mid) / ((upper - lower) / 2)

        if price < lower:
            return Signal(symbol=symbol, side="buy", price=price, rsi=band_pos,
                          reason=f"${price:.2f} < lower band ${lower:.2f} (pos={band_pos:.2f}σ)")
        if price > upper:
            return Signal(symbol=symbol, side="sell", price=price, rsi=band_pos,
                          reason=f"${price:.2f} > upper band ${upper:.2f} (pos={band_pos:.2f}σ)")
        return None
