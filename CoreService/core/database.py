from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker, scoped_session, joinedload
from contextlib import contextmanager
import datetime
import logging

from sqlalchemy import func # <-- Ensure this exists
from sqlalchemy import case # <-- Add this for conditional logic if needed later


from .models import Base, SourceAccount, CopyAccount, CopySettings, SourceCopyMapping, TradeHistory, DATABASE_URL

logger = logging.getLogger(__name__)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
session_factory = scoped_session(SessionLocal)

@contextmanager
def get_db_session():
    """مدیریت session دیتابیس با commit/rollback خودکار."""
    db = session_factory()
    try:
        yield db
        db.commit()
    except Exception as e:
        logger.error(f"Database Error: {e}")
        db.rollback()
        raise
    finally:
        session_factory.remove()

def init_db():
    """ایجاد جداول دیتابیس."""
    logger.info("Initializing database tables...")
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created.")

def add_source_account(name: str) -> SourceAccount:
    """افزودن حساب مستر جدید با ID هوشمند (S1, S2, ...)."""
    with get_db_session() as db:
        last_source = db.query(SourceAccount)\
                        .filter(SourceAccount.source_id_str.like('S%'))\
                        .order_by(SourceAccount.id.desc())\
                        .first()
        next_id_num = 1
        if last_source:
            try:
                num_part = last_source.source_id_str.replace('S', '')
                if num_part.isdigit():
                    next_id_num = int(num_part) + 1
                else:
                    max_numeric_id = db.query(func.max(SourceAccount.id)).scalar()
                    if max_numeric_id:
                        next_id_num = max_numeric_id + 1
            except Exception:
                max_numeric_id = db.query(func.max(SourceAccount.id)).scalar()
                if max_numeric_id:
                    next_id_num = max_numeric_id + 1
        new_source_id_str = f"S{next_id_num}"
        existing = db.query(SourceAccount).filter(SourceAccount.source_id_str == new_source_id_str).first()
        while existing:
            next_id_num += 1
            new_source_id_str = f"S{next_id_num}"
            existing = db.query(SourceAccount).filter(SourceAccount.source_id_str == new_source_id_str).first()
        new_source = SourceAccount(
            name=name,
            source_id_str=new_source_id_str,
        )
        db.add(new_source)
        db.flush()
        logger.info(f"New source account '{name}' added with smart ID '{new_source_id_str}'", 
                    extra={'entity_id': new_source.id, 'details': {'name': name, 'source_id_str': new_source_id_str}})
        return new_source

def delete_source_account(source_id: int) -> bool:
    """حذف حساب مستر و تمام اتصالات مرتبط."""
    with get_db_session() as db:
        source_to_delete = db.query(SourceAccount).filter(SourceAccount.id == source_id).first()
        if source_to_delete:
            source_name = source_to_delete.name
            source_str_id = source_to_delete.source_id_str
            db.delete(source_to_delete)
            logger.info(f"Source account '{source_name}' (ID: {source_id}, StrID: {source_str_id}) deleted successfully.", 
                        extra={'entity_id': source_id})
            return True
        else:
            logger.warning(f"Attempted to delete non-existent source account with ID: {source_id}", 
                         extra={'entity_id': source_id})
            return False

def update_source_account_name(source_id: int, new_name: str) -> SourceAccount | None:
    """بروزرسانی نام حساب مستر."""
    with get_db_session() as db:
        source_to_update = db.query(SourceAccount).filter(SourceAccount.id == source_id).first()
        if source_to_update:
            old_name = source_to_update.name
            source_to_update.name = new_name
            logger.info(f"Source account name updated (ID: {source_id}) from '{old_name}' to '{new_name}'.",
                        extra={'entity_id': source_id, 'details': {'old_name': old_name, 'new_name': new_name}})
            return source_to_update
        else:
            logger.warning(f"Attempted to update name for non-existent source account with ID: {source_id}",
                         extra={'entity_id': source_id})
            return None






# === توابع مدیریت حساب‌های کپی ===

def is_copy_id_str_unique(copy_id_str: str) -> bool:
    """بررسی می‌کند آیا Copy ID Str داده شده در دیتابیس منحصر به فرد است یا خیر."""
    with get_db_session() as db:
        return db.query(CopyAccount).filter(CopyAccount.copy_id_str == copy_id_str).first() is None

def add_copy_account(name: str, copy_id_str: str, dd_percent: float = 5.0, alert_percent: float = 4.0) -> CopyAccount:
    """افزودن حساب کپی جدید با تنظیمات پیش‌فرض."""
    # اعتبارسنجی اولیه
    if not name or not copy_id_str:
        raise ValueError("Name and Copy ID Str cannot be empty.")
    if dd_percent < 0 or alert_percent < 0:
         raise ValueError("Drawdown percentages cannot be negative.")

    with get_db_session() as db:
        # بررسی یکتایی copy_id_str
        existing = db.query(CopyAccount.id).filter(CopyAccount.copy_id_str == copy_id_str).first()
        if existing:
            raise ValueError(f"Copy ID Str '{copy_id_str}' already exists.")

        new_copy = CopyAccount(
            name=name,
            copy_id_str=copy_id_str,
            is_active=True # حساب‌های جدید به طور پیش‌فرض فعال هستند
            # account_number اکنون اختیاری است و توسط ربات پرسیده نمی‌شود
        )
        db.add(new_copy)
        # flush اولیه برای گرفتن new_copy.id قبل از ساختن settings
        db.flush()

        # ایجاد تنظیمات پیش‌فرض
        new_settings = CopySettings(
            copy_account_id=new_copy.id, # اتصال به حساب کپی تازه ایجاد شده
            daily_drawdown_percent=dd_percent,
            alert_drawdown_percent=alert_percent,
            reset_dd_flag=False # پیش‌فرض
        )
        db.add(new_settings)
        db.flush() # flush نهایی برای اطمینان از ذخیره settings
        logger.info(f"New copy account '{name}' (ID Str: {copy_id_str}) added with settings (DD: {dd_percent}%, Alert: {alert_percent}%).",
                    extra={'entity_id': new_copy.id, 'details': {'name': name, 'copy_id_str': copy_id_str, 'dd': dd_percent}})
        # refresh برای بارگذاری رابطه settings (اختیاری، اگر نیاز به دسترسی فوری باشد)
        # db.refresh(new_copy, ['settings'])
        return new_copy
    



def delete_copy_account(copy_id: int) -> bool:
    """حذف حساب کپی و تمام تنظیمات و اتصالات مرتبط."""
    with get_db_session() as db:
        copy_to_delete = db.query(CopyAccount).filter(CopyAccount.id == copy_id).first()
        if copy_to_delete:
            copy_name = copy_to_delete.name
            copy_str_id = copy_to_delete.copy_id_str
            db.delete(copy_to_delete)
            # cascade حذف CopySettings و SourceCopyMapping را انجام می‌دهد
            logger.info(f"Copy account '{copy_name}' (ID: {copy_id}, StrID: {copy_str_id}) deleted successfully.",
                        extra={'entity_id': copy_id})
            return True
        else:
            logger.warning(f"Attempted to delete non-existent copy account with ID: {copy_id}",
                         extra={'entity_id': copy_id})
            return False

def update_copy_account_name(copy_id: int, new_name: str) -> CopyAccount | None:
    """بروزرسانی نام حساب کپی."""
    if not new_name:
        raise ValueError("New name cannot be empty.")
    with get_db_session() as db:
        copy_to_update = db.query(CopyAccount).filter(CopyAccount.id == copy_id).first()
        if copy_to_update:
            old_name = copy_to_update.name
            copy_to_update.name = new_name
            logger.info(f"Copy account name updated (ID: {copy_id}) from '{old_name}' to '{new_name}'.",
                        extra={'entity_id': copy_id, 'details': {'old_name': old_name, 'new_name': new_name}})
            return copy_to_update
        else:
            logger.warning(f"Attempted to update name for non-existent copy account with ID: {copy_id}",
                         extra={'entity_id': copy_id})
            return None

def update_copy_settings(copy_id: int, settings_data: dict) -> bool:
    """بروزرسانی تنظیمات یک حساب کپی (CopySettings)."""
    allowed_keys = {'daily_drawdown_percent', 'alert_drawdown_percent', 'reset_dd_flag'}
    update_values = {}
    log_details = {}

    # اعتبارسنجی ورودی‌ها
    for key, value in settings_data.items():
        if key not in allowed_keys:
            logger.warning(f"Attempted to update invalid setting '{key}' for copy ID {copy_id}.")
            continue # از کلید نامعتبر رد شو

        if key in ['daily_drawdown_percent', 'alert_drawdown_percent']:
            try:
                float_value = float(value)
                if float_value < 0:
                    raise ValueError(f"{key} cannot be negative.")
                update_values[key] = float_value
                log_details[key] = float_value
            except (ValueError, TypeError):
                 logger.error(f"Invalid value type for {key}: {value}. Expected positive float.", extra={'entity_id': copy_id})
                 return False # خطا در اعتبارسنجی
        elif key == 'reset_dd_flag':
             # این مقدار باید boolean باشد
             bool_value = bool(value)
             update_values[key] = bool_value
             log_details[key] = bool_value

    if not update_values:
         logger.warning(f"No valid settings provided to update for copy ID {copy_id}.")
         return False # هیچ داده معتبری برای بروزرسانی وجود نداشت

    with get_db_session() as db:
        settings_to_update = db.query(CopySettings).filter(CopySettings.copy_account_id == copy_id).first()
        if settings_to_update:
            for key, value in update_values.items():
                setattr(settings_to_update, key, value)
            logger.info(f"Copy settings updated for ID {copy_id}.",
                        extra={'entity_id': copy_id, 'details': log_details})
            return True
        else:
            logger.error(f"CopySettings not found for copy account ID: {copy_id}", extra={'entity_id': copy_id})
            return False




def get_copy_account_by_id(copy_id: int) -> CopyAccount | None:
    """دریافت اطلاعات یک حساب کپی با ID عددی."""
    with get_db_session() as db:
        # ممکن است بخواهیم تنظیمات را همزمان بارگذاری کنیم
        return db.query(CopyAccount).options(joinedload(CopyAccount.settings)).filter(CopyAccount.id == copy_id).first()

def get_mapping_by_id(mapping_id: int) -> SourceCopyMapping | None:
    """دریافت اطلاعات یک اتصال (Mapping) با ID رکورد آن."""
    with get_db_session() as db:
        # بارگذاری روابط source_account و copy_account برای دسترسی به نام‌ها
        return db.query(SourceCopyMapping)\
                 .options(joinedload(SourceCopyMapping.source_account),
                          joinedload(SourceCopyMapping.copy_account))\
                 .filter(SourceCopyMapping.id == mapping_id).first()



# === توابع اصلی مدیریت اتصالات ===

def get_mappings_for_copy(copy_id: int) -> list[dict]:
    """دریافت لیست اتصالات فعال برای یک حساب کپی خاص به صورت دیکشنری."""
    mappings_list = []
    with get_db_session() as db:
        mappings = db.query(SourceCopyMapping)\
                     .options(joinedload(SourceCopyMapping.source_account))\
                     .filter(SourceCopyMapping.copy_account_id == copy_id)\
                     .order_by(SourceCopyMapping.id)\
                     .all()

        for m in mappings:
            if m.source_account: # اطمینان از اینکه منبع حذف نشده
                mappings_list.append({
                    "id": m.id, # ID خود رکورد mapping
                    "copy_account_id": m.copy_account_id,
                    "source_account_id": m.source_account_id,
                    "source_name": m.source_account.name,
                    "source_id_str": m.source_account.source_id_str,
                    "is_enabled": m.is_enabled,
                    "copy_mode": m.copy_mode,
                    "allowed_symbols": m.allowed_symbols,
                    "volume_type": m.volume_type,
                    "volume_value": m.volume_value,
                    "max_lot_size": m.max_lot_size,
                    "max_concurrent_trades": m.max_concurrent_trades,
                    "source_drawdown_limit": m.source_drawdown_limit
                })
    return mappings_list

def get_available_sources_for_copy(copy_id: int) -> list[SourceAccount]:
    """دریافت لیست منابعی که هنوز به حساب کپی مشخص شده متصل نشده‌اند."""
    with get_db_session() as db:
        # ۱. گرفتن ID تمام منابعی که *به* این کپی متصل هستند
        connected_source_ids = db.query(SourceCopyMapping.source_account_id)\
                                 .filter(SourceCopyMapping.copy_account_id == copy_id)\
                                 .subquery() # ایجاد یک زیرکوئری

        # ۲. گرفتن تمام منابعی که ID آنها در لیست بالا *نیست*
        available_sources = db.query(SourceAccount)\
                              .filter(SourceAccount.id.notin_(connected_source_ids))\
                              .order_by(SourceAccount.name)\
                              .all()
        return available_sources

def create_mapping_by_ids(copy_id: int, source_id: int, **kwargs) -> SourceCopyMapping | None:
    """ایجاد اتصال جدید با استفاده از ID عددی کپی و منبع، با تنظیمات پیش‌فرض."""
    with get_db_session() as db:
        # بررسی وجود کپی و منبع
        copy_exists = db.query(CopyAccount.id).filter(CopyAccount.id == copy_id).first()
        source_exists = db.query(SourceAccount.id).filter(SourceAccount.id == source_id).first()
        if not copy_exists or not source_exists:
            logger.error(f"Cannot create mapping: Copy (ID:{copy_id}) or Source (ID:{source_id}) not found.")
            raise ValueError("Copy or Source account not found by ID")

        # بررسی عدم وجود اتصال تکراری
        existing_mapping = db.query(SourceCopyMapping.id)\
                             .filter(SourceCopyMapping.copy_account_id == copy_id,
                                     SourceCopyMapping.source_account_id == source_id)\
                             .first()
        if existing_mapping:
             logger.warning(f"Mapping between Copy ID {copy_id} and Source ID {source_id} already exists.")
             return None # یا خطای مناسب‌تری برگردانیم

        # ایجاد اتصال با تنظیمات پیش‌فرض V2 + محدودیت‌های پیش‌فرض V1 (صفر = نامحدود)
        new_mapping = SourceCopyMapping(
            copy_account_id=copy_id,
            source_account_id=source_id,
            is_enabled=kwargs.get('is_enabled', True),
            volume_type=kwargs.get('volume_type', "MULTIPLIER"),
            volume_value=kwargs.get('volume_value', 1.0),
            copy_mode=kwargs.get('copy_mode', 'ALL'),
            allowed_symbols=kwargs.get('allowed_symbols', None),
            max_lot_size=kwargs.get('max_lot_size', 0.0), # پیش‌فرض نامحدود
            max_concurrent_trades=kwargs.get('max_concurrent_trades', 0), # پیش‌فرض نامحدود
            source_drawdown_limit=kwargs.get('source_drawdown_limit', 0.0) # پیش‌فرض نامحدود
        )
        db.add(new_mapping)
        db.flush()
        logger.info(f"New mapping created between Copy ID {copy_id} and Source ID {source_id} (Mapping ID: {new_mapping.id}).")
        return new_mapping

def delete_mapping(mapping_id: int) -> bool:
    """حذف یک اتصال با استفاده از ID رکورد mapping."""
    with get_db_session() as db:
        mapping_to_delete = db.query(SourceCopyMapping).filter(SourceCopyMapping.id == mapping_id).first()
        if mapping_to_delete:
            copy_id = mapping_to_delete.copy_account_id
            source_id = mapping_to_delete.source_account_id
            db.delete(mapping_to_delete)
            logger.info(f"Mapping (ID: {mapping_id}) between Copy ID {copy_id} and Source ID {source_id} deleted successfully.",
                        extra={'entity_id': mapping_id})
            return True
        else:
            logger.warning(f"Attempted to delete non-existent mapping with ID: {mapping_id}",
                         extra={'entity_id': mapping_id})
            return False

def update_mapping_settings(mapping_id: int, settings_data: dict) -> bool:
    """بروزرسانی تنظیمات یک اتصال (Mapping) با استفاده از ID رکورد آن."""
    # لیست ستون‌های مجاز برای بروزرسانی در جدول SourceCopyMapping
    allowed_keys = {
        'is_enabled', 'copy_mode', 'allowed_symbols',
        'volume_type', 'volume_value',
        'max_lot_size', 'max_concurrent_trades', 'source_drawdown_limit'
    }
    update_values = {}
    log_details = {}

    # اعتبارسنجی ورودی‌ها
    for key, value in settings_data.items():
        if key not in allowed_keys:
            logger.warning(f"Attempted to update invalid mapping setting '{key}' for Mapping ID {mapping_id}.")
            continue

        # اعتبارسنجی نوع داده (مثال ساده)
        field_type = SourceCopyMapping.__table__.columns[key].type
        try:
            if isinstance(field_type, (sqlalchemy.sql.sqltypes.Float, sqlalchemy.sql.sqltypes.Numeric)):
                new_val = float(value)
                if new_val < 0 and key not in ['source_drawdown_limit']: # DD limit می‌تواند منفی باشد؟ (در مدل 0 است)
                    raise ValueError(f"{key} cannot be negative.")
                update_values[key] = new_val
            elif isinstance(field_type, sqlalchemy.sql.sqltypes.Integer):
                new_val = int(value)
                if new_val < 0:
                    raise ValueError(f"{key} cannot be negative.")
                update_values[key] = new_val
            elif isinstance(field_type, sqlalchemy.sql.sqltypes.Boolean):
                update_values[key] = bool(value)
            elif isinstance(field_type, sqlalchemy.sql.sqltypes.String):
                update_values[key] = str(value) if value is not None else None
            else:
                 update_values[key] = value # برای انواع دیگر فعلا بدون اعتبارسنجی

            log_details[key] = update_values[key]

        except (ValueError, TypeError) as e:
             logger.error(f"Invalid value type for mapping setting '{key}': {value}. Error: {e}", extra={'entity_id': mapping_id})
             return False # خطا در اعتبارسنجی

    if not update_values:
         logger.warning(f"No valid mapping settings provided to update for Mapping ID {mapping_id}.")
         return False

    with get_db_session() as db:
        mapping_to_update = db.query(SourceCopyMapping).filter(SourceCopyMapping.id == mapping_id).first()
        if mapping_to_update:
            for key, value in update_values.items():
                setattr(mapping_to_update, key, value)
            logger.info(f"Mapping settings updated for ID {mapping_id}.",
                        extra={'entity_id': mapping_id, 'details': log_details})
            return True
        else:
            logger.error(f"Mapping not found for ID: {mapping_id}", extra={'entity_id': mapping_id})
            return False

# --- (پایان بخش مدیریت اتصالات) ---










def create_mapping(copy_id_str: str, source_id_str: str, volume_type: str, volume_value: float, **kwargs) -> SourceCopyMapping:
    """ایجاد اتصال جدید بین حساب کپی و مستر."""
    with get_db_session() as db:
        copy_account = db.query(CopyAccount).filter(CopyAccount.copy_id_str == copy_id_str).first()
        source_account = db.query(SourceAccount).filter(SourceAccount.source_id_str == source_id_str).first()
        if not copy_account or not source_account:
            raise ValueError("Copy or Source account not found")
        new_mapping = SourceCopyMapping(
            copy_account_id=copy_account.id,
            source_account_id=source_account.id,
            volume_type=volume_type,
            volume_value=volume_value,
            copy_mode=kwargs.get('copy_mode', 'ALL'),
            allowed_symbols=kwargs.get('allowed_symbols'),
            max_lot_size=kwargs.get('max_lot_size', 0.0),
            max_concurrent_trades=kwargs.get('max_concurrent_trades', 0),
            source_drawdown_limit=kwargs.get('source_drawdown_limit', 0.0)
        )
        db.add(new_mapping)
        db.flush()
        return new_mapping

def get_full_status_report():
    """تهیه گزارش کامل وضعیت سیستم برای ربات تلگرام."""
    report = []
    with get_db_session() as db:
        copy_accounts = (
            db.query(CopyAccount)
            .options(
                joinedload(CopyAccount.settings),
                joinedload(CopyAccount.mappings).joinedload(SourceCopyMapping.source_account)
            )
            .filter(CopyAccount.is_active == True)
            .all()
        )
        for acc in copy_accounts:
            acc_data = {
                "name": acc.name,
                "copy_id_str": acc.copy_id_str,
                "is_active": acc.is_active,
                "settings": {
                    "daily_drawdown_percent": acc.settings.daily_drawdown_percent if acc.settings else 0
                },
                "mappings": []
            }
            for m in acc.mappings:
                if m.source_account:
                    acc_data["mappings"].append({
                        "is_enabled": m.is_enabled,
                        "source_name": m.source_account.name,
                        "source_id_str": m.source_account.source_id_str,
                        "volume_type": m.volume_type,
                        "volume_value": m.volume_value
                    })
            report.append(acc_data)
    return report





# === توابع آمار ===

def get_statistics_summary(time_filter: str = "all") -> list[dict]:
    """محاسبه خلاصه آمار سود/زیان و تعداد معاملات با فیلتر زمانی."""
    with get_db_session() as db:
        # شروع کوئری با انتخاب ستون‌های لازم و توابع تجمعی
        query = db.query(
            TradeHistory.copy_account_id,
            TradeHistory.source_account_id,
            CopyAccount.name.label('copy_name'),
            SourceAccount.name.label('source_name'), # ممکن است NULL باشد
            func.sum(TradeHistory.profit).label('total_profit'),
            func.count(TradeHistory.id).label('trade_count')
        ).select_from(TradeHistory)\
         .join(CopyAccount, TradeHistory.copy_account_id == CopyAccount.id)\
         .outerjoin(SourceAccount, TradeHistory.source_account_id == SourceAccount.id) # Left join برای منابع حذف شده

        # اعمال فیلتر زمانی
        now = datetime.datetime.utcnow()
        start_date = None
        if time_filter == "today":
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif time_filter == "7d":
            start_date = now - datetime.timedelta(days=7)
            start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0) # شروع از ابتدای روز هفتم
        # elif time_filter == "30d": # (فعلا غیرفعال)
        #     start_date = now - datetime.timedelta(days=30)
        #     start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)

        if start_date:
            query = query.filter(TradeHistory.timestamp >= start_date)

        # گروه‌بندی نتایج
        query = query.group_by(
            TradeHistory.copy_account_id,
            TradeHistory.source_account_id,
            CopyAccount.name,
            SourceAccount.name
        ).order_by(
            CopyAccount.name, # مرتب‌سازی بر اساس نام حساب کپی
            SourceAccount.name # سپس بر اساس نام منبع
        )

        # اجرای کوئری و تبدیل نتایج به لیست دیکشنری
        results = query.all()

        # تبدیل نتایج SQLAlchemy Row به دیکشنری‌های ساده
        summary_list = [
            {
                "copy_account_id": r.copy_account_id,
                "source_account_id": r.source_account_id,
                "copy_name": r.copy_name,
                "source_name": r.source_name if r.source_account_id else "نامشخص/حذف شده", # مدیریت منبع حذف شده
                "total_profit": r.total_profit,
                "trade_count": r.trade_count
            } for r in results
        ]
        logger.info(f"Statistics summary generated for filter '{time_filter}'. Found {len(summary_list)} grouped results.")
        return summary_list




def get_config_for_copy_ea(copy_id_str: str) -> dict:
    """تهیه تنظیمات کامل برای اکسپرت کپی."""
    config = {
        "copy_id_str": copy_id_str,
        "global_settings": {},
        "mappings": []
    }
    with get_db_session() as db:
        copy_account = (
            db.query(CopyAccount)
            .options(
                joinedload(CopyAccount.settings),
                joinedload(CopyAccount.mappings).joinedload(SourceCopyMapping.source_account)
            )
            .filter(CopyAccount.copy_id_str == copy_id_str)
            .first()
        )
        if not copy_account:
            raise ValueError(f"No CopyAccount found with ID: {copy_id_str}")
        if not copy_account.is_active:
            raise ValueError(f"CopyAccount {copy_id_str} is disabled.")
        if copy_account.settings:
            config["global_settings"] = {
                "daily_drawdown_percent": copy_account.settings.daily_drawdown_percent,
                "alert_drawdown_percent": copy_account.settings.alert_drawdown_percent,
                "reset_dd_flag": copy_account.settings.reset_dd_flag
            }
            if copy_account.settings.reset_dd_flag:
                copy_account.settings.reset_dd_flag = False
        for mapping in copy_account.mappings:
            if mapping.is_enabled and mapping.source_account:
                config["mappings"].append({
                    "source_topic_id": mapping.source_account.source_id_str,
                    "copy_mode": mapping.copy_mode,
                    "allowed_symbols": mapping.allowed_symbols,
                    "volume_type": mapping.volume_type,
                    "volume_value": mapping.volume_value,
                    "max_lot_size": mapping.max_lot_size,
                    "max_concurrent_trades": mapping.max_concurrent_trades,
                    "source_drawdown_limit": mapping.source_drawdown_limit
                })
    return config

def save_trade_history(copy_id_str: str, source_id_str: str, symbol: str, profit: float, source_ticket: int):
    """ذخیره تاریخچه معامله از اکسپرت کپی."""
    with get_db_session() as db:
        copy_account = db.query(CopyAccount.id).filter(CopyAccount.copy_id_str == copy_id_str).scalar()
        source_account = db.query(SourceAccount.id).filter(SourceAccount.source_id_str == source_id_str).scalar()
        if not copy_account:
            logger.warning(f"Could not save history. Copy account '{copy_id_str}' not found.")
            return
        new_trade = TradeHistory(
            copy_account_id=copy_account,
            source_account_id=source_account,
            symbol=symbol,
            profit=profit,
            source_ticket=source_ticket
        )
        db.add(new_trade)

if __name__ == "__main__":
    """تست عملکرد توابع دیتابیس."""
    logger.info("Initializing DB...")
    init_db()
    logger.info("Testing Database Functions...")
    try:
        src1 = add_source_account(name="یعقوب هوشمند")
        cpy1 = add_copy_account(name="کپی ۱ هوشمند", copy_id_str="C_SMART", account_number=2002, dd_percent=10.0)
        mapping1 = create_mapping(
            copy_id_str="C_SMART",
            source_id_str=src1.source_id_str,
            volume_type="MULTIPLIER",
            volume_value=1.0,
        )
        save_trade_history(
            copy_id_str="C_SMART",
            source_id_str=src1.source_id_str,
            symbol="EURUSD", profit=50.25, source_ticket=67890
        )
        report = get_full_status_report()
        logger.info("✅ Database test completed successfully.")
    except Exception as e:
        logger.error(f"❌ Database test failed: {e}")