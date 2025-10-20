import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship, declarative_base
from sqlalchemy.engine import Engine
from sqlalchemy import event

DATABASE_URL = "sqlite:///trade_copier.db"
Base = declarative_base()

@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """فعال‌سازی پشتیبانی از Foreign Key در SQLite."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

class SourceAccount(Base):
    """جدول حساب‌های مستر (منابع سیگنال)."""
    __tablename__ = 'source_accounts'
    
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    source_id_str = Column(String, unique=True, nullable=False, index=True)
    account_number = Column(Integer)
    mappings = relationship("SourceCopyMapping", back_populates="source_account", cascade="all, delete-orphan")
    trade_history = relationship("TradeHistory", back_populates="source_account")

    def __repr__(self):
        return f"<SourceAccount(id={self.id}, name='{self.name}', source_id_str='{self.source_id_str}')>"

class CopyAccount(Base):
    """جدول حساب‌های کپی (اسلیو)."""
    __tablename__ = 'copy_accounts'
    
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    copy_id_str = Column(String, unique=True, nullable=False, index=True)
    account_number = Column(Integer)
    is_active = Column(Boolean, default=True, nullable=False)
    settings = relationship("CopySettings", back_populates="copy_account", uselist=False, cascade="all, delete-orphan")
    mappings = relationship("SourceCopyMapping", back_populates="copy_account", cascade="all, delete-orphan")
    trade_history = relationship("TradeHistory", back_populates="copy_account")

    def __repr__(self):
        return f"<CopyAccount(id={self.id}, name='{self.name}', copy_id_str='{self.copy_id_str}')>"

class CopySettings(Base):
    """تنظیمات اختصاصی حساب‌های کپی."""
    __tablename__ = 'copy_settings'
    
    id = Column(Integer, primary_key=True)
    copy_account_id = Column(Integer, ForeignKey('copy_accounts.id', ondelete="CASCADE"), unique=True, nullable=False)
    daily_drawdown_percent = Column(Float, default=5.0, nullable=False)
    alert_drawdown_percent = Column(Float, default=4.0, nullable=False)
    reset_dd_flag = Column(Boolean, default=False, nullable=False)
    copy_account = relationship("CopyAccount", back_populates="settings")

    def __repr__(self):
        return f"<CopySettings(copy_account_id={self.copy_account_id}, dd_percent={self.daily_drawdown_percent})>"

class SourceCopyMapping(Base):
    """جدول اتصالات بین حساب‌های کپی و مستر."""
    __tablename__ = 'source_copy_mappings'
    
    id = Column(Integer, primary_key=True)
    copy_account_id = Column(Integer, ForeignKey('copy_accounts.id', ondelete="CASCADE"), nullable=False)
    source_account_id = Column(Integer, ForeignKey('source_accounts.id', ondelete="CASCADE"), nullable=False)
    is_enabled = Column(Boolean, default=True, nullable=False)
    copy_mode = Column(String, default="ALL", nullable=False)
    allowed_symbols = Column(String, nullable=True)
    volume_type = Column(String, default="MULTIPLIER", nullable=False)
    volume_value = Column(Float, default=1.0, nullable=False)
    max_lot_size = Column(Float, default=0.0, nullable=False)
    max_concurrent_trades = Column(Integer, default=0, nullable=False)
    source_drawdown_limit = Column(Float, default=0.0, nullable=False)
    copy_account = relationship("CopyAccount", back_populates="mappings")
    source_account = relationship("SourceAccount", back_populates="mappings")
    __table_args__ = (UniqueConstraint('copy_account_id', 'source_account_id', name='_copy_source_uc'),)

    def __repr__(self):
        return f"<Mapping(copy_id={self.copy_account_id}, source_id={self.source_account_id}, vol_type='{self.volume_type}')>"

class TradeHistory(Base):
    """جدول تاریخچه معاملات."""
    __tablename__ = 'trade_history'
    
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    copy_account_id = Column(Integer, ForeignKey('copy_accounts.id'), nullable=False)
    source_account_id = Column(Integer, ForeignKey('source_accounts.id'), nullable=True)
    symbol = Column(String, nullable=False)
    profit = Column(Float, nullable=False)
    source_ticket = Column(Integer, index=True)
    copy_account = relationship("CopyAccount", back_populates="trade_history")
    source_account = relationship("SourceAccount", back_populates="trade_history")

    def __repr__(self):
        return f"<TradeHistory(symbol='{self.symbol}', profit={self.profit})>"

def init_db():
    """ایجاد موتور دیتابیس و جداول."""
    engine = create_engine(DATABASE_URL, echo=False)
    Base.metadata.create_all(bind=engine)
    print("Database initialized and tables created successfully.")
    return engine

if __name__ == "__main__":
    """تست راه‌اندازی دیتابیس."""
    print("Initializing database...")
    init_db()
