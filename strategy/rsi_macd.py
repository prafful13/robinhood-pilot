from __future__ import annotations

import pandas as pd

from strategy.base import Signal, Strategy


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
        historicals = await broker.get_historicals(self.watchlist, self.bar_interval, self.lookback_days)
        quotes = await broker.get_quotes(self.watchlist)

        min_bars = max(self.rsi_period, self.slow + self.signal_period) + 2
        for symbol in self.watchlist:
            bars = historicals.get(symbol, [])
            price = float(quotes.get(symbol, {}).get("last_trade_price", 0))
            if price <= 0 or len(bars) < min_bars:
                continue
            sig = self._compute(symbol, bars, price)
            if sig:
                signals.append(sig)

        return signals

    def _compute(self, symbol: str, bars: list[dict], price: float) -> Signal | None:
        closes = pd.to_numeric(
            pd.Series([b["close_price"] for b in bars]), errors="coerce"
        ).dropna().reset_index(drop=True)

        # Wilder's RSI
        delta = closes.diff()
        avg_gain = delta.clip(lower=0).ewm(
            alpha=1 / self.rsi_period, min_periods=self.rsi_period, adjust=False
        ).mean()
        avg_loss = (-delta.clip(upper=0)).ewm(
            alpha=1 / self.rsi_period, min_periods=self.rsi_period, adjust=False
        ).mean()
        rs = avg_gain / avg_loss.replace(0, float("nan"))
        current_rsi = float((100 - 100 / (1 + rs)).dropna().iloc[-1])

        # MACD histogram
        macd = closes.ewm(span=self.fast, adjust=False).mean() - closes.ewm(span=self.slow, adjust=False).mean()
        histogram = float((macd - macd.ewm(span=self.signal_period, adjust=False).mean()).iloc[-1])

        if current_rsi < self.oversold and histogram > 0:
            return Signal(
                symbol=symbol, side="buy", price=price, rsi=current_rsi,
                reason=f"RSI {current_rsi:.1f} < {self.oversold} + MACD hist={histogram:.4f} (bullish)",
            )
        if current_rsi > self.overbought and histogram < 0:
            return Signal(
                symbol=symbol, side="sell", price=price, rsi=current_rsi,
                reason=f"RSI {current_rsi:.1f} > {self.overbought} + MACD hist={histogram:.4f} (bearish)",
            )
        return None
