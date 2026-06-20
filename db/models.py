from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    dollar_amount: Mapped[float] = mapped_column(Float, nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    rsi_at_signal: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    holding_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cost_basis: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class PortfolioSnapshot(Base):
    """Recorded by the bot every cycle. Drives the portfolio P&L chart in the dashboard."""
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    equity: Mapped[float] = mapped_column(Float, nullable=False)      # total account equity
    cash: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    portfolio_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # positions value


class BotStatus(Base):
    """Single-row heartbeat written by the bot each cycle (upsert on id=1)."""
    __tablename__ = "bot_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_cycle_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    token_saved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)


class RuntimeConfig(Base):
    """Live strategy + risk parameters, editable from the dashboard (upsert on id=1)."""
    __tablename__ = "runtime_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False, default="rsi_mean_reversion")
    rsi_period: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    oversold: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    overbought: Mapped[int] = mapped_column(Integer, nullable=False, default=70)
    max_trade_usd: Mapped[float] = mapped_column(Float, nullable=False, default=300.0)
    max_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    daily_loss_limit_usd: Mapped[float] = mapped_column(Float, nullable=False, default=50.0)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class BotControl(Base):
    """Single-row control record written by the dashboard, read by the bot (id=1)."""
    __tablename__ = "bot_control"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    paused_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    portfolio_refresh_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
