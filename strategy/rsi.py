from __future__ import annotations
"""
RSI Mean Reversion strategy.
  Buy  when RSI(14) crosses below oversold threshold (default 30).
  Sell when RSI(14) crosses above overbought threshold (default 70).
"""

import pandas as pd

import logging

from strategy.base import Signal, Strategy

log = logging.getLogger(__name__)


class RSIMeanReversion(Strategy):
    def __init__(self, cfg: dict):
        s = cfg["strategy"]
        self.watchlist: list[str] = cfg["watchlist"]
        self.rsi_period: int = s["rsi_period"]
        self.oversold: float = s["oversold"]
        self.overbought: float = s["overbought"]
        self.bar_interval: str = s["bar_interval"]
        self.lookback_days: int = s["lookback_days"]

    def _compute_rsi(self, bars: list[dict]) -> pd.Series:
        closes = pd.to_numeric(
            pd.Series([b["close_price"] for b in bars]), errors="coerce"
        ).dropna().reset_index(drop=True)

        delta = closes.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        # Wilder's smoothing: simple mean for first window, then EWM
        n = self.rsi_period
        avg_gain = gain.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()

        rs = avg_gain / avg_loss.replace(0, float("nan"))
        return 100 - (100 / (1 + rs))

    async def generate_signals(self, broker) -> list[Signal]:
        signals: list[Signal] = []
        self.last_metrics: dict = {sym: {"rsi": None, "price": None, "signal": None} for sym in self.watchlist}

        historicals = await broker.get_historicals(
            self.watchlist, self.bar_interval, self.lookback_days
        )
        quotes = await broker.get_quotes(self.watchlist)

        for symbol in self.watchlist:
            bars = historicals.get(symbol, [])
            if len(bars) < self.rsi_period + 2:
                log.warning("%s: only %d bars, need %d — skipping", symbol, len(bars), self.rsi_period + 2)
                continue

            rsi_series = self._compute_rsi(bars)
            if rsi_series.isna().all():
                continue

            current_rsi = float(rsi_series.dropna().iloc[-1])
            prev_rsi = float(rsi_series.dropna().iloc[-2])
            quote = quotes.get(symbol, {})
            price = float(quote.get("last_trade_price") or 0)

            # Always record RSI even if price is unavailable
            self.last_metrics[symbol]["rsi"] = round(current_rsi, 2)
            self.last_metrics[symbol]["macd_hist"] = None
            self.last_metrics[symbol]["bb_pct_b"] = None
            if price > 0:
                self.last_metrics[symbol]["price"] = price

            if price <= 0:
                continue

            signal: str | None = None

            # Buy: RSI crossed below oversold
            if current_rsi < self.oversold:
                signal = "buy"
                signals.append(Signal(
                    symbol=symbol,
                    side="buy",
                    rsi=current_rsi,
                    price=price,
                    reason=f"RSI {current_rsi:.1f} < {self.oversold}",
                ))

            # Sell: RSI crossed above overbought
            elif current_rsi > self.overbought:
                signal = "sell"
                signals.append(Signal(
                    symbol=symbol,
                    side="sell",
                    rsi=current_rsi,
                    price=price,
                    reason=f"RSI {current_rsi:.1f} > {self.overbought}",
                ))

            self.last_metrics[symbol] = {
                "rsi": round(current_rsi, 2),
                "price": price,
                "signal": signal,
            }

        return signals
