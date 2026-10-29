from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from copy import deepcopy

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from sqlalchemy import select
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified

from petgenics.db import SessionLocal
from petgenics.models import BotSession, Order, OrderAudit, Product, Salesperson, ScannerRun
from petgenics.orders import accessible_brands, accessible_parties, create_draft, submit_draft
from petgenics.scanner import analyze_document
from petgenics.money import discount_value, free_quantity_value, order_quantity


def _buttons(rows: list[tuple[str, str]], width: int = 2) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(label, callback_data=data) for label, data in rows]
    return InlineKeyboardMarkup([buttons[i:i + width] for i in range(0, len(buttons), width)])


def _brand_buttons(brands: list[str]) -> InlineKeyboardMarkup:
    return _buttons([(brand, f"brand:{index}") for index, brand in enumerate(brands[:50])])


async def _saved_state(session, user_id: int) -> BotSession:
    row = await session.scalar(select(BotSession).where(BotSession.telegram_user_id == user_id))
    if row is None:
        row = BotSession(telegram_user_id=user_id, state={})
        session.add(row)
        await session.flush()
    return row


async def _persist_state(session, saved: BotSession, state: dict) -> None:
    saved.state = deepcopy(state)
    flag_modified(saved, "state")
    order_id = state.get("draft_order_id")
    if order_id is None:
        return
    order = await session.get(Order, int(order_id))
    if order is not None and order.status == "Draft":
        items = deepcopy(state.get("items", []))
        old_items = (order.draft_data or {}).get("items", [])
        brands = {item.get("brand") for item in items if item.get("brand")}
        draft_brand = next(iter(brands)) if len(brands) == 1 else (
            "Multiple Brands" if brands else state.get("brand", "Unselected")
        )
        if old_items != items or order.remarks != state.get("remarks") or order.brand != draft_brand:
            session.add(OrderAudit(
                order_id=order.id,
                actor=str(saved.telegram_user_id),
                action="draft_updated",
                details={
                    "items": [
                        {key: item.get(key) for key in (
                            "brand", "product_code", "quantity", "free_quantity", "rate", "discount_percent"
                        )}
                        for item in items
                    ],
                    "remarks": state.get("remarks"),
                    "brand": draft_brand,
                },
            ))
        order.draft_data = {"items": items}
        order.remarks = state.get("remarks")
        order.brand = draft_brand


async def _salesperson(session, user_id: int) -> Salesperson | None:
    return await session.scalar(
        select(Salesperson).where(Salesperson.telegram_user_id == user_id, Salesperson.active.is_(True))
    )


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    async with SessionLocal() as session:
        salesperson = await _salesperson(session, user.id)
    if salesperson is None:
        await update.effective_message.reply_text(
            "Your Telegram account is not approved yet. Please ask your Petgenics administrator to approve it."
        )
        return
    await update.effective_message.reply_text(
        f"Welcome, {salesperson.display_name}. Use /order to start an order, /cart to review it, "
        "/switchbrand to change brands, /cancel to clear it, or /help for commands."
    )


async def _help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "/order - start or resume an order\n"
        "/neworder - discard the current draft and start a fresh order\n"
        "/cart - review the current cart\n"
        "/switchbrand - switch brand without clearing the cart\n"
        "/back - return to the previous order step\n"
        "/remarks text - add order remarks\n"
        "/submit - submit the order\n"
        "/cancel - cancel the unfinished order\n"
        "You can also send a PDF, JPG or PNG order sheet for scanner-assisted review."
    )


async def _remarks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    value = " ".join(context.args).strip() or None
    async with SessionLocal() as session:
        saved = await _saved_state(session, update.effective_user.id)
        state = deepcopy(saved.state or {})
        if not state.get("draft_order_id"):
            await update.effective_message.reply_text("Start or resume an order before adding remarks.")
            return
        state["remarks"] = value
        await _persist_state(session, saved, state)
        order = await session.get(Order, int(state["draft_order_id"]))
        if order is not None and order.status == "Draft":
            order.remarks = value
        await session.commit()
    await update.effective_message.reply_text("Order remarks updated." if value else "Order remarks cleared.")


async def _new_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with SessionLocal() as session:
        saved = await _saved_state(session, update.effective_user.id)
        old_order_id = (saved.state or {}).get("draft_order_id")
        if old_order_id:
            old_order = await session.get(Order, int(old_order_id))
            if old_order is not None and old_order.status == "Draft":
                await session.delete(old_order)
        saved.state = {}
        await session.commit()
    await _order(update, context)


async def _order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with SessionLocal() as session:
        salesperson = await _salesperson(session, update.effective_user.id)
        if salesperson is None:
            await update.effective_message.reply_text("Your account is not approved. Contact your administrator.")
            return
        saved = await _saved_state(session, update.effective_user.id)
        state = saved.state or {}
        if state.get("draft_order_id") and state.get("party_id") and state.get("brand"):
            await update.effective_message.reply_text(
                f"Resuming your order for {state.get('party_name')} / {state['brand']}."
                "\nSend a product code or product name to search."
            )
            state["step"] = "search"
            await _persist_state(session, saved, state)
            await session.commit()
            return
        if state.get("draft_order_id") and state.get("party_id"):
            salesperson = await _salesperson(session, update.effective_user.id)
            brands = await accessible_brands(session, salesperson.id)
            state["brand_choices"] = brands
            await _persist_state(session, saved, state)
            await session.commit()
            await update.effective_message.reply_text(
                f"Resume draft for {state.get('party_name')}. Select a brand:",
                reply_markup=_brand_buttons(brands),
            )
            return
        parties = await accessible_parties(session, salesperson.id)
        if not parties:
            await update.effective_message.reply_text("No parties are assigned to your account.")
            return
        state["step"] = "party_search"
        await _persist_state(session, saved, state)
        await session.commit()
        await update.effective_message.reply_text(
            "Select a customer party or type its name/code to search:",
            reply_markup=_buttons([(f"{p.name} · {p.party_code}", f"party:{p.id}") for p in parties[:50]]),
        )


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with SessionLocal() as session:
        saved = await _saved_state(session, update.effective_user.id)
        order_id = (saved.state or {}).get("draft_order_id")
        if order_id:
            order = await session.get(Order, int(order_id))
            if order is not None and order.status == "Draft":
                await session.delete(order)
        saved.state = {}
        await session.commit()
    await update.effective_message.reply_text("Unfinished order cleared.")


async def _cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with SessionLocal() as session:
        saved = await _saved_state(session, update.effective_user.id)
        state = saved.state or {}
        items = state.get("items", [])
        if not items:
            await update.effective_message.reply_text("Your cart is empty. Use /order to begin.")
            return
        lines = [
            f"Party: {state.get('party_name', 'not selected')} · "
            f"Current brand: {state.get('brand', 'not selected')}"
        ]
        total = Decimal("0")
        for item in items:
            line = Decimal(item["quantity"]) * Decimal(item["rate"])
            line *= Decimal("1") - Decimal(item["discount_percent"]) / Decimal("100")
            total += line
            lines.append(
                f"{item.get('brand', '')} · {item['product_code']} · {item['product_name']}: {item['quantity']} + "
                f"{item['free_quantity']} free × ₹{item['rate']} · {item['discount_percent']}% off"
            )
        lines.append(f"Total: ₹{total:.2f}\nUse /submit to submit or /cancel to clear.")
        if state.get("remarks"):
            lines.append(f"Remarks: {state['remarks']}")
        item_buttons = []
        for index, item in enumerate(items):
            item_buttons.append((f"Edit item {index + 1}", f"edit:{index}"))
            item_buttons.append((f"Remove item {index + 1}", f"remove:{index}"))
        await update.effective_message.reply_text(
            "\n".join(lines),
            reply_markup=_buttons(item_buttons, 2) if item_buttons else None,
        )


async def _switch_brand(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with SessionLocal() as session:
        salesperson = await _salesperson(session, update.effective_user.id)
        if salesperson is None:
            await update.effective_message.reply_text("Your account is not approved.")
            return
        brands = await accessible_brands(session, salesperson.id)
        if not brands:
            await update.effective_message.reply_text("No brands are assigned to your account.")
            return
        saved = await _saved_state(session, update.effective_user.id)
        if not (saved.state or {}).get("draft_order_id"):
            await update.effective_message.reply_text("Start an order with /order before switching brands.")
            return
        state = deepcopy(saved.state)
        state["brand_choices"] = brands
        await _persist_state(session, saved, state)
        await session.commit()
        await update.effective_message.reply_text(
            "Choose a brand (your cart will be kept):",
            reply_markup=_brand_buttons(brands),
        )


async def _back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with SessionLocal() as session:
        saved = await _saved_state(session, update.effective_user.id)
        state = deepcopy(saved.state or {})
        step = state.get("step")
        if step == "free_quantity":
            state["step"] = "quantity"
            message = "Previous step: enter the order quantity again."
        elif step == "discount":
            salesperson = await _salesperson(session, update.effective_user.id)
            state["step"] = "free_quantity" if salesperson and salesperson.allow_free_quantity else "quantity"
            message = "Previous step: enter free quantity (0 if none)." if state["step"] == "free_quantity" else "Previous step: enter the order quantity again."
        elif step == "quantity":
            state.pop("pending", None)
            state["step"] = "search"
            message = "Previous step: send a product code, name, or family to search."
        elif step == "edit_quantity":
            state.pop("edit_index", None)
            state["step"] = "search"
            message = "Edit cancelled. Use /cart to continue reviewing."
        elif step == "search":
            salesperson = await _salesperson(session, update.effective_user.id)
            brands = await accessible_brands(session, salesperson.id) if salesperson else []
            state["brand_choices"] = brands
            await _persist_state(session, saved, state)
            await session.commit()
            message = "Choose a brand:"
            await update.effective_message.reply_text(message, reply_markup=_brand_buttons(brands))
            return
        else:
            message = "Use /order to select or search for a customer party."
        await _persist_state(session, saved, state)
        await session.commit()
    await update.effective_message.reply_text(message)


async def _submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with SessionLocal() as session:
        saved = await _saved_state(session, update.effective_user.id)
        state = saved.state or {}
        if not state.get("items") or not state.get("party_id") or not state.get("brand"):
            await update.effective_message.reply_text("Select a party and brand and add products before submitting.")
            return
        try:
            order = await submit_draft(
                session,
                update.effective_user.id,
                int(state["draft_order_id"]),
                state["items"],
                state.get("remarks"),
            )
        except (ValueError, KeyError) as exc:
            await update.effective_message.reply_text(f"Order was not submitted: {exc}")
            return
        saved.state = {}
        await session.commit()
    await update.effective_message.reply_text(f"Order {order.order_number} submitted for approval.")


async def _text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = (update.effective_message.text or "").strip()
    async with SessionLocal() as session:
        salesperson = await _salesperson(session, user_id)
        if salesperson is None:
            await update.effective_message.reply_text("Your account is not approved. Contact your administrator.")
            return
        saved = await _saved_state(session, user_id)
        state = deepcopy(saved.state or {})
        step = state.get("step")
        if step == "party_search":
            parties = await accessible_parties(session, salesperson.id)
            matches = [
                party for party in parties
                if text.casefold() in party.name.casefold() or text.casefold() in party.party_code.casefold()
            ][:50]
            if not matches:
                await update.effective_message.reply_text("No assigned party matched. Try its name or stable code.")
                return
            await update.effective_message.reply_text(
                "Select a matching party:",
                reply_markup=_buttons([(f"{p.name} · {p.party_code}", f"party:{p.id}") for p in matches]),
            )
            return
        if step == "search":
            allowed_brands = set(await accessible_brands(session, salesperson.id))
            if state.get("brand") not in allowed_brands:
                await update.effective_message.reply_text(
                    "Your brand assignment changed. Use /switchbrand to select an authorized brand."
                )
                return
            products = list((await session.scalars(
                select(Product).where(
                    Product.active.is_(True),
                    Product.brand == state.get("brand"),
                    (Product.name.ilike(f"%{text}%"))
                    | (Product.product_code.ilike(f"%{text}%"))
                    | (Product.family.ilike(f"%{text}%")),
                ).order_by(Product.name).limit(30)
            )).all())
            if not products:
                await update.effective_message.reply_text("No matching products. Try a different code or name.")
                return
            await update.effective_message.reply_text(
                "Choose a product and pack variant:",
                reply_markup=_buttons([
                    (f"{p.name} · {p.pack_size or 'pack not listed'} · {p.product_code}", f"product:{p.id}")
                    for p in products
                ], 1),
            )
            return
        if step == "edit_quantity":
            try:
                quantity = order_quantity(text)
                index = int(state["edit_index"])
                state["items"][index]["quantity"] = str(quantity)
            except (InvalidOperation, ValueError, IndexError, KeyError):
                await update.effective_message.reply_text("Enter a quantity greater than zero.")
                return
            state.pop("edit_index", None)
            state["step"] = "search"
            await _persist_state(session, saved, state)
            await session.commit()
            await update.effective_message.reply_text("Quantity updated. Use /cart to review or keep adding products.")
            return
        if step == "quantity":
            try:
                quantity = order_quantity(text)
            except (InvalidOperation, ValueError):
                await update.effective_message.reply_text("Enter a quantity greater than zero.")
                return
            state["pending"]["quantity"] = str(quantity)
            if not salesperson.allow_free_quantity:
                state["pending"]["free_quantity"] = "0"
                if salesperson.allow_discount:
                    state["step"] = "discount"
                    await _persist_state(session, saved, state)
                    await session.commit()
                    await update.effective_message.reply_text(
                        f"Enter additional discount percent (0 to {salesperson.max_discount_percent})."
                    )
                    return
                state["pending"]["discount_percent"] = "0"
                state["items"] = state.get("items", []) + [state.pop("pending")]
                state["step"] = "search"
                await _persist_state(session, saved, state)
                await session.commit()
                await update.effective_message.reply_text("Added. Search another product in the same brand, or use /cart.")
                return
            state["step"] = "free_quantity"
            await _persist_state(session, saved, state)
            await session.commit()
            await update.effective_message.reply_text("Enter free quantity (0 if none).")
            return
        if step == "free_quantity":
            try:
                free_quantity = free_quantity_value(text)
            except (InvalidOperation, ValueError):
                await update.effective_message.reply_text("Enter a free quantity of zero or greater.")
                return
            state["pending"]["free_quantity"] = str(free_quantity)
            if salesperson.allow_discount:
                state["step"] = "discount"
                await _persist_state(session, saved, state)
                await session.commit()
                await update.effective_message.reply_text(
                    f"Enter additional discount percent (0 to {salesperson.max_discount_percent})."
                )
                return
            state["pending"]["discount_percent"] = "0"
            state["items"] = state.get("items", []) + [state.pop("pending")]
            state["step"] = "search"
            await _persist_state(session, saved, state)
            await session.commit()
            await update.effective_message.reply_text("Added. Search another product in the same brand, or use /cart.")
            return
        if step == "discount":
            try:
                discount = discount_value(text)
                if discount > salesperson.max_discount_percent:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                await update.effective_message.reply_text(
                    f"Discount must be between 0 and {salesperson.max_discount_percent} percent."
                )
                return
            state["pending"]["discount_percent"] = str(discount)
            state["items"] = state.get("items", []) + [state.pop("pending")]
            state["step"] = "search"
            await _persist_state(session, saved, state)
            await session.commit()
            await update.effective_message.reply_text("Added. Search another product in the same brand, or use /cart.")
            return
    await update.effective_message.reply_text("Use /order to start or resume an order, or /help for commands.")


async def _callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data or ""
    async with SessionLocal() as session:
        salesperson = await _salesperson(session, user_id)
        if salesperson is None:
            await query.edit_message_text("Your account is not approved.")
            return
        saved = await _saved_state(session, user_id)
        state = saved.state or {}
        if data.startswith("party:"):
            party_id = int(data.split(":", 1)[1])
            parties = await accessible_parties(session, salesperson.id)
            party = next((p for p in parties if p.id == party_id), None)
            if party is None:
                await query.edit_message_text("This party is not assigned to you.")
                return
            existing_draft_id = state.get("draft_order_id")
            if existing_draft_id:
                existing_draft = await session.get(Order, int(existing_draft_id))
                if existing_draft is not None and existing_draft.status == "Draft":
                    await query.edit_message_text(
                        "A draft is already active. Use /neworder to replace it, or /cancel to discard it."
                    )
                    return
            draft = await create_draft(session, user_id, party.id)
            pending_scan_items = state.get("pending_scan_items", [])
            scan_items = []
            for scanned in pending_scan_items:
                product = await session.get(Product, scanned["product_id"])
                if (
                    product is not None
                    and product.active
                    and product.product_code == scanned["product_code"]
                    and product.selling_rate is not None
                ):
                    scan_items.append({
                        "product_id": product.id,
                        "product_code": product.product_code,
                        "product_name": product.name,
                        "brand": product.brand,
                        "quantity": scanned["quantity"],
                        "free_quantity": "0",
                        "rate": str(product.selling_rate),
                        "discount_percent": "0",
                    })
            first_brand = scan_items[0]["brand"] if scan_items else None
            state = {
                "draft_order_id": draft.id, "party_id": party.id, "party_name": party.name,
                "items": scan_items, "step": "search",
            }
            if first_brand:
                state["brand"] = first_brand
            brands = await accessible_brands(session, salesperson.id)
            state["brand_choices"] = brands
            await _persist_state(session, saved, state)
            await session.commit()
            await query.edit_message_text(
                f"Party: {party.name}\n"
                f"{len(scan_items)} confirmed scanner line(s) added to the editable draft.\n"
                "Review with /cart. Select a brand to add more products:",
                reply_markup=_brand_buttons(brands),
            )
            return
        if data.startswith("scan-review:"):
            try:
                run_id = int(data.split(":", 1)[1])
            except ValueError:
                await query.edit_message_text("Invalid scanner review.")
                return
            run = await session.scalar(
                select(ScannerRun)
                .options(load_only(
                    ScannerRun.id, ScannerRun.telegram_user_id, ScannerRun.result,
                    ScannerRun.expires_at,
                ))
                .where(ScannerRun.id == run_id)
            )
            if (
                run is None
                or run.telegram_user_id != user_id
                or run.expires_at <= datetime.now(timezone.utc)
                or run.result.get("expired")
                or run.result.get("confirmed")
            ):
                await query.edit_message_text("That scanner review is unavailable.")
                return
            allowed_brands = set(await accessible_brands(session, salesperson.id))
            scan_items = []
            unmatched = 0
            for line in run.result.get("lines", []):
                if (
                    not line.get("product_id")
                    or not line.get("quantity")
                    or Decimal(str(line.get("confidence", "0"))) < Decimal("0.9")
                ):
                    unmatched += 1
                    continue
                product = await session.get(Product, int(line["product_id"]))
                if (
                    product is None
                    or not product.active
                    or product.brand not in allowed_brands
                    or product.selling_rate is None
                ):
                    unmatched += 1
                    continue
                try:
                    quantity = order_quantity(line["quantity"])
                except (InvalidOperation, ValueError):
                    unmatched += 1
                    continue
                scan_items.append({
                    "product_id": product.id,
                    "product_code": product.product_code,
                    "product_name": product.name,
                    "brand": product.brand,
                    "quantity": str(quantity),
                    "free_quantity": "0",
                    "rate": str(product.selling_rate),
                    "discount_percent": "0",
                })
            existing_draft_id = state.get("draft_order_id")
            existing_draft = (
                await session.get(Order, int(existing_draft_id)) if existing_draft_id else None
            )
            run.result = {**run.result, "confirmed": True}
            if existing_draft is not None and existing_draft.status == "Draft":
                state["items"] = state.get("items", []) + scan_items
                await _persist_state(session, saved, state)
                await session.commit()
                await query.edit_message_text(
                    f"{len(scan_items)} confirmed scanner line(s) added to your existing editable draft. "
                    f"{unmatched} line(s) need manual entry. Use /cart to review and /submit when ready."
                )
                return
            state = {"pending_scan_items": scan_items, "step": "party_search"}
            saved.state = state
            parties = await accessible_parties(session, salesperson.id)
            await session.commit()
            message = (
                f"{len(scan_items)} certain product/quantity line(s) are ready for a draft. "
                f"{unmatched} line(s) need manual entry. Scanner does not infer free quantities, "
                "rates, or discounts; review and edit the draft before submission. Select a party:"
            )
            await query.edit_message_text(
                message,
                reply_markup=_buttons([(f"{p.name} · {p.party_code}", f"party:{p.id}") for p in parties[:50]]),
            )
            return
        if data.startswith("scan-discard:"):
            try:
                run_id = int(data.split(":", 1)[1])
            except ValueError:
                await query.edit_message_text("Invalid scanner review.")
                return
            run = await session.scalar(
                select(ScannerRun)
                .options(load_only(ScannerRun.id, ScannerRun.telegram_user_id, ScannerRun.expires_at))
                .where(ScannerRun.id == run_id)
            )
            if run is not None and run.telegram_user_id == user_id:
                run.document_bytes = None
                run.result = {"discarded": True}
                run.expires_at = datetime.now(timezone.utc)
                await session.commit()
            await query.edit_message_text("Scanner review discarded; uploaded document removed.")
            return
        if data.startswith("brand:"):
            try:
                brand_index = int(data.split(":", 1)[1])
                brands = await accessible_brands(session, salesperson.id)
                brand = state.get("brand_choices", [])[brand_index]
            except (ValueError, IndexError, TypeError):
                await query.edit_message_text("That brand selection is no longer available.")
                return
            if brand not in brands:
                await query.edit_message_text("This brand is not assigned to you.")
                return
            state["brand"] = brand
            state["step"] = "search"
            await _persist_state(session, saved, state)
            await session.commit()
            await query.edit_message_text(f"Brand: {brand}\nSend a product code or name to search.")
            return
        if data.startswith("product:"):
            product_id = int(data.split(":", 1)[1])
            product = await session.get(Product, product_id)
            allowed_brands = set(await accessible_brands(session, salesperson.id))
            if (
                product is None
                or not product.active
                or product.brand != state.get("brand")
                or product.brand not in allowed_brands
            ):
                await query.edit_message_text("That product is no longer available in this brand.")
                return
            if product.selling_rate is None:
                await query.edit_message_text(
                    f"No selling rate is configured for {product.product_code}. Ask an administrator to update the product master."
                )
                return
            state["pending"] = {
                "product_id": product.id,
                "product_code": product.product_code,
                "product_name": product.name,
                "brand": product.brand,
                "quantity": "0",
                "free_quantity": "0",
                "rate": str(product.selling_rate),
                "discount_percent": "0",
            }
            state["step"] = "quantity"
            await _persist_state(session, saved, state)
            await session.commit()
            await query.edit_message_text(
                f"{product.name}\nCode: {product.product_code}\nPack: {product.pack_size or 'not listed'}\n"
                f"MRP: {product.mrp or 'not listed'} · Rate: {product.selling_rate or 'not configured'}\n"
                f"Stock: {product.stock if product.stock is not None else 'not provided'}\nEnter order quantity:"
            )
            return
        if data.startswith(("remove:", "edit:")):
            try:
                index = int(data.split(":", 1)[1])
                item = state.get("items", [])[index]
            except (ValueError, IndexError):
                await query.edit_message_text("That cart item is no longer available.")
                return
            if data.startswith("remove:"):
                state["items"].pop(index)
                await _persist_state(session, saved, state)
                await session.commit()
                await query.edit_message_text(f"Removed {item['product_code']} from the cart.")
                return
            state["edit_index"] = index
            state["step"] = "edit_quantity"
            await _persist_state(session, saved, state)
            await session.commit()
            await query.edit_message_text(f"Enter a new quantity for {item['product_code']}:")


async def _document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    telegram_file = message.document or message.photo[-1]
    filename = getattr(telegram_file, "file_name", None) or "order.jpg"
    if not filename.lower().endswith((".pdf", ".jpg", ".jpeg", ".png")):
        await message.reply_text("Upload a PDF, JPG or PNG order document.")
        return
    if telegram_file.file_size and telegram_file.file_size > context.bot_data["max_upload_bytes"]:
        await message.reply_text("That file exceeds the configured upload-size limit.")
        return
    async with SessionLocal() as session:
        salesperson = await _salesperson(session, user.id)
        if salesperson is None:
            await message.reply_text("Your account is not approved. Contact your administrator.")
            return
        file = await context.bot.get_file(telegram_file.file_id)
        content = bytes(await file.download_as_bytearray())
        if len(content) > context.bot_data["max_upload_bytes"]:
            await message.reply_text("That file exceeds the configured upload-size limit.")
            return
        try:
            brands = set(await accessible_brands(session, salesperson.id))
            result = await analyze_document(session, content, filename, brands)
        except (ValueError, OSError, RuntimeError) as exc:
            await message.reply_text(f"Could not read that document: {exc}")
            return
        from datetime import datetime, timedelta, timezone

        run = ScannerRun(
            telegram_user_id=user.id,
            filename=filename[:255],
            result=result,
            document_bytes=content,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=context.bot_data["scanner_retention_hours"]),
        )
        session.add(run)
        await session.commit()
        lines = result["lines"][:20]
        summary = [f"Scanner review #{run.id} — {len(result['pages'])} page(s)."]
        summary += [f"Unreadable pages: {result['unreadable_pages']}"] if result["unreadable_pages"] else []
        if lines:
            summary.extend(
                f"{'✓' if line['product_code'] and line['quantity'] and line['confidence'] >= 0.9 else '⚠'} "
                f"{line['source_text'][:100]} → {line['product_code'] or 'unmatched'}, "
                f"qty {line['quantity'] or 'confirm'}"
                for line in lines
            )
        else:
            summary.append("No readable order lines were detected.")
        summary.append("Confirm to create an editable order draft; unrecognized lines will not be added.")
        await message.reply_text(
            "\n".join(summary),
            reply_markup=_buttons([
                ("Review confirmed matches", f"scan-review:{run.id}"),
                ("Discard review", f"scan-discard:{run.id}"),
            ]),
        )


def build_bot(token: str, max_upload_bytes: int, scanner_retention_hours: int) -> Application:
    application = Application.builder().token(token).build()
    application.bot_data["max_upload_bytes"] = max_upload_bytes
    application.bot_data["scanner_retention_hours"] = scanner_retention_hours
    application.add_handler(CommandHandler("start", _start))
    application.add_handler(CommandHandler("help", _help))
    application.add_handler(CommandHandler("remarks", _remarks))
    application.add_handler(CommandHandler("order", _order))
    application.add_handler(CommandHandler("neworder", _new_order))
    application.add_handler(CommandHandler("cancel", _cancel))
    application.add_handler(CommandHandler("cart", _cart))
    application.add_handler(CommandHandler("switchbrand", _switch_brand))
    application.add_handler(CommandHandler("back", _back))
    application.add_handler(CommandHandler("submit", _submit))
    application.add_handler(CallbackQueryHandler(_callback))
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, _document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _text))
    return application
