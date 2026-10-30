from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload
from petgenics.admin_ui import clip_text, inline_keyboard
from petgenics.db import SessionLocal
from petgenics.models import Order, OrderAudit, Party, Salesperson

ORDER_STATUSES = {
    "submitted": ("Pending approval", "Submitted"),
    "approved": ("Approved", "Approved"),
    "rejected": ("Rejected", "Rejected"),
    "exported": ("Exported", "Exported"),
    "draft": ("Drafts", "Draft"),
    "all": ("All orders", None),
}


async def show_order_filters(query) -> None:
    await query.edit_message_text(
        "Orders: choose a status or search by order, party, salesperson, or brand.",
        reply_markup=inline_keyboard([
            [("Pending approval", "admin:orders:filter:submitted"),
             ("Approved", "admin:orders:filter:approved")],
            [("Rejected", "admin:orders:filter:rejected"),
             ("Exported", "admin:orders:filter:exported")],
            [("Drafts", "admin:orders:filter:draft"), ("All", "admin:orders:filter:all")],
            [("Search orders", "admin:orders:search"),
             ("Approve all pending", "admin:orders:bulk:confirm")],
            [("Admin menu", "admin:menu")],
        ]),
    )


async def show_order_page(query, context, status_key: str, page: int = 0, search: str | None = None) -> None:
    if status_key not in ORDER_STATUSES:
        await query.edit_message_text("Unknown order status.")
        return
    _, status = ORDER_STATUSES[status_key]
    async with SessionLocal() as session:
        statement = select(Order).options(selectinload(Order.items))
        if status:
            statement = statement.where(Order.status == status)
        if search:
            statement = (
                statement.join(Party, Party.id == Order.party_id)
                .join(Salesperson, Salesperson.id == Order.salesperson_id)
                .where(or_(
                    Order.order_number.ilike(f"%{search}%"),
                    Party.name.ilike(f"%{search}%"),
                    Party.party_code.ilike(f"%{search}%"),
                    Salesperson.display_name.ilike(f"%{search}%"),
                    Order.brand.ilike(f"%{search}%"),
                ))
            )
        total_orders = await session.scalar(
            select(func.count()).select_from(statement.order_by(None).subquery())
        ) or 0
        total_pages = max(1, (total_orders + 7) // 8)
        page = min(max(page, 0), total_pages - 1)
        page_orders = list((await session.scalars(
            statement.order_by(Order.created_at.desc()).limit(8).offset(page * 8)
        )).all())
        summaries = []
        for order in page_orders:
            party = await session.get(Party, order.party_id)
            summaries.append(
                f"{order.order_number} · {order.status} · "
                f"{clip_text(party.name if party else 'Unknown party', 50)} · "
                f"{len(order.items)} item(s)"
            )
    context.user_data["admin_order_list"] = [order.id for order in page_orders]
    context.user_data["admin_order_filter"] = status_key
    context.user_data["admin_order_search"] = search
    rows = [
        [(summary, f"admin:detail:{order.id}")]
        for summary, order in zip(summaries, page_orders)
    ]
    nav = []
    if page:
        nav.append(("Previous", f"admin:orders:page:{status_key}:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(("Next", f"admin:orders:page:{status_key}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([("Filters", "admin:orders"), ("Admin menu", "admin:menu")])
    heading = ORDER_STATUSES[status_key][0]
    if search:
        heading += f" matching {clip_text(search, 80)}"
    body = f"{heading} · page {page + 1}/{total_pages}\n\n" + (
        "\n".join(summaries) if summaries else "No orders found."
    )
    await query.edit_message_text(body[:3900], reply_markup=inline_keyboard(rows))


async def show_order_detail(query, order_id: int) -> None:
    async with SessionLocal() as session:
        order = await session.scalar(
            select(Order).where(Order.id == order_id).options(selectinload(Order.items))
        )
        if order is None:
            await query.edit_message_text("Order not found.")
            return
        party = await session.get(Party, order.party_id)
        person = await session.get(Salesperson, order.salesperson_id)
        audit = list((await session.scalars(
            select(OrderAudit).where(OrderAudit.order_id == order.id)
            .order_by(OrderAudit.created_at.desc()).limit(10)
        )).all())
        total = sum((item.line_total for item in order.items), Decimal("0"))
        lines = [
            f"Order {order.order_number} · {order.status}",
            f"Date: {order.created_at.isoformat()}",
            f"Salesperson: {person.display_name if person else 'Unknown'}",
            f"Party: {party.party_code + ' · ' + party.name if party else 'Unknown'}",
            f"Brand(s): {order.brand}",
            "",
        ]
        detail_items = order.items
        if order.status == "Draft":
            draft_items = (order.draft_data or {}).get("items", [])
            lines.append("Unsubmitted draft items:")
            for item in draft_items:
                quantity = Decimal(str(item.get("quantity", "0")))
                rate = Decimal(str(item.get("rate", "0")))
                discount = Decimal(str(item.get("discount_percent", "0")))
                line_total = quantity * rate * (Decimal("1") - discount / Decimal("100"))
                total += line_total
                lines.append(
                    f"{item.get('product_code', '')} · {clip_text(item.get('product_name', ''), 80)} · "
                    f"Qty {item.get('quantity', '0')} + {item.get('free_quantity', '0')} free · "
                    f"Rate {item.get('rate', '0')} · Discount {item.get('discount_percent', '0')}% · "
                    f"Total {line_total:.2f}"
                )
        for item in detail_items:
            lines.append(
                f"{item.product_code} · {clip_text(item.product_name, 80)} · {item.pack_size or '-'}\n"
                f"Qty {item.quantity} + {item.free_quantity} free · Rate {item.rate} · "
                f"Discount {item.discount_percent}% · Total {item.line_total}"
            )
        lines.extend(["", f"Order total: {total:.2f}", f"Remarks: {clip_text(order.remarks or '-', 250)}"])
        if audit:
            lines.extend(["", "Recent history:"])
            lines.extend(
                f"{event.created_at.isoformat()} · {event.action} · {clip_text(event.actor, 40)}"
                for event in audit
            )
        buttons = []
        if order.status == "Submitted":
            buttons.extend([
                ("Approve", f"admin:order:approve:{order.id}"),
                ("Reject / add reason", f"admin:order:reject:{order.id}"),
            ])
        elif order.status in {"Approved", "Rejected"}:
            buttons.append(("Move back to pending", f"admin:order:pending:{order.id}"))
        buttons.extend([
            ("PDF", f"admin:order:pdf:{order.id}"),
            ("Internal Excel", f"admin:order:xlsx:{order.id}"),
        ])
        if order.status in {"Approved", "Exported"}:
            buttons.append(("MARG Excel", f"admin:order:marg:{order.id}"))
        buttons.append(("Back to orders", "admin:orders"))
    await query.edit_message_text(
        "\n".join(lines)[:3900],
        reply_markup=inline_keyboard([buttons[index:index + 2] for index in range(0, len(buttons), 2)]),
    )


async def handle_order_navigation(update, context, data: str) -> bool:
    query = update.callback_query
    if data == "admin:orders":
        await show_order_filters(query)
        return True
    if data.startswith("admin:detail:"):
        try:
            order_id = int(data.rsplit(":", 1)[1])
        except ValueError:
            await query.edit_message_text("Invalid order.")
            return True
        await show_order_detail(query, order_id)
        return True
    if data.startswith("admin:orders:filter:"):
        status_key = data.rsplit(":", 1)[1]
        await show_order_page(query, context, status_key)
        return True
    if data.startswith("admin:orders:page:"):
        try:
            _, _, _, status_key, page_text = data.split(":")
            page = int(page_text)
        except ValueError:
            await query.edit_message_text("That order page is no longer available.")
            return True
        await show_order_page(
            query, context, status_key, page, context.user_data.get("admin_order_search")
        )
        return True
    if data == "admin:orders:search":
        context.user_data["admin_input"] = {"kind": "orders_search"}
        await query.edit_message_text(
            "Type an order number, party name/code, salesperson, or brand. "
            "Use /cancel to stop searching."
        )
        return True
    return False


async def notify_admins_new_order(bot, order_id: int, admin_ids) -> None:
    from telegram.error import TelegramError

    async with SessionLocal() as session:
        order = await session.scalar(
            select(Order).where(Order.id == order_id).options(selectinload(Order.items))
        )
        if order is None or order.status != "Submitted":
            return
        party = await session.get(Party, order.party_id)
        person = await session.get(Salesperson, order.salesperson_id)
        text = (
            f"New order submitted: {order.order_number}\n"
            f"Salesperson: {person.display_name if person else 'Unknown'}\n"
            f"Party: {party.name if party else 'Unknown'}\n"
            f"Items: {len(order.items)} · Brand: {order.brand}"
        )
        markup = inline_keyboard([[
            ("View", f"admin:detail:{order.id}"),
            ("Approve", f"admin:order:approve:{order.id}"),
            ("Reject", f"admin:order:reject:{order.id}"),
        ]])
    for admin_id in admin_ids:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=text,
                reply_markup=markup,
            )
        except TelegramError:
            continue
