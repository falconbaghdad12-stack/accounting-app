# -*- coding: utf-8 -*-
"""
برنامج محاسبي متكامل - قيد ثنائي (Double Entry)
مبني بلغة Python باستخدام Streamlit + SQLite (أو Turso عند النشر السحابي)

طريقة التشغيل محليًا:
    pip install -r requirements.txt
    streamlit run app.py

طريقة التشغيل على Streamlit Community Cloud:
    أضف بملف .streamlit/secrets.toml (أو من إعدادات Secrets بالمنصة):
        TURSO_DATABASE_URL = "libsql://xxxx.turso.io"
        TURSO_AUTH_TOKEN = "xxxxxxxx"
    وإذا ما وفّرت هذي الإعدادات، البرنامج يشتغل محليًا بملف accounting.db عادي.
"""

import streamlit as st
import sqlite3
import pandas as pd
from datetime import date, datetime
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounting.db")


def _get_secret(key):
    try:
        return st.secrets.get(key, None)
    except Exception:
        return None


USE_CLOUD_DB = bool(_get_secret("TURSO_DATABASE_URL") and _get_secret("TURSO_AUTH_TOKEN"))

# ---------------------------------------------------------------------------
# قاعدة البيانات
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def get_conn():
    if USE_CLOUD_DB:
        import libsql
        conn = libsql.connect(
            database=_get_secret("TURSO_DATABASE_URL"),
            auth_token=_get_secret("TURSO_AUTH_TOKEN"),
        )
    else:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
    except Exception:
        pass  # بعض قواعد البيانات السحابية لا تدعم هذا الأمر، وهذا لا يؤثر على عمل البرنامج
    return conn


def query_df(conn, query, params=None):
    """ينفّذ استعلام SELECT ويرجع DataFrame - يعمل بأمان مع SQLite المحلي وTurso السحابي معًا"""
    cur = conn.cursor()
    if params:
        cur.execute(query, params)
    else:
        cur.execute(query)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT NOT NULL,        -- اصول / خصوم / حقوق الملكية / ايرادات / مصروفات
            nature TEXT NOT NULL,      -- مدين / دائن
            parent_code TEXT,
            is_active INTEGER DEFAULT 1
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_date TEXT NOT NULL,
            description TEXT,
            source TEXT,               -- نوع الحركة: يدوي / مبيعات / مشتريات / صندوق / مصاريف / ايرادات
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS journal_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            journal_id INTEGER NOT NULL,
            account_code TEXT NOT NULL,
            debit REAL DEFAULT 0,
            credit REAL DEFAULT 0,
            line_desc TEXT,
            party_type TEXT,           -- customer / supplier / NULL
            party_id INTEGER,
            reconciled INTEGER DEFAULT 0,
            FOREIGN KEY (journal_id) REFERENCES journal(id) ON DELETE CASCADE,
            FOREIGN KEY (account_code) REFERENCES accounts(code)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            opening_balance REAL DEFAULT 0,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            opening_balance REAL DEFAULT 0,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS fixed_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            asset_account_code TEXT NOT NULL,
            accum_dep_account_code TEXT NOT NULL,
            expense_account_code TEXT NOT NULL,
            purchase_date TEXT NOT NULL,
            cost REAL NOT NULL,
            salvage_value REAL DEFAULT 0,
            useful_life_months INTEGER NOT NULL,
            accumulated_depreciation REAL DEFAULT 0,
            is_active INTEGER DEFAULT 1
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS depreciation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL,
            period_date TEXT NOT NULL,
            amount REAL NOT NULL,
            journal_id INTEGER,
            FOREIGN KEY (asset_id) REFERENCES fixed_assets(id)
        )
    """)
    conn.commit()

    # ترقية قواعد بيانات قديمة (أنشئت قبل إضافة هذه الأعمدة)
    existing_cols = [r[1] for r in cur.execute("PRAGMA table_info(journal_lines)").fetchall()]
    for col, coltype in [("party_type", "TEXT"), ("party_id", "INTEGER"), ("reconciled", "INTEGER DEFAULT 0")]:
        if col not in existing_cols:
            cur.execute(f"ALTER TABLE journal_lines ADD COLUMN {col} {coltype}")
    conn.commit()

    seed_accounts(conn)


def seed_accounts(conn):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM accounts")
    if cur.fetchone()[0] > 0:
        return

    # شجرة حسابات موحدة (دليل حسابات قياسي)
    accounts = [
        # code, name, type, nature, parent_code
        ("1000", "الأصول", "اصول", "مدين", None),
        ("1100", "الأصول المتداولة", "اصول", "مدين", "1000"),
        ("1101", "الصندوق", "اصول", "مدين", "1100"),
        ("1102", "البنك", "اصول", "مدين", "1100"),
        ("1103", "العملاء (المدينون)", "اصول", "مدين", "1100"),
        ("1104", "المخزون", "اصول", "مدين", "1100"),
        ("1105", "أوراق قبض", "اصول", "مدين", "1100"),
        ("1200", "الأصول الثابتة", "اصول", "مدين", "1000"),
        ("1201", "أثاث ومعدات", "اصول", "مدين", "1200"),
        ("1202", "سيارات", "اصول", "مدين", "1200"),
        ("1203", "مجمع الإهلاك", "اصول", "دائن", "1200"),

        ("2000", "الخصوم", "خصوم", "دائن", None),
        ("2100", "الخصوم المتداولة", "خصوم", "دائن", "2000"),
        ("2101", "الموردون (الدائنون)", "خصوم", "دائن", "2100"),
        ("2102", "أوراق دفع", "خصوم", "دائن", "2100"),
        ("2103", "مصروفات مستحقة", "خصوم", "دائن", "2100"),
        ("2200", "الخصوم طويلة الأجل", "خصوم", "دائن", "2000"),
        ("2201", "قروض طويلة الأجل", "خصوم", "دائن", "2200"),

        ("3000", "حقوق الملكية", "حقوق الملكية", "دائن", None),
        ("3101", "رأس المال", "حقوق الملكية", "دائن", "3000"),
        ("3102", "المسحوبات الشخصية", "حقوق الملكية", "مدين", "3000"),
        ("3103", "الأرباح المرحلة", "حقوق الملكية", "دائن", "3000"),

        ("4000", "الإيرادات", "ايرادات", "دائن", None),
        ("4101", "إيرادات المبيعات", "ايرادات", "دائن", "4000"),
        ("4102", "إيرادات خدمات", "ايرادات", "دائن", "4000"),
        ("4103", "إيرادات أخرى", "ايرادات", "دائن", "4000"),
        ("4200", "مردودات ومسموحات المبيعات", "ايرادات", "مدين", "4000"),

        ("5000", "المصروفات", "مصروفات", "مدين", None),
        ("5101", "تكلفة البضاعة المباعة", "مصروفات", "مدين", "5000"),
        ("5102", "مصروف الإيجار", "مصروفات", "مدين", "5000"),
        ("5103", "مصروف الرواتب والأجور", "مصروفات", "مدين", "5000"),
        ("5104", "مصروف الكهرباء والماء", "مصروفات", "مدين", "5000"),
        ("5105", "مصروف الاتصالات والإنترنت", "مصروفات", "مدين", "5000"),
        ("5106", "مصروف الصيانة", "مصروفات", "مدين", "5000"),
        ("5107", "مصروف الإهلاك", "مصروفات", "مدين", "5000"),
        ("5108", "مصاريف متنوعة", "مصروفات", "مدين", "5000"),
    ]
    cur.executemany(
        "INSERT INTO accounts (code, name, type, nature, parent_code) VALUES (?,?,?,?,?)",
        accounts
    )
    conn.commit()


# ---------------------------------------------------------------------------
# دوال مساعدة - الحسابات
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300, show_spinner=False)
def get_accounts_df(active_only=True):
    conn = get_conn()
    q = "SELECT code, name, type, nature, parent_code, is_active FROM accounts"
    if active_only:
        q += " WHERE is_active = 1"
    q += " ORDER BY code"
    df = query_df(conn, q)
    return df


def account_label_map():
    df = get_accounts_df()
    return {row["code"]: f'{row["code"]} - {row["name"]}' for _, row in df.iterrows()}


def leaf_accounts_only(df):
    """يرجع الحسابات التي ليست أب لحساب آخر (الحسابات التفصيلية فقط) - تصلح للترحيل عليها"""
    parents = set(df["parent_code"].dropna().unique())
    return df[~df["code"].isin(parents)]


def add_account(code, name, acc_type, nature, parent_code):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO accounts (code, name, type, nature, parent_code) VALUES (?,?,?,?,?)",
            (code, name, acc_type, nature, parent_code if parent_code else None)
        )
        conn.commit()
        get_accounts_df.clear()
        return True, "تمت إضافة الحساب بنجاح"
    except Exception:
        return False, "رمز الحساب موجود مسبقًا"


# ---------------------------------------------------------------------------
# دوال مساعدة - القيود
# ---------------------------------------------------------------------------

def post_journal_entry(entry_date, description, lines, source="يدوي"):
    """
    lines: list of dicts {account_code, debit, credit, line_desc}
    يتحقق من توازن القيد (مجموع مدين = مجموع دائن) قبل الترحيل
    """
    total_debit = sum(l["debit"] for l in lines)
    total_credit = sum(l["credit"] for l in lines)

    if round(total_debit, 2) != round(total_credit, 2):
        return False, f"القيد غير متوازن! مدين={total_debit:.2f} دائن={total_credit:.2f}"
    if total_debit == 0:
        return False, "لا يمكن ترحيل قيد بقيمة صفر"

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO journal (entry_date, description, source, created_at) VALUES (?,?,?,?)",
        (str(entry_date), description, source, datetime.now().isoformat())
    )
    journal_id = cur.lastrowid
    for l in lines:
        if l["debit"] == 0 and l["credit"] == 0:
            continue
        cur.execute(
            """INSERT INTO journal_lines
               (journal_id, account_code, debit, credit, line_desc, party_type, party_id)
               VALUES (?,?,?,?,?,?,?)""",
            (journal_id, l["account_code"], l["debit"], l["credit"], l.get("line_desc", ""),
             l.get("party_type"), l.get("party_id"))
        )
    conn.commit()
    return True, f"تم ترحيل القيد رقم {journal_id} بنجاح"


def delete_journal_entry(journal_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM journal_lines WHERE journal_id=?", (journal_id,))
    cur.execute("DELETE FROM journal WHERE id=?", (journal_id,))
    conn.commit()


def get_journal_df(date_from=None, date_to=None):
    conn = get_conn()
    q = """
        SELECT j.id as رقم_القيد, j.entry_date as التاريخ, j.description as البيان,
               j.source as المصدر, jl.account_code as رمز_الحساب,
               a.name as اسم_الحساب, jl.debit as مدين, jl.credit as دائن,
               jl.line_desc as بيان_السطر
        FROM journal j
        JOIN journal_lines jl ON jl.journal_id = j.id
        JOIN accounts a ON a.code = jl.account_code
    """
    conditions = []
    params = []
    if date_from:
        conditions.append("j.entry_date >= ?")
        params.append(str(date_from))
    if date_to:
        conditions.append("j.entry_date <= ?")
        params.append(str(date_to))
    if conditions:
        q += " WHERE " + " AND ".join(conditions)
    q += " ORDER BY j.id DESC, jl.id"
    df = query_df(conn, q, params)
    return df


def get_ledger_df(account_code, date_from=None, date_to=None):
    conn = get_conn()
    q = """
        SELECT j.entry_date as التاريخ, j.id as رقم_القيد, j.description as البيان,
               jl.debit as مدين, jl.credit as دائن
        FROM journal_lines jl
        JOIN journal j ON j.id = jl.journal_id
        WHERE jl.account_code = ?
    """
    params = [account_code]
    if date_from:
        q += " AND j.entry_date >= ?"
        params.append(str(date_from))
    if date_to:
        q += " AND j.entry_date <= ?"
        params.append(str(date_to))
    q += " ORDER BY j.entry_date, j.id"
    df = query_df(conn, q, params)
    return df


def account_balance(account_code, nature, date_from=None, date_to=None):
    df = get_ledger_df(account_code, date_from, date_to)
    total_debit = df["مدين"].sum()
    total_credit = df["دائن"].sum()
    if nature == "مدين":
        return total_debit - total_credit
    else:
        return total_credit - total_debit


def get_all_account_balances(date_from=None, date_to=None):
    """
    يجيب أرصدة كل الحسابات التفصيلية باستعلام واحد فقط (بدل استعلام لكل حساب) -
    هذا مهم جدًا لتقليل عدد رحلات الشبكة عند استخدام قاعدة بيانات سحابية.
    """
    conn = get_conn()
    q = """
        SELECT jl.account_code as code, SUM(jl.debit) as total_debit, SUM(jl.credit) as total_credit
        FROM journal_lines jl
        JOIN journal j ON j.id = jl.journal_id
        WHERE 1=1
    """
    params = []
    if date_from:
        q += " AND j.entry_date >= ?"
        params.append(str(date_from))
    if date_to:
        q += " AND j.entry_date <= ?"
        params.append(str(date_to))
    q += " GROUP BY jl.account_code"
    sums_df = query_df(conn, q, params if params else None)

    accounts = leaf_accounts_only(get_accounts_df())
    merged = accounts.merge(sums_df, on="code", how="left")
    merged["total_debit"] = merged["total_debit"].fillna(0.0)
    merged["total_credit"] = merged["total_credit"].fillna(0.0)
    merged["balance"] = merged.apply(
        lambda r: (r["total_debit"] - r["total_credit"]) if r["nature"] == "مدين"
        else (r["total_credit"] - r["total_debit"]),
        axis=1
    )
    return merged


def trial_balance_df(date_from=None, date_to=None, balances=None):
    if balances is None:
        balances = get_all_account_balances(date_from, date_to)
    rows = []
    for _, acc in balances.iterrows():
        total_debit, total_credit = acc["total_debit"], acc["total_credit"]
        if total_debit == 0 and total_credit == 0:
            continue
        balance = total_debit - total_credit
        rows.append({
            "رمز الحساب": acc["code"],
            "اسم الحساب": acc["name"],
            "مدين": total_debit,
            "دائن": total_credit,
            "الرصيد مدين": balance if balance > 0 else 0,
            "الرصيد دائن": -balance if balance < 0 else 0,
        })
    return pd.DataFrame(rows)


def income_statement(date_from=None, date_to=None, balances=None):
    if balances is None:
        balances = get_all_account_balances(date_from, date_to)
    revenues = balances[balances["type"] == "ايرادات"]
    expenses = balances[balances["type"] == "مصروفات"]

    rev_rows, exp_rows = [], []
    total_rev, total_exp = 0, 0
    for _, acc in revenues.iterrows():
        bal = acc["balance"]
        if bal != 0:
            rev_rows.append({"الحساب": f'{acc["code"]} - {acc["name"]}', "المبلغ": bal})
            total_rev += bal
    for _, acc in expenses.iterrows():
        bal = acc["balance"]
        if bal != 0:
            exp_rows.append({"الحساب": f'{acc["code"]} - {acc["name"]}', "المبلغ": bal})
            total_exp += bal

    net_income = total_rev - total_exp
    return pd.DataFrame(rev_rows), total_rev, pd.DataFrame(exp_rows), total_exp, net_income


def balance_sheet(date_from=None, date_to=None, balances=None):
    if balances is None:
        balances = get_all_account_balances(date_from, date_to)
    assets = balances[balances["type"] == "اصول"]
    liabilities = balances[balances["type"] == "خصوم"]
    equity = balances[balances["type"] == "حقوق الملكية"]

    def build(df_accounts):
        rows, total = [], 0
        for _, acc in df_accounts.iterrows():
            bal = acc["balance"]
            if bal != 0:
                rows.append({"الحساب": f'{acc["code"]} - {acc["name"]}', "المبلغ": bal})
                total += bal
        return pd.DataFrame(rows), total

    assets_df, total_assets = build(assets)
    liab_df, total_liab = build(liabilities)
    equity_df, total_equity = build(equity)

    # صافي الدخل يضاف لحقوق الملكية (لأنه لم يُقفل بعد)
    _, _, _, _, net_income = income_statement(date_from, date_to, balances=balances)
    total_equity_with_income = total_equity + net_income

    return assets_df, total_assets, liab_df, total_liab, equity_df, total_equity_with_income, net_income


# ---------------------------------------------------------------------------
# العملاء والموردون (ذمم مدينة / ذمم دائنة)
# ---------------------------------------------------------------------------
AR_ACCOUNT = "1103"   # العملاء (المدينون)
AP_ACCOUNT = "2101"   # الموردون (الدائنون)


def get_customers_df():
    conn = get_conn()
    df = query_df(conn, "SELECT * FROM customers ORDER BY name")
    return df


def get_suppliers_df():
    conn = get_conn()
    df = query_df(conn, "SELECT * FROM suppliers ORDER BY name")
    return df


def add_customer(name, phone="", opening_balance=0.0):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO customers (name, phone, opening_balance, created_at) VALUES (?,?,?,?)",
                (name, phone, opening_balance, datetime.now().isoformat()))
    new_id = cur.lastrowid
    conn.commit()
    return new_id


def add_supplier(name, phone="", opening_balance=0.0):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO suppliers (name, phone, opening_balance, created_at) VALUES (?,?,?,?)",
                (name, phone, opening_balance, datetime.now().isoformat()))
    new_id = cur.lastrowid
    conn.commit()
    return new_id


def get_party_ledger(party_type, party_id, account_code):
    """كل الحركات المرتبطة بعميل/مورد معيّن على حساب الذمم"""
    conn = get_conn()
    q = """
        SELECT j.entry_date as التاريخ, j.id as رقم_القيد, j.description as البيان,
               jl.debit as مدين, jl.credit as دائن
        FROM journal_lines jl
        JOIN journal j ON j.id = jl.journal_id
        WHERE jl.account_code = ? AND jl.party_type = ? AND jl.party_id = ?
        ORDER BY j.entry_date, j.id
    """
    df = query_df(conn, q, [account_code, party_type, party_id])
    return df


def party_balance(party_type, party_id, account_code, opening_balance=0.0):
    df = get_party_ledger(party_type, party_id, account_code)
    return opening_balance + df["مدين"].sum() - df["دائن"].sum()


def party_aging(party_type, party_id, account_code, opening_balance=0.0, as_of=None):
    """
    تحليل أعمار الديون بطريقة FIFO: الدفعات (دائن) تُطفئ أقدم الفواتير (مدين) أولاً.
    يرجع قائمة بالفواتير غير المسددة مع عمرها بالأيام.
    """
    if as_of is None:
        as_of = date.today()
    as_of = pd.Timestamp(as_of)

    df = get_party_ledger(party_type, party_id, account_code)
    invoices = []  # [تاريخ, رقم_قيد, بيان, متبقي]
    if opening_balance > 0:
        invoices.append({"التاريخ": pd.Timestamp(df["التاريخ"].min()) if not df.empty else as_of,
                          "رقم_القيد": "-", "البيان": "رصيد افتتاحي", "متبقي": opening_balance})

    payments_pool = 0.0
    for _, r in df.iterrows():
        if r["مدين"] > 0:
            invoices.append({"التاريخ": pd.Timestamp(r["التاريخ"]), "رقم_القيد": r["رقم_القيد"],
                              "البيان": r["البيان"], "متبقي": r["مدين"]})
        if r["دائن"] > 0:
            payments_pool += r["دائن"]

    # تطبيق الدفعات على أقدم الفواتير أولاً (FIFO)
    invoices.sort(key=lambda x: x["التاريخ"])
    for inv in invoices:
        if payments_pool <= 0:
            break
        applied = min(payments_pool, inv["متبقي"])
        inv["متبقي"] -= applied
        payments_pool -= applied

    rows = []
    buckets = {"0-30 يوم": 0.0, "31-60 يوم": 0.0, "61-90 يوم": 0.0, "أكثر من 90 يوم": 0.0}
    for inv in invoices:
        if inv["متبقي"] <= 0.01:
            continue
        age_days = (as_of - inv["التاريخ"]).days
        if age_days <= 30:
            bucket = "0-30 يوم"
        elif age_days <= 60:
            bucket = "31-60 يوم"
        elif age_days <= 90:
            bucket = "61-90 يوم"
        else:
            bucket = "أكثر من 90 يوم"
        buckets[bucket] += inv["متبقي"]
        rows.append({
            "التاريخ": inv["التاريخ"].date(), "رقم القيد": inv["رقم_القيد"], "البيان": inv["البيان"],
            "المبلغ المتبقي": round(inv["متبقي"], 2), "العمر (يوم)": age_days, "الفئة": bucket
        })
    return pd.DataFrame(rows), buckets


# ---------------------------------------------------------------------------
# الأصول الثابتة والإهلاك
# ---------------------------------------------------------------------------

def get_fixed_assets_df(active_only=True):
    conn = get_conn()
    q = "SELECT * FROM fixed_assets"
    if active_only:
        q += " WHERE is_active = 1"
    df = query_df(conn, q)
    return df


def add_fixed_asset(name, asset_account, accum_dep_account, expense_account,
                     purchase_date, cost, salvage_value, useful_life_months,
                     funding_account, post_opening_entry=True):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO fixed_assets
        (name, asset_account_code, accum_dep_account_code, expense_account_code,
         purchase_date, cost, salvage_value, useful_life_months, accumulated_depreciation)
        VALUES (?,?,?,?,?,?,?,?,0)
    """, (name, asset_account, accum_dep_account, expense_account,
          str(purchase_date), cost, salvage_value, useful_life_months))
    asset_id = cur.lastrowid
    conn.commit()

    if post_opening_entry and cost > 0:
        lines = [
            {"account_code": asset_account, "debit": cost, "credit": 0, "line_desc": f"شراء أصل: {name}"},
            {"account_code": funding_account, "debit": 0, "credit": cost, "line_desc": f"شراء أصل: {name}"},
        ]
        post_journal_entry(purchase_date, f"إثبات أصل ثابت: {name}", lines, source="أصول ثابتة")

    return asset_id


def monthly_depreciation_amount(asset_row):
    depreciable = asset_row["cost"] - asset_row["salvage_value"]
    if asset_row["useful_life_months"] <= 0:
        return 0.0
    return round(depreciable / asset_row["useful_life_months"], 2)


def remaining_depreciable_value(asset_row):
    return round(asset_row["cost"] - asset_row["salvage_value"] - asset_row["accumulated_depreciation"], 2)


def depreciation_already_posted(asset_id, period_date):
    """يتحقق هل تم ترحيل إهلاك لنفس الأصل خلال نفس الشهر/السنة مسبقًا"""
    period_ym = str(period_date)[:7]  # YYYY-MM
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM depreciation_log WHERE asset_id=? AND substr(period_date,1,7)=?",
        (asset_id, period_ym)
    )
    count = cur.fetchone()[0]
    return count > 0


def post_depreciation_entry(asset_id, period_date, force=False):
    """يرحّل قيد إهلاك لأصل واحد عن فترة معينة (لا يتجاوز القيمة القابلة للإهلاك)"""
    if not force and depreciation_already_posted(asset_id, period_date):
        return False, "تم ترحيل إهلاك لهذا الأصل خلال نفس الشهر مسبقًا — تخطّي لمنع التكرار"

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM fixed_assets WHERE id=?", (asset_id,))
    row = cur.fetchone()
    cols = [d[0] for d in cur.description]
    asset = dict(zip(cols, row))

    remaining = remaining_depreciable_value(asset)
    if remaining <= 0:
        return False, f"الأصل '{asset['name']}' مُهلَك بالكامل، لا يوجد إهلاك إضافي"

    amount = min(monthly_depreciation_amount(asset), remaining)
    if amount <= 0:
        return False, "لا يوجد مبلغ إهلاك لترحيله"

    lines = [
        {"account_code": asset["expense_account_code"], "debit": amount, "credit": 0,
         "line_desc": f"إهلاك: {asset['name']}"},
        {"account_code": asset["accum_dep_account_code"], "debit": 0, "credit": amount,
         "line_desc": f"إهلاك: {asset['name']}"},
    ]
    ok, msg = post_journal_entry(period_date, f"قيد إهلاك - {asset['name']}", lines, source="إهلاك")
    if ok:
        conn = get_conn()
        cur = conn.cursor()
        journal_id = cur.execute("SELECT id FROM journal ORDER BY id DESC LIMIT 1").fetchone()[0]
        cur.execute("UPDATE fixed_assets SET accumulated_depreciation = accumulated_depreciation + ? WHERE id=?",
                    (amount, asset_id))
        cur.execute("INSERT INTO depreciation_log (asset_id, period_date, amount, journal_id) VALUES (?,?,?,?)",
                    (asset_id, str(period_date), amount, journal_id))
        conn.commit()
    return ok, msg


def get_depreciation_log_df(asset_id=None):
    conn = get_conn()
    q = """
        SELECT d.period_date as تاريخ_الفترة, f.name as الأصل, d.amount as المبلغ, d.journal_id as رقم_القيد
        FROM depreciation_log d JOIN fixed_assets f ON f.id = d.asset_id
    """
    params = []
    if asset_id:
        q += " WHERE d.asset_id = ?"
        params.append(asset_id)
    q += " ORDER BY d.period_date DESC"
    df = query_df(conn, q, params)
    return df


# ---------------------------------------------------------------------------
# التسوية البنكية (Reconciliation)
# ---------------------------------------------------------------------------

def get_reconcile_lines(account_code, up_to_date=None):
    conn = get_conn()
    q = """
        SELECT jl.id as line_id, j.entry_date as التاريخ, j.id as رقم_القيد, j.description as البيان,
               jl.debit as مدين, jl.credit as دائن, jl.reconciled as مطابق
        FROM journal_lines jl JOIN journal j ON j.id = jl.journal_id
        WHERE jl.account_code = ?
    """
    params = [account_code]
    if up_to_date:
        q += " AND j.entry_date <= ?"
        params.append(str(up_to_date))
    q += " ORDER BY j.entry_date, j.id"
    df = query_df(conn, q, params)
    return df


def set_line_reconciled(line_id, reconciled: bool):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE journal_lines SET reconciled=? WHERE id=?", (1 if reconciled else 0, line_id))
    conn.commit()


# ---------------------------------------------------------------------------
# واجهة المستخدم - Streamlit
# ---------------------------------------------------------------------------

st.set_page_config(page_title="النظام المحاسبي المتكامل", layout="wide")


@st.cache_resource(show_spinner=False)
def _init_db_once():
    """يشغّل init_db() مرة واحدة فقط طوال عمر السيرفر، بدل كل تفاعل بالواجهة"""
    init_db()
    return True


_init_db_once()

st.markdown("""
<style>
    .stApp { direction: rtl; }
    div[data-testid="stMetricValue"] { direction: ltr; }
    table { direction: rtl; }
</style>
""", unsafe_allow_html=True)

st.title("📊 النظام المحاسبي المتكامل - قيد ثنائي")

menu = st.sidebar.radio(
    "القائمة الرئيسية",
    [
        "🏠 الرئيسية",
        "🌳 شجرة الحسابات",
        "✍️ قيد يومية عام",
        "🛒 حركة مبيعات",
        "📦 حركة مشتريات",
        "💰 حركة صندوق (قبض / صرف)",
        "🧾 مصروفات",
        "💵 إيرادات أخرى",
        "👥 العملاء (ذمم مدينة)",
        "🏭 الموردون (ذمم دائنة)",
        "🏢 الأصول الثابتة والإهلاك",
        "🔄 التسوية البنكية",
        "📚 دفتر اليومية",
        "📖 دفتر الأستاذ",
        "⚖️ ميزان المراجعة",
        "📈 قائمة الدخل",
        "🏦 قائمة المركز المالي",
    ]
)

acc_map = account_label_map()
acc_options = list(acc_map.keys())


def acc_selectbox(label, key, filter_types=None, default_code=None):
    df = get_accounts_df()
    df = leaf_accounts_only(df)
    if filter_types:
        df = df[df["type"].isin(filter_types)]
    options = df["code"].tolist()
    labels = {c: acc_map[c] for c in options}
    if not options:
        st.warning("لا توجد حسابات مناسبة")
        return None
    idx = options.index(default_code) if default_code in options else 0
    choice = st.selectbox(label, options, index=idx, format_func=lambda c: labels[c], key=key)
    return choice


# ---------------------------------------------------------------------------
# الرئيسية
# ---------------------------------------------------------------------------
if menu == "🏠 الرئيسية":
    st.subheader("نظرة عامة")
    col1, col2, col3 = st.columns(3)

    home_balances = get_all_account_balances()
    _, total_rev, _, total_exp, net_income = income_statement(balances=home_balances)
    assets_df, total_assets, liab_df, total_liab, equity_df, total_equity, _ = balance_sheet(balances=home_balances)

    col1.metric("إجمالي الإيرادات", f"{total_rev:,.2f}")
    col2.metric("إجمالي المصروفات", f"{total_exp:,.2f}")
    col3.metric("صافي الدخل", f"{net_income:,.2f}")

    col4, col5, col6 = st.columns(3)
    col4.metric("إجمالي الأصول", f"{total_assets:,.2f}")
    col5.metric("إجمالي الخصوم", f"{total_liab:,.2f}")
    col6.metric("إجمالي حقوق الملكية", f"{total_equity:,.2f}")

    st.divider()
    st.subheader("آخر القيود المرحّلة")
    df = get_journal_df()
    if not df.empty:
        st.dataframe(df.head(20), use_container_width=True, hide_index=True)
    else:
        st.info("لا توجد قيود بعد. ابدأ بتسجيل حركاتك من القائمة الجانبية.")

# ---------------------------------------------------------------------------
# شجرة الحسابات
# ---------------------------------------------------------------------------
elif menu == "🌳 شجرة الحسابات":
    st.subheader("شجرة الحسابات (دليل الحسابات الموحد)")

    df = get_accounts_df()

    def build_tree(df, parent=None, level=0):
        children = df[df["parent_code"] == parent].sort_values("code")
        for _, row in children.iterrows():
            indent = "&nbsp;&nbsp;&nbsp;&nbsp;" * level
            icon = "📁" if row["code"] in df["parent_code"].values else "📄"
            st.markdown(f"{indent}{icon} **{row['code']}** — {row['name']} "
                        f"<span style='color:gray;font-size:0.85em'>({row['type']} / {row['nature']})</span>",
                        unsafe_allow_html=True)
            build_tree(df, row["code"], level + 1)

    build_tree(df, None, 0)

    st.divider()
    st.subheader("➕ إضافة حساب جديد")
    with st.form("add_account_form"):
        c1, c2 = st.columns(2)
        new_code = c1.text_input("رمز الحساب (مثال: 1106)")
        new_name = c2.text_input("اسم الحساب")
        c3, c4 = st.columns(2)
        new_type = c3.selectbox("نوع الحساب", ["اصول", "خصوم", "حقوق الملكية", "ايرادات", "مصروفات"])
        new_nature = c4.selectbox("طبيعة الحساب", ["مدين", "دائن"])
        parent_options = [""] + get_accounts_df()["code"].tolist()
        new_parent = st.selectbox("الحساب الأب (اختياري)", parent_options,
                                   format_func=lambda c: "بدون" if c == "" else acc_map.get(c, c))
        submitted = st.form_submit_button("إضافة الحساب")
        if submitted:
            if not new_code or not new_name:
                st.error("الرجاء تعبئة رمز واسم الحساب")
            else:
                ok, msg = add_account(new_code, new_name, new_type, new_nature, new_parent or None)
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)

# ---------------------------------------------------------------------------
# قيد يومية عام (يدوي)
# ---------------------------------------------------------------------------
elif menu == "✍️ قيد يومية عام":
    st.subheader("قيد يومية عام (يدوي)")
    st.caption("أدخل سطور القيد بحيث يكون مجموع المدين = مجموع الدائن")

    c1, c2 = st.columns(2)
    entry_date = c1.date_input("التاريخ", value=date.today())
    description = c2.text_input("بيان القيد")

    if "manual_lines" not in st.session_state:
        st.session_state.manual_lines = pd.DataFrame(
            [{"رمز الحساب": acc_options[0] if acc_options else "", "مدين": 0.0, "دائن": 0.0, "بيان السطر": ""}]
        )

    df_accounts = leaf_accounts_only(get_accounts_df())
    edited = st.data_editor(
        st.session_state.manual_lines,
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "رمز الحساب": st.column_config.SelectboxColumn(
                "رمز الحساب", options=df_accounts["code"].tolist(), required=True
            ),
            "مدين": st.column_config.NumberColumn("مدين", min_value=0.0, step=0.01),
            "دائن": st.column_config.NumberColumn("دائن", min_value=0.0, step=0.01),
        },
        key="journal_editor"
    )

    total_d = edited["مدين"].sum()
    total_c = edited["دائن"].sum()
    c1, c2, c3 = st.columns(3)
    c1.metric("إجمالي مدين", f"{total_d:,.2f}")
    c2.metric("إجمالي دائن", f"{total_c:,.2f}")
    c3.metric("الفرق", f"{total_d - total_c:,.2f}")

    if st.button("✅ ترحيل القيد", type="primary"):
        lines = [
            {"account_code": r["رمز الحساب"], "debit": float(r["مدين"] or 0),
             "credit": float(r["دائن"] or 0), "line_desc": r.get("بيان السطر", "")}
            for _, r in edited.iterrows() if r["رمز الحساب"]
        ]
        ok, msg = post_journal_entry(entry_date, description, lines, source="يدوي")
        if ok:
            st.success(msg)
            st.session_state.manual_lines = pd.DataFrame(
                [{"رمز الحساب": acc_options[0], "مدين": 0.0, "دائن": 0.0, "بيان السطر": ""}]
            )
            st.rerun()
        else:
            st.error(msg)

# ---------------------------------------------------------------------------
# حركة مبيعات
# ---------------------------------------------------------------------------
elif menu == "🛒 حركة مبيعات":
    st.subheader("تسجيل حركة مبيعات")
    st.caption("يتم توليد القيد تلقائيًا: من حـ / الصندوق أو العملاء ... إلى حـ / إيرادات المبيعات")

    sale_type = st.radio("نوع البيع", ["نقدي", "آجل (على الحساب)"], horizontal=True, key="s_type")

    customer_id = None
    if sale_type == "آجل (على الحساب)":
        with st.expander("➕ إضافة عميل جديد بسرعة", expanded=False):
            nc1, nc2, nc3 = st.columns(3)
            new_c_name = nc1.text_input("اسم العميل", key="new_cust_name")
            new_c_phone = nc2.text_input("رقم الهاتف (اختياري)", key="new_cust_phone")
            new_c_open = nc3.number_input("رصيد افتتاحي (إن وجد)", min_value=0.0, step=0.01, key="new_cust_open")
            if st.button("إضافة العميل", key="add_cust_btn"):
                if new_c_name:
                    add_customer(new_c_name, new_c_phone, new_c_open)
                    st.success("تمت إضافة العميل، اختره من القائمة تحت")
                    st.rerun()
                else:
                    st.error("أدخل اسم العميل")

        customers_df = get_customers_df()
        if customers_df.empty:
            st.warning("لا يوجد عملاء مسجلين بعد، أضف عميلًا من الأعلى أولًا")
        else:
            cust_label = st.selectbox(
                "اختر العميل", customers_df["id"].tolist(),
                format_func=lambda i: customers_df[customers_df["id"] == i]["name"].values[0],
                key="s_customer"
            )
            customer_id = cust_label

    with st.form("sales_form"):
        c1, c2 = st.columns(2)
        s_date = c1.date_input("التاريخ", value=date.today(), key="s_date")
        s_desc = c2.text_input("بيان الحركة", value="بيع بضاعة", key="s_desc")

        debit_account = acc_selectbox(
            "الحساب المدين (النقدية/البنك أو العميل)", "s_debit_acc",
            filter_types=["اصول"],
            default_code="1101" if sale_type == "نقدي" else "1103"
        )
        revenue_account = acc_selectbox("حساب الإيراد", "s_rev_acc", filter_types=["ايرادات"], default_code="4101")
        amount = st.number_input("المبلغ", min_value=0.0, step=0.01, key="s_amount")

        st.markdown("**(اختياري) تسجيل تكلفة البضاعة المباعة**")
        c3, c4 = st.columns(2)
        include_cogs = c3.checkbox("تسجيل قيد تكلفة البضاعة المباعة أيضًا")
        cogs_amount = c4.number_input("تكلفة البضاعة", min_value=0.0, step=0.01, key="s_cogs")

        submitted = st.form_submit_button("✅ ترحيل حركة المبيعات", type="primary")
        if submitted:
            if amount <= 0:
                st.error("الرجاء إدخال مبلغ أكبر من صفر")
            elif sale_type == "آجل (على الحساب)" and customer_id is None:
                st.error("الرجاء اختيار عميل للبيع الآجل")
            else:
                debit_line = {"account_code": debit_account, "debit": amount, "credit": 0, "line_desc": s_desc}
                if sale_type == "آجل (على الحساب)":
                    debit_line["party_type"] = "customer"
                    debit_line["party_id"] = customer_id
                lines = [
                    debit_line,
                    {"account_code": revenue_account, "debit": 0, "credit": amount, "line_desc": s_desc},
                ]
                ok, msg = post_journal_entry(s_date, s_desc, lines, source="مبيعات")
                if ok and include_cogs and cogs_amount > 0:
                    cogs_lines = [
                        {"account_code": "5101", "debit": cogs_amount, "credit": 0, "line_desc": "تكلفة بضاعة مباعة"},
                        {"account_code": "1104", "debit": 0, "credit": cogs_amount, "line_desc": "تخفيض المخزون"},
                    ]
                    post_journal_entry(s_date, "تكلفة البضاعة المباعة", cogs_lines, source="مبيعات")
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)

# ---------------------------------------------------------------------------
# حركة مشتريات
# ---------------------------------------------------------------------------
elif menu == "📦 حركة مشتريات":
    st.subheader("تسجيل حركة مشتريات")
    st.caption("يتم توليد القيد تلقائيًا: من حـ / المخزون إلى حـ / الصندوق أو الموردون")

    purchase_type = st.radio("نوع الشراء", ["نقدي", "آجل (على الحساب)"], horizontal=True, key="p_type")

    supplier_id = None
    if purchase_type == "آجل (على الحساب)":
        with st.expander("➕ إضافة مورد جديد بسرعة", expanded=False):
            ns1, ns2, ns3 = st.columns(3)
            new_s_name = ns1.text_input("اسم المورد", key="new_supp_name")
            new_s_phone = ns2.text_input("رقم الهاتف (اختياري)", key="new_supp_phone")
            new_s_open = ns3.number_input("رصيد افتتاحي (إن وجد)", min_value=0.0, step=0.01, key="new_supp_open")
            if st.button("إضافة المورد", key="add_supp_btn"):
                if new_s_name:
                    add_supplier(new_s_name, new_s_phone, new_s_open)
                    st.success("تمت إضافة المورد، اختره من القائمة تحت")
                    st.rerun()
                else:
                    st.error("أدخل اسم المورد")

        suppliers_df = get_suppliers_df()
        if suppliers_df.empty:
            st.warning("لا يوجد موردون مسجلون بعد، أضف موردًا من الأعلى أولًا")
        else:
            supplier_id = st.selectbox(
                "اختر المورد", suppliers_df["id"].tolist(),
                format_func=lambda i: suppliers_df[suppliers_df["id"] == i]["name"].values[0],
                key="p_supplier"
            )

    with st.form("purchase_form"):
        c1, c2 = st.columns(2)
        p_date = c1.date_input("التاريخ", value=date.today(), key="p_date")
        p_desc = c2.text_input("بيان الحركة", value="شراء بضاعة", key="p_desc")

        credit_account = acc_selectbox(
            "الحساب الدائن (النقدية/البنك أو المورد)", "p_credit_acc",
            filter_types=["اصول", "خصوم"],
            default_code="1101" if purchase_type == "نقدي" else "2101"
        )
        debit_account = acc_selectbox("الحساب المدين (المخزون / أصل)", "p_debit_acc",
                                       filter_types=["اصول"], default_code="1104")
        amount = st.number_input("المبلغ", min_value=0.0, step=0.01, key="p_amount")

        submitted = st.form_submit_button("✅ ترحيل حركة المشتريات", type="primary")
        if submitted:
            if amount <= 0:
                st.error("الرجاء إدخال مبلغ أكبر من صفر")
            elif purchase_type == "آجل (على الحساب)" and supplier_id is None:
                st.error("الرجاء اختيار مورد للشراء الآجل")
            else:
                credit_line = {"account_code": credit_account, "debit": 0, "credit": amount, "line_desc": p_desc}
                if purchase_type == "آجل (على الحساب)":
                    credit_line["party_type"] = "supplier"
                    credit_line["party_id"] = supplier_id
                lines = [
                    {"account_code": debit_account, "debit": amount, "credit": 0, "line_desc": p_desc},
                    credit_line,
                ]
                ok, msg = post_journal_entry(p_date, p_desc, lines, source="مشتريات")
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)

# ---------------------------------------------------------------------------
# حركة صندوق (قبض / صرف)
# ---------------------------------------------------------------------------
elif menu == "💰 حركة صندوق (قبض / صرف)":
    st.subheader("سند قبض / سند صرف")

    voucher_type = st.radio("نوع السند", ["سند قبض (وارد للصندوق)", "سند صرف (خارج من الصندوق)"],
                             horizontal=True, key="v_type")

    link_party = st.checkbox("🔗 ربط السند بعميل أو مورد (تحصيل/سداد ذمة)")
    party_type, party_id, other_default = None, None, None
    if link_party:
        if voucher_type.startswith("سند قبض"):
            st.caption("تحصيل من عميل → يُخفّض رصيد العميل تلقائيًا")
            customers_df = get_customers_df()
            if customers_df.empty:
                st.warning("لا يوجد عملاء مسجلون بعد")
            else:
                party_id = st.selectbox("اختر العميل", customers_df["id"].tolist(),
                                         format_func=lambda i: customers_df[customers_df["id"] == i]["name"].values[0],
                                         key="v_customer")
                party_type = "customer"
                other_default = AR_ACCOUNT
        else:
            st.caption("سداد لمورد → يُخفّض رصيد المورد تلقائيًا")
            suppliers_df = get_suppliers_df()
            if suppliers_df.empty:
                st.warning("لا يوجد موردون مسجلون بعد")
            else:
                party_id = st.selectbox("اختر المورد", suppliers_df["id"].tolist(),
                                         format_func=lambda i: suppliers_df[suppliers_df["id"] == i]["name"].values[0],
                                         key="v_supplier")
                party_type = "supplier"
                other_default = AP_ACCOUNT

    with st.form("cash_form"):
        c1, c2 = st.columns(2)
        c_date = c1.date_input("التاريخ", value=date.today(), key="c_date")
        c_desc = c2.text_input("بيان الحركة", key="c_desc")

        cash_account = acc_selectbox("حساب الصندوق / البنك", "c_cash_acc",
                                      filter_types=["اصول"], default_code="1101")
        if link_party and party_id is not None:
            other_account = other_default
            st.info(f"الحساب الآخر: {acc_map.get(other_account, other_account)} (تلقائي حسب الطرف المختار)")
        else:
            other_account = acc_selectbox("الحساب الآخر (العميل / المورد / أي حساب)", "c_other_acc",
                                           filter_types=["اصول", "خصوم", "حقوق الملكية", "ايرادات", "مصروفات"])
        amount = st.number_input("المبلغ", min_value=0.0, step=0.01, key="c_amount")

        submitted = st.form_submit_button("✅ ترحيل السند", type="primary")
        if submitted:
            if amount <= 0:
                st.error("الرجاء إدخال مبلغ أكبر من صفر")
            elif link_party and party_id is None:
                st.error("الرجاء اختيار عميل/مورد أو إلغاء تفعيل الربط")
            else:
                other_line = {"account_code": other_account}
                if link_party and party_id is not None:
                    other_line["party_type"] = party_type
                    other_line["party_id"] = party_id
                if voucher_type.startswith("سند قبض"):
                    lines = [
                        {"account_code": cash_account, "debit": amount, "credit": 0, "line_desc": c_desc},
                        {**other_line, "debit": 0, "credit": amount, "line_desc": c_desc},
                    ]
                else:
                    lines = [
                        {**other_line, "debit": amount, "credit": 0, "line_desc": c_desc},
                        {"account_code": cash_account, "debit": 0, "credit": amount, "line_desc": c_desc},
                    ]
                ok, msg = post_journal_entry(c_date, c_desc, lines, source="صندوق")
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)

# ---------------------------------------------------------------------------
# مصروفات
# ---------------------------------------------------------------------------
elif menu == "🧾 مصروفات":
    st.subheader("تسجيل مصروف")
    st.caption("يتم توليد القيد تلقائيًا: من حـ / المصروف إلى حـ / الصندوق أو الدائنون")

    with st.form("expense_form"):
        c1, c2 = st.columns(2)
        e_date = c1.date_input("التاريخ", value=date.today(), key="e_date")
        e_desc = c2.text_input("بيان المصروف", key="e_desc")

        expense_account = acc_selectbox("نوع المصروف", "e_exp_acc", filter_types=["مصروفات"])
        pay_type = st.radio("طريقة الدفع", ["نقدًا", "آجل (مستحق)"], horizontal=True)
        pay_account = acc_selectbox(
            "الحساب الدائن", "e_pay_acc",
            filter_types=["اصول", "خصوم"],
            default_code="1101" if pay_type == "نقدًا" else "2103"
        )
        amount = st.number_input("المبلغ", min_value=0.0, step=0.01, key="e_amount")

        submitted = st.form_submit_button("✅ ترحيل المصروف", type="primary")
        if submitted:
            if amount <= 0:
                st.error("الرجاء إدخال مبلغ أكبر من صفر")
            else:
                lines = [
                    {"account_code": expense_account, "debit": amount, "credit": 0, "line_desc": e_desc},
                    {"account_code": pay_account, "debit": 0, "credit": amount, "line_desc": e_desc},
                ]
                ok, msg = post_journal_entry(e_date, e_desc, lines, source="مصاريف")
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)

# ---------------------------------------------------------------------------
# إيرادات أخرى
# ---------------------------------------------------------------------------
elif menu == "💵 إيرادات أخرى":
    st.subheader("تسجيل إيراد (غير المبيعات)")
    st.caption("يتم توليد القيد تلقائيًا: من حـ / الصندوق أو العملاء إلى حـ / الإيراد")

    with st.form("revenue_form"):
        c1, c2 = st.columns(2)
        r_date = c1.date_input("التاريخ", value=date.today(), key="r_date")
        r_desc = c2.text_input("بيان الإيراد", key="r_desc")

        receive_type = st.radio("طريقة القبض", ["نقدًا", "آجل (مستحق قبض)"], horizontal=True)
        receive_account = acc_selectbox(
            "الحساب المدين", "r_recv_acc",
            filter_types=["اصول"],
            default_code="1101" if receive_type == "نقدًا" else "1103"
        )
        revenue_account = acc_selectbox("حساب الإيراد", "r_rev_acc", filter_types=["ايرادات"], default_code="4102")
        amount = st.number_input("المبلغ", min_value=0.0, step=0.01, key="r_amount")

        submitted = st.form_submit_button("✅ ترحيل الإيراد", type="primary")
        if submitted:
            if amount <= 0:
                st.error("الرجاء إدخال مبلغ أكبر من صفر")
            else:
                lines = [
                    {"account_code": receive_account, "debit": amount, "credit": 0, "line_desc": r_desc},
                    {"account_code": revenue_account, "debit": 0, "credit": amount, "line_desc": r_desc},
                ]
                ok, msg = post_journal_entry(r_date, r_desc, lines, source="ايرادات")
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)

# ---------------------------------------------------------------------------
# العملاء (ذمم مدينة)
# ---------------------------------------------------------------------------
elif menu == "👥 العملاء (ذمم مدينة)":
    st.subheader("العملاء - الذمم المدينة (Accounts Receivable)")

    with st.expander("➕ إضافة عميل جديد"):
        c1, c2, c3 = st.columns(3)
        n_name = c1.text_input("اسم العميل", key="ar_new_name")
        n_phone = c2.text_input("رقم الهاتف", key="ar_new_phone")
        n_open = c3.number_input("رصيد افتتاحي", min_value=0.0, step=0.01, key="ar_new_open")
        if st.button("إضافة", key="ar_add_btn"):
            if n_name:
                add_customer(n_name, n_phone, n_open)
                st.success("تمت الإضافة")
                st.rerun()
            else:
                st.error("أدخل اسم العميل")

    customers_df = get_customers_df()
    if customers_df.empty:
        st.info("لا يوجد عملاء مسجلون بعد")
    else:
        st.markdown("### إجمالي أرصدة العملاء")
        summary_rows = []
        for _, cust in customers_df.iterrows():
            bal = party_balance("customer", cust["id"], AR_ACCOUNT, cust["opening_balance"])
            summary_rows.append({"العميل": cust["name"], "الهاتف": cust["phone"], "الرصيد المستحق": round(bal, 2)})
        summary_df = pd.DataFrame(summary_rows)
        st.dataframe(summary_df, use_container_width=True, hide_index=True)
        st.metric("إجمالي الذمم المدينة", f"{summary_df['الرصيد المستحق'].sum():,.2f}")

        st.divider()
        st.markdown("### 🔍 كشف حساب عميل وتحليل أعمار الديون")
        sel_cust = st.selectbox("اختر العميل", customers_df["id"].tolist(),
                                 format_func=lambda i: customers_df[customers_df["id"] == i]["name"].values[0],
                                 key="ar_sel_cust")
        as_of_date = st.date_input("حتى تاريخ", value=date.today(), key="ar_as_of")
        cust_row = customers_df[customers_df["id"] == sel_cust].iloc[0]

        ledger = get_party_ledger("customer", sel_cust, AR_ACCOUNT)
        st.markdown("**كشف الحساب**")
        if ledger.empty and cust_row["opening_balance"] == 0:
            st.info("لا توجد حركات على هذا العميل")
        else:
            st.dataframe(ledger, use_container_width=True, hide_index=True)

        aging_df, buckets = party_aging("customer", sel_cust, AR_ACCOUNT, cust_row["opening_balance"], as_of_date)
        st.markdown("**تحليل أعمار الديون (Aging)**")
        b1, b2, b3, b4 = st.columns(4)
        b1.metric("0-30 يوم", f"{buckets['0-30 يوم']:,.2f}")
        b2.metric("31-60 يوم", f"{buckets['31-60 يوم']:,.2f}")
        b3.metric("61-90 يوم", f"{buckets['61-90 يوم']:,.2f}")
        b4.metric("أكثر من 90 يوم", f"{buckets['أكثر من 90 يوم']:,.2f}")
        if not aging_df.empty:
            st.dataframe(aging_df, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# الموردون (ذمم دائنة)
# ---------------------------------------------------------------------------
elif menu == "🏭 الموردون (ذمم دائنة)":
    st.subheader("الموردون - الذمم الدائنة (Accounts Payable)")

    with st.expander("➕ إضافة مورد جديد"):
        c1, c2, c3 = st.columns(3)
        n_name = c1.text_input("اسم المورد", key="ap_new_name")
        n_phone = c2.text_input("رقم الهاتف", key="ap_new_phone")
        n_open = c3.number_input("رصيد افتتاحي", min_value=0.0, step=0.01, key="ap_new_open")
        if st.button("إضافة", key="ap_add_btn"):
            if n_name:
                add_supplier(n_name, n_phone, n_open)
                st.success("تمت الإضافة")
                st.rerun()
            else:
                st.error("أدخل اسم المورد")

    suppliers_df = get_suppliers_df()
    if suppliers_df.empty:
        st.info("لا يوجد موردون مسجلون بعد")
    else:
        st.markdown("### إجمالي أرصدة الموردين")
        summary_rows = []
        for _, sup in suppliers_df.iterrows():
            bal = party_balance("supplier", sup["id"], AP_ACCOUNT, sup["opening_balance"])
            summary_rows.append({"المورد": sup["name"], "الهاتف": sup["phone"], "الرصيد المستحق": round(bal, 2)})
        summary_df = pd.DataFrame(summary_rows)
        st.dataframe(summary_df, use_container_width=True, hide_index=True)
        st.metric("إجمالي الذمم الدائنة", f"{summary_df['الرصيد المستحق'].sum():,.2f}")

        st.divider()
        st.markdown("### 🔍 كشف حساب مورد وتحليل أعمار الديون")
        sel_sup = st.selectbox("اختر المورد", suppliers_df["id"].tolist(),
                                format_func=lambda i: suppliers_df[suppliers_df["id"] == i]["name"].values[0],
                                key="ap_sel_sup")
        as_of_date = st.date_input("حتى تاريخ", value=date.today(), key="ap_as_of")
        sup_row = suppliers_df[suppliers_df["id"] == sel_sup].iloc[0]

        ledger = get_party_ledger("supplier", sel_sup, AP_ACCOUNT)
        st.markdown("**كشف الحساب**")
        if ledger.empty and sup_row["opening_balance"] == 0:
            st.info("لا توجد حركات على هذا المورد")
        else:
            st.dataframe(ledger, use_container_width=True, hide_index=True)

        aging_df, buckets = party_aging("supplier", sel_sup, AP_ACCOUNT, sup_row["opening_balance"], as_of_date)
        st.markdown("**تحليل أعمار الديون (Aging)**")
        b1, b2, b3, b4 = st.columns(4)
        b1.metric("0-30 يوم", f"{buckets['0-30 يوم']:,.2f}")
        b2.metric("31-60 يوم", f"{buckets['31-60 يوم']:,.2f}")
        b3.metric("61-90 يوم", f"{buckets['61-90 يوم']:,.2f}")
        b4.metric("أكثر من 90 يوم", f"{buckets['أكثر من 90 يوم']:,.2f}")
        if not aging_df.empty:
            st.dataframe(aging_df, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# الأصول الثابتة والإهلاك
# ---------------------------------------------------------------------------
elif menu == "🏢 الأصول الثابتة والإهلاك":
    st.subheader("سجل الأصول الثابتة والإهلاك (طريقة القسط الثابت)")

    with st.expander("➕ إضافة أصل ثابت جديد"):
        with st.form("add_asset_form"):
            c1, c2 = st.columns(2)
            a_name = c1.text_input("اسم الأصل (مثال: سيارة تويوتا 2024)")
            a_date = c2.date_input("تاريخ الشراء", value=date.today())

            c3, c4, c5 = st.columns(3)
            a_cost = c3.number_input("تكلفة الشراء", min_value=0.0, step=0.01)
            a_salvage = c4.number_input("القيمة التخريدية (المتبقية)", min_value=0.0, step=0.01)
            a_life = c5.number_input("العمر الإنتاجي (بالأشهر)", min_value=1, step=1, value=60)

            a_asset_acc = acc_selectbox("حساب الأصل", "a_asset_acc", filter_types=["اصول"], default_code="1201")
            a_accum_acc = acc_selectbox("حساب مجمع الإهلاك", "a_accum_acc", filter_types=["اصول"], default_code="1203")
            a_exp_acc = acc_selectbox("حساب مصروف الإهلاك", "a_exp_acc", filter_types=["مصروفات"], default_code="5107")
            a_funding_acc = acc_selectbox("طريقة التمويل (دفع من)", "a_funding_acc",
                                           filter_types=["اصول", "خصوم"], default_code="1101")

            submitted = st.form_submit_button("✅ إضافة الأصل وترحيل قيد الشراء", type="primary")
            if submitted:
                if not a_name or a_cost <= 0:
                    st.error("أدخل اسم الأصل وتكلفة أكبر من صفر")
                else:
                    add_fixed_asset(a_name, a_asset_acc, a_accum_acc, a_exp_acc, a_date,
                                     a_cost, a_salvage, int(a_life), a_funding_acc)
                    st.success("تمت إضافة الأصل وترحيل قيد الشراء")
                    st.rerun()

    assets_df = get_fixed_assets_df()
    if assets_df.empty:
        st.info("لا توجد أصول ثابتة مسجلة بعد")
    else:
        st.markdown("### سجل الأصول")
        display_rows = []
        for _, a in assets_df.iterrows():
            display_rows.append({
                "id": a["id"], "الاسم": a["name"], "تاريخ الشراء": a["purchase_date"],
                "التكلفة": a["cost"], "القيمة التخريدية": a["salvage_value"],
                "العمر (شهر)": a["useful_life_months"],
                "الإهلاك الشهري": monthly_depreciation_amount(a),
                "مجمع الإهلاك": a["accumulated_depreciation"],
                "صافي القيمة الدفترية": a["cost"] - a["accumulated_depreciation"],
            })
        disp_df = pd.DataFrame(display_rows)
        st.dataframe(disp_df.drop(columns=["id"]), use_container_width=True, hide_index=True)

        st.divider()
        st.markdown("### 📆 ترحيل قيد الإهلاك الدوري")
        c1, c2 = st.columns(2)
        dep_period = c1.date_input("تاريخ فترة الإهلاك", value=date.today(), key="dep_date")
        dep_mode = c2.radio("نطاق الترحيل", ["أصل واحد", "كل الأصول دفعة واحدة"], horizontal=True)
        force_dep = st.checkbox("⚠️ السماح بترحيل إهلاك إضافي حتى لو رُحّل لنفس الشهر مسبقًا (تصحيح/استدراك)")

        if dep_mode == "أصل واحد":
            sel_asset = st.selectbox("اختر الأصل", assets_df["id"].tolist(),
                                      format_func=lambda i: assets_df[assets_df["id"] == i]["name"].values[0])
            if st.button("✅ ترحيل قيد الإهلاك", type="primary"):
                ok, msg = post_depreciation_entry(sel_asset, dep_period, force=force_dep)
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.warning(msg)
        else:
            if st.button("✅ ترحيل إهلاك كل الأصول لهذه الفترة", type="primary"):
                results = []
                for aid in assets_df["id"].tolist():
                    ok, msg = post_depreciation_entry(aid, dep_period, force=force_dep)
                    results.append(msg)
                for r in results:
                    st.write("- " + r)
                st.rerun()

        st.divider()
        st.markdown("### 📜 سجل قيود الإهلاك السابقة")
        log_df = get_depreciation_log_df()
        if log_df.empty:
            st.info("لا توجد قيود إهلاك مرحّلة بعد")
        else:
            st.dataframe(log_df, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# التسوية البنكية (Reconciliation)
# ---------------------------------------------------------------------------
elif menu == "🔄 التسوية البنكية":
    st.subheader("التسوية البنكية / تسوية الصندوق")
    st.caption("قارن رصيد الدفاتر برصيد كشف الحساب البنكي، وحدد الحركات غير المطابقة (المعلّقة)")

    cash_bank_accounts = get_accounts_df()
    cash_bank_accounts = leaf_accounts_only(cash_bank_accounts)
    cash_bank_accounts = cash_bank_accounts[cash_bank_accounts["type"] == "اصول"]

    sel_account = st.selectbox("اختر حساب الصندوق / البنك المراد تسويته",
                                cash_bank_accounts["code"].tolist(),
                                format_func=lambda c: acc_map.get(c, c),
                                index=cash_bank_accounts["code"].tolist().index("1101")
                                if "1101" in cash_bank_accounts["code"].tolist() else 0)

    c1, c2 = st.columns(2)
    recon_date = c1.date_input("تسوية حتى تاريخ", value=date.today(), key="recon_date")
    statement_balance = c2.number_input("الرصيد حسب كشف الحساب البنكي", step=0.01, key="recon_stmt_balance")

    df = get_reconcile_lines(sel_account, recon_date)
    if df.empty:
        st.info("لا توجد حركات على هذا الحساب حتى هذا التاريخ")
    else:
        df_display = df.copy()
        df_display["مطابق"] = df_display["مطابق"].astype(bool)

        edited = st.data_editor(
            df_display[["line_id", "التاريخ", "رقم_القيد", "البيان", "مدين", "دائن", "مطابق"]],
            use_container_width=True, hide_index=True, key="recon_editor",
            column_config={
                "line_id": None,  # إخفاء العمود
                "مطابق": st.column_config.CheckboxColumn("مطابق مع كشف الحساب؟"),
            },
            disabled=["التاريخ", "رقم_القيد", "البيان", "مدين", "دائن"]
        )

        if st.button("💾 حفظ حالة المطابقة", type="primary"):
            for _, row in edited.iterrows():
                set_line_reconciled(int(row["line_id"]), bool(row["مطابق"]))
            st.success("تم حفظ حالة المطابقة")
            st.rerun()

        book_balance = df["مدين"].sum() - df["دائن"].sum()
        unreconciled = df[df["مطابق"] == 0]
        outstanding_debits = unreconciled["مدين"].sum()    # إيداعات لم تظهر بكشف الحساب بعد
        outstanding_credits = unreconciled["دائن"].sum()   # شيكات/سحوبات لم تظهر بكشف الحساب بعد

        adjusted_book_balance = book_balance
        reconciled_balance = book_balance - outstanding_debits + outstanding_credits

        st.divider()
        st.markdown("### 📋 ملخص التسوية")
        c1, c2, c3 = st.columns(3)
        c1.metric("رصيد الدفاتر (دفتر الأستاذ)", f"{book_balance:,.2f}")
        c2.metric("رصيد كشف الحساب البنكي", f"{statement_balance:,.2f}")
        diff = statement_balance - book_balance
        c3.metric("الفرق قبل التسوية", f"{diff:,.2f}")

        st.write(f"➕ مبالغ مقيدة بدفاترنا ولم تظهر بكشف الحساب بعد (إيداعات معلّقة): **{outstanding_debits:,.2f}**")
        st.write(f"➖ مبالغ مقيدة بدفاترنا ولم تُخصم من كشف الحساب بعد (شيكات/سحوبات معلّقة): **{outstanding_credits:,.2f}**")

        final_diff = statement_balance - reconciled_balance
        if abs(final_diff) < 0.01:
            st.success(f"✅ التسوية متطابقة! الرصيد المسوّى = {reconciled_balance:,.2f}")
        else:
            st.error(f"⚠️ لا يزال هناك فرق غير مفسّر = {final_diff:,.2f} — راجع القيود يدويًا")

# ---------------------------------------------------------------------------
# دفتر اليومية
# ---------------------------------------------------------------------------
elif menu == "📚 دفتر اليومية":
    st.subheader("دفتر اليومية العام")

    c1, c2 = st.columns(2)
    d_from = c1.date_input("من تاريخ", value=None, key="j_from")
    d_to = c2.date_input("إلى تاريخ", value=None, key="j_to")

    df = get_journal_df(d_from if d_from else None, d_to if d_to else None)
    if df.empty:
        st.info("لا توجد قيود ضمن هذا النطاق")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)

        st.divider()
        st.subheader("🗑️ حذف قيد")
        entry_ids = sorted(df["رقم_القيد"].unique(), reverse=True)
        del_id = st.selectbox("اختر رقم القيد للحذف", entry_ids)
        if st.button("حذف القيد نهائيًا", type="secondary"):
            delete_journal_entry(int(del_id))
            st.success(f"تم حذف القيد رقم {del_id}")
            st.rerun()

# ---------------------------------------------------------------------------
# دفتر الأستاذ
# ---------------------------------------------------------------------------
elif menu == "📖 دفتر الأستاذ":
    st.subheader("دفتر الأستاذ (حسب الحساب)")

    df_accounts = leaf_accounts_only(get_accounts_df())
    sel_code = st.selectbox("اختر الحساب", df_accounts["code"].tolist(),
                             format_func=lambda c: acc_map.get(c, c))
    c1, c2 = st.columns(2)
    d_from = c1.date_input("من تاريخ", value=None, key="l_from")
    d_to = c2.date_input("إلى تاريخ", value=None, key="l_to")

    if sel_code:
        acc_row = df_accounts[df_accounts["code"] == sel_code].iloc[0]
        df = get_ledger_df(sel_code, d_from if d_from else None, d_to if d_to else None)
        if df.empty:
            st.info("لا توجد حركات على هذا الحساب")
        else:
            df = df.copy()
            running = 0
            balances = []
            for _, r in df.iterrows():
                if acc_row["nature"] == "مدين":
                    running += r["مدين"] - r["دائن"]
                else:
                    running += r["دائن"] - r["مدين"]
                balances.append(running)
            df["الرصيد التراكمي"] = balances
            st.dataframe(df, use_container_width=True, hide_index=True)

            final_balance = balances[-1]
            st.metric(f"الرصيد النهائي - {acc_row['name']}", f"{final_balance:,.2f} ({acc_row['nature']})")

# ---------------------------------------------------------------------------
# ميزان المراجعة
# ---------------------------------------------------------------------------
elif menu == "⚖️ ميزان المراجعة":
    st.subheader("ميزان المراجعة")

    c1, c2 = st.columns(2)
    d_from = c1.date_input("من تاريخ", value=None, key="tb_from")
    d_to = c2.date_input("إلى تاريخ", value=None, key="tb_to")

    df = trial_balance_df(d_from if d_from else None, d_to if d_to else None)
    if df.empty:
        st.info("لا توجد بيانات")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("إجمالي مدين", f"{df['مدين'].sum():,.2f}")
        c2.metric("إجمالي دائن", f"{df['دائن'].sum():,.2f}")
        c3.metric("إجمالي الأرصدة المدينة", f"{df['الرصيد مدين'].sum():,.2f}")
        c4.metric("إجمالي الأرصدة الدائنة", f"{df['الرصيد دائن'].sum():,.2f}")

        diff = df["الرصيد مدين"].sum() - df["الرصيد دائن"].sum()
        if abs(diff) < 0.01:
            st.success("✅ الميزان متوازن")
        else:
            st.error(f"⚠️ الميزان غير متوازن، الفرق = {diff:,.2f}")

# ---------------------------------------------------------------------------
# قائمة الدخل
# ---------------------------------------------------------------------------
elif menu == "📈 قائمة الدخل":
    st.subheader("قائمة الدخل")

    c1, c2 = st.columns(2)
    d_from = c1.date_input("من تاريخ", value=None, key="is_from")
    d_to = c2.date_input("إلى تاريخ", value=None, key="is_to")

    rev_df, total_rev, exp_df, total_exp, net_income = income_statement(
        d_from if d_from else None, d_to if d_to else None
    )

    st.markdown("### الإيرادات")
    if not rev_df.empty:
        st.dataframe(rev_df, use_container_width=True, hide_index=True)
    st.write(f"**إجمالي الإيرادات: {total_rev:,.2f}**")

    st.markdown("### المصروفات")
    if not exp_df.empty:
        st.dataframe(exp_df, use_container_width=True, hide_index=True)
    st.write(f"**إجمالي المصروفات: {total_exp:,.2f}**")

    st.divider()
    if net_income >= 0:
        st.success(f"### 💰 صافي الربح: {net_income:,.2f}")
    else:
        st.error(f"### 📉 صافي الخسارة: {net_income:,.2f}")

# ---------------------------------------------------------------------------
# قائمة المركز المالي
# ---------------------------------------------------------------------------
elif menu == "🏦 قائمة المركز المالي":
    st.subheader("قائمة المركز المالي (الميزانية العمومية)")

    c1, c2 = st.columns(2)
    d_from = c1.date_input("من تاريخ", value=None, key="bs_from")
    d_to = c2.date_input("إلى تاريخ", value=None, key="bs_to")

    assets_df, total_assets, liab_df, total_liab, equity_df, total_equity, net_income = balance_sheet(
        d_from if d_from else None, d_to if d_to else None
    )

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("### 🏛️ الأصول")
        if not assets_df.empty:
            st.dataframe(assets_df, use_container_width=True, hide_index=True)
        st.write(f"**إجمالي الأصول: {total_assets:,.2f}**")

    with col2:
        st.markdown("### 📑 الخصوم")
        if not liab_df.empty:
            st.dataframe(liab_df, use_container_width=True, hide_index=True)
        st.write(f"**إجمالي الخصوم: {total_liab:,.2f}**")

        st.markdown("### 🧑‍💼 حقوق الملكية")
        if not equity_df.empty:
            st.dataframe(equity_df, use_container_width=True, hide_index=True)
        st.write(f"صافي دخل الفترة (غير مقفل): {net_income:,.2f}")
        st.write(f"**إجمالي حقوق الملكية: {total_equity:,.2f}**")

    st.divider()
    diff = total_assets - (total_liab + total_equity)
    if abs(diff) < 0.01:
        st.success(f"✅ الميزانية متوازنة | الأصول = الخصوم + حقوق الملكية = {total_assets:,.2f}")
    else:
        st.error(f"⚠️ الميزانية غير متوازنة! الفرق = {diff:,.2f}")
