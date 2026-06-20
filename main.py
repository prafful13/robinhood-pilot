from __future__ import annotations
"""
Trading bot main loop.
  - Runs every 15 minutes during market hours (9:30–16:00 ET, Mon–Fri)
  - Fetches RSI signals for the watchlist
  - Applies risk checks, then places orders
  - Persists every trade to PostgreSQL
"""

import asyncio
import logging
import os
import signal
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import pytz
import yaml

from broker.robinhood import RobinhoodClient
from db.database import SessionLocal, init_db, init_runtime_config
from db.models import BotControl, BotStatus, PortfolioSnapshot, RuntimeConfig, Trade
from risk.manager import RiskManager
from strategy.rsi import RSIMeanReversion

_LOG_FMT = "%(asctime)s  %(levelname)-7s  %(message)s"
_LOG_DATE = "%Y-%m-%d %H:%M:%S"


def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Stdout — always present so `kubectl logs` works
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter(_LOG_FMT, _LOG_DATE))
    root.addHandler(stream)

    # Daily-rotating file on PVC — active when LOG_DIR is mounted
    log_dir = Path(os.environ.get("LOG_DIR", "/logs"))
    if log_dir.exists():
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            log_dir / "bot.log",
            when="midnight",
            backupCount=100,   # keep ~100 daily files; 10Gi PVC is the hard cap
            encoding="utf-8",
            utc=True,
        )
        file_handler.setFormatter(logging.Formatter(_LOG_FMT, _LOG_DATE))
        root.addHandler(file_handler)
        logging.getLogger(__name__).info(f"File logging active: {log_dir}/bot.log")


_setup_logging()
log = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")
CHECK_INTERVAL_SECS = 900  # 15 minutes


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def is_market_open() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now <= market_close


def record_trade(
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    dollar_amount: float,
    order_id: str | None,
    rsi: float | None,
    realized_pnl: float | None = None,
    holding_days: int | None = None,
    cost_basis: float | None = None,
):
    now = datetime.now(ET)
    with SessionLocal() as db:
        db.add(Trade(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            dollar_amount=dollar_amount,
            trade_date=now.date(),
            executed_at=now,
            order_id=order_id,
            rsi_at_signal=rsi,
            realized_pnl=realized_pnl,
            holding_days=holding_days,
            cost_basis=cost_basis,
        ))
        db.commit()


async def run_cycle(broker: RobinhoodClient, strategy: RSIMeanReversion, risk: RiskManager, account: str, token_data: dict | None = None):
    log.info("── running strategy cycle ──")
    now = datetime.now(ET).replace(tzinfo=None)

    positions = await broker.get_positions(account)
    portfolio = await broker.get_portfolio(account)
    _record_portfolio(portfolio, now)
    _record_bot_status(token_data, now)

    signals = await strategy.generate_signals(broker)

    if not signals:
        log.info("no signals this cycle")
        return

    for sig in signals:
        log.info(f"  signal: {sig.side.upper()} {sig.symbol}  RSI={sig.rsi:.1f}  price=${sig.price:.2f}")

        if sig.side == "buy":
            ok, reason = risk.can_buy(sig.symbol, positions)
            if not ok:
                log.info(f"  → skipped ({reason})")
                continue

            dollar_str = f"{risk.max_trade_usd:.2f}"
            review = await broker.review_order(account, sig.symbol, "buy", dollar_str)
            log.info(f"  review: {review}")

            result = await broker.place_buy_order(account, sig.symbol, dollar_str)
            order_id = result.get("data", {}).get("order", {}).get("id")
            est_qty = risk.max_trade_usd / sig.price

            record_trade(
                symbol=sig.symbol,
                side="buy",
                quantity=est_qty,
                price=sig.price,
                dollar_amount=risk.max_trade_usd,
                order_id=order_id,
                rsi=sig.rsi,
            )
            log.info(f"  ✓ BUY order placed: {sig.symbol}  ${dollar_str}  order={order_id}")

        elif sig.side == "sell":
            ok, reason = risk.can_sell(sig.symbol, positions)
            if not ok:
                log.info(f"  → skipped ({reason})")
                continue

            position = risk.position_for(sig.symbol, positions)
            qty_str = str(float(position["quantity"]))
            avg_cost = float(position.get("average_buy_price", 0))

            review = await broker.review_sell_order(account, sig.symbol, qty_str)
            log.info(f"  review: {review}")

            result = await broker.place_sell_order(account, sig.symbol, qty_str)
            order_id = result.get("data", {}).get("order", {}).get("id")
            qty = float(position["quantity"])
            pnl = qty * (sig.price - avg_cost)

            record_trade(
                symbol=sig.symbol,
                side="sell",
                quantity=qty,
                price=sig.price,
                dollar_amount=qty * sig.price,
                order_id=order_id,
                rsi=sig.rsi,
                realized_pnl=pnl,
                cost_basis=avg_cost,
            )
            log.info(f"  ✓ SELL order placed: {sig.symbol}  qty={qty_str}  PnL=${pnl:.2f}  order={order_id}")


def _apply_runtime_config(cfg: dict) -> dict:
    """Overlay live RuntimeConfig DB values onto config.yaml base config."""
    try:
        with SessionLocal() as db:
            rc = db.get(RuntimeConfig, 1)
        if rc is None:
            return cfg
        return {
            **cfg,
            "strategy": {
                **cfg["strategy"],
                "rsi_period": rc.rsi_period,
                "oversold": rc.oversold,
                "overbought": rc.overbought,
            },
            "risk": {
                **cfg["risk"],
                "max_trade_usd": rc.max_trade_usd,
                "max_positions": rc.max_positions,
                "daily_loss_limit_usd": rc.daily_loss_limit_usd,
            },
        }
    except Exception:
        return cfg


_HEARTBEAT = Path("/tmp/heartbeat")


def _touch_heartbeat() -> None:
    _HEARTBEAT.touch()


def _is_paused() -> bool:
    with SessionLocal() as db:
        ctrl = db.get(BotControl, 1)
        return bool(ctrl and ctrl.paused)


async def _maybe_refresh_portfolio(broker: RobinhoodClient, account: str) -> None:
    """Service a manual portfolio refresh request from the dashboard (within ~60s)."""
    with SessionLocal() as db:
        ctrl = db.get(BotControl, 1)
        if not (ctrl and ctrl.portfolio_refresh_requested):
            return
        ctrl.portfolio_refresh_requested = False
        db.commit()
    try:
        now = datetime.now(ET).replace(tzinfo=None)
        portfolio = await broker.get_portfolio(account)
        _record_portfolio(portfolio, now)
        log.info("manual portfolio refresh completed")
    except Exception as e:
        log.warning(f"manual portfolio refresh failed: {e}")


def _record_portfolio(portfolio: dict, now: datetime) -> None:
    # API returns: total_value (full account), equity_value (stock positions), cash
    equity = float(portfolio.get("total_value", 0) or 0)
    cash = float(portfolio.get("cash", 0) or 0)
    port_val = float(portfolio.get("equity_value", 0) or 0)
    with SessionLocal() as db:
        db.add(PortfolioSnapshot(
            recorded_at=now,
            equity=equity,
            cash=cash,
            portfolio_value=port_val,
        ))
        db.commit()


def _record_bot_status(token_data: dict | None, now: datetime, error: str | None = None) -> None:
    token_expires_at = None
    token_saved_at = None
    if token_data:
        saved = token_data.get("saved_at", 0)
        expires_in = token_data.get("expires_in", 0)
        token_saved_at = datetime.fromtimestamp(saved, tz=ET).replace(tzinfo=None)
        token_expires_at = datetime.fromtimestamp(saved + expires_in, tz=ET).replace(tzinfo=None)
    with SessionLocal() as db:
        existing = db.get(BotStatus, 1)
        if existing:
            existing.last_cycle_at = now
            existing.token_expires_at = token_expires_at
            existing.token_saved_at = token_saved_at
            existing.last_error = error
        else:
            db.add(BotStatus(
                id=1,
                last_cycle_at=now,
                token_expires_at=token_expires_at,
                token_saved_at=token_saved_at,
                last_error=error,
            ))
        db.commit()


async def main():
    cfg = load_config()
    init_db()
    init_runtime_config(cfg)

    account = cfg["account_number"]

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig,
            lambda s=sig: (
                log.info(f"Received {signal.Signals(s).name} — finishing current cycle then exiting"),
                shutdown.set(),
            ),
        )

    log.info("Robinhood RSI trader starting up")
    log.info(f"Watchlist: {cfg['watchlist']}")
    log.info(f"RSI thresholds: buy<{cfg['strategy']['oversold']}  sell>{cfg['strategy']['overbought']}")
    log.info(f"Risk: max ${cfg['risk']['max_trade_usd']}/trade  "
             f"max {cfg['risk']['max_positions']} positions  "
             f"daily loss limit ${cfg['risk']['daily_loss_limit_usd']}")

    async with RobinhoodClient(cfg) as broker:
        while not shutdown.is_set():
            _touch_heartbeat()
            now = datetime.now(ET).replace(tzinfo=None)
            token_data = broker.get_token_data()

            # Rebuild strategy and risk each cycle so dashboard config changes take effect immediately
            effective_cfg = _apply_runtime_config(cfg)
            strategy = RSIMeanReversion(effective_cfg)
            risk = RiskManager(effective_cfg)

            if is_market_open():
                if _is_paused():
                    log.info("bot is paused — skipping trade cycle")
                    _record_bot_status(token_data, now)
                else:
                    try:
                        await run_cycle(broker, strategy, risk, account, token_data)
                    except Exception as e:
                        log.error(f"cycle error: {e}", exc_info=True)
                        _record_bot_status(token_data, now, str(e))
            else:
                log.info(f"market closed ({datetime.now(ET).strftime('%a %H:%M ET')}) — sleeping")
                _record_bot_status(token_data, now)

            # Sleep in 60-second chunks so manual portfolio refresh requests
            # from the dashboard are serviced within ~60 seconds.
            remaining = CHECK_INTERVAL_SECS
            while remaining > 0 and not shutdown.is_set():
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=min(60, remaining))
                    break
                except asyncio.TimeoutError:
                    remaining -= 60
                    await _maybe_refresh_portfolio(broker, account)

    log.info("Bot shut down cleanly")


if __name__ == "__main__":
    asyncio.run(main())
