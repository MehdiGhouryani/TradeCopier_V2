# CoreService/core/telegram_bot.py
#
# این فایل، منطق کامل ربات تلگرام (مدیریت و هشدار) را پیاده‌سازی می‌کند.
# این ربات جایگزین config_bot.py و log_watcher.py می‌شود.

import os
import logging
import asyncio
from dotenv import load_dotenv
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
from telegram.error import TelegramError

from . import database # وارد کردن لایه دیتابیس برای اجرای دستورات

# بارگذاری متغیرهای محیطی از .env
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID"))
except (ValueError, TypeError):
    logging.critical("ADMIN_ID در فایل .env به درستی تنظیم نشده است.")
    exit()

# صف هشدارهایی که از سرور ZMQ می‌آیند
# سرور ZMQ پیام‌ها را در این صف می‌گذارد و این ربات آن‌ها را می‌خواند و ارسال می‌کند
alert_queue: asyncio.Queue = None

logger = logging.getLogger(__name__)

# --- دکوراتور (Decorator) برای محدود کردن دسترسی به ادمین ---
def admin_only(func):
    """دکوراتور برای اطمینان از اینکه فقط ادمین از دستور استفاده می‌کند"""
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id != ADMIN_ID:
            logger.warning(f"دسترسی غیرمجاز توسط کاربر {user_id}")
            if update.message:
                await update.message.reply_text("❌ شما مجاز به استفاده از این ربات نیستید.")
            elif update.callback_query:
                await update.callback_query.answer("❌ دسترسی غیرمجاز", show_alert=True)
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- دستورات اصلی ربات ---

@admin_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دستور /start - نمایش منوی اصلی و وضعیت کلی"""
    
    keyboard = [
        [InlineKeyboardButton("📊 نمایش وضعیت کلی", callback_data="status:main")],
        [InlineKeyboardButton("📈 آمار معاملات", callback_data="stats:main")],
        [InlineKeyboardButton("🛡️ مدیریت حساب‌های کپی", callback_data="copy:main")],
        [InlineKeyboardButton("📡 مدیریت منابع (مستر)", callback_data="source:main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text("به ربات مدیریت TradeCopier Professional خوش آمدید. لطفاً یک گزینه را انتخاب کنید:", reply_markup=reply_markup)

@admin_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش وضعیت کلی سیستم با فراخوانی دیتابیس"""
    query = update.callback_query
    if query:
        await query.answer("در حال دریافت گزارش وضعیت...")

    try:
        # فراخوانی تابع از database.py برای دریافت گزارش کامل
        report_data = database.get_full_status_report()
        
        if not report_data:
            await query.edit_message_text("هیچ حساب کپی فعالی در سیستم تعریف نشده است.")
            return

        message = "📊 *گزارش وضعیت سیستم*\n\n"
        for copy_acc in report_data:
            message += f"🛡️ *حساب: {copy_acc.name}* (`{copy_acc.copy_id_str}`)\n"
            if copy_acc.settings:
                message += f"  ▫️ ریسک روزانه: `{copy_acc.settings.daily_drawdown_percent}`٪\n"
            
            if not copy_acc.mappings:
                message += "  ▫️ *اتصالات: ۰*\n"
            else:
                message += f"  ▫️ *اتصالات: {len(copy_acc.mappings)}*\n"
                for m in copy_acc.mappings:
                    status = "✅" if m.is_enabled else "🛑"
                    message += f"    {status} ⟵ *{m.source_account.name}* (`{m.source_account.source_id_str}`)\n"
                    message += f"        (حجم: {m.volume_type} {m.volume_value})\n"
            message += "\n"

        keyboard = [
            [InlineKeyboardButton("🔄 به‌روزرسانی", callback_data="status:main")],
            [InlineKeyboardButton("🔙 بازگشت به منو", callback_data="main_menu_placeholder")] # (در گام بعد تکمیل می‌شود)
        ]
        await query.edit_message_text(message, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        logger.error(f"خطا در دریافت گزارش وضعیت: {e}")
        await query.edit_message_text(f"❌ خطایی در هنگام دریافت گزارش رخ داد:\n`{e}`", parse_mode=ParseMode.MARKDOWN)


# --- تسک پس‌زمینه (Background Task) برای ارسال هشدار ---

async def alert_sender_task(bot: BotCommand):
    """
    این تسک به صورت دائم در پس‌زمینه اجرا می‌شود.
    به صف هشدار (alert_queue) گوش می‌دهد و هر پیامی که سرور ZMQ
    در آن قرار دهد را بلافاصله برای ادمین ارسال می‌کند.
    """
    logger.info("تسک ارسال‌کننده هشدار (Alert Sender) راه‌اندازی شد.")
    if not alert_queue:
        logger.error("صف هشدار (alert_queue) مقداردهی نشده است!")
        return

    while True:
        try:
            # منتظر دریافت پیام از صف
            alert_message = await alert_queue.get()
            
            if not alert_message:
                continue

            # ارسال پیام برای ادمین
            await bot.send_message(chat_id=ADMIN_ID, text=alert_message, parse_mode=ParseMode.MARKDOWN)
            
        except TelegramError as e:
            logger.error(f"خطا در ارسال هشدار تلگرام: {e}")
        except Exception as e:
            logger.error(f"خطای پیش‌بینی نشده در تسک هشدار: {e}")
        finally:
            alert_queue.task_done()


# --- تابع اصلی راه‌اندازی ربات ---

async def run(queue: asyncio.Queue):
    """
    تابع اصلی راه‌اندازی و اجرای ربات تلگرام.
    این تابع توسط main.py فراخوانی می‌شود.
    """
    global alert_queue
    alert_queue = queue # دریافت صف مشترک از main.py
    
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN در فایل .env تنظیم نشده است. ربات تلگرام راه‌اندازی نمی‌شود.")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    # --- ثبت دستورات ---
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(status_command, pattern="^status:main$"))
    # (سایر هندلرها در گام‌های بعد اضافه می‌شوند)
    
    # ثبت دستورات در منوی تلگرام
    commands = [
        BotCommand("start", "شروع به کار و نمایش منوی اصلی"),
    ]
    await application.bot.set_my_commands(commands)
    
    # --- راه‌اندازی تسک‌های پس‌زمینه ---
    
    # ۱. تسک ارسال هشدارهای آنی (دریافتی از ZMQ)
    asyncio.create_task(alert_sender_task(application.bot))
    
    # ۲. اجرای خود ربات (برای پاسخ به دستورات)
    logger.info("ربات تلگرام در حال راه‌اندازی (Polling)...")
    await application.run_polling(allowed_updates=Update.ALL_TYPES)