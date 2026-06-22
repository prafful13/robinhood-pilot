from __future__ import annotations

import logging

import pandas as pd

from strategy.base import Signal, Strategy

log = logging.getLogger(__name__)


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
        self.last_metrics: dict = {
            sym: {"rsi": None, "price": None, "signal": None, "macd_hist": None, "bb_pct_b": None}
            for sym in self.watchlist
        }

        historicals = await broker.get_historicals(self.watchlist, self.bar_interval, self.lookback_days)
        quotes = await broker.get_quotes(self.watchlist)

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

            if len(closes) < self.period + 2:
                log.warning("%s: only %d bars for BB, need %d", symbol, len(closes), self.period + 2)
                continue

            middle = closes.rolling(self.period).mean()
            std = closes.rolling(self.period).std()
            upper = float((middle + self.num_std * std).iloc[-1])
            lower = float((middle - self.num_std * std).iloc[-1])

            if pd.isna(lower) or (upper - lower) == 0:
                continue

            # %B: 0 = lower band, 50 = middle, 100 = upper band (>100 or <0 = outside bands)
            pct_b = (price - lower) / (upper - lower) * 100
            self.last_metrics[symbol]["bb_pct_b"] = round(pct_b, 2)

            if price <= 0:
                continue

            sig: str | None = None
            if price < lower:
                sig = "buy"
                band_pos = (price - float(middle.iloc[-1])) / ((upper - lower) / 2)
                signals.append(Signal(symbol=symbol, side="buy", price=price, rsi=band_pos,
                                      reason=f"${price:.2f} < lower band ${lower:.2f} (%B={pct_b:.1f})"))
            elif price > upper:
                sig = "sell"
                band_pos = (price - float(middle.iloc[-1])) / ((upper - lower) / 2)
                signals.append(Signal(symbol=symbol, side="sell", price=price, rsi=band_pos,
                                      reason=f"${price:.2f} > upper band ${upper:.2f} (%B={pct_b:.1f})"))
            self.last_metrics[symbol]["signal"] = sig

        return signals
