import os
import io
import json
import base64
import sqlite3
import requests
from PIL import Image
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# --- 1. إعداد قاعدة البيانات ---
conn = sqlite3.connect("company_accounting.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE,
    name TEXT,
    type TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS journal_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT DEFAULT CURRENT_TIMESTAMP,
    debit_account TEXT,
    credit_account TEXT,
    amount REAL,
    entity_name TEXT,
    description TEXT
)
""")

default_accounts = [
    ('101', 'الخزينة / النقدية', 'asset'),
    ('102', 'البنك', 'asset'),
    ('103', 'العملاء / المدينون', 'asset'),
    ('201', 'الموردون / الدائنون', 'liability'),
    ('301', 'رأس المال', 'equity'),
    ('401', 'إيرادات المبيعات والخدمات', 'revenue'),
    ('501', 'مصروفات تشغيلية وعمومية', 'expense'),
    ('502', 'رواتب وأجور', 'expense'),
    ('503', 'إيجار ومرافق', 'expense')
]
cursor.executemany("INSERT OR IGNORE INTO accounts (code, name, type) VALUES (?, ?, ?)", default_accounts)
conn.commit()

# --- 2. الوظائف المحاسبية والتقارير ---

def add_journal_entry(debit: str, credit: str, amount: float, entity: str, desc: str) -> str:
    cursor.execute(
        "INSERT INTO journal_entries (debit_account, credit_account, amount, entity_name, description) VALUES (?, ?, ?, ?, ?)",
        (debit, credit, amount, entity, desc)
    )
    conn.commit()
    return (
        f"📋 تم تسجيل القيد بنجاح:\n"
        f"• من حـ/ {debit}: {amount:,.2f} جنيه\n"
        f"• إلى حـ/ {credit}: {amount:,.2f} جنيه\n"
        f"• البيان: {desc} ({entity})"
    )

def generate_income_statement() -> str:
    cursor.execute("""
        SELECT a.name, IFNULL(SUM(j.amount), 0)
        FROM accounts a
        LEFT JOIN journal_entries j ON a.name = j.credit_account
        WHERE a.type = 'revenue'
        GROUP BY a.name
    """)
    revenues = cursor.fetchall()
    total_rev = sum(r[1] for r in revenues)

    cursor.execute("""
        SELECT a.name, IFNULL(SUM(j.amount), 0)
        FROM accounts a
        LEFT JOIN journal_entries j ON a.name = j.debit_account
        WHERE a.type = 'expense'
        GROUP BY a.name
    """)
    expenses = cursor.fetchall()
    total_exp = sum(e[1] for e in expenses)

    net_profit = total_rev - total_exp
    res = "📈 **قائمة الدخل (الأرباح والخسائر):**\n\n"
    res += "🔹 **الإيرادات:**\n"
    for r in revenues:
        res += f"  - {r[0]}: {r[1]:,.2f} جنيه\n"
    res += f"**إجمالي الإيرادات:** {total_rev:,.2f} جنيه\n\n"
    
    res += "🔹 **المصروفات:**\n"
    for e in expenses:
        res += f"  - {e[0]}: {e[1]:,.2f} جنيه\n"
    res += f"**إجمالي المصروفات:** {total_exp:,.2f} جنيه\n\n"
    res += f"🏆 **صافي الربح / (الخسارة):** {net_profit:,.2f} جنيه"
    return res

def generate_balance_sheet() -> str:
    cursor.execute("""
        SELECT a.name,
               (IFNULL((SELECT SUM(amount) FROM journal_entries WHERE debit_account = a.name), 0) -
                IFNULL((SELECT SUM(amount) FROM journal_entries WHERE credit_account = a.name), 0)) as bal
        FROM accounts a WHERE a.type = 'asset'
    """)
    assets = cursor.fetchall()
    total_assets = sum(a[1] for a in assets)

    cursor.execute("""
        SELECT a.name,
               (IFNULL((SELECT SUM(amount) FROM journal_entries WHERE credit_account = a.name), 0) -
                IFNULL((SELECT SUM(amount) FROM journal_entries WHERE debit_account = a.name), 0)) as bal
        FROM accounts a WHERE a.type = 'liability'
    """)
    liabilities = cursor.fetchall()
    total_liab = sum(l[1] for l in liabilities)

    res = "🏛️ **قائمة المركز المالي (الميزانية العمومية):**\n\n"
    res += "💼 **الأصول:**\n"
    for a in assets:
        res += f"  - {a[0]}: {a[1]:,.2f} جنيه\n"
    res += f"**إجمالي الأصول:** {total_assets:,.2f} جنيه\n\n"

    res += "🤝 **الالتزامات:**\n"
    for l in liabilities:
        res += f"  - {l[0]}: {l[1]:,.2f} جنيه\n"
    res += f"**إجمالي الالتزامات:** {total_liab:,.2f} جنيه\n\n"
    return res

def get_entities_ledger(entity_type: str) -> str:
    acc_name = 'العملاء / المدينون' if entity_type == 'client' else 'الموردون / الدائنون'
    cursor.execute("SELECT entity_name, debit_account, credit_account, amount, description, date FROM journal_entries WHERE debit_account = ? OR credit_account = ?", (acc_name, acc_name))
    rows = cursor.fetchall()
    if not rows:
        return f"لا توجد حركات مسجلة تخص {acc_name} حتى الآن."
    
    msg = f"📋 **دفتر أستاذ {acc_name}:**\n\n"
    for r in rows:
        msg += f"• [{r[5][:10]}] {r[0]} | {r[3]:,.2f} جنيه | {r[4]}\n"
    return msg

# --- 3. محرك Gemini 3.6 Flash ---

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

def call_gemini(user_prompt: str, image_bytes=None) -> str:
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY
    }
    
    system_instruction = (
        "أنت رئيس حسابات ومستشار مالي تنفيذي (CFO) خبير وودود، تتحدث بلهجة مصرية مهذبة وعملية.\n"
        "الحسابات المعتمدة:\n"
        "['الخزينة / النقدية', 'البنك', 'العملاء / المدينون', 'الموردون / الدائنون', 'إيرادات المبيعات والخدمات', 'مصروفات تشغيلية وعمومية', 'رواتب وأجور', 'إيجار ومرافق'].\n\n"
        "قم دائماً بالرد حصراً بصيغة JSON بدون أي كلام خارجي:\n"
        "1. للنقاش أو التحية أو الاستشارة:\n"
        '{"action": "chat", "reply": "ردك الودود والمفصل هنا"}\n\n'
        "2. لتسجيل حركة مالية:\n"
        '{"action": "record", "debit": "الحساب المدين", "credit": "الحساب الدائن", "amount": رقم, "entity": "الطرف المعني", "desc": "البيان", "advice_or_chat": "تعليقك المالي"}\n\n'
        "3. للقوائم المالية:\n"
        '{"action": "income_statement" أو "balance_sheet"}\n\n'
        "4. لكشوف الحسابات:\n"
        '{"action": "entities", "entity_type": "client" أو "vendor"}\n'
    )

    parts = [{"text": system_instruction + "\n\nرسالة المستخدم: " + user_prompt}]
    if image_bytes:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(image_bytes).decode('utf-8')}})

    payload = {
        "contents": [{"parts": parts}]
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        res_data = response.json()

        if "error" in res_data:
            return f"تنبيه من المزود: {res_data['error'].get('message', 'خطأ غير معروف')}"

        if "candidates" not in res_data or not res_data["candidates"]:
            return "لم يصل رد، يرجى المحاولة ثانية."

        raw = res_data["candidates"][0]["content"]["parts"][0]["text"].strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw.strip())
        
        act = data.get("action")
        if act == "chat":
            return data.get("reply", "أهلاً بك يا فندم، أنا معك وسامعك تمام، قل لي بتفكر في إيه؟")
        elif act == "income_statement":
            return generate_income_statement() + "\n\n💡 تحب نحلل أي بند سوا؟"
        elif act == "balance_sheet":
            return generate_balance_sheet() + "\n\n💡 جاهز نراجع معاً موقف السيولة والالتزامات."
        elif act == "entities":
            return get_entities_ledger(data.get("entity_type", "client"))
        elif act == "record":
            record_res = add_journal_entry(
                data.get("debit", "الخزينة / النقدية"),
                data.get("credit", "إيرادات المبيعات والخدمات"),
                float(data.get("amount", 0.0)),
                data.get("entity", "عام"),
                data.get("desc", "تسجيل حركة")
            )
            chat_note = data.get("advice_or_chat", "")
            return f"{record_res}\n\n💬 {chat_note}" if chat_note else record_res
        else:
            return data.get("reply", "أنا تحت أمرك، تفضل بأي استفسار.")
    except Exception as e:
        return f"حدث خطأ أثناء المعالجة: {str(e)}"

# --- 4. تشغيل تيليجرام ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً بك يا فندم! 💼 أنا مستشارك المالي ورئيس الحسابات للشركة.\n\n"
        "أنا جاهز لمناقشة حساباتك وتسجيل القيود وعمل القوائم المالية في أي وقت. تفضل بما في بالك!"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(call_gemini(update.message.text))

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = await update.message.photo[-1].get_file()
    img_bytes = await photo.download_as_bytearray()
    await update.message.reply_text(call_gemini("حلل هذه الفاتورة وسجلها محاسبياً، واديني رأيك فيها.", image_bytes=bytes(img_bytes)))

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    print("المستشار المالي الذكي يعمل الآن...")
    app.run_polling()
