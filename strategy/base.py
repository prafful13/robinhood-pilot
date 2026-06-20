from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal


@dataclass
class Signal:
    symbol: str
    side: Literal["buy", "sell"]
    rsi: float
    price: float
    reason: str = ""


class Strategy(ABC):
    @abstractmethod
    async def generate_signals(self, broker) -> list[Signal]:
        ...
