from __future__ import annotations

import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from db.models import Base


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


def _migrate() -> None:
    """Forward-only column additions for tables that already exist in prod."""
    with ENGINE.connect() as conn:
        # bot_control.portfolio_refresh_requested added after initial deploy
        conn.execute(text(
            "ALTER TABLE bot_control "
            "ADD COLUMN IF NOT EXISTS portfolio_refresh_requested BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        conn.commit()
