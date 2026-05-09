import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class MarketplaceEnum(str, enum.Enum):
    marketplace_1 = "marketplace_1"
    marketplace_2 = "marketplace_2"
    all = "all"


class SyncStatusEnum(str, enum.Enum):
    running = "running"
    completed = "completed"
    failed = "failed"
    partial = "partial"


class SyncActionEnum(str, enum.Enum):
    stock_update = "stock_update"
    price_update = "price_update"
    skip = "skip"
    failed = "failed"


class AlertTypeEnum(str, enum.Enum):
    low_stock = "low_stock"
    out_of_stock = "out_of_stock"
    price_anomaly = "price_anomaly"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)

    # Stock levels
    supplier_stock: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    marketplace_1_stock: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    marketplace_2_stock: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Prices
    supplier_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    marketplace_1_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    marketplace_2_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Timestamps
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    sync_logs: Mapped[list["SyncLog"]] = relationship(
        "SyncLog", back_populates="product_ref", foreign_keys="SyncLog.sku",
        primaryjoin="Product.sku == SyncLog.sku", viewonly=True
    )
    alerts: Mapped[list["PriceAlert"]] = relationship(
        "PriceAlert", back_populates="product_ref", foreign_keys="PriceAlert.sku",
        primaryjoin="Product.sku == PriceAlert.sku", viewonly=True
    )


class SyncJob(Base):
    __tablename__ = "sync_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    marketplace: Mapped[MarketplaceEnum] = mapped_column(
        Enum(MarketplaceEnum), nullable=False
    )
    status: Mapped[SyncStatusEnum] = mapped_column(
        Enum(SyncStatusEnum), default=SyncStatusEnum.running, nullable=False
    )

    # Counters
    products_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    products_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    products_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Timestamps
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Error details
    error_summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Relationships
    logs: Mapped[list["SyncLog"]] = relationship("SyncLog", back_populates="sync_job")


class SyncLog(Base):
    __tablename__ = "sync_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sync_job_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sync_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sku: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    marketplace: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[SyncActionEnum] = mapped_column(Enum(SyncActionEnum), nullable=False)
    old_value: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    new_value: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    sync_job: Mapped["SyncJob"] = relationship("SyncJob", back_populates="logs")
    product_ref: Mapped[Optional["Product"]] = relationship(
        "Product",
        primaryjoin="SyncLog.sku == Product.sku",
        foreign_keys="[SyncLog.sku]",
        viewonly=True,
    )


class PriceAlert(Base):
    __tablename__ = "price_alerts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    alert_type: Mapped[AlertTypeEnum] = mapped_column(Enum(AlertTypeEnum), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    product_ref: Mapped[Optional["Product"]] = relationship(
        "Product",
        primaryjoin="PriceAlert.sku == Product.sku",
        foreign_keys="[PriceAlert.sku]",
        viewonly=True,
    )
