from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from risk.guardrails import (
    RiskLimits,
    _ABS_MAX_CONCENTRATION_PCT,
    _ABS_MAX_NOTIONAL_PER_ORDER,
    _ABS_MAX_ORDERS_PER_CYCLE,
)


# ---------------------------------------------------------------------------
# RiskLimits unit tests
# ---------------------------------------------------------------------------


class TestRiskLimitsConstruction:
    def test_valid_construction(self):
        rl = RiskLimits(max_notional_per_order=300.0, max_orders_per_cycle=3, max_concentration_pct=25.0)
        assert rl.max_notional_per_order == 300.0
        assert rl.max_orders_per_cycle == 3
        assert rl.max_concentration_pct == 25.0
        assert rl._orders_this_cycle == 0

    def test_zero_notional_raises(self):
        with pytest.raises(ValueError, match="max_notional_per_order must be positive"):
            RiskLimits(max_notional_per_order=0, max_orders_per_cycle=3, max_concentration_pct=25.0)

    def test_negative_notional_raises(self):
        with pytest.raises(ValueError, match="max_notional_per_order must be positive"):
            RiskLimits(max_notional_per_order=-1.0, max_orders_per_cycle=3, max_concentration_pct=25.0)

    def test_zero_orders_per_cycle_raises(self):
        with pytest.raises(ValueError, match="max_orders_per_cycle must be positive"):
            RiskLimits(max_notional_per_order=300.0, max_orders_per_cycle=0, max_concentration_pct=25.0)

    def test_zero_concentration_raises(self):
        with pytest.raises(ValueError, match="max_concentration_pct must be in"):
            RiskLimits(max_notional_per_order=300.0, max_orders_per_cycle=3, max_concentration_pct=0.0)

    def test_over_100_concentration_raises(self):
        with pytest.raises(ValueError, match="max_concentration_pct must be in"):
            RiskLimits(max_notional_per_order=300.0, max_orders_per_cycle=3, max_concentration_pct=101.0)

    def test_notional_clamped_to_absolute_ceiling(self):
        rl = RiskLimits(
            max_notional_per_order=_ABS_MAX_NOTIONAL_PER_ORDER + 500,
            max_orders_per_cycle=3,
            max_concentration_pct=25.0,
        )
        assert rl.max_notional_per_order == _ABS_MAX_NOTIONAL_PER_ORDER

    def test_orders_per_cycle_clamped_to_absolute_ceiling(self):
        rl = RiskLimits(
            max_notional_per_order=300.0,
            max_orders_per_cycle=_ABS_MAX_ORDERS_PER_CYCLE + 100,
            max_concentration_pct=25.0,
        )
        assert rl.max_orders_per_cycle == _ABS_MAX_ORDERS_PER_CYCLE

    def test_concentration_clamped_to_absolute_ceiling(self):
        rl = RiskLimits(
            max_notional_per_order=300.0,
            max_orders_per_cycle=3,
            max_concentration_pct=_ABS_MAX_CONCENTRATION_PCT + 10,
        )
        assert rl.max_concentration_pct == _ABS_MAX_CONCENTRATION_PCT


class TestCheckNotional:
    def setup_method(self):
        self.rl = RiskLimits(max_notional_per_order=500.0, max_orders_per_cycle=5, max_concentration_pct=30.0)

    def test_within_limit_passes(self):
        ok, reason = self.rl.check_notional("AAPL", 400.0)
        assert ok is True
        assert reason == ""

    def test_exactly_at_limit_passes(self):
        ok, reason = self.rl.check_notional("AAPL", 500.0)
        assert ok is True

    def test_exceeds_limit_fails(self):
        ok, reason = self.rl.check_notional("AAPL", 501.0)
        assert ok is False
        assert "AAPL" in reason
        assert "501.00" in reason
        assert "500.00" in reason

    def test_zero_notional_fails(self):
        ok, reason = self.rl.check_notional("AAPL", 0.0)
        assert ok is False
        assert "positive" in reason

    def test_negative_notional_fails(self):
        ok, reason = self.rl.check_notional("AAPL", -10.0)
        assert ok is False


class TestCheckOrderCount:
    def setup_method(self):
        self.rl = RiskLimits(max_notional_per_order=300.0, max_orders_per_cycle=2, max_concentration_pct=25.0)

    def test_first_order_passes(self):
        ok, _ = self.rl.check_order_count()
        assert ok is True

    def test_at_limit_fails(self):
        self.rl.record_order_placed()
        self.rl.record_order_placed()
        ok, reason = self.rl.check_order_count()
        assert ok is False
        assert "2" in reason

    def test_reset_cycle_clears_counter(self):
        self.rl.record_order_placed()
        self.rl.record_order_placed()
        self.rl.reset_cycle()
        ok, _ = self.rl.check_order_count()
        assert ok is True

    def test_record_and_count_increments(self):
        assert self.rl._orders_this_cycle == 0
        self.rl.record_order_placed()
        assert self.rl._orders_this_cycle == 1
        ok, _ = self.rl.check_order_count()
        assert ok is True
        self.rl.record_order_placed()
        ok, _ = self.rl.check_order_count()
        assert ok is False


class TestCheckConcentration:
    def setup_method(self):
        self.rl = RiskLimits(max_notional_per_order=500.0, max_orders_per_cycle=5, max_concentration_pct=25.0)

    def test_within_concentration_passes(self):
        ok, _ = self.rl.check_concentration("AAPL", 200.0, 1000.0)
        assert ok is True

    def test_exactly_at_limit_passes(self):
        ok, _ = self.rl.check_concentration("AAPL", 250.0, 1000.0)
        assert ok is True

    def test_exceeds_concentration_fails(self):
        ok, reason = self.rl.check_concentration("AAPL", 300.0, 1000.0)
        assert ok is False
        assert "AAPL" in reason
        assert "30.0%" in reason

    def test_zero_portfolio_value_skips_check(self):
        ok, _ = self.rl.check_concentration("AAPL", 9999.0, 0.0)
        assert ok is True

    def test_negative_portfolio_value_skips_check(self):
        ok, _ = self.rl.check_concentration("AAPL", 9999.0, -1.0)
        assert ok is True


class TestFromConfig:
    def test_builds_from_full_config(self):
        cfg = {"risk": {"max_notional_per_order": 400.0, "max_orders_per_cycle": 4, "max_concentration_pct": 20.0}}
        rl = RiskLimits.from_config(cfg)
        assert rl.max_notional_per_order == 400.0
        assert rl.max_orders_per_cycle == 4
        assert rl.max_concentration_pct == 20.0

    def test_falls_back_to_max_trade_usd(self):
        cfg = {"risk": {"max_trade_usd": 250.0}}
        rl = RiskLimits.from_config(cfg)
        assert rl.max_notional_per_order == 250.0

    def test_uses_defaults_when_keys_absent(self):
        cfg = {"risk": {}}
        rl = RiskLimits.from_config(cfg)
        assert rl.max_notional_per_order == 300.0
        assert rl.max_orders_per_cycle == 3
        assert rl.max_concentration_pct == 25.0

    def test_empty_config(self):
        rl = RiskLimits.from_config({})
        assert rl.max_notional_per_order == 300.0


# ---------------------------------------------------------------------------
# Integration: dry-run mode in _reconcile
# ---------------------------------------------------------------------------


class TestDryRunMode:
    """_reconcile with dry_run=True must log orders but never call broker.place_buy/sell_order."""

    def _make_broker(self):
        broker = MagicMock()
        broker.review_order = AsyncMock()
        broker.place_buy_order = AsyncMock(return_value={"data": {"order": {"id": "ord-1"}}})
        broker.review_sell_order = AsyncMock()
        broker.place_sell_order = AsyncMock(return_value={"data": {"order": {"id": "ord-2"}}})
        broker.get_quotes = AsyncMock(return_value={"AAPL": {"last_trade_price": "150.0"}})
        return broker

    def _make_pending(self, side: str = "buy", symbol: str = "AAPL") -> MagicMock:
        from db.models import DesiredPosition
        dp = MagicMock(spec=DesiredPosition)
        dp.id = 1
        dp.symbol = symbol
        dp.side = side
        dp.target_usd = 300.0
        dp.signal_rsi = 28.0
        dp.signal_price = 150.0
        dp.retry_count = 0
        dp.status = "pending"
        return dp

    def _patch_session(self, dp: MagicMock):
        mock_sl = MagicMock()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [dp]
        mock_sl.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_sl.return_value.__exit__ = MagicMock(return_value=False)
        return mock_sl

    @pytest.mark.asyncio
    async def test_dry_run_buy_does_not_submit_order(self):
        import main

        broker = self._make_broker()
        dp = self._make_pending("buy")
        updated: list[dict] = []

        with (
            patch.object(main, "SessionLocal", self._patch_session(dp)),
            patch.object(main, "_update_desired", side_effect=lambda id, **kw: updated.append(kw)),
            patch.object(main, "record_trade") as mock_record,
            patch.object(main, "_is_kill_switch_active", return_value=False),
        ):
            risk = MagicMock()
            risk.can_buy.return_value = (True, "")
            risk.max_trade_usd = 300.0
            await main._reconcile(
                broker, risk, "acc-1", [], MagicMock(),
                dry_run=True,
            )

        broker.place_buy_order.assert_not_called()
        mock_record.assert_not_called()
        assert any(u.get("status") == "achieved" for u in updated)
        assert any("dry-run" in (u.get("error_msg") or "") for u in updated)

    @pytest.mark.asyncio
    async def test_dry_run_sell_does_not_submit_order(self):
        import main

        broker = self._make_broker()
        dp = self._make_pending("sell", "AAPL")
        positions = [{"symbol": "AAPL", "quantity": "2", "average_buy_price": "140.0"}]
        updated: list[dict] = []

        with (
            patch.object(main, "SessionLocal", self._patch_session(dp)),
            patch.object(main, "_update_desired", side_effect=lambda id, **kw: updated.append(kw)),
            patch.object(main, "record_trade") as mock_record,
            patch.object(main, "_is_kill_switch_active", return_value=False),
        ):
            risk = MagicMock()
            risk.can_sell.return_value = (True, "")
            risk.position_for.return_value = {"symbol": "AAPL", "quantity": "2", "average_buy_price": "140.0"}
            await main._reconcile(
                broker, risk, "acc-1", positions, MagicMock(),
                dry_run=True,
            )

        broker.place_sell_order.assert_not_called()
        mock_record.assert_not_called()
        assert any(u.get("status") == "achieved" for u in updated)

    @pytest.mark.asyncio
    async def test_dry_run_increments_order_counter(self):
        import main

        broker = self._make_broker()
        dp = self._make_pending("buy")
        limits = RiskLimits(max_notional_per_order=500.0, max_orders_per_cycle=5, max_concentration_pct=30.0)

        with (
            patch.object(main, "SessionLocal", self._patch_session(dp)),
            patch.object(main, "_update_desired"),
            patch.object(main, "record_trade"),
            patch.object(main, "_is_kill_switch_active", return_value=False),
        ):
            risk = MagicMock()
            risk.can_buy.return_value = (True, "")
            risk.max_trade_usd = 300.0
            await main._reconcile(
                broker, risk, "acc-1", [], MagicMock(),
                dry_run=True,
                limits=limits,
            )

        assert limits._orders_this_cycle == 1


# ---------------------------------------------------------------------------
# Integration: kill-switch in _reconcile
# ---------------------------------------------------------------------------


class TestKillSwitch:
    def _make_broker(self):
        broker = MagicMock()
        broker.review_order = AsyncMock()
        broker.place_buy_order = AsyncMock(return_value={"data": {"order": {"id": "ord-1"}}})
        return broker

    @pytest.mark.asyncio
    async def test_kill_switch_halts_all_submission(self):
        import main

        broker = self._make_broker()

        with patch.object(main, "_is_kill_switch_active", return_value=True):
            risk = MagicMock()
            await main._reconcile(broker, risk, "acc-1", [], MagicMock())

        broker.place_buy_order.assert_not_called()
        broker.review_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_kill_switch_off_allows_orders(self):
        """When kill switch is off, the normal reconcile path runs."""
        import main

        from db.models import DesiredPosition
        dp = MagicMock(spec=DesiredPosition)
        dp.id = 1
        dp.symbol = "GOOGL"
        dp.side = "buy"
        dp.target_usd = 300.0
        dp.signal_rsi = 25.0
        dp.signal_price = 100.0
        dp.retry_count = 0
        dp.status = "pending"

        mock_sl = MagicMock()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [dp]
        mock_sl.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_sl.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch.object(main, "SessionLocal", mock_sl),
            patch.object(main, "_update_desired"),
            patch.object(main, "record_trade"),
            patch.object(main, "_place_order_with_retry", new=AsyncMock(
                return_value={"data": {"order": {"id": "ord-x"}}}
            )),
            patch.object(main, "_is_kill_switch_active", return_value=False),
        ):
            risk = MagicMock()
            risk.can_buy.return_value = (True, "")
            risk.max_trade_usd = 300.0
            await main._reconcile(broker, risk, "acc-1", [], MagicMock())

        broker.review_order.assert_called_once()


# ---------------------------------------------------------------------------
# Integration: guardrail blocking in _reconcile
# ---------------------------------------------------------------------------


class TestGuardrailsInReconcile:
    """Guardrail failures must block order submission and increment retry_count."""

    def _make_broker(self):
        broker = MagicMock()
        broker.review_order = AsyncMock()
        broker.place_buy_order = AsyncMock(return_value={"data": {"order": {"id": "ord-1"}}})
        return broker

    def _make_pending(self, target_usd: float = 300.0) -> MagicMock:
        from db.models import DesiredPosition
        dp = MagicMock(spec=DesiredPosition)
        dp.id = 1
        dp.symbol = "AAPL"
        dp.side = "buy"
        dp.target_usd = target_usd
        dp.signal_rsi = 28.0
        dp.signal_price = 150.0
        dp.retry_count = 0
        dp.status = "pending"
        return dp

    def _patch_session(self, dp: MagicMock):
        mock_sl = MagicMock()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [dp]
        mock_sl.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_sl.return_value.__exit__ = MagicMock(return_value=False)
        return mock_sl

    @pytest.mark.asyncio
    async def test_notional_guardrail_blocks_order(self):
        import main

        broker = self._make_broker()
        dp = self._make_pending(target_usd=600.0)
        limits = RiskLimits(max_notional_per_order=400.0, max_orders_per_cycle=5, max_concentration_pct=50.0)
        updated: list[dict] = []

        with (
            patch.object(main, "SessionLocal", self._patch_session(dp)),
            patch.object(main, "_update_desired", side_effect=lambda id, **kw: updated.append(kw)),
            patch.object(main, "record_trade") as mock_record,
            patch.object(main, "_is_kill_switch_active", return_value=False),
        ):
            risk = MagicMock()
            risk.can_buy.return_value = (True, "")
            risk.max_trade_usd = 300.0
            await main._reconcile(
                broker, risk, "acc-1", [], MagicMock(),
                limits=limits,
            )

        broker.place_buy_order.assert_not_called()
        mock_record.assert_not_called()
        assert any("retry_count" in u for u in updated)

    @pytest.mark.asyncio
    async def test_order_count_guardrail_stops_further_orders(self):
        import main

        broker = self._make_broker()
        limits = RiskLimits(max_notional_per_order=500.0, max_orders_per_cycle=1, max_concentration_pct=50.0)
        limits.record_order_placed()  # already at limit

        dp = self._make_pending(target_usd=300.0)
        updated: list[dict] = []

        with (
            patch.object(main, "SessionLocal", self._patch_session(dp)),
            patch.object(main, "_update_desired", side_effect=lambda id, **kw: updated.append(kw)),
            patch.object(main, "record_trade") as mock_record,
            patch.object(main, "_is_kill_switch_active", return_value=False),
        ):
            risk = MagicMock()
            risk.can_buy.return_value = (True, "")
            risk.max_trade_usd = 300.0
            await main._reconcile(
                broker, risk, "acc-1", [], MagicMock(),
                limits=limits,
            )

        broker.place_buy_order.assert_not_called()
        mock_record.assert_not_called()

    @pytest.mark.asyncio
    async def test_concentration_guardrail_blocks_oversized_order(self):
        import main

        broker = self._make_broker()
        limits = RiskLimits(max_notional_per_order=500.0, max_orders_per_cycle=5, max_concentration_pct=10.0)
        dp = self._make_pending(target_usd=300.0)
        updated: list[dict] = []

        with (
            patch.object(main, "SessionLocal", self._patch_session(dp)),
            patch.object(main, "_update_desired", side_effect=lambda id, **kw: updated.append(kw)),
            patch.object(main, "record_trade") as mock_record,
            patch.object(main, "_is_kill_switch_active", return_value=False),
        ):
            risk = MagicMock()
            risk.can_buy.return_value = (True, "")
            risk.max_trade_usd = 300.0
            # portfolio_value=500 → 300/500=60% > 10% limit
            await main._reconcile(
                broker, risk, "acc-1", [], MagicMock(),
                limits=limits,
                portfolio_value=500.0,
            )

        broker.place_buy_order.assert_not_called()
        mock_record.assert_not_called()

    @pytest.mark.asyncio
    async def test_reset_cycle_allows_orders_after_limit(self):
        """After reset_cycle(), orders blocked by order count should be allowed again."""
        import main

        broker = self._make_broker()
        limits = RiskLimits(max_notional_per_order=500.0, max_orders_per_cycle=1, max_concentration_pct=50.0)
        limits.record_order_placed()  # at limit

        limits.reset_cycle()  # reset — should allow again

        dp = self._make_pending(target_usd=300.0)
        updated: list[dict] = []

        with (
            patch.object(main, "SessionLocal", self._patch_session(dp)),
            patch.object(main, "_update_desired", side_effect=lambda id, **kw: updated.append(kw)),
            patch.object(main, "record_trade"),
            patch.object(main, "_place_order_with_retry", new=AsyncMock(
                return_value={"data": {"order": {"id": "ord-1"}}}
            )),
            patch.object(main, "_is_kill_switch_active", return_value=False),
        ):
            risk = MagicMock()
            risk.can_buy.return_value = (True, "")
            risk.max_trade_usd = 300.0
            await main._reconcile(
                broker, risk, "acc-1", [], MagicMock(),
                limits=limits,
            )

        broker.review_order.assert_called_once()


# ---------------------------------------------------------------------------
# Integration: _is_kill_switch_active reads from DB
# ---------------------------------------------------------------------------


class TestIsKillSwitchActive:
    def test_returns_true_when_set(self):
        import main

        ctrl = MagicMock()
        ctrl.kill_switch = True

        mock_sl = MagicMock()
        mock_db = MagicMock()
        mock_db.get.return_value = ctrl
        mock_sl.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_sl.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(main, "SessionLocal", mock_sl):
            assert main._is_kill_switch_active() is True

    def test_returns_false_when_not_set(self):
        import main

        ctrl = MagicMock()
        ctrl.kill_switch = False

        mock_sl = MagicMock()
        mock_db = MagicMock()
        mock_db.get.return_value = ctrl
        mock_sl.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_sl.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(main, "SessionLocal", mock_sl):
            assert main._is_kill_switch_active() is False

    def test_returns_false_when_no_bot_control_row(self):
        import main

        mock_sl = MagicMock()
        mock_db = MagicMock()
        mock_db.get.return_value = None
        mock_sl.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_sl.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(main, "SessionLocal", mock_sl):
            assert main._is_kill_switch_active() is False

    def test_returns_false_when_attribute_missing(self):
        """Older DB rows without kill_switch column → getattr fallback returns False."""
        import main

        ctrl = MagicMock(spec=[])  # spec=[] means no attributes defined
        mock_sl = MagicMock()
        mock_db = MagicMock()
        mock_db.get.return_value = ctrl
        mock_sl.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_sl.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(main, "SessionLocal", mock_sl):
            assert main._is_kill_switch_active() is False
