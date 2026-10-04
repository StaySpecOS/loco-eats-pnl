import sqlite3
import pandas as pd
import streamlit as st
from datetime import datetime, date

# --- DATABASE LAYER ---
DB_FILE = "loco_eats_pnl.db"

def get_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with get_connection() as conn:
        # 1. Daily Sales Table (Ingested from Square)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sales_date TEXT UNIQUE NOT NULL,
                gross_sales REAL NOT NULL,
                net_sales REAL NOT NULL,
                tax REAL NOT NULL,
                processing_fees REAL DEFAULT 0.0,
                order_count INTEGER DEFAULT 0
            )
        """)
        # 2. Variable Food & Packaging Expenses (COGS)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cogs_expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                expense_date TEXT NOT NULL,
                vendor TEXT NOT NULL,
                category TEXT NOT NULL, 
                amount REAL NOT NULL,
                invoice_num TEXT,
                notes TEXT
            )
        """)
        # 3. Fixed Operating Overhead (Amortized Daily)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fixed_costs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name TEXT UNIQUE NOT NULL,
                monthly_amount REAL NOT NULL,
                daily_rate REAL NOT NULL
            )
        """)

init_db()

# --- STREAMLIT DASHBOARD UI ---
st.set_page_config(page_title="LOCO Eats - COGS & P&L Engine", layout="wide")
st.title("LOCO Eats | Cost Accounting & Sales Reconciliation Engine")

tabs = st.tabs(["📊 Performance Dashboard", "💳 Square Daily Sales", "📦 COGS & Packaging Log", "🏢 Fixed Overhead Schedule"])

# --- TAB 1: EXECUTIVE FINANCIAL DASHBOARD ---
with tabs[0]:
    st.subheader("Financial Performance & Cost of Goods Sold (COGS)")
    
    conn = get_connection()
    sales_df = pd.read_sql("SELECT * FROM daily_sales ORDER BY sales_date DESC", conn)
    cogs_df = pd.read_sql("SELECT * FROM cogs_expenses ORDER BY expense_date DESC", conn)
    fixed_df = pd.read_sql("SELECT * FROM fixed_costs", conn)
    conn.close()

    total_net_sales = sales_df["net_sales"].sum() if not sales_df.empty else 0.0
    total_processing_fees = sales_df["processing_fees"].sum() if not sales_df.empty else 0.0
    
    # Granular Expense Groupings
    food_spend = cogs_df[cogs_df["category"].isin(["Food Ingredients", "Dairy", "Produce", "Meat/Poultry", "Beverage"])]["amount"].sum()
    packaging_spend = cogs_df[cogs_df["category"] == "Packaging & Disposables"]["amount"].sum()
    condiment_spend = cogs_df[cogs_df["category"] == "Condiments & Supplies"]["amount"].sum()
    total_cogs = food_spend + packaging_spend + condiment_spend

    # Overhead Calculations
    daily_fixed_burn = fixed_df["daily_rate"].sum() if not fixed_df.empty else 0.0
    active_sales_days = len(sales_df["sales_date"].unique()) if not sales_df.empty else 1
    total_amortized_fixed = daily_fixed_burn * max(active_sales_days, 1)

    # Ratios
    food_cost_pct = (food_spend / total_net_sales * 100) if total_net_sales > 0 else 0.0
    pkg_cost_pct = (packaging_spend / total_net_sales * 100) if total_net_sales > 0 else 0.0
    total_cogs_pct = (total_cogs / total_net_sales * 100) if total_net_sales > 0 else 0.0
    net_profit = total_net_sales - total_cogs - total_amortized_fixed - total_processing_fees

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Net Sales (Square)", f"${total_net_sales:,.2f}")
    col2.metric("Total Food Spend", f"${food_spend:,.2f}", delta=f"{food_cost_pct:.1f}% of Sales", delta_color="inverse")
    col3.metric("Packaging & Supplies", f"${packaging_spend + condiment_spend:,.2f}", delta=f"{pkg_cost_pct:.1f}% of Sales", delta_color="inverse")
    col4.metric("Net Contribution Profit", f"${net_profit:,.2f}")

    st.markdown("---")
    c1, c2 = st.columns([1, 1])
    with c1:
        st.write("### Expense Category Breakdown")
        if not cogs_df.empty:
            cat_summary = cogs_df.groupby("category")["amount"].sum().reset_index()
            cat_summary["% of Total"] = (cat_summary["amount"] / cat_summary["amount"].sum()) * 100
            cat_summary["amount"] = cat_summary["amount"].map("${:,.2f}".format)
            cat_summary["% of Total"] = cat_summary["% of Total"].map("{:.1f}%".format)
            st.dataframe(cat_summary, use_container_width=True)
        else:
            st.info("No expense disbursements logged yet.")
    with c2:
        st.write("### Operational Benchmarks")
        st.write(f"- **Target Food Cost %:** `25.0%` (Current: **`{food_cost_pct:.1f}%`**)")
        st.write(f"- **Packaging & Paper Target:** `3.5% - 5.0%` (Current: **`{pkg_cost_pct:.1f}%`**)")
        st.write(f"- **Fixed Daily Overhead Allocation:** `${daily_fixed_burn:,.2f} / day`")

# --- TAB 2: SQUARE DAILY SALES SUBMISSION ---
with tabs[1]:
    st.subheader("Ingest Daily Square Sales Report")
    with st.form("square_entry_form", clear_on_submit=True):
        f_date = st.date_input("Business Date", value=date.today())
        c_a, c_b = st.columns(2)
        f_gross = c_a.number_input("Gross Sales ($)", min_value=0.0, step=10.0, format="%.2f")
        f_net = c_b.number_input("Net Sales ($)", min_value=0.0, step=10.0, format="%.2f")
        c_c, c_d = st.columns(2)
        f_tax = c_c.number_input("Sales Tax Collected ($)", min_value=0.0, step=1.0, format="%.2f")
        f_fees = c_d.number_input("Square Card Processing Fees ($)", min_value=0.0, step=1.0, format="%.2f")
        f_orders = st.number_input("Total Order Count", min_value=0, step=1)
        
        submit_sales = st.form_submit_button("Commit Square Daily Sales")
        if submit_sales:
            conn = get_connection()
            try:
                conn.execute("""
                    INSERT INTO daily_sales (sales_date, gross_sales, net_sales, tax, processing_fees, order_count)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(sales_date) DO UPDATE SET
                        gross_sales=excluded.gross_sales,
                        net_sales=excluded.net_sales,
                        tax=excluded.tax,
                        processing_fees=excluded.processing_fees,
                        order_count=excluded.order_count
                """, (f_date.strftime("%Y-%m-%d"), f_gross, f_net, f_tax, f_fees, f_orders))
                conn.commit()
                st.success(f"Successfully recorded Square sales for {f_date.strftime('%Y-%m-%d')}!")
            except Exception as e:
                st.error(f"Error saving record: {e}")
            finally:
                conn.close()

# --- TAB 3: COGS, PACKAGING & CONDIMENTS LOG ---
with tabs[2]:
    st.subheader("Log Direct COGS Disbursement")
    with st.form("cogs_form", clear_on_submit=True):
        c_date = st.date_input("Disbursement Date", value=date.today())
        c_vendor = st.selectbox("Vendor", [
            "Gordon Food Service", "Restaurant Depot", "PepsiCo / Pepsi Beverages",
            "Costco Wholesale", "Sam's Club", "Papaya Fruit Market", "WebstaurantStore",
            "J&M Vending", "Amazon", "Walmart Supercenter", "Other"
        ])
        c_cat = st.selectbox("Accounting Category", [
            "Food Ingredients", "Packaging & Disposables", "Condiments & Supplies",
            "Beverage", "Dairy", "Produce", "Meat/Poultry"
        ])
        c_amount = st.number_input("Total Paid ($)", min_value=0.0, step=5.0, format="%.2f")
        c_inv = st.text_input("Invoice / Receipt Number")
        c_notes = st.text_area("Itemized Details (e.g. 16oz cups, cone sleeves, mozzarella)")
        
        submit_cogs = st.form_submit_button("Record COGS Outlay")
        if submit_cogs:
            conn = get_connection()
            conn.execute("""
                INSERT INTO cogs_expenses (expense_date, vendor, category, amount, invoice_num, notes)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (c_date.strftime("%Y-%m-%d"), c_vendor, c_cat, c_amount, c_inv, c_notes))
            conn.commit()
            conn.close()
            st.success("COGS transaction logged.")

# --- TAB 4: FIXED OVERHEAD ALLOCATION ---
with tabs[3]:
    st.subheader("Monthly Fixed Operating Overhead")
    st.caption("These costs amortize daily over a standard 30.4-day operational cycle.")
    
    with st.form("fixed_cost_form", clear_on_submit=True):
        fc_name = st.text_input("Expense Name (e.g. Arena Concession Base Rent, Insurance, POS Lease, OptiSigns)")
        fc_amount = st.number_input("Monthly Billed Amount ($)", min_value=0.0, step=25.0, format="%.2f")
        
        if st.form_submit_button("Add / Update Fixed Expense"):
            if fc_name and fc_amount > 0:
                d_rate = fc_amount / 30.417
                conn = get_connection()
                conn.execute("""
                    INSERT INTO fixed_costs (item_name, monthly_amount, daily_rate)
                    VALUES (?, ?, ?)
                    ON CONFLICT(item_name) DO UPDATE SET
                        monthly_amount=excluded.monthly_amount,
                        daily_rate=excluded.daily_rate
                """, (fc_name, fc_amount, d_rate))
                conn.commit()
                conn.close()
                st.success(f"Configured {fc_name} at ${d_rate:.2f}/day.")

    conn = get_connection()
    df_fixed = pd.read_sql("SELECT item_name as 'Item', monthly_amount as 'Monthly ($)', daily_rate as 'Daily Allocation ($)' FROM fixed_costs", conn)
    conn.close()
    if not df_fixed.empty:
        st.table(df_fixed)