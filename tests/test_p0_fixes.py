from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestTokenRefreshInCluster:
    """Issue #18 — token refresh must raise in k8s pods, not fall through to PKCE."""

    @pytest.mark.asyncio
    async def test_refresh_failure_raises_in_cluster(self):
        from broker.oauth import get_access_token

        expired_tokens = {
            "access_token": "old",
            "refresh_token": "rt",
            "expires_in": 3600,
            "saved_at": 0,  # epoch → always expired
        }

        with (
            patch("broker.oauth._load_tokens", return_value=expired_tokens),
            patch("broker.oauth._is_expired", return_value=True),
            patch(
                "broker.oauth._discover_token_url", new=AsyncMock(return_value="https://token.url")
            ),
            patch(
                "broker.oauth._refresh_access_token",
                new=AsyncMock(side_effect=Exception("network error")),
            ),
            patch.dict(os.environ, {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}),
        ):
            with pytest.raises(RuntimeError, match="token refresh failed in k8s pod"):
                await get_access_token({"resource": "r", "token_url": "t"})

    @pytest.mark.asyncio
    async def test_refresh_failure_falls_through_locally(self):
        """Local: refresh failure should open browser PKCE flow, not raise."""
        from broker.oauth import get_access_token

        expired_tokens = {
            "access_token": "old",
            "refresh_token": "rt",
            "expires_in": 3600,
            "saved_at": 0,
        }

        pkce_tokens = {
            "access_token": "new",
            "refresh_token": "rt2",
            "expires_in": 3600,
            "saved_at": 9e9,
        }

        with (
            patch("broker.oauth._load_tokens", return_value=expired_tokens),
            patch("broker.oauth._is_expired", return_value=True),
            patch(
                "broker.oauth._discover_token_url", new=AsyncMock(return_value="https://token.url")
            ),
            patch(
                "broker.oauth._refresh_access_token",
                new=AsyncMock(side_effect=Exception("network error")),
            ),
            patch("broker.oauth._generate_pkce", return_value=("verifier", "challenge")),
            patch("broker.oauth.secrets.token_urlsafe", return_value="state123"),
            patch("broker.oauth.webbrowser.open"),
            patch(
                "broker.oauth._run_callback_server",
                new=AsyncMock(return_value=("code", "state123")),
            ),
            patch("broker.oauth._exchange_code", new=AsyncMock(return_value=pkce_tokens)),
            patch("broker.oauth._save_tokens"),
            patch.dict(os.environ, {}, clear=True),  # no KUBERNETES_SERVICE_HOST
        ):
            token = await get_access_token(
                {
                    "resource": "r",
                    "token_url": "t",
                    "auth_url": "https://auth.url",
                    "redirect_uri": "http://localhost:3118/callback",
                    "scope": "trading",
                    "client_id": "test-client",
                }
            )

        assert token == "new"

    @pytest.mark.asyncio
    async def test_valid_token_returned_without_refresh(self):
        from broker.oauth import get_access_token

        valid_tokens = {
            "access_token": "valid",
            "refresh_token": "rt",
            "expires_in": 3600,
            "saved_at": 9e9,  # far future → not expired
        }

        with (
            patch("broker.oauth._load_tokens", return_value=valid_tokens),
            patch("broker.oauth._is_expired", return_value=False),
        ):
            token = await get_access_token({})

        assert token == "valid"


class TestTradePriceGuard:
    """Issue #19 — price=0.0 must never be recorded; live quote fallback required."""

    def _make_broker(self, live_price: float | None = 150.0) -> MagicMock:
        broker = MagicMock()
        if live_price is not None:
            broker.get_quotes = AsyncMock(
                return_value={"AAPL": {"last_trade_price": str(live_price)}}
            )
        else:
            broker.get_quotes = AsyncMock(return_value={})
        broker.review_order = AsyncMock()
        broker.place_buy_order = AsyncMock(return_value={"data": {"order": {"id": "ord-1"}}})
        broker.review_sell_order = AsyncMock()
        broker.place_sell_order = AsyncMock(return_value={"data": {"order": {"id": "ord-2"}}})
        return broker

    def _make_pending(self, signal_price: float | None, side: str = "buy") -> MagicMock:
        from db.models import DesiredPosition

        dp = MagicMock(spec=DesiredPosition)
        dp.id = 1
        dp.symbol = "AAPL"
        dp.side = side
        dp.target_usd = 300.0
        dp.signal_rsi = 28.0
        dp.signal_price = signal_price
        dp.retry_count = 0
        dp.status = "pending"
        return dp

    def _patch_session(self, main_mod: object, dp: MagicMock):
        mock_sl = MagicMock()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [dp]
        mock_sl.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_sl.return_value.__exit__ = MagicMock(return_value=False)
        return mock_sl

    @pytest.mark.asyncio
    async def test_buy_uses_signal_price_when_present(self):
        """signal_price is used directly; no quote fetch needed."""
        import main

        broker = self._make_broker()
        recorded: list[dict] = []
        dp = self._make_pending(150.0)

        with (
            patch.object(main, "SessionLocal", self._patch_session(main, dp)),
            patch.object(main, "_update_desired"),
            patch.object(main, "record_trade", side_effect=lambda **kw: recorded.append(kw)),
            patch.object(
                main,
                "_place_order_with_retry",
                new=AsyncMock(return_value={"data": {"order": {"id": "ord-1"}}}),
            ),
        ):
            risk = MagicMock()
            risk.can_buy.return_value = (True, "")
            risk.max_trade_usd = 300.0
            await main._reconcile(broker, risk, "acc-1", [], MagicMock())

        broker.get_quotes.assert_not_called()
        assert len(recorded) == 1
        assert recorded[0]["price"] == 150.0
        assert recorded[0]["quantity"] == pytest.approx(300.0 / 150.0)

    @pytest.mark.asyncio
    async def test_buy_fetches_live_quote_when_signal_price_none(self):
        import main

        broker = self._make_broker(live_price=148.5)
        recorded: list[dict] = []
        dp = self._make_pending(None)

        with (
            patch.object(main, "SessionLocal", self._patch_session(main, dp)),
            patch.object(main, "_update_desired"),
            patch.object(main, "record_trade", side_effect=lambda **kw: recorded.append(kw)),
            patch.object(
                main,
                "_place_order_with_retry",
                new=AsyncMock(return_value={"data": {"order": {"id": "ord-1"}}}),
            ),
        ):
            risk = MagicMock()
            risk.can_buy.return_value = (True, "")
            risk.max_trade_usd = 300.0
            await main._reconcile(broker, risk, "acc-1", [], MagicMock())

        broker.get_quotes.assert_called_once_with(["AAPL"])
        assert len(recorded) == 1
        assert recorded[0]["price"] == 148.5

    @pytest.mark.asyncio
    async def test_buy_skips_record_when_live_quote_unavailable(self):
        import main

        broker = self._make_broker(live_price=None)
        recorded: list[dict] = []
        updated: list[dict] = []
        dp = self._make_pending(None)

        with (
            patch.object(main, "SessionLocal", self._patch_session(main, dp)),
            patch.object(main, "_update_desired", side_effect=lambda id, **kw: updated.append(kw)),
            patch.object(main, "record_trade", side_effect=lambda **kw: recorded.append(kw)),
            patch.object(
                main,
                "_place_order_with_retry",
                new=AsyncMock(return_value={"data": {"order": {"id": "ord-1"}}}),
            ),
        ):
            risk = MagicMock()
            risk.can_buy.return_value = (True, "")
            risk.max_trade_usd = 300.0
            await main._reconcile(broker, risk, "acc-1", [], MagicMock())

        assert recorded == [], "record_trade must not be called when live price unavailable"
        assert any(u.get("status") == "achieved" for u in updated), (
            "desired position must still be marked achieved"
        )
