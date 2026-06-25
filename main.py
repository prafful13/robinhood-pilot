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
from db.models import (
    BotControl,
    BotStatus,
    DesiredPosition,
    Position,
    PortfolioSnapshot,
    RuntimeConfig,
    SymbolSnapshot,
    Trade,
)
from risk.manager import RiskManager
from strategy.base import Strategy
from strategy.bollinger import BollingerBands
from strategy.macd import MACDCrossover
from strategy.rsi import RSIMeanReversion
from strategy.rsi_macd import RSIMACDCombo

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
            backupCount=100,  # keep ~100 daily files; 10Gi PVC is the hard cap
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
        db.add(
            Trade(
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
            )
        )
        db.commit()


_ORDER_MAX_RETRIES = 4
_ORDER_BACKOFF_BASE = 5  # doubles each attempt: 5 → 10 → 20 → 40 s
_MAX_CYCLE_RETRIES = 5  # give up after N full 15-min cycle attempts


async def _place_order_with_retry(place_fn, *args) -> dict:
    """Exponential backoff retry within a single cycle. Returns {} on total failure."""
    for attempt in range(1, _ORDER_MAX_RETRIES + 1):
        result = await place_fn(*args)
        if result and "data" in result:
            return result
        if attempt < _ORDER_MAX_RETRIES:
            delay = _ORDER_BACKOFF_BASE * (2 ** (attempt - 1))
            log.warning(
                "  order attempt %d/%d returned no data — retrying in %ds",
                attempt,
                _ORDER_MAX_RETRIES,
                delay,
            )
            await asyncio.sleep(delay)
    return {}


def _sync_desired_state(
    signals: list, positions: list[dict], max_trade_usd: float, now: datetime
) -> None:
    """Convert fresh strategy signals into desired-position entries.

    Rules:
    - Signal for already-held buy / already-gone sell → skip (already achieved)
    - No existing entry for symbol → insert as pending
    - Existing pending/failed same direction → skip (already tracking; reset if failed)
    - Existing pending/failed opposing direction → supersede old, insert new
    """
    held = {p["symbol"] for p in positions if float(p.get("quantity", 0)) > 0}

    with SessionLocal() as db:
        for sig in signals:
            already_achieved = (sig.side == "buy" and sig.symbol in held) or (
                sig.side == "sell" and sig.symbol not in held
            )
            if already_achieved:
                continue

            existing = (
                db.query(DesiredPosition)
                .filter(
                    DesiredPosition.symbol == sig.symbol,
                    DesiredPosition.status.in_(["pending", "failed"]),
                )
                .order_by(DesiredPosition.created_at.desc())
                .first()
            )

            if existing:
                if existing.side == sig.side:
                    if existing.status == "failed":
                        # Signal re-fired — reset and try again
                        existing.status = "pending"
                        existing.retry_count = 0
                        existing.error_msg = None
                        existing.signal_rsi = sig.rsi
                        existing.signal_price = sig.price
                        existing.created_at = now
                        log.info(
                            f"  desired {sig.side.upper()} {sig.symbol} reset (signal re-fired)"
                        )
                    # else: same direction already pending, nothing to do
                    continue
                else:
                    # Opposing signal — supersede old entry
                    existing.status = "superseded"
                    log.info(
                        f"  desired {existing.side.upper()} {sig.symbol} superseded by {sig.side.upper()} signal"
                    )

            db.add(
                DesiredPosition(
                    symbol=sig.symbol,
                    side=sig.side,
                    target_usd=max_trade_usd if sig.side == "buy" else None,
                    signal_rsi=sig.rsi,
                    signal_price=sig.price,
                    created_at=now,
                    status="pending",
                )
            )
            log.info(f"  desired state: {sig.side.upper()} {sig.symbol}  RSI={sig.rsi:.1f}")
        db.commit()


def _update_desired(d_id: int, **kwargs) -> None:
    with SessionLocal() as db:
        d = db.get(DesiredPosition, d_id)
        if d is None:
            return
        for k, v in kwargs.items():
            setattr(d, k, v)
        db.commit()


async def _reconcile(
    broker: RobinhoodClient,
    risk: RiskManager,
    account: str,
    positions: list[dict],
    now: datetime,
) -> None:
    """Try to close the gap between desired positions and actual portfolio."""
    held = {p["symbol"]: p for p in positions if float(p.get("quantity", 0)) > 0}

    with SessionLocal() as db:
        rows = db.query(DesiredPosition).filter(DesiredPosition.status == "pending").all()
        pending = [
            (d.id, d.symbol, d.side, d.target_usd, d.signal_rsi, d.signal_price, d.retry_count)
            for d in rows
        ]

    if not pending:
        return

    log.info(f"  reconciling {len(pending)} desired position(s)")

    for d_id, symbol, side, target_usd, signal_rsi, signal_price, retry_count in pending:
        # Already in desired state?
        if side == "buy" and symbol in held:
            _update_desired(d_id, status="achieved", last_attempted_at=now)
            log.info(f"  ✓ {symbol} already held — marking achieved")
            continue
        if side == "sell" and symbol not in held:
            _update_desired(d_id, status="achieved", last_attempted_at=now)
            log.info(f"  ✓ {symbol} already clear — marking achieved")
            continue

        # Cycle retry limit
        if retry_count >= _MAX_CYCLE_RETRIES:
            _update_desired(
                d_id,
                status="failed",
                last_attempted_at=now,
                error_msg=f"exceeded {_MAX_CYCLE_RETRIES} cycle retries",
            )
            log.error(f"  ✗ desired {side.upper()} {symbol} FAILED — max cycle retries reached")
            continue

        log.info(
            f"  attempting {side.upper()} {symbol}  (cycle {retry_count + 1}/{_MAX_CYCLE_RETRIES})"
        )

        if side == "buy":
            ok, reason = risk.can_buy(symbol, positions)
            if not ok:
                _update_desired(
                    d_id, retry_count=retry_count + 1, last_attempted_at=now, error_msg=reason
                )
                log.info(f"  → risk blocked: {reason}")
                continue

            dollar_str = f"{(target_usd or risk.max_trade_usd):.2f}"
            await broker.review_order(account, symbol, "buy", dollar_str)
            result = await _place_order_with_retry(
                broker.place_buy_order, account, symbol, dollar_str
            )

            if not result:
                msg = "order failed after in-cycle retries"
                _update_desired(
                    d_id, retry_count=retry_count + 1, last_attempted_at=now, error_msg=msg
                )
                log.error(f"  ✗ BUY {symbol} — {msg} (will retry next cycle)")
            else:
                order_id = result.get("data", {}).get("order", {}).get("id")
                amt = target_usd or risk.max_trade_usd
                if signal_price is not None:
                    price = signal_price
                else:
                    quotes = await broker.get_quotes([symbol])
                    price = float(quotes.get(symbol, {}).get("last_trade_price") or 0)
                    if not price:
                        log.error(
                            f"  ✗ BUY {symbol}: signal_price is None and live quote unavailable — trade not recorded"
                        )
                        _update_desired(
                            d_id, status="achieved", last_attempted_at=now, error_msg=None
                        )
                        log.info(f"  ✓ BUY {symbol} achieved  order={order_id}")
                        continue
                record_trade(
                    symbol=symbol,
                    side="buy",
                    quantity=amt / price,
                    price=price,
                    dollar_amount=amt,
                    order_id=order_id,
                    rsi=signal_rsi,
                )
                _update_desired(d_id, status="achieved", last_attempted_at=now, error_msg=None)
                log.info(f"  ✓ BUY {symbol} achieved  order={order_id}")

        elif side == "sell":
            ok, reason = risk.can_sell(symbol, positions)
            if not ok:
                _update_desired(
                    d_id, retry_count=retry_count + 1, last_attempted_at=now, error_msg=reason
                )
                log.info(f"  → risk blocked: {reason}")
                continue

            position = risk.position_for(symbol, positions)
            qty_str = str(float(position["quantity"]))
            avg_cost = float(position.get("average_buy_price", 0))

            await broker.review_sell_order(account, symbol, qty_str)
            result = await _place_order_with_retry(
                broker.place_sell_order, account, symbol, qty_str
            )

            if not result:
                msg = "order failed after in-cycle retries"
                _update_desired(
                    d_id, retry_count=retry_count + 1, last_attempted_at=now, error_msg=msg
                )
                log.error(f"  ✗ SELL {symbol} — {msg} (will retry next cycle)")
            else:
                order_id = result.get("data", {}).get("order", {}).get("id")
                qty = float(position["quantity"])
                if signal_price is not None:
                    price = signal_price
                else:
                    quotes = await broker.get_quotes([symbol])
                    price = float(quotes.get(symbol, {}).get("last_trade_price") or 0)
                    if not price:
                        log.error(
                            f"  ✗ SELL {symbol}: signal_price is None and live quote unavailable — trade not recorded"
                        )
                        _update_desired(
                            d_id, status="achieved", last_attempted_at=now, error_msg=None
                        )
                        log.info(f"  ✓ SELL {symbol} achieved  order={order_id}")
                        continue
                record_trade(
                    symbol=symbol,
                    side="sell",
                    quantity=qty,
                    price=price,
                    dollar_amount=qty * price,
                    order_id=order_id,
                    rsi=signal_rsi,
                    realized_pnl=qty * (price - avg_cost),
                    cost_basis=avg_cost,
                )
                _update_desired(d_id, status="achieved", last_attempted_at=now, error_msg=None)
                log.info(f"  ✓ SELL {symbol} achieved  order={order_id}")


def _record_positions(positions: list[dict], now: datetime) -> None:
    if not positions:
        return
    with SessionLocal() as db:
        for pos in positions:
            qty = float(pos.get("quantity", 0))
            if qty <= 0:
                continue
            symbol = pos.get("symbol", "")
            avg_cost = float(pos.get("average_buy_price", 0))
            current_price = float(pos.get("current_price", 0))
            held_since = None
            if "opened_at" in pos:
                try:
                    held_since = datetime.fromisoformat(
                        pos["opened_at"].replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                except (ValueError, AttributeError):
                    pass
            db.add(
                Position(
                    symbol=symbol,
                    quantity=qty,
                    avg_cost=avg_cost,
                    current_price=current_price,
                    recorded_at=now,
                    held_since=held_since,
                )
            )
        db.commit()


async def run_cycle(
    broker: RobinhoodClient,
    strategy: Strategy,
    risk: RiskManager,
    account: str,
    token_data: dict | None = None,
):
    log.info("── running strategy cycle ──")
    now = datetime.now(ET).replace(tzinfo=None)

    positions = await broker.get_positions(account)
    portfolio = await broker.get_portfolio(account)
    _record_portfolio(portfolio, now)
    _record_positions(positions, now)
    _record_bot_status(token_data, now)

    signals = await strategy.generate_signals(broker)
    _record_symbol_snapshots(getattr(strategy, "last_metrics", {}), now)

    for sig in signals:
        log.info(
            f"  signal: {sig.side.upper()} {sig.symbol}  RSI={sig.rsi:.1f}  price=${sig.price:.2f}"
        )
    if not signals:
        log.info("  no new signals")

    _sync_desired_state(signals, positions, risk.max_trade_usd, now)
    await _reconcile(broker, risk, account, positions, now)


def _apply_runtime_config(cfg: dict) -> dict:
    """Overlay live RuntimeConfig DB values onto config.yaml base config."""
    try:
        with SessionLocal() as db:
            rc = db.get(RuntimeConfig, 1)
        if rc is None:
            return cfg
        return {
            **cfg,
            "_runtime_strategy": rc.strategy,
            "strategy": {
                **cfg["strategy"],
                "rsi_period": rc.rsi_period,
                "oversold": rc.oversold,
                "overbought": rc.overbought,
                "macd_fast": rc.macd_fast,
                "macd_slow": rc.macd_slow,
                "macd_signal_period": rc.macd_signal_period,
                "bb_period": rc.bb_period,
                "bb_std_dev": rc.bb_std_dev,
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


def _make_strategy(cfg: dict) -> Strategy:
    key = cfg.get("_runtime_strategy", "rsi_mean_reversion")
    if key == "macd_crossover":
        return MACDCrossover(cfg)
    if key == "bollinger_bands":
        return BollingerBands(cfg)
    if key == "rsi_macd_combo":
        return RSIMACDCombo(cfg)
    return RSIMeanReversion(cfg)


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
        positions = await broker.get_positions(account)
        _record_positions(positions, now)
        log.info("manual portfolio refresh completed")
    except Exception as e:
        log.warning(f"manual portfolio refresh failed: {e}")


def _record_symbol_snapshots(metrics: dict, now: datetime) -> None:
    if not metrics:
        return
    with SessionLocal() as db:
        for sym, data in metrics.items():
            db.add(
                SymbolSnapshot(
                    symbol=sym,
                    recorded_at=now,
                    rsi=data.get("rsi"),
                    price=data.get("price"),
                    signal=data.get("signal"),
                    macd_hist=data.get("macd_hist"),
                    bb_pct_b=data.get("bb_pct_b"),
                )
            )
        db.commit()


def _record_portfolio(portfolio: dict, now: datetime) -> None:
    # API returns: total_value (full account), equity_value (stock positions), cash
    equity = float(portfolio.get("total_value", 0) or 0)
    cash = float(portfolio.get("cash", 0) or 0)
    port_val = float(portfolio.get("equity_value", 0) or 0)
    with SessionLocal() as db:
        db.add(
            PortfolioSnapshot(
                recorded_at=now,
                equity=equity,
                cash=cash,
                portfolio_value=port_val,
            )
        )
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
            db.add(
                BotStatus(
                    id=1,
                    last_cycle_at=now,
                    token_expires_at=token_expires_at,
                    token_saved_at=token_saved_at,
                    last_error=error,
                )
            )
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
                log.info(
                    f"Received {signal.Signals(s).name} — finishing current cycle then exiting"
                ),
                shutdown.set(),
            ),
        )

    log.info("Robinhood RSI trader starting up")
    log.info(f"Watchlist: {cfg['watchlist']}")
    log.info(
        f"RSI thresholds: buy<{cfg['strategy']['oversold']}  sell>{cfg['strategy']['overbought']}"
    )
    log.info(
        f"Risk: max ${cfg['risk']['max_trade_usd']}/trade  "
        f"max {cfg['risk']['max_positions']} positions  "
        f"daily loss limit ${cfg['risk']['daily_loss_limit_usd']}"
    )

    async with RobinhoodClient(cfg) as broker:
        while not shutdown.is_set():
            _touch_heartbeat()
            now = datetime.now(ET).replace(tzinfo=None)
            token_data = broker.get_token_data()

            # Rebuild strategy and risk each cycle so dashboard config changes take effect immediately
            effective_cfg = _apply_runtime_config(cfg)
            strategy = _make_strategy(effective_cfg)
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
                except TimeoutError:
                    remaining -= 60
                    _touch_heartbeat()
                    await _maybe_refresh_portfolio(broker, account)

    log.info("Bot shut down cleanly")


if __name__ == "__main__":
    asyncio.run(main())
