from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

import pandas as pd


@dataclass
class Signal:
    symbol: str
    side: Literal["buy", "sell"]
    rsi: float
    price: float
    reason: str = ""


class Strategy(ABC):
    @staticmethod
    def _rsi(closes: pd.Series, period: int) -> float | None:
        """Wilder's RSI — returns the latest RSI value, or None if insufficient data."""
        delta = closes.diff()
        avg_gain = (
            delta.clip(lower=0).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        )
        avg_loss = (
            (-delta.clip(upper=0)).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        )
        rsi = (100 - 100 / (1 + avg_gain / avg_loss.replace(0, float("nan")))).dropna()
        return float(rsi.iloc[-1]) if not rsi.empty else None

    @abstractmethod
    async def generate_signals(self, broker) -> list[Signal]: ...
