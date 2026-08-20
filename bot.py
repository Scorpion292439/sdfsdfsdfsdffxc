import os
import json
import logging
import asyncio
import time
import threading
from datetime import date
from flask import Flask, render_template_string, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# --- WEB PANEL İÇİN LOG YAKALAYICI ---
class MemoryLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.logs = []
    def emit(self, record):
        self.logs.append(self.format(record))
        if len(self.logs) > 150: self.logs.pop(0)

memory_handler = MemoryLogHandler()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
logger.addHandler(memory_handler)

TOKEN = "8697686670:AAHZX2U5Wx0jZwVfHzf9SCdI1mB-mtB1c9s"
ADMIN_IDS = [8522767291]
DB_FILE = "database.json"

app = Flask(__name__, template_folder='.')
bot_running = False

# --- BASİT HTML DASHBOARD ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>Bot Durum</title></head>
<body style="background:#0f172a; color:white; font-family:sans-serif; padding:20px;">
    <h1>🤖 Aliens_eye Bot</h1>
    <p>Durum: <span id="status">Bekliyor...</span></p>
    <pre id="logs" style="background:#000; padding:15px;"></pre>
    <script>
        async function update() {
            let res = await fetch('/logs');
            let data = await res.json();
            document.getElementById('status').innerText = data.status;
            document.getElementById('logs').innerText = data.logs;
        }
        setInterval(update, 3000);
        update();
    </script>
</body>
</html>
"""

def load_db():
    if not os.path.exists(DB_FILE): return {"premium": [], "usage": {}}
    with open(DB_FILE, "r") as f: return json.load(f)

def save_db(data):
    with open(DB_FILE, "w") as f: json.dump(data, f)

# --- BOT MANTIĞI ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Aliens_eye Aktif. Kullanıcı adı gönder.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.message.text
    await update.message.reply_text(f"🔍 {username} aranıyor... (Bu modül artık tek dosyada!)")
    # Tarama mantığını buraya genişletebilirsin
    logger.info(f"Taranan: {username}")

@app.route('/')
def dashboard():
    return render_template_string(HTML_TEMPLATE)

@app.route('/logs')
def get_logs():
    return jsonify({"status": "online" if bot_running else "offline", "logs": "\n".join(memory_handler.logs)})

def run_bot():
    global bot_running
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app_bot = ApplicationBuilder().token(TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    bot_running = True
    app_bot.run_polling()

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
