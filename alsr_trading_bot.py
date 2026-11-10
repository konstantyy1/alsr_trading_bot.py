import logging
import os
import threading
from datetime import datetime, time
import pytz
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- FLASK ДЛЯ RENDER (Щоб сервіс не засинав) ---
app_flask = Flask(__name__)

@app_flask.route("/")
def home():
    return "ALSR Trading Bot is running 24/7!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)

# --- НАЛАШТУВАННЯ БОТА ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_USERNAME = "@konstantyy1"
TIMEZONE = pytz.timezone("Europe/Warsaw")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

def generate_signal(session_name: str) -> str:
    now = datetime.now(TIMEZONE).strftime("%d.%m.%Y")
    return (
        f"🚨 <b>ALSR INTRADAY SIGNAL — EUR/USD</b> 🚨\n"
        f"📅 <b>Дата:</b> {now}\n"
        f"⏰ <b>Сесія:</b> {session_name}\n"
        f"-----------------------------------\n"
        f"📈 <b>Напрямок:</b> SHORT (Sell Limit)\n"
        f"🎯 <b>Зона входу (Entry):</b> 1.07350 – 1.07380\n"
        f"🛑 <b>Stop Loss:</b> 1.07460 (11 Pips)\n"
        f"🎯 <b>Take Profit (TP1 - 70%):</b> 1.07200 (1:1.5 RRR)\n"
        f"🚀 <b>Take Profit (TP2 - 30%):</b> 1.07070 (1:2.5 RRR)\n"
        f"-----------------------------------\n"
        f"🛡 <b>Ризик на угоду:</b> 0.35% ($35 на $10k FTMO)\n"
        f"⚠️ <b>Правило дня:</b> Максимум 2 стоп-лоси на день! Якщо TP1 взято — SL в BU."
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    context.bot_data["user_chat_id"] = chat_id
    
    await update.message.reply_html(
        f"Вітаю, {user.mention_html()}!\n\n"
        f"🤖 <b>ALSR Signals Bot</b> успішно активовано у хмарі Render.\n"
        f"Ви підключені до системи сповіщень для акаунта <b>{TARGET_USERNAME}</b>.\n\n"
        f"⏰ <b>Розклад сигналів (Час Poznań):</b>\n"
        f"• <b>London Session:</b> 09:30\n"
        f"• <b>New York Session:</b> 14:30\n\n"
        f"Використовуйте /test_signal для перевірки доставки."
    )

async def test_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    signal = generate_signal("Test Session (Ручна перевірка)")
    await update.message.reply_html(signal)

async def send_london_signal(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.bot_data.get("user_chat_id")
    if chat_id:
        signal = generate_signal("London Open (09:30 Poznań)")
        await context.bot.send_message(chat_id=chat_id, text=signal, parse_mode="HTML")

async def send_ny_signal(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.bot_data.get("user_chat_id")
    if chat_id:
        signal = generate_signal("New York Open (14:30 Poznań)")
        await context.bot.send_message(chat_id=chat_id, text=signal, parse_mode="HTML")

def main():
    # Запускаємо веб-сервер у фоновому потоці для Render
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()

    # Запускаємо Telegram бота
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("test_signal", test_signal))

    job_queue = app.job_queue
    job_queue.run_daily(send_london_signal, time=time(hour=9, minute=30, tzinfo=TIMEZONE), days=(1, 2, 3, 4, 5))
    job_queue.run_daily(send_ny_signal, time=time(hour=14, minute=30, tzinfo=TIMEZONE), days=(1, 2, 3, 4, 5))

    print("🤖 Бот та веб-сервер успішно запущені!")
    app.run_polling()

if __name__ == "__main__":
    main()
