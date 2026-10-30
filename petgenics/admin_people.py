from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import func, or_, select
from sqlalchemy import String, cast
from telegram.error import TelegramError

from petgenics.admin_ui import clip_text, inline_keyboard, paginate
from petgenics.db import SessionLocal
from petgenics.models import (
    Order,
    Party,
    Product,
    Salesperson,
    SalespersonBrand,
    SalespersonParty,
)


async def show_salespeople(query, context, filter_name: str = "all", page: int = 0) -> None:
    async with SessionLocal() as session:
        statement = select(Salesperson).order_by(Salesperson.active, Salesperson.display_name)
        if filter_name == "pending":
            statement = statement.where(Salesperson.active.is_(False))
        elif filter_name == "active":
            statement = statement.where(Salesperson.active.is_(True))
        people = list((await session.scalars(statement.limit(1000))).all())
        pending_count = await session.scalar(select(func.count()).select_from(Salesperson).where(Salesperson.active.is_(False))) or 0
    items, page, total_pages = paginate(people, page)
    rows = []
    for person in items:
        rows.append([(
            f"{clip_text(person.display_name, 30)} · {'Active' if person.active else 'Needs approval'}",
            f"admin:person:open:{person.id}",
        )])
    rows.extend([
        [("Needs approval", "admin:people:filter:pending"),
         ("Active", "admin:people:filter:active"),
         ("All", "admin:people:filter:all")],
        [("Add salesperson", "admin:people:add")],
    ])
    nav = []
    if page:
        nav.append(("Previous", f"admin:people:page:{filter_name}:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(("Next", f"admin:people:page:{filter_name}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([("Admin menu", "admin:menu")])
    await query.edit_message_text(
        f"Salespersons · {len(people)} total · {pending_count} need approval · "
        f"page {page + 1}/{total_pages}\n" +
        ("\n".join(f"{p.display_name} · ID {p.telegram_user_id} · {'active' if p.active else 'inactive'}" for p in items)
         or "No accounts."),
        reply_markup=inline_keyboard(rows),
    )


async def show_salesperson(query, person_id: int) -> None:
    async with SessionLocal() as session:
        person = await session.get(Salesperson, person_id)
        if person is None:
            await query.edit_message_text("Salesperson not found.")
            return
        brands = list((await session.scalars(
            select(SalespersonBrand.brand).where(SalespersonBrand.salesperson_id == person.id)
            .order_by(SalespersonBrand.brand)
        )).all())
        parties = list((await session.scalars(
            select(Party.party_code).join(SalespersonParty)
            .where(SalespersonParty.salesperson_id == person.id).order_by(Party.party_code)
        )).all())
        order_count = await session.scalar(
            select(func.count()).select_from(Order).where(Order.salesperson_id == person.id)
        ) or 0
    rows = [
        [(("Deactivate" if person.active else "Approve"), f"admin:person:{'deactivate' if person.active else 'approve'}:{person.id}")],
        [("Discount permission", f"admin:people:discount:{person.id}"),
         (("Disable free qty" if person.allow_free_quantity else "Allow free qty"), f"admin:people:free:{person.id}")],
        [("Edit brands", f"admin:people:brands:{person.id}"),
         ("Edit parties", f"admin:people:parties:{person.id}:0")],
        [("Rename", f"admin:people:rename:{person.id}"),
         ("List orders", f"admin:people:orders:{person.id}:0")],
        [("Message", f"admin:people:message:{person.id}"),
         ("Delete", f"admin:people:delete:{person.id}")],
        [("Salespersons", "admin:people")],
    ]
    await query.edit_message_text(
        f"{person.display_name}\nTelegram ID: {person.telegram_user_id}\n"
        f"Status: {'Active' if person.active else 'Needs approval'}\n"
        f"Discount: {'up to ' + str(person.max_discount_percent) + '%' if person.allow_discount else 'off'}\n"
        f"Free quantity: {'allowed' if person.allow_free_quantity else 'not allowed'}\n"
        f"Brands: {', '.join(brands) or 'none'}\nParties: {', '.join(parties[:15]) or 'none'}"
        f"{' …' if len(parties) > 15 else ''}\nOrders: {order_count}",
        reply_markup=inline_keyboard(rows),
    )


async def handle_people_callback(update, context, data: str) -> bool:
    query = update.callback_query
    if data == "admin:people":
        await show_salespeople(query, context)
        return True
    parts = data.split(":")
    if data.startswith("admin:people:filter:"):
        await show_salespeople(query, context, parts[-1])
        return True
    if data.startswith("admin:people:page:") and len(parts) == 5:
        try:
            page = int(parts[4])
        except ValueError:
            page = 0
        await show_salespeople(query, context, parts[3], page)
        return True
    if len(parts) == 4 and parts[:3] == ["admin", "person", "open"]:
        await show_salesperson(query, int(parts[3]))
        return True
    if data == "admin:people:add":
        context.user_data["admin_input"] = {"kind": "person_add"}
        await query.edit_message_text(
            "Send a new salesperson as: TELEGRAM_ID Name\nThe new account will be active, with no brand or party assignments."
        )
        return True
    if parts[:3] == ["admin", "people", "discount"] and len(parts) == 4:
        await _show_discount_options(query, int(parts[3]))
        return True
    if parts[:3] == ["admin", "people", "discount_set"] and len(parts) == 5:
        if parts[4] == "custom":
            context.user_data["admin_input"] = {
                "kind": "discount_custom", "person_id": int(parts[3])
            }
            await query.edit_message_text("Send the maximum discount (0 to 100, max 2 decimal places).")
        else:
            await _set_discount(query, int(parts[3]), parts[4])
        return True
    if parts[:3] == ["admin", "people", "free"] and len(parts) == 4:
        async with SessionLocal() as session:
            person = await session.get(Salesperson, int(parts[3]))
            if person is None:
                await query.edit_message_text("Salesperson not found.")
                return True
            person.allow_free_quantity = not person.allow_free_quantity
            await session.commit()
        await show_salesperson(query, int(parts[3]))
        return True
    if parts[:3] == ["admin", "people", "rename"] and len(parts) == 4:
        context.user_data["admin_input"] = {"kind": "person_rename", "person_id": int(parts[3])}
        await query.edit_message_text("Send the new display name.")
        return True
    if parts[:3] == ["admin", "people", "message"] and len(parts) == 4:
        context.user_data["admin_input"] = {"kind": "person_message", "person_id": int(parts[3])}
        await query.edit_message_text("Type the message to send. Delivery may fail if the user has not started the bot.")
        return True
    if parts[:3] == ["admin", "people", "delete"] and len(parts) == 4 and parts[3] != "confirm":
        await _confirm_delete_person(query, context, int(parts[3]))
        return True
    if data == "admin:people:delete:confirm":
        await _delete_person(query, context)
        return True
    if len(parts) in {4, 5} and parts[:3] == ["admin", "people", "brands"]:
        await _show_person_brands(
            query, context, int(parts[3]), int(parts[4]) if len(parts) == 5 else 0
        )
        return True
    if parts[:3] == ["admin", "people", "brand_toggle"] and len(parts) == 5:
        await _toggle_person_brand(query, context, int(parts[3]), int(parts[4]))
        return True
    if parts[:3] == ["admin", "people", "brand_all"] and len(parts) == 4:
        await _assign_all_brands(query, context, int(parts[3]))
        return True
    if parts[:4] == ["admin", "people", "brand_clear", "apply"] and len(parts) == 5:
        person_id = int(parts[4])
        pending = context.user_data.pop("admin_people_confirm", {})
        if pending.get("kind") != "brand_clear" or pending.get("person_id") != person_id:
            await query.edit_message_text("That action expired.")
            return True
        async with SessionLocal() as session:
            await session.execute(SalespersonBrand.__table__.delete().where(
                SalespersonBrand.salesperson_id == person_id
            ))
            await session.commit()
        await _show_person_brands(query, context, person_id)
        return True
    if parts[:3] == ["admin", "people", "brand_clear"] and len(parts) == 5 and parts[4] == "confirm":
        person_id = int(parts[3])
        context.user_data["admin_people_confirm"] = {"kind": "brand_clear", "person_id": person_id}
        await query.edit_message_text(
            "Remove every brand assignment for this salesperson?",
            reply_markup=inline_keyboard([[
                ("Confirm", f"admin:people:brand_clear:apply:{person_id}"),
                ("Cancel", f"admin:person:open:{person_id}"),
            ]]),
        )
        return True
    if parts[:3] == ["admin", "people", "parties"] and len(parts) == 5:
        await _show_person_parties(query, context, int(parts[3]), int(parts[4]))
        return True
    if parts[:3] == ["admin", "people", "party_toggle"] and len(parts) == 5:
        await _toggle_person_party(query, int(parts[3]), int(parts[4]))
        return True
    if data.startswith("admin:people:party_page:"):
        await _show_person_parties(query, context, int(parts[3]), int(parts[4]))
        return True
    if data.startswith("admin:people:party_add_search:"):
        person_id = int(parts[3])
        context.user_data["admin_input"] = {"kind": "person_party_search", "person_id": person_id}
        await query.edit_message_text("Type a party name or code to add.")
        return True
    if parts[:4] == ["admin", "people", "party_add_all", "confirm"] and len(parts) == 5:
        context.user_data["admin_people_confirm"] = {
            "kind": "party_add_all", "person_id": int(parts[4])
        }
        await query.edit_message_text(
            "Assign every active party to this salesperson?",
            reply_markup=inline_keyboard([[
                ("Confirm", f"admin:people:party_add_all:apply:{parts[4]}"),
                ("Cancel", f"admin:person:open:{parts[4]}"),
            ]]),
        )
        return True
    if parts[:4] == ["admin", "people", "party_add_all", "apply"] and len(parts) == 5:
        pending = context.user_data.pop("admin_people_confirm", {})
        person_id = int(parts[4])
        if pending.get("person_id") != person_id:
            await query.edit_message_text("That action expired.")
            return True
        await _assign_all_parties(query, context, person_id)
        return True
    if parts[:3] == ["admin", "people", "party_clear"] and len(parts) == 4:
        context.user_data["admin_people_confirm"] = {"kind": "party_clear", "person_id": int(parts[3])}
        await query.edit_message_text(
            "Remove all party assignments for this salesperson?",
            reply_markup=inline_keyboard([[
                ("Confirm", f"admin:people:party_clear:apply:{parts[3]}"),
                ("Cancel", f"admin:person:open:{parts[3]}"),
            ]]),
        )
        return True
    if parts[:4] == ["admin", "people", "party_clear", "apply"] and len(parts) == 5:
        person_id = int(parts[4])
        pending = context.user_data.pop("admin_people_confirm", {})
        if pending.get("kind") != "party_clear" or pending.get("person_id") != person_id:
            await query.edit_message_text("That action expired.")
            return True
        await _clear_person_parties(query, person_id)
        return True
    if parts[:3] == ["admin", "people", "orders"] and len(parts) == 5:
        await _show_person_orders(query, int(parts[3]), int(parts[4]))
        return True
    if data == "admin:people:broadcast":
        await show_broadcast_menu(query)
        return True
    if data == "admin:people:broadcast:all":
        context.user_data["admin_input"] = {"kind": "broadcast_all"}
        await query.edit_message_text("Type the message for all active salespeople.")
        return True
    if data.startswith("admin:people:broadcast:person:"):
        context.user_data["admin_input"] = {"kind": "broadcast_person_search"}
        await query.edit_message_text("Type the salesperson name or Telegram ID.")
        return True
    if parts[:3] == ["admin", "broadcast", "target"] and len(parts) == 4:
        try:
            person_id = int(parts[3])
        except ValueError:
            await query.edit_message_text("Invalid salesperson.")
            return True
        context.user_data["admin_input"] = {
            "kind": "broadcast_person", "person_id": person_id
        }
        await query.edit_message_text("Type the message to send to this salesperson.")
        return True
    if data == "admin:broadcast:confirm":
        await _send_broadcast(query, context)
        return True
    return False


async def _show_discount_options(query, person_id: int) -> None:
    values = ["off", "2", "5", "10", "15", "20", "25", "custom"]
    buttons = [
        (("Off" if value == "off" else "Custom" if value == "custom" else f"{value}% max"),
         f"admin:people:discount_set:{person_id}:{value}")
        for value in values
    ]
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    rows.append([("Back", f"admin:person:open:{person_id}")])
    await query.edit_message_text("Choose the maximum allowed discount:", reply_markup=inline_keyboard(rows))


async def _set_discount(query, person_id: int, value: str) -> None:
    if value not in {"off", "2", "5", "10", "15", "20", "25"}:
        await query.edit_message_text("Unknown discount preset.")
        return
    amount = Decimal("0") if value == "off" else Decimal(value)
    async with SessionLocal() as session:
        person = await session.get(Salesperson, person_id)
        if person is None:
            await query.edit_message_text("Salesperson not found.")
            return
        person.allow_discount = value != "off"
        person.max_discount_percent = amount
        await session.commit()
    await show_salesperson(query, person_id)


async def _show_person_brands(query, context, person_id: int, page: int = 0) -> None:
    async with SessionLocal() as session:
        all_brands = sorted(set((await session.scalars(
            select(Product.brand).distinct()
        )).all()) | set((await session.scalars(
            select(SalespersonBrand.brand).distinct()
        )).all()))
        selected = set((await session.scalars(
            select(SalespersonBrand.brand).where(SalespersonBrand.salesperson_id == person_id)
        )).all())
    context.user_data["admin_brand_names"] = all_brands
    items, page, total_pages = paginate(all_brands, page)
    start = page * 8
    rows = [[
        (("✓ " if brand in selected else "") + clip_text(brand, 35),
         f"admin:people:brand_toggle:{person_id}:{start + index}")
    ] for index, brand in enumerate(items)]
    if page:
        rows.append([("Previous", f"admin:people:brands:{person_id}:{page - 1}")])
    if page + 1 < total_pages:
        rows.append([("Next", f"admin:people:brands:{person_id}:{page + 1}")])
    rows.append([("Assign all", f"admin:people:brand_all:{person_id}"),
                 ("Clear all", f"admin:people:brand_clear:{person_id}:confirm")])
    rows.append([("Back to profile", f"admin:person:open:{person_id}")])
    await query.edit_message_text(
        f"Assigned brands for salesperson {person_id}:\n{', '.join(sorted(selected)) or 'none'}",
        reply_markup=inline_keyboard(rows),
    )


async def _toggle_person_brand(query, context, person_id: int, index: int) -> None:
    try:
        brand = context.user_data["admin_brand_names"][index]
    except (KeyError, IndexError):
        await query.edit_message_text("Brand list expired. Reopen brand assignments.")
        return
    async with SessionLocal() as session:
        existing = await session.scalar(select(SalespersonBrand).where(
            SalespersonBrand.salesperson_id == person_id, SalespersonBrand.brand == brand
        ))
        if existing:
            await session.delete(existing)
        elif await session.get(Salesperson, person_id):
            session.add(SalespersonBrand(salesperson_id=person_id, brand=brand))
        await session.commit()
    await _show_person_brands(query, context, person_id, index // 8)


async def _assign_all_brands(query, context, person_id: int) -> None:
    async with SessionLocal() as session:
        brands = set((await session.scalars(select(Product.brand).where(Product.active.is_(True)).distinct())).all())
        assigned = set((await session.scalars(select(SalespersonBrand.brand).where(
            SalespersonBrand.salesperson_id == person_id
        ))).all())
        for brand in brands - assigned:
            session.add(SalespersonBrand(salesperson_id=person_id, brand=brand))
        await session.commit()
    await _show_person_brands(query, context, person_id)


async def _show_person_parties(query, context, person_id: int, page: int) -> None:
    async with SessionLocal() as session:
        parties = list((await session.scalars(select(Party).where(Party.active.is_(True)).order_by(Party.name))).all())
        selected = set((await session.scalars(select(SalespersonParty.party_id).where(
            SalespersonParty.salesperson_id == person_id
        ))).all())
    items, page, total = paginate(parties, page)
    context.user_data["admin_party_choices"] = [party.id for party in items]
    rows = [[
        (("✓ " if party.id in selected else "") + f"{clip_text(party.name, 32)} · {party.party_code}",
         f"admin:people:party_toggle:{person_id}:{party.id}")
    ] for party in items]
    rows.append([("Search and add", f"admin:people:party_add_search:{person_id}")])
    rows.append([("Add all", f"admin:people:party_add_all:confirm:{person_id}")])
    rows.append([("Clear assignments", f"admin:people:party_clear:{person_id}")])
    nav = []
    if page:
        nav.append(("Previous", f"admin:people:party_page:{person_id}:{page - 1}"))
    if page + 1 < total:
        nav.append(("Next", f"admin:people:party_page:{person_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([("Back to profile", f"admin:person:open:{person_id}")])
    await query.edit_message_text(
        f"Parties · page {page + 1}/{total}. Check means assigned.",
        reply_markup=inline_keyboard(rows),
    )


async def _toggle_person_party(query, person_id: int, party_id: int) -> None:
    async with SessionLocal() as session:
        existing = await session.scalar(select(SalespersonParty).where(
            SalespersonParty.salesperson_id == person_id, SalespersonParty.party_id == party_id
        ))
        if existing:
            await session.delete(existing)
        else:
            session.add(SalespersonParty(salesperson_id=person_id, party_id=party_id))
        await session.commit()
    await _show_person_parties(query, {}, person_id, 0)


async def _assign_all_parties(query, context, person_id: int) -> None:
    async with SessionLocal() as session:
        party_ids = set((await session.scalars(select(Party.id).where(Party.active.is_(True)))).all())
        assigned = set((await session.scalars(select(SalespersonParty.party_id).where(
            SalespersonParty.salesperson_id == person_id
        ))).all())
        for party_id in party_ids - assigned:
            session.add(SalespersonParty(salesperson_id=person_id, party_id=party_id))
        await session.commit()
    await _show_person_parties(query, context, person_id, 0)


async def _clear_person_parties(query, person_id: int) -> None:
    async with SessionLocal() as session:
        await session.execute(SalespersonParty.__table__.delete().where(
            SalespersonParty.salesperson_id == person_id
        ))
        await session.commit()
    await query.edit_message_text("All party assignments cleared.")


async def _show_person_orders(query, person_id: int, page: int) -> None:
    async with SessionLocal() as session:
        orders = list((await session.scalars(
            select(Order).where(Order.salesperson_id == person_id)
            .order_by(Order.created_at.desc()).limit(500)
        )).all())
    items, page, total = paginate(orders, page)
    rows = [[(f"{order.order_number} · {order.status}", f"admin:detail:{order.id}")]
            for order in items]
    if page:
        rows.append([("Previous", f"admin:people:orders:{person_id}:{page - 1}")])
    if page + 1 < total:
        rows.append([("Next", f"admin:people:orders:{person_id}:{page + 1}")])
    rows.append([("Back to profile", f"admin:person:open:{person_id}")])
    await query.edit_message_text(
        f"Orders · page {page + 1}/{total}\n" +
        ("\n".join(f"{o.order_number} · {o.status}" for o in items) or "No orders."),
        reply_markup=inline_keyboard(rows),
    )


async def _confirm_delete_person(query, context, person_id: int) -> None:
    async with SessionLocal() as session:
        person = await session.get(Salesperson, person_id)
        orders = await session.scalar(
            select(func.count()).select_from(Order).where(Order.salesperson_id == person_id)
        ) or 0
    if person is None:
        await query.edit_message_text("Salesperson not found.")
    elif orders:
        await query.edit_message_text("This salesperson has orders. Deactivate the account instead.")
    else:
        context.user_data["admin_people_confirm"] = {"kind": "delete_person", "person_id": person_id}
        await query.edit_message_text(
            f"Delete {person.display_name}?",
            reply_markup=inline_keyboard([[
                ("Confirm delete", "admin:people:delete:confirm"),
                ("Cancel", f"admin:person:open:{person_id}"),
            ]]),
        )


async def _delete_person(query, context) -> None:
    pending = context.user_data.pop("admin_people_confirm", {})
    person_id = pending.get("person_id") if pending.get("kind") == "delete_person" else None
    if not person_id:
        await query.edit_message_text("That confirmation expired.")
        return
    async with SessionLocal() as session:
        person = await session.get(Salesperson, person_id)
        if person is None:
            await query.edit_message_text("Salesperson not found.")
            return
        order_count = await session.scalar(
            select(func.count()).select_from(Order).where(Order.salesperson_id == person_id)
        ) or 0
        if order_count:
            await query.edit_message_text("This salesperson now has orders. Deactivate instead.")
            return
        await session.execute(SalespersonBrand.__table__.delete().where(
            SalespersonBrand.salesperson_id == person_id
        ))
        await session.execute(SalespersonParty.__table__.delete().where(
            SalespersonParty.salesperson_id == person_id
        ))
        await session.delete(person)
        await session.commit()
    await query.edit_message_text("Salesperson deleted.")


async def show_broadcast_menu(query) -> None:
    await query.edit_message_text(
        "Broadcast:",
        reply_markup=inline_keyboard([
            [("All active salespeople", "admin:people:broadcast:all")],
            [("One salesperson", "admin:people:broadcast:person")],
            [("Admin menu", "admin:menu")],
        ]),
    )


async def handle_people_text(update, context, value: str) -> bool:
    pending = context.user_data.get("admin_input")
    if not pending or pending.get("kind") not in {
        "person_add", "person_rename", "person_message", "person_party_search",
        "broadcast_all", "broadcast_person_search", "broadcast_person", "discount_custom",
    }:
        return False
    kind = pending["kind"]
    if kind in {"broadcast_all", "broadcast_person"} and len(value) > 4096:
        await update.effective_message.reply_text(
            "Telegram messages can be at most 4,096 characters. Shorten the message and try again."
        )
        return True
    if kind == "person_add":
        parts = value.strip().split(maxsplit=1)
        if len(parts) != 2 or not parts[1] or len(parts[1]) > 200:
            await update.effective_message.reply_text("Use: TELEGRAM_ID Name (name <=200 characters).")
            return True
        try:
            telegram_id = int(parts[0])
            if telegram_id <= 0:
                raise ValueError
        except ValueError:
            await update.effective_message.reply_text("Telegram ID must be a positive number.")
            return True
        async with SessionLocal() as session:
            if await session.scalar(select(Salesperson.id).where(
                Salesperson.telegram_user_id == telegram_id
            )) is not None:
                await update.effective_message.reply_text("That Telegram ID already exists.")
                return True
            session.add(Salesperson(
                telegram_user_id=telegram_id,
                display_name=parts[1],
                active=True,
            ))
            await session.commit()
        context.user_data.pop("admin_input", None)
        await update.effective_message.reply_text("Salesperson added as active. Assign authorized brands and parties before orders.")
        return True
    if kind == "person_rename":
        name = value.strip()
        if not name or len(name) > 200:
            await update.effective_message.reply_text("Name must contain 1 to 200 characters.")
            return True
        async with SessionLocal() as session:
            person = await session.get(Salesperson, pending["person_id"])
            if person is None:
                await update.effective_message.reply_text("Salesperson not found.")
                context.user_data.pop("admin_input", None)
                return True
            person.display_name = name
            await session.commit()
        context.user_data.pop("admin_input", None)
        await update.effective_message.reply_text("Salesperson renamed.")
        return True
    if kind == "person_message":
        async with SessionLocal() as session:
            person = await session.get(Salesperson, pending["person_id"])
            telegram_id = person.telegram_user_id if person else None
        context.user_data.pop("admin_input", None)
        if telegram_id is None:
            await update.effective_message.reply_text("Salesperson not found.")
            return True
        try:
            await context.bot.send_message(telegram_id, value)
            result = "Message delivered."
        except TelegramError:
            result = "Could not deliver the message; the user may have blocked the bot or not started it."
        await update.effective_message.reply_text(result)
        return True
    if kind == "person_party_search":
        matches = await _find_parties(value)
        if not matches:
            await update.effective_message.reply_text("No parties matched.")
            return True
        context.user_data["admin_party_search_ids"] = [party.id for party in matches]
        rows = [[
            (f"{clip_text(party.name, 40)} · {party.party_code}",
             f"admin:people:party_toggle:{pending['person_id']}:{party.id}")
        ] for party in matches]
        await update.effective_message.reply_text(
            "Select a party to assign:",
            reply_markup=inline_keyboard(rows),
        )
        context.user_data.pop("admin_input", None)
        return True
    if kind in {"broadcast_all", "broadcast_person_search"}:
        if kind == "broadcast_person_search":
            async with SessionLocal() as session:
                people = list((await session.scalars(select(Salesperson).where(
                    or_(
                        Salesperson.display_name.ilike(f"%{value}%"),
                        cast(Salesperson.telegram_user_id, String).ilike(f"%{value}%"),
                    )
                ).limit(8))).all())
            if not people:
                await update.effective_message.reply_text("No salesperson found.")
                return True
            rows = [[
                (f"{person.display_name} · {person.telegram_user_id}",
                 f"admin:broadcast:target:{person.id}")
            ] for person in people]
            context.user_data["admin_broadcast_target_ids"] = [person.id for person in people]
            await update.effective_message.reply_text("Choose a recipient:", reply_markup=inline_keyboard(rows))
            context.user_data.pop("admin_input", None)
            return True
        context.user_data["admin_broadcast"] = {"target": "all", "text": value}
        await update.effective_message.reply_text(
            "Preview:\n" + clip_text(value, 500),
            reply_markup=inline_keyboard([[("Send", "admin:broadcast:confirm"), ("Cancel", "admin:people")]]),
        )
        context.user_data.pop("admin_input", None)
        return True
    if kind == "broadcast_person":
        async with SessionLocal() as session:
            person = await session.get(Salesperson, pending["person_id"])
            telegram_id = person.telegram_user_id if person else None
        context.user_data.pop("admin_input", None)
        if telegram_id is None:
            await update.effective_message.reply_text("Salesperson not found.")
            return True
        context.user_data["admin_broadcast"] = {"target": str(telegram_id), "text": value}
        await update.effective_message.reply_text(
            f"Preview for {telegram_id}:\n{clip_text(value, 500)}",
            reply_markup=inline_keyboard([[("Send", "admin:broadcast:confirm"), ("Cancel", "admin:people")]]),
        )
        return True
    if kind == "discount_custom":
        try:
            value_decimal = Decimal(value)
            if not value_decimal.is_finite() or not Decimal("0") <= value_decimal <= Decimal("100") or value_decimal.as_tuple().exponent < -2:
                raise ValueError
        except (InvalidOperation, ValueError):
            await update.effective_message.reply_text("Enter a discount from 0 to 100 with at most 2 decimal places.")
            return True
        async with SessionLocal() as session:
            person = await session.get(Salesperson, pending["person_id"])
            if person is None:
                await update.effective_message.reply_text("Salesperson not found.")
                return True
            person.allow_discount = value_decimal > 0
            person.max_discount_percent = value_decimal
            await session.commit()
        context.user_data.pop("admin_input", None)
        await update.effective_message.reply_text("Discount permission updated.")
        return True
    return False


async def _find_parties(text: str) -> list[Party]:
    async with SessionLocal() as session:
        return list((await session.scalars(
            select(Party).where(
                Party.active.is_(True),
                or_(Party.name.ilike(f"%{text}%"), Party.party_code.ilike(f"%{text}%")),
            ).order_by(Party.name).limit(8)
        )).all())


async def _send_broadcast(query, context) -> None:
    from telegram.error import TelegramError
    import asyncio

    pending = context.user_data.pop("admin_broadcast", None)
    if not pending or not pending.get("text") or not pending.get("target"):
        await query.edit_message_text("Broadcast preview expired.")
        return
    async with SessionLocal() as session:
        if pending["target"] == "all":
            recipients = list((await session.scalars(
                select(Salesperson.telegram_user_id).where(Salesperson.active.is_(True))
            )).all())
        else:
            recipients = [int(pending["target"])]
    delivered = failed = 0
    for telegram_id in recipients:
        try:
            await context.bot.send_message(chat_id=telegram_id, text=pending["text"])
            delivered += 1
        except TelegramError:
            failed += 1
        await asyncio.sleep(0.05)
    await query.edit_message_text(f"Broadcast complete. Delivered: {delivered} · Failed: {failed}")
