import sqlite3
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "nepse_portfolio.db"
API_URL = "https://shubhamnpk.github.io/yonepse/data/nepse_data.json"
TIMEOUT = 15

st.set_page_config(page_title="NEPSE Portfolio Tracker", page_icon="📈", layout="wide")

st.markdown("""
<style>
.stApp {background: #0b1220; color: #e8eef9;}
[data-testid="stMetric"] {background: #121d30; padding: 16px; border-radius: 14px; border: 1px solid #26354d;}
div[data-testid="stSidebar"] {background: #101a2b;}
h1, h2, h3 {color: #f2f6ff;}
.small-muted {color: #9eacc2; font-size: 0.9rem;}
</style>
""", unsafe_allow_html=True)


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            price REAL NOT NULL CHECK(price >= 0),
            fees REAL NOT NULL DEFAULT 0,
            note TEXT DEFAULT ''
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS manual_prices (
            symbol TEXT PRIMARY KEY,
            price REAL NOT NULL,
            updated_at TEXT NOT NULL
        )""")


def load_transactions():
    with db() as con:
        return pd.read_sql_query(
            "SELECT id, trade_date, symbol, side, quantity, price, fees, note FROM transactions ORDER BY trade_date DESC, id DESC",
            con
        )


def save_transaction(trade_date, symbol, side, quantity, price, fees, note):
    with db() as con:
        con.execute(
            "INSERT INTO transactions(trade_date,symbol,side,quantity,price,fees,note) VALUES(?,?,?,?,?,?,?)",
            (str(trade_date), symbol.strip().upper(), side, int(quantity), float(price), float(fees), note.strip())
        )


def delete_transaction(tx_id):
    with db() as con:
        con.execute("DELETE FROM transactions WHERE id=?", (int(tx_id),))


def load_manual_prices():
    with db() as con:
        return pd.read_sql_query("SELECT symbol, price, updated_at FROM manual_prices ORDER BY symbol", con)


def save_manual_price(symbol, price):
    with db() as con:
        con.execute("""
            INSERT INTO manual_prices(symbol,price,updated_at) VALUES(?,?,?)
            ON CONFLICT(symbol) DO UPDATE SET price=excluded.price, updated_at=excluded.updated_at
        """, (symbol.strip().upper(), float(price), datetime.now().isoformat(timespec="minutes")))


def get_api_data():
    try:
        response = requests.get(API_URL, timeout=TIMEOUT, headers={"User-Agent": "NEPSE-Portfolio-Tracker/1.0"})
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            for key in ("data", "stocks", "results", "items"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break
        if not isinstance(payload, list):
            return pd.DataFrame(), "Unexpected API response format."
        df = pd.json_normalize(payload)
        if df.empty:
            return df, "API returned no rows."

        # Public dataset field names can evolve; detect common names.
        cols = {str(c).lower().replace("_", "").replace(" ", ""): c for c in df.columns}
        def find(*names):
            for name in names:
                key = name.lower().replace("_", "").replace(" ", "")
                if key in cols:
                    return cols[key]
            return None

        sym_col = find("symbol", "stockSymbol", "ticker", "securitySymbol")
        price_col = find("ltp", "lastTradedPrice", "price", "closePrice", "closingPrice", "closing")
        if not sym_col or not price_col:
            return df, "Could not detect symbol/price fields in the API response. Use manual prices below."
        out = pd.DataFrame()
        out["symbol"] = df[sym_col].astype(str).str.upper().str.strip()
        out["ltp"] = pd.to_numeric(df[price_col], errors="coerce")
        change_col = find("changePercent", "percentageChange", "changePercentage", "percentChange")
        if change_col:
            out["change_percent"] = pd.to_numeric(df[change_col], errors="coerce")
        else:
            out["change_percent"] = pd.NA
        name_col = find("companyName", "name", "securityName", "company")
        out["company"] = df[name_col].astype(str) if name_col else out["symbol"]
        out = out.dropna(subset=["symbol", "ltp"]).drop_duplicates("symbol")
        return out, None
    except requests.RequestException as exc:
        return pd.DataFrame(), f"Could not reach market API: {exc}"
    except (ValueError, TypeError) as exc:
        return pd.DataFrame(), f"Could not read API data: {exc}"


def money(value):
    return f"NPR {value:,.2f}"


def build_positions(tx, price_map):
    if tx.empty:
        return pd.DataFrame(columns=["symbol", "quantity", "avg_cost", "cost_value", "ltp", "market_value", "pnl", "return_pct"])
    positions = []
    for symbol, group in tx.groupby("symbol"):
        qty = 0
        cost_basis = 0.0
        realized = 0.0
        for row in group.sort_values(["trade_date", "id"]).itertuples():
            q, p, fees = int(row.quantity), float(row.price), float(row.fees or 0)
            if row.side == "BUY":
                cost_basis += q * p + fees
                qty += q
            else:
                if q > qty:
                    # Keep history visible but don't permit a negative holding to distort average cost.
                    sold_qty = qty
                else:
                    sold_qty = q
                avg = (cost_basis / qty) if qty else 0.0
                realized += sold_qty * (p - avg) - fees
                cost_basis -= avg * sold_qty
                qty -= sold_qty
                if qty <= 0:
                    qty = 0
                    cost_basis = 0.0
        if qty <= 0:
            continue
        avg_cost = cost_basis / qty if qty else 0.0
        ltp = price_map.get(symbol)
        market_value = float(ltp) * qty if ltp is not None and pd.notna(ltp) else None
        pnl = market_value - cost_basis if market_value is not None else None
        return_pct = (pnl / cost_basis * 100) if pnl is not None and cost_basis else None
        positions.append({
            "symbol": symbol, "quantity": qty, "avg_cost": avg_cost, "cost_value": cost_basis,
            "ltp": ltp, "market_value": market_value, "pnl": pnl, "return_pct": return_pct
        })
    return pd.DataFrame(positions)


init_db()
st.title("📈 NEPSE Portfolio Tracker")
st.caption("Python • Public market-data API • SQLite • Streamlit UI")
st.markdown('<p class="small-muted">Personal portfolio monitoring only. Prices may be delayed or unavailable; verify figures with your broker before acting.</p>', unsafe_allow_html=True)

with st.sidebar:
    st.header("⚙️ Market data")
    refresh = st.button("🔄 Refresh API prices", use_container_width=True)
    st.caption("Data source: YONEPSE public JSON dataset")
    st.link_button("Open API documentation", "https://shubhamnpk.github.io/yonepse/pages/docs.html", use_container_width=True)
    st.divider()
    st.header("Quick guide")
    st.write("1. Add each BUY/SELL trade in Transactions.")
    st.write("2. Refresh market prices.")
    st.write("3. If a symbol is missing, enter its price under Manual prices.")
    st.caption("Your trade records are stored locally in nepse_portfolio.db.")

if "market_data" not in st.session_state or refresh:
    with st.spinner("Fetching latest available NEPSE data..."):
        market_df, market_error = get_api_data()
    st.session_state["market_data"] = market_df
    st.session_state["market_error"] = market_error
market_df = st.session_state.get("market_data", pd.DataFrame())
market_error = st.session_state.get("market_error")

manual_df = load_manual_prices()
price_map = {}
if not market_df.empty:
    price_map.update(dict(zip(market_df["symbol"], market_df["ltp"])))
if not manual_df.empty:
    # API prices take precedence; manual prices fill missing symbols only.
    for row in manual_df.itertuples():
        price_map.setdefault(row.symbol, row.price)

tx = load_transactions()
positions = build_positions(tx, price_map)
total_cost = float(positions["cost_value"].sum()) if not positions.empty else 0.0
known_value = float(positions["market_value"].dropna().sum()) if not positions.empty else 0.0
known_cost = float(positions.loc[positions["market_value"].notna(), "cost_value"].sum()) if not positions.empty else 0.0
unpriced_count = int(positions["ltp"].isna().sum()) if not positions.empty else 0
total_pnl = known_value - known_cost
total_return = total_pnl / known_cost * 100 if known_cost else 0.0

m1, m2, m3, m4 = st.columns(4)
m1.metric("Invested cost (open holdings)", money(total_cost))
m2.metric("Market value (priced holdings)", money(known_value))
m3.metric("Unrealized P/L (priced holdings)", money(total_pnl), f"{total_return:.2f}%")
m4.metric("Unpriced holdings", str(unpriced_count))

if market_error:
    st.warning(f"Market API status: {market_error} You can still use the tracker by adding manual prices.")
elif not market_df.empty:
    st.success(f"Market data loaded: {len(market_df)} symbols. Check the API source for its latest update time.")
else:
    st.info("No market prices loaded yet. Add manual prices or refresh the API.")

tab1, tab2, tab3, tab4 = st.tabs(["📊 Portfolio", "➕ Transactions", "💹 Market watch", "🛠️ Manual prices"])

with tab1:
    st.subheader("Your holdings")
    if positions.empty:
        st.info("No open holdings yet. Add a BUY transaction in the Transactions tab.")
    else:
        view = positions.copy()
        for col in ("avg_cost", "cost_value", "ltp", "market_value", "pnl", "return_pct"):
            view[col] = pd.to_numeric(view[col], errors="coerce")
        st.dataframe(
            view.rename(columns={
                "symbol":"Symbol", "quantity":"Quantity", "avg_cost":"Average cost",
                "cost_value":"Cost value (NPR)", "ltp":"Latest price (NPR)",
                "market_value":"Market value (NPR)", "pnl":"Unrealized P/L (NPR)", "return_pct":"Return (%)"
            }),
            use_container_width=True, hide_index=True,
            column_config={
                "Average cost": st.column_config.NumberColumn(format="%.2f"),
                "Cost value (NPR)": st.column_config.NumberColumn(format="%.2f"),
                "Latest price (NPR)": st.column_config.NumberColumn(format="%.2f"),
                "Market value (NPR)": st.column_config.NumberColumn(format="%.2f"),
                "Unrealized P/L (NPR)": st.column_config.NumberColumn(format="%.2f"),
                "Return (%)": st.column_config.NumberColumn(format="%.2f%%")
            }
        )
        chart_data = positions.dropna(subset=["market_value"])
        if not chart_data.empty:
            left, right = st.columns(2)
            with left:
                st.markdown("#### Allocation by market value")
                st.bar_chart(chart_data.set_index("symbol")[["market_value"]])
            with right:
                st.markdown("#### Unrealized P/L by stock")
                pnl_chart = chart_data.set_index("symbol")[["pnl"]]
                st.bar_chart(pnl_chart)
    st.subheader("Transaction history")
    if tx.empty:
        st.caption("No transactions recorded.")
    else:
        st.dataframe(tx, use_container_width=True, hide_index=True)
        csv = tx.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Export transactions CSV", csv, "nepse_transactions.csv", "text/csv")

with tab2:
    st.subheader("Record a trade")
    with st.form("trade_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        trade_date = c1.date_input("Trade date", value=date.today())
        symbol = c2.text_input("Stock symbol", placeholder="e.g. NABIL").upper().strip()
        side = c3.selectbox("Transaction", ["BUY", "SELL"])
        c4, c5, c6 = st.columns(3)
        quantity = c4.number_input("Quantity (shares)", min_value=1, value=10, step=1)
        price = c5.number_input("Price per share (NPR)", min_value=0.0, value=500.0, step=1.0)
        fees = c6.number_input("Total fees (NPR)", min_value=0.0, value=0.0, step=1.0)
        note = st.text_input("Note (optional)", placeholder="Broker / purpose")
        submitted = st.form_submit_button("Save transaction", type="primary")
        if submitted:
            if not symbol:
                st.error("Enter a stock symbol.")
            else:
                current = load_transactions()
                current_qty = 0
                if not current.empty:
                    for row in current[current["symbol"] == symbol].sort_values(["trade_date", "id"]).itertuples():
                        current_qty += int(row.quantity) if row.side == "BUY" else -int(row.quantity)
                if side == "SELL" and int(quantity) > current_qty:
                    st.error(f"Cannot sell {int(quantity)} shares: recorded available quantity is {current_qty}. Add/verify BUY transactions first.")
                else:
                    save_transaction(trade_date, symbol, side, quantity, price, fees, note)
                    st.success("Transaction saved.")
                    st.rerun()
    st.divider()
    st.subheader("Delete a transaction")
    tx_for_delete = load_transactions()
    if not tx_for_delete.empty:
        choices = {
            f"#{int(r.id)} · {r.trade_date} · {r.side} {int(r.quantity)} {r.symbol} @ {float(r.price):.2f}": int(r.id)
            for r in tx_for_delete.itertuples()
        }
        with st.form("delete_form"):
            selected_label = st.selectbox("Choose transaction", list(choices.keys()))
            confirm_delete = st.checkbox("I confirm deletion of this transaction")
            delete_submit = st.form_submit_button("Delete selected transaction")
            if delete_submit:
                if confirm_delete:
                    delete_transaction(choices[selected_label])
                    st.success("Transaction deleted.")
                    st.rerun()
                else:
                    st.error("Tick the confirmation checkbox first.")

with tab3:
    st.subheader("Latest available market prices")
    if market_df.empty:
        st.info("Market watch is unavailable because the API did not return usable data.")
    else:
        query = st.text_input("Search by symbol or company", placeholder="e.g. NABIL")
        filtered = market_df.copy()
        if query:
            filtered = filtered[
                filtered["symbol"].str.contains(query, case=False, na=False)
                | filtered["company"].str.contains(query, case=False, na=False)
            ]
        sort_cols = [c for c in ["change_percent", "ltp", "symbol"] if c in filtered.columns]
        if "change_percent" in filtered.columns:
            filtered = filtered.sort_values("change_percent", ascending=False, na_position="last")
        st.dataframe(filtered, use_container_width=True, hide_index=True)
        st.download_button("⬇️ Export market data CSV", filtered.to_csv(index=False).encode("utf-8"), "nepse_market_data.csv", "text/csv")

with tab4:
    st.subheader("Manual price override / fallback")
    st.write("Use this when API data is unavailable or a stock symbol is missing. API prices take precedence if they are present.")
    with st.form("manual_price_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        manual_symbol = c1.text_input("Stock symbol", placeholder="e.g. NABIL").upper().strip()
        manual_price = c2.number_input("Price (NPR)", min_value=0.0, value=500.0, step=1.0)
        save_price = st.form_submit_button("Save manual price")
        if save_price:
            if not manual_symbol:
                st.error("Enter a symbol.")
            else:
                save_manual_price(manual_symbol, manual_price)
                st.success("Manual price saved.")
                st.rerun()
    current_manual = load_manual_prices()
    if not current_manual.empty:
        st.dataframe(current_manual, use_container_width=True, hide_index=True)
        with st.form("remove_manual"):
            symbols = current_manual["symbol"].tolist()
            to_remove = st.selectbox("Remove manual price for", symbols)
            remove = st.form_submit_button("Remove manual price")
            if remove:
                with db() as con:
                    con.execute("DELETE FROM manual_prices WHERE symbol=?", (to_remove,))
                st.success("Manual price removed.")
                st.rerun()

st.divider()
st.caption("Calculations are estimates based on the transactions entered. This starter app does not connect to your broker or MeroShare, does not place trades, and does not calculate all Nepal-specific taxes/settlement charges. Keep a backup of nepse_portfolio.db.")
