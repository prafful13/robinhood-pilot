from __future__ import annotations
from datetime import date

from db.database import SessionLocal
from db.models import Trade


class RiskManager:
    def __init__(self, cfg: dict):
        r = cfg["risk"]
        self.max_trade_usd: float = r["max_trade_usd"]
        self.max_positions: int = r["max_positions"]
        self.daily_loss_limit: float = r["daily_loss_limit_usd"]

    def _daily_realized_pnl(self) -> float:
        today = date.today()
        with SessionLocal() as db:
            trades = (
                db.query(Trade)
                .filter(
                    Trade.side == "sell",
                    Trade.trade_date == today,
                )
                .all()
            )
        return sum(t.realized_pnl or 0.0 for t in trades)

    def held_symbols(self, positions: list[dict]) -> set[str]:
        return {p["symbol"] for p in positions if float(p.get("quantity", 0)) > 0}

    def can_buy(self, symbol: str, positions: list[dict]) -> tuple[bool, str]:
        held = self.held_symbols(positions)

        if symbol in held:
            return False, f"already holding {symbol}"

        if len(held) >= self.max_positions:
            return False, f"max {self.max_positions} positions reached"

        pnl = self._daily_realized_pnl()
        if pnl <= -self.daily_loss_limit:
            return False, f"daily loss limit hit (${-pnl:.2f})"

        return True, ""

    def can_sell(self, symbol: str, positions: list[dict]) -> tuple[bool, str]:
        held = self.held_symbols(positions)
        if symbol not in held:
            return False, f"no position in {symbol}"
        return True, ""

    def position_for(self, symbol: str, positions: list[dict]) -> dict | None:
        for p in positions:
            if p["symbol"] == symbol and float(p.get("quantity", 0)) > 0:
                return p
        return None
