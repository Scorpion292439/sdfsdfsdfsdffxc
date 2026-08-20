import os
import json
import logging
import asyncio
import time
import threading
from datetime import date
from flask import Flask, render_template_string, jsonify
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# --- CANLI LOG YAKALAYICI (WEB PANEL İÇİN) ---
class MemoryLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.logs = []

    def emit(self, record):
        log_entry = self.format(record)
        self.logs.append(log_entry)
        if len(self.logs) > 150:
            self.logs.pop(0)

memory_handler = MemoryLogHandler()
memory_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
logger.addHandler(memory_handler)

TOKEN = "8697686670:AAHZX2U5Wx0jZwVfHzf9SCdI1mB-mtB1c9s"
ADMIN_IDS = [8522767291]
DB_FILE = "database.json"

app = Flask(__name__)
bot_running = False

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <title>Aliens_eye Bot Dashboard</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #0f172a; color: #f8fafc; margin: 0; padding: 20px; }
        .container { max-width: 800px; margin: 0 auto; background: #1e293b; padding: 25px; border-radius: 12px; box-shadow: 0 10px 25px rgba(0,0,0,0.3); }
        h1 { color: #38bdf8; margin-top: 0; }
        .status-box { display: flex; align-items: center; justify-content: space-between; background: #0f172a; padding: 15px 20px; border-radius: 8px; margin-bottom: 20px; border: 1px solid #334155; }
        .badge { padding: 6px 12px; border-radius: 20px; font-weight: bold; font-size: 14px; }
        .online { background: #065f46; color: #34d399; }
        .offline { background: #7f1d1d; color: #f87171; }
        pre { background: #0f172a; padding: 15px; border-radius: 8px; overflow-x: auto; height: 300px; border: 1px solid #334155; color: #a5f3fc; font-size: 13px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🤖 Aliens_eye Bot Dashboard</h1>
        <div class="status-box">
            <div>
                <strong>Bot Durumu:</strong> <span id="statusBadge" class="badge online">Kontrol Ediliyor...</span>
            </div>
        </div>
        <h3>📋 Canlı Sistem Logları</h3>
        <pre id="logBox">Loglar yükleniyor...</pre>
    </div>
    <script>
        async function fetchLogs() {
            try {
                let response = await fetch('/logs');
                let data = await response.json();
                document.getElementById('logBox').innerText = data.logs;
                let badge = document.getElementById('statusBadge');
                badge.className = data.status === 'online' ? 'badge online' : 'badge offline';
                badge.innerText = data.status === 'online' ? 'Aktif & Çalışıyor' : 'Durduruldu';
            } catch (e) { console.error("Loglar alınamadı"); }
        }
        setInterval(fetchLogs, 3000);
        fetchLogs();
    </script>
</body>
</html>
"""

def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"premium": [], "usage": {}}

def save_db(data):
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"DB Kaydetme Hatası: {e}")

user_targets = {}

# --- ÖNCELİKLİ KUYRUK YÖNETİCİSİ ---
class ScanQueueManager:
    def __init__(self, max_concurrent=1):
        self.max_concurrent = max_concurrent
        self.active_scans = 0
        self.queue = []

    async def acquire(self, is_premium: bool):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        priority = 0 if is_premium else 1
        entry_time = time.time()

        entry = (priority, entry_time, future)
        self.queue.append(entry)
        self.queue.sort(key=lambda x: (x[0], x[1]))

        if self.active_scans < self.max_concurrent and self.queue[0] == entry:
            self.queue.pop(0)
            self.active_scans += 1
            return 0

        while True:
            await future
            if self.active_scans < self.max_concurrent and self.queue and self.queue[0] == entry:
                self.queue.pop(0)
                self.active_scans += 1
                break
            else:
                future = loop.create_future()
                entry = (priority, entry_time, future)
                for i, item in enumerate(self.queue):
                    if item[1] == entry_time:
                        self.queue[i] = entry
                        break
                self.queue.sort(key=lambda x: (x[0], x[1]))

        return self.get_current_position(entry_time)

    def release(self):
        self.active_scans = max(0, self.active_scans - 1)
        if self.queue and self.active_scans < self.max_concurrent:
            self.active_scans += 1
            _, _, future = self.queue.pop(0)
            if not future.done():
                future.set_result(True)

    def get_current_position(self, entry_time: float) -> int:
        sorted_q = sorted(self.queue, key=lambda x: (x[0], x[1]))
        for idx, item in enumerate(sorted_q):
            if item[1] == entry_time:
                return idx + 1
        return len(self.queue)

scan_manager = ScanQueueManager(max_concurrent=1)

# --- /START KOMUTU ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    logger.info(f"📥 /start komutu alındı: {user.full_name} (ID: {user_id})")
    
    db = load_db()
    is_premium = user_id in db["premium"] or user_id in ADMIN_IDS

    if is_premium:
        subscription_status = "👑 **VIP / Premium Abonelik (Sınırsız)**"
        keyboard = [[InlineKeyboardButton("💬 Destek & İletişim (@machaa4)", url="https://t.me/machaa4")]]
    else:
        subscription_status = "🆓 **Free Plan (Günde 2 Hak)**"
        keyboard = [[InlineKeyboardButton("💎 Premium Satın Al (@machaa4)", url="https://t.me/machaa4")]]

    reply_markup = InlineKeyboardMarkup(keyboard)
    welcome_text = (
        f"🤖 **Hoş geldin, {user.first_name}!**\n"
        f"🌐 *Aliens_eye AI-OSINT Altyapısı Aktif*\n\n"
        f"📋 **Hesap Durumun:** {subscription_status}\n\n"
        f"🔍 Sorgulamak istediğin **kullanıcı adını** doğrudan mesaj olarak gönder."
    )
    await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")

# --- ADMIN: PREMIUM VERME ---
async def give_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Bu komutu kullanmaya yetkin yok.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Kullanım: `/prim <Kullanici_ID>`", parse_mode="Markdown")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Geçersiz Kullanıcı ID.")
        return

    db = load_db()
    if target_id not in db["premium"]:
        db["premium"].append(target_id)
        save_db(db)
        await update.message.reply_text(f"✅ Başarılı! `{target_id}` ID'li kullanıcıya **Premium Abonelik** tanımlandı.", parse_mode="Markdown")
        try:
            await context.bot.send_message(chat_id=target_id, text="🎉 **Tebrikler!** Hesabına yönetici tarafından **👑 Premium Abonelik** tanımlandı.", parse_mode="Markdown")
        except Exception:
            pass
    else:
        await update.message.reply_text(f"ℹ️ `{target_id}` zaten Premium üyeliğe sahip.", parse_mode="Markdown")

# --- KULLANICI MESAJI VE LİMİT ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    if text.startswith("/"): return

    username = text.split()[0]
    user_id = user.id
    today_str = str(date.today())

    db = load_db()
    is_premium = user_id in db["premium"] or user_id in ADMIN_IDS

    if not is_premium:
        if user_id not in db["usage"] or db["usage"][user_id]["date"] != today_str:
            db["usage"][user_id] = {"date": today_str, "count": 0}

        if db["usage"][user_id]["count"] >= 2:
            keyboard = [[InlineKeyboardButton("💎 Premium Satın Al (@machaa4)", url="https://t.me/machaa4")]]
            await update.message.reply_text("⚠️ Günlük ücretsiz tarama hakkın (2/2) doldu!", reply_markup=InlineKeyboardMarkup(keyboard))
            return

    user_targets[user_id] = username

    if is_premium:
        keyboard = [
            [InlineKeyboardButton("🔍 Basic Mod", callback_data="scan_basic")],
            [InlineKeyboardButton("⚡ Intermediate Mod", callback_data="scan_intermediate")],
            [InlineKeyboardButton("🚀 Advanced Mod", callback_data="scan_advanced")]
        ]
        await update.message.reply_text(f"👑 **VIP Üye**\n`{username}` için mod seç:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        db["usage"][user_id]["count"] += 1
        save_db(db)
        remaining = 2 - db["usage"][user_id]["count"]
        status_msg = await update.message.reply_text(f"🔍 `{username}` kuyruğa ekleniyor...", parse_mode="Markdown")
        asyncio.create_task(handle_scan_with_queue(update, context, user_id, username, "basic", f"Abonelik: Free (Kalan: {remaining}/2)", status_msg))

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if user_id not in user_targets:
        await query.edit_message_text("⚠️ Zaman aşımı. Lütfen kullanıcı adını tekrar gönder.")
        return

    username = user_targets[user_id]
    mode = data.replace("scan_", "")

    status_msg = await query.edit_message_text(f"👑 `{username}` hedefi **{mode.upper()}** modunda kuyruğa alınıyor...", parse_mode="Markdown")
    asyncio.create_task(handle_scan_with_queue(update, context, user_id, username, mode, "Abonelik: 👑 VIP (Sınırsız)", status_msg))

async def handle_scan_with_queue(update, context, user_id, username, mode, footer_info, status_msg):
    db = load_db()
    position = await scan_manager.acquire(user_id in db["premium"] or user_id in ADMIN_IDS)
    
    if position > 1:
        try: await status_msg.edit_text(f"⚠️ Sıradaki yeriniz: **{position}**", parse_mode="Markdown")
        except Exception: pass
    else:
        try: await status_msg.edit_text(f"🔍 `{username}` taranıyor...", parse_mode="Markdown")
        except Exception: pass

    try:
        await execute_scan(status_msg.chat.id, context, user_id, username, mode, footer_info, status_msg.message_id)
    finally:
        scan_manager.release()

async def execute_scan(chat_id, context, user_id, username, mode, footer_info, status_message_id):
    logger.info(f"🔍 [TARAMA] Kullanıcı: {user_id} | Mod: {mode} | Hedef: {username}")
    try:
        sites = {
            "GitHub": f"https://github.com/{username}",
            "Telegram": f"https://t.me/{username}",
            "Twitter": f"https://twitter.com/{username}",
            "TikTok": f"https://www.tiktok.com/@{username}",
            "Instagram": f"https://www.instagram.com/{username}/"
        }

        found_accounts = []
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
            for name, url in sites.items():
                try:
                    res = await client.get(url)
                    if res.status_code == 200:
                        found_accounts.append(url)
                except Exception:
                    pass

        try: await context.bot.delete_message(chat_id=chat_id, message_id=status_message_id)
        except Exception: pass

        if found_accounts:
            txt_content = f"=== MACHA OSINT TARGET REPORT ===\nTarget: {username}\nTotal Found: {len(found_accounts)}\n\n"
            for index, acc in enumerate(found_accounts, 1):
                txt_content += f"{index}. {acc}\n"
            
            file_path = f"{username}_report.txt"
            with open(file_path, "w", encoding="utf-8") as f: f.write(txt_content)

            with open(file_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id, 
                    document=InputFile(f, filename=file_path), 
                    caption=f"🎯 **{username}** taraması tamamlandı!\n📊 Toplam: `{len(found_accounts)}`\n\nℹ️ *{footer_info}*", 
                    parse_mode="Markdown"
                )
            if os.path.exists(file_path): os.remove(file_path)
        else:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ `{username}` için sonuç bulunamadı.\n\nℹ️ *{footer_info}*", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Tarama Hatası: {e}", exc_info=True)

@app.route('/')
def dashboard():
    return render_template_string(HTML_TEMPLATE)

@app.route('/logs')
def get_logs():
    global bot_running
    log_text = "\n".join(memory_handler.logs) if memory_handler.logs else "Log akışı bekleniyor..."
    return jsonify({"status": "online" if bot_running else "offline", "logs": log_text})

if __name__ == "__main__":
    # Flask web sunucusunu arka planda çalıştırıyoruz
    port = int(os.environ.get("PORT", 10000))
    flask_thread = threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port, use_reloader=False), daemon=True)
    flask_thread.start()
    logger.info(f"🌐 Flask web paneli {port} portunda başlatıldı.")

    # Telegram botunu ana thread'de (main thread) çalıştırıyoruz
    logger.info("🤖 Telegram bot polling başlatılıyor...")
    app_bot = ApplicationBuilder().token(TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CommandHandler("prim", give_premium))
    app_bot.add_handler(CallbackQueryHandler(button_callback))
    app_bot.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    bot_running = True
    app_bot.run_polling(drop_pending_updates=True)import os
import json
import logging
import asyncio
import time
import threading
from datetime import date
from flask import Flask, render_template_string, jsonify
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# --- CANLI LOG YAKALAYICI (WEB PANEL İÇİN) ---
class MemoryLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.logs = []

    def emit(self, record):
        log_entry = self.format(record)
        self.logs.append(log_entry)
        if len(self.logs) > 150:
            self.logs.pop(0)

memory_handler = MemoryLogHandler()
memory_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
logger.addHandler(memory_handler)

TOKEN = "8697686670:AAHZX2U5Wx0jZwVfHzf9SCdI1mB-mtB1c9s"
ADMIN_IDS = [8522767291]
DB_FILE = "database.json"

app = Flask(__name__)
bot_running = False

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <title>Aliens_eye Bot Dashboard</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #0f172a; color: #f8fafc; margin: 0; padding: 20px; }
        .container { max-width: 800px; margin: 0 auto; background: #1e293b; padding: 25px; border-radius: 12px; box-shadow: 0 10px 25px rgba(0,0,0,0.3); }
        h1 { color: #38bdf8; margin-top: 0; }
        .status-box { display: flex; align-items: center; justify-content: space-between; background: #0f172a; padding: 15px 20px; border-radius: 8px; margin-bottom: 20px; border: 1px solid #334155; }
        .badge { padding: 6px 12px; border-radius: 20px; font-weight: bold; font-size: 14px; }
        .online { background: #065f46; color: #34d399; }
        .offline { background: #7f1d1d; color: #f87171; }
        pre { background: #0f172a; padding: 15px; border-radius: 8px; overflow-x: auto; height: 300px; border: 1px solid #334155; color: #a5f3fc; font-size: 13px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🤖 Aliens_eye Bot Dashboard</h1>
        <div class="status-box">
            <div>
                <strong>Bot Durumu:</strong> <span id="statusBadge" class="badge online">Kontrol Ediliyor...</span>
            </div>
        </div>
        <h3>📋 Canlı Sistem Logları</h3>
        <pre id="logBox">Loglar yükleniyor...</pre>
    </div>
    <script>
        async function fetchLogs() {
            try {
                let response = await fetch('/logs');
                let data = await response.json();
                document.getElementById('logBox').innerText = data.logs;
                let badge = document.getElementById('statusBadge');
                badge.className = data.status === 'online' ? 'badge online' : 'badge offline';
                badge.innerText = data.status === 'online' ? 'Aktif & Çalışıyor' : 'Durduruldu';
            } catch (e) { console.error("Loglar alınamadı"); }
        }
        setInterval(fetchLogs, 3000);
        fetchLogs();
    </script>
</body>
</html>
"""

def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"premium": [], "usage": {}}

def save_db(data):
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"DB Kaydetme Hatası: {e}")

user_targets = {}

# --- ÖNCELİKLİ KUYRUK YÖNETİCİSİ ---
class ScanQueueManager:
    def __init__(self, max_concurrent=1):
        self.max_concurrent = max_concurrent
        self.active_scans = 0
        self.queue = []

    async def acquire(self, is_premium: bool):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        priority = 0 if is_premium else 1
        entry_time = time.time()

        entry = (priority, entry_time, future)
        self.queue.append(entry)
        self.queue.sort(key=lambda x: (x[0], x[1]))

        if self.active_scans < self.max_concurrent and self.queue[0] == entry:
            self.queue.pop(0)
            self.active_scans += 1
            return 0

        while True:
            await future
            if self.active_scans < self.max_concurrent and self.queue and self.queue[0] == entry:
                self.queue.pop(0)
                self.active_scans += 1
                break
            else:
                future = loop.create_future()
                entry = (priority, entry_time, future)
                for i, item in enumerate(self.queue):
                    if item[1] == entry_time:
                        self.queue[i] = entry
                        break
                self.queue.sort(key=lambda x: (x[0], x[1]))

        return self.get_current_position(entry_time)

    def release(self):
        self.active_scans = max(0, self.active_scans - 1)
        if self.queue and self.active_scans < self.max_concurrent:
            self.active_scans += 1
            _, _, future = self.queue.pop(0)
            if not future.done():
                future.set_result(True)

    def get_current_position(self, entry_time: float) -> int:
        sorted_q = sorted(self.queue, key=lambda x: (x[0], x[1]))
        for idx, item in enumerate(sorted_q):
            if item[1] == entry_time:
                return idx + 1
        return len(self.queue)

scan_manager = ScanQueueManager(max_concurrent=1)

# --- /START KOMUTU ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    logger.info(f"📥 /start komutu alındı: {user.full_name} (ID: {user_id})")
    
    db = load_db()
    is_premium = user_id in db["premium"] or user_id in ADMIN_IDS

    if is_premium:
        subscription_status = "👑 **VIP / Premium Abonelik (Sınırsız)**"
        keyboard = [[InlineKeyboardButton("💬 Destek & İletişim (@machaa4)", url="https://t.me/machaa4")]]
    else:
        subscription_status = "🆓 **Free Plan (Günde 2 Hak)**"
        keyboard = [[InlineKeyboardButton("💎 Premium Satın Al (@machaa4)", url="https://t.me/machaa4")]]

    reply_markup = InlineKeyboardMarkup(keyboard)
    welcome_text = (
        f"🤖 **Hoş geldin, {user.first_name}!**\n"
        f"🌐 *Aliens_eye AI-OSINT Altyapısı Aktif*\n\n"
        f"📋 **Hesap Durumun:** {subscription_status}\n\n"
        f"🔍 Sorgulamak istediğin **kullanıcı adını** doğrudan mesaj olarak gönder."
    )
    await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")

# --- ADMIN: PREMIUM VERME ---
async def give_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Bu komutu kullanmaya yetkin yok.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Kullanım: `/prim <Kullanici_ID>`", parse_mode="Markdown")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Geçersiz Kullanıcı ID.")
        return

    db = load_db()
    if target_id not in db["premium"]:
        db["premium"].append(target_id)
        save_db(db)
        await update.message.reply_text(f"✅ Başarılı! `{target_id}` ID'li kullanıcıya **Premium Abonelik** tanımlandı.", parse_mode="Markdown")
        try:
            await context.bot.send_message(chat_id=target_id, text="🎉 **Tebrikler!** Hesabına yönetici tarafından **👑 Premium Abonelik** tanımlandı.", parse_mode="Markdown")
        except Exception:
            pass
    else:
        await update.message.reply_text(f"ℹ️ `{target_id}` zaten Premium üyeliğe sahip.", parse_mode="Markdown")

# --- KULLANICI MESAJI VE LİMİT ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    if text.startswith("/"): return

    username = text.split()[0]
    user_id = user.id
    today_str = str(date.today())

    db = load_db()
    is_premium = user_id in db["premium"] or user_id in ADMIN_IDS

    if not is_premium:
        if user_id not in db["usage"] or db["usage"][user_id]["date"] != today_str:
            db["usage"][user_id] = {"date": today_str, "count": 0}

        if db["usage"][user_id]["count"] >= 2:
            keyboard = [[InlineKeyboardButton("💎 Premium Satın Al (@machaa4)", url="https://t.me/machaa4")]]
            await update.message.reply_text("⚠️ Günlük ücretsiz tarama hakkın (2/2) doldu!", reply_markup=InlineKeyboardMarkup(keyboard))
            return

    user_targets[user_id] = username

    if is_premium:
        keyboard = [
            [InlineKeyboardButton("🔍 Basic Mod", callback_data="scan_basic")],
            [InlineKeyboardButton("⚡ Intermediate Mod", callback_data="scan_intermediate")],
            [InlineKeyboardButton("🚀 Advanced Mod", callback_data="scan_advanced")]
        ]
        await update.message.reply_text(f"👑 **VIP Üye**\n`{username}` için mod seç:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        db["usage"][user_id]["count"] += 1
        save_db(db)
        remaining = 2 - db["usage"][user_id]["count"]
        status_msg = await update.message.reply_text(f"🔍 `{username}` kuyruğa ekleniyor...", parse_mode="Markdown")
        asyncio.create_task(handle_scan_with_queue(update, context, user_id, username, "basic", f"Abonelik: Free (Kalan: {remaining}/2)", status_msg))

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if user_id not in user_targets:
        await query.edit_message_text("⚠️ Zaman aşımı. Lütfen kullanıcı adını tekrar gönder.")
        return

    username = user_targets[user_id]
    mode = data.replace("scan_", "")

    status_msg = await query.edit_message_text(f"👑 `{username}` hedefi **{mode.upper()}** modunda kuyruğa alınıyor...", parse_mode="Markdown")
    asyncio.create_task(handle_scan_with_queue(update, context, user_id, username, mode, "Abonelik: 👑 VIP (Sınırsız)", status_msg))

async def handle_scan_with_queue(update, context, user_id, username, mode, footer_info, status_msg):
    db = load_db()
    position = await scan_manager.acquire(user_id in db["premium"] or user_id in ADMIN_IDS)
    
    if position > 1:
        try: await status_msg.edit_text(f"⚠️ Sıradaki yeriniz: **{position}**", parse_mode="Markdown")
        except Exception: pass
    else:
        try: await status_msg.edit_text(f"🔍 `{username}` taranıyor...", parse_mode="Markdown")
        except Exception: pass

    try:
        await execute_scan(status_msg.chat.id, context, user_id, username, mode, footer_info, status_msg.message_id)
    finally:
        scan_manager.release()

async def execute_scan(chat_id, context, user_id, username, mode, footer_info, status_message_id):
    logger.info(f"🔍 [TARAMA] Kullanıcı: {user_id} | Mod: {mode} | Hedef: {username}")
    try:
        sites = {
            "GitHub": f"https://github.com/{username}",
            "Telegram": f"https://t.me/{username}",
            "Twitter": f"https://twitter.com/{username}",
            "TikTok": f"https://www.tiktok.com/@{username}",
            "Instagram": f"https://www.instagram.com/{username}/"
        }

        found_accounts = []
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
            for name, url in sites.items():
                try:
                    res = await client.get(url)
                    if res.status_code == 200:
                        found_accounts.append(url)
                except Exception:
                    pass

        try: await context.bot.delete_message(chat_id=chat_id, message_id=status_message_id)
        except Exception: pass

        if found_accounts:
            txt_content = f"=== MACHA OSINT TARGET REPORT ===\nTarget: {username}\nTotal Found: {len(found_accounts)}\n\n"
            for index, acc in enumerate(found_accounts, 1):
                txt_content += f"{index}. {acc}\n"
            
            file_path = f"{username}_report.txt"
            with open(file_path, "w", encoding="utf-8") as f: f.write(txt_content)

            with open(file_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id, 
                    document=InputFile(f, filename=file_path), 
                    caption=f"🎯 **{username}** taraması tamamlandı!\n📊 Toplam: `{len(found_accounts)}`\n\nℹ️ *{footer_info}*", 
                    parse_mode="Markdown"
                )
            if os.path.exists(file_path): os.remove(file_path)
        else:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ `{username}` için sonuç bulunamadı.\n\nℹ️ *{footer_info}*", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Tarama Hatası: {e}", exc_info=True)

@app.route('/')
def dashboard():
    return render_template_string(HTML_TEMPLATE)

@app.route('/logs')
def get_logs():
    global bot_running
    log_text = "\n".join(memory_handler.logs) if memory_handler.logs else "Log akışı bekleniyor..."
    return jsonify({"status": "online" if bot_running else "offline", "logs": log_text})

if __name__ == "__main__":
    # Flask web sunucusunu arka planda çalıştırıyoruz
    port = int(os.environ.get("PORT", 10000))
    flask_thread = threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port, use_reloader=False), daemon=True)
    flask_thread.start()
    logger.info(f"🌐 Flask web paneli {port} portunda başlatıldı.")

    # Telegram botunu ana thread'de (main thread) çalıştırıyoruz
    logger.info("🤖 Telegram bot polling başlatılıyor...")
    app_bot = ApplicationBuilder().token(TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CommandHandler("prim", give_premium))
    app_bot.add_handler(CallbackQueryHandler(button_callback))
    app_bot.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    bot_running = True
    app_bot.run_polling(drop_pending_updates=True)
