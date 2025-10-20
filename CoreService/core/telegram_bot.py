from email.mime import application
import os
import logging
import asyncio
from dotenv import load_dotenv
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from telegram.constants import ParseMode
from telegram.error import TelegramError, BadRequest
from functools import wraps
from telegram.helpers import escape_markdown 
from . import database 
from sqlalchemy.orm import joinedload
import traceback
import json
import datetime




# --- State ها برای ConversationHandler ---
(
    # منابع
    SOURCE_NAME, EDIT_SOURCE_NAME,
    # حساب‌های کپی
    COPY_NAME, COPY_ID_STR, COPY_DD, EDIT_COPY_NAME,
    # تنظیمات حساب کپی
    EDIT_COPY_SETTING_DD, EDIT_COPY_SETTING_ALERT,
    # --- جدید: اتصالات ---
    CONN_VOLUME_VALUE, CONN_SYMBOLS, CONN_LIMIT_VALUE,

) = range(11)




load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID"))
except (ValueError, TypeError):
    logging.critical("ADMIN_ID در فایل .env به درستی تنظیم نشده است.")
    exit()

alert_queue: asyncio.Queue = None
logger = logging.getLogger(__name__)

def admin_only(func):
    """دکوراتور برای محدود کردن دسترسی به ادمین."""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user or user.id != ADMIN_ID:
            log_extra = {'user_id': user.id, 'username': f"@{user.username}" if user.username else "N/A", 'status': 'denied'}
            action_attempt = "N/A"
            if update.callback_query:
                action_attempt = f"callback:{update.callback_query.data}"
            elif update.message and update.message.text:
                action_attempt = f"command:{update.message.text}"
            log_extra['action_attempt'] = action_attempt
            logger.warning("Unauthorized access attempt", extra=log_extra)
            unauthorized_text = "❌ شما مجاز به استفاده از این ربات نیستید."
            if update.callback_query:
                await update.callback_query.answer(unauthorized_text, show_alert=True)
            elif update.message:
                await update.message.reply_text(unauthorized_text)
            return
        log_extra = {'user_id': user.id, 'status': 'start'}
        if user.username:
            log_extra['username'] = f"@{user.username}"
        message_log = "Admin action received"
        if update.callback_query:
            log_extra['callback_data'] = update.callback_query.data
            message_log = "Admin callback received"
        elif update.message and update.message.text:
            if update.message.text.startswith('/'):
                log_extra['command'] = update.message.text
                message_log = "Admin command received"
            elif context.user_data.get('waiting_for'):
                log_extra['input_for'] = context.user_data.get('waiting_for')
                message_log = "Admin text input received"
        logger.info(message_log, extra=log_extra)
        return await func(update, context, *args, **kwargs)
    return wrapped

@admin_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش منوی اصلی و خوش‌آمدگویی."""
    keyboard = [
        [InlineKeyboardButton("📊 نمایش وضعیت کلی", callback_data="status:main")],
        [InlineKeyboardButton("📈 آمار معاملات", callback_data="stats:main")],
        [InlineKeyboardButton("📡 مدیریت منابع (مستر)", callback_data="sources:main")],
        [InlineKeyboardButton("🛡️ مدیریت حساب‌های کپی", callback_data="copy:main")],
        [InlineKeyboardButton("🔗 مدیریت اتصالات", callback_data="conn:main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "به ربات مدیریت TradeCopier Professional (V2) خوش آمدید.\nلطفاً یک گزینه را انتخاب کنید:"
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                logger.warning("Failed to edit message to main menu", extra={'error': str(e)})
        await update.callback_query.answer()
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

@admin_only
async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بازگشت به منوی اصلی."""
    await start_command(update, context)

@admin_only
async def sources_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش منوی اصلی مدیریت منابع."""
    query = update.callback_query
    await query.answer()
    try:
        with database.get_db_session() as db:
            sources = db.query(database.SourceAccount).order_by(database.SourceAccount.id).all()
    except Exception as e:
        logger.error("Failed to query sources from database", exc_info=True)
        await query.edit_message_text("❌ خطایی در خواندن لیست منابع رخ داد.")
        return
    keyboard = [[InlineKeyboardButton(escape_markdown(s.name, version=2), callback_data=f"sources:select:{s.id}")] for s in sources]
    keyboard.append([InlineKeyboardButton("➕ افزودن منبع جدید", callback_data="sources:add:start")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="main_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "مدیریت منابع (مستر):\nیک منبع را برای ویرایش انتخاب کنید یا منبع جدیدی اضافه کنید:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN_V2
    )
    return ConversationHandler.END

@admin_only
async def sources_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شروع فرآیند افزودن منبع جدید."""
    query = update.callback_query
    await query.answer()
    cancel_keyboard = [['/cancel']]
    reply_markup = ReplyKeyboardMarkup(cancel_keyboard, one_time_keyboard=True)
    await query.edit_message_text(
        "لطفا نام نمایشی برای منبع جدید را وارد کنید:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو و بازگشت", callback_data="sources:main")]])
    )
    logger.info("Starting add source conversation, waiting for name.", extra={'user_id': update.effective_user.id})
    return SOURCE_NAME


async def sources_add_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دریافت و ذخیره نام منبع جدید."""
    user = update.effective_user
    source_name = update.message.text.strip()
    log_extra = {'user_id': user.id, 'input_for': 'SOURCE_NAME', 'text_received': source_name}
    if not source_name:
        await update.message.reply_text("❌ نام نمی‌تواند خالی باشد. لطفاً نام معتبری وارد کنید یا با /cancel لغو کنید.")
        return SOURCE_NAME
    try:
        new_source = database.add_source_account(name=source_name)
        success_message = (
            f"✅ منبع *{escape_markdown(new_source.name, 2)}* با موفقیت افزوده شد\\.\n\n"
            f"▫️ شناسه خودکار: `{escape_markdown(new_source.source_id_str, 2)}`"
        )
        await update.message.reply_text(
            success_message,
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        log_extra.update({'status': 'success', 'entity_id': new_source.id})
        logger.info("New source added via Telegram bot.", extra=log_extra)
        
        keyboard = [[InlineKeyboardButton("🔙 بازگشت به لیست منابع", callback_data="sources:main")]]
        await update.message.reply_text("برای ادامه مدیریت منابع، روی دکمه زیر کلیک کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
        
        return ConversationHandler.END
        
    except Exception as e:
        logger.error("Failed to add source account to database", exc_info=True, extra=log_extra)
        await update.message.reply_text(
            f"❌ خطایی در هنگام افزودن منبع رخ داد: {escape_markdown(str(e), 2)}\nلطفا دوباره تلاش کنید یا با /cancel لغو کنید.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return ConversationHandler.END






async def sources_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """لغو فرآیند افزودن یا ویرایش منبع."""
    user = update.effective_user
    logger.info(f"User {user.id} cancelled the operation.", extra={'user_id': user.id})
    await update.message.reply_text('عملیات لغو شد.', reply_markup=ReplyKeyboardRemove())
    await start_command(update, context)
    return ConversationHandler.END





@admin_only
async def sources_select_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش منوی مدیریت برای منبع انتخاب‌شده."""
    query = update.callback_query
    await query.answer()
    try:
        source_id = int(query.data.split(':')[-1])
        context.user_data['selected_source_id'] = source_id
    except (IndexError, ValueError):
        logger.error(f"Invalid callback data for source selection: {query.data}")
        await query.edit_message_text("❌ خطای داخلی: ID منبع نامعتبر است.")
        return
    source = None
    try:
        with database.get_db_session() as db:
            source = db.query(database.SourceAccount).filter(database.SourceAccount.id == source_id).first()
    except Exception as e:
        logger.error(f"Failed to query selected source (ID: {source_id})", exc_info=True)
        await query.edit_message_text("❌ خطایی در خواندن اطلاعات منبع رخ داد.")
        return
    if not source:
        await query.edit_message_text("❌ منبع مورد نظر یافت نشد (ممکن است حذف شده باشد).")
        await sources_main_menu(update, context)
        return
    keyboard = [
        [InlineKeyboardButton("✏️ ویرایش نام", callback_data=f"sources:edit_name:start:{source_id}")],
        [InlineKeyboardButton("🗑️ حذف منبع", callback_data=f"sources:delete:confirm:{source_id}")],
        [InlineKeyboardButton("🔙 بازگشت به لیست منابع", callback_data="sources:main")]
    ]
    await query.edit_message_text(
        f"مدیریت منبع *{escape_markdown(source.name, 2)}* (`{escape_markdown(source.source_id_str, 2)}`):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2
    )





@admin_only
async def sources_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش پیام تأیید حذف منبع."""
    query = update.callback_query
    await query.answer()
    try:
        source_id = int(query.data.split(':')[-1])
        context.user_data['selected_source_id'] = source_id
    except (IndexError, ValueError):
        logger.error(f"Invalid callback data for source delete confirm: {query.data}")
        await query.edit_message_text("❌ خطای داخلی: ID منبع نامعتبر است.")
        return
    source_name = "منبع انتخاب شده"
    try:
        with database.get_db_session() as db:
            source = db.query(database.SourceAccount.name).filter(database.SourceAccount.id == source_id).scalar()
            if source:
                source_name = source
    except Exception as e:
        logger.warning(f"Could not fetch source name for delete confirmation (ID: {source_id})", exc_info=True)
    keyboard = [
        [InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"sources:delete:execute:{source_id}")],
        [InlineKeyboardButton("❌ خیر، بازگشت", callback_data=f"sources:select:{source_id}")]
    ]
    confirmation_text = (
        f"آیا از حذف منبع *{escape_markdown(source_name, 2)}* و **تمام اتصالات مرتبط با آن** مطمئن هستید؟\n"
        f"این عمل غیرقابل بازگشت است\\."
    )
    await query.edit_message_text(
        confirmation_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2
    )





@admin_only
async def sources_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """اجرای عملیات حذف منبع."""
    query = update.callback_query
    await query.answer("در حال حذف منبع...")
    try:
        source_id = int(query.data.split(':')[-1])
    except (IndexError, ValueError):
        logger.error(f"Invalid callback data for source delete execute: {query.data}")
        await query.edit_message_text("❌ خطای داخلی: ID منبع نامعتبر است.")
        return
    source_name = f"منبع با ID {source_id}"
    try:
        with database.get_db_session() as db_read:
            name = db_read.query(database.SourceAccount.name).filter(database.SourceAccount.id == source_id).scalar()
            if name:
                source_name = name
        deleted = database.delete_source_account(source_id)
        if deleted:
            await query.edit_message_text(
                f"✅ منبع *{escape_markdown(source_name, 2)}* با موفقیت حذف شد\\.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به لیست منابع", callback_data="sources:main")]]),
                parse_mode=ParseMode.MARKDOWN_V2
            )
        else:
            await query.edit_message_text(
                f"⚠️ منبع *{escape_markdown(source_name, 2)}* یافت نشد \\(احتمالا قبلا حذف شده است\\)\\.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به لیست منابع", callback_data="sources:main")]]),
                parse_mode=ParseMode.MARKDOWN_V2
            )
    except Exception as e:
        logger.error(f"Failed to execute source deletion (ID: {source_id})", exc_info=True)
        await query.edit_message_text(
            f"❌ خطایی در هنگام حذف منبع رخ داد: {escape_markdown(str(e), 2)}\\.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به لیست منابع", callback_data="sources:main")]]),
            parse_mode=ParseMode.MARKDOWN_V2
        )
    finally:
        context.user_data.pop('selected_source_id', None)





@admin_only
async def sources_edit_name_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شروع فرآیند ویرایش نام منبع."""
    query = update.callback_query
    await query.answer()
    source_id = context.user_data.get('selected_source_id')
    if not source_id:
        await query.edit_message_text("❌ خطای داخلی: ID منبع انتخاب شده یافت نشد.")
        return ConversationHandler.END
    current_name = "منبع فعلی"
    try:
        with database.get_db_session() as db:
            name = db.query(database.SourceAccount.name).filter(database.SourceAccount.id == source_id).scalar()
            if name:
                current_name = name
    except Exception:
        logger.warning(f"Could not fetch current source name for edit prompt (ID: {source_id})")
    cancel_keyboard = [['/cancel']]
    reply_markup = ReplyKeyboardMarkup(cancel_keyboard, one_time_keyboard=True)
    await query.edit_message_text(
        f"نام فعلی: *{escape_markdown(current_name, 2)}*\n\nلطفاً نام نمایشی جدید را وارد کنید:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو و بازگشت", callback_data=f"sources:select:{source_id}")]])
    )
    logger.info(f"Starting edit source name conversation for ID {source_id}, waiting for new name.", extra={'user_id': update.effective_user.id, 'entity_id': source_id})
    return EDIT_SOURCE_NAME




async def sources_edit_name_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دریافت و ذخیره نام جدید منبع."""
    user = update.effective_user
    new_name = update.message.text.strip()
    source_id = context.user_data.get('selected_source_id')
    log_extra = {'user_id': user.id, 'input_for': 'EDIT_SOURCE_NAME', 'text_received': new_name, 'entity_id': source_id}
    if not new_name:
        await update.message.reply_text("❌ نام نمی‌تواند خالی باشد. لطفاً نام معتبری وارد کنید یا با /cancel لغو کنید.")
        return EDIT_SOURCE_NAME
    if not source_id:
        await update.message.reply_text("❌ خطای داخلی: ID منبع یافت نشد. با /cancel لغو کنید.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    try:
        updated_source = database.update_source_account_name(source_id=source_id, new_name=new_name)
        if updated_source:
            await update.message.reply_text(
                f"✅ نام منبع با موفقیت به *{escape_markdown(updated_source.name, 2)}* تغییر کرد\\.",
                reply_markup=ReplyKeyboardRemove(),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            log_extra['status'] = 'success'
            logger.info("Source name updated via Telegram bot.", extra=log_extra)
            keyboard = [[InlineKeyboardButton("🔙 بازگشت به منوی منبع", callback_data=f"sources:select:{source_id}")]]
            await update.message.reply_text("برای ادامه مدیریت این منبع، روی دکمه زیر کلیک کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await update.message.reply_text(
                "⚠️ منبع مورد نظر یافت نشد (ممکن است همزمان حذف شده باشد).",
                reply_markup=ReplyKeyboardRemove()
            )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Failed to update source name (ID: {source_id})", exc_info=True, extra=log_extra)
        await update.message.reply_text(
            f"❌ خطایی در هنگام ویرایش نام رخ داد: {escape_markdown(str(e), 2)}\nلطفا دوباره تلاش کنید یا با /cancel لغو کنید.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return ConversationHandler.END




# --- مدیریت حساب‌های کپی ---




@admin_only
async def copy_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش منوی اصلی مدیریت حساب‌های کپی."""
    query = update.callback_query
    await query.answer()

    copies = []
    try:
        with database.get_db_session() as db:
            copies = db.query(database.CopyAccount).order_by(database.CopyAccount.id).all()
    except Exception as e:
        logger.error("Failed to query copy accounts from database", exc_info=True)
        await query.edit_message_text("❌ خطایی در خواندن لیست حساب‌های کپی رخ داد.")
        return ConversationHandler.END # اگر در گفتگویی بودیم خارج شو

    keyboard = [
        [InlineKeyboardButton(
            escape_markdown(c.name, version=2),
            callback_data=f"copy:select:{c.id}"
        )] for c in copies
    ]
    keyboard.append([InlineKeyboardButton("➕ افزودن حساب کپی جدید", callback_data="copy:add:start")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="main_menu")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "مدیریت حساب‌های کپی (اسلیو):\nیک حساب را برای ویرایش انتخاب کنید یا حساب جدیدی اضافه کنید:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN_V2
    )
    return ConversationHandler.END # اگر در گفتگویی بودیم خارج شو

# --- Conversation: افزودن حساب کپی ---

@admin_only
async def copy_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شروع فرآیند افزودن حساب کپی جدید (پرسیدن نام)."""
    query = update.callback_query
    await query.answer()
    context.user_data.clear() # پاک کردن داده‌های قبلی
    await query.edit_message_text(
        "۱/۳: لطفاً نام نمایشی برای حساب کپی جدید را وارد کنید:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data="copy:main")]])
    )
    logger.info("Starting add copy conversation, waiting for name.", extra={'user_id': update.effective_user.id})
    return COPY_NAME

async def copy_add_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دریافت نام، ذخیره موقت و پرسیدن Copy ID Str."""
    user = update.effective_user
    copy_name = update.message.text.strip()
    log_extra = {'user_id': user.id, 'input_for': 'COPY_NAME', 'text_received': copy_name}

    if not copy_name:
        await update.message.reply_text("❌ نام نمی‌تواند خالی باشد. لطفاً نام معتبری وارد کنید یا با /cancel لغو کنید.")
        return COPY_NAME # در همین State بمان

    context.user_data['new_copy_name'] = copy_name
    logger.debug("Received copy name, waiting for copy_id_str.", extra=log_extra)
    await update.message.reply_text(
        "۲/۳: لطفاً شناسه منحصر به فرد (Copy ID Str) را وارد کنید (فقط حروف انگلیسی و اعداد، مثال: `CA` یا `Copy_1`).\nاین شناسه در تنظیمات اکسپرت کپی استفاده خواهد شد.",
         reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data="copy:main")]]), # دکمه لغو برای state بعدی
         parse_mode=ParseMode.MARKDOWN_V2
    )
    return COPY_ID_STR

async def copy_add_receive_id_str(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دریافت Copy ID Str، اعتبارسنجی، ذخیره موقت و پرسیدن DD%."""
    user = update.effective_user
    copy_id_str = update.message.text.strip()
    # اعتبارسنجی اولیه (می‌تواند کامل‌تر باشد)
    if not copy_id_str or not copy_id_str.isalnum() or ' ' in copy_id_str:
         await update.message.reply_text("❌ شناسه نامعتبر است. فقط از حروف انگلیسی و اعداد بدون فاصله استفاده کنید. (مثال: `CA2`).\nلطفاً دوباره وارد کنید یا با /cancel لغو کنید.", parse_mode=ParseMode.MARKDOWN_V2)
         return COPY_ID_STR

    log_extra = {'user_id': user.id, 'input_for': 'COPY_ID_STR', 'text_received': copy_id_str}

    # TODO: بررسی یکتایی copy_id_str در دیتابیس (نیاز به تابع در database.py)
    # if not database.is_copy_id_str_unique(copy_id_str):
    #     await update.message.reply_text(f"❌ شناسه '{escape_markdown(copy_id_str, 2)}' قبلاً استفاده شده است. لطفاً شناسه دیگری وارد کنید یا با /cancel لغو کنید.", parse_mode=ParseMode.MARKDOWN_V2)
    #     return COPY_ID_STR

    context.user_data['new_copy_id_str'] = copy_id_str
    logger.debug("Received copy_id_str, waiting for dd_percent.", extra=log_extra)
    await update.message.reply_text(
        "۳/۳: لطفاً درصد حد ضرر روزانه (Daily Drawdown Percent) را وارد کنید (عدد مثبت، مثال: `5.0`).\nبرای غیرفعال کردن عدد `0` را وارد کنید.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data="copy:main")]]),
        parse_mode=ParseMode.MARKDOWN_V2
    )
    return COPY_DD

async def copy_add_receive_dd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دریافت DD%، ذخیره حساب کپی جدید در دیتابیس و پایان گفتگو."""
    user = update.effective_user
    dd_percent_str = update.message.text.strip()
    log_extra = {'user_id': user.id, 'input_for': 'COPY_DD', 'text_received': dd_percent_str}

    try:
        dd_percent = float(dd_percent_str)
        if dd_percent < 0:
            raise ValueError("Percentage cannot be negative.")
    except ValueError:
        await update.message.reply_text("❌ ورودی نامعتبر است. لطفاً یک عدد مثبت (مانند `5.0` یا `0`) وارد کنید یا با /cancel لغو کنید.", parse_mode=ParseMode.MARKDOWN_V2)
        return COPY_DD # در همین State بمان

    copy_name = context.user_data.get('new_copy_name')
    copy_id_str = context.user_data.get('new_copy_id_str')

    if not copy_name or not copy_id_str:
        logger.error("Missing name or id_str in user_data during final step of add copy.", extra=log_extra)
        await update.message.reply_text("❌ خطای داخلی رخ داد. اطلاعات قبلی یافت نشد. با /cancel لغو کنید.")
        return ConversationHandler.END

    try:
        # فراخوانی تابع دیتابیس (که بعداً پیاده‌سازی می‌شود)
        # فرض می‌کنیم alert_percent پیش‌فرض 4.0 است
        new_copy = database.add_copy_account(
            name=copy_name,
            copy_id_str=copy_id_str,
            dd_percent=dd_percent,
            alert_percent=max(0, dd_percent - 1.0) # مثال: الرت 1% کمتر از DD
        )

        success_message = (
            f"✅ حساب کپی *{escape_markdown(new_copy.name, 2)}* با موفقیت افزوده شد\\.\n\n"
            f"▫️ شناسه: `{escape_markdown(new_copy.copy_id_str, 2)}`\n"
            f"▫️ حد ضرر روزانه: `{dd_percent:.2f}%`"
        )
        await update.message.reply_text(
            success_message,
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        log_extra.update({'status': 'success', 'entity_id': new_copy.id, 'details': {'name': copy_name, 'id_str': copy_id_str, 'dd': dd_percent}})
        logger.info("New copy account added via Telegram bot.", extra=log_extra)

        # نمایش منوی اصلی حساب‌های کپی
        # نیاز به query دارد -> ارسال دکمه بازگشت
        keyboard = [[InlineKeyboardButton("🔙 بازگشت به لیست حساب‌ها", callback_data="copy:main")]]
        await update.message.reply_text("برای ادامه مدیریت حساب‌های کپی، روی دکمه زیر کلیک کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END

    except ValueError as ve: # خطای احتمالی یکتا نبودن copy_id_str از دیتابیس
         logger.warning(f"Failed to add copy account due to validation error: {ve}", extra=log_extra)
         await update.message.reply_text(f"❌ {escape_markdown(str(ve), 2)}\nلطفا با /cancel لغو کرده و دوباره تلاش کنید.", parse_mode=ParseMode.MARKDOWN_V2)
         return ConversationHandler.END # پایان گفتگو

    except Exception as e:
        logger.error("Failed to add copy account to database", exc_info=True, extra=log_extra)
        await update.message.reply_text(
            f"❌ خطایی در هنگام افزودن حساب کپی رخ داد: {escape_markdown(str(e), 2)}\nلطفا دوباره تلاش کنید یا با /cancel لغو کنید.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return ConversationHandler.END

async def copy_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """لغو فرآیند افزودن یا ویرایش حساب کپی."""
    user = update.effective_user
    logger.info(f"User {user.id} cancelled the copy operation.", extra={'user_id': user.id})
    await update.message.reply_text(
        'عملیات لغو شد.',
        reply_markup=ReplyKeyboardRemove()
    )
    context.user_data.clear()
    # بازگشت به منوی اصلی حساب‌های کپی
    # نیاز به query دارد -> دکمه ارسال می‌کنیم
    keyboard = [[InlineKeyboardButton("🔙 بازگشت به لیست حساب‌ها", callback_data="copy:main")]]
    await update.message.reply_text("برای ادامه، به لیست حساب‌ها بازگردید:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END

# --- انتخاب حساب کپی و منوی مدیریت آن ---

@admin_only
async def copy_select_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش منوی مدیریت برای حساب کپی انتخاب شده."""
    query = update.callback_query
    await query.answer()
    try:
        copy_id = int(query.data.split(':')[-1])
        context.user_data['selected_copy_id'] = copy_id
    except (IndexError, ValueError):
        logger.error(f"Invalid callback data for copy selection: {query.data}")
        await query.edit_message_text("❌ خطای داخلی: ID حساب کپی نامعتبر است.")
        return

    copy_account = None
    try:
        with database.get_db_session() as db:
            # Fetch copy account with its settings eagerly
            copy_account = db.query(database.CopyAccount)\
                             .options(joinedload(database.CopyAccount.settings))\
                             .filter(database.CopyAccount.id == copy_id).first()
    except Exception as e:
        logger.error(f"Failed to query selected copy account (ID: {copy_id})", exc_info=True)
        await query.edit_message_text("❌ خطایی در خواندن اطلاعات حساب کپی رخ داد.")
        return

    if not copy_account:
        await query.edit_message_text("❌ حساب کپی مورد نظر یافت نشد (ممکن است حذف شده باشد).")
        # بازگشت به منوی اصلی کپی
        # await copy_main_menu(update, context) # نیاز به query دارد
        keyboard = [[InlineKeyboardButton("🔙 بازگشت به لیست حساب‌ها", callback_data="copy:main")]]
        await query.message.reply_text("برای ادامه، به لیست حساب‌ها بازگردید:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    keyboard = [
        [InlineKeyboardButton("✏️ ویرایش نام", callback_data=f"copy:edit_name:start:{copy_id}")],
        [InlineKeyboardButton("⚙️ تنظیمات (DD%, Alert%)", callback_data=f"copy:settings:menu:{copy_id}")],
        [InlineKeyboardButton("🗑️ حذف حساب", callback_data=f"copy:delete:confirm:{copy_id}")],
        [InlineKeyboardButton("🔙 بازگشت به لیست حساب‌ها", callback_data="copy:main")]
    ]

    await query.edit_message_text(
        f"مدیریت حساب کپی *{escape_markdown(copy_account.name, 2)}* (`{escape_markdown(copy_account.copy_id_str, 2)}`):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2
    )

# --- Conversation: ویرایش نام حساب کپی ---

@admin_only
async def copy_edit_name_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شروع فرآیند ویرایش نام حساب کپی."""
    query = update.callback_query
    await query.answer()
    copy_id = context.user_data.get('selected_copy_id')
    if not copy_id:
         await query.edit_message_text("❌ خطای داخلی: ID حساب کپی انتخاب شده یافت نشد.")
         return ConversationHandler.END
    current_name = "حساب کپی فعلی"
    try:
        with database.get_db_session() as db:
             name = db.query(database.CopyAccount.name).filter(database.CopyAccount.id == copy_id).scalar()
             if name: current_name = name
    except Exception:
         logger.warning(f"Could not fetch current copy account name for edit prompt (ID: {copy_id})")

    await query.edit_message_text(
        f"نام فعلی: *{escape_markdown(current_name, 2)}*\n\nلطفاً نام نمایشی جدید را وارد کنید:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو و بازگشت", callback_data=f"copy:select:{copy_id}")]])
    )
    logger.info(f"Starting edit copy name conversation for ID {copy_id}, waiting for new name.", extra={'user_id': update.effective_user.id, 'entity_id': copy_id})
    return EDIT_COPY_NAME

async def copy_edit_name_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دریافت و ذخیره نام جدید حساب کپی."""
    user = update.effective_user
    new_name = update.message.text.strip()
    copy_id = context.user_data.get('selected_copy_id')
    log_extra = {'user_id': user.id, 'input_for': 'EDIT_COPY_NAME', 'text_received': new_name, 'entity_id': copy_id}

    if not new_name:
        await update.message.reply_text("❌ نام نمی‌تواند خالی باشد. لطفاً نام معتبری وارد کنید یا با /cancel لغو کنید.")
        return EDIT_COPY_NAME
    if not copy_id:
         await update.message.reply_text("❌ خطای داخلی: ID حساب کپی یافت نشد. با /cancel لغو کنید.")
         return ConversationHandler.END

    try:
        updated_copy = database.update_copy_account_name(copy_id=copy_id, new_name=new_name)
        if updated_copy:
            await update.message.reply_text(
                f"✅ نام حساب کپی با موفقیت به *{escape_markdown(updated_copy.name, 2)}* تغییر کرد\\.",
                reply_markup=ReplyKeyboardRemove(),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            log_extra['status'] = 'success'
            logger.info("Copy account name updated via Telegram bot.", extra=log_extra)
            keyboard = [[InlineKeyboardButton("🔙 بازگشت به منوی حساب", callback_data=f"copy:select:{copy_id}")]]
            await update.message.reply_text("برای ادامه مدیریت این حساب، روی دکمه زیر کلیک کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
             await update.message.reply_text("⚠️ حساب کپی مورد نظر یافت نشد.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Failed to update copy account name (ID: {copy_id})", exc_info=True, extra=log_extra)
        await update.message.reply_text(
            f"❌ خطایی در هنگام ویرایش نام رخ داد: {escape_markdown(str(e), 2)}\nلطفا دوباره تلاش کنید یا با /cancel لغو کنید.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return ConversationHandler.END

# --- حذف حساب کپی ---

@admin_only
async def copy_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش پیام تأیید حذف حساب کپی."""
    query = update.callback_query
    await query.answer()
    try:
        copy_id = int(query.data.split(':')[-1])
        context.user_data['selected_copy_id'] = copy_id
    except (IndexError, ValueError):
         logger.error(f"Invalid callback data for copy delete confirm: {query.data}")
         await query.edit_message_text("❌ خطای داخلی: ID حساب کپی نامعتبر است.")
         return

    copy_name = "حساب کپی انتخاب شده"
    try:
        with database.get_db_session() as db:
            name = db.query(database.CopyAccount.name).filter(database.CopyAccount.id == copy_id).scalar()
            if name: copy_name = name
    except Exception:
         logger.warning(f"Could not fetch copy account name for delete confirmation (ID: {copy_id})")

    keyboard = [
        [InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"copy:delete:execute:{copy_id}")],
        [InlineKeyboardButton("❌ خیر، بازگشت", callback_data=f"copy:select:{copy_id}")]
    ]
    confirmation_text = (
        f"آیا از حذف حساب کپی *{escape_markdown(copy_name, 2)}* و **تمام تنظیمات و اتصالات مرتبط با آن** مطمئن هستید؟\n"
        f"این عمل غیرقابل بازگشت است\\."
    )
    await query.edit_message_text(
        confirmation_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2
    )

@admin_only
async def copy_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """اجرای عملیات حذف حساب کپی."""
    query = update.callback_query
    await query.answer("در حال حذف حساب کپی...")
    try:
        copy_id = int(query.data.split(':')[-1])
    except (IndexError, ValueError):
         logger.error(f"Invalid callback data for copy delete execute: {query.data}")
         await query.edit_message_text("❌ خطای داخلی: ID حساب کپی نامعتبر است.")
         return

    copy_name = f"حساب کپی با ID {copy_id}"
    try:
         with database.get_db_session() as db_read:
              name = db_read.query(database.CopyAccount.name).filter(database.CopyAccount.id == copy_id).scalar()
              if name: copy_name = name
         deleted = database.delete_copy_account(copy_id) # تابع دیتابیس که بعداً نوشته می‌شود
         if deleted:
             await query.edit_message_text(
                 f"✅ حساب کپی *{escape_markdown(copy_name, 2)}* با موفقیت حذف شد\\.",
                 reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به لیست حساب‌ها", callback_data="copy:main")]]),
                 parse_mode=ParseMode.MARKDOWN_V2
             )
         else:
             await query.edit_message_text(
                 f"⚠️ حساب کپی *{escape_markdown(copy_name, 2)}* یافت نشد.",
                 reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به لیست حساب‌ها", callback_data="copy:main")]]),
                 parse_mode=ParseMode.MARKDOWN_V2
             )
    except Exception as e:
        logger.error(f"Failed to execute copy account deletion (ID: {copy_id})", exc_info=True)
        await query.edit_message_text(
             f"❌ خطایی در هنگام حذف حساب کپی رخ داد: {escape_markdown(str(e), 2)}\\.",
             reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به لیست حساب‌ها", callback_data="copy:main")]]),
             parse_mode=ParseMode.MARKDOWN_V2
        )
    finally:
         context.user_data.pop('selected_copy_id', None)

# --- منو و Conversation: ویرایش تنظیمات حساب کپی ---

@admin_only
async def copy_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش منوی تنظیمات (DD%, Alert%) برای حساب کپی انتخاب شده."""
    query = update.callback_query
    await query.answer()
    copy_id = context.user_data.get('selected_copy_id')
    if not copy_id:
         await query.edit_message_text("❌ خطای داخلی: ID حساب کپی یافت نشد.")
         return ConversationHandler.END # از هر گفتگویی خارج شو

    settings = None
    copy_name = "حساب کپی"
    try:
        with database.get_db_session() as db:
            # خواندن تنظیمات
            copy_account = db.query(database.CopyAccount)\
                             .options(joinedload(database.CopyAccount.settings))\
                             .filter(database.CopyAccount.id == copy_id).first()
            if copy_account:
                copy_name = copy_account.name
                settings = copy_account.settings
    except Exception as e:
        logger.error(f"Failed to query copy settings (ID: {copy_id})", exc_info=True)
        await query.edit_message_text("❌ خطایی در خواندن تنظیمات رخ داد.")
        return ConversationHandler.END

    if not settings:
         # این حالت نباید اتفاق بیفتد چون هنگام افزودن، تنظیمات ساخته می‌شود
         logger.error(f"Settings not found for copy account ID: {copy_id}")
         await query.edit_message_text("❌ خطای داخلی: تنظیمات حساب یافت نشد.")
         return ConversationHandler.END

    dd_percent = settings.daily_drawdown_percent
    alert_percent = settings.alert_drawdown_percent

    keyboard = [
        [InlineKeyboardButton(f"حد ضرر روزانه: {dd_percent:.2f}%", callback_data=f"copy:settings:edit_dd:start:{copy_id}")],
        [InlineKeyboardButton(f"حد هشدار روزانه: {alert_percent:.2f}%", callback_data=f"copy:settings:edit_alert:start:{copy_id}")],
        # TODO: دکمه ریست فلگ DD در آینده
        [InlineKeyboardButton("🔙 بازگشت به منوی حساب", callback_data=f"copy:select:{copy_id}")]
    ]

    await query.edit_message_text(
        f"تنظیمات حساب کپی *{escape_markdown(copy_name, 2)}*:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2
    )
    return ConversationHandler.END # اگر در گفتگویی بودیم خارج شو

@admin_only
async def copy_settings_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شروع فرآیند ویرایش یک تنظیم خاص (DD% یا Alert%)."""
    query = update.callback_query
    await query.answer()
    try:
        parts = query.data.split(':')
        setting_type = parts[3] # 'dd' or 'alert'
        copy_id = int(parts[5])
        context.user_data['selected_copy_id'] = copy_id
        context.user_data['editing_setting'] = setting_type
    except (IndexError, ValueError):
         logger.error(f"Invalid callback data for starting settings edit: {query.data}")
         await query.edit_message_text("❌ خطای داخلی: داده نامعتبر است.")
         return ConversationHandler.END

    current_value = 0.0
    setting_name = ""
    next_state = None
    try:
        with database.get_db_session() as db:
            settings = db.query(database.CopySettings).filter(database.CopySettings.copy_account_id == copy_id).first()
            if settings:
                if setting_type == 'dd':
                     current_value = settings.daily_drawdown_percent
                     setting_name = "حد ضرر روزانه"
                     next_state = EDIT_COPY_SETTING_DD
                elif setting_type == 'alert':
                     current_value = settings.alert_drawdown_percent
                     setting_name = "حد هشدار روزانه"
                     next_state = EDIT_COPY_SETTING_ALERT
                else:
                     raise ValueError("Invalid setting type")
            else:
                await query.edit_message_text("❌ خطای داخلی: تنظیمات یافت نشد.")
                return ConversationHandler.END
    except Exception as e:
        logger.error(f"Failed to fetch current setting value (ID: {copy_id}, Type: {setting_type})", exc_info=True)
        await query.edit_message_text("❌ خطایی در خواندن مقدار فعلی رخ داد.")
        return ConversationHandler.END

    await query.edit_message_text(
        f"مقدار فعلی *{setting_name}*: `{current_value:.2f}%`\n\nلطفاً مقدار جدید را به درصد وارد کنید (عدد مثبت، مثال: `4.5`):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو و بازگشت", callback_data=f"copy:settings:menu:{copy_id}")]]) ,# بازگشت به منوی تنظیمات
        parse_mode=ParseMode.MARKDOWN_V2
    )
    logger.info(f"Starting edit copy setting conversation (ID: {copy_id}, Setting: {setting_type})", extra={'user_id': update.effective_user.id, 'entity_id': copy_id})
    return next_state

async def copy_settings_edit_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دریافت مقدار جدید تنظیمات، ذخیره و پایان گفتگو."""
    user = update.effective_user
    new_value_str = update.message.text.strip()
    copy_id = context.user_data.get('selected_copy_id')
    setting_type = context.user_data.get('editing_setting') # 'dd' or 'alert'
    log_extra = {'user_id': user.id, 'input_for': f'EDIT_COPY_SETTING_{setting_type.upper()}', 'text_received': new_value_str, 'entity_id': copy_id}

    try:
        new_value = float(new_value_str)
        if new_value < 0:
            raise ValueError("Percentage cannot be negative.")
    except ValueError:
        await update.message.reply_text("❌ ورودی نامعتبر است. لطفاً یک عدد مثبت (مانند `4.5` یا `0`) وارد کنید یا با /cancel لغو کنید.", parse_mode=ParseMode.MARKDOWN_V2)
        # تعیین state بعدی بر اساس نوع تنظیم
        return EDIT_COPY_SETTING_DD if setting_type == 'dd' else EDIT_COPY_SETTING_ALERT

    if not copy_id or not setting_type:
         logger.error("Missing copy_id or setting_type in user_data during settings edit receive.", extra=log_extra)
         await update.message.reply_text("❌ خطای داخلی رخ داد. با /cancel لغو کنید.")
         return ConversationHandler.END

    settings_data = {}
    setting_name = ""
    if setting_type == 'dd':
         settings_data['daily_drawdown_percent'] = new_value
         setting_name = "حد ضرر روزانه"
    elif setting_type == 'alert':
         settings_data['alert_drawdown_percent'] = new_value
         setting_name = "حد هشدار روزانه"

    try:
        # فراخوانی تابع دیتابیس (که بعداً پیاده‌سازی می‌شود)
        success = database.update_copy_settings(copy_id=copy_id, settings_data=settings_data)

        if success:
            await update.message.reply_text(
                f"✅ *{setting_name}* با موفقیت به `{new_value:.2f}%` تغییر کرد\\.",
                reply_markup=ReplyKeyboardRemove(),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            log_extra.update({'status': 'success', 'details': settings_data})
            logger.info("Copy setting updated via Telegram bot.", extra=log_extra)
            # بازگشت به منوی تنظیمات
            keyboard = [[InlineKeyboardButton("🔙 بازگشت به تنظیمات حساب", callback_data=f"copy:settings:menu:{copy_id}")]]
            await update.message.reply_text("برای ادامه مدیریت تنظیمات، روی دکمه زیر کلیک کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
             # این حالت معمولاً نباید رخ دهد مگر اینکه حساب همزمان حذف شود
             await update.message.reply_text("⚠️ تنظیمات حساب یافت نشد.", reply_markup=ReplyKeyboardRemove())

        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Failed to update copy settings (ID: {copy_id})", exc_info=True, extra=log_extra)
        await update.message.reply_text(
            f"❌ خطایی در هنگام ویرایش تنظیمات رخ داد: {escape_markdown(str(e), 2)}\nلطفا دوباره تلاش کنید یا با /cancel لغو کنید.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return ConversationHandler.END
    finally:
         # پاک کردن state موقت
         context.user_data.pop('editing_setting', None)

# --- (پایان بخش مدیریت حساب‌های کپی) ---




# --- مدیریت اتصالات (Mappings) ---

@admin_only
async def conn_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش منوی اصلی مدیریت اتصالات (انتخاب حساب کپی)."""
    query = update.callback_query
    await query.answer()
    context.user_data.clear() # پاک کردن state قبلی

    copies = []
    try:
        with database.get_db_session() as db:
            copies = db.query(database.CopyAccount).filter(database.CopyAccount.is_active == True)\
                       .order_by(database.CopyAccount.id).all()
    except Exception as e:
        logger.error("Failed to query active copy accounts for connections menu", exc_info=True)
        await query.edit_message_text("❌ خطایی در خواندن لیست حساب‌های کپی رخ داد.")
        return ConversationHandler.END

    if not copies:
         await query.edit_message_text(
             "هیچ حساب کپی فعالی برای مدیریت اتصالات یافت نشد.",
             reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="main_menu")]])
         )
         return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(
            escape_markdown(c.name, version=2),
            callback_data=f"conn:select_copy:{c.id}"
        )] for c in copies
    ]
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="main_menu")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "مدیریت اتصالات:\nیک حساب کپی را برای مشاهده یا ویرایش اتصالات آن انتخاب کنید:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN_V2
    )
    return ConversationHandler.END

@admin_only
async def conn_display_copy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش اتصالات موجود و منابع قابل اتصال برای حساب کپی انتخاب شده."""
    query = update.callback_query
    await query.answer()
    try:
        copy_id = int(query.data.split(':')[-1])
        context.user_data['selected_copy_id'] = copy_id
    except (IndexError, ValueError):
        logger.error(f"Invalid callback data for conn_select_copy: {query.data}")
        await query.edit_message_text("❌ خطای داخلی: ID حساب کپی نامعتبر است.")
        return ConversationHandler.END

    copy_account = None
    mappings = []
    available_sources = []
    try:
        # فراخوانی توابع دیتابیس (که بعداً پیاده‌سازی می‌شوند)
        copy_account = database.get_copy_account_by_id(copy_id) # تابع فرضی
        mappings = database.get_mappings_for_copy(copy_id) # تابع فرضی
        available_sources = database.get_available_sources_for_copy(copy_id) # تابع فرضی
    except Exception as e:
        logger.error(f"Failed to query connection details for copy ID {copy_id}", exc_info=True)
        await query.edit_message_text("❌ خطایی در خواندن اطلاعات اتصالات رخ داد.")
        return ConversationHandler.END

    if not copy_account:
        await query.edit_message_text("❌ حساب کپی مورد نظر یافت نشد.")
        # بازگشت به منوی اصلی اتصالات
        keyboard = [[InlineKeyboardButton("🔙 بازگشت به لیست حساب‌ها", callback_data="conn:main")]]
        await query.message.reply_text("برای ادامه، به لیست حساب‌ها بازگردید:", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END

    keyboard = []
    copy_name_escaped = escape_markdown(copy_account.name, 2)

    # --- نمایش اتصالات موجود ---
    if not mappings:
        keyboard.append([InlineKeyboardButton("این حساب به هیچ منبعی متصل نیست", callback_data="noop")])
    else:
        keyboard.append([InlineKeyboardButton("🔽 *اتصالات فعال* 🔽", callback_data="noop")])
        for mapping in mappings:
            source_name = escape_markdown(mapping.get('source_name', '؟؟'), 2)
            source_id_str = escape_markdown(mapping.get('source_id_str', '؟؟'), 2)
            mapping_id = mapping.get('id') # ID خود رکورد mapping

            header_text = f"─── {source_name} (`{source_id_str}`) ───"
            keyboard.append([InlineKeyboardButton(header_text, callback_data="noop")])

            # دکمه‌های ویرایش (با callback_data شامل mapping_id)
            vol_type = mapping.get('volume_type', 'MULTIPLIER')
            vol_value = mapping.get('volume_value', 1.0)
            vol_text = f"حجم: {vol_type} {vol_value:.2f}"

            mode = mapping.get('copy_mode', 'ALL')
            mode_text = f"حالت: {mode}"
            if mode == 'SYMBOLS':
                symbols = mapping.get('allowed_symbols', '')
                short_symbols = symbols[:10] + '...' if symbols and len(symbols) > 10 else (symbols or 'خالی')
                mode_text += f" ({escape_markdown(short_symbols, 2)})"

            max_lot = mapping.get('max_lot_size', 0.0)
            max_trades = mapping.get('max_concurrent_trades', 0)
            dd_limit = mapping.get('source_drawdown_limit', 0.0)
            max_lot_text = f"لات: {'♾️' if max_lot <= 0 else f'{max_lot:.2f}'}"
            max_trades_text = f"تعداد: {'♾️' if max_trades <= 0 else str(max_trades)}"
            dd_limit_text = f"ضرر$: {'♾️' if dd_limit <= 0 else f'{dd_limit:.2f}'}"

            keyboard.append([
                InlineKeyboardButton(f"⚙️ {vol_text}", callback_data=f"conn:edit:volume_type:{mapping_id}"),
                InlineKeyboardButton(f"🚦 {mode_text}", callback_data=f"conn:edit:mode_menu:{mapping_id}")
            ])
            keyboard.append([
                InlineKeyboardButton(f"🚫 {max_lot_text}", callback_data=f"conn:edit:limit:max_lot:{mapping_id}"),
                InlineKeyboardButton(f"🔢 {max_trades_text}", callback_data=f"conn:edit:limit:max_trades:{mapping_id}"),
                InlineKeyboardButton(f"💣 {dd_limit_text}", callback_data=f"conn:edit:limit:dd_limit:{mapping_id}")
            ])
            keyboard.append([
                InlineKeyboardButton("✂️ قطع اتصال", callback_data=f"conn:disconnect:{mapping_id}")
            ])

    # --- نمایش منابع قابل اتصال ---
    if available_sources:
        keyboard.append([InlineKeyboardButton("─" * 20, callback_data="noop")])
        keyboard.append([InlineKeyboardButton("➕ *اتصال به منبع جدید* ➕", callback_data="noop")])
        for source in available_sources:
            connect_text = f"🔗 {escape_markdown(source.name, 2)} (`{escape_markdown(source.source_id_str, 2)}`)"
            # Callback data شامل copy_id و source.id (نه source_id_str)
            keyboard.append([InlineKeyboardButton(connect_text, callback_data=f"conn:connect:{copy_id}:{source.id}")])

    keyboard.append([InlineKeyboardButton("🔙 بازگشت به لیست حساب‌ها", callback_data="conn:main")])

    try:
        await query.edit_message_text(
            f"مدیریت اتصالات حساب *{copy_name_escaped}*:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
             logger.warning(f"Failed to edit connections display for copy ID {copy_id}", extra={'error': str(e)})
        # else ignore if message not modified

    return ConversationHandler.END # پایان هر گفتگوی احتمالی



# --- اقدامات مستقیم اتصال/قطع اتصال ---

@admin_only
async def conn_connect_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ایجاد یک اتصال جدید با تنظیمات پیش‌فرض."""
    query = update.callback_query
    await query.answer("در حال ایجاد اتصال...")
    try:
        parts = query.data.split(':')
        copy_id = int(parts[2])
        source_id = int(parts[3]) # ID عددی منبع
        context.user_data['selected_copy_id'] = copy_id # ذخیره برای بازگشت به منو
    except (IndexError, ValueError):
        logger.error(f"Invalid callback data for conn_connect: {query.data}")
        await query.edit_message_text("❌ خطای داخلی: داده نامعتبر است\\.")
        return ConversationHandler.END # اگر در گفتگویی بودیم

    log_extra = {'user_id': update.effective_user.id, 'copy_id': copy_id, 'source_id': source_id}

    try:
        # فراخوانی تابع دیتابیس (که بعداً پیاده‌سازی/بررسی می‌شود)
        # فرض می‌کنیم create_mapping به ID عددی نیاز دارد
        success = database.create_mapping_by_ids(
             copy_id=copy_id,
             source_id=source_id,
             # تنظیمات پیش‌فرض در خود تابع دیتابیس اعمال می‌شوند
        ) # تابع فرضی جدید

        if success:
            logger.info("New mapping created successfully.", extra=log_extra)
            # نمایش مجدد منوی اتصالات برای همین حساب کپی
            await conn_display_copy(update, context) # await لازم است
        else:
             # این حالت معمولا نباید رخ دهد مگر اینکه اتصال از قبل وجود داشته باشد
             logger.warning("Mapping creation failed, possibly duplicate.", extra=log_extra)
             await query.answer("⚠️ اتصال از قبل وجود دارد یا خطایی رخ داده.", show_alert=True)
             # نمایش مجدد منو برای اطمینان
             await conn_display_copy(update, context)

    except Exception as e:
        logger.error(f"Failed to create mapping (CopyID: {copy_id}, SourceID: {source_id})", exc_info=True, extra=log_extra)
        await query.edit_message_text(f"❌ خطایی در هنگام ایجاد اتصال رخ داد: {escape_markdown(str(e), 2)}\\.", parse_mode=ParseMode.MARKDOWN_V2)
        # دکمه بازگشت
        keyboard = [[InlineKeyboardButton("🔙 بازگشت به لیست حساب‌ها", callback_data="conn:main")]]
        await query.message.reply_text("لطفا دوباره تلاش کنید:", reply_markup=InlineKeyboardMarkup(keyboard))

    return ConversationHandler.END # اطمینان از خروج از هر گفتگو

@admin_only
async def conn_disconnect_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حذف یک اتصال موجود."""
    query = update.callback_query
    await query.answer("در حال قطع اتصال...")
    try:
        # ID رکورد mapping از callback_data استخراج می‌شود
        mapping_id = int(query.data.split(':')[-1])
    except (IndexError, ValueError):
        logger.error(f"Invalid callback data for conn_disconnect: {query.data}")
        await query.edit_message_text("❌ خطای داخلی: داده نامعتبر است\\.")
        return ConversationHandler.END

    log_extra = {'user_id': update.effective_user.id, 'mapping_id': mapping_id}
    copy_id = context.user_data.get('selected_copy_id') # ID کپی را برای بازگشت به منو نگه می‌داریم

    try:
        # فراخوانی تابع دیتابیس (که بعداً پیاده‌سازی می‌شود)
        deleted = database.delete_mapping(mapping_id) # تابع فرضی

        if deleted:
            logger.info("Mapping deleted successfully.", extra=log_extra)
            if copy_id: # اگر ID کپی را داریم، به منوی آن برگرد
                 # callback_data را برای conn_display_copy بازسازی می‌کنیم
                 query.data = f"conn:select_copy:{copy_id}"
                 await conn_display_copy(update, context)
            else: # اگر ID کپی در context نبود، به منوی اصلی برگرد
                 await conn_main_menu(update, context)
        else:
             logger.warning("Attempted to delete non-existent mapping.", extra=log_extra)
             await query.answer("⚠️ اتصال یافت نشد (ممکن است قبلاً حذف شده باشد).", show_alert=True)
             # نمایش مجدد منو (اگر copy_id داریم)
             if copy_id:
                  query.data = f"conn:select_copy:{copy_id}"
                  await conn_display_copy(update, context)
             else:
                  await conn_main_menu(update, context)

    except Exception as e:
        logger.error(f"Failed to delete mapping (MappingID: {mapping_id})", exc_info=True, extra=log_extra)
        await query.edit_message_text(f"❌ خطایی در هنگام قطع اتصال رخ داد: {escape_markdown(str(e), 2)}\\.", parse_mode=ParseMode.MARKDOWN_V2)
        # دکمه بازگشت (اگر copy_id داریم به منوی کپی، وگرنه به منوی اصلی)
        back_button_data = f"conn:select_copy:{copy_id}" if copy_id else "conn:main"
        back_button_text = "🔙 بازگشت به منوی اتصالات حساب" if copy_id else "🔙 بازگشت به لیست حساب‌ها"
        keyboard = [[InlineKeyboardButton(back_button_text, callback_data=back_button_data)]]
        await query.message.reply_text("لطفا دوباره تلاش کنید:", reply_markup=InlineKeyboardMarkup(keyboard))

    return ConversationHandler.END



# --- Conversation: ویرایش حجم اتصال ---

@admin_only
async def conn_set_volume_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش دکمه‌های انتخاب نوع حجم (Multiplier/Fixed)."""
    query = update.callback_query
    await query.answer()
    try:
        # ID رکورد mapping از callback_data استخراج می‌شود
        mapping_id = int(query.data.split(':')[-1])
        # ذخیره mapping_id برای استفاده در مراحل بعدی گفتگو
        context.user_data['selected_mapping_id'] = mapping_id
    except (IndexError, ValueError):
        logger.error(f"Invalid callback data for conn_set_volume_type: {query.data}")
        await query.edit_message_text("❌ خطای داخلی: داده نامعتبر است\\.")
        return ConversationHandler.END

    # TODO: خواندن مقدار فعلی از دیتابیس برای نمایش بهتر (اختیاری)
    # mapping_info = database.get_mapping_by_id(mapping_id)
    # current_type = mapping_info.get('volume_type', 'MULTIPLIER')

    keyboard = [
        [InlineKeyboardButton("ضریب (Multiplier)", callback_data=f"conn:set_volume_value:mult:{mapping_id}")],
        [InlineKeyboardButton("حجم ثابت (Fixed)", callback_data=f"conn:set_volume_value:fixed:{mapping_id}")],
        [InlineKeyboardButton("🔙 لغو", callback_data=f"conn:cancel_edit:{mapping_id}")] # دکمه لغو جدید
    ]
    await query.edit_message_text(
        "لطفاً نوع محاسبه حجم برای این اتصال را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2
    )
    # چون این تابع ورودی ConversationHandler نیست، state برنمی‌گردانیم

@admin_only
async def conn_set_volume_value_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شروع گفتگو برای دریافت مقدار حجم."""
    query = update.callback_query
    await query.answer()
    try:
        parts = query.data.split(':')
        vol_type = parts[3] # 'mult' or 'fixed'
        mapping_id = int(parts[4])
        # ذخیره نوع و ID در user_data
        context.user_data['selected_mapping_id'] = mapping_id
        context.user_data['editing_volume_type'] = vol_type
    except (IndexError, ValueError):
        logger.error(f"Invalid callback data for conn_set_volume_value_start: {query.data}")
        await query.edit_message_text("❌ خطای داخلی: داده نامعتبر است\\.")
        return ConversationHandler.END

    # TODO: خواندن مقدار فعلی از دیتابیس (اختیاری)
    # mapping_info = database.get_mapping_by_id(mapping_id)
    # current_value = mapping_info.get('volume_value', 1.0)

    prompt = "لطفاً مقدار **ضریب** را وارد کنید \\(مثال: `1.5`\\):" if vol_type == "mult" else "لطفاً مقدار **حجم ثابت** را وارد کنید \\(مثال: `0.1`\\):"
    await query.edit_message_text(
        prompt,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data=f"conn:cancel_edit:{mapping_id}")]]) ,
        parse_mode=ParseMode.MARKDOWN_V2
    )
    logger.info(f"Starting edit volume value conversation (MappingID: {mapping_id}, Type: {vol_type})", extra={'user_id': update.effective_user.id, 'entity_id': mapping_id})
    return CONN_VOLUME_VALUE # ورود به State انتظار مقدار حجم

async def conn_set_volume_value_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دریافت مقدار حجم، اعتبارسنجی، ذخیره و پایان گفتگو."""
    user = update.effective_user
    new_value_str = update.message.text.strip()
    mapping_id = context.user_data.get('selected_mapping_id')
    vol_type_short = context.user_data.get('editing_volume_type') # 'mult' or 'fixed'
    log_extra = {'user_id': user.id, 'input_for': 'CONN_VOLUME_VALUE', 'text_received': new_value_str, 'entity_id': mapping_id}

    try:
        new_value = float(new_value_str)
        if new_value <= 0:
            raise ValueError("Volume value must be positive.")
    except ValueError:
        await update.message.reply_text("❌ ورودی نامعتبر است\\. لطفاً یک عدد مثبت بزرگتر از صفر \\(مانند `1.5` یا `0.1`\\) وارد کنید یا با /cancel لغو کنید\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return CONN_VOLUME_VALUE # در همین State بمان

    if not mapping_id or not vol_type_short:
         logger.error("Missing mapping_id or volume_type in user_data during volume edit receive.", extra=log_extra)
         await update.message.reply_text("❌ خطای داخلی رخ داد\\. با /cancel لغو کنید\\.")
         context.user_data.clear()
         return ConversationHandler.END

    vol_type_full = "MULTIPLIER" if vol_type_short == "mult" else "FIXED"
    settings_data = {
        'volume_type': vol_type_full,
        'volume_value': new_value
    }

    try:
        # فراخوانی تابع دیتابیس (که بعداً پیاده‌سازی می‌شود)
        success = database.update_mapping_settings(mapping_id=mapping_id, settings_data=settings_data) # تابع فرضی

        if success:
            await update.message.reply_text(
                f"✅ تنظیمات حجم با موفقیت به *{vol_type_full} {new_value:.2f}* تغییر کرد\\.",
                reply_markup=ReplyKeyboardRemove(),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            log_extra.update({'status': 'success', 'details': settings_data})
            logger.info("Mapping volume updated via Telegram bot.", extra=log_extra)

            # بازگشت به منوی نمایش اتصالات حساب کپی مربوطه
            copy_id = context.user_data.get('selected_copy_id') # ID کپی باید از قبل در context باشد
            if copy_id:
                 # ساخت callback_data جعلی برای conn_display_copy
                 fake_query_data = f"conn:select_copy:{copy_id}"
                 update.callback_query = update.message.chat # نیاز به یک آبجکت موقت
                 update.callback_query.data = fake_query_data
                 await conn_display_copy(update, context) # نمایش مجدد منو
            else:
                 # اگر copy_id نبود، دکمه بازگشت به منوی اصلی اتصالات
                 keyboard = [[InlineKeyboardButton("🔙 بازگشت به لیست حساب‌ها", callback_data="conn:main")]]
                 await update.message.reply_text("برای ادامه مدیریت اتصالات کلیک کنید:", reply_markup=InlineKeyboardMarkup(keyboard))

        else:
             await update.message.reply_text("⚠️ اتصال مورد نظر یافت نشد.", reply_markup=ReplyKeyboardRemove())

        context.user_data.clear() # پاک کردن تمام user_data پس از اتمام
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Failed to update mapping volume (MappingID: {mapping_id})", exc_info=True, extra=log_extra)
        await update.message.reply_text(
            f"❌ خطایی در هنگام ویرایش حجم رخ داد: {escape_markdown(str(e), 2)}\nلطفا دوباره تلاش کنید یا با /cancel لغو کنید\\.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        context.user_data.clear()
        return ConversationHandler.END

async def conn_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """لغو فرآیند ویرایش اتصال و بازگشت به منوی نمایش اتصالات."""
    query = update.callback_query
    user = update.effective_user
    mapping_id = context.user_data.get('selected_mapping_id')
    copy_id = context.user_data.get('selected_copy_id')
    log_extra = {'user_id': user.id, 'mapping_id': mapping_id, 'copy_id': copy_id}
    logger.info(f"User cancelled connection edit operation.", extra=log_extra)

    await query.answer("لغو شد")
    context.user_data.clear()

    # بازگشت به منوی نمایش اتصالات حساب کپی مربوطه
    if copy_id:
        # ساخت callback_data جعلی برای conn_display_copy
        query.data = f"conn:select_copy:{copy_id}"
        try:
            await conn_display_copy(update, context)
        except Exception as e:
             logger.error("Error returning to conn_display_copy after cancel", exc_info=True)
             await query.edit_message_text("❌ خطایی در بازگشت به منو رخ داد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")]]))
    else:
         # اگر copy_id نبود، به منوی اصلی اتصالات برگرد
         await conn_main_menu(update, context)

    return ConversationHandler.END




# --- Conversation: ویرایش حالت کپی اتصال ---

@admin_only
async def conn_set_mode_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش دکمه‌های انتخاب حالت کپی (ALL/GOLD_ONLY/SYMBOLS)."""
    query = update.callback_query
    await query.answer()
    try:
        mapping_id = int(query.data.split(':')[-1])
        context.user_data['selected_mapping_id'] = mapping_id
    except (IndexError, ValueError):
        logger.error(f"Invalid callback data for conn_set_mode_menu: {query.data}")
        await query.edit_message_text("❌ خطای داخلی: داده نامعتبر است\\.")
        return ConversationHandler.END # اگر در گفتگویی بودیم

    # TODO: خواندن حالت فعلی از دیتابیس (اختیاری)
    # mapping_info = database.get_mapping_by_id(mapping_id)
    # current_mode = mapping_info.get('copy_mode', 'ALL')

    keyboard = [
        [InlineKeyboardButton("1️⃣ همه نمادها (ALL)", callback_data=f"conn:set_mode_action:ALL:{mapping_id}")],
        [InlineKeyboardButton("2️⃣ فقط طلا (GOLD_ONLY)", callback_data=f"conn:set_mode_action:GOLD_ONLY:{mapping_id}")],
        [InlineKeyboardButton("3️⃣ نمادهای خاص (SYMBOLS)", callback_data=f"conn:set_mode_action:SYMBOLS:{mapping_id}")],
        [InlineKeyboardButton("🔙 لغو", callback_data=f"conn:cancel_edit:{mapping_id}")]
    ]
    await query.edit_message_text(
        "لطفاً حالت کپی نمادها برای این اتصال را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2
    )
    # چون این تابع ورودی ConversationHandler نیست، state برنمی‌گردانیم

@admin_only
async def conn_set_mode_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پردازش انتخاب حالت کپی. اگر SYMBOLS بود، درخواست لیست نمادها."""
    query = update.callback_query
    await query.answer()
    try:
        parts = query.data.split(':')
        mode = parts[3] # 'ALL', 'GOLD_ONLY', 'SYMBOLS'
        mapping_id = int(parts[4])
        context.user_data['selected_mapping_id'] = mapping_id # اطمینان از ذخیره
    except (IndexError, ValueError):
        logger.error(f"Invalid callback data for conn_set_mode_action: {query.data}")
        await query.edit_message_text("❌ خطای داخلی: داده نامعتبر است\\.")
        return ConversationHandler.END

    log_extra = {'user_id': update.effective_user.id, 'entity_id': mapping_id, 'details': {'mode': mode}}

    # اگر حالت SYMBOLS انتخاب شد، وارد state انتظار نمادها شو
    if mode == 'SYMBOLS':
        await query.edit_message_text(
            "لطفاً لیست نمادهای مجاز را وارد کنید\\. نمادها را با سمی‌کالن \\(`;`\\) از هم جدا کنید\\.\nمثال: `EURUSD;GBPUSD;XAUUSD`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data=f"conn:cancel_edit:{mapping_id}")]]) ,
            parse_mode=ParseMode.MARKDOWN_V2
        )
        logger.info(f"Starting edit copy mode conversation (MappingID: {mapping_id}), waiting for symbols.", extra=log_extra)
        return CONN_SYMBOLS # ورود به State انتظار لیست نمادها
    else:
        # اگر ALL یا GOLD_ONLY بود، مستقیماً دیتابیس را آپدیت کن
        settings_data = {'copy_mode': mode, 'allowed_symbols': None} # allowed_symbols را پاک کن
        try:
            success = database.update_mapping_settings(mapping_id=mapping_id, settings_data=settings_data) # تابع فرضی
            if success:
                await query.answer(f"✅ حالت کپی به {mode} تغییر کرد.")
                log_extra['status'] = 'success'
                logger.info("Mapping copy mode updated directly.", extra=log_extra)
                # بازگشت به منوی نمایش اتصالات
                copy_id = context.user_data.get('selected_copy_id')
                if copy_id:
                     query.data = f"conn:select_copy:{copy_id}"
                     await conn_display_copy(update, context)
                else:
                     await conn_main_menu(update, context) # بازگشت به منوی اصلی اتصالات
            else:
                await query.answer("⚠️ اتصال یافت نشد.", show_alert=True)
        except Exception as e:
            logger.error(f"Failed to update mapping mode directly (MappingID: {mapping_id})", exc_info=True, extra=log_extra)
            await query.edit_message_text(f"❌ خطایی در هنگام ویرایش حالت کپی رخ داد: {escape_markdown(str(e), 2)}\\.", parse_mode=ParseMode.MARKDOWN_V2)

        context.user_data.clear() # state موقت را پاک کن
        return ConversationHandler.END # پایان گفتگو

async def conn_set_symbols_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دریافت لیست نمادها، اعتبارسنجی، ذخیره و پایان گفتگو."""
    user = update.effective_user
    symbols_text = update.message.text.strip()
    mapping_id = context.user_data.get('selected_mapping_id')
    log_extra = {'user_id': user.id, 'input_for': 'CONN_SYMBOLS', 'text_received': symbols_text, 'entity_id': mapping_id}

    if not symbols_text:
        await update.message.reply_text("❌ لیست نمادها نمی‌تواند خالی باشد\\. لطفاً حداقل یک نماد وارد کنید یا با /cancel لغو کنید\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return CONN_SYMBOLS # در همین State بمان

    # پاک‌سازی و فرمت‌بندی ورودی (مانند V1)
    symbols = [s.strip().upper() for s in symbols_text.split(';') if s.strip()]
    if not symbols:
        await update.message.reply_text("❌ فرمت ورودی نامعتبر است\\. لطفاً نمادها را با سمی‌کالن \\(`;`\\) جدا کنید یا با /cancel لغو کنید\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return CONN_SYMBOLS

    formatted_symbols = ";".join(symbols)

    if not mapping_id:
         logger.error("Missing mapping_id in user_data during symbols receive.", extra=log_extra)
         await update.message.reply_text("❌ خطای داخلی رخ داد\\. با /cancel لغو کنید\\.")
         context.user_data.clear()
         return ConversationHandler.END

    settings_data = {
        'copy_mode': 'SYMBOLS',
        'allowed_symbols': formatted_symbols
    }

    try:
        success = database.update_mapping_settings(mapping_id=mapping_id, settings_data=settings_data) # تابع فرضی
        if success:
            await update.message.reply_text(
                f"✅ حالت کپی به 'SYMBOLS' تغییر کرد و لیست نمادها ذخیره شد:\n`{escape_markdown(formatted_symbols, 2)}`",
                reply_markup=ReplyKeyboardRemove(),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            log_extra.update({'status': 'success', 'details': settings_data})
            logger.info("Mapping copy mode and symbols updated via Telegram bot.", extra=log_extra)

            # بازگشت به منوی نمایش اتصالات
            copy_id = context.user_data.get('selected_copy_id')
            if copy_id:
                 fake_query_data = f"conn:select_copy:{copy_id}"
                 update.callback_query = update.message.chat # نیاز به یک آبجکت موقت
                 update.callback_query.data = fake_query_data
                 await conn_display_copy(update, context)
            else:
                 keyboard = [[InlineKeyboardButton("🔙 بازگشت به لیست حساب‌ها", callback_data="conn:main")]]
                 await update.message.reply_text("برای ادامه مدیریت اتصالات کلیک کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
             await update.message.reply_text("⚠️ اتصال مورد نظر یافت نشد.", reply_markup=ReplyKeyboardRemove())

        context.user_data.clear()
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Failed to update mapping symbols (MappingID: {mapping_id})", exc_info=True, extra=log_extra)
        await update.message.reply_text(
            f"❌ خطایی در هنگام ویرایش نمادها رخ داد: {escape_markdown(str(e), 2)}\nلطفا دوباره تلاش کنید یا با /cancel لغو کنید\\.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        context.user_data.clear()
        return ConversationHandler.END





# CoreService/core/telegram_bot.py

# ... (تابع conn_set_symbols_receive) ...

# --- Conversation: ویرایش محدودیت‌های امنیتی اتصال ---

@admin_only
async def conn_set_limit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شروع گفتگو برای دریافت مقدار محدودیت امنیتی."""
    query = update.callback_query
    await query.answer()
    try:
        parts = query.data.split(':')
        limit_type = parts[4] # 'max_lot', 'max_trades', 'dd_limit'
        mapping_id = int(parts[5])
        # ذخیره نوع و ID در user_data
        context.user_data['selected_mapping_id'] = mapping_id
        context.user_data['editing_limit_type'] = limit_type
    except (IndexError, ValueError):
        logger.error(f"Invalid callback data for conn_set_limit_start: {query.data}")
        await query.edit_message_text("❌ خطای داخلی: داده نامعتبر است\\.")
        return ConversationHandler.END

    # TODO: خواندن مقدار فعلی از دیتابیس (اختیاری)
    # mapping_info = database.get_mapping_by_id(mapping_id)
    # current_value = mapping_info.get(database_key_map[limit_type], 0.0)

    prompt_text = ""
    example = ""
    if limit_type == "max_lot":
        prompt_text = "حداکثر حجم مجاز \\(Max Lot Size\\) برای هر معامله از این سورس را وارد کنید \\(عدد بزرگتر مساوی صفر\\)\\. عدد `0` به معنی نامحدود است\\."
        example = "مثال: `1.5` یا `0`"
    elif limit_type == "max_trades":
        prompt_text = "حداکثر تعداد معاملات باز همزمان \\(Max Concurrent Trades\\) از این سورس را وارد کنید \\(عدد صحیح بزرگتر مساوی صفر\\)\\. عدد `0` به معنی نامحدود است\\."
        example = "مثال: `3` یا `0`"
    elif limit_type == "dd_limit":
        prompt_text = f"حد ضرر شناور \\(Source Drawdown Limit\\) برای *مجموع معاملات باز* این سورس را به واحد پولی حساب وارد کنید \\(عدد بزرگتر مساوی صفر\\)\\. عدد `0` به معنی نامحدود است\\."
        example = "مثال: `200.0` یا `0`"
    else:
         await query.edit_message_text("❌ خطای داخلی: نوع محدودیت نامعتبر است\\.")
         return ConversationHandler.END


    await query.edit_message_text(
        f"{prompt_text}\n{example}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data=f"conn:cancel_edit:{mapping_id}")]]) ,
        parse_mode=ParseMode.MARKDOWN_V2
    )
    logger.info(f"Starting edit limit value conversation (MappingID: {mapping_id}, Type: {limit_type})", extra={'user_id': update.effective_user.id, 'entity_id': mapping_id})
    return CONN_LIMIT_VALUE # ورود به State انتظار مقدار محدودیت

async def conn_set_limit_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دریافت مقدار محدودیت، اعتبارسنجی، ذخیره و پایان گفتگو."""
    user = update.effective_user
    new_value_str = update.message.text.strip()
    mapping_id = context.user_data.get('selected_mapping_id')
    limit_type = context.user_data.get('editing_limit_type') # 'max_lot', 'max_trades', 'dd_limit'
    log_extra = {'user_id': user.id, 'input_for': 'CONN_LIMIT_VALUE', 'text_received': new_value_str, 'entity_id': mapping_id, 'details': {'limit_type': limit_type}}

    new_value = None
    error_message = None
    limit_key_db = None # نام ستون در دیتابیس
    limit_name_fa = "" # نام فارسی برای پیام

    try:
        if limit_type == "max_lot":
            limit_key_db = "max_lot_size"
            limit_name_fa = "حداکثر حجم"
            new_value = float(new_value_str)
            if new_value < 0: raise ValueError("Value must be non-negative.")
        elif limit_type == "max_trades":
            limit_key_db = "max_concurrent_trades"
            limit_name_fa = "حداکثر معاملات همزمان"
            new_value = int(new_value_str)
            if new_value < 0: raise ValueError("Value must be non-negative.")
        elif limit_type == "dd_limit":
            limit_key_db = "source_drawdown_limit"
            limit_name_fa = "حد ضرر دلاری سورس"
            new_value = float(new_value_str)
            if new_value < 0: raise ValueError("Value must be non-negative.")
        else:
            error_message = "❌ نوع محدودیت نامعتبر است\\."

    except ValueError:
        if limit_type == "max_trades":
             error_message = "❌ ورودی نامعتبر است\\. لطفاً یک عدد صحیح \\(مانند `3`\\) یا `0` وارد کنید یا با /cancel لغو کنید\\."
        else:
             error_message = "❌ ورودی نامعتبر است\\. لطفاً یک عدد \\(مانند `1.5` یا `200`\\) یا `0` وارد کنید یا با /cancel لغو کنید\\."

    if error_message:
        await update.message.reply_text(error_message, parse_mode=ParseMode.MARKDOWN_V2)
        return CONN_LIMIT_VALUE # در همین State بمان

    if not mapping_id or not limit_key_db:
         logger.error("Missing mapping_id or limit_key_db in user_data/logic during limit edit receive.", extra=log_extra)
         await update.message.reply_text("❌ خطای داخلی رخ داد\\. با /cancel لغو کنید\\.")
         context.user_data.clear()
         return ConversationHandler.END

    settings_data = {limit_key_db: new_value}

    try:
        success = database.update_mapping_settings(mapping_id=mapping_id, settings_data=settings_data) # تابع فرضی
        if success:
            status_text = "غیرفعال شد (نامحدود)" if new_value <= 0 else f"روی `{escape_markdown(str(new_value), 2)}` تنظیم شد"
            await update.message.reply_text(
                f"✅ *{limit_name_fa}* با موفقیت {status_text}\\.",
                reply_markup=ReplyKeyboardRemove(),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            log_extra.update({'status': 'success', 'details': {**log_extra['details'], **settings_data}})
            logger.info("Mapping limit updated via Telegram bot.", extra=log_extra)

            # بازگشت به منوی نمایش اتصالات
            copy_id = context.user_data.get('selected_copy_id')
            if copy_id:
                 fake_query_data = f"conn:select_copy:{copy_id}"
                 update.callback_query = update.message.chat
                 update.callback_query.data = fake_query_data
                 await conn_display_copy(update, context)
            else:
                 keyboard = [[InlineKeyboardButton("🔙 بازگشت به لیست حساب‌ها", callback_data="conn:main")]]
                 await update.message.reply_text("برای ادامه مدیریت اتصالات کلیک کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
             await update.message.reply_text("⚠️ اتصال مورد نظر یافت نشد.", reply_markup=ReplyKeyboardRemove())

        context.user_data.clear()
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Failed to update mapping limit (MappingID: {mapping_id})", exc_info=True, extra=log_extra)
        await update.message.reply_text(
            f"❌ خطایی در هنگام ویرایش محدودیت رخ داد: {escape_markdown(str(e), 2)}\nلطفا دوباره تلاش کنید یا با /cancel لغو کنید\\.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        context.user_data.clear()
        return ConversationHandler.END



# --- آمار معاملات ---

@admin_only
async def stats_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش منوی انتخاب بازه زمانی برای آمار."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("📊 آمار کل زمان", callback_data="stats:show:all")],
        [InlineKeyboardButton("📊 آمار امروز", callback_data="stats:show:today")],
        [InlineKeyboardButton("📊 آمار ۷ روز اخیر", callback_data="stats:show:7d")],
        # [InlineKeyboardButton("📊 آمار ۳۰ روز اخیر", callback_data="stats:show:30d")], # (فعلا غیرفعال)
        [InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="main_menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
         await query.edit_message_text(
             "لطفاً بازه زمانی مورد نظر برای نمایش آمار را انتخاب کنید:",
             reply_markup=reply_markup
         )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
             logger.warning(f"Failed to edit message for stats menu: {e}")

@admin_only
async def stats_show_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """محاسبه و نمایش گزارش آمار معاملات بر اساس فیلتر زمانی."""
    query = update.callback_query
    await query.answer("در حال محاسبه آمار...")
    user_id = update.effective_user.id
    time_filter = "all" # پیش‌فرض
    try:
        time_filter = query.data.split(':')[-1]
    except IndexError:
        pass # Use default 'all'

    log_extra = {'user_id': user_id, 'callback_data': query.data, 'details': {'time_filter': time_filter}}

    # ویرایش پیام برای نمایش انتظار
    try:
        await query.edit_message_text("⏳ در حال محاسبه آمار برای بازه انتخابی\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest: # Ignore if message not modified
        pass

    title = "📊 آمار کل معاملات"
    if time_filter == "today": title = "📊 آمار معاملات امروز"
    elif time_filter == "7d": title = "📊 آمار معاملات ۷ روز اخیر"
    # elif time_filter == "30d": title = "📊 آمار معاملات ۳۰ روز اخیر"

    try:
        # فراخوانی تابع جدید دیتابیس (که بعداً پیاده‌سازی می‌شود)
        summary_results = database.get_statistics_summary(time_filter=time_filter) # تابع فرضی

        if not summary_results:
            await query.edit_message_text(
                f"*{escape_markdown(title, 2)}*\n\nهنوز هیچ داده‌ای برای نمایش در این بازه زمانی وجود ندارد\\.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="stats:main")]]),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return

        # --- فرمت‌بندی پیام خروجی (مشابه V1) ---
        message_lines = [f"*{escape_markdown(title, 2)}*"]
        grand_total_profit = sum(item['total_profit'] for item in summary_results)
        grand_total_trades = sum(item['trade_count'] for item in summary_results)

        message_lines.append(f"> *مجموع سود/زیان:* `{escape_markdown(f'{grand_total_profit:,.2f}', 2)}`")
        message_lines.append(f"> *تعداد معاملات:* `{escape_markdown(str(grand_total_trades), 2)}`")
        message_lines.append("> \\n> ─── *جزئیات بر اساس حساب کپی* ───\\n>")

        # گروه‌بندی نتایج بر اساس copy_id (چون query دیتابیس ممکن است به این شکل برنگرداند)
        stats_by_copy = {}
        for item in summary_results:
             copy_id = item['copy_account_id']
             if copy_id not in stats_by_copy:
                  stats_by_copy[copy_id] = {'copy_name': item['copy_name'], 'total_profit': 0, 'total_trades': 0, 'sources': []}
             stats_by_copy[copy_id]['total_profit'] += item['total_profit']
             stats_by_copy[copy_id]['total_trades'] += item['trade_count']
             stats_by_copy[copy_id]['sources'].append({
                 'source_id': item['source_account_id'],
                 'source_name': item['source_name'],
                 'profit': item['total_profit'],
                 'trades': item['trade_count']
             })


        for copy_id, data in stats_by_copy.items():
            copy_name = escape_markdown(data.get('copy_name', f'ID:{copy_id}'), 2)
            message_lines.append(f"🛡️ *حساب:* {copy_name}")
            message_lines.append(f">  ▫️ *مجموع سود/زیان:* `{escape_markdown(f'{data['total_profit']:,.2f}', 2)}`")
            message_lines.append(f">  ▫️ *تعداد معاملات:* `{escape_markdown(str(data['total_trades']), 2)}`")
            message_lines.append(">  ▫️ *تفکیک منابع:*")
            if not data['sources']:
                 message_lines.append(">       └── *بدون معامله ثبت شده برای این حساب*")
            else:
                for source_stat in data['sources']:
                    source_name = escape_markdown(source_stat.get('source_name', 'ناشناس/حذف شده'), 2)
                    profit_str = escape_markdown(f"{source_stat['profit']:,.2f}", 2)
                    trades_str = escape_markdown(str(source_stat['trades']), 2)
                    message_lines.append(f">       └── *{source_name}:* سود/زیان: `{profit_str}`, تعداد: `{trades_str}`")
            message_lines.append(">") # خط خالی

        final_message = "\n".join(message_lines)
        # --- پایان فرمت‌بندی ---

        keyboard = [
             [InlineKeyboardButton("🔄 به‌روزرسانی", callback_data=f"stats:show:{time_filter}")],
             [InlineKeyboardButton("🔙 بازگشت به انتخاب بازه", callback_data="stats:main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
             await query.edit_message_text(
                 text=final_message,
                 reply_markup=reply_markup,
                 parse_mode=ParseMode.MARKDOWN_V2
             )
             log_extra['status'] = 'success'
             logger.info(f"Statistics displayed successfully for filter: {time_filter}.", extra=log_extra)
        except BadRequest as e:
             if "message is too long" in str(e).lower():
                  logger.warning(f"Statistics message too long for filter {time_filter}, sending truncated.", extra={**log_extra, 'status': 'truncated'})
                  await query.edit_message_text(
                       text=final_message[:4000] + "\n\n✂️\\.\\.\\. \\(پیام کامل نمایش داده نشد\\)",
                       reply_markup=reply_markup,
                       parse_mode=ParseMode.MARKDOWN_V2
                  )
             elif "Message is not modified" not in str(e):
                  raise
             # else: پیام تغییری نکرده، رد شو

    except Exception as e:
        logger.error("Unexpected error in stats_show_report.", exc_info=True, extra=log_extra)
        await query.edit_message_text(
            "❌ یک خطای غیرمنتظره در نمایش آمار رخ داد\\. گزارش برای ادمین ارسال شد\\.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="stats:main")]]),
            parse_mode=ParseMode.MARKDOWN_V2
        )












@admin_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش وضعیت کلی سیستم."""
    query = update.callback_query
    if query:
        await query.answer("در حال دریافت گزارش وضعیت...")
    try:
        report_data = database.get_full_status_report()
        if not report_data:
            await query.edit_message_text("هیچ حساب کپی فعالی در سیستم تعریف نشده است.")
            return
        message = "📊 *گزارش وضعیت سیستم*\n\n"
        for copy_acc in report_data:
            message += f"🛡️ *حساب: {copy_acc['name']}* (`{copy_acc['copy_id_str']}`)\n"
            if copy_acc.get('settings'):
                message += f"  ▫️ ریسک روزانه: `{copy_acc['settings']['daily_drawdown_percent']}`٪\n"
            mappings = copy_acc.get('mappings', [])
            if not mappings:
                message += "  ▫️ *اتصالات: ۰*\n"
            else:
                message += f"  ▫️ *اتصالات: {len(mappings)}*\n"
                for m in mappings:
                    status = "✅" if m['is_enabled'] else "🛑"
                    message += f"    {status} ⟵ *{m['source_name']}* (`{m['source_id_str']}`)\n"
                    message += f"        (حجم: {m['volume_type']} {m['volume_value']})\n"
            message += "\n"
        keyboard = [
            [InlineKeyboardButton("🔄 به‌روزرسانی", callback_data="status:main")],
            [InlineKeyboardButton("🔙 بازگشت به منو", callback_data="main_menu")]
        ]
        await query.edit_message_text(message, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"خطا در دریافت گزارش وضعیت: {e}", exc_info=True)
        await query.edit_message_text(f"❌ خطایی در هنگام دریافت گزارش رخ داد:\n`{e}`", parse_mode=ParseMode.MARKDOWN)

async def alert_sender_task(bot: Application):
    """ارسال هشدارهای دریافتی از صف ZMQ به ادمین."""
    logger.info("تسک ارسال‌کننده هشدار راه‌اندازی شد.")
    if not alert_queue:
        logger.error("صف هشدار (alert_queue) مقداردهی نشده است!")
        return
    while True:
        try:
            alert_message = await alert_queue.get()
            if not alert_message:
                continue
            await bot.bot.send_message(chat_id=ADMIN_ID, text=alert_message, parse_mode=ParseMode.MARKDOWN)
        except TelegramError as e:
            logger.error(f"خطا در ارسال هشدار تلگرام: {e}")
        except Exception as e:
            logger.error(f"خطای پیش‌بینی نشده در تسک هشدار: {e}")
        finally:
            if alert_queue:
                alert_queue.task_done()







async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log Errors caused by Updates and send detailed report to admin."""
    logger.error(f"Exception while handling an update:", exc_info=context.error)

    # Build detailed error message
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)

    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    user_data_str = json.dumps(context.user_data, indent=2, ensure_ascii=False, default=str) if context.user_data else "Empty" # Added default=str for safety

    try: # Try markdown V2 first
        header = "🚨 *خطای مهم در ربات*\n\n"
        update_info = f"*Update:*\n```json\n{escape_markdown(update_str, 2)}\n```\n"
        user_data_info = f"*User Data:*\n```json\n{escape_markdown(user_data_str, 2)}\n```\n"
        traceback_info = f"*Traceback:*\n```\n{escape_markdown(tb_string, 2)}\n```"
        message = header + update_info + user_data_info + traceback_info
        parse_mode = ParseMode.MARKDOWN_V2
    except ValueError: # Fallback to plain text if markdown fails
         header = "🚨 Critical Bot Error 🚨\n\n"
         update_info = f"Update:\n{update_str}\n\n"
         user_data_info = f"User Data:\n{user_data_str}\n\n"
         traceback_info = f"Traceback:\n{tb_string}\n"
         message = header + update_info + user_data_info + traceback_info
         parse_mode = None # Plain text

    MAX_LEN = 4096
    if len(message) <= MAX_LEN:
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=message, parse_mode=parse_mode)
        except Exception as send_err:
             logger.error(f"Failed to send detailed error notification to admin: {send_err}")
    else:
         # If too long, send traceback as a file
         try:
             await context.bot.send_message(chat_id=ADMIN_ID, text=header + update_info + user_data_info + "\n(Traceback sent as file due to length)", parse_mode=parse_mode)
             with open("error_traceback.txt", "w", encoding="utf-8") as f:
                 f.write(tb_string)
             with open("error_traceback.txt", "rb") as f:
                 await context.bot.send_document(chat_id=ADMIN_ID, document=f, caption="Error Traceback")
             os.remove("error_traceback.txt")
         except Exception as send_err:
              logger.error(f"Failed to send error traceback file to admin: {send_err}")





async def run(queue: asyncio.Queue):
    """راه‌اندازی و اجرای ربات تلگرام."""
    global alert_queue
    alert_queue = queue
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN در فایل .env تنظیم نشده است. ربات تلگرام راه‌اندازی نمی‌شود.")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    # --- ConversationHandler ها ---

    # Handler for Source Management (Add, Edit Name)
    source_management_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(sources_add_start, pattern="^sources:add:start$"),
            CallbackQueryHandler(sources_edit_name_start, pattern="^sources:edit_name:start:\d+$")
        ],
        states={
            SOURCE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, sources_add_receive_name)],
            EDIT_SOURCE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, sources_edit_name_receive)],
        },
        fallbacks=[
            CommandHandler("cancel", sources_cancel),
            CallbackQueryHandler(sources_main_menu, pattern="^sources:main$"),
            CallbackQueryHandler(sources_select_menu, pattern="^sources:select:\d+$")
        ],
        map_to_parent={ ConversationHandler.END: -1 }
    )

    # Handler for Adding a Copy Account
    add_copy_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(copy_add_start, pattern="^copy:add:start$")],
        states={
            COPY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, copy_add_receive_name)],
            COPY_ID_STR: [MessageHandler(filters.TEXT & ~filters.COMMAND, copy_add_receive_id_str)],
            COPY_DD: [MessageHandler(filters.TEXT & ~filters.COMMAND, copy_add_receive_dd)],
        },
        fallbacks=[
            CommandHandler("cancel", copy_cancel),
            CallbackQueryHandler(copy_main_menu, pattern="^copy:main$")
        ],
        map_to_parent={ ConversationHandler.END: -1 }
    )

    # Handler for Editing Copy Account Name
    edit_copy_name_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(copy_edit_name_start, pattern="^copy:edit_name:start:\d+$")],
        states={
            EDIT_COPY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, copy_edit_name_receive)],
        },
        fallbacks=[
            CommandHandler("cancel", copy_cancel),
            CallbackQueryHandler(copy_select_menu, pattern="^copy:select:\d+$")
        ],
        map_to_parent={ ConversationHandler.END: -1 }
    )

    # Handler for Editing Copy Account Settings (DD%, Alert%)
    edit_copy_settings_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(copy_settings_edit_start, pattern="^copy:settings:edit_dd:start:\d+$"),
            CallbackQueryHandler(copy_settings_edit_start, pattern="^copy:settings:edit_alert:start:\d+$"),
        ],
        states={
            EDIT_COPY_SETTING_DD: [MessageHandler(filters.TEXT & ~filters.COMMAND, copy_settings_edit_receive)],
            EDIT_COPY_SETTING_ALERT: [MessageHandler(filters.TEXT & ~filters.COMMAND, copy_settings_edit_receive)],
        },
        fallbacks=[
            CommandHandler("cancel", copy_cancel),
            CallbackQueryHandler(copy_settings_menu, pattern="^copy:settings:menu:\d+$")
        ],
        map_to_parent={ ConversationHandler.END: -1 }
    )

    # --- ثبت هندلرها ---

    # Command Handlers
    application.add_handler(CommandHandler("start", start_command))

    # CallbackQuery Handlers (Menus & Actions)
    application.add_handler(CallbackQueryHandler(main_menu, pattern="^main_menu$"))
    application.add_handler(CallbackQueryHandler(status_command, pattern="^status:main$"))

    # Source Handlers
    application.add_handler(CallbackQueryHandler(sources_main_menu, pattern="^sources:main$"))
    application.add_handler(CallbackQueryHandler(sources_select_menu, pattern="^sources:select:\d+$"))
    application.add_handler(CallbackQueryHandler(sources_delete_confirm, pattern="^sources:delete:confirm:\d+$"))
    application.add_handler(CallbackQueryHandler(sources_delete_execute, pattern="^sources:delete:execute:\d+$"))

    # Copy Handlers
    application.add_handler(CallbackQueryHandler(copy_main_menu, pattern="^copy:main$"))
    application.add_handler(CallbackQueryHandler(copy_select_menu, pattern="^copy:select:\d+$"))
    application.add_handler(CallbackQueryHandler(copy_settings_menu, pattern="^copy:settings:menu:\d+$"))
    application.add_handler(CallbackQueryHandler(copy_delete_confirm, pattern="^copy:delete:confirm:\d+$"))
    application.add_handler(CallbackQueryHandler(copy_delete_execute, pattern="^copy:delete:execute:\d+$"))

    # Conversation Handlers
    application.add_handler(source_management_conv_handler)
    application.add_handler(add_copy_conv_handler)
    application.add_handler(edit_copy_name_conv_handler)
    application.add_handler(edit_copy_settings_conv_handler)

    # --- هندلرهای منوی اتصالات ---
    application.add_handler(CallbackQueryHandler(conn_main_menu, pattern="^conn:main$"))
    application.add_handler(CallbackQueryHandler(conn_display_copy, pattern="^conn:select_copy:\d+$"))
    # --- هندلرهای اقدام مستقیم ---
    application.add_handler(CallbackQueryHandler(conn_connect_execute, pattern="^conn:connect:\d+:\d+$"))
    application.add_handler(CallbackQueryHandler(conn_disconnect_execute, pattern="^conn:disconnect:\d+$"))

    # --- هندلرهای آمار ---
    application.add_handler(CallbackQueryHandler(stats_main_menu, pattern="^stats:main$"))
    application.add_handler(CallbackQueryHandler(stats_show_report, pattern="^stats:show:(all|today|7d|30d)$")) # pattern برای فیلترها
    # --- ConversationHandler برای ویرایش حجم اتصال ---
    edit_conn_volume_conv_handler = ConversationHandler(
        entry_points=[
            # این دکمه از conn_display_copy می‌آید و فقط منوی انتخاب نوع را نشان می‌دهد
            CallbackQueryHandler(conn_set_volume_type, pattern="^conn:edit:volume_type:\d+$"),
            # این دکمه از conn_set_volume_type می‌آید و گفتگو را شروع می‌کند
            CallbackQueryHandler(conn_set_volume_value_start, pattern="^conn:set_volume_value:(mult|fixed):\d+$")
        ],
        states={
            CONN_VOLUME_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, conn_set_volume_value_receive)],
        },
        fallbacks=[
            CommandHandler("cancel", conn_cancel), # دستور /cancel
            CallbackQueryHandler(conn_cancel, pattern="^conn:cancel_edit:\d+$") # دکمه لغو Inline
        ],
        map_to_parent={ ConversationHandler.END: -1 }
    )
    application.add_handler(edit_conn_volume_conv_handler)



    # --- ConversationHandler برای ویرایش حالت کپی اتصال ---
    edit_conn_mode_conv_handler = ConversationHandler(
        entry_points=[
            # این دکمه از conn_display_copy می‌آید و منوی انتخاب حالت را نشان می‌دهد
            CallbackQueryHandler(conn_set_mode_menu, pattern="^conn:edit:mode_menu:\d+$"),
            # این دکمه از conn_set_mode_menu می‌آید و حالت را پردازش می‌کند
            # اگر SYMBOLS بود وارد state می‌شود، وگرنه مستقیم آپدیت می‌کند
            CallbackQueryHandler(conn_set_mode_action, pattern="^conn:set_mode_action:(ALL|GOLD_ONLY|SYMBOLS):\d+$")
        ],
        states={
            CONN_SYMBOLS: [MessageHandler(filters.TEXT & ~filters.COMMAND, conn_set_symbols_receive)],
        },
        fallbacks=[
            CommandHandler("cancel", conn_cancel),
            CallbackQueryHandler(conn_cancel, pattern="^conn:cancel_edit:\d+$") # دکمه لغو مشترک
        ],
        map_to_parent={ ConversationHandler.END: -1 }
    )
    application.add_handler(edit_conn_mode_conv_handler)



    # --- ConversationHandler برای ویرایش محدودیت‌های امنیتی اتصال ---
    edit_conn_limit_conv_handler = ConversationHandler(
        entry_points=[
            # این دکمه از conn_display_copy می‌آید و گفتگو را شروع می‌کند
            CallbackQueryHandler(conn_set_limit_start, pattern="^conn:edit:limit:(max_lot|max_trades|dd_limit):\d+$")
        ],
        states={
            CONN_LIMIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, conn_set_limit_receive)],
        },
        fallbacks=[
            CommandHandler("cancel", conn_cancel),
            CallbackQueryHandler(conn_cancel, pattern="^conn:cancel_edit:\d+$") # لغو مشترک
        ],
        map_to_parent={ ConversationHandler.END: -1 }
    )
    application.add_handler(edit_conn_limit_conv_handler)









    # Error handler (must be last)
    application.add_error_handler(error_handler)

    # ثبت دستورات در منوی تلگرام
    commands = [BotCommand("start", "شروع به کار و نمایش منوی اصلی")]

    # --- راه‌اندازی غیر-مسدود کننده ---
    try:
        asyncio.create_task(alert_sender_task(application))
        await application.initialize()
        await application.bot.set_my_commands(commands)
        if application.updater:
            await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        await application.start()
        logger.info("ربات تلگرام به صورت غیر-مسدود راه‌اندازی شد و در حال اجراست...")
        await asyncio.Event().wait() 
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("در حال خاموش کردن ربات تلگرام (درخواست لغو)...")
    except Exception as e:
        logger.error(f"خطای پیش‌بینی نشده در راه‌اندازی ربات: {e}", exc_info=True)
    finally:
        logger.info("شروع فرآیند خاموش کردن ربات تلگرام...")
        if application.updater and application.updater.is_running:
            await application.updater.stop()
        if application.running:
            await application.stop()
        await application.shutdown()
        logger.info("ربات تلگرام خاموش شد.")