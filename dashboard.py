"""
Local Streamlit tax & portfolio dashboard.
Run with:  streamlit run dashboard.py
Auto-refreshes every 60 seconds.
"""

import asyncio
from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import yaml

from db.database import SessionLocal, init_db
from db.models import Trade
from tax.calculator import compute as compute_tax

st.set_page_config(page_title="Robinhood Trader", page_icon="📈", layout="wide")

REFRESH_SECS = 60


@st.cache_data(ttl=REFRESH_SECS)
def load_config():
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
        "id": t.id,
        "symbol": t.symbol,
        "side": t.side,
        "quantity": t.quantity,
        "price": t.price,
        "dollar_amount": t.dollar_amount,
        "trade_date": t.trade_date,
        "executed_at": t.executed_at,
        "rsi_at_signal": t.rsi_at_signal,
        "realized_pnl": t.realized_pnl,
        "holding_days": t.holding_days,
        "cost_basis": t.cost_basis,
        "order_id": t.order_id,
    } for t in rows])


@st.cache_data(ttl=REFRESH_SECS)
def load_tax_summary(short_rate: float, long_rate: float):
    return compute_tax(short_rate, long_rate)


# ── Sidebar ────────────────────────────────────────────────────────────────

cfg = load_config()
tax_cfg = cfg["tax"]

st.sidebar.title("Settings")
st.sidebar.markdown(f"**Account:** ••••{cfg['account_number'][-4:]}")
st.sidebar.markdown(f"**Watchlist:** {', '.join(cfg['watchlist'])}")
short_pct = st.sidebar.slider("Short-term tax rate", 10, 50, int(tax_cfg["short_term_rate"] * 100), 1, format="%d%%")
long_pct  = st.sidebar.slider("Long-term tax rate",   0, 25, int(tax_cfg["long_term_rate"]  * 100), 1, format="%d%%")
short_rate = short_pct / 100
long_rate  = long_pct  / 100
st.sidebar.caption(f"Auto-refreshes every {REFRESH_SECS}s")

# ── Header ─────────────────────────────────────────────────────────────────

st.title("📈 Robinhood RSI Trader")
st.caption(f"Last updated: {datetime.now().strftime('%b %d %Y  %H:%M:%S')}")

# ── Tax Summary ────────────────────────────────────────────────────────────

summary = load_tax_summary(short_rate, long_rate)

st.subheader("Tax Obligation (Realized Trades)")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Short-Term Gain", f"${summary.short_term_gain:,.2f}",
          delta=f"{short_rate*100:.0f}% rate", delta_color="off")
c2.metric("Short-Term Tax", f"${summary.short_term_tax:,.2f}")
c3.metric("Long-Term Gain", f"${summary.long_term_gain:,.2f}",
          delta=f"{long_rate*100:.0f}% rate", delta_color="off")
c4.metric("Long-Term Tax", f"${summary.long_term_tax:,.2f}")
c5.metric("Total Estimated Tax", f"${summary.total_tax:,.2f}",
          delta=f"Net gain ${summary.short_term_gain + summary.long_term_gain:,.2f}", delta_color="off")

# Per-symbol tax breakdown
if summary.by_symbol:
    st.markdown("**By Symbol**")
    rows = []
    for sym, gains in summary.by_symbol.items():
        st_g = gains["short_term"]
        lt_g = gains["long_term"]
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
    # Summary metrics
    buys = df[df["side"] == "buy"]
    sells = df[df["side"] == "sell"]
    total_pnl = df["realized_pnl"].sum()

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Trades", len(df))
    m2.metric("Buys", len(buys))
    m3.metric("Sells", len(sells))
    m4.metric("Total Realized P&L", f"${total_pnl:,.2f}",
              delta_color="normal" if total_pnl >= 0 else "inverse")

    # PnL chart over time
    sell_df = df[df["side"] == "sell"].copy()
    if not sell_df.empty:
        sell_df = sell_df.sort_values("executed_at")
        sell_df["cumulative_pnl"] = sell_df["realized_pnl"].cumsum()
        fig = px.line(
            sell_df, x="executed_at", y="cumulative_pnl",
            title="Cumulative Realized P&L",
            labels={"executed_at": "Date", "cumulative_pnl": "P&L ($)"},
            color_discrete_sequence=["#00C805"],
        )
        fig.add_hline(y=0, line_dash="dash", line_color="gray")
        st.plotly_chart(fig, use_container_width=True)

    # Per-symbol P&L bar chart
    if not sell_df.empty:
        sym_pnl = sell_df.groupby("symbol")["realized_pnl"].sum().reset_index()
        sym_pnl.columns = ["Symbol", "Realized P&L"]
        colors = ["#00C805" if v >= 0 else "#FF5000" for v in sym_pnl["Realized P&L"]]
        fig2 = px.bar(sym_pnl, x="Symbol", y="Realized P&L",
                      title="Realized P&L by Symbol",
                      color="Realized P&L",
                      color_continuous_scale=["#FF5000", "#00C805"])
        st.plotly_chart(fig2, use_container_width=True)

    # Raw trade table
    st.markdown("**All Trades**")
    display_cols = ["executed_at", "symbol", "side", "quantity", "price",
                    "dollar_amount", "rsi_at_signal", "realized_pnl", "order_id"]
    available = [c for c in display_cols if c in df.columns]
    st.dataframe(
        df[available].rename(columns={
            "executed_at": "Time",
            "symbol": "Symbol",
            "side": "Side",
            "quantity": "Qty",
            "price": "Price",
            "dollar_amount": "Amount $",
            "rsi_at_signal": "RSI",
            "realized_pnl": "P&L $",
            "order_id": "Order ID",
        }),
        use_container_width=True,
        hide_index=True,
    )

# ── RSI Strategy Reference ─────────────────────────────────────────────────

with st.expander("Strategy Reference"):
    s = cfg["strategy"]
    r = cfg["risk"]
    st.markdown(f"""
| Parameter | Value |
|---|---|
| Indicator | RSI({s['rsi_period']}) on {s['bar_interval']} bars |
| Buy signal | RSI < {s['oversold']} |
| Sell signal | RSI > {s['overbought']} |
| Max trade | ${r['max_trade_usd']} |
| Max positions | {r['max_positions']} |
| Daily loss limit | ${r['daily_loss_limit_usd']} |
""")

# ── Auto-refresh ───────────────────────────────────────────────────────────

st.markdown(
    f"""<meta http-equiv="refresh" content="{REFRESH_SECS}">""",
    unsafe_allow_html=True,
)
