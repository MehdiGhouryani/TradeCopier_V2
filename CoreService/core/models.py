# CoreService/core/models.py
#
# این فایل، شالوده و نقشه پایگاه داده ماست.
# ما در اینجا با استفاده از SQLAlchemy ORM، ساختار جداول را تعریف می‌کنیم.
# این مدل‌ها، جایگزین کامل فایل ecosystem.json و فایل‌های .cfg هستند.

import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship, declarative_base
from sqlalchemy.engine import Engine
from sqlalchemy import event

# نام فایل دیتابیس (می‌تواند به سادگی یک فایل SQLite باشد)
DATABASE_URL = "sqlite:///trade_copier.db"

# ایجاد پایه تعریف مدل‌ها
Base = declarative_base()

# --- فعال‌سازی پشتیبانی از Foreign Key در SQLite ---
# این بخش ضروری است تا SQLite محدودیت‌های روابط را جدی بگیرد
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

# --- تعریف جداول ---

class SourceAccount(Base):
    """
    جدول حساب‌های مستر (منابع سیگنال).
    جایگزین آرایه 'sources' در ecosystem.json
    """
    __tablename__ = 'source_accounts'
    
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False) # نام نمایشی (مثل: یعقوب)
    # شناسه منحصر به فرد برای تاپیک ZMQ و کامنت اکسپرت
    source_id_str = Column(String, unique=True, nullable=False, index=True) # (مثال: "source_1" یا "S1")
    account_number = Column(Integer) # شماره حساب MT5 (اختیاری، برای نمایش)
    
    # --- روابط ---
    # یک حساب سورس می‌تواند در چندین "نگاشت" (اتصال) استفاده شود
    mappings = relationship("SourceCopyMapping", back_populates="source_account", cascade="all, delete-orphan")
    # یک حساب سورس، تاریخچه معاملاتی مرتبط با خود را دارد
    trade_history = relationship("TradeHistory", back_populates="source_account")

    def __repr__(self):
        return f"<SourceAccount(id={self.id}, name='{self.name}', source_id_str='{self.source_id_str}')>"


class CopyAccount(Base):
    """
    جدول حساب‌های اسلیو (کپی‌کننده‌ها).
    جایگزین آرایه 'copies' در ecosystem.json
    """
    __tablename__ = 'copy_accounts'
    
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False) # نام نمایشی (مثال: کپی 1)
    # شناسه منحصر به فردی که اکسپرت اسلیو برای شناسایی خود ارسال می‌کند
    copy_id_str = Column(String, unique=True, nullable=False, index=True) # (مثال: "copy_A")
    account_number = Column(Integer) # شماره حساب MT5 (اختیاری)
    is_active = Column(Boolean, default=True, nullable=False) # آیا این اکانت کلاً فعال است؟
    
    # --- روابط ---
    # هر حساب کپی، یک ردیف تنظیمات اختصاصی دارد (رابطه یک به یک)
    settings = relationship("CopySettings", back_populates="copy_account", uselist=False, cascade="all, delete-orphan")
    # هر حساب کپی، می‌تواند به چندین سورس متصل شود (رابطه یک به چند)
    mappings = relationship("SourceCopyMapping", back_populates="copy_account", cascade="all, delete-orphan")
    # تاریخچه معاملات این حساب کپی
    trade_history = relationship("TradeHistory", back_populates="copy_account")

    def __repr__(self):
        return f"<CopyAccount(id={self.id}, name='{self.name}', copy_id_str='{self.copy_id_str}')>"


class CopySettings(Base):
    """
    تنظیمات اختصاصی هر حساب کپی (مانند مدیریت ریسک).
    جایگزین آبجکت 'settings' در ecosystem.json
    """
    __tablename__ = 'copy_settings'
    
    id = Column(Integer, primary_key=True)
    copy_account_id = Column(Integer, ForeignKey('copy_accounts.id', ondelete="CASCADE"), unique=True, nullable=False)
    
    # تنظیمات مدیریت ریسک روزانه کلی
    daily_drawdown_percent = Column(Float, default=5.0, nullable=False)
    alert_drawdown_percent = Column(Float, default=4.0, nullable=False)
    
    # فلگی که توسط اکسپرت برای ریست شدن DD استفاده می‌شود
    reset_dd_flag = Column(Boolean, default=False, nullable=False)
    
    # --- روابط ---
    # رابطه یک به یک با حساب کپی
    copy_account = relationship("CopyAccount", back_populates="settings")

    def __repr__(self):
        return f"<CopySettings(copy_account_id={self.copy_account_id}, dd_percent={self.daily_drawdown_percent})>"


class SourceCopyMapping(Base):
    """
    جدول اتصالات (نگاشت‌ها) - قلب تپنده منطق کپی.
    این جدول مشخص می‌کند «کدام حساب کپی» به «کدام حساب سورس» با «چه تنظیماتی» متصل است.
    جایگزین آبجکت 'mapping' در ecosystem.json
    """
    __tablename__ = 'source_copy_mappings'
    
    id = Column(Integer, primary_key=True)
    copy_account_id = Column(Integer, ForeignKey('copy_accounts.id', ondelete="CASCADE"), nullable=False)
    source_account_id = Column(Integer, ForeignKey('source_accounts.id', ondelete="CASCADE"), nullable=False)
    
    is_enabled = Column(Boolean, default=True, nullable=False) # آیا این اتصال خاص فعال است؟
    
    # تنظیمات فیلتر نمادها
    copy_mode = Column(String, default="ALL", nullable=False) # "ALL", "GOLD_ONLY", "SYMBOLS"
    allowed_symbols = Column(String, nullable=True) # "EURUSD;GBPUSD"
    
    # تنظیمات مدیریت حجم
    volume_type = Column(String, default="MULTIPLIER", nullable=False) # "MULTIPLIER", "FIXED"
    volume_value = Column(Float, default=1.0, nullable=False)
    
    # تنظیمات مدیریت ریسک پیشرفته (بر اساس هر سورس)
    max_lot_size = Column(Float, default=0.0, nullable=False) # 0.0 =
    max_concurrent_trades = Column(Integer, default=0, nullable=False) # 0 =
    source_drawdown_limit = Column(Float, default=0.0, nullable=False) # 0.0 =
    
    # --- روابط ---
    copy_account = relationship("CopyAccount", back_populates="mappings")
    source_account = relationship("SourceAccount", back_populates="mappings")
    
    # اطمینان از اینکه یک حساب کپی نمی‌تواند دو بار به یک سورس وصل شود
    __table_args__ = (UniqueConstraint('copy_account_id', 'source_account_id', name='_copy_source_uc'),)

    def __repr__(self):
        return f"<Mapping(copy_id={self.copy_account_id}, source_id={self.source_account_id}, vol_type='{self.volume_type}')>"


class TradeHistory(Base):
    """
    جدول تاریخچه معاملات.
    جایگزین مکانیزم پارس کردن لاگ برای آمار.
    """
    __tablename__ = 'trade_history'
    
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    copy_account_id = Column(Integer, ForeignKey('copy_accounts.id'), nullable=False)
    source_account_id = Column(Integer, ForeignKey('source_accounts.id'), nullable=True) # ممکن است سورس حذف شده باشد
    
    symbol = Column(String, nullable=False)
    profit = Column(Float, nullable=False)
    source_ticket = Column(Integer, index=True) # تیکت اصلی معامله در حساب مستر
    
    # --- روابط ---
    copy_account = relationship("CopyAccount", back_populates="trade_history")
    source_account = relationship("SourceAccount", back_populates="trade_history")

    def __repr__(self):
        return f"<TradeHistory(symbol='{self.symbol}', profit={self.profit})>"


# --- تابع اصلی برای راه‌اندازی دیتابیس ---

def init_db():
    """
    موتور دیتابیس را ایجاد کرده و تمام جداول تعریف شده در بالا را می‌سازد.
    """
    # ایجاد موتور دیتابیس
    engine = create_engine(DATABASE_URL, echo=False) # echo=True برای دیباگ SQL
    
    # ایجاد تمام جداول (اگر وجود نداشته باشند)
    Base.metadata.create_all(bind=engine)
    
    print("Database initialized and tables created successfully.")
    return engine

if __name__ == "__main__":
    # اگر این فایل مستقیماً اجرا شود، دیتابیس را می‌سازد
    print("Initializing database...")
    init_db()