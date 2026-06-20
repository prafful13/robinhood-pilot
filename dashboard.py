from __future__ import annotations

from datetime import datetime, timedelta

import pytz

ET = pytz.timezone("America/New_York")  # bot stores all timestamps in ET


def _to_local(dt: datetime | None) -> datetime | None:
    """Convert an ET-naive datetime to the display timezone (naive)."""
    if dt is None:
        return None
    return ET.localize(dt).astimezone(_LOCAL_TZ).replace(tzinfo=None)

from types import SimpleNamespace

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import yaml

from db.database import SessionLocal, init_db, init_runtime_config
from db.models import BotControl, BotStatus, PortfolioSnapshot, RuntimeConfig, Trade
from tax.calculator import compute as compute_tax

init_db()  # ensure all tables exist (including BotStatus, PortfolioSnapshot)

st.set_page_config(
    page_title="Prafful's Sick of Trading",
    page_icon="📈",
    layout="wide",
    menu_items={},          # hides the hamburger menu (Get Help / Report Bug / About)
)

# Hide the Streamlit Deploy button and hamburger menu — local private app
st.markdown(
    """<style>
    [data-testid="stDeployButton"]  { display: none !important; }
    [data-testid="stMainMenuButton"] { display: none !important; }
    #MainMenu                        { display: none !important; }
    </style>""",
    unsafe_allow_html=True,
)

REFRESH_SECS = 60

PERIOD_OPTIONS = {
    "1D": timedelta(days=1),
    "1W": timedelta(weeks=1),
    "1M": timedelta(days=30),
    "6M": timedelta(days=182),
    "1Y": timedelta(days=365),
    "5Y": timedelta(days=1825),
    "Max": None,
}


@st.cache_data(ttl=REFRESH_SECS)
def load_config() -> dict:
    with open("config.yaml") as f:
        return yaml.safe_load(f)


@st.cache_data(ttl=REFRESH_SECS)
def load_trades() -> pd.DataFrame:
    init_db()
    with SessionLocal() as db:
        rows = db.query(Trade).order_by(Trade.executed_at.desc()).all()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{
        "id": t.id, "symbol": t.symbol, "side": t.side,
        "quantity": t.quantity, "price": t.price,
        "dollar_amount": t.dollar_amount, "trade_date": t.trade_date,
        "executed_at": t.executed_at, "rsi_at_signal": t.rsi_at_signal,
        "realized_pnl": t.realized_pnl, "holding_days": t.holding_days,
        "cost_basis": t.cost_basis, "order_id": t.order_id,
    } for t in rows])


@st.cache_data(ttl=REFRESH_SECS)
def load_portfolio_snapshots() -> pd.DataFrame:
    with SessionLocal() as db:
        rows = db.query(PortfolioSnapshot).order_by(PortfolioSnapshot.recorded_at.asc()).all()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{
        "recorded_at": s.recorded_at,
        "equity": s.equity,
        "cash": s.cash,
        "portfolio_value": s.portfolio_value,
    } for s in rows])


def load_bot_control() -> SimpleNamespace:
    """Not cached — reads current pause state directly each render."""
    with SessionLocal() as db:
        ctrl = db.get(BotControl, 1)
        return SimpleNamespace(paused=bool(ctrl and ctrl.paused), paused_at=ctrl.paused_at if ctrl else None)


def _request_portfolio_refresh() -> None:
    with SessionLocal() as db:
        ctrl = db.get(BotControl, 1)
        if ctrl:
            ctrl.portfolio_refresh_requested = True
        else:
            db.add(BotControl(id=1, paused=False, portfolio_refresh_requested=True))
        db.commit()


def _set_paused(paused: bool) -> None:
    now_et = datetime.now(ET).replace(tzinfo=None)
    with SessionLocal() as db:
        ctrl = db.get(BotControl, 1)
        if ctrl:
            ctrl.paused = paused
            ctrl.paused_at = now_et if paused else None
        else:
            db.add(BotControl(id=1, paused=paused, paused_at=now_et if paused else None))
        db.commit()


@st.cache_data(ttl=REFRESH_SECS)
def load_bot_status() -> SimpleNamespace | None:
    with SessionLocal() as db:
        s = db.get(BotStatus, 1)
        if s is None:
            return None
        return SimpleNamespace(
            last_cycle_at=s.last_cycle_at,
            token_expires_at=s.token_expires_at,
            token_saved_at=s.token_saved_at,
            last_error=s.last_error,
        )


@st.cache_data(ttl=REFRESH_SECS)
def load_tax_summary(short_rate: float, long_rate: float):
    return compute_tax(short_rate, long_rate)


def load_runtime_config() -> SimpleNamespace | None:
    """Not cached — reads live DB values so form shows current config."""
    with SessionLocal() as db:
        rc = db.get(RuntimeConfig, 1)
    if rc is None:
        return None
    return SimpleNamespace(
        strategy=rc.strategy, rsi_period=rc.rsi_period,
        oversold=rc.oversold, overbought=rc.overbought,
        macd_fast=rc.macd_fast, macd_slow=rc.macd_slow, macd_signal_period=rc.macd_signal_period,
        bb_period=rc.bb_period, bb_std_dev=rc.bb_std_dev,
        max_trade_usd=rc.max_trade_usd, max_positions=rc.max_positions,
        daily_loss_limit_usd=rc.daily_loss_limit_usd, updated_at=rc.updated_at,
    )


STRATEGY_OPTIONS = {
    "RSI Mean-Reversion":  "rsi_mean_reversion",
    "MACD Crossover":      "macd_crossover",
    "Bollinger Bands":     "bollinger_bands",
    "RSI + MACD Combo":    "rsi_macd_combo",
}
_STRATEGY_KEYS = list(STRATEGY_OPTIONS.keys())
_STRATEGY_VALS = list(STRATEGY_OPTIONS.values())


def _save_runtime_config(
    strategy: str, rsi_period: int, oversold: int, overbought: int,
    macd_fast: int, macd_slow: int, macd_signal_period: int,
    bb_period: int, bb_std_dev: float,
    max_trade_usd: float, max_positions: int, daily_loss_limit_usd: float,
) -> None:
    now_et = datetime.now(ET).replace(tzinfo=None)
    with SessionLocal() as db:
        rc = db.get(RuntimeConfig, 1)
        if rc:
            rc.strategy = strategy
            rc.rsi_period = rsi_period
            rc.oversold = oversold
            rc.overbought = overbought
            rc.macd_fast = macd_fast
            rc.macd_slow = macd_slow
            rc.macd_signal_period = macd_signal_period
            rc.bb_period = bb_period
            rc.bb_std_dev = bb_std_dev
            rc.max_trade_usd = max_trade_usd
            rc.max_positions = max_positions
            rc.daily_loss_limit_usd = daily_loss_limit_usd
            rc.updated_at = now_et
        else:
            db.add(RuntimeConfig(
                id=1, strategy=strategy, rsi_period=rsi_period,
                oversold=oversold, overbought=overbought,
                macd_fast=macd_fast, macd_slow=macd_slow, macd_signal_period=macd_signal_period,
                bb_period=bb_period, bb_std_dev=bb_std_dev,
                max_trade_usd=max_trade_usd, max_positions=max_positions,
                daily_loss_limit_usd=daily_loss_limit_usd, updated_at=now_et,
            ))
        db.commit()


# ── Sidebar ────────────────────────────────────────────────────────────────

cfg = load_config()
init_runtime_config(cfg)  # seed DB defaults from config.yaml on first run
tax_cfg = cfg["tax"]
_LOCAL_TZ = pytz.timezone(cfg.get("display_timezone", "America/New_York"))
_TZ_ABBR = datetime.now(_LOCAL_TZ).strftime("%Z")

st.sidebar.title("⚙ Settings")
st.sidebar.markdown(f"**Account:** ••••{cfg['account_number'][-4:]}")
st.sidebar.markdown(f"**Watchlist:** {', '.join(cfg['watchlist'])}")

st.sidebar.divider()
st.sidebar.markdown("##### Strategy & Risk")

rc = load_runtime_config()
s_def, r_def = cfg["strategy"], cfg["risk"]

current_strategy_key = rc.strategy if rc else "rsi_mean_reversion"
current_strategy_idx = _STRATEGY_VALS.index(current_strategy_key) if current_strategy_key in _STRATEGY_VALS else 0

with st.sidebar:
    with st.form("runtime_config_form"):
        strategy_label = st.selectbox("Strategy", _STRATEGY_KEYS, index=current_strategy_idx)

        st.markdown("**RSI** *(RSI, RSI+MACD)*")
        rsi_period = st.number_input(
            "Period", min_value=2, max_value=50, step=1,
            value=rc.rsi_period if rc else s_def["rsi_period"],
        )
        ov_col, ob_col = st.columns(2)
        oversold   = ov_col.number_input(
            "Buy <", min_value=1, max_value=49, step=1,
            value=rc.oversold if rc else s_def["oversold"],
        )
        overbought = ob_col.number_input(
            "Sell >", min_value=51, max_value=99, step=1,
            value=rc.overbought if rc else s_def["overbought"],
        )

        st.markdown("**MACD** *(MACD, RSI+MACD)*")
        mf_col, ms_col, msig_col = st.columns(3)
        macd_fast = mf_col.number_input(
            "Fast", min_value=2, max_value=50, step=1,
            value=rc.macd_fast if rc else s_def.get("macd_fast", 12),
        )
        macd_slow = ms_col.number_input(
            "Slow", min_value=3, max_value=200, step=1,
            value=rc.macd_slow if rc else s_def.get("macd_slow", 26),
        )
        macd_sig = msig_col.number_input(
            "Signal", min_value=2, max_value=50, step=1,
            value=rc.macd_signal_period if rc else s_def.get("macd_signal_period", 9),
        )

        st.markdown("**Bollinger Bands** *(BB)*")
        bb_p_col, bb_s_col = st.columns(2)
        bb_period = bb_p_col.number_input(
            "Period", min_value=5, max_value=100, step=1,
            value=rc.bb_period if rc else s_def.get("bb_period", 20),
        )
        bb_std = bb_s_col.number_input(
            "Std devs", min_value=0.5, max_value=5.0, step=0.5, format="%.1f",
            value=float(rc.bb_std_dev if rc else s_def.get("bb_std_dev", 2.0)),
        )

        st.markdown("**Risk Limits**")
        max_trade = st.number_input(
            "Max trade ($)", min_value=1.0, step=10.0, format="%.0f",
            value=float(rc.max_trade_usd if rc else r_def["max_trade_usd"]),
        )
        mp_col, dl_col = st.columns(2)
        max_pos = mp_col.number_input(
            "Max pos", min_value=1, max_value=20, step=1,
            value=rc.max_positions if rc else r_def["max_positions"],
        )
        daily_loss = dl_col.number_input(
            "Daily loss ($)", min_value=0.0, step=5.0, format="%.0f",
            value=float(rc.daily_loss_limit_usd if rc else r_def["daily_loss_limit_usd"]),
        )

        submitted = st.form_submit_button("Apply Config", type="primary", use_container_width=True)

if submitted:
    _save_runtime_config(
        strategy=STRATEGY_OPTIONS[strategy_label],
        rsi_period=int(rsi_period), oversold=int(oversold), overbought=int(overbought),
        macd_fast=int(macd_fast), macd_slow=int(macd_slow), macd_signal_period=int(macd_sig),
        bb_period=int(bb_period), bb_std_dev=float(bb_std),
        max_trade_usd=float(max_trade), max_positions=int(max_pos),
        daily_loss_limit_usd=float(daily_loss),
    )
    st.cache_data.clear()
    st.rerun()

if rc and rc.updated_at:
    st.sidebar.caption(f"Config saved: {_to_local(rc.updated_at).strftime(f'%b %d %H:%M {_TZ_ABBR}')}")

st.sidebar.divider()
st.sidebar.markdown("##### Tax Rates")
short_pct = st.sidebar.slider("Short-term", 10, 50, int(tax_cfg["short_term_rate"] * 100), 1, format="%d%%")
long_pct  = st.sidebar.slider("Long-term",   0, 25, int(tax_cfg["long_term_rate"]  * 100), 1, format="%d%%")
short_rate = short_pct / 100
long_rate  = long_pct  / 100

st.sidebar.divider()
with st.sidebar.expander("Strategy Reference"):
    st.markdown("""
All strategies run on 15-min bars, 7-day lookback, Mon–Fri 9:30–16:00 ET.
Parameters above are live — bot picks them up on the next cycle.

**RSI Mean-Reversion**
Buy when RSI < oversold; sell when RSI > overbought. Wilder's EWM.

**MACD Crossover**
Buy when MACD histogram flips positive (bullish crossover); sell when it flips negative. MACD = EMA(fast) − EMA(slow), signal = EMA(MACD, signal).

**Bollinger Bands**
Buy when price closes below the lower band (middle − N·σ); sell when above upper band. Mean-reversion on price vs. rolling volatility.

**RSI + MACD Combo**
Fires RSI signals only when MACD histogram confirms direction. Fewer trades, fewer false positives.
""")

st.sidebar.caption(f"Auto-refreshes every {REFRESH_SECS}s")

# ── Header ─────────────────────────────────────────────────────────────────

st.title("📈 Prafful's Sick of Trading")
st.caption(f"Last updated: {datetime.now(_LOCAL_TZ).strftime(f'%b %d %Y  %H:%M:%S {_TZ_ABBR}')}")

# ── Bot Status + Token Validity ────────────────────────────────────────────

ctrl = load_bot_control()
hdr_col, btn_col = st.columns([5, 1])
hdr_col.subheader("Bot Status")
with btn_col:
    if ctrl.paused:
        if st.button("▶ Resume", type="primary", use_container_width=True):
            _set_paused(False)
            st.cache_data.clear()
            st.rerun()
    else:
        if st.button("⏹ Pause", type="secondary", use_container_width=True):
            _set_paused(True)
            st.cache_data.clear()
            st.rerun()

if ctrl.paused:
    paused_str = f" since {_to_local(ctrl.paused_at).strftime(f'%b %d %H:%M {_TZ_ABBR}')}" if ctrl.paused_at else ""
    st.warning(f"Trading is **PAUSED**{paused_str}. The bot is running but will not place any orders. Click **Resume** to re-enable.")

status = load_bot_status()
# Age comparisons: DB timestamps are ET-naive, so compare against ET now
now_et = datetime.now(ET).replace(tzinfo=None)

s1, s2, s3, s4 = st.columns(4)

if status and status.last_cycle_at:
    age_mins = (now_et - status.last_cycle_at).total_seconds() / 60
    cycle_label = f"{age_mins:.0f}m ago" if age_mins < 60 else f"{age_mins/60:.1f}h ago"
    cycle_ok = age_mins < 20
    s1.metric("Last Cycle", cycle_label, delta="live" if cycle_ok else "stale", delta_color="normal" if cycle_ok else "inverse")
else:
    s1.metric("Last Cycle", "No data", delta="bot not running?", delta_color="inverse")

if status and status.token_expires_at:
    time_left = status.token_expires_at - now_et
    days_left = time_left.total_seconds() / 86400
    if days_left > 1:
        token_label = f"{days_left:.1f}d"
    elif days_left > 0:
        token_label = f"{time_left.total_seconds()/3600:.1f}h"
    else:
        token_label = "EXPIRED"
    token_ok = days_left > 1
    s2.metric("Token Expires In", token_label,
              delta="valid" if token_ok else "needs refresh",
              delta_color="normal" if token_ok else "inverse")
    if status.token_saved_at:
        issued_local = _to_local(status.token_saved_at)
        s3.metric("Token Issued", issued_local.strftime(f"%b %d %H:%M {_TZ_ABBR}"))
else:
    s2.metric("Token Expires In", "Unknown")
    s3.metric("Token Issued", "Unknown")

if status and status.last_error:
    s4.metric("Last Error", "⚠ Error", delta=status.last_error[:40], delta_color="inverse")
else:
    s4.metric("Last Error", "None", delta="healthy", delta_color="normal")

if status and status.token_expires_at:
    days_left = (status.token_expires_at - now_et).total_seconds() / 86400
    if days_left < 1:
        st.error(f"⚠ OAuth token expired or expiring soon ({days_left:.1f} days). Run: `uv run inv auth && uv run inv k8s-seal && kubectl apply -f k8s/sealed/ && uv run inv k8s-restart`")
    elif days_left < 3:
        st.warning(f"OAuth token expires in {days_left:.1f} days. Consider re-authenticating soon.")

st.divider()

# ── Portfolio Value & P&L ──────────────────────────────────────────────────

pv_hdr, pv_btn = st.columns([5, 1])
pv_hdr.subheader("Portfolio Value")
with pv_btn:
    if st.button("⟳ Refresh", type="secondary", use_container_width=True):
        _request_portfolio_refresh()
        st.session_state["portfolio_refresh_at"] = datetime.now(ET)
        st.cache_data.clear()
        st.rerun()

if "portfolio_refresh_at" in st.session_state:
    elapsed = (datetime.now(ET) - st.session_state["portfolio_refresh_at"]).total_seconds()
    if elapsed < 90:
        st.info(f"Portfolio refresh requested — data will update within ~60 seconds ({elapsed:.0f}s elapsed).")
    else:
        del st.session_state["portfolio_refresh_at"]

snapshots = load_portfolio_snapshots()

if not snapshots.empty:
    # Period selector
    period_cols = st.columns(len(PERIOD_OPTIONS))
    selected_period = st.session_state.get("period", "1M")
    for i, (label, _) in enumerate(PERIOD_OPTIONS.items()):
        if period_cols[i].button(label, type="primary" if label == selected_period else "secondary", use_container_width=True):
            st.session_state["period"] = label
            selected_period = label

    # Convert ET-naive timestamps to local timezone for display and filtering
    snapshots["recorded_at"] = snapshots["recorded_at"].apply(_to_local)
    now_local = datetime.now(_LOCAL_TZ).replace(tzinfo=None)

    delta = PERIOD_OPTIONS[selected_period]
    if delta:
        cutoff = now_local - delta
        df_period = snapshots[snapshots["recorded_at"] >= cutoff].copy()
    else:
        df_period = snapshots.copy()

    if not df_period.empty:
        first_equity = df_period["equity"].iloc[0]
        last_equity = df_period["equity"].iloc[-1]
        pnl = last_equity - first_equity
        pnl_pct = (pnl / first_equity * 100) if first_equity else 0

        pv1, pv2, pv3 = st.columns(3)
        pv1.metric("Current Equity", f"${last_equity:,.2f}")
        pv2.metric(f"P&L ({selected_period})", f"${pnl:+,.2f}", delta=f"{pnl_pct:+.2f}%",
                   delta_color="normal" if pnl >= 0 else "inverse")
        if not df_period["portfolio_value"].isna().all():
            pv3.metric("Positions Value", f"${df_period['portfolio_value'].iloc[-1]:,.2f}")

        # Equity chart
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=df_period["recorded_at"], y=df_period["equity"],
            mode="lines", name="Equity",
            line=dict(color="#00C805", width=2),
            fill="tozeroy", fillcolor="rgba(0,200,5,0.08)",
        ))
        if not df_period["cash"].isna().all():
            fig.add_trace(go.Scatter(
                x=df_period["recorded_at"], y=df_period["cash"],
                mode="lines", name="Cash",
                line=dict(color="#636EFA", width=1, dash="dot"),
            ))
        fig.update_layout(
            title=f"Account Equity — {selected_period}",
            xaxis_title="", yaxis_title="USD",
            hovermode="x unified", legend=dict(orientation="h"),
            margin=dict(l=0, r=0, t=40, b=0),
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info(f"No portfolio data in the selected period ({selected_period}).")
else:
    st.info("No portfolio snapshots yet. Click **⟳ Refresh** to fetch the current balance, or wait for the next market-hours cycle.")

st.divider()

# ── Tax Summary ────────────────────────────────────────────────────────────

st.subheader("Tax Obligation (Realized Trades)")

summary = load_tax_summary(short_rate, long_rate)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Short-Term Gain", f"${summary.short_term_gain:,.2f}",
          delta=f"{short_rate*100:.0f}% rate", delta_color="off")
c2.metric("Short-Term Tax", f"${summary.short_term_tax:,.2f}")
c3.metric("Long-Term Gain", f"${summary.long_term_gain:,.2f}",
          delta=f"{long_rate*100:.0f}% rate", delta_color="off")
c4.metric("Long-Term Tax", f"${summary.long_term_tax:,.2f}")
c5.metric("Total Estimated Tax", f"${summary.total_tax:,.2f}",
          delta=f"Net gain ${summary.short_term_gain + summary.long_term_gain:,.2f}", delta_color="off")

if summary.by_symbol:
    rows = []
    for sym, gains in summary.by_symbol.items():
        st_g, lt_g = gains["short_term"], gains["long_term"]
        rows.append({
            "Symbol": sym,
            "Short-Term Gain": f"${st_g:,.2f}",
            "ST Tax": f"${st_g * short_rate:,.2f}",
            "Long-Term Gain": f"${lt_g:,.2f}",
            "LT Tax": f"${lt_g * long_rate:,.2f}",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.divider()

# ── Trade History ──────────────────────────────────────────────────────────

st.subheader("Trade History")
df = load_trades()

if df.empty:
    st.info("No trades recorded yet. The bot is running — trades will appear after market open (Mon–Fri 9:30–16:00 ET).")
else:
    buys  = df[df["side"] == "buy"]
    sells = df[df["side"] == "sell"]
    total_pnl = df["realized_pnl"].sum()

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Trades", len(df))
    m2.metric("Buys", len(buys))
    m3.metric("Sells", len(sells))
    m4.metric("Total Realized P&L", f"${total_pnl:,.2f}",
              delta_color="normal" if total_pnl >= 0 else "inverse")

    sell_df = df[df["side"] == "sell"].copy()
    if not sell_df.empty:
        sell_df = sell_df.sort_values("executed_at")
        sell_df["cumulative_pnl"] = sell_df["realized_pnl"].cumsum()
        fig = px.line(sell_df, x="executed_at", y="cumulative_pnl",
                      title="Cumulative Realized P&L",
                      labels={"executed_at": "Date", "cumulative_pnl": "P&L ($)"},
                      color_discrete_sequence=["#00C805"])
        fig.add_hline(y=0, line_dash="dash", line_color="gray")
        st.plotly_chart(fig, use_container_width=True)

        sym_pnl = sell_df.groupby("symbol")["realized_pnl"].sum().reset_index()
        sym_pnl.columns = ["Symbol", "Realized P&L"]
        fig2 = px.bar(sym_pnl, x="Symbol", y="Realized P&L", title="Realized P&L by Symbol",
                      color="Realized P&L", color_continuous_scale=["#FF5000", "#00C805"])
        st.plotly_chart(fig2, use_container_width=True)

    display_cols = ["executed_at", "symbol", "side", "quantity", "price",
                    "dollar_amount", "rsi_at_signal", "realized_pnl", "order_id"]
    available = [c for c in display_cols if c in df.columns]
    st.dataframe(
        df[available].rename(columns={
            "executed_at": "Time", "symbol": "Symbol", "side": "Side",
            "quantity": "Qty", "price": "Price", "dollar_amount": "Amount $",
            "rsi_at_signal": "RSI", "realized_pnl": "P&L $", "order_id": "Order ID",
        }),
        use_container_width=True, hide_index=True,
    )

# ── Auto-refresh ───────────────────────────────────────────────────────────

st.markdown(
    f"""<meta http-equiv="refresh" content="{REFRESH_SECS}">""",
    unsafe_allow_html=True,
)
