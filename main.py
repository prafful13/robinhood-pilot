from __future__ import annotations
"""
Trading bot main loop.
  - Runs every 15 minutes during market hours (9:30–16:00 ET, Mon–Fri)
  - Fetches RSI signals for the watchlist
  - Applies risk checks, then places orders
  - Persists every trade to SQLite
"""

import asyncio
import logging
from datetime import date, datetime

import pytz
import yaml

from broker.robinhood import RobinhoodClient
from db.database import SessionLocal, init_db
from db.models import Trade
from risk.manager import RiskManager
from strategy.rsi import RSIMeanReversion

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
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


async def run_cycle(broker: RobinhoodClient, strategy: RSIMeanReversion, risk: RiskManager, account: str):
    log.info("── running strategy cycle ──")

    positions = await broker.get_positions(account)
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


async def main():
    cfg = load_config()
    init_db()

    account = cfg["account_number"]
    strategy = RSIMeanReversion(cfg)
    risk = RiskManager(cfg)

    log.info("Robinhood RSI trader starting up")
    log.info(f"Watchlist: {cfg['watchlist']}")
    log.info(f"RSI thresholds: buy<{cfg['strategy']['oversold']}  sell>{cfg['strategy']['overbought']}")
    log.info(f"Risk: max ${cfg['risk']['max_trade_usd']}/trade  "
             f"max {cfg['risk']['max_positions']} positions  "
             f"daily loss limit ${cfg['risk']['daily_loss_limit_usd']}")

    async with RobinhoodClient(cfg) as broker:
        while True:
            if is_market_open():
                try:
                    await run_cycle(broker, strategy, risk, account)
                except Exception as e:
                    log.error(f"cycle error: {e}", exc_info=True)
            else:
                now = datetime.now(ET)
                log.info(f"market closed ({now.strftime('%a %H:%M ET')}) — sleeping")

            await asyncio.sleep(CHECK_INTERVAL_SECS)


if __name__ == "__main__":
    asyncio.run(main())
