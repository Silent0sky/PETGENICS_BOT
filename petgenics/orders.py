from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from petgenics.models import (
    Order,
    OrderAudit,
    OrderItem,
    Party,
    Product,
    Salesperson,
    SalespersonBrand,
    SalespersonParty,
)
from petgenics.money import discount_value, free_quantity_value, line_total, order_quantity, rate_value


class OrderPermissionError(ValueError):
    pass


async def accessible_parties(session: AsyncSession, salesperson_id: int) -> list[Party]:
    result = await session.scalars(
        select(Party)
        .join(SalespersonParty, SalespersonParty.party_id == Party.id)
        .where(SalespersonParty.salesperson_id == salesperson_id, Party.active.is_(True))
        .order_by(Party.name)
    )
    return list(result)


async def accessible_brands(session: AsyncSession, salesperson_id: int) -> list[str]:
    result = await session.scalars(
        select(SalespersonBrand.brand)
        .join(Product, Product.brand == SalespersonBrand.brand)
        .where(SalespersonBrand.salesperson_id == salesperson_id, Product.active.is_(True))
        .distinct()
        .order_by(SalespersonBrand.brand)
    )
    return list(result)


async def create_draft(session: AsyncSession, telegram_user_id: int, party_id: int) -> Order:
    salesperson = await session.scalar(
        select(Salesperson).where(Salesperson.telegram_user_id == telegram_user_id, Salesperson.active.is_(True))
    )
    if salesperson is None:
        raise OrderPermissionError("This Telegram account is not approved.")
    party_link = await session.scalar(select(SalespersonParty.id).where(
        SalespersonParty.salesperson_id == salesperson.id,
        SalespersonParty.party_id == party_id,
    ))
    party = await session.get(Party, party_id)
    if not party_link or party is None or not party.active:
        raise OrderPermissionError("This party is not assigned to the salesperson.")
    order = Order(
        order_number=f"PG-{datetime.now(timezone.utc):%Y%m%d}-{uuid4().hex[:8].upper()}",
        salesperson_id=salesperson.id,
        party_id=party.id,
        brand="Unselected",
        status="Draft",
        draft_data={"items": []},
    )
    session.add(order)
    await session.flush()
    session.add(OrderAudit(
        order_id=order.id,
        actor=str(telegram_user_id),
        action="draft_created",
        details={"party_code": party.party_code},
    ))
    return order


async def submit_draft(
    session: AsyncSession,
    telegram_user_id: int,
    order_id: int,
    items: list[dict],
    remarks: str | None = None,
) -> Order:
    order = await session.scalar(
        select(Order).where(Order.id == order_id).with_for_update()
    )
    if order is None:
        raise ValueError("The saved draft no longer exists.")
    if order.status in {"Submitted", "Approved", "Exported"}:
        return order
    if order.status != "Draft":
        raise ValueError(f"Order in {order.status} status cannot be submitted.")
    salesperson = await session.scalar(select(Salesperson).where(
        Salesperson.id == order.salesperson_id,
        Salesperson.telegram_user_id == telegram_user_id,
        Salesperson.active.is_(True),
    ))
    if salesperson is None:
        raise OrderPermissionError("This Telegram account is not approved for this draft.")
    party_link = await session.scalar(select(SalespersonParty.id).where(
        SalespersonParty.salesperson_id == salesperson.id,
        SalespersonParty.party_id == order.party_id,
    ))
    if not party_link:
        raise OrderPermissionError("This party is no longer assigned to the salesperson.")
    if not items:
        raise ValueError("An order must contain at least one product.")
    if (order.draft_data or {}).get("items", []) != items:
        raise ValueError("Saved draft items changed; review the cart and submit again.")
    brands: set[str] = set()
    for item in items:
        product = await session.get(Product, item["product_id"])
        if product is None or not product.active or product.product_code != item["product_code"]:
            raise OrderPermissionError("A draft product is no longer active or has changed.")
        brand_link = await session.scalar(select(SalespersonBrand.id).where(
            SalespersonBrand.salesperson_id == salesperson.id,
            SalespersonBrand.brand == product.brand,
        ))
        if not brand_link:
            raise OrderPermissionError(f"Brand {product.brand} is not assigned to the salesperson.")
        quantity = order_quantity(item["quantity"])
        free_quantity = free_quantity_value(item.get("free_quantity", "0"))
        if product.selling_rate is None:
            raise ValueError(f"Selling rate is not configured for product {product.product_code}.")
        rate = rate_value(item.get("rate", product.selling_rate))
        discount = discount_value(item.get("discount_percent", "0"))
        if free_quantity < 0:
            raise ValueError("Free quantity cannot be negative.")
        if free_quantity and not salesperson.allow_free_quantity:
            raise OrderPermissionError("Free quantity is not authorised for this salesperson.")
        if discount and (
            not salesperson.allow_discount or discount > salesperson.max_discount_percent
        ):
            raise OrderPermissionError("The requested discount is not authorised.")
        total = line_total(quantity, rate, discount)
        order.items.append(OrderItem(
            product_id=product.id,
            product_code=product.product_code,
            product_name=product.name,
            brand=product.brand,
            pack_size=product.pack_size,
            quantity=quantity,
            free_quantity=free_quantity,
            rate=rate,
            discount_percent=discount,
            line_total=total,
        ))
        brands.add(product.brand)
    order.brand = next(iter(brands)) if len(brands) == 1 else "Multiple Brands"
    order.draft_data = {"items": []}
    order.status = "Submitted"
    order.remarks = remarks
    order.submitted_at = datetime.now(timezone.utc)
    order.submission_key = str(order.id)
    session.add(OrderAudit(
        order_id=order.id,
        actor=str(telegram_user_id),
        action="submitted",
        details={"order_number": order.order_number, "items": len(order.items)},
    ))
    await session.commit()
    return order
