from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Hard-coded absolute maximums that cannot be overridden at runtime.
# These act as a final circuit-breaker independent of strategy logic.
_ABS_MAX_NOTIONAL_PER_ORDER: float = 1_000.0   # never spend more than $1,000 in one order
_ABS_MAX_ORDERS_PER_CYCLE: int = 10             # never place more than 10 orders in one cycle
_ABS_MAX_CONCENTRATION_PCT: float = 50.0        # never put more than 50% of portfolio in one symbol


@dataclass
class RiskLimits:
    """Order-level safety guardrails checked before every order submission.

    These limits are independent of RiskManager (which tracks daily P&L and
    position counts). RiskLimits enforces hard notional caps and prevents
    runaway order bursts within a single reconciliation cycle.

    All three limit types are configurable per-run but are further bounded by
    absolute maximums that cannot be exceeded regardless of config.
    """
    max_notional_per_order: float   # maximum dollar amount for a single buy order
    max_orders_per_cycle: int       # maximum number of orders to submit in one cycle
    max_concentration_pct: float    # maximum % of total portfolio value for a single symbol

    # Tracks how many orders have been placed in the current cycle.
    _orders_this_cycle: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_notional_per_order <= 0:
            raise ValueError(f"max_notional_per_order must be positive, got {self.max_notional_per_order}")
        if self.max_orders_per_cycle <= 0:
            raise ValueError(f"max_orders_per_cycle must be positive, got {self.max_orders_per_cycle}")
        if not (0 < self.max_concentration_pct <= 100):
            raise ValueError(f"max_concentration_pct must be in (0, 100], got {self.max_concentration_pct}")

        # Clamp to absolute safety ceilings — strategy config cannot exceed these.
        if self.max_notional_per_order > _ABS_MAX_NOTIONAL_PER_ORDER:
            log.warning(
                "max_notional_per_order %.2f exceeds absolute ceiling %.2f — clamping",
                self.max_notional_per_order,
                _ABS_MAX_NOTIONAL_PER_ORDER,
            )
            self.max_notional_per_order = _ABS_MAX_NOTIONAL_PER_ORDER

        if self.max_orders_per_cycle > _ABS_MAX_ORDERS_PER_CYCLE:
            log.warning(
                "max_orders_per_cycle %d exceeds absolute ceiling %d — clamping",
                self.max_orders_per_cycle,
                _ABS_MAX_ORDERS_PER_CYCLE,
            )
            self.max_orders_per_cycle = _ABS_MAX_ORDERS_PER_CYCLE

        if self.max_concentration_pct > _ABS_MAX_CONCENTRATION_PCT:
            log.warning(
                "max_concentration_pct %.1f exceeds absolute ceiling %.1f — clamping",
                self.max_concentration_pct,
                _ABS_MAX_CONCENTRATION_PCT,
            )
            self.max_concentration_pct = _ABS_MAX_CONCENTRATION_PCT

    def reset_cycle(self) -> None:
        """Call at the start of each reconciliation cycle to reset the per-cycle order counter."""
        self._orders_this_cycle = 0

    def check_notional(self, symbol: str, notional: float) -> tuple[bool, str]:
        """Return (True, "") if the notional amount is within limits, else (False, reason)."""
        if notional <= 0:
            return False, f"notional must be positive, got {notional:.2f} for {symbol}"
        if notional > self.max_notional_per_order:
            return False, (
                f"order notional ${notional:.2f} for {symbol} exceeds limit "
                f"${self.max_notional_per_order:.2f}"
            )
        return True, ""

    def check_order_count(self) -> tuple[bool, str]:
        """Return (True, "") if another order can be placed this cycle."""
        if self._orders_this_cycle >= self.max_orders_per_cycle:
            return False, (
                f"max orders per cycle ({self.max_orders_per_cycle}) already reached "
                f"({self._orders_this_cycle} placed this cycle)"
            )
        return True, ""

    def check_concentration(
        self, symbol: str, order_notional: float, portfolio_value: float
    ) -> tuple[bool, str]:
        """Return (True, "") if buying order_notional of symbol does not breach concentration limit.

        If portfolio_value is zero (e.g. no cash/positions yet), the check is skipped.
        """
        if portfolio_value <= 0:
            return True, ""
        pct = (order_notional / portfolio_value) * 100.0
        if pct > self.max_concentration_pct:
            return False, (
                f"order ${order_notional:.2f} for {symbol} would represent "
                f"{pct:.1f}% of portfolio (limit: {self.max_concentration_pct:.1f}%)"
            )
        return True, ""

    def record_order_placed(self) -> None:
        """Increment the per-cycle counter after a successful order submission."""
        self._orders_this_cycle += 1

    @classmethod
    def from_config(cls, cfg: dict) -> "RiskLimits":
        """Build from the merged config dict (config.yaml + RuntimeConfig overlay)."""
        risk = cfg.get("risk", {})
        return cls(
            max_notional_per_order=float(risk.get("max_notional_per_order", risk.get("max_trade_usd", 300.0))),
            max_orders_per_cycle=int(risk.get("max_orders_per_cycle", 3)),
            max_concentration_pct=float(risk.get("max_concentration_pct", 25.0)),
        )
