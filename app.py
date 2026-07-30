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

def _create_conn():
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


@st.cache_resource(show_spinner=False)
def _get_cached_local_conn():
    return _create_conn()


@st.cache_resource(show_spinner=False, ttl=90)
def _get_cached_cloud_conn():
    return _create_conn()


def get_conn():
    """
    قاعدة البيانات السحابية (Turso) تعتمد بروتوكول Hrana القائم على HTTP، وربطها (Stream)
    ينتهي بعد فترة خمول فتفشل الاتصالات المخزّنة مؤقتًا برسالة "stream not found" -
    لذلك نخزّنه مؤقتًا بعمر محدود (TTL=90 ثانية) بدل عدم التخزين إطلاقًا: هذا يعطي نفس سرعة
    التخزين المؤقت العادي ضمن تحميل الصفحة الواحدة (عشرات الاستدعاءات المتتالية)، مع تجديد
    الاتصال تلقائيًا كل دقيقة ونصف قبل ما يموت الـ stream من الخمول. اتصال SQLite المحلي
    يبقى مخزّنًا بلا حد زمني لأنه ما يعاني من نفس مشكلة انتهاء الصلاحية.
    """
    if USE_CLOUD_DB:
        return _get_cached_cloud_conn()
    return _get_cached_local_conn()


def query_df(conn, query, params=None):
    """ينفّذ استعلام SELECT ويرجع DataFrame - يعمل بأمان مع SQLite المحلي وTurso السحابي معًا"""
    try:
        cur = conn.cursor()
        if params:
            cur.execute(query, params)
        else:
            cur.execute(query)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return pd.DataFrame(rows, columns=cols)
    except Exception as e:
        # في حال مات الـ Hrana stream قبل انتهاء صلاحية التخزين المؤقت (TTL) بسبب خمول غير متوقع،
        # نجدد الاتصال ونعيد المحاولة مرة واحدة بدل ما نفشل الصفحة كاملة
        if USE_CLOUD_DB and "stream" in str(e).lower():
            _get_cached_cloud_conn.clear()
            fresh_conn = get_conn()
            cur = fresh_conn.cursor()
            if params:
                cur.execute(query, params)
            else:
                cur.execute(query)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            return pd.DataFrame(rows, columns=cols)
        raise


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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS taxes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            rate REAL NOT NULL,
            tax_type TEXT NOT NULL,        -- مبيعات / مشتريات
            account_code TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            FOREIGN KEY (account_code) REFERENCES accounts(code)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS fiscal_year_closings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fiscal_year TEXT NOT NULL UNIQUE,
            closing_date TEXT NOT NULL,
            journal_id INTEGER,
            net_income REAL,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            journal_id INTEGER,
            details TEXT,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT UNIQUE,
            name TEXT NOT NULL,
            base_unit TEXT NOT NULL DEFAULT 'وحدة',
            cost_price REAL DEFAULT 0,
            sale_price REAL DEFAULT 0,
            quantity_on_hand REAL DEFAULT 0,
            inventory_account_code TEXT NOT NULL,
            revenue_account_code TEXT NOT NULL,
            cogs_account_code TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            FOREIGN KEY (inventory_account_code) REFERENCES accounts(code),
            FOREIGN KEY (revenue_account_code) REFERENCES accounts(code),
            FOREIGN KEY (cogs_account_code) REFERENCES accounts(code)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS product_uoms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            uom_name TEXT NOT NULL,
            factor REAL NOT NULL DEFAULT 1,
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sales_invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference TEXT,
            invoice_date TEXT NOT NULL,
            due_date TEXT,
            customer_id INTEGER,
            payment_type TEXT NOT NULL,
            cash_account_code TEXT,
            tax_id INTEGER,
            project_id INTEGER,
            subtotal REAL NOT NULL,
            tax_amount REAL DEFAULT 0,
            total_amount REAL NOT NULL,
            journal_id INTEGER,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sales_invoice_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            uom_id INTEGER NOT NULL,
            quantity REAL NOT NULL,
            base_quantity REAL NOT NULL,
            unit_price REAL NOT NULL,
            line_total REAL NOT NULL,
            unit_cost REAL DEFAULT 0,
            FOREIGN KEY (invoice_id) REFERENCES sales_invoices(id) ON DELETE CASCADE,
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS purchase_invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference TEXT,
            invoice_date TEXT NOT NULL,
            due_date TEXT,
            supplier_id INTEGER,
            payment_type TEXT NOT NULL,
            cash_account_code TEXT,
            tax_id INTEGER,
            project_id INTEGER,
            subtotal REAL NOT NULL,
            tax_amount REAL DEFAULT 0,
            total_amount REAL NOT NULL,
            journal_id INTEGER,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS purchase_invoice_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL,
            line_type TEXT NOT NULL,          -- مخزون / أصل_ثابت
            product_id INTEGER,
            uom_id INTEGER,
            quantity REAL,
            base_quantity REAL,
            unit_cost REAL,
            line_total REAL NOT NULL,
            description TEXT,
            is_converted INTEGER DEFAULT 0,
            fixed_asset_id INTEGER,
            FOREIGN KEY (invoice_id) REFERENCES purchase_invoices(id) ON DELETE CASCADE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT,
            name TEXT NOT NULL,
            client_name TEXT,
            budget REAL DEFAULT 0,
            start_date TEXT,
            is_active INTEGER DEFAULT 1
        )
    """)
    conn.commit()

    # ترقية قواعد بيانات قديمة (أنشئت قبل إضافة هذه الأعمدة)
    existing_cols = [r[1] for r in cur.execute("PRAGMA table_info(journal_lines)").fetchall()]
    for col, coltype in [("party_type", "TEXT"), ("party_id", "INTEGER"), ("reconciled", "INTEGER DEFAULT 0"),
                          ("project_id", "INTEGER")]:
        if col not in existing_cols:
            cur.execute(f"ALTER TABLE journal_lines ADD COLUMN {col} {coltype}")

    existing_journal_cols = [r[1] for r in cur.execute("PRAGMA table_info(journal)").fetchall()]
    for col, coltype in [("is_reversed", "INTEGER DEFAULT 0"), ("reversed_journal_id", "INTEGER")]:
        if col not in existing_journal_cols:
            cur.execute(f"ALTER TABLE journal ADD COLUMN {col} {coltype}")
    conn.commit()

    # بيانات الشركة الافتراضية (تُعدَّل لاحقًا من صفحة الإعدادات)
    if get_setting("company_name") is None:
        set_setting("company_name", "الكف الفني")
    if get_setting("company_activity") is None:
        set_setting("company_activity", "شركة خدمات إعلانية وتنفيذ مشاريع")
    if get_setting("currency") is None:
        set_setting("currency", "IQD")

    seed_accounts(conn)

    # ضمان وجود حسابات الضريبة حتى على قواعد بيانات موجودة مسبقًا (تم إنشاؤها قبل إضافة الضرائب) —
    # seed_accounts() لا يضيف شيئًا إذا كان جدول الحسابات مُعبّأ مسبقًا
    for code, name, acc_type, nature, parent_code in [
        ("1106", "ضريبة القيمة المضافة - قابلة للخصم (مشتريات)", "اصول", "مدين", "1100"),
        ("2104", "ضريبة القيمة المضافة - مستحقة الدفع (مبيعات)", "خصوم", "دائن", "2100"),
    ]:
        cur.execute("SELECT 1 FROM accounts WHERE code=?", (code,))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO accounts (code, name, type, nature, parent_code) VALUES (?,?,?,?,?)",
                (code, name, acc_type, nature, parent_code)
            )
    conn.commit()

    seed_taxes(conn)


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
        ("1106", "ضريبة القيمة المضافة - قابلة للخصم (مشتريات)", "اصول", "مدين", "1100"),
        ("1200", "الأصول الثابتة", "اصول", "مدين", "1000"),
        ("1201", "أثاث ومعدات", "اصول", "مدين", "1200"),
        ("1202", "سيارات", "اصول", "مدين", "1200"),
        ("1203", "مجمع الإهلاك", "اصول", "دائن", "1200"),

        ("2000", "الخصوم", "خصوم", "دائن", None),
        ("2100", "الخصوم المتداولة", "خصوم", "دائن", "2000"),
        ("2101", "الموردون (الدائنون)", "خصوم", "دائن", "2100"),
        ("2102", "أوراق دفع", "خصوم", "دائن", "2100"),
        ("2103", "مصروفات مستحقة", "خصوم", "دائن", "2100"),
        ("2104", "ضريبة القيمة المضافة - مستحقة الدفع (مبيعات)", "خصوم", "دائن", "2100"),
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


def seed_taxes(conn):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM taxes")
    if cur.fetchone()[0] > 0:
        return
    cur.execute(
        "INSERT INTO taxes (name, rate, tax_type, account_code) VALUES (?,?,?,?)",
        ("ضريبة القيمة المضافة 15% (مبيعات)", 15.0, "مبيعات", "2104")
    )
    cur.execute(
        "INSERT INTO taxes (name, rate, tax_type, account_code) VALUES (?,?,?,?)",
        ("ضريبة القيمة المضافة 15% (مشتريات)", 15.0, "مشتريات", "1106")
    )
    conn.commit()


# ---------------------------------------------------------------------------
# الإعدادات وقفل الفترات المحاسبية وسجل التدقيق
# ---------------------------------------------------------------------------

def get_setting(key, default=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    return row[0] if row else default


def set_setting(key, value):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM settings WHERE key=?", (key,))
    cur.execute("INSERT INTO settings (key, value) VALUES (?,?)", (key, str(value)))
    conn.commit()


def is_date_locked(entry_date):
    lock_date = get_setting("lock_date")
    if not lock_date:
        return False
    return str(entry_date) <= lock_date


def log_audit(action, journal_id, details=""):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO audit_log (action, journal_id, details, created_at) VALUES (?,?,?,?)",
        (action, journal_id, details, datetime.now().isoformat())
    )
    conn.commit()


def get_audit_log_df():
    conn = get_conn()
    q = """
        SELECT a.created_at as التوقيت, a.action as الإجراء, a.journal_id as رقم_القيد, a.details as التفاصيل
        FROM audit_log a ORDER BY a.id DESC
    """
    return query_df(conn, q)


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
# دوال مساعدة - الضرائب (VAT)
# ---------------------------------------------------------------------------

def get_taxes_df(active_only=True, tax_type=None):
    conn = get_conn()
    q = "SELECT * FROM taxes"
    conditions = []
    if active_only:
        conditions.append("is_active = 1")
    if tax_type:
        conditions.append(f"tax_type = '{tax_type}'")
    if conditions:
        q += " WHERE " + " AND ".join(conditions)
    return query_df(conn, q)


def add_tax(name, rate, tax_type, account_code):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO taxes (name, rate, tax_type, account_code) VALUES (?,?,?,?)",
        (name, rate, tax_type, account_code)
    )
    conn.commit()


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
    if is_date_locked(entry_date):
        return False, f"لا يمكن الترحيل: تاريخ {entry_date} يقع ضمن فترة مقفلة (تاريخ القفل: {get_setting('lock_date')})"

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
               (journal_id, account_code, debit, credit, line_desc, party_type, party_id, project_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (journal_id, l["account_code"], l["debit"], l["credit"], l.get("line_desc", ""),
             l.get("party_type"), l.get("party_id"), l.get("project_id"))
        )
    conn.commit()
    log_audit("ترحيل قيد", journal_id, f"مصدر: {source} | بيان: {description}")
    return True, f"تم ترحيل القيد رقم {journal_id} بنجاح"


def reverse_journal_entry(journal_id, reversal_date=None, reason=""):
    """
    بدل الحذف النهائي: ينشئ قيدًا عكسيًا (يبادل المدين بالدائن لكل سطر) ويربطه بالقيد الأصلي.
    القيد الأصلي يبقى بالسجل دائمًا للحفاظ على سلامة الأثر المحاسبي (Audit Trail).
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM journal WHERE id=?", (journal_id,))
    row = cur.fetchone()
    if not row:
        return False, "القيد غير موجود"
    cols = [d[0] for d in cur.description]
    entry = dict(zip(cols, row))

    if entry.get("is_reversed"):
        return False, "هذا القيد معكوس مسبقًا"
    if entry.get("reversed_journal_id"):
        return False, "هذا قيد عكسي بحد ذاته، لا يمكن عكسه مرة أخرى"

    if reversal_date is None:
        reversal_date = date.today()

    lines_df = query_df(conn, "SELECT * FROM journal_lines WHERE journal_id=?", [journal_id])
    reversal_lines = [
        {"account_code": r["account_code"], "debit": r["credit"], "credit": r["debit"],
         "line_desc": r["line_desc"], "party_type": r["party_type"], "party_id": r["party_id"]}
        for _, r in lines_df.iterrows()
    ]
    desc = f"عكس القيد رقم {journal_id} - {entry['description']}" + (f" ({reason})" if reason else "")
    ok, msg = post_journal_entry(reversal_date, desc, reversal_lines, source="عكس قيد")
    if not ok:
        return False, msg

    cur.execute("SELECT id FROM journal ORDER BY id DESC LIMIT 1")
    new_journal_id = cur.fetchone()[0]
    cur.execute("UPDATE journal SET is_reversed=1 WHERE id=?", (journal_id,))
    cur.execute("UPDATE journal SET reversed_journal_id=? WHERE id=?", (journal_id, new_journal_id))
    conn.commit()
    log_audit("عكس قيد", journal_id, f"تم إنشاء قيد عكسي رقم {new_journal_id}" + (f" | السبب: {reason}" if reason else ""))
    return True, f"تم عكس القيد رقم {journal_id} بنجاح عبر القيد الجديد رقم {new_journal_id}"


def get_journal_df(date_from=None, date_to=None):
    conn = get_conn()
    q = """
        SELECT j.id as رقم_القيد, j.entry_date as التاريخ, j.description as البيان,
               j.source as المصدر, jl.account_code as رمز_الحساب,
               a.name as اسم_الحساب, jl.debit as مدين, jl.credit as دائن,
               jl.line_desc as بيان_السطر,
               j.is_reversed as معكوس, j.reversed_journal_id as قيد_عكس_رقم
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
                     funding_account, post_opening_entry=True, party_type=None, party_id=None):
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
        funding_line = {"account_code": funding_account, "debit": 0, "credit": cost, "line_desc": f"شراء أصل: {name}"}
        if party_type and party_id:
            funding_line["party_type"] = party_type
            funding_line["party_id"] = party_id
        lines = [
            {"account_code": asset_account, "debit": cost, "credit": 0, "line_desc": f"شراء أصل: {name}"},
            funding_line,
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
# المنتجات والمخزون ووحدات القياس
# ---------------------------------------------------------------------------

def get_products_df(active_only=True):
    conn = get_conn()
    q = "SELECT * FROM products"
    if active_only:
        q += " WHERE is_active = 1"
    q += " ORDER BY name"
    return query_df(conn, q)


def add_product(name, base_unit, inventory_account, revenue_account, cogs_account,
                 sale_price=0.0, sku="", opening_qty=0.0, opening_cost=0.0):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO products (sku, name, base_unit, cost_price, sale_price, quantity_on_hand,
           inventory_account_code, revenue_account_code, cogs_account_code)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (sku.strip() or None, name, base_unit, opening_cost, sale_price, opening_qty,
         inventory_account, revenue_account, cogs_account)
    )
    product_id = cur.lastrowid
    cur.execute("INSERT INTO product_uoms (product_id, uom_name, factor) VALUES (?,?,1)",
                (product_id, base_unit))
    conn.commit()
    return product_id


def get_product_uoms_df(product_id):
    conn = get_conn()
    return query_df(conn, "SELECT * FROM product_uoms WHERE product_id=? ORDER BY factor", [product_id])


def add_product_uom(product_id, uom_name, factor):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO product_uoms (product_id, uom_name, factor) VALUES (?,?,?)",
                (product_id, uom_name, factor))
    conn.commit()


def product_uom_options_df():
    """كل تركيبات (منتج × وحدة قياس) المتاحة للاختيار بفواتير المبيعات/المشتريات"""
    conn = get_conn()
    q = """
        SELECT pu.id as uom_id, pu.product_id, p.name as product_name, pu.uom_name, pu.factor,
               p.sale_price, p.cost_price, p.inventory_account_code, p.revenue_account_code, p.cogs_account_code
        FROM product_uoms pu JOIN products p ON p.id = pu.product_id
        WHERE p.is_active = 1
        ORDER BY p.name, pu.factor
    """
    return query_df(conn, q)


# ---------------------------------------------------------------------------
# فواتير المبيعات (متعددة البنود مع خصم المخزون وتكلفة البضاعة المباعة)
# ---------------------------------------------------------------------------

def post_sales_invoice(invoice_date, customer_id, payment_type, cash_account, tax_id, lines, project_id=None,
                        due_date=None, reference=""):
    """lines: [{"product_id", "uom_id", "quantity", "unit_price"}]"""
    if not lines:
        return False, "لا توجد بنود بالفاتورة"

    conn = get_conn()
    cur = conn.cursor()

    enriched = []
    subtotal = 0.0
    total_cogs = 0.0
    for l in lines:
        cur.execute("SELECT * FROM products WHERE id=?", (l["product_id"],))
        prow = cur.fetchone()
        pcols = [d[0] for d in cur.description]
        product = dict(zip(pcols, prow))
        cur.execute("SELECT * FROM product_uoms WHERE id=?", (l["uom_id"],))
        urow = cur.fetchone()
        ucols = [d[0] for d in cur.description]
        uom = dict(zip(ucols, urow))

        base_qty = l["quantity"] * uom["factor"]
        line_total = round(l["quantity"] * l["unit_price"], 2)
        line_cogs = round(base_qty * product["cost_price"], 2)
        subtotal += line_total
        total_cogs += line_cogs
        enriched.append({**l, "product": product, "uom": uom, "base_quantity": base_qty,
                          "line_total": line_total, "line_cogs": line_cogs})

    tax_amount = 0.0
    tax_row = None
    if tax_id:
        cur.execute("SELECT * FROM taxes WHERE id=?", (tax_id,))
        trow = cur.fetchone()
        tcols = [d[0] for d in cur.description]
        tax_row = dict(zip(tcols, trow))
        tax_amount = round(subtotal * float(tax_row["rate"]) / 100, 2)

    total_amount = round(subtotal + tax_amount, 2)

    debit_account = cash_account if payment_type == "نقدي" else AR_ACCOUNT
    debit_line = {"account_code": debit_account, "debit": total_amount, "credit": 0, "line_desc": "فاتورة مبيعات"}
    if payment_type != "نقدي":
        debit_line["party_type"] = "customer"
        debit_line["party_id"] = customer_id

    je_lines = [debit_line]
    revenue_totals = {}
    for e in enriched:
        acc = e["product"]["revenue_account_code"]
        revenue_totals[acc] = revenue_totals.get(acc, 0) + e["line_total"]
    for acc, amt in revenue_totals.items():
        je_lines.append({"account_code": acc, "debit": 0, "credit": amt, "line_desc": "إيراد فاتورة مبيعات",
                          "project_id": project_id})

    if tax_amount > 0:
        je_lines.append({"account_code": tax_row["account_code"], "debit": 0, "credit": tax_amount,
                          "line_desc": tax_row["name"]})

    je_desc = f"فاتورة مبيعات - {reference}" if reference else "فاتورة مبيعات"
    ok, msg = post_journal_entry(invoice_date, je_desc, je_lines, source="فاتورة مبيعات")
    if not ok:
        return False, msg
    cur.execute("SELECT id FROM journal ORDER BY id DESC LIMIT 1")
    journal_id = cur.fetchone()[0]

    if total_cogs > 0:
        cogs_dr, cogs_cr = {}, {}
        for e in enriched:
            if e["line_cogs"] <= 0:
                continue
            cogs_dr[e["product"]["cogs_account_code"]] = cogs_dr.get(e["product"]["cogs_account_code"], 0) + e["line_cogs"]
            cogs_cr[e["product"]["inventory_account_code"]] = cogs_cr.get(e["product"]["inventory_account_code"], 0) + e["line_cogs"]
        cogs_lines = [{"account_code": acc, "debit": amt, "credit": 0, "line_desc": "تكلفة بضاعة مباعة",
                       "project_id": project_id}
                      for acc, amt in cogs_dr.items()]
        cogs_lines += [{"account_code": acc, "debit": 0, "credit": amt, "line_desc": "تخفيض المخزون"}
                       for acc, amt in cogs_cr.items()]
        post_journal_entry(invoice_date, "تكلفة البضاعة المباعة - فاتورة مبيعات", cogs_lines, source="فاتورة مبيعات")

    cur.execute(
        """INSERT INTO sales_invoices (reference, invoice_date, due_date, customer_id, payment_type,
           cash_account_code, tax_id, project_id, subtotal, tax_amount, total_amount, journal_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (reference, str(invoice_date), str(due_date) if due_date else str(invoice_date), customer_id, payment_type,
         cash_account if payment_type == "نقدي" else None,
         tax_id, project_id, subtotal, tax_amount, total_amount, journal_id, datetime.now().isoformat())
    )
    invoice_id = cur.lastrowid
    for e in enriched:
        cur.execute(
            """INSERT INTO sales_invoice_lines (invoice_id, product_id, uom_id, quantity, base_quantity,
               unit_price, line_total, unit_cost) VALUES (?,?,?,?,?,?,?,?)""",
            (invoice_id, e["product_id"], e["uom_id"], e["quantity"], e["base_quantity"],
             e["unit_price"], e["line_total"], e["product"]["cost_price"])
        )
        cur.execute("UPDATE products SET quantity_on_hand = quantity_on_hand - ? WHERE id=?",
                    (e["base_quantity"], e["product_id"]))
    conn.commit()
    return True, f"تم ترحيل فاتورة المبيعات رقم {invoice_id} بنجاح (الإجمالي: {total_amount:,.2f})"


def get_sales_invoices_df():
    conn = get_conn()
    q = """
        SELECT si.id as رقم_الفاتورة, si.invoice_date as التاريخ, c.name as العميل,
               p.name as المشروع,
               si.payment_type as طريقة_الدفع, si.subtotal as الإجمالي_قبل_الضريبة,
               si.tax_amount as الضريبة, si.total_amount as الإجمالي, si.journal_id as رقم_القيد
        FROM sales_invoices si LEFT JOIN customers c ON c.id = si.customer_id
        LEFT JOIN projects p ON p.id = si.project_id
        ORDER BY si.id DESC
    """
    return query_df(conn, q)


# ---------------------------------------------------------------------------
# فواتير المشتريات (بنود مخزون + بنود أصول ثابتة بانتظار التحويل)
# ---------------------------------------------------------------------------

def post_purchase_invoice(invoice_date, supplier_id, payment_type, cash_account, tax_id, lines, project_id=None,
                           due_date=None, reference=""):
    """
    lines: بنود مخزون {"line_type": "مخزون", "product_id", "uom_id", "quantity", "unit_cost"}
           أو بنود أصول ثابتة {"line_type": "أصل_ثابت", "description", "line_total"}
    بنود الأصول الثابتة لا تُرحّل بقيد فوري، بل تبقى بانتظار تحويلها لموديل الإهلاك.
    """
    if not lines:
        return False, "لا توجد بنود بالفاتورة"

    conn = get_conn()
    cur = conn.cursor()

    inventory_lines = [l for l in lines if l["line_type"] == "مخزون"]
    asset_lines = [l for l in lines if l["line_type"] == "أصل_ثابت"]

    enriched = []
    subtotal_inventory = 0.0
    for l in inventory_lines:
        cur.execute("SELECT * FROM products WHERE id=?", (l["product_id"],))
        prow = cur.fetchone()
        pcols = [d[0] for d in cur.description]
        product = dict(zip(pcols, prow))
        cur.execute("SELECT * FROM product_uoms WHERE id=?", (l["uom_id"],))
        urow = cur.fetchone()
        ucols = [d[0] for d in cur.description]
        uom = dict(zip(ucols, urow))

        base_qty = l["quantity"] * uom["factor"]
        line_total = round(l["quantity"] * l["unit_cost"], 2)
        base_unit_cost = (l["unit_cost"] / uom["factor"]) if uom["factor"] else l["unit_cost"]
        subtotal_inventory += line_total
        enriched.append({**l, "product": product, "uom": uom, "base_quantity": base_qty,
                          "line_total": line_total, "base_unit_cost": base_unit_cost})

    subtotal_assets = sum(a["line_total"] for a in asset_lines)
    subtotal = round(subtotal_inventory + subtotal_assets, 2)

    tax_amount = 0.0
    tax_row = None
    if tax_id:
        cur.execute("SELECT * FROM taxes WHERE id=?", (tax_id,))
        trow = cur.fetchone()
        tcols = [d[0] for d in cur.description]
        tax_row = dict(zip(tcols, trow))
        tax_amount = round(subtotal * float(tax_row["rate"]) / 100, 2)

    total_amount = round(subtotal + tax_amount, 2)
    immediate_total = round(subtotal_inventory + tax_amount, 2)  # الأصول الثابتة تُستثنى لحين التحويل

    credit_account = cash_account if payment_type == "نقدي" else AP_ACCOUNT
    credit_line = {"account_code": credit_account, "debit": 0, "credit": immediate_total, "line_desc": "فاتورة مشتريات"}
    if payment_type != "نقدي":
        credit_line["party_type"] = "supplier"
        credit_line["party_id"] = supplier_id

    je_lines = []
    inv_totals = {}
    for e in enriched:
        acc = e["product"]["inventory_account_code"]
        inv_totals[acc] = inv_totals.get(acc, 0) + e["line_total"]
    for acc, amt in inv_totals.items():
        je_lines.append({"account_code": acc, "debit": amt, "credit": 0, "line_desc": "شراء بضاعة - فاتورة مشتريات",
                          "project_id": project_id})

    if tax_amount > 0:
        je_lines.append({"account_code": tax_row["account_code"], "debit": tax_amount, "credit": 0,
                          "line_desc": tax_row["name"]})

    journal_id = None
    if je_lines:
        je_lines.append(credit_line)
        je_desc = f"فاتورة مشتريات - {reference}" if reference else "فاتورة مشتريات"
        ok, msg = post_journal_entry(invoice_date, je_desc, je_lines, source="فاتورة مشتريات")
        if not ok:
            return False, msg
        cur.execute("SELECT id FROM journal ORDER BY id DESC LIMIT 1")
        journal_id = cur.fetchone()[0]

    cur.execute(
        """INSERT INTO purchase_invoices (reference, invoice_date, due_date, supplier_id, payment_type,
           cash_account_code, tax_id, project_id, subtotal, tax_amount, total_amount, journal_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (reference, str(invoice_date), str(due_date) if due_date else str(invoice_date), supplier_id, payment_type,
         cash_account if payment_type == "نقدي" else None,
         tax_id, project_id, subtotal, tax_amount, total_amount, journal_id, datetime.now().isoformat())
    )
    invoice_id = cur.lastrowid

    for e in enriched:
        cur.execute(
            """INSERT INTO purchase_invoice_lines
               (invoice_id, line_type, product_id, uom_id, quantity, base_quantity, unit_cost, line_total)
               VALUES (?, 'مخزون', ?, ?, ?, ?, ?, ?)""",
            (invoice_id, e["product_id"], e["uom_id"], e["quantity"], e["base_quantity"],
             e["unit_cost"], e["line_total"])
        )
        product = e["product"]
        old_qty, old_cost = product["quantity_on_hand"], product["cost_price"]
        new_qty = old_qty + e["base_quantity"]
        new_avg_cost = ((old_qty * old_cost) + (e["base_quantity"] * e["base_unit_cost"])) / new_qty if new_qty > 0 else e["base_unit_cost"]
        cur.execute("UPDATE products SET quantity_on_hand=?, cost_price=? WHERE id=?",
                    (new_qty, round(new_avg_cost, 4), e["product_id"]))

    for a in asset_lines:
        cur.execute(
            """INSERT INTO purchase_invoice_lines (invoice_id, line_type, description, line_total, is_converted)
               VALUES (?, 'أصل_ثابت', ?, ?, 0)""",
            (invoice_id, a["description"], a["line_total"])
        )

    conn.commit()
    msg = f"تم ترحيل فاتورة المشتريات رقم {invoice_id} بنجاح (الإجمالي: {total_amount:,.2f})"
    if asset_lines:
        msg += f" — يوجد {len(asset_lines)} بند أصول ثابتة بانتظار التحويل لموديل الإهلاك"
    return True, msg


def get_purchase_invoices_df():
    conn = get_conn()
    q = """
        SELECT pi.id as رقم_الفاتورة, pi.invoice_date as التاريخ, s.name as المورد,
               p.name as المشروع,
               pi.payment_type as طريقة_الدفع, pi.subtotal as الإجمالي_قبل_الضريبة,
               pi.tax_amount as الضريبة, pi.total_amount as الإجمالي, pi.journal_id as رقم_القيد
        FROM purchase_invoices pi LEFT JOIN suppliers s ON s.id = pi.supplier_id
        LEFT JOIN projects p ON p.id = pi.project_id
        ORDER BY pi.id DESC
    """
    return query_df(conn, q)


def get_pending_asset_lines_df():
    conn = get_conn()
    q = """
        SELECT pil.id as رقم_البند, pi.id as رقم_الفاتورة, pi.invoice_date as التاريخ,
               s.name as المورد, pil.description as الوصف, pil.line_total as المبلغ
        FROM purchase_invoice_lines pil
        JOIN purchase_invoices pi ON pi.id = pil.invoice_id
        LEFT JOIN suppliers s ON s.id = pi.supplier_id
        WHERE pil.line_type = 'أصل_ثابت' AND pil.is_converted = 0
        ORDER BY pi.invoice_date
    """
    return query_df(conn, q)


def convert_purchase_line_to_asset(line_id, name, asset_account, accum_dep_account, expense_account,
                                    useful_life_months, salvage_value=0.0):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT pil.*, pi.invoice_date, pi.payment_type, pi.cash_account_code, pi.supplier_id
        FROM purchase_invoice_lines pil JOIN purchase_invoices pi ON pi.id = pil.invoice_id
        WHERE pil.id=?
    """, (line_id,))
    row = cur.fetchone()
    if not row:
        return False, "البند غير موجود"
    cols = [d[0] for d in cur.description]
    line = dict(zip(cols, row))
    if line["is_converted"]:
        return False, "تم تحويل هذا البند مسبقًا"

    funding_account = line["cash_account_code"] if line["payment_type"] == "نقدي" else AP_ACCOUNT
    party_type = "supplier" if (line["payment_type"] != "نقدي" and line["supplier_id"]) else None
    party_id = line["supplier_id"] if party_type else None

    asset_id = add_fixed_asset(
        name, asset_account, accum_dep_account, expense_account,
        line["invoice_date"], line["line_total"], salvage_value, useful_life_months,
        funding_account, post_opening_entry=True, party_type=party_type, party_id=party_id
    )
    cur.execute("UPDATE purchase_invoice_lines SET is_converted=1, fixed_asset_id=? WHERE id=?",
                (asset_id, line_id))
    conn.commit()
    log_audit("تحويل بند فاتورة لأصل ثابت", None, f"بند رقم {line_id} -> أصل رقم {asset_id}")
    return True, f"تم تحويل البند لأصل ثابت رقم {asset_id} بنجاح، وتمت إضافته لموديل الإهلاك"


# ---------------------------------------------------------------------------
# المشاريع والحسابات التحليلية (Analytic Accounting)
# ---------------------------------------------------------------------------

def get_projects_df(active_only=True):
    conn = get_conn()
    q = "SELECT * FROM projects"
    if active_only:
        q += " WHERE is_active = 1"
    q += " ORDER BY name"
    return query_df(conn, q)


def add_project(name, code="", client_name="", budget=0.0, start_date=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO projects (code, name, client_name, budget, start_date) VALUES (?,?,?,?,?)",
        (code.strip() or None, name, client_name, budget, str(start_date) if start_date else None)
    )
    conn.commit()
    return cur.lastrowid


def project_selectbox(label, key, allow_none=True, default_project_id=None):
    """صندوق اختيار مشروع اختياري - يُرجع project_id أو None. يُستخدم بصفحات القيود اليدوية والفواتير"""
    projects_df = get_projects_df()
    options = ([None] if allow_none else []) + projects_df["id"].tolist()
    labels = {None: "بدون مشروع"}
    labels.update({row["id"]: row["name"] for _, row in projects_df.iterrows()})
    idx = options.index(default_project_id) if default_project_id in options else 0
    return st.selectbox(label, options, index=idx, format_func=lambda i: labels.get(i, i), key=key)


def project_transactions_df(project_id, date_from=None, date_to=None):
    conn = get_conn()
    q = """
        SELECT j.entry_date as التاريخ, j.id as رقم_القيد, j.description as البيان,
               a.type as نوع_الحساب, a.code as رمز_الحساب, a.name as اسم_الحساب,
               jl.debit as مدين, jl.credit as دائن
        FROM journal_lines jl
        JOIN journal j ON j.id = jl.journal_id
        JOIN accounts a ON a.code = jl.account_code
        WHERE jl.project_id = ?
    """
    params = [project_id]
    if date_from:
        q += " AND j.entry_date >= ?"
        params.append(str(date_from))
    if date_to:
        q += " AND j.entry_date <= ?"
        params.append(str(date_to))
    q += " ORDER BY j.entry_date, j.id"
    return query_df(conn, q, params)


def project_profitability(project_id, date_from=None, date_to=None):
    """يرجع (إجمالي الإيراد, إجمالي التكلفة, صافي ربح/خسارة المشروع, تفاصيل الحركات)"""
    df = project_transactions_df(project_id, date_from, date_to)
    if df.empty:
        return 0.0, 0.0, 0.0, df

    revenue_rows = df[df["نوع_الحساب"] == "ايرادات"]
    cost_rows = df[df["نوع_الحساب"].isin(["مصروفات"])]

    total_revenue = float((revenue_rows["دائن"] - revenue_rows["مدين"]).sum())
    total_cost = float((cost_rows["مدين"] - cost_rows["دائن"]).sum())
    net_profit = total_revenue - total_cost
    return total_revenue, total_cost, net_profit, df


# ---------------------------------------------------------------------------
# الإقفال السنوي (Year-End Closing)
# ---------------------------------------------------------------------------

def get_fiscal_year_closings_df():
    conn = get_conn()
    q = """
        SELECT fiscal_year as السنة_المالية, closing_date as تاريخ_الإقفال,
               journal_id as رقم_قيد_الإقفال, net_income as صافي_الدخل, created_at as توقيت_الإقفال
        FROM fiscal_year_closings ORDER BY fiscal_year DESC
    """
    return query_df(conn, q)


def is_fiscal_year_closed(fiscal_year):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM fiscal_year_closings WHERE fiscal_year=?", (fiscal_year,))
    return cur.fetchone()[0] > 0


def close_fiscal_year(year_start, year_end, retained_earnings_account="3103"):
    """
    يقفل السنة المالية: يصفّر أرصدة حسابات الإيرادات والمصروفات بترحيلها لحساب الأرباح المرحلة،
    ثم يقفل الفترة تلقائيًا (تاريخ القفل = نهاية السنة) لمنع أي تعديل لاحق على قيود مقفلة.
    """
    fiscal_year = str(year_start)[:4]
    if is_fiscal_year_closed(fiscal_year):
        return False, f"السنة المالية {fiscal_year} مقفلة مسبقًا"

    balances = get_all_account_balances(year_start, year_end)
    _, _, _, _, net_income = income_statement(year_start, year_end, balances=balances)

    lines = []
    revenues = balances[(balances["type"] == "ايرادات") & (balances["balance"] != 0)]
    expenses = balances[(balances["type"] == "مصروفات") & (balances["balance"] != 0)]

    for _, acc in revenues.iterrows():
        lines.append({"account_code": acc["code"], "debit": acc["balance"], "credit": 0,
                      "line_desc": f"إقفال حساب إيراد: {acc['name']}"})
    for _, acc in expenses.iterrows():
        lines.append({"account_code": acc["code"], "debit": 0, "credit": acc["balance"],
                      "line_desc": f"إقفال حساب مصروف: {acc['name']}"})

    if not lines:
        return False, "لا توجد حركات إيرادات أو مصروفات ضمن هذه السنة لإقفالها"

    if net_income >= 0:
        lines.append({"account_code": retained_earnings_account, "debit": 0, "credit": net_income,
                      "line_desc": "ترحيل صافي ربح السنة للأرباح المرحلة"})
    else:
        lines.append({"account_code": retained_earnings_account, "debit": -net_income, "credit": 0,
                      "line_desc": "ترحيل صافي خسارة السنة من الأرباح المرحلة"})

    ok, msg = post_journal_entry(year_end, f"قيد إقفال السنة المالية {fiscal_year}", lines, source="إقفال سنوي")
    if not ok:
        return False, msg

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM journal ORDER BY id DESC LIMIT 1")
    journal_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO fiscal_year_closings (fiscal_year, closing_date, journal_id, net_income, created_at) VALUES (?,?,?,?,?)",
        (fiscal_year, str(year_end), journal_id, float(net_income), datetime.now().isoformat())
    )
    conn.commit()
    set_setting("lock_date", str(year_end))
    log_audit("إقفال سنوي", journal_id, f"إقفال السنة {fiscal_year}، صافي الدخل={net_income:.2f}")
    return True, f"تم إقفال السنة المالية {fiscal_year} بنجاح. صافي الدخل المرحّل = {net_income:,.2f}. تم قفل جميع الفترات حتى {year_end} تلقائيًا."


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

    /* مظهر يحاكي واجهة أودو (Odoo) - نفس اللون البنفسجي المميز لشريطها الجانبي */
    section[data-testid="stSidebar"] {
        background-color: #714B67;
    }
    section[data-testid="stSidebar"] * {
        color: #FFFFFF !important;
    }
    section[data-testid="stSidebar"] .stRadio label span {
        color: #FFFFFF !important;
    }
    div.stButton > button[kind="primary"], button[kind="primaryFormSubmit"] {
        background-color: #714B67;
        border-color: #714B67;
    }
</style>
""", unsafe_allow_html=True)

company_name = get_setting("company_name", "الكف الفني")
company_activity = get_setting("company_activity", "شركة خدمات إعلانية وتنفيذ مشاريع")
st.markdown(f"""
<div style="background:linear-gradient(135deg,#714B67,#8F5A82); padding:18px 26px;
            border-radius:10px; margin-bottom:18px;">
    <h2 style="color:white; margin:0;">🏢 {company_name}</h2>
    <p style="color:#EDE0EA; margin:4px 0 0 0;">{company_activity}</p>
</div>
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
        "📦 المخزون والمنتجات",
        "🧾 فاتورة مبيعات",
        "🧾 فاتورة مشتريات",
        "🗂️ المشاريع",
        "📊 تقرير ربحية المشاريع",
        "💰 حركة صندوق (قبض / صرف)",
        "🧾 مصروفات",
        "💵 إيرادات أخرى",
        "👥 العملاء (ذمم مدينة)",
        "🏭 الموردون (ذمم دائنة)",
        "🏢 الأصول الثابتة والإهلاك",
        "🔄 التسوية البنكية",
        "🧾 الضرائب (VAT)",
        "📚 دفتر اليومية",
        "📖 دفتر الأستاذ",
        "⚖️ ميزان المراجعة",
        "📈 قائمة الدخل",
        "🏦 قائمة المركز المالي",
        "🔒 الإقفال السنوي",
        "⚙️ الإعدادات وقفل الفترات",
        "📋 سجل التدقيق",
    ]
)

lock_date_active = get_setting("lock_date")
if lock_date_active:
    st.sidebar.warning(f"🔒 الفترات مقفلة حتى تاريخ: {lock_date_active}")

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

    projects_for_lines = get_projects_df()
    project_line_options = ["بدون مشروع"] + projects_for_lines["name"].tolist()
    project_name_to_id = {row["name"]: row["id"] for _, row in projects_for_lines.iterrows()}

    if "manual_lines" not in st.session_state:
        st.session_state.manual_lines = pd.DataFrame(
            [{"رمز الحساب": acc_options[0] if acc_options else "", "مدين": 0.0, "دائن": 0.0,
              "بيان السطر": "", "المشروع": "بدون مشروع"}]
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
            "المشروع": st.column_config.SelectboxColumn("المشروع (اختياري)", options=project_line_options),
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
             "credit": float(r["دائن"] or 0), "line_desc": r.get("بيان السطر", ""),
             "project_id": project_name_to_id.get(r.get("المشروع"))}
            for _, r in edited.iterrows() if r["رمز الحساب"]
        ]
        ok, msg = post_journal_entry(entry_date, description, lines, source="يدوي")
        if ok:
            st.success(msg)
            st.session_state.manual_lines = pd.DataFrame(
                [{"رمز الحساب": acc_options[0], "مدين": 0.0, "دائن": 0.0, "بيان السطر": "", "المشروع": "بدون مشروع"}]
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
        amount = st.number_input("المبلغ (قبل الضريبة)", min_value=0.0, step=0.01, key="s_amount")
        s_project_id = project_selectbox("المشروع (اختياري)", "s_project")

        sales_taxes_df = get_taxes_df(tax_type="مبيعات")
        tax_options = ["بدون ضريبة"] + sales_taxes_df["id"].tolist() if not sales_taxes_df.empty else ["بدون ضريبة"]
        sel_tax = st.selectbox(
            "الضريبة", tax_options,
            format_func=lambda t: "بدون ضريبة" if t == "بدون ضريبة"
            else f'{sales_taxes_df[sales_taxes_df["id"] == t]["name"].values[0]}',
            key="s_tax"
        )
        tax_amount = 0.0
        if sel_tax != "بدون ضريبة":
            tax_row = sales_taxes_df[sales_taxes_df["id"] == sel_tax].iloc[0]
            tax_amount = round(amount * float(tax_row["rate"]) / 100, 2)
            st.caption(f"قيمة الضريبة ({tax_row['rate']}%): {tax_amount:,.2f} — الإجمالي المستحق: {amount + tax_amount:,.2f}")

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
                total_amount = amount + tax_amount
                debit_line = {"account_code": debit_account, "debit": total_amount, "credit": 0, "line_desc": s_desc}
                if sale_type == "آجل (على الحساب)":
                    debit_line["party_type"] = "customer"
                    debit_line["party_id"] = customer_id
                lines = [
                    debit_line,
                    {"account_code": revenue_account, "debit": 0, "credit": amount, "line_desc": s_desc,
                     "project_id": s_project_id},
                ]
                if tax_amount > 0:
                    tax_row = sales_taxes_df[sales_taxes_df["id"] == sel_tax].iloc[0]
                    lines.append({"account_code": tax_row["account_code"], "debit": 0, "credit": tax_amount,
                                  "line_desc": f"{tax_row['name']} على الحركة: {s_desc}"})
                ok, msg = post_journal_entry(s_date, s_desc, lines, source="مبيعات")
                if ok and include_cogs and cogs_amount > 0:
                    cogs_lines = [
                        {"account_code": "5101", "debit": cogs_amount, "credit": 0, "line_desc": "تكلفة بضاعة مباعة",
                         "project_id": s_project_id},
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
        amount = st.number_input("المبلغ (قبل الضريبة)", min_value=0.0, step=0.01, key="p_amount")
        p_project_id = project_selectbox("المشروع (اختياري)", "p_project")

        purchase_taxes_df = get_taxes_df(tax_type="مشتريات")
        tax_options = ["بدون ضريبة"] + purchase_taxes_df["id"].tolist() if not purchase_taxes_df.empty else ["بدون ضريبة"]
        sel_tax = st.selectbox(
            "الضريبة", tax_options,
            format_func=lambda t: "بدون ضريبة" if t == "بدون ضريبة"
            else f'{purchase_taxes_df[purchase_taxes_df["id"] == t]["name"].values[0]}',
            key="p_tax"
        )
        tax_amount = 0.0
        if sel_tax != "بدون ضريبة":
            tax_row = purchase_taxes_df[purchase_taxes_df["id"] == sel_tax].iloc[0]
            tax_amount = round(amount * float(tax_row["rate"]) / 100, 2)
            st.caption(f"قيمة الضريبة ({tax_row['rate']}%): {tax_amount:,.2f} — الإجمالي المستحق: {amount + tax_amount:,.2f}")

        submitted = st.form_submit_button("✅ ترحيل حركة المشتريات", type="primary")
        if submitted:
            if amount <= 0:
                st.error("الرجاء إدخال مبلغ أكبر من صفر")
            elif purchase_type == "آجل (على الحساب)" and supplier_id is None:
                st.error("الرجاء اختيار مورد للشراء الآجل")
            else:
                total_amount = amount + tax_amount
                credit_line = {"account_code": credit_account, "debit": 0, "credit": total_amount, "line_desc": p_desc}
                if purchase_type == "آجل (على الحساب)":
                    credit_line["party_type"] = "supplier"
                    credit_line["party_id"] = supplier_id
                lines = [
                    {"account_code": debit_account, "debit": amount, "credit": 0, "line_desc": p_desc,
                     "project_id": p_project_id},
                    credit_line,
                ]
                if tax_amount > 0:
                    tax_row = purchase_taxes_df[purchase_taxes_df["id"] == sel_tax].iloc[0]
                    lines.append({"account_code": tax_row["account_code"], "debit": tax_amount, "credit": 0,
                                  "line_desc": f"{tax_row['name']} على الحركة: {p_desc}"})
                ok, msg = post_journal_entry(p_date, p_desc, lines, source="مشتريات")
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)

# ---------------------------------------------------------------------------
# المخزون والمنتجات
# ---------------------------------------------------------------------------
elif menu == "📦 المخزون والمنتجات":
    st.subheader("المخزون والمنتجات")
    st.caption("عرّف المنتجات مع وحدة القياس الأساسية، وأضف وحدات إضافية (باكيت، كرتون...) بمعامل تحويل لكل منتج")

    products_df = get_products_df(active_only=False)
    st.markdown("### المنتجات الحالية")
    if products_df.empty:
        st.info("لا توجد منتجات مضافة بعد")
    else:
        disp = products_df.copy()
        disp["حساب_المخزون"] = disp["inventory_account_code"].map(lambda c: acc_map.get(c, c))
        disp["حساب_الإيراد"] = disp["revenue_account_code"].map(lambda c: acc_map.get(c, c))
        disp["حساب_التكلفة"] = disp["cogs_account_code"].map(lambda c: acc_map.get(c, c))
        st.dataframe(
            disp[["id", "sku", "name", "base_unit", "quantity_on_hand", "cost_price", "sale_price",
                  "حساب_المخزون", "حساب_الإيراد", "حساب_التكلفة"]]
                .rename(columns={"id": "الرقم", "sku": "الكود", "name": "الاسم", "base_unit": "الوحدة الأساسية",
                                 "quantity_on_hand": "الكمية بالمخزون", "cost_price": "متوسط التكلفة",
                                 "sale_price": "سعر البيع"}),
            use_container_width=True, hide_index=True
        )

    st.divider()
    st.markdown("### ➕ إضافة منتج جديد")
    with st.form("add_product_form"):
        c1, c2 = st.columns(2)
        p_name = c1.text_input("اسم المنتج")
        p_sku = c2.text_input("الكود (SKU) - اختياري")
        c3, c4 = st.columns(2)
        p_base_unit = c3.text_input("الوحدة الأساسية (وحدة / متر / كيلو...)", value="وحدة")
        p_sale_price = c4.number_input("سعر البيع الافتراضي (للوحدة الأساسية)", min_value=0.0, step=0.01)
        c5, c6 = st.columns(2)
        p_opening_qty = c5.number_input("الكمية الافتتاحية (بالوحدة الأساسية)", min_value=0.0, step=0.01)
        p_opening_cost = c6.number_input("متوسط تكلفة الوحدة الافتتاحية", min_value=0.0, step=0.01)

        p_inv_acc = acc_selectbox("حساب المخزون", "p_inv_acc", filter_types=["اصول"], default_code="1104")
        p_rev_acc = acc_selectbox("حساب الإيراد الافتراضي", "p_rev_acc", filter_types=["ايرادات"], default_code="4101")
        p_cogs_acc = acc_selectbox("حساب تكلفة البضاعة المباعة", "p_cogs_acc", filter_types=["مصروفات"], default_code="5101")

        submitted = st.form_submit_button("✅ إضافة المنتج", type="primary")
        if submitted:
            if not p_name:
                st.error("أدخل اسم المنتج")
            else:
                add_product(p_name, p_base_unit, p_inv_acc, p_rev_acc, p_cogs_acc,
                            p_sale_price, p_sku, p_opening_qty, p_opening_cost)
                st.success("تمت إضافة المنتج ووحدة القياس الأساسية له")
                st.rerun()

    st.divider()
    st.markdown("### 📐 إضافة وحدة قياس إضافية لمنتج (باكيت، كرتون...)")
    if products_df.empty:
        st.info("أضف منتجًا أولًا")
    else:
        with st.form("add_uom_form"):
            uom_product = st.selectbox("المنتج", products_df["id"].tolist(),
                                        format_func=lambda i: products_df[products_df["id"] == i]["name"].values[0])
            c1, c2 = st.columns(2)
            uom_name = c1.text_input("اسم الوحدة (باكيت / كرتون / دستة...)")
            uom_factor = c2.number_input("كم تعادل من الوحدة الأساسية", min_value=0.0001, step=0.5, value=1.0)
            submitted_uom = st.form_submit_button("➕ إضافة الوحدة")
            if submitted_uom:
                if not uom_name:
                    st.error("أدخل اسم الوحدة")
                else:
                    add_product_uom(uom_product, uom_name, uom_factor)
                    st.success("تمت إضافة وحدة القياس")
                    st.rerun()

        st.markdown("#### وحدات القياس المعرّفة لكل منتج")
        for _, prod in products_df.iterrows():
            uoms = get_product_uoms_df(prod["id"])
            with st.expander(f'{prod["name"]} ({len(uoms)} وحدة)'):
                st.dataframe(uoms[["uom_name", "factor"]].rename(columns={"uom_name": "الوحدة", "factor": "المعامل"}),
                             use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# فاتورة مبيعات (متعددة البنود)
# ---------------------------------------------------------------------------
elif menu == "🧾 فاتورة مبيعات":
    st.caption("فاتورة عميل (Customer Invoice)")
    st.markdown("## 📝 مسودة (Draft)")

    uom_opts_df = product_uom_options_df()
    if uom_opts_df.empty:
        st.warning("أضف منتجات أولًا من صفحة 'المخزون والمنتجات' قبل إنشاء فاتورة مبيعات")
    else:
        hcol1, hcol2 = st.columns(2)
        with hcol1:
            si_sale_type = st.radio("نوع البيع", ["نقدي", "آجل (على الحساب)"], horizontal=True, key="si_type")
            si_customer_id = None
            if si_sale_type == "آجل (على الحساب)":
                with st.expander("➕ إضافة عميل جديد بسرعة"):
                    nc1, nc2, nc3 = st.columns(3)
                    nc_name = nc1.text_input("اسم العميل", key="si_new_cust_name")
                    nc_phone = nc2.text_input("رقم الهاتف (اختياري)", key="si_new_cust_phone")
                    nc_open = nc3.number_input("رصيد افتتاحي (إن وجد)", min_value=0.0, step=0.01, key="si_new_cust_open")
                    if st.button("إضافة العميل", key="si_add_cust_btn"):
                        if nc_name:
                            add_customer(nc_name, nc_phone, nc_open)
                            st.success("تمت الإضافة، اختره من القائمة تحت")
                            st.rerun()
                        else:
                            st.error("أدخل اسم العميل")
                customers_df = get_customers_df()
                if customers_df.empty:
                    st.warning("لا يوجد عملاء مسجلون بعد")
                else:
                    si_customer_id = st.selectbox(
                        "👤 العميل (Customer)", customers_df["id"].tolist(),
                        format_func=lambda i: customers_df[customers_df["id"] == i]["name"].values[0],
                        key="si_customer"
                    )
            si_reference = st.text_input("🔖 المرجع (Reference)", key="si_reference")
        with hcol2:
            si_date = st.date_input("📅 تاريخ الفاتورة (Invoice Date)", value=date.today(), key="si_date")
            si_due_date = st.date_input("⏰ تاريخ الاستحقاق (Due Date)", value=date.today(), key="si_due_date")
            st.text_input("📒 اليومية (Journal)", value="فاتورة مبيعات", disabled=True, key="si_journal_display")
            st.text_input("💱 العملة (Currency)", value=get_setting("currency", "IQD"),
                          disabled=True, key="si_currency_display")

        uom_label_map = {}
        for _, r in uom_opts_df.iterrows():
            uom_label_map[f'{r["product_name"]} - {r["uom_name"]}'] = int(r["uom_id"])
        options = list(uom_label_map.keys())

        if "si_lines" not in st.session_state:
            first = uom_opts_df.iloc[0]
            st.session_state.si_lines = pd.DataFrame([
                {"الصنف / الوحدة": options[0], "الكمية": 1.0, "سعر الوحدة": float(first["sale_price"])}
            ])

        tab_lines, tab_journal, tab_other = st.tabs(
            ["🧾 بنود الفاتورة (Invoice Lines)", "📒 قيود اليومية (Journal Items)", "ℹ️ معلومات أخرى (Other Info)"]
        )

        # نحسب "معلومات أخرى" أولًا بالكود (بالرغم إنها آخر تبويب بالعرض) عشان نحتاج قيمها
        # (الضريبة/حساب الدفع/المشروع) قبل ما نبني معاينة قيود اليومية بتبويب "قيود اليومية"
        with tab_other:
            sales_taxes_df = get_taxes_df(tax_type="مبيعات")
            tax_choice_options = ["بدون ضريبة"] + sales_taxes_df["id"].tolist() if not sales_taxes_df.empty else ["بدون ضريبة"]
            si_tax_choice = st.selectbox(
                "الضريبة (Tax)", tax_choice_options,
                format_func=lambda t: "بدون ضريبة" if t == "بدون ضريبة"
                else sales_taxes_df[sales_taxes_df["id"] == t]["name"].values[0],
                key="si_tax"
            )
            si_tax_id = None if si_tax_choice == "بدون ضريبة" else int(si_tax_choice)
            si_cash_acc = None
            if si_sale_type == "نقدي":
                si_cash_acc = acc_selectbox("حساب الصندوق/البنك", "si_cash_acc", filter_types=["اصول"], default_code="1101")
            si_project_id = project_selectbox("المشروع (اختياري)", "si_project")

        with tab_lines:
            edited_si = st.data_editor(
                st.session_state.si_lines, num_rows="dynamic", use_container_width=True,
                column_config={
                    "الصنف / الوحدة": st.column_config.SelectboxColumn(
                        "المنتج / الوحدة (Product/UoM)", options=options, required=True
                    ),
                    "الكمية": st.column_config.NumberColumn("الكمية (Qty)", min_value=0.0, step=0.01),
                    "سعر الوحدة": st.column_config.NumberColumn("السعر (Price)", min_value=0.0, step=0.01),
                },
                key="si_editor"
            )

            si_preview_enriched = []
            for _, r in edited_si.iterrows():
                label = r["الصنف / الوحدة"]
                if label not in uom_label_map or r["الكمية"] <= 0:
                    continue
                uom_id = uom_label_map[label]
                urow = uom_opts_df[uom_opts_df["uom_id"] == uom_id].iloc[0]
                si_preview_enriched.append({
                    "product_id": int(urow["product_id"]), "uom_id": uom_id,
                    "quantity": float(r["الكمية"]), "unit_price": float(r["سعر الوحدة"]),
                    "line_total": round(float(r["الكمية"]) * float(r["سعر الوحدة"]), 2),
                    "revenue_account_code": urow["revenue_account_code"],
                    "المنتج": urow["product_name"], "الوحدة": urow["uom_name"],
                })

            if si_preview_enriched:
                st.markdown("**تفاصيل البنود (Product / Label / Account / Quantity / UoM / Price / Tax excl.)**")
                st.dataframe(
                    pd.DataFrame([{
                        "المنتج (Product)": e["المنتج"], "الوحدة (UoM)": e["الوحدة"],
                        "الحساب (Account)": acc_map.get(e["revenue_account_code"], e["revenue_account_code"]),
                        "الكمية (Quantity)": e["quantity"], "السعر (Price)": e["unit_price"],
                        "بدون ضريبة (Tax excl.)": e["line_total"],
                    } for e in si_preview_enriched]),
                    use_container_width=True, hide_index=True
                )

        with tab_journal:
            if not si_preview_enriched:
                st.info("أضف بندًا واحدًا على الأقل بالفاتورة لعرض معاينة قيد اليومية")
            else:
                si_subtotal_preview = sum(e["line_total"] for e in si_preview_enriched)
                si_tax_preview = 0.0
                if si_tax_id:
                    tax_row_preview = sales_taxes_df[sales_taxes_df["id"] == si_tax_id].iloc[0]
                    si_tax_preview = round(si_subtotal_preview * float(tax_row_preview["rate"]) / 100, 2)
                si_total_preview = si_subtotal_preview + si_tax_preview

                debit_acc_preview = si_cash_acc if si_sale_type == "نقدي" else AR_ACCOUNT
                je_rows = [{"رمز الحساب": debit_acc_preview,
                           "الحساب": acc_map.get(debit_acc_preview, debit_acc_preview),
                           "مدين": si_total_preview, "دائن": 0.0}]
                rev_totals = {}
                for e in si_preview_enriched:
                    rev_totals[e["revenue_account_code"]] = rev_totals.get(e["revenue_account_code"], 0) + e["line_total"]
                for acc, amt in rev_totals.items():
                    je_rows.append({"رمز الحساب": acc, "الحساب": acc_map.get(acc, acc), "مدين": 0.0, "دائن": amt})
                if si_tax_preview > 0:
                    tax_acc = sales_taxes_df[sales_taxes_df["id"] == si_tax_id].iloc[0]["account_code"]
                    je_rows.append({"رمز الحساب": tax_acc, "الحساب": acc_map.get(tax_acc, tax_acc),
                                    "مدين": 0.0, "دائن": si_tax_preview})
                st.caption("معاينة القيد المحاسبي اللي بينترحّل عند التأكيد")
                st.dataframe(pd.DataFrame(je_rows), use_container_width=True, hide_index=True)

        if si_preview_enriched:
            si_subtotal_preview = sum(e["line_total"] for e in si_preview_enriched)
            si_tax_preview = 0.0
            if si_tax_id:
                tax_row_preview = sales_taxes_df[sales_taxes_df["id"] == si_tax_id].iloc[0]
                si_tax_preview = round(si_subtotal_preview * float(tax_row_preview["rate"]) / 100, 2)
            c1, c2, c3 = st.columns(3)
            c1.metric("الإجمالي قبل الضريبة (Untaxed)", f"{si_subtotal_preview:,.2f}")
            c2.metric("الضريبة (Tax)", f"{si_tax_preview:,.2f}")
            c3.metric("الإجمالي (Total)", f"{si_subtotal_preview + si_tax_preview:,.2f}")

        if st.button("✅ تأكيد (Confirm)", type="primary"):
            lines = [{"product_id": e["product_id"], "uom_id": e["uom_id"],
                     "quantity": e["quantity"], "unit_price": e["unit_price"]} for e in si_preview_enriched]

            if not lines:
                st.error("أضف بندًا واحدًا على الأقل بكمية أكبر من صفر")
            elif si_sale_type == "آجل (على الحساب)" and si_customer_id is None:
                st.error("اختر عميلًا للبيع الآجل")
            else:
                ok, msg = post_sales_invoice(si_date, si_customer_id, si_sale_type, si_cash_acc, si_tax_id, lines,
                                              project_id=si_project_id, due_date=si_due_date, reference=si_reference)
                if ok:
                    st.success(f"✅ مرحّلة (Posted) — {msg}")
                    st.session_state.si_lines = pd.DataFrame([
                        {"الصنف / الوحدة": options[0], "الكمية": 1.0,
                         "سعر الوحدة": float(uom_opts_df.iloc[0]["sale_price"])}
                    ])
                    st.rerun()
                else:
                    st.error(msg)

    st.divider()
    st.markdown("### 📋 فواتير المبيعات السابقة")
    si_list = get_sales_invoices_df()
    if si_list.empty:
        st.info("لا توجد فواتير مسجلة بعد")
    else:
        st.dataframe(si_list, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# فاتورة مشتريات (متعددة البنود + تحويل أصول ثابتة)
# ---------------------------------------------------------------------------
elif menu == "🧾 فاتورة مشتريات":
    st.caption("فاتورة مورد (Vendor Bill)")
    st.markdown("## 📝 مسودة (Draft)")

    hcol1, hcol2 = st.columns(2)
    with hcol1:
        pi_purchase_type = st.radio("نوع الشراء", ["نقدي", "آجل (على الحساب)"], horizontal=True, key="pi_type")
        pi_supplier_id = None
        if pi_purchase_type == "آجل (على الحساب)":
            with st.expander("➕ إضافة مورد جديد بسرعة"):
                ns1, ns2, ns3 = st.columns(3)
                ns_name = ns1.text_input("اسم المورد", key="pi_new_supp_name")
                ns_phone = ns2.text_input("الهاتف (اختياري)", key="pi_new_supp_phone")
                ns_open = ns3.number_input("رصيد افتتاحي (إن وجد)", min_value=0.0, step=0.01, key="pi_new_supp_open")
                if st.button("إضافة المورد", key="pi_add_supp_btn"):
                    if ns_name:
                        add_supplier(ns_name, ns_phone, ns_open)
                        st.success("تمت الإضافة، اختره من القائمة تحت")
                        st.rerun()
                    else:
                        st.error("أدخل اسم المورد")
            suppliers_df = get_suppliers_df()
            if suppliers_df.empty:
                st.warning("لا يوجد موردون مسجلون بعد")
            else:
                pi_supplier_id = st.selectbox(
                    "👤 المورد (Vendor)", suppliers_df["id"].tolist(),
                    format_func=lambda i: suppliers_df[suppliers_df["id"] == i]["name"].values[0],
                    key="pi_supplier"
                )
        pi_reference = st.text_input("🔖 مرجع الفاتورة (Bill Reference)", key="pi_reference")
    with hcol2:
        pi_date = st.date_input("📅 تاريخ الفاتورة (Bill Date)", value=date.today(), key="pi_date")
        pi_due_date = st.date_input("⏰ تاريخ الاستحقاق (Due Date)", value=date.today(), key="pi_due_date")
        st.text_input("📒 اليومية (Journal)", value="فاتورة مشتريات", disabled=True, key="pi_journal_display")
        st.text_input("💱 العملة (Currency)", value=get_setting("currency", "IQD"),
                      disabled=True, key="pi_currency_display")

    tab_lines, tab_journal, tab_other = st.tabs(
        ["🧾 بنود الفاتورة (Invoice Lines)", "📒 قيود اليومية (Journal Items)", "ℹ️ معلومات أخرى (Other Info)"]
    )

    # نحسب "معلومات أخرى" أولًا بالكود (بالرغم إنها آخر تبويب بالعرض) عشان نحتاج قيمها
    # (الضريبة/حساب الدفع/المشروع) قبل ما نبني معاينة قيود اليومية بتبويب "قيود اليومية"
    with tab_other:
        pi_cash_acc = None
        if pi_purchase_type == "نقدي":
            pi_cash_acc = acc_selectbox("حساب الصندوق/البنك", "pi_cash_acc", filter_types=["اصول"], default_code="1101")
        pi_project_id = project_selectbox("المشروع (اختياري)", "pi_project")
        purchase_taxes_df = get_taxes_df(tax_type="مشتريات")
        pi_tax_choice_options = ["بدون ضريبة"] + purchase_taxes_df["id"].tolist() if not purchase_taxes_df.empty else ["بدون ضريبة"]
        pi_tax_choice = st.selectbox(
            "الضريبة (Tax)", pi_tax_choice_options,
            format_func=lambda t: "بدون ضريبة" if t == "بدون ضريبة"
            else purchase_taxes_df[purchase_taxes_df["id"] == t]["name"].values[0],
            key="pi_tax"
        )
        pi_tax_id = None if pi_tax_choice == "بدون ضريبة" else int(pi_tax_choice)

    with tab_lines:
        st.markdown("**📦 بنود مخزون (Product Lines)**")
        uom_opts_df = product_uom_options_df()
        uom_label_map = {}
        edited_inv = pd.DataFrame(columns=["الصنف / الوحدة", "الكمية", "تكلفة الوحدة"])
        if uom_opts_df.empty:
            st.info("لا توجد منتجات مضافة بعد — أضف منتجات من صفحة 'المخزون والمنتجات' لتسجيل بنود مخزون")
        else:
            for _, r in uom_opts_df.iterrows():
                uom_label_map[f'{r["product_name"]} - {r["uom_name"]}'] = int(r["uom_id"])
            inv_options = list(uom_label_map.keys())
            if "pi_inv_lines" not in st.session_state:
                st.session_state.pi_inv_lines = pd.DataFrame([
                    {"الصنف / الوحدة": inv_options[0], "الكمية": 0.0, "تكلفة الوحدة": 0.0}
                ])
            edited_inv = st.data_editor(
                st.session_state.pi_inv_lines, num_rows="dynamic", use_container_width=True,
                column_config={
                    "الصنف / الوحدة": st.column_config.SelectboxColumn(
                        "المنتج / الوحدة (Product/UoM)", options=inv_options, required=True
                    ),
                    "الكمية": st.column_config.NumberColumn("الكمية (Qty)", min_value=0.0, step=0.01),
                    "تكلفة الوحدة": st.column_config.NumberColumn("تكلفة الوحدة (Price)", min_value=0.0, step=0.01),
                },
                key="pi_inv_editor"
            )

        pi_preview_enriched = []
        for _, r in edited_inv.iterrows():
            label = r["الصنف / الوحدة"]
            if label not in uom_label_map or r["الكمية"] <= 0:
                continue
            uom_id = uom_label_map[label]
            urow = uom_opts_df[uom_opts_df["uom_id"] == uom_id].iloc[0]
            pi_preview_enriched.append({
                "product_id": int(urow["product_id"]), "uom_id": uom_id,
                "quantity": float(r["الكمية"]), "unit_cost": float(r["تكلفة الوحدة"]),
                "line_total": round(float(r["الكمية"]) * float(r["تكلفة الوحدة"]), 2),
                "inventory_account_code": urow["inventory_account_code"],
                "المنتج": urow["product_name"], "الوحدة": urow["uom_name"],
            })

        if pi_preview_enriched:
            st.markdown("**تفاصيل بنود المخزون (Product / Account / Quantity / UoM / Price / Tax excl.)**")
            st.dataframe(
                pd.DataFrame([{
                    "المنتج (Product)": e["المنتج"], "الوحدة (UoM)": e["الوحدة"],
                    "الحساب (Account)": acc_map.get(e["inventory_account_code"], e["inventory_account_code"]),
                    "الكمية (Quantity)": e["quantity"], "التكلفة (Price)": e["unit_cost"],
                    "بدون ضريبة (Tax excl.)": e["line_total"],
                } for e in pi_preview_enriched]),
                use_container_width=True, hide_index=True
            )

        st.markdown("**🏢 بنود أصول ثابتة (اختياري) — Fixed Asset Lines**")
        st.caption("هذي البنود ما تُرحّل بقيد فوري — راح تظهر بقائمة 'بانتظار التحويل' بأسفل الصفحة لربطها بموديل الإهلاك")
        if "pi_asset_lines" not in st.session_state:
            st.session_state.pi_asset_lines = pd.DataFrame([{"وصف الأصل": "", "المبلغ": 0.0}])
        edited_assets = st.data_editor(
            st.session_state.pi_asset_lines, num_rows="dynamic", use_container_width=True,
            column_config={
                "وصف الأصل": st.column_config.TextColumn("وصف الأصل (Label)"),
                "المبلغ": st.column_config.NumberColumn("المبلغ (Price)", min_value=0.0, step=0.01),
            },
            key="pi_asset_editor"
        )
        pi_asset_lines_input = [
            {"description": r["وصف الأصل"], "line_total": float(r["المبلغ"])}
            for _, r in edited_assets.iterrows() if r["وصف الأصل"] and r["المبلغ"] > 0
        ]

    with tab_journal:
        if not pi_preview_enriched and not pi_asset_lines_input:
            st.info("أضف بندًا واحدًا على الأقل بالفاتورة لعرض معاينة قيد اليومية")
        else:
            pi_subtotal_inv_preview = sum(e["line_total"] for e in pi_preview_enriched)
            pi_subtotal_assets_preview = sum(a["line_total"] for a in pi_asset_lines_input)
            pi_tax_preview = 0.0
            if pi_tax_id:
                tax_row_preview = purchase_taxes_df[purchase_taxes_df["id"] == pi_tax_id].iloc[0]
                pi_tax_preview = round((pi_subtotal_inv_preview + pi_subtotal_assets_preview)
                                       * float(tax_row_preview["rate"]) / 100, 2)
            pi_immediate_total_preview = pi_subtotal_inv_preview + pi_tax_preview

            if pi_preview_enriched or pi_tax_preview > 0:
                credit_acc_preview = pi_cash_acc if pi_purchase_type == "نقدي" else AP_ACCOUNT
                je_rows = []
                inv_totals = {}
                for e in pi_preview_enriched:
                    inv_totals[e["inventory_account_code"]] = inv_totals.get(e["inventory_account_code"], 0) + e["line_total"]
                for acc, amt in inv_totals.items():
                    je_rows.append({"رمز الحساب": acc, "الحساب": acc_map.get(acc, acc), "مدين": amt, "دائن": 0.0})
                if pi_tax_preview > 0:
                    tax_acc = purchase_taxes_df[purchase_taxes_df["id"] == pi_tax_id].iloc[0]["account_code"]
                    je_rows.append({"رمز الحساب": tax_acc, "الحساب": acc_map.get(tax_acc, tax_acc),
                                    "مدين": pi_tax_preview, "دائن": 0.0})
                je_rows.append({"رمز الحساب": credit_acc_preview,
                               "الحساب": acc_map.get(credit_acc_preview, credit_acc_preview),
                               "مدين": 0.0, "دائن": pi_immediate_total_preview})
                st.caption("معاينة القيد المحاسبي الفوري (بنود المخزون + الضريبة فقط)")
                st.dataframe(pd.DataFrame(je_rows), use_container_width=True, hide_index=True)

            if pi_asset_lines_input:
                st.info(f"⏳ فيه {len(pi_asset_lines_input)} بند أصول ثابتة بمبلغ إجمالي "
                        f"{pi_subtotal_assets_preview:,.2f} ما بيترحّل فورًا — بيضل بانتظار التحويل لموديل الإهلاك")

    if pi_preview_enriched or pi_asset_lines_input:
        pi_subtotal_inv_preview = sum(e["line_total"] for e in pi_preview_enriched)
        pi_subtotal_assets_preview = sum(a["line_total"] for a in pi_asset_lines_input)
        pi_tax_preview = 0.0
        if pi_tax_id:
            tax_row_preview = purchase_taxes_df[purchase_taxes_df["id"] == pi_tax_id].iloc[0]
            pi_tax_preview = round((pi_subtotal_inv_preview + pi_subtotal_assets_preview)
                                   * float(tax_row_preview["rate"]) / 100, 2)
        c1, c2, c3 = st.columns(3)
        c1.metric("إجمالي بنود المخزون (Untaxed)", f"{pi_subtotal_inv_preview:,.2f}")
        c2.metric("إجمالي بنود الأصول الثابتة", f"{pi_subtotal_assets_preview:,.2f}")
        c3.metric("الإجمالي الكلي (Total)", f"{pi_subtotal_inv_preview + pi_subtotal_assets_preview + pi_tax_preview:,.2f}")

    if st.button("✅ تأكيد (Confirm)", type="primary"):
        lines = [{"line_type": "مخزون", "product_id": e["product_id"], "uom_id": e["uom_id"],
                 "quantity": e["quantity"], "unit_cost": e["unit_cost"]} for e in pi_preview_enriched]
        lines += [{"line_type": "أصل_ثابت", "description": a["description"], "line_total": a["line_total"]}
                 for a in pi_asset_lines_input]

        if not lines:
            st.error("أضف بندًا واحدًا على الأقل (مخزون أو أصل ثابت)")
        elif pi_purchase_type == "آجل (على الحساب)" and pi_supplier_id is None:
            st.error("اختر موردًا للشراء الآجل")
        else:
            ok, msg = post_purchase_invoice(pi_date, pi_supplier_id, pi_purchase_type, pi_cash_acc, pi_tax_id, lines,
                                             project_id=pi_project_id, due_date=pi_due_date, reference=pi_reference)
            if ok:
                st.success(f"✅ مرحّلة (Posted) — {msg}")
                st.session_state.pi_inv_lines = pd.DataFrame([
                    {"الصنف / الوحدة": list(uom_label_map.keys())[0] if uom_label_map else "",
                     "الكمية": 0.0, "تكلفة الوحدة": 0.0}
                ])
                st.session_state.pi_asset_lines = pd.DataFrame([{"وصف الأصل": "", "المبلغ": 0.0}])
                st.rerun()
            else:
                st.error(msg)

    st.divider()
    st.subheader("🏢 بنود أصول ثابتة بانتظار التحويل لموديل الإهلاك")
    pending_df = get_pending_asset_lines_df()
    if pending_df.empty:
        st.info("لا توجد بنود بانتظار التحويل")
    else:
        st.dataframe(pending_df, use_container_width=True, hide_index=True)
        sel_line = st.selectbox(
            "اختر البند لتحويله لأصل ثابت", pending_df["رقم_البند"].tolist(),
            format_func=lambda i: pending_df[pending_df["رقم_البند"] == i]["الوصف"].values[0],
            key="pending_sel_line"
        )
        sel_row = pending_df[pending_df["رقم_البند"] == sel_line].iloc[0]
        with st.form("convert_asset_form"):
            cc1, cc2 = st.columns(2)
            asset_name = cc1.text_input("اسم الأصل", value=sel_row["الوصف"])
            life_months = cc2.number_input("العمر الإنتاجي (بالأشهر)", min_value=1, value=60, step=1)
            asset_acc = acc_selectbox("حساب الأصل", "conv_asset_acc", filter_types=["اصول"], default_code="1201")
            accum_acc = acc_selectbox("حساب مجمع الإهلاك", "conv_accum_acc", filter_types=["اصول"], default_code="1203")
            exp_acc = acc_selectbox("حساب مصروف الإهلاك", "conv_exp_acc", filter_types=["مصروفات"], default_code="5107")
            salvage = st.number_input("القيمة التخريدية", min_value=0.0, step=0.01, key="conv_salvage")
            submitted_conv = st.form_submit_button("✅ تحويل لأصل ثابت", type="primary")
            if submitted_conv:
                ok, msg = convert_purchase_line_to_asset(int(sel_line), asset_name, asset_acc, accum_acc,
                                                          exp_acc, int(life_months), salvage)
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)

    st.divider()
    st.markdown("### 📋 فواتير المشتريات السابقة")
    pi_list = get_purchase_invoices_df()
    if pi_list.empty:
        st.info("لا توجد فواتير مسجلة بعد")
    else:
        st.dataframe(pi_list, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# المشاريع (الحسابات التحليلية)
# ---------------------------------------------------------------------------
elif menu == "🗂️ المشاريع":
    st.subheader("المشاريع (الحسابات التحليلية)")
    st.caption("عرّف كل مشروع تنفّذه الشركة، وبعدين اربط قيود المبيعات/المشتريات/المصروفات فيه "
               "عشان تقدر تعرف تكلفة وربحية كل مشروع لحاله")

    projects_df = get_projects_df(active_only=False)
    st.markdown("### المشاريع الحالية")
    if projects_df.empty:
        st.info("لا توجد مشاريع مضافة بعد")
    else:
        st.dataframe(
            projects_df[["id", "code", "name", "client_name", "budget", "start_date", "is_active"]]
                .rename(columns={"id": "الرقم", "code": "الكود", "name": "اسم المشروع",
                                 "client_name": "العميل", "budget": "الموازنة التقديرية",
                                 "start_date": "تاريخ البدء", "is_active": "نشط"}),
            use_container_width=True, hide_index=True
        )

    st.divider()
    st.markdown("### ➕ إضافة مشروع جديد")
    with st.form("add_project_form"):
        c1, c2 = st.columns(2)
        pr_name = c1.text_input("اسم المشروع")
        pr_code = c2.text_input("كود المشروع (اختياري)")
        c3, c4 = st.columns(2)
        pr_client = c3.text_input("اسم العميل (اختياري)")
        pr_budget = c4.number_input("الموازنة التقديرية (اختياري)", min_value=0.0, step=0.01)
        pr_start = st.date_input("تاريخ البدء", value=date.today())
        submitted = st.form_submit_button("✅ إضافة المشروع", type="primary")
        if submitted:
            if not pr_name:
                st.error("أدخل اسم المشروع")
            else:
                add_project(pr_name, pr_code, pr_client, pr_budget, pr_start)
                st.success("تمت إضافة المشروع")
                st.rerun()

# ---------------------------------------------------------------------------
# تقرير ربحية المشاريع
# ---------------------------------------------------------------------------
elif menu == "📊 تقرير ربحية المشاريع":
    st.subheader("تقرير ربحية المشاريع")
    st.caption("يجمع كل الإيرادات والتكاليف المرتبطة بمشروع معيّن (من فواتير المبيعات/المشتريات، "
               "المصروفات، وقيود اليومية العامة الموسومة بنفس المشروع)")

    projects_df = get_projects_df(active_only=False)
    if projects_df.empty:
        st.info("أضف مشروعًا أولًا من صفحة 'المشاريع'")
    else:
        sel_project = st.selectbox("اختر المشروع", projects_df["id"].tolist(),
                                    format_func=lambda i: projects_df[projects_df["id"] == i]["name"].values[0],
                                    key="proj_report_sel")
        c1, c2 = st.columns(2)
        pr_from = c1.date_input("من تاريخ", value=None, key="proj_from")
        pr_to = c2.date_input("إلى تاريخ", value=None, key="proj_to")

        total_revenue, total_cost, net_profit, details_df = project_profitability(
            sel_project, pr_from if pr_from else None, pr_to if pr_to else None
        )

        c1, c2, c3 = st.columns(3)
        c1.metric("إجمالي إيراد المشروع", f"{total_revenue:,.2f}")
        c2.metric("إجمالي تكلفة المشروع", f"{total_cost:,.2f}")
        c3.metric("صافي ربح/خسارة المشروع", f"{net_profit:,.2f}")

        proj_row = projects_df[projects_df["id"] == sel_project].iloc[0]
        if proj_row["budget"] and proj_row["budget"] > 0:
            st.progress(min(total_cost / proj_row["budget"], 1.0))
            st.caption(f"التكلفة الفعلية مقابل الموازنة التقديرية: {total_cost:,.2f} / {proj_row['budget']:,.2f}")

        st.divider()
        st.markdown("### تفاصيل الحركات المرتبطة بالمشروع")
        if details_df.empty:
            st.info("لا توجد حركات مرتبطة بهذا المشروع ضمن هذا النطاق")
        else:
            st.dataframe(details_df, use_container_width=True, hide_index=True)

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
        e_project_id = project_selectbox("المشروع (اختياري)", "e_project")

        submitted = st.form_submit_button("✅ ترحيل المصروف", type="primary")
        if submitted:
            if amount <= 0:
                st.error("الرجاء إدخال مبلغ أكبر من صفر")
            else:
                lines = [
                    {"account_code": expense_account, "debit": amount, "credit": 0, "line_desc": e_desc,
                     "project_id": e_project_id},
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
        st.subheader("↩️ عكس قيد (بدل الحذف النهائي)")
        st.caption("لا يمكن حذف القيود المرحّلة نهائيًا حفاظًا على الأثر المحاسبي (Audit Trail) — "
                   "بدلًا من ذلك يتم إنشاء قيد عكسي يلغي أثر القيد الأصلي مع إبقائه بالسجل.")
        entry_ids = sorted(df["رقم_القيد"].unique(), reverse=True)
        rev_id = st.selectbox("اختر رقم القيد لعكسه", entry_ids, key="rev_sel_id")

        entry_status = df[df["رقم_القيد"] == rev_id].iloc[0]
        if bool(entry_status["معكوس"]):
            st.warning("⚠️ هذا القيد معكوس مسبقًا")
        elif pd.notna(entry_status["قيد_عكس_رقم"]) and entry_status["المصدر"] == "عكس قيد":
            st.info("ℹ️ هذا قيد عكسي بحد ذاته")
        else:
            c1, c2 = st.columns(2)
            rev_date = c1.date_input("تاريخ قيد العكس", value=date.today(), key="rev_date")
            rev_reason = c2.text_input("سبب العكس (اختياري)", key="rev_reason")
            if st.button("↩️ عكس القيد", type="secondary"):
                ok, msg = reverse_journal_entry(int(rev_id), rev_date, rev_reason)
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)

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

# ---------------------------------------------------------------------------
# الضرائب (VAT)
# ---------------------------------------------------------------------------
elif menu == "🧾 الضرائب (VAT)":
    st.subheader("إعداد الضرائب وتقرير ضريبة القيمة المضافة")

    st.markdown("### الضرائب المُعرّفة")
    taxes_df = get_taxes_df(active_only=False)
    if taxes_df.empty:
        st.info("لا توجد ضرائب معرّفة بعد")
    else:
        st.dataframe(
            taxes_df.assign(الحساب_المرتبط=taxes_df["account_code"].map(lambda c: acc_map.get(c, c)))
                    [["id", "name", "rate", "tax_type", "الحساب_المرتبط", "is_active"]]
                    .rename(columns={"id": "الرقم", "name": "الاسم", "rate": "النسبة %", "tax_type": "النوع", "is_active": "مفعّلة"}),
            use_container_width=True, hide_index=True
        )

    with st.expander("➕ إضافة ضريبة جديدة"):
        with st.form("add_tax_form"):
            c1, c2 = st.columns(2)
            t_name = c1.text_input("اسم الضريبة (مثال: ضريبة القيمة المضافة 15%)")
            t_rate = c2.number_input("النسبة %", min_value=0.0, max_value=100.0, step=0.5, value=15.0)
            c3, c4 = st.columns(2)
            t_type = c3.selectbox("نوع الضريبة", ["مبيعات", "مشتريات"])
            t_account = acc_selectbox("حساب الضريبة", "t_account",
                                       filter_types=["اصول", "خصوم"],
                                       default_code="2104" if t_type == "مبيعات" else "1106")
            submitted = st.form_submit_button("إضافة الضريبة", type="primary")
            if submitted:
                if not t_name or not t_account:
                    st.error("أدخل اسم الضريبة واختر حساب الضريبة")
                else:
                    add_tax(t_name, t_rate, t_type, t_account)
                    st.success("تمت إضافة الضريبة")
                    st.rerun()

    st.divider()
    st.markdown("### 📊 تقرير الضريبة عن فترة")
    c1, c2 = st.columns(2)
    vat_from = c1.date_input("من تاريخ", value=None, key="vat_from")
    vat_to = c2.date_input("إلى تاريخ", value=None, key="vat_to")

    sales_tax_bal = account_balance("2104", "دائن", vat_from if vat_from else None, vat_to if vat_to else None)
    purchase_tax_bal = account_balance("1106", "مدين", vat_from if vat_from else None, vat_to if vat_to else None)
    net_vat = sales_tax_bal - purchase_tax_bal

    c1, c2, c3 = st.columns(3)
    c1.metric("ضريبة المبيعات (مستحقة الدفع)", f"{sales_tax_bal:,.2f}")
    c2.metric("ضريبة المشتريات (قابلة للخصم)", f"{purchase_tax_bal:,.2f}")
    c3.metric("صافي الضريبة المستحقة", f"{net_vat:,.2f}")
    if net_vat >= 0:
        st.info(f"💡 صافي المبلغ المستحق للهيئة الضريبية: {net_vat:,.2f}")
    else:
        st.info(f"💡 صافي مبلغ قابل للاسترداد من الهيئة الضريبية: {-net_vat:,.2f}")

# ---------------------------------------------------------------------------
# الإقفال السنوي
# ---------------------------------------------------------------------------
elif menu == "🔒 الإقفال السنوي":
    st.subheader("الإقفال السنوي (Year-End Closing)")
    st.caption("يصفّر أرصدة حسابات الإيرادات والمصروفات بترحيلها لحساب الأرباح المرحلة، "
               "ثم يقفل الفترة تلقائيًا لمنع أي تعديل لاحق على قيود السنة المقفلة.")

    closings_df = get_fiscal_year_closings_df()
    st.markdown("### سجل السنوات المالية المقفلة")
    if closings_df.empty:
        st.info("لا توجد سنوات مالية مقفلة بعد")
    else:
        st.dataframe(closings_df, use_container_width=True, hide_index=True)

    st.divider()
    st.markdown("### إقفال سنة مالية جديدة")
    c1, c2 = st.columns(2)
    year_start = c1.date_input("بداية السنة المالية", value=date(date.today().year, 1, 1), key="fy_start")
    year_end = c2.date_input("نهاية السنة المالية", value=date(date.today().year, 12, 31), key="fy_end")
    retained_acc = acc_selectbox("حساب الأرباح المرحلة", "fy_retained_acc",
                                  filter_types=["حقوق الملكية"], default_code="3103")

    fiscal_year_preview = str(year_start)[:4]
    if is_fiscal_year_closed(fiscal_year_preview):
        st.warning(f"⚠️ السنة المالية {fiscal_year_preview} مقفلة مسبقًا")
    else:
        _, total_rev, _, total_exp, net_income_preview = income_statement(year_start, year_end)
        st.write(f"معاينة: إجمالي الإيرادات = {total_rev:,.2f} | إجمالي المصروفات = {total_exp:,.2f} | "
                 f"صافي الدخل = {net_income_preview:,.2f}")
        confirm = st.checkbox("⚠️ أؤكد أنني أريد إقفال هذه السنة المالية بشكل نهائي (لا يمكن التراجع بسهولة)")
        if st.button("🔒 إقفال السنة المالية", type="primary", disabled=not confirm):
            ok, msg = close_fiscal_year(year_start, year_end, retained_acc)
            if ok:
                st.success(msg)
                st.rerun()
            else:
                st.error(msg)

# ---------------------------------------------------------------------------
# الإعدادات وقفل الفترات المحاسبية
# ---------------------------------------------------------------------------
elif menu == "⚙️ الإعدادات وقفل الفترات":
    st.subheader("🏢 بيانات الشركة")
    with st.form("company_info_form"):
        c1, c2 = st.columns(2)
        new_company_name = c1.text_input("اسم الشركة", value=get_setting("company_name", "الكف الفني"))
        new_company_activity = c2.text_input("نشاط الشركة", value=get_setting("company_activity", ""))
        if st.form_submit_button("💾 حفظ بيانات الشركة", type="primary"):
            set_setting("company_name", new_company_name)
            set_setting("company_activity", new_company_activity)
            log_audit("تعديل إعداد", None, "تحديث بيانات الشركة")
            st.success("تم حفظ بيانات الشركة")
            st.rerun()

    st.divider()
    st.subheader("قفل الفترات المحاسبية (Lock Date)")
    st.caption("أي قيد بتاريخ أقدم من أو يساوي تاريخ القفل لن يُقبل ترحيله أو عكسه — "
               "يحمي البيانات المحاسبية المقفلة من التعديل بعد المراجعة أو التقديم الرسمي.")

    current_lock = get_setting("lock_date")
    if current_lock:
        st.info(f"🔒 تاريخ القفل الحالي: {current_lock}")
    else:
        st.info("لا يوجد تاريخ قفل حاليًا — كل الفترات مفتوحة للتعديل")

    new_lock = st.date_input("تعيين تاريخ قفل جديد", value=date.today(), key="new_lock_date")
    c1, c2 = st.columns(2)
    if c1.button("🔒 تعيين تاريخ القفل", type="primary"):
        set_setting("lock_date", str(new_lock))
        log_audit("تعديل إعداد", None, f"تعيين تاريخ القفل إلى {new_lock}")
        st.success(f"تم تعيين تاريخ القفل إلى {new_lock}")
        st.rerun()
    if c2.button("🔓 إلغاء القفل بالكامل", type="secondary"):
        set_setting("lock_date", "")
        log_audit("تعديل إعداد", None, "إلغاء تاريخ القفل بالكامل")
        st.success("تم إلغاء القفل، جميع الفترات مفتوحة الآن")
        st.rerun()

# ---------------------------------------------------------------------------
# سجل التدقيق (Audit Log)
# ---------------------------------------------------------------------------
elif menu == "📋 سجل التدقيق":
    st.subheader("سجل التدقيق (Audit Log)")
    st.caption("سجل كامل لكل عمليات الترحيل والعكس والإقفال وتعديل الإعدادات، للحفاظ على الشفافية والتتبع.")

    audit_df = get_audit_log_df()
    if audit_df.empty:
        st.info("لا توجد أحداث مسجلة بعد")
    else:
        st.dataframe(audit_df, use_container_width=True, hide_index=True)
