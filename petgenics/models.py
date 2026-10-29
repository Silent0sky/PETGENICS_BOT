from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_code: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(500))
    brand: Mapped[str] = mapped_column(String(200), index=True)
    family: Mapped[str | None] = mapped_column(String(200), nullable=True)
    mrp: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    pack_size: Mapped[str | None] = mapped_column(String(120), nullable=True)
    selling_rate: Mapped[Decimal | None] = mapped_column(Numeric(27, 15), nullable=True)
    stock: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source_details: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Party(Base):
    __tablename__ = "parties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    party_code: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Salesperson(Base):
    __tablename__ = "salespersons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_discount: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_free_quantity: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_discount_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))


class SalespersonBrand(Base):
    __tablename__ = "salesperson_brands"
    __table_args__ = (UniqueConstraint("salesperson_id", "brand", name="uq_salesperson_brand"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    salesperson_id: Mapped[int] = mapped_column(ForeignKey("salespersons.id", ondelete="CASCADE"))
    brand: Mapped[str] = mapped_column(String(200), index=True)


class SalespersonParty(Base):
    __tablename__ = "salesperson_parties"
    __table_args__ = (UniqueConstraint("salesperson_id", "party_id", name="uq_salesperson_party"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    salesperson_id: Mapped[int] = mapped_column(ForeignKey("salespersons.id", ondelete="CASCADE"))
    party_id: Mapped[int] = mapped_column(ForeignKey("parties.id", ondelete="CASCADE"))


class BotSession(Base):
    __tablename__ = "bot_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    state: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("status IN ('Draft', 'Submitted', 'Approved', 'Rejected', 'Exported')", name="ck_order_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_number: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    submission_key: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    salesperson_id: Mapped[int] = mapped_column(ForeignKey("salespersons.id"))
    party_id: Mapped[int] = mapped_column(ForeignKey("parties.id"))
    brand: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="Draft", index=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_data: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    items: Mapped[list["OrderItem"]] = relationship(cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_item_quantity"),
        CheckConstraint("free_quantity >= 0", name="ck_order_item_free_quantity"),
        CheckConstraint("rate >= 0", name="ck_order_item_rate"),
        CheckConstraint("discount_percent >= 0 AND discount_percent <= 100", name="ck_order_item_discount"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    product_code: Mapped[str] = mapped_column(String(120))
    product_name: Mapped[str] = mapped_column(String(500))
    brand: Mapped[str] = mapped_column(String(200))
    pack_size: Mapped[str | None] = mapped_column(String(120), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    free_quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("0"))
    rate: Mapped[Decimal] = mapped_column(Numeric(27, 15))
    discount_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    line_total: Mapped[Decimal] = mapped_column(Numeric(14, 2))


class OrderAudit(Base):
    __tablename__ = "order_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    actor: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(100))
    details: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    filename: Mapped[str] = mapped_column(String(255))
    summary: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    errors: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    successful_rows: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MargMapping(Base):
    __tablename__ = "marg_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    columns: Mapped[dict] = mapped_column(JSONB, nullable=False)
    required_fields: Mapped[list] = mapped_column(JSONB, nullable=False)
    combined_allowed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ScannerRun(Base):
    __tablename__ = "scanner_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    filename: Mapped[str] = mapped_column(String(255))
    result: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    document_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
