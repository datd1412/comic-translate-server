import uuid
from datetime import datetime, timedelta
from typing import Optional, List
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Boolean, ForeignKey, Text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

from config import settings

engine_kwargs = {"pool_pre_ping": True}
if settings.DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(settings.DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String, unique=True, index=True)
    password_hash = Column(String, nullable=True)
    auth_provider = Column(String, default="email")  # "email" or "google"
    
    # Standard comic-translate credits format
    credits_subscription = Column(Integer, default=0)
    credits_one_time = Column(Integer, default=settings.DEFAULT_FREE_CREDITS)
    credits_total = Column(Integer, default=settings.DEFAULT_FREE_CREDITS)
    monthly_credits = Column(Integer, default=0)
    tier = Column(String, default="Free")
    
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    sessions = relationship("UserSession", back_populates="user", cascade="all, delete")
    payments = relationship("Payment", back_populates="user", cascade="all, delete")
    credit_ledger_entries = relationship("CreditLedger", back_populates="user", cascade="all, delete")
    
    def update_total_credits(self):
        self.credits_total = self.credits_subscription + self.credits_one_time

class UserSession(Base):
    __tablename__ = "sessions"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"))
    refresh_token = Column(String, unique=True, index=True)
    expires_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    user = relationship("User", back_populates="sessions")


class Payment(Base):
    __tablename__ = "payments"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    provider = Column(String, nullable=False, default="payos")

    # Provider IDs for idempotency and reconciliation
    provider_txn_ref = Column(String, unique=True, index=True, nullable=False)
    provider_transaction_id = Column(String, unique=True, index=True, nullable=True)
    idempotency_key = Column(String, unique=True, index=True, nullable=True)

    amount_vnd = Column(Integer, nullable=False)
    credits = Column(Integer, nullable=False)
    currency = Column(String, nullable=False, default="VND")
    status = Column(String, nullable=False, default="pending")

    checkout_url = Column(Text, nullable=True)
    description = Column(String, nullable=True)
    raw_request = Column(Text, nullable=True)
    raw_callback = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    paid_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="payments")


class CreditLedger(Base):
    __tablename__ = "credit_ledger"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)

    delta_credits = Column(Integer, nullable=False)
    balance_before = Column(Integer, nullable=False)
    balance_after = Column(Integer, nullable=False)

    entry_type = Column(String, nullable=False)  # topup, usage, refund, adjustment
    reference_type = Column(String, nullable=True)  # payment, translation, ocr
    reference_id = Column(String, nullable=True)
    note = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    user = relationship("User", back_populates="credit_ledger_entries")

# Create tables
Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
