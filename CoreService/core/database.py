# CoreService/core/database.py
#
# این فایل، لایه انتزاعی دیتابیس (Database Abstraction Layer) است.
# تمام بخش‌های دیگر برنامه (سرور ZMQ، ربات تلگرام) باید *فقط*
# از توابع این فایل برای تعامل با دیتابیس استفاده کنند.
# این کار از بروز خطا جلوگیری کرده و مدیریت session ها را تضمین می‌کند.

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session, joinedload
from contextlib import contextmanager
import datetime

# وارد کردن مدل‌هایی که در فایل قبلی تعریف کردیم
from .models import Base, SourceAccount, CopyAccount, CopySettings, SourceCopyMapping, TradeHistory, DATABASE_URL

# --- راه‌اندازی اولیه موتور و Session ---

# ایجاد موتور اصلی اتصال به دیتابیس
# connect_args={"check_same_thread": False} مخصوص SQLite است تا در محیط‌های چندنخی (مانند ربات و سرور) کار کند
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

# ایجاد یک "کارخانه" سازنده Session
# این session ها، کانال‌های ارتباطی ما با دیتابیس هستند
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# (پیشرفته) استفاده از scoped_session برای اطمینان از اینکه هر نخ (thread)
# در برنامه، session منحصر به فرد خود را داشته باشد
session_factory = scoped_session(SessionLocal)

@contextmanager
def get_db_session():
    """
    یک ابزار مدیریتی (Context Manager) برای تضمین مدیریت صحیح Session ها.
    این تابع، یک session جدید باز می‌کند، آن را در اختیار کد قرار می‌دهد،
    سپس به طور خودکار commit (در صورت موفقیت) یا rollback (در صورت خطا)
    کرده و در نهایت session را می‌بندد.
    
    مثال استفاده:
    with get_db_session() as db:
        db.add(new_user)
    """
    db = session_factory()
    try:
        yield db
        db.commit()
    except Exception as e:
        print(f"Database Error: {e}")
        db.rollback()
        raise
    finally:
        session_factory.remove()

def init_db():
    """
    تمام جداول تعریف شده در models.py را در دیتابیس ایجاد می‌کند.
    این تابع باید در main.py یک بار فراخوانی شود.
    """
    print("Initializing database tables...")
    Base.metadata.create_all(bind=engine)
    print("Database tables created.")

# --- توابع CRUD (Create, Read, Update, Delete) ---
# این توابع، رابط کاربری تمیز ما برای ربات تلگرام و سرور ZMQ هستند

# === توابع مدیریتی ربات تلگرام ===

def add_source_account(name: str, source_id_str: str, account_number: int) -> SourceAccount:
    """یک حساب مستر (سورس) جدید اضافه می‌کند."""
    with get_db_session() as db:
        new_source = SourceAccount(
            name=name,
            source_id_str=source_id_str,
            account_number=account_number
        )
        db.add(new_source)
        db.flush() # برای دریافت id
        return new_source

def add_copy_account(name: str, copy_id_str: str, account_number: int, dd_percent: float = 5.0, alert_percent: float = 4.0) -> CopyAccount:
    """یک حساب اسلیو (کپی) جدید به همراه تنظیمات پیش‌فرض آن اضافه می‌کند."""
    with get_db_session() as db:
        new_copy = CopyAccount(
            name=name,
            copy_id_str=copy_id_str,
            account_number=account_number,
            is_active=True
        )
        db.add(new_copy)
        
        # بلافاصله تنظیمات پیش‌فرض را هم برای آن می‌سازیم
        new_settings = CopySettings(
            copy_account=new_copy,
            daily_drawdown_percent=dd_percent,
            alert_drawdown_percent=alert_percent
        )
        db.add(new_settings)
        db.flush()
        return new_copy

def create_mapping(copy_id_str: str, source_id_str: str, volume_type: str, volume_value: float, **kwargs) -> SourceCopyMapping:
    """یک اتصال جدید (نگاشت) بین اسلیو و مستر برقرار می‌کند."""
    with get_db_session() as db:
        # ابتدا ID عددی اسلیو و مستر را پیدا می‌کنیم
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
    """
    یک گزارش کامل از وضعیت تمام سیستم برای نمایش در ربات تلگرام آماده می‌کند.
    (نسخه اصلاح شده و امن برای جلوگیری از خطای Detached Session)
    """
    report = []
    with get_db_session() as db:
        # از joinedload برای بارگذاری بهینه روابط و جلوگیری از کوئری‌های N+1 استفاده می‌کنیم
        copy_accounts = (
            db.query(CopyAccount)
            .options(
                joinedload(CopyAccount.settings),
                joinedload(CopyAccount.mappings).joinedload(SourceCopyMapping.source_account)
            )
            .filter(CopyAccount.is_active == True)
            .all()
        )

        # --- اصلاحیه اصلی: تبدیل آبجکت‌ها به دیکشنری قبل از بستن سشن ---
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



def get_statistics(time_filter: str = "all"):
    """آمار سود و زیان را برای ربات تلگرام محاسبه می‌کند."""
    with get_db_session() as db:
        query = db.query(TradeHistory)
        
        now = datetime.datetime.utcnow()
        if time_filter == "today":
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
            query = query.filter(TradeHistory.timestamp >= start_date)
        elif time_filter == "7d":
            start_date = now - datetime.timedelta(days=7)
            query = query.filter(TradeHistory.timestamp >= start_date)
        
        # در اینجا می‌توانید کوئری‌های پیچیده‌تر آماری (مانند SUM و GROUP BY) بنویسید
        # فعلاً فقط لیست خام را برمی‌گردانیم
        return query.all()

# === توابع حیاتی برای سرور ZMQ (ارتباط با اکسپرت‌ها) ===

def get_config_for_copy_ea(copy_id_str: str) -> dict:
    """
    مهم‌ترین تابع جدید:
    تنظیمات کامل یک اکسپرت اسلیو را برای ارسال از طریق ZMQ آماده می‌کند.
    """
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

        # ۱. افزودن تنظیمات کلی
        if copy_account.settings:
            config["global_settings"] = {
                "daily_drawdown_percent": copy_account.settings.daily_drawdown_percent,
                "alert_drawdown_percent": copy_account.settings.alert_drawdown_percent,
                "reset_dd_flag": copy_account.settings.reset_dd_flag
            }
            # (اختیاری) پس از خواندن فلگ ریست، آن را خاموش می‌کنیم
            if copy_account.settings.reset_dd_flag:
                copy_account.settings.reset_dd_flag = False

        # ۲. افزودن لیست اتصالات (Mappings)
        for mapping in copy_account.mappings:
            if mapping.is_enabled and mapping.source_account:
                config["mappings"].append({
                    # شناسه سورس برای دنبال کردن در تاپیک ZMQ
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
    """
    گزارش بسته شدن معامله را (که از اکسپرت اسلیو می‌آید) در دیتابیس ذخیره می‌کند.
    """
    with get_db_session() as db:
        # پیدا کردن ID های عددی بر اساس ID های رشته‌ای
        copy_account = db.query(CopyAccount.id).filter(CopyAccount.copy_id_str == copy_id_str).scalar()
        source_account = db.query(SourceAccount.id).filter(SourceAccount.source_id_str == source_id_str).scalar()
        
        if not copy_account:
            print(f"Warning: Could not save history. Copy account '{copy_id_str}' not found.")
            return

        new_trade = TradeHistory(
            copy_account_id=copy_account,
            source_account_id=source_account, # ممکن است None باشد اگر سورس حذف شده باشد
            symbol=symbol,
            profit=profit,
            source_ticket=source_ticket
        )
        db.add(new_trade)

# --- اجرای آزمایشی (برای تست فایل) ---
if __name__ == "__main__":
    print("Initializing DB...")
    init_db()
    
    print("\n--- Testing Database Functions ---")
    
    try:
        # ۱. افزودن سورس و کپی
        print("Adding accounts...")
        src1 = add_source_account(name="یعقوب", source_id_str="S1", account_number=1001)
        cpy1 = add_copy_account(name="کپی 1", copy_id_str="C_A", account_number=2001, dd_percent=10.0)
        
        # ۲. ایجاد اتصال
        print("Creating mapping...")
        mapping1 = create_mapping(
            copy_id_str="C_A",
            source_id_str="S1",
            volume_type="MULTIPLIER",
            volume_value=2.0,
            max_lot_size=1.5
        )
        
        # ۳. دریافت تنظیمات (همانطور که اکسپرت آن را می‌بیند)
        print("\n--- Generating Config for EA 'C_A' ---")
        config_data = get_config_for_copy_ea("C_A")
        import json
        print(json.dumps(config_data, indent=2))
        
        # ۴. ذخیره تاریخچه معامله
        print("\nSaving trade history...")
        save_trade_history(
            copy_id_str="C_A",
            source_id_str="S1",
            symbol="XAUUSD",
            profit=150.75,
            source_ticket=12345
        )
        
        # ۵. دریافت گزارش وضعیت
        print("\n--- Full Status Report ---")
        report = get_full_status_report()
        for copy_acc in report:
            print(f"🛡️ Account: {copy_acc.name} (Active: {copy_acc.is_active})")
            print(f"  Settings: DD={copy_acc.settings.daily_drawdown_percent}%")
            for m in copy_acc.mappings:
                print(f"    └── 🔗 -> {m.source_account.name} (Vol: {m.volume_type} {m.volume_value})")

        print("\n✅ Database test completed successfully.")

    except Exception as e:
        print(f"\n❌ Database test failed: {e}")