from __future__ import annotations

import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from db.models import Base, RuntimeConfig


def _get_db_url() -> str:
    # In k8s pods: DB_URL is injected from the postgres-credentials SealedSecret
    if url := os.environ.get("DB_URL"):
        return url
    # Local dev: read password from macOS Keychain; connect via port-forward
    pg_host = os.environ.get("POSTGRES_HOST", "localhost")
    pg_port = os.environ.get("POSTGRES_PORT", "5432")
    try:
        from vault.keychain import get

        password = get("postgres_password") or ""
    except Exception:
        password = ""
    return f"postgresql+psycopg2://trader:{password}@{pg_host}:{pg_port}/robinhood_trader"


ENGINE = create_engine(_get_db_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=ENGINE, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(ENGINE)
    _migrate()


def init_runtime_config(cfg: dict) -> None:
    """Seed RuntimeConfig row from config.yaml defaults if it doesn't exist yet."""
    with SessionLocal() as db:
        if db.get(RuntimeConfig, 1) is not None:
            return
        s, r = cfg["strategy"], cfg["risk"]
        try:
            db.add(
                RuntimeConfig(
                    id=1,
                    strategy="rsi_mean_reversion",
                    rsi_period=s["rsi_period"],
                    oversold=s["oversold"],
                    overbought=s["overbought"],
                    max_trade_usd=r["max_trade_usd"],
                    max_positions=r["max_positions"],
                    daily_loss_limit_usd=r["daily_loss_limit_usd"],
                    macd_fast=s.get("macd_fast", 12),
                    macd_slow=s.get("macd_slow", 26),
                    macd_signal_period=s.get("macd_signal_period", 9),
                    bb_period=s.get("bb_period", 20),
                    bb_std_dev=s.get("bb_std_dev", 2.0),
                    order_max_retries=r.get("order_max_retries", 4),
                    max_cycle_retries=r.get("max_cycle_retries", 5),
                )
            )
            db.commit()
        except Exception:
            db.rollback()  # another process beat us to it; ignore


def _migrate() -> None:
    """Forward-only migrations for tables that already exist in prod."""
    with ENGINE.connect() as conn:
        conn.execute(
            text(
                "ALTER TABLE bot_control "
                "ADD COLUMN IF NOT EXISTS portfolio_refresh_requested BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )
        # Remove snapshots written before the portfolio field-name fix (equity was always 0)
        conn.execute(text("DELETE FROM portfolio_snapshots WHERE equity = 0"))
        # MACD + Bollinger Bands columns added to runtime_config
        for col, coltype, default in [
            ("macd_fast", "INTEGER", "12"),
            ("macd_slow", "INTEGER", "26"),
            ("macd_signal_period", "INTEGER", "9"),
            ("bb_period", "INTEGER", "20"),
            ("bb_std_dev", "FLOAT", "2.0"),
            ("order_max_retries", "INTEGER", "4"),
            ("max_cycle_retries", "INTEGER", "5"),
        ]:
            conn.execute(
                text(
                    f"ALTER TABLE runtime_config "
                    f"ADD COLUMN IF NOT EXISTS {col} {coltype} NOT NULL DEFAULT {default}"
                )
            )
        for col, coltype in [("macd_hist", "FLOAT"), ("bb_pct_b", "FLOAT")]:
            conn.execute(
                text(f"ALTER TABLE symbol_snapshots ADD COLUMN IF NOT EXISTS {col} {coltype}")
            )
        conn.commit()
