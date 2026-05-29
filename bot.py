import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

# Setup logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Get your Telegram Token from environment variables
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Your live GitHub Pages Mini App URL
    mini_app_url = "https://github.io"
    
    keyboard = [
        [InlineKeyboardButton("📱 Open Quiz App", web_app=WebAppInfo(url=mini_app_url))]
    ]
    
    await update.message.reply_text(
        "👋 **Xush kelibsiz!**\n\nPDF yoki Word fayllaringizdan tezkor testlar yaratish uchun quyidagi tugmani bosing:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

def main():
    if not TELEGRAM_TOKEN:
        logging.error("TELEGRAM_TOKEN is missing!")
        return

    # Build the application
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Add handler for /start command
    application.add_handler(CommandHandler("start", start))
    
    # Start the Bot
    logging.info("Starting Telegram Bot...")
    application.run_polling()

if __name__ == '__main__':
    main()
