import os
import io
import time
import sqlite3
import pandas as pd
import streamlit as st
import requests
import json
import base64
import re
from datetime import datetime, date
from PIL import Image

# --- DATABASE SETUP ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "loco_eats_pnl.db")

def get_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with get_connection() as conn:
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fixed_costs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name TEXT UNIQUE NOT NULL,
                monthly_amount REAL NOT NULL,
                daily_rate REAL NOT NULL
            )
        """)

init_db()

# --- SAFE SECRETS HELPER ---
def get_safe_secret(key, default=""):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default

# --- SECURITY ACCESS GATE ---
def check_password():
    def password_entered():
        master_pw = get_safe_secret("APP_PASSWORD", "LocoEats2026!")
        if st.session_state.get("password_input") == master_pw:
            st.session_state["password_correct"] = True
            if "password_input" in st.session_state:
                del st.session_state["password_input"]
        else:
            st.session_state["password_correct"] = False

    if "password_correct" not in st.session_state:
        st.session_state["password_correct"] = False

    if not st.session_state["password_correct"]:
        st.title("🔒 LOCO Eats Concession Portal")
        st.caption("Arctic Edge Ice Arena | Management Access Gate")
        st.text_input("Enter Passcode", type="password", on_change=password_entered, key="password_input")
        if st.session_state.get("password_correct") is False and "password_input" in st.session_state:
            st.error("Incorrect passcode.")
        return False
    return True

if not check_password():
    st.stop()

# --- URL SANITIZER (FIXES CONNECTION ADAPTER ERRORS) ---
def clean_api_url(raw_url):
    """Strips all invisible unicode, zero-width spaces, and whitespace to prevent requests errors."""
    cleaned = "".join(c for c in str(raw_url) if 32 < ord(c) < 127).strip()
    match = re.search(r'https?://[a-zA-Z0-9./:?=&_%-]+', cleaned)
    return match.group(0) if match else cleaned

# --- IMAGE OPTIMIZER & COMPRESSION ---
def optimize_image_for_ocr(image_bytes, max_dim=1600, quality=85):
    """Resizes large smartphone photos to prevent HTTP ReadTimeout errors."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        out_buf = io.BytesIO()
        img.save(out_buf, format="JPEG", quality=quality, optimize=True)
        return out_buf.getvalue(), "image/jpeg"
    except Exception:
        return image_bytes, "image/jpeg"

# --- VISION OCR PARSER ---
def parse_receipt_with_vision(image_bytes, mime_type, api_key):
    """Sends compressed receipt photo to Google Vision with strict URL cleaning and model failover."""
    clean_key = "".join(c for c in str(api_key).strip() if 32 < ord(c) < 127)
    opt_bytes, opt_mime = optimize_image_for_ocr(image_bytes)
    b64_img = base64.b64encode(opt_bytes).decode("utf-8")
    
    candidate_models = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash-latest", "gemini-2.5-pro"]
    prompt = """
    Extract all individual line items from this restaurant/concession receipt.
    Assign each item to one of these exact categories: 'Food Ingredients', 'Packaging & Disposables', 'Condiments & Supplies', 'Beverage', 'Dairy', 'Produce', or 'Meat/Poultry'.
    Return strict JSON with this exact schema:
    {"vendor": "string", "date": "YYYY-MM-DD", "invoice_num": "string", "items": [{"item_name": "string", "category": "string", "amount": 0.00}]}
    """
    payload = {
        "contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": opt_mime, "data": b64_img}}]}],
        "generationConfig": {"response_mime_type": "application/json"}
    }
    
    last_err = ""
    for model_name in candidate_models:
        raw_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={clean_key}"
        url = clean_api_url(raw_url)
        for attempt in range(2):
            try:
                resp = requests.post(url, json=payload, timeout=40)
                if resp.status_code == 200:
                    raw_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                    if raw_text.startswith("```json"):
                        raw_text = raw_text[7:]
                    elif raw_text.startswith("```"):
                        raw_text = raw_text[3:]
                    if raw_text.endswith("```"):
                        raw_text = raw_text[:-3]
                    return json.loads(raw_text.strip()), None
                elif resp.status_code in [503, 429]:
                    last_err = f"{model_name} busy (HTTP {resp.status_code})"
                    time.sleep(1.0)
                    continue
                elif resp.status_code == 404:
                    last_err = f"{model_name} not found"
                    break
                else:
                    last_err = f"API Error [{resp.status_code}]: {resp.text}"
                    break
            except requests.exceptions.Timeout:
                last_err = f"{model_name} timed out"
                continue
            except Exception as e:
                last_err = str(e)
                break

    return None, f"Could not extract receipt. {last_err}. Please retry in a few moments."

# --- UNIVERSAL SQUARE CSV PARSER ---
def extract_date_from_text(text):
    if not text:
        return None
    m = re.search(r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})', str(text))
    if m:
        try:
            return pd.to_datetime(m.group(1)).strftime('%Y-%m-%d')
        except Exception:
            pass
    m = re.search(r'([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})', str(text))
    if m:
        try:
            return pd.to_datetime(m.group(1)).strftime('%Y-%m-%d')
        except Exception:
            pass
    return None

def parse_square_file(uploaded_file):
    try:
        df = pd.read_csv(uploaded_file)
    except Exception:
        uploaded_file.seek(0)
        df = pd.read_csv(uploaded_file, encoding="utf-8-sig")

    df.columns = [str(c).strip().replace("\ufeff", "") for c in df.columns]

    def clean_curr(s):
        return pd.to_numeric(s.astype(str).str.replace('$', '', regex=False).str.replace(',', '', regex=False), errors="coerce").fillna(0.0)

    # FORMAT 1: Transfer Details Export
    if any(c in df.columns for c in ["Payment Date", "Deposit Date"]) and "Collected" in df.columns:
        date_col = "Payment Date" if "Payment Date" in df.columns else "Deposit Date"
        for col in ["Collected", "Fees", "Deposited"]:
            if col in df.columns:
                df[col] = clean_curr(df[col])
        if "Type" in df.columns:
            df = df[df["Type"] == "Payment"]
            
        grouped = df.groupby(date_col).agg(
            gross_sales=("Collected", "sum"),
            processing_fees=("Fees", lambda x: abs(x.sum())),
            order_count=("Transaction ID", "count" if "Transaction ID" in df.columns else "size")
        ).reset_index()
        grouped.rename(columns={date_col: "sales_date"}, inplace=True)
        grouped["net_sales"] = (grouped["gross_sales"] / 1.06).round(2)
        grouped["tax"] = (grouped["gross_sales"] - grouped["net_sales"]).round(2)
        return "Transfer Details Export", grouped, None

    # FORMAT 2: Square Sales Summary (Vertical 2-column)
    first_col_str = str(df.columns[0]).lower()
    first_col_values = [str(x).strip().lower() for x in df.iloc[:, 0].tolist()] if not df.empty else []
    
    if "sales summary" in first_col_str or "gross sales" in first_col_values:
        metrics = {}
        for _, row in df.iterrows():
            k = str(row.iloc[0]).strip().lower()
            v = str(row.iloc[1]).strip() if len(row) > 1 else "0"
            metrics[k] = v

        def to_float(val_str):
            if not val_str or val_str == "nan":
                return 0.0
            cleaned = re.sub(r"[^\d.-]", "", val_str)
            try:
                return float(cleaned)
            except Exception:
                return 0.0

        gross = to_float(metrics.get("gross sales", "0"))
        net = to_float(metrics.get("net sales", "0"))
        tax = to_float(metrics.get("tax", "0"))
        fees = abs(to_float(metrics.get("fees", "0")))
        orders = int(to_float(metrics.get("total transactions", metrics.get("transactions", "0"))))

        if gross == 0.0 and "total collected" in metrics:
            gross = to_float(metrics.get("total collected", "0"))
        if net == 0.0 and gross > 0:
            net = round(gross - tax, 2)

        detected_date = extract_date_from_text(getattr(uploaded_file, "name", "")) or extract_date_from_text(df.columns[0])
        default_date = detected_date if detected_date else date.today().strftime("%Y-%m-%d")

        summary_df = pd.DataFrame([{
            "sales_date": default_date,
            "gross_sales": gross,
            "net_sales": net,
            "tax": tax,
            "processing_fees": fees,
            "order_count": orders
        }])
        return "Sales Summary Report", summary_df, detected_date

    # FORMAT 3: Transactions Report
    date_col = next((c for c in df.columns if c.strip().lower() in ["date", "transaction date", "payment date"]), None)
    gross_col = next((c for c in df.columns if c.strip().lower() in ["gross sales", "total collected", "total"]), None)

    if date_col and gross_col and len(df.columns) > 2:
        for c in [gross_col, "Net Sales", "Tax", "Fees"]:
            if c in df.columns:
                df[c] = clean_curr(df[c])

        df["clean_date"] = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
        df = df.dropna(subset=["clean_date"])

        net_col = "Net Sales" if "Net Sales" in df.columns else None
        tax_col = "Tax" if "Tax" in df.columns else None
        fees_col = "Fees" if "Fees" in df.columns else None

        grouped = df.groupby("clean_date").agg(
            gross_sales=(gross_col, "sum"),
            net_sales=(net_col, "sum") if net_col else (gross_col, lambda x: round(x.sum() / 1.06, 2)),
            tax=(tax_col, "sum") if tax_col else (gross_col, lambda x: round(x.sum() - (x.sum() / 1.06), 2)),
            processing_fees=(fees_col, lambda x: abs(x.sum())) if fees_col else (gross_col, lambda x: 0.0),
            order_count=(date_col, "count")
        ).reset_index()
        grouped.rename(columns={"clean_date": "sales_date"}, inplace=True)
        return "Transactions Report", grouped, None

    return None, df.columns.tolist(), None

# --- STREAMLIT UI CONFIGURATION ---
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

    total_gross = sales_df["gross_sales"].sum() if not sales_df.empty else 0.0
    total_net = sales_df["net_sales"].sum() if not sales_df.empty else 0.0
    total_fees = sales_df["processing_fees"].sum() if not sales_df.empty else 0.0
    total_orders = sales_df["order_count"].sum() if not sales_df.empty else 0
    
    food_spend = cogs_df[cogs_df["category"].isin(["Food Ingredients", "Dairy", "Produce", "Meat/Poultry", "Beverage"])]["amount"].sum()
    packaging_spend = cogs_df[cogs_df["category"] == "Packaging & Disposables"]["amount"].sum()
    condiment_spend = cogs_df[cogs_df["category"] == "Condiments & Supplies"]["amount"].sum()
    total_cogs = food_spend + packaging_spend + condiment_spend

    daily_fixed_burn = fixed_df["daily_rate"].sum() if not fixed_df.empty else 0.0
    active_sales_days = len(sales_df["sales_date"].unique()) if not sales_df.empty else 1
    total_amortized_fixed = daily_fixed_burn * max(active_sales_days, 1)

    food_cost_pct = (food_spend / total_net * 100) if total_net > 0 else 0.0
    pkg_cost_pct = (packaging_spend / total_net * 100) if total_net > 0 else 0.0
    net_profit = total_net - total_cogs - total_amortized_fixed - total_fees

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Gross Collected", f"${total_gross:,.2f}", delta=f"{total_orders} Orders")
    col2.metric("Net Sales (Excl. Tax)", f"${total_net:,.2f}")
    col3.metric("Total COGS Spend", f"${total_cogs:,.2f}", delta=f"{food_cost_pct + pkg_cost_pct:.1f}% COGS")
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
        st.write("### Sales & Fee Metrics")
        avg_ticket = (total_gross / total_orders) if total_orders > 0 else 0.0
        effective_fee = (total_fees / total_gross * 100) if total_gross > 0 else 0.0
        st.write(f"- **Average Order Value (AOV):** `${avg_ticket:,.2f}`")
        st.write(f"- **Total Square Processing Fees:** `${total_fees:,.2f}` ({effective_fee:.2f}% of gross)")
        st.write(f"- **Fixed Daily Overhead Allocation:** `${daily_fixed_burn:,.2f} / day`")

# --- TAB 2: SQUARE DAILY SALES ---
with tabs[1]:
    st.subheader("Square Sales Reconciliation")
    
    st.markdown("#### 📁 Upload Square CSV Report")
    st.caption("Upload any Square export: **Sales Summary**, **Transfer Details**, or **Transactions Report**.")
    uploaded_file = st.file_uploader("Upload Square CSV Export", type=["csv"])
    
    if uploaded_file is not None:
        report_type, parsed_data, detected_date = parse_square_file(uploaded_file)
        if report_type:
            st.success(f"Detected Square Format: **{report_type}**")
            
            if report_type == "Sales Summary Report":
                initial_d = pd.to_datetime(detected_date).date() if detected_date else date.today()
                confirmed_date = st.date_input("Confirm Operating Date for this Report:", value=initial_d)
                parsed_data["sales_date"] = confirmed_date.strftime("%Y-%m-%d")

            st.dataframe(parsed_data, use_container_width=True)
            
            if st.button(f"Commit {len(parsed_data)} Days to Database", type="primary"):
                conn = get_connection()
                for _, r in parsed_data.iterrows():
                    conn.execute("""
                        INSERT INTO daily_sales (sales_date, gross_sales, net_sales, tax, processing_fees, order_count)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(sales_date) DO UPDATE SET
                            gross_sales=excluded.gross_sales,
                            net_sales=excluded.net_sales,
                            tax=excluded.tax,
                            processing_fees=excluded.processing_fees,
                            order_count=excluded.order_count
                    """, (str(r["sales_date"]), float(r["gross_sales"]), float(r["net_sales"]), float(r["tax"]), float(r["processing_fees"]), int(r["order_count"])))
                conn.commit()
                conn.close()
                st.success("Successfully imported all sales data into the database!")
                st.rerun()
        else:
            st.error("Uploaded CSV headers do not match expected Square formats.")
            st.write("Columns found in your uploaded file:", parsed_data)

    st.markdown("---")
    st.markdown("#### Manual Sales Entry")
    with st.form("manual_sales_form", clear_on_submit=False):
        m_date = st.date_input("Business Date", value=date.today())
        c_1, c_2 = st.columns(2)
        m_gross = c_1.number_input("Gross Sales ($)", min_value=0.0, step=10.0, format="%.2f")
        m_net = c_2.number_input("Net Sales ($)", min_value=0.0, step=10.0, format="%.2f")
        c_3, c_4 = st.columns(2)
        m_tax = c_3.number_input("Sales Tax Collected ($)", min_value=0.0, step=1.0, format="%.2f")
        m_fees = c_4.number_input("Square Fees ($)", min_value=0.0, step=1.0, format="%.2f")
        m_orders = st.number_input("Order Count", min_value=0, step=1)
        
        if st.form_submit_button("Commit Daily Record"):
            conn = get_connection()
            conn.execute("""
                INSERT INTO daily_sales (sales_date, gross_sales, net_sales, tax, processing_fees, order_count)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(sales_date) DO UPDATE SET
                    gross_sales=excluded.gross_sales,
                    net_sales=excluded.net_sales,
                    tax=excluded.tax,
                    processing_fees=excluded.processing_fees,
                    order_count=excluded.order_count
            """, (m_date.strftime("%Y-%m-%d"), m_gross, m_net, m_tax, m_fees, m_orders))
            conn.commit()
            conn.close()
            st.success(f"Recorded sales for {m_date.strftime('%Y-%m-%d')}.")
            st.rerun()

    conn = get_connection()
    df_sales_history = pd.read_sql("SELECT sales_date as 'Date', gross_sales as 'Gross ($)', net_sales as 'Net ($)', tax as 'Tax ($)', processing_fees as 'Fees ($)', order_count as 'Orders' FROM daily_sales ORDER BY sales_date DESC", conn)
    conn.close()
    if not df_sales_history.empty:
        st.write("#### Reconciled Sales Ledger")
        st.dataframe(df_sales_history, use_container_width=True)

# --- TAB 3: COGS & PACKAGING LOG ---
with tabs[2]:
    st.subheader("Direct COGS Ingestion")
    
    st.markdown("#### 📷 AI Vision Receipt Scanner (Automated Line-Item Extraction)")
    col_cam, col_upload = st.columns(2)
    with col_cam:
        cam_pic = st.camera_input("Snap receipt with phone camera")
    with col_upload:
        file_pic = st.file_uploader("Or upload image (JPG, PNG)", type=["jpg", "jpeg", "png"])
        
    active_receipt = cam_pic or file_pic
    
    if active_receipt is not None:
        vision_key = get_safe_secret("GEMINI_API_KEY", "")
        if not vision_key:
            vision_key = st.text_input("Gemini API Key (or save in Streamlit Secrets)", type="password")
            
        if st.button("⚡ Scan & Extract Line Items", type="primary"):
            if not vision_key:
                st.error("Please enter a Gemini API Key to run Vision OCR.")
            else:
                with st.spinner("Analyzing receipt with Vision AI..."):
                    img_bytes = active_receipt.getvalue()
                    mime_type = active_receipt.type or "image/jpeg"
                    parsed_result, err = parse_receipt_with_vision(img_bytes, mime_type, vision_key)
                    
                if err:
                    st.error(err)
                elif parsed_result:
                    st.session_state["scanned_receipt"] = parsed_result
                    st.success(f"Extracted {len(parsed_result.get('items', []))} items from {parsed_result.get('vendor', 'Unknown')}!")
                    
    if "scanned_receipt" in st.session_state:
        rec = st.session_state["scanned_receipt"]
        st.markdown(f"**Vendor:** `{rec.get('vendor', 'Unknown')}` | **Date:** `{rec.get('date', 'Unknown')}` | **Invoice/Receipt #:** `{rec.get('invoice_num', 'N/A')}`")
        
        items_df = pd.DataFrame(rec.get("items", []))
        st.write("Review and edit categories or amounts if necessary:")
        edited_df = st.data_editor(items_df, use_container_width=True, num_rows="dynamic")
        
        if st.button("💾 Commit Line Items to Database", type="primary"):
            conn = get_connection()
            for _, r in edited_df.iterrows():
                conn.execute("""
                    INSERT INTO cogs_expenses (expense_date, vendor, category, amount, invoice_num, notes)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    rec.get("date", date.today().strftime("%Y-%m-%d")),
                    rec.get("vendor", "Scanned Vendor"),
                    r.get("category", "Food Ingredients"),
                    float(r.get("amount", 0.0)),
                    rec.get("invoice_num", ""),
                    r.get("item_name", "")
                ))
            conn.commit()
            conn.close()
            st.success("Successfully written to loco_eats_pnl.db!")
            del st.session_state["scanned_receipt"]
            st.rerun()

    st.markdown("---")
    st.markdown("#### ✍ Manual Outlay Entry")
    with st.form("manual_cogs_form", clear_on_submit=True):
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
        
        if st.form_submit_button("Record COGS Outlay"):
            conn = get_connection()
            conn.execute("""
                INSERT INTO cogs_expenses (expense_date, vendor, category, amount, invoice_num, notes)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (c_date.strftime("%Y-%m-%d"), c_vendor, c_cat, c_amount, c_inv, c_notes))
            conn.commit()
            conn.close()
            st.success("COGS transaction logged.")
            st.rerun()

    conn = get_connection()
    df_cogs_history = pd.read_sql("SELECT expense_date as 'Date', vendor as 'Vendor', category as 'Category', amount as 'Amount ($)', notes as 'Item Description' FROM cogs_expenses ORDER BY expense_date DESC LIMIT 25", conn)
    conn.close()
    if not df_cogs_history.empty:
        st.write("#### Recent COGS Entries")
        st.dataframe(df_cogs_history, use_container_width=True)

# --- TAB 4: FIXED OVERHEAD ---
with tabs[3]:
    st.subheader("Monthly Fixed Operating Overhead")
    with st.form("fixed_cost_form", clear_on_submit=True):
        fc_name = st.text_input("Expense Name (e.g. Arena Base Rent, Insurance, POS Lease, OptiSigns)")
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
                st.rerun()

    conn = get_connection()
    df_fixed = pd.read_sql("SELECT item_name as 'Item', monthly_amount as 'Monthly ($)', daily_rate as 'Daily Allocation ($)' FROM fixed_costs", conn)
    conn.close()
    if not df_fixed.empty:
        st.table(df_fixed)