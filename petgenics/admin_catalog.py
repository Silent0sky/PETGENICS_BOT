from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import func, or_, select, update
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


async def show_catalog(query, context, kind: str, page: int = 0, search: str | None = None) -> None:
    if search is not None:
        context.user_data[f"admin_{kind}_search"] = search
    elif page:
        search = context.user_data.get(f"admin_{kind}_search")
    else:
        context.user_data.pop(f"admin_{kind}_search", None)
    if kind == "products":
        async with SessionLocal() as session:
            statement = select(Product).order_by(Product.brand, Product.name)
            if search:
                statement = statement.where(or_(
                    Product.product_code.ilike(f"%{search}%"),
                    Product.name.ilike(f"%{search}%"),
                    Product.family.ilike(f"%{search}%"),
                ))
            products = list((await session.scalars(statement.limit(1000))).all())
        items, page, total_pages = paginate(products, page)
        context.user_data["admin_product_list"] = [product.id for product in items]
        rows = [[
            (f"{clip_text(product.product_code, 25)} · {clip_text(product.name, 32)}",
             f"admin:product:open:{product.id}")
        ] for product in items]
        rows.append([("Search", "admin:products:search"), ("Add product", "admin:products:add")])
        rows.append([("Low/out stock", "admin:products:filter:stock:0"),
                     ("No rate", "admin:products:filter:norate:0")])
        rows.append([("Inactive products", "admin:products:filter:inactive:0")])
        if page:
            rows.append([("Previous", f"admin:products:page:{page - 1}")])
        if page + 1 < total_pages:
            rows.append([("Next", f"admin:products:page:{page + 1}")])
        rows.append([("Menu", "admin:menu")])
        await query.edit_message_text(
            f"Products · page {page + 1}/{total_pages}\n" +
            ("\n".join(f"{p.product_code} · {p.name} · {p.brand}" for p in items) or "No matches."),
            reply_markup=inline_keyboard(rows),
        )
        return
    if kind == "parties":
        async with SessionLocal() as session:
            statement = select(Party).order_by(Party.name)
            if search:
                statement = statement.where(or_(
                    Party.party_code.ilike(f"%{search}%"), Party.name.ilike(f"%{search}%")
                ))
            parties = list((await session.scalars(statement.limit(1000))).all())
        items, page, total_pages = paginate(parties, page)
        context.user_data["admin_party_list"] = [party.id for party in items]
        rows = [[
            (f"{clip_text(party.party_code, 22)} · {clip_text(party.name, 38)}",
             f"admin:party:open:{party.id}")
        ] for party in items]
        rows.append([("Search", "admin:parties:search"), ("Add party", "admin:parties:add")])
        rows.append([("Inactive", "admin:parties:filter:inactive:0"),
                     ("Unassigned", "admin:parties:filter:unassigned:0")])
        if page:
            rows.append([("Previous", f"admin:parties:page:{page - 1}")])
        if page + 1 < total_pages:
            rows.append([("Next", f"admin:parties:page:{page + 1}")])
        rows.append([("Menu", "admin:menu")])
        await query.edit_message_text(
            f"Parties · page {page + 1}/{total_pages}\n" +
            ("\n".join(f"{p.party_code} · {p.name}" for p in items) or "No matches."),
            reply_markup=inline_keyboard(rows),
        )
        return
    if kind == "brands":
        await show_brands(query, context, page)


async def show_brands(query, context, page: int = 0) -> None:
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(Product.brand, func.count(Product.id))
            .group_by(Product.brand).order_by(Product.brand)
        )).all())
        brand_names = list((await session.scalars(
            select(Product.brand).distinct().order_by(Product.brand)
        )).all())
        assigned_names = list((await session.scalars(
            select(SalespersonBrand.brand).distinct().order_by(SalespersonBrand.brand)
        )).all())
    cached = context.user_data.get("admin_brand_names")
    all_names = sorted(set(brand_names) | set(assigned_names))
    if cached != all_names:
        context.user_data["admin_brand_names"] = all_names
    counts = dict(rows)
    items, page, total_pages = paginate(all_names, page)
    buttons = [[
        (f"{clip_text(name, 40)} · {counts.get(name, 0)}", f"admin:brand:open:{all_names.index(name)}")
    ] for name in items]
    if page:
        buttons.append([("Previous", f"admin:brands:page:{page - 1}")])
    if page + 1 < total_pages:
        buttons.append([("Next", f"admin:brands:page:{page + 1}")])
    buttons.append([("Admin menu", "admin:menu")])
    await query.edit_message_text(
        f"Brands · page {page + 1}/{total_pages}\n" +
        ("\n".join(f"{name} · {counts.get(name, 0)} product(s)" for name in items) or "No brands."),
        reply_markup=inline_keyboard(buttons),
    )


async def handle_catalog_callback(update, context, data: str) -> bool:
    query = update.callback_query
    if data == "admin:products":
        await show_catalog(query, context, "products")
        return True
    if data == "admin:parties":
        await show_catalog(query, context, "parties")
        return True
    if data == "admin:brands":
        await show_catalog(query, context, "brands")
        return True
    if data.startswith("admin:products:"):
        action, _, value = data[len("admin:products:"):].partition(":")
        if action == "search":
            context.user_data["admin_input"] = {"kind": "catalog_search", "catalog": "products"}
            await query.edit_message_text("Type part of a product code, name, or family.")
        elif action == "add":
            context.user_data["admin_input"] = {"kind": "product_add"}
            await query.edit_message_text("Send: CODE | Name | Brand | Pack | MRP | Rate | Stock")
        elif action == "page":
            await show_catalog(
                query, context, "products", int(value or 0),
                context.user_data.get("admin_products_search"),
            )
        elif action == "filter":
            filter_name, _, page = value.partition(":")
            await _show_product_filter(query, context, filter_name, int(page or 0))
        else:
            return False
        return True
    if data.startswith("admin:parties:"):
        action, _, value = data[len("admin:parties:"):].partition(":")
        if action == "search":
            context.user_data["admin_input"] = {"kind": "catalog_search", "catalog": "parties"}
            await query.edit_message_text("Type part of a party name or stable code.")
        elif action == "add":
            context.user_data["admin_input"] = {"kind": "party_add"}
            await query.edit_message_text("Send a party as: CODE | Name")
        elif action == "page":
            await show_catalog(
                query, context, "parties", int(value or 0),
                context.user_data.get("admin_parties_search"),
            )
        elif action == "filter":
            filter_name, _, page = value.partition(":")
            await _show_party_filter(query, context, filter_name, int(page or 0))
        else:
            return False
        return True
    if data.startswith("admin:brands:page:"):
        try:
            page = int(data.rsplit(":", 1)[1])
        except ValueError:
            page = 0
        await show_catalog(query, context, "brands", page)
        return True
    if data.startswith("admin:product:"):
        return await _product_callback(query, context, data)
    if data.startswith("admin:party:"):
        return await _party_callback(query, context, data)
    if data.startswith("admin:brand:"):
        return await _brand_callback(query, context, data)
    return False


async def _show_product_filter(query, context, filter_name: str, page: int = 0) -> None:
    async with SessionLocal() as session:
        statement = select(Product).order_by(Product.brand, Product.name)
        if filter_name == "stock":
            statement = statement.where(Product.stock <= Decimal("10"))
        elif filter_name == "norate":
            statement = statement.where(Product.selling_rate.is_(None))
        elif filter_name == "inactive":
            statement = statement.where(Product.active.is_(False))
        else:
            await query.edit_message_text("Unknown product filter.")
            return
        products = list((await session.scalars(statement.limit(500))).all())
    items, page, total_pages = paginate(products, page)
    context.user_data["admin_product_list"] = [product.id for product in items]
    rows = [[
        (f"{clip_text(product.product_code, 24)} · {clip_text(product.name, 35)}",
         f"admin:product:open:{product.id}")
    ] for product in items]
    if page:
        rows.append([("Previous", f"admin:products:filter:{filter_name}:{page - 1}")])
    if page + 1 < total_pages:
        rows.append([("Next", f"admin:products:filter:{filter_name}:{page + 1}")])
    rows.append([("Back to products", "admin:products")])
    label = {"stock": "Low or out of stock", "norate": "Products without rate",
             "inactive": "Inactive products"}[filter_name]
    await query.edit_message_text(
        f"{label} · page {page + 1}/{total_pages} ({len(products)}):\n" +
        ("\n".join(f"{p.product_code} · {p.name} · stock {p.stock}" for p in items) or "None."),
        reply_markup=inline_keyboard(rows),
    )


async def _show_party_filter(query, context, filter_name: str, page: int = 0) -> None:
    async with SessionLocal() as session:
        statement = select(Party).order_by(Party.name)
        if filter_name == "inactive":
            statement = statement.where(Party.active.is_(False))
        else:
            statement = statement.where(~select(SalespersonParty.id).where(
                SalespersonParty.party_id == Party.id
            ).exists())
        parties = list((await session.scalars(statement.limit(500))).all())
    items, page, total_pages = paginate(parties, page)
    rows = [[
        (f"{clip_text(party.party_code, 25)} · {clip_text(party.name, 35)}",
         f"admin:party:open:{party.id}")
    ] for party in items]
    if page:
        rows.append([("Previous", f"admin:parties:filter:{filter_name}:{page - 1}")])
    if page + 1 < total_pages:
        rows.append([("Next", f"admin:parties:filter:{filter_name}:{page + 1}")])
    rows.append([("Back to parties", "admin:parties")])
    await query.edit_message_text(
        f"{'Inactive' if filter_name == 'inactive' else 'Unassigned'} parties · "
        f"page {page + 1}/{total_pages} ({len(parties)}):\n" +
        ("\n".join(f"{p.party_code} · {p.name}" for p in items) or "None."),
        reply_markup=inline_keyboard(rows),
    )


async def _product_callback(query, context, data: str) -> bool:
    parts = data.split(":")
    if len(parts) == 4 and parts[2] == "open":
        try:
            product_id = int(parts[3])
        except ValueError:
            return True
        async with SessionLocal() as session:
            product = await session.get(Product, product_id)
        if product is None:
            await query.edit_message_text("Product not found.")
            return True
        rows = [
            [("Rate", f"admin:product:edit:{product.id}:selling_rate"),
             ("MRP", f"admin:product:edit:{product.id}:mrp")],
            [("Stock", f"admin:product:edit:{product.id}:stock"),
             ("Name", f"admin:product:edit:{product.id}:name")],
            [("Pack size", f"admin:product:edit:{product.id}:pack_size"),
             ("Family", f"admin:product:edit:{product.id}:family")],
            [("Brand", f"admin:product:edit:{product.id}:brand"),
             (("Deactivate" if product.active else "Activate"), f"admin:product:toggle:{product.id}")],
            [("Back", "admin:products")],
        ]
        await query.edit_message_text(
            f"{product.name}\nCode: {product.product_code}\nBrand: {product.brand}\n"
            f"Pack: {product.pack_size or '-'} · MRP {product.mrp or '-'} · "
            f"Rate {product.selling_rate or '-'} · Stock {product.stock if product.stock is not None else '-'}\n"
            f"Status: {'Active' if product.active else 'Inactive'}",
            reply_markup=inline_keyboard(rows),
        )
        return True
    if len(parts) == 5 and parts[2] == "edit":
        try:
            product_id = int(parts[3])
        except ValueError:
            return True
        context.user_data["admin_input"] = {
            "kind": "product_edit", "product_id": product_id, "field": parts[4]
        }
        await query.edit_message_text(
            f"Send the new {parts[4]} value. Send '-' to clear nullable fields. Use /cancel to stop."
        )
        return True
    if len(parts) == 4 and parts[2] == "toggle":
        try:
            product_id = int(parts[3])
        except ValueError:
            return True
        async with SessionLocal() as session:
            product = await session.get(Product, product_id)
            if product is None:
                await query.edit_message_text("Product not found.")
                return True
            product.active = not product.active
            await session.commit()
        await query.edit_message_text(f"Product is now {'active' if product.active else 'inactive'}.")
        return True
    return False


async def _party_callback(query, context, data: str) -> bool:
    parts = data.split(":")
    if len(parts) == 4 and parts[2] == "open":
        try:
            party_id = int(parts[3])
        except ValueError:
            return True
        async with SessionLocal() as session:
            party = await session.get(Party, party_id)
            if party is None:
                await query.edit_message_text("Party not found.")
                return True
            people = list((await session.scalars(
                select(Salesperson).join(SalespersonParty).where(SalespersonParty.party_id == party.id)
            )).all())
            orders = await session.scalar(select(func.count()).select_from(Order).where(Order.party_id == party.id)) or 0
        await query.edit_message_text(
            f"{party.name}\nCode: {party.party_code}\nStatus: {'Active' if party.active else 'Inactive'}\n"
            f"Salespeople assigned: {', '.join(p.display_name for p in people) or 'none'}\nOrders: {orders}",
            reply_markup=inline_keyboard([
                [("Rename", f"admin:party:edit:{party.id}:name"),
                 (("Deactivate" if party.active else "Activate"), f"admin:party:toggle:{party.id}")],
                [("Set ERP party code", f"admin:party:edit:{party.id}:party_code")],
                [("Back", "admin:parties")],
            ]),
        )
        return True
    if len(parts) == 5 and parts[2] == "edit":
        field = parts[4]
        if field not in {"name", "party_code"}:
            await query.edit_message_text("Unsupported party field.")
            return True
        context.user_data["admin_input"] = {
            "kind": "party_edit", "party_id": int(parts[3]), "field": field
        }
        prompt = (
            "Send the actual ERP party code (1 to 120 characters)."
            if field == "party_code" else "Send the new party name."
        )
        await query.edit_message_text(prompt)
        return True
    if len(parts) == 4 and parts[2] == "toggle":
        party_id = int(parts[3])
        async with SessionLocal() as session:
            party = await session.get(Party, party_id)
            if party is None:
                await query.edit_message_text("Party not found.")
                return True
            party.active = not party.active
            await session.commit()
            active = party.active
        await query.edit_message_text(f"Party is now {'active' if active else 'inactive'}.")
        return True
    return False


async def _brand_callback(query, context, data: str) -> bool:
    parts = data.split(":")
    if len(parts) == 4 and parts[2] == "open":
        try:
            index = int(parts[3])
            brand = context.user_data.get("admin_brand_names", [])[index]
        except (ValueError, IndexError):
            await query.edit_message_text("This brand selection expired. Open Brands again.")
            return True
        async with SessionLocal() as session:
            counts = dict((await session.execute(
                select(Product.active, func.count(Product.id)).where(Product.brand == brand)
                .group_by(Product.active)
            )).all())
            rates_missing = await session.scalar(select(func.count()).select_from(Product).where(
                Product.brand == brand, Product.selling_rate.is_(None)
            )) or 0
            people = list((await session.scalars(
                select(Salesperson).join(SalespersonBrand)
                .where(SalespersonBrand.brand == brand).order_by(Salesperson.display_name)
            )).all())
        await query.edit_message_text(
            f"Brand: {brand}\nActive products: {counts.get(True, 0)} · "
            f"Inactive products: {counts.get(False, 0)} · Without rate: {rates_missing}\n"
            f"Assigned salespeople: {', '.join(p.display_name for p in people) or 'none'}",
            reply_markup=inline_keyboard([
                [("Deactivate all", f"admin:brand:confirm:{index}:off"),
                 ("Activate all", f"admin:brand:confirm:{index}:on")],
                [("Back", "admin:brands")],
            ]),
        )
        return True
    if len(parts) == 5 and parts[2] == "confirm":
        context.user_data["admin_brand_action"] = {
            "index": int(parts[3]), "active": parts[4] == "on"
        }
        await query.edit_message_text(
            "Change the active status for every product of this brand?",
            reply_markup=inline_keyboard([[
                ("Confirm", "admin:brand:apply"),
                ("Cancel", "admin:brands"),
            ]]),
        )
        return True
    if data == "admin:brand:apply":
        action = context.user_data.pop("admin_brand_action", None)
        if action is None:
            await query.edit_message_text("That confirmation expired. Open Brands again.")
            return True
        try:
            brand = context.user_data["admin_brand_names"][action["index"]]
        except (KeyError, IndexError):
            await query.edit_message_text("That brand selection expired.")
            return True
        async with SessionLocal() as session:
            result = await session.execute(
                update(Product).where(Product.brand == brand).values(active=action["active"])
            )
            await session.commit()
        await query.edit_message_text(f"Updated {result.rowcount} product(s) for {brand}.")
        return True
    return False


async def handle_catalog_text(update, context, value: str) -> bool:
    pending = context.user_data.get("admin_input")
    if not pending or pending.get("kind") not in {
        "catalog_search", "product_add", "product_edit", "party_add", "party_edit"
    }:
        return False
    kind = pending["kind"]
    if kind == "catalog_search":
        context.user_data.pop("admin_input", None)
        await show_catalog(_MessageQuery(update.effective_message), context, pending["catalog"], search=value)
        return True
    if kind == "product_add":
        parts = [part.strip() for part in value.split("|")]
        if len(parts) != 7:
            await update.effective_message.reply_text("Use exactly: CODE | Name | Brand | Pack | MRP | Rate | Stock")
            return True
        code, name, brand, pack, raw_mrp, raw_rate, raw_stock = parts
        try:
            mrp = _decimal_or_none(raw_mrp, "MRP")
            rate = _decimal_or_none(raw_rate, "selling rate")
            stock = _decimal_or_none(raw_stock, "stock")
            if not code or not name or not brand or len(code) > 120 or len(name) > 500 or len(brand) > 200:
                raise ValueError("Code, name and brand are required and must fit database limits.")
            if mrp is not None and (mrp < 0 or mrp.as_tuple().exponent < -2 or mrp > Decimal("999999999999.99")):
                raise ValueError("MRP must be non-negative with at most 2 decimal places.")
            if rate is not None and (rate < 0 or rate.as_tuple().exponent < -15 or rate > Decimal("999999999999.999999999999999")):
                raise ValueError("Rate is outside the supported range.")
            if stock is not None and (stock.as_tuple().exponent < -3 or abs(stock) > Decimal("99999999999.999")):
                raise ValueError("Stock supports at most 3 decimal places and the database limit.")
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return True
        async with SessionLocal() as session:
            existing = await session.scalar(select(Product.id).where(Product.product_code == code))
            if existing is not None:
                await update.effective_message.reply_text("That product code already exists.")
                return True
            product = Product(
                product_code=code, name=name, brand=brand, pack_size=pack or None,
                mrp=mrp, selling_rate=rate, stock=stock,
            )
            session.add(product)
            await session.commit()
        context.user_data.pop("admin_input", None)
        await update.effective_message.reply_text(f"Added product {code}.")
        return True
    if kind == "party_add":
        parts = [part.strip() for part in value.split("|", 1)]
        if len(parts) != 2 or not all(parts) or len(parts[0]) > 120 or len(parts[1]) > 300:
            await update.effective_message.reply_text("Use: CODE | Name (code <=120, name <=300 characters).")
            return True
        async with SessionLocal() as session:
            if await session.scalar(select(Party.id).where(Party.party_code == parts[0])) is not None:
                await update.effective_message.reply_text("That party code already exists.")
                return True
            session.add(Party(party_code=parts[0], name=parts[1]))
            await session.commit()
        context.user_data.pop("admin_input", None)
        await update.effective_message.reply_text(f"Added party {parts[0]}.")
        return True
    if kind == "product_edit":
        await _save_product_edit(update, context, pending, value)
        return True
    if kind == "party_edit":
        party_id = pending["party_id"]
        field = pending.get("field", "name")
        new_value = value.strip()
        max_length = 120 if field == "party_code" else 300
        if not new_value or len(new_value) > max_length:
            await update.effective_message.reply_text(
                f"{'Party code' if field == 'party_code' else 'Party name'} "
                f"must contain 1 to {max_length} characters."
            )
            return True
        async with SessionLocal() as session:
            party = await session.get(Party, party_id)
            if party is None:
                await update.effective_message.reply_text("Party not found.")
                context.user_data.pop("admin_input", None)
                return True
            if field == "party_code":
                duplicate = await session.scalar(select(Party.id).where(
                    Party.party_code == new_value, Party.id != party_id
                ))
                if duplicate is not None:
                    await update.effective_message.reply_text("That party code is already in use.")
                    return True
                party.party_code = new_value
            else:
                party.name = new_value
            await session.commit()
        context.user_data.pop("admin_input", None)
        await update.effective_message.reply_text(
            "ERP party code updated." if field == "party_code" else "Party name updated."
        )
        return True
    return False


async def _save_product_edit(update, context, pending: dict, raw: str) -> None:
    field = pending["field"]
    fields = {"name", "brand", "pack_size", "family", "mrp", "selling_rate", "stock"}
    if field not in fields:
        await update.effective_message.reply_text("Unsupported product field.")
        return
    value = raw.strip()
    try:
        if field in {"mrp", "selling_rate", "stock"}:
            if value == "-":
                converted = None
            else:
                converted = _decimal_or_none(value, field)
                if converted is None and field not in {"stock", "selling_rate", "mrp"}:
                    raise ValueError(f"{field} cannot be blank; use a number.")
                if converted is not None:
                    places = {"mrp": 2, "selling_rate": 15, "stock": 3}[field]
                    if converted.as_tuple().exponent < -places:
                        raise ValueError(f"{field} allows at most {places} decimal places.")
                    if field != "stock" and converted < 0:
                        raise ValueError(f"{field} cannot be negative.")
                    limits = {
                        "mrp": Decimal("999999999999.99"),
                        "selling_rate": Decimal("999999999999.999999999999999"),
                        "stock": Decimal("99999999999.999"),
                    }
                    if abs(converted) > limits[field]:
                        raise ValueError(f"{field} exceeds the supported database range.")
        else:
            converted = None if value == "-" and field in {"pack_size", "family"} else value
            max_length = {"name": 500, "brand": 200, "pack_size": 120, "family": 200}[field]
            if converted is None and field == "name":
                raise ValueError("Product name cannot be cleared.")
            if converted is not None and (not converted or len(converted) > max_length):
                raise ValueError(f"{field} must be 1 to {max_length} characters.")
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    async with SessionLocal() as session:
        product = await session.get(Product, pending["product_id"])
        if product is None:
            await update.effective_message.reply_text("Product not found.")
            context.user_data.pop("admin_input", None)
            return
        setattr(product, field, converted)
        await session.commit()
    context.user_data.pop("admin_input", None)
    await update.effective_message.reply_text(
        f"Product {field} updated. If the brand changed, review salesperson brand assignments."
        if field == "brand" else f"Product {field} updated."
    )


def _decimal_or_none(value: str, field: str) -> Decimal | None:
    if value.strip() in {"", "-"}:
        return None
    try:
        result = Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValueError(f"Enter a valid {field} amount.") from exc
    if not result.is_finite():
        raise ValueError(f"Enter a finite {field} amount.")
    return result


class _MessageQuery:
    def __init__(self, message):
        self.message = message
