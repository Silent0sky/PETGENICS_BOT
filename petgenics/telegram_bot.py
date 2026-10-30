from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from copy import deepcopy
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from sqlalchemy import func, select
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified

from petgenics.admin_telegram import (
    admin_callback,
    admin_menu,
    admin_order,
    admin_permission,
    admin_report,
    handle_admin_document,
    my_id,
)
from petgenics.bot_menu import ALL_BUTTON_LABELS, MENU_ACTIONS, reply_keyboard
from petgenics.db import SessionLocal
from petgenics.models import (
    BotSession,
    Order,
    OrderAudit,
    Party,
    Product,
    Salesperson,
    SalespersonParty,
    ScannerRun,
)
from petgenics.orders import accessible_brands, accessible_parties, create_draft, submit_draft
from petgenics.scanner import analyze_document
from petgenics.money import discount_value, free_quantity_value, line_total, order_quantity
from petgenics.text_orders import parse_text_order, text_match_score


def _buttons(rows: list[tuple[str, str]], width: int = 2) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(label, callback_data=data) for label, data in rows]
    return InlineKeyboardMarkup([buttons[i:i + width] for i in range(0, len(buttons), width)])


def _brand_buttons(brands: list[str]) -> InlineKeyboardMarkup:
    return _buttons([(brand, f"brand:{index}") for index, brand in enumerate(brands[:50])])


def _party_buttons(
    parties: list[Party], allow_add: bool = False, allow_search: bool = False
) -> InlineKeyboardMarkup:
    rows = []
    for party in parties[:50]:
        name = party.name if len(party.name) <= 40 else party.name[:39] + "…"
        code = party.party_code if len(party.party_code) <= 18 else party.party_code[:17] + "…"
        rows.append((f"{name} · {code}", f"party:{party.id}"))
    if allow_search:
        rows.append(("🔎 Search another party", "party:search"))
    if allow_add:
        rows.append(("➕ Add new party", "party:add"))
    return _buttons(rows)


def _text_order_keyboard(state: dict, allow_confirm: bool) -> InlineKeyboardMarkup:
    rows = []
    for index, item in enumerate(state.get("text_order_items", [])):
        rows.append((f"🔎 Search product {index + 1}", f"textorder:search:{index}"))
        for candidate in item.get("candidates", []):
            name = candidate["name"]
            brand = candidate["brand"]
            if len(name) > 34:
                name = name[:33] + "…"
            if len(brand) > 18:
                brand = brand[:17] + "…"
            rows.append((
                f"{index + 1}. {name} · {brand}",
                f"textorder:item:{index}:{candidate['id']}",
            ))
    if allow_confirm:
        rows.append(("✅ Confirm and submit order", "textorder:confirm"))
    rows.append(("❌ Discard text order", "textorder:cancel"))
    return _buttons(rows)


def _text_order_preview(state: dict, salesperson: Salesperson) -> tuple[str, bool]:
    def short(value: object, limit: int) -> str:
        text = str(value)
        return text if len(text) <= limit else text[:limit - 1] + "…"

    lines = [
        "Review pasted order before submission:",
        f"Party: {short(state['text_order_party_name'], 120)}",
    ]
    items = state.get("text_order_items", [])
    total = Decimal("0")
    all_matched = bool(items)
    has_free_quantity = False
    for index, item in enumerate(items):
        selected = item.get("selected")
        if selected is None:
            all_matched = False
            lines.append(
                f"{index + 1}. {short(item['source_text'], 90)}"
                "\n   Product: not selected — choose a match below."
            )
            continue
        quantity = Decimal(item["quantity"])
        free_quantity = Decimal(item["free_quantity"])
        has_free_quantity = has_free_quantity or free_quantity > 0
        product_name = short(selected["name"], 32)
        brand = short(selected["brand"], 16)
        pack_size = short(selected.get("pack_size") or "not set", 16)
        mrp = short(f"₹{selected['mrp']}", 16) if selected.get("mrp") is not None else "not set"
        stock = short(selected["stock"], 16) if selected.get("stock") is not None else "not set"
        product_query = short(item["product_query"], 32)
        lines.append(
            f"{index + 1}. {product_query} → {product_name} ({brand})"
        )
        lines.append(
            f"   Code: {selected['code']} · Pack: {pack_size} · "
            f"MRP: {mrp} · Stock: {stock}"
        )
        if selected.get("rate") is None:
            all_matched = False
            lines.append(
                "   Selling rate is missing; ask an admin to update this product."
            )
            continue
        try:
            line_total_value = line_total(quantity, Decimal(selected["rate"]), Decimal("0"))
        except ValueError:
            all_matched = False
            lines.append("   Quantity or total is outside the supported order limit.")
            continue
        total += line_total_value
        confidence_note = " · suggested match — verify" if selected.get("confidence", 1) < 1 else ""
        lines.append(
            f"   {short(quantity, 14)} ordered + {short(free_quantity, 14)} free · "
            f"Rate: ₹{short(selected['rate'], 16)}"
            f"{confidence_note}"
        )
    lines.append(f"Subtotal before discounts: ₹{total:.2f}")
    free_allowed = salesperson.allow_free_quantity
    if has_free_quantity and not free_allowed:
        all_matched = False
        lines.append("Free quantity is not enabled for your account. Ask an admin to authorize it.")
    if all_matched:
        lines.append("Tap Confirm and submit order, or choose another product match above.")
    else:
        lines.append("Resolve all product matches and permissions before submitting.")
    return "\n".join(lines), all_matched


def _rank_text_matches(
    query: str, candidates: list[Any], label, limit: int = 3
) -> list[tuple[Any, float]]:
    ranked = [(candidate, text_match_score(query, label(candidate))) for candidate in candidates]
    return sorted(ranked, key=lambda match: (-match[1], label(match[0]).casefold()))[:limit]


def _text_product_candidate(product: Product, confidence: float) -> dict:
    return {
        "id": product.id,
        "code": product.product_code,
        "name": product.name,
        "brand": product.brand,
        "pack_size": product.pack_size,
        "mrp": str(product.mrp) if product.mrp is not None else None,
        "rate": str(product.selling_rate) if product.selling_rate is not None else None,
        "stock": str(product.stock) if product.stock is not None else None,
        "confidence": round(confidence, 3),
    }


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
    if user.id in context.bot_data.get("admin_telegram_ids", set()):
        await update.effective_message.reply_text(
            "Welcome, Petgenics administrator. Use /admin for admin tools or /help for commands.",
            reply_markup=reply_keyboard(True),
        )
        return
    async with SessionLocal() as session:
        salesperson = await _salesperson(session, user.id)
    if salesperson is None:
        await update.effective_message.reply_text(
            "Your Telegram account is not approved yet. Please ask your Petgenics administrator to approve it.",
            reply_markup=reply_keyboard(False),
        )
        return
    await update.effective_message.reply_text(
        f"Welcome, {salesperson.display_name}. Use /order to start an order, /cart to review it, "
        "/switchbrand to change brands, /cancel to clear it, or /help for commands.",
        reply_markup=reply_keyboard(False),
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
        "/myid - show your Telegram user ID\n"
        "/admin - open the admin menu (approved administrators only)\n"
        "/adminorder ORDER_NUMBER - download files for an older order (admins only)\n"
        "/adminreport YYYY-MM-DD - download a date-wise order Excel (admins only)\n"
        "/adminpermission ID yes/no MAX_DISCOUNT yes/no - set discount/free quantity permissions\n"
        "You can also send a PDF, JPG or PNG order sheet for scanner-assisted review.",
        reply_markup=reply_keyboard(
            update.effective_user.id in context.bot_data.get("admin_telegram_ids", set())
        ),
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
            await update.effective_message.reply_text(
                "Your account is not approved. Contact your administrator.",
                reply_markup=reply_keyboard(
                    update.effective_user.id in context.bot_data.get("admin_telegram_ids", set())
                ),
            )
            return
        await update.effective_message.reply_text(
            "Order buttons are ready below. You can also type a party, product, or quantity.",
            reply_markup=reply_keyboard(False),
        )
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
        brands = await accessible_brands(session, salesperson.id)
        if not brands:
            await update.effective_message.reply_text("No companies/brands are assigned to your account.")
            return
        state = {
            "step": "brand_select",
            "brand_choices": brands,
            "order_start": "company_first",
        }
        await _persist_state(session, saved, state)
        await session.commit()
        await update.effective_message.reply_text(
            "First select the company / brand:",
            reply_markup=_brand_buttons(brands),
        )


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("admin_input", None)
    context.user_data.pop("admin_import_kind", None)
    async with SessionLocal() as session:
        saved = await _saved_state(session, update.effective_user.id)
        order_id = (saved.state or {}).get("draft_order_id")
        if order_id:
            order = await session.get(Order, int(order_id))
            if order is not None and order.status == "Draft":
                await session.delete(order)
        saved.state = {}
        await session.commit()
    await update.effective_message.reply_text(
        "Unfinished order cleared.",
        reply_markup=reply_keyboard(
            update.effective_user.id in context.bot_data.get("admin_telegram_ids", set())
        ),
    )


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
        elif step == "party_add_name" and state.get("order_start") == "company_first":
            state["step"] = "party_search"
            await _persist_state(session, saved, state)
            await session.commit()
            await update.effective_message.reply_text(
                "Search for a party by name/code, or add a new party:",
                reply_markup=_party_buttons([], allow_add=True, allow_search=True),
            )
            return
        elif step == "party_search" and state.get("order_start") == "company_first":
            salesperson = await _salesperson(session, update.effective_user.id)
            brands = await accessible_brands(session, salesperson.id) if salesperson else []
            state["step"] = "brand_select"
            state["brand_choices"] = brands
            state.pop("brand", None)
            await _persist_state(session, saved, state)
            await session.commit()
            await update.effective_message.reply_text(
                "Previous step: choose a company / brand.",
                reply_markup=_brand_buttons(brands),
            )
            return
        else:
            message = "Use /order to select or search for a customer party."
        await _persist_state(session, saved, state)
        await session.commit()
    await update.effective_message.reply_text(message)


async def _submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _maybe_admin_input(update, context):
        return
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
    await update.effective_message.reply_text(
        f"Order {order.order_number} submitted for approval.",
        reply_markup=reply_keyboard(
            update.effective_user.id in context.bot_data.get("admin_telegram_ids", set())
        ),
    )
    from petgenics.admin_telegram import notify_admins_new_order

    await notify_admins_new_order(
        context.bot, order.id, context.bot_data.get("admin_telegram_ids", ())
    )


async def _text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _maybe_admin_input(update, context):
        return
    user_id = update.effective_user.id
    text = (update.effective_message.text or "").strip()
    async with SessionLocal() as session:
        salesperson = await _salesperson(session, user_id)
        if salesperson is None:
            if user_id in context.bot_data.get("admin_telegram_ids", set()):
                await update.effective_message.reply_text(
                    "No admin text action is waiting. Use /admin to open the admin panel."
                )
            else:
                await update.effective_message.reply_text(
                    "Your account is not approved. Contact your administrator."
                )
            return
        saved = await _saved_state(session, user_id)
        state = deepcopy(saved.state or {})
        search_index = state.get("text_order_search_index")
        if state.get("step") == "text_order_review" and search_index is not None:
            try:
                item_index = int(search_index)
                item = state["text_order_items"][item_index]
            except (ValueError, IndexError, KeyError):
                await update.effective_message.reply_text(
                    "That product search is no longer available. Discard the pasted order and try again."
                )
                return
            brands = set(await accessible_brands(session, salesperson.id))
            matching_products = list((await session.scalars(select(Product).where(
                Product.active.is_(True),
                Product.brand.in_(brands),
                (Product.name.ilike(f"%{text}%"))
                | (Product.product_code.ilike(f"%{text}%")),
            ).order_by(Product.name).limit(30))).all()) if brands else []
            ranked_products = _rank_text_matches(
                text,
                matching_products,
                lambda product: f"{product.name} {product.pack_size or ''} {product.product_code}",
            )
            item["candidates"] = [
                _text_product_candidate(product, score)
                for product, score in ranked_products
            ]
            item["selected"] = None
            state.pop("text_order_search_index", None)
            preview, can_confirm = _text_order_preview(state, salesperson)
            await _persist_state(session, saved, state)
            await session.commit()
            await update.effective_message.reply_text(
                preview if matching_products else (
                    f"No products matched “{text}”. Search another product name/code "
                    "or discard the pasted order."
                ),
                reply_markup=_text_order_keyboard(state, can_confirm),
            )
            return
        if "\n" in text and "+" in text:
            try:
                parsed_order = parse_text_order(text)
            except ValueError as exc:
                await update.effective_message.reply_text(str(exc))
                return
            draft_id = state.get("draft_order_id")
            if draft_id:
                current_draft = await session.get(Order, int(draft_id))
                if current_draft is not None and current_draft.status == "Draft":
                    await update.effective_message.reply_text(
                        "Finish or cancel your current order before starting a pasted text order."
                    )
                    return
            parties = await accessible_parties(session, salesperson.id)
            party_matches = _rank_text_matches(
                parsed_order["party_name"],
                parties,
                lambda party: f"{party.name} {party.party_code}",
                limit=8,
            )
            if not party_matches or party_matches[0][1] < 0.2:
                await update.effective_message.reply_text(
                    f"No assigned party resembles “{parsed_order['party_name']}”. "
                    "Ask an admin to assign the party, then send the text order again."
                )
                return
            state = {
                "step": "text_order_party",
                "pending_text_order": parsed_order,
                "text_order_party_candidates": [
                    {"id": party.id, "name": party.name, "code": party.party_code}
                    for party, _score in party_matches
                ],
            }
            await _persist_state(session, saved, state)
            await session.commit()
            markup = _buttons([
                (
                    f"{party.name[:40]} · {party.party_code[:14]}",
                    f"textorder:party:{party.id}",
                )
                for party, _score in party_matches
            ])
            await update.effective_message.reply_text(
                f"Choose the party matching “{parsed_order['party_name']}”:",
                reply_markup=markup,
            )
            return
        step = state.get("step")
        if step == "party_add_name":
            name = text.strip()
            if not name or len(name) > 300:
                await update.effective_message.reply_text(
                    "Party name must contain 1 to 300 characters. Send the name or use /cancel."
                )
                return
            if state.get("order_start") != "company_first" or not state.get("brand"):
                await update.effective_message.reply_text(
                    "Party creation is only available after choosing a company. Use /order to restart."
                )
                return
            brands = await accessible_brands(session, salesperson.id)
            if state["brand"] not in brands:
                await update.effective_message.reply_text(
                    "Your company assignment changed. Use /order to choose an authorized company."
                )
                return
            duplicate = await session.scalar(
                select(Party.id).where(func.lower(Party.name) == name.casefold())
            )
            if duplicate is not None:
                await update.effective_message.reply_text(
                    "A party with this name already exists. Ask your admin to assign it to your account."
                )
                return
            party_code = f"NEW-{uuid4().hex[:16].upper()}"
            party = Party(party_code=party_code, name=name, active=True)
            session.add(party)
            await session.flush()
            session.add(SalespersonParty(salesperson_id=salesperson.id, party_id=party.id))
            await session.flush()
            draft = await create_draft(session, user_id, party.id)
            state = {
                "draft_order_id": draft.id,
                "party_id": party.id,
                "party_name": party.name,
                "brand": state["brand"],
                "brand_choices": brands,
                "items": [],
                "step": "search",
                "order_start": "company_first",
            }
            await _persist_state(session, saved, state)
            await session.commit()
            await update.effective_message.reply_text(
                f"Party created and assigned to your account.\n"
                f"Party: {party.name}\nTemporary internal code: {party_code}\n"
                "Ask your admin to replace this with the actual ERP party code before MARG export.\n"
                f"Company: {state['brand']}\nSend a product code or name to search."
            )
            return
        if step == "party_search":
            parties = await accessible_parties(session, salesperson.id)
            matches = [
                party for party in parties
                if text.casefold() in party.name.casefold() or text.casefold() in party.party_code.casefold()
            ][:50]
            if not matches:
                can_add = state.get("order_start") == "company_first" and bool(state.get("brand"))
                await update.effective_message.reply_text(
                    "No assigned party matched. Try another name/code or add a new party.",
                    reply_markup=_party_buttons([], allow_add=can_add, allow_search=can_add),
                )
                return
            await update.effective_message.reply_text(
                "Select a matching party:",
                reply_markup=_party_buttons(
                    matches,
                    allow_add=state.get("order_start") == "company_first",
                    allow_search=state.get("order_start") == "company_first",
                ),
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
        if data.startswith("textorder:"):
            if data == "textorder:cancel":
                if state.get("step") not in {"text_order_party", "text_order_review"}:
                    await query.edit_message_text("There is no pasted text order to discard.")
                    return
                saved.state = {}
                await session.commit()
                await query.edit_message_text("Pasted text order discarded.")
                return

            if data.startswith("textorder:party:"):
                if state.get("step") != "text_order_party":
                    await query.edit_message_text("That pasted order is no longer waiting for party selection.")
                    return
                try:
                    party_id = int(data.rsplit(":", 1)[1])
                except ValueError:
                    await query.edit_message_text("Invalid party selection.")
                    return
                if party_id not in {
                    int(candidate["id"])
                    for candidate in state.get("text_order_party_candidates", [])
                }:
                    await query.edit_message_text("That party was not offered for this pasted order.")
                    return
                parties = await accessible_parties(session, salesperson.id)
                party = next((candidate for candidate in parties if candidate.id == party_id), None)
                if party is None:
                    await query.edit_message_text("This party is no longer assigned to you.")
                    return
                brands = set(await accessible_brands(session, salesperson.id))
                products = list((await session.scalars(select(Product).where(
                    Product.active.is_(True),
                    Product.brand.in_(brands),
                ))).all()) if brands else []
                pending = state["pending_text_order"]
                matched_items = []
                for item in pending["items"]:
                    ranked = _rank_text_matches(
                        item["product_query"],
                        products,
                        lambda product: (
                            f"{product.name} {product.pack_size or ''} {product.product_code}"
                        ),
                    )
                    candidates = [
                        _text_product_candidate(product, score)
                        for product, score in ranked if score >= 0.35
                    ]
                    selected = None
                    if candidates:
                        first_score = candidates[0]["confidence"]
                        second_score = candidates[1]["confidence"] if len(candidates) > 1 else 0
                        if first_score >= 0.95 and first_score - second_score >= 0.12:
                            selected = dict(candidates[0])
                    matched_items.append({**item, "candidates": candidates, "selected": selected})
                state["step"] = "text_order_review"
                state["text_order_party_id"] = party.id
                state["text_order_party_name"] = party.name
                state["text_order_items"] = matched_items
                state.pop("pending_text_order", None)
                state.pop("text_order_party_candidates", None)
                preview, can_confirm = _text_order_preview(state, salesperson)
                await _persist_state(session, saved, state)
                await session.commit()
                await query.edit_message_text(
                    preview,
                    reply_markup=_text_order_keyboard(state, can_confirm),
                )
                return

            if data.startswith("textorder:search:"):
                if state.get("step") != "text_order_review":
                    await query.edit_message_text("That pasted order is no longer waiting for product review.")
                    return
                try:
                    item_index = int(data.rsplit(":", 1)[1])
                    state["text_order_items"][item_index]
                except (ValueError, IndexError, KeyError):
                    await query.edit_message_text("That product search is no longer available.")
                    return
                state["text_order_search_index"] = item_index
                await _persist_state(session, saved, state)
                await session.commit()
                await query.edit_message_text(
                    f"Send a product name, pack size, or product code for line {item_index + 1}."
                )
                return

            if data.startswith("textorder:item:"):
                if state.get("step") != "text_order_review":
                    await query.edit_message_text("That pasted order is no longer waiting for product review.")
                    return
                try:
                    _, _, raw_index, raw_product_id = data.split(":")
                    item_index, product_id = int(raw_index), int(raw_product_id)
                    item = state["text_order_items"][item_index]
                    candidate = next(
                        match for match in item.get("candidates", [])
                        if int(match["id"]) == product_id
                    )
                except (ValueError, IndexError, KeyError, StopIteration):
                    await query.edit_message_text("That product match is no longer available.")
                    return
                product = await session.get(Product, product_id)
                allowed_brands = set(await accessible_brands(session, salesperson.id))
                if (
                    product is None
                    or not product.active
                    or product.brand not in allowed_brands
                    or product.product_code != candidate["code"]
                ):
                    await query.edit_message_text("That product is no longer available to your account.")
                    return
                item["selected"] = _text_product_candidate(
                    product, float(candidate.get("confidence", 1))
                )
                state.pop("text_order_search_index", None)
                preview, can_confirm = _text_order_preview(state, salesperson)
                await _persist_state(session, saved, state)
                await session.commit()
                await query.edit_message_text(
                    preview,
                    reply_markup=_text_order_keyboard(state, can_confirm),
                )
                return

            if data == "textorder:confirm":
                if state.get("step") != "text_order_review":
                    await query.edit_message_text("That pasted order is no longer waiting for confirmation.")
                    return
                product_ids = {
                    int(item["selected"]["id"])
                    for item in state.get("text_order_items", [])
                    if item.get("selected") is not None
                }
                products = {
                    product.id: product
                    for product in (await session.scalars(
                        select(Product)
                        .where(Product.id.in_(product_ids))
                        .order_by(Product.id)
                        .with_for_update()
                    )).all()
                } if product_ids else {}
                allowed_brands = set(await accessible_brands(session, salesperson.id))
                catalog_changed = False
                for item in state.get("text_order_items", []):
                    selected = item.get("selected")
                    if selected is None:
                        continue
                    product = products.get(int(selected["id"]))
                    if (
                        product is None
                        or not product.active
                        or product.brand not in allowed_brands
                        or product.product_code != selected["code"]
                    ):
                        await query.edit_message_text(
                            "A selected product is no longer available to your account. "
                            "Discard this pasted order and start again."
                        )
                        return
                    refreshed = _text_product_candidate(
                        product, float(selected.get("confidence", 1))
                    )
                    if refreshed != selected:
                        item["selected"] = refreshed
                        catalog_changed = True
                if catalog_changed:
                    preview, can_confirm = _text_order_preview(state, salesperson)
                    await _persist_state(session, saved, state)
                    await session.commit()
                    await query.edit_message_text(
                        "Catalog details changed since your last review. Check the updated details "
                        "and confirm again.\n\n" + preview,
                        reply_markup=_text_order_keyboard(state, can_confirm),
                    )
                    return
                _preview, can_confirm = _text_order_preview(state, salesperson)
                if not can_confirm:
                    await query.edit_message_text(
                        "This order is not ready. Match every product and ask an admin to enable "
                        "free quantities if needed."
                    )
                    return
                items = []
                for item in state["text_order_items"]:
                    selected = item["selected"]
                    items.append({
                        "product_id": int(selected["id"]),
                        "product_code": selected["code"],
                        "product_name": selected["name"],
                        "brand": selected["brand"],
                        "pack_size": selected["pack_size"],
                        "quantity": item["quantity"],
                        "free_quantity": item["free_quantity"],
                        "rate": selected["rate"],
                        "discount_percent": "0",
                    })
                try:
                    draft = await create_draft(
                        session, user_id, int(state["text_order_party_id"])
                    )
                    draft.draft_data = {"items": items}
                    order = await submit_draft(
                        session, user_id, draft.id, items
                    )
                except (ValueError, KeyError) as exc:
                    await query.edit_message_text(f"Order was not submitted: {exc}")
                    return
                saved.state = {}
                await session.commit()
                await query.edit_message_text(
                    f"Order {order.order_number} submitted for approval."
                )
                from petgenics.admin_telegram import notify_admins_new_order

                await notify_admins_new_order(
                    context.bot, order.id, context.bot_data.get("admin_telegram_ids", ())
                )
                return

        if data.startswith("party:"):
            if data == "party:search":
                if (
                    state.get("step") != "party_search"
                    or state.get("order_start") != "company_first"
                    or state.get("draft_order_id")
                    or not state.get("brand")
                ):
                    await query.edit_message_text("Start a new order and choose a company before searching parties.")
                    return
                brands = await accessible_brands(session, salesperson.id)
                if state["brand"] not in brands:
                    await query.edit_message_text("This company is no longer assigned to you.")
                    return
                await query.edit_message_text(
                    f"Search parties for {state['brand']}.\n"
                    "Type the party name or party code; matching results will appear."
                )
                return
            if data == "party:add":
                if (
                    state.get("step") != "party_search"
                    or state.get("order_start") != "company_first"
                    or state.get("draft_order_id")
                    or not state.get("brand")
                ):
                    await query.edit_message_text("Start a new order and choose a company before adding a party.")
                    return
                brands = await accessible_brands(session, salesperson.id)
                if state["brand"] not in brands:
                    await query.edit_message_text("This company is no longer assigned to you.")
                    return
                state["step"] = "party_add_name"
                await _persist_state(session, saved, state)
                await session.commit()
                await query.edit_message_text(
                    "Send the party name. A temporary internal code will be generated; "
                    "ask your admin to replace it with the ERP party code before MARG export."
                )
                return
            party_id = int(data.split(":", 1)[1])
            parties = await accessible_parties(session, salesperson.id)
            party = next((p for p in parties if p.id == party_id), None)
            if party is None:
                await query.edit_message_text("This party is not assigned to you.")
                return
            company_first = state.get("order_start") == "company_first"
            selected_brand = state.get("brand") if company_first else None
            if company_first and (
                state.get("step") != "party_search"
                or selected_brand not in await accessible_brands(session, salesperson.id)
            ):
                await query.edit_message_text("Choose an authorized company before selecting a party.")
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
            if selected_brand:
                state["brand"] = selected_brand
            if first_brand:
                state["brand"] = first_brand
            brands = await accessible_brands(session, salesperson.id)
            state["brand_choices"] = brands
            await _persist_state(session, saved, state)
            await session.commit()
            if selected_brand:
                await query.edit_message_text(
                    f"Company: {selected_brand}\nParty: {party.name}\n"
                    "Send a product code or name to search."
                )
                return
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
                reply_markup=_party_buttons(parties),
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
            if state.get("step") == "brand_select" and state.get("order_start") == "company_first":
                state["brand"] = brand
                state["step"] = "party_search"
                await _persist_state(session, saved, state)
                await session.commit()
                await query.edit_message_text(
                    f"Company: {brand}\nSearch for a party by name/code, or add a new party.",
                    reply_markup=_party_buttons([], allow_add=True, allow_search=True),
                )
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
    if await handle_admin_document(update, context):
        return
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


def build_bot(
    token: str,
    max_upload_bytes: int,
    scanner_retention_hours: int,
    admin_telegram_ids: tuple[int, ...] = (),
    report_utc_offset_minutes: int = 330,
) -> Application:
    application = Application.builder().token(token).build()
    application.bot_data["max_upload_bytes"] = max_upload_bytes
    application.bot_data["scanner_retention_hours"] = scanner_retention_hours
    application.bot_data["admin_telegram_ids"] = set(admin_telegram_ids)
    application.bot_data["report_utc_offset_minutes"] = report_utc_offset_minutes
    application.add_handler(CommandHandler("myid", my_id))
    application.add_handler(CommandHandler("admin", admin_menu))
    application.add_handler(CommandHandler("adminorder", admin_order))
    application.add_handler(CommandHandler("adminpermission", admin_permission))
    application.add_handler(CommandHandler("adminreport", admin_report))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin:"))
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
    button_pattern = "^(?:" + "|".join(re.escape(label) for label in ALL_BUTTON_LABELS) + ")$"
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(button_pattern), _handle_menu_button)
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _text))
    return application


async def _maybe_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    from petgenics.admin_telegram import handle_admin_text

    return await handle_admin_text(update, context)


async def _handle_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from petgenics.admin_telegram import admin_menu, admin_report

    message = update.effective_message
    action = MENU_ACTIONS.get((message.text or "").strip())
    context.user_data.pop("admin_input", None)
    context.user_data.pop("admin_import_kind", None)
    is_admin = update.effective_user.id in context.bot_data.get("admin_telegram_ids", set())
    if action == "admin":
        if not is_admin:
            await message.reply_text("Administrator access is required.")
            return
        await admin_menu(update, context)
    elif action == "report_today":
        if not is_admin:
            await message.reply_text("Administrator access is required.")
            return
        await admin_report(update, context)
    elif action == "new_order":
        await _new_order(update, context)
    elif action == "cart":
        await _cart(update, context)
    elif action == "switch_brand":
        await _switch_brand(update, context)
    elif action == "submit":
        await _submit(update, context)
    elif action == "back":
        await _back(update, context)
    elif action == "cancel":
        await _cancel(update, context)
    elif action == "help":
        await _help(update, context)
