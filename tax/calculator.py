from __future__ import annotations
"""
FIFO cost-basis tax calculator.
Reads all trades from the local DB and returns:
  - realized short-term / long-term gains
  - estimated tax owed
  - per-symbol breakdown
"""

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date

from db.database import SessionLocal
from db.models import Trade


@dataclass
class TaxSummary:
    short_term_gain: float = 0.0
    long_term_gain: float = 0.0
    short_term_tax: float = 0.0
    long_term_tax: float = 0.0
    total_tax: float = 0.0
    by_symbol: dict = field(default_factory=dict)


def compute(short_term_rate: float = 0.24, long_term_rate: float = 0.15) -> TaxSummary:
    with SessionLocal() as db:
        trades = (
            db.query(Trade)
            .order_by(Trade.executed_at)
            .all()
        )

    # FIFO lots: symbol -> deque of (quantity, price, date)
    lots: dict[str, deque] = defaultdict(deque)
    by_symbol: dict[str, dict] = defaultdict(lambda: {"short_term": 0.0, "long_term": 0.0})

    st_gain = 0.0
    lt_gain = 0.0

    for trade in trades:
        if trade.side == "buy":
            lots[trade.symbol].append({
                "qty": trade.quantity,
                "price": trade.price,
                "date": trade.trade_date,
            })
        elif trade.side == "sell":
            remaining = trade.quantity
            while remaining > 0 and lots[trade.symbol]:
                lot = lots[trade.symbol][0]
                used = min(remaining, lot["qty"])
                gain = used * (trade.price - lot["price"])
                days = (trade.trade_date - lot["date"]).days

                if days >= 365:
                    lt_gain += gain
                    by_symbol[trade.symbol]["long_term"] += gain
                else:
                    st_gain += gain
                    by_symbol[trade.symbol]["short_term"] += gain

                lot["qty"] -= used
                remaining -= used
                if lot["qty"] == 0:
                    lots[trade.symbol].popleft()

    st_tax = max(0.0, st_gain * short_term_rate)
    lt_tax = max(0.0, lt_gain * long_term_rate)

    return TaxSummary(
        short_term_gain=st_gain,
        long_term_gain=lt_gain,
        short_term_tax=st_tax,
        long_term_tax=lt_tax,
        total_tax=st_tax + lt_tax,
        by_symbol=dict(by_symbol),
    )
