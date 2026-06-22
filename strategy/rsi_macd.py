from __future__ import annotations

import logging

import pandas as pd

from strategy.base import Signal, Strategy

log = logging.getLogger(__name__)


class RSIMACDCombo(Strategy):
    """RSI mean-reversion only when MACD histogram confirms direction.

    Buy:  RSI < oversold  AND  MACD histogram > 0 (momentum turning up)
    Sell: RSI > overbought AND MACD histogram < 0 (momentum turning down)

    Reduces false signals vs. RSI alone in choppy markets.
    """

    def __init__(self, cfg: dict) -> None:
        s = cfg["strategy"]
        self.watchlist: list[str] = cfg["watchlist"]
        self.rsi_period = int(s.get("rsi_period", 14))
        self.oversold = float(s.get("oversold", 30))
        self.overbought = float(s.get("overbought", 70))
        self.fast = int(s.get("macd_fast", 12))
        self.slow = int(s.get("macd_slow", 26))
        self.signal_period = int(s.get("macd_signal_period", 9))
        self.bar_interval: str = s["bar_interval"]
        self.lookback_days: int = s["lookback_days"]

    async def generate_signals(self, broker) -> list[Signal]:
        signals: list[Signal] = []
        self.last_metrics: dict = {
            sym: {"rsi": None, "price": None, "signal": None, "macd_hist": None, "bb_pct_b": None}
            for sym in self.watchlist
        }

        historicals = await broker.get_historicals(self.watchlist, self.bar_interval, self.lookback_days)
        quotes = await broker.get_quotes(self.watchlist)
        min_bars = max(self.rsi_period, self.slow + self.signal_period) + 2

        for symbol in self.watchlist:
            bars = historicals.get(symbol, [])
            if not bars:
                continue

            closes = pd.to_numeric(
                pd.Series([b["close_price"] for b in bars]), errors="coerce"
            ).dropna().reset_index(drop=True)

            price = float(quotes.get(symbol, {}).get("last_trade_price") or 0)
            rsi_val = Strategy._rsi(closes, self.rsi_period)

            if rsi_val is not None:
                self.last_metrics[symbol]["rsi"] = round(rsi_val, 2)
            if price > 0:
                self.last_metrics[symbol]["price"] = price

            if len(closes) < min_bars:
                log.warning("%s: only %d bars, need %d", symbol, len(closes), min_bars)
                continue

            macd = closes.ewm(span=self.fast, adjust=False).mean() - closes.ewm(span=self.slow, adjust=False).mean()
            histogram = float((macd - macd.ewm(span=self.signal_period, adjust=False).mean()).iloc[-1])
            self.last_metrics[symbol]["macd_hist"] = round(histogram, 6)

            if rsi_val is None or price <= 0:
                continue

            sig: str | None = None
            if rsi_val < self.oversold and histogram > 0:
                sig = "buy"
                signals.append(Signal(
                    symbol=symbol, side="buy", price=price, rsi=rsi_val,
                    reason=f"RSI {rsi_val:.1f} < {self.oversold} + MACD hist={histogram:.4f} (bullish)",
                ))
            elif rsi_val > self.overbought and histogram < 0:
                sig = "sell"
                signals.append(Signal(
                    symbol=symbol, side="sell", price=price, rsi=rsi_val,
                    reason=f"RSI {rsi_val:.1f} > {self.overbought} + MACD hist={histogram:.4f} (bearish)",
                ))
            self.last_metrics[symbol]["signal"] = sig

        return signals
