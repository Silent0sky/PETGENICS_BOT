from __future__ import annotations

import io
import json
import textwrap
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from petgenics.db import SessionLocal
from petgenics.excel_io import (
    preview_workbook,
    product_template_bytes,
    read_mapped_rows,
    workbook_bytes,
)
from petgenics.imports import import_parties, import_products
from petgenics.marg import internal_report, order_rows
from petgenics.models import (
    ImportBatch,
    MargMapping,
    Order,
    OrderAudit,
    Party,
    Product,
    Salesperson,
    SalespersonBrand,
    SalespersonParty,
)


def _admin_ids(context: ContextTypes.DEFAULT_TYPE) -> set[int]:
    return context.bot_data.get("admin_telegram_ids", set())


def _is_admin(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    return user_id in _admin_ids(context)


def _keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=data) for label, data in row]
        for row in rows
    ])


def _menu() -> InlineKeyboardMarkup:
    return _keyboard([
        [("Review orders", "admin:orders"), ("Salesperson approvals", "admin:people")],
        [("Excel/PDF downloads", "admin:downloads"), ("Import products", "admin:import:products")],
        [("Import parties", "admin:import:parties"), ("Import assignments", "admin:import:assignments")],
        [("Recent import results", "admin:imports")],
    ])


async def admin_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        await update.effective_message.reply_text("Administrator access is required.")
        return
    if not context.args:
        report_date = date.today()
    elif len(context.args) == 1:
        try:
            report_date = date.fromisoformat(context.args[0])
        except ValueError:
            await update.effective_message.reply_text("Use /adminreport or /adminreport YYYY-MM-DD.")
            return
    else:
        await update.effective_message.reply_text("Use /adminreport or /adminreport YYYY-MM-DD.")
        return
    await _send_daily_report(_MessageQuery(update.effective_message), report_date)


async def admin_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        await update.effective_message.reply_text("Administrator access is required.")
        return
    if len(context.args) != 1:
        await update.effective_message.reply_text("Use /adminorder ORDER_NUMBER.")
        return
    async with SessionLocal() as session:
        order = await session.scalar(select(Order).where(Order.order_number == context.args[0]))
    if order is None:
        await update.effective_message.reply_text("Order not found.")
        return
    buttons = [
        ("PDF summary", f"admin:order:pdf:{order.id}"),
        ("Internal Excel", f"admin:order:xlsx:{order.id}"),
    ]
    if order.status in {"Approved", "Exported"}:
        buttons.append(("MARG Excel", f"admin:order:marg:{order.id}"))
    await update.effective_message.reply_text(
        f"{order.order_number} · {order.status}. Choose a file:",
        reply_markup=_keyboard([buttons]),
    )


async def admin_permission(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        await update.effective_message.reply_text("Administrator access is required.")
        return
    if len(context.args) != 4:
        await update.effective_message.reply_text(
            "Use /adminpermission TELEGRAM_ID discount_yes_no max_discount_percent free_quantity_yes_no\n"
            "Example: /adminpermission 123456789 yes 5 no"
        )
        return
    try:
        telegram_user_id = int(context.args[0])
        discount_permission = context.args[1].casefold()
        discount_text = context.args[2]
        free_permission = context.args[3].casefold()
        max_discount = Decimal(discount_text)
        if discount_permission not in {"yes", "no"} or free_permission not in {"yes", "no"}:
            raise ValueError
        if telegram_user_id <= 0 or not Decimal("0") <= max_discount <= Decimal("100"):
            raise ValueError
        if max_discount.as_tuple().exponent < -2:
            raise ValueError
        allow_discount = discount_permission == "yes"
        allow_free_quantity = free_permission == "yes"
    except (ValueError, ArithmeticError):
        await update.effective_message.reply_text(
            "Invalid values. Use a positive Telegram ID, yes/no permissions, and a discount from 0 to 100."
        )
        return
    async with SessionLocal() as session:
        person = await session.scalar(select(Salesperson).where(
            Salesperson.telegram_user_id == telegram_user_id
        ))
        if person is None:
            await update.effective_message.reply_text("Salesperson account not found; import assignments first.")
            return
        person.allow_discount = allow_discount
        person.max_discount_percent = max_discount if allow_discount else Decimal("0")
        person.allow_free_quantity = allow_free_quantity
        await session.commit()
        name = person.display_name
    await update.effective_message.reply_text(
        f"Updated {name}: discount {'allowed up to ' + str(max_discount) + '%' if allow_discount else 'not allowed'}; "
        f"free quantity {'allowed' if allow_free_quantity else 'not allowed'}."
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        f"Your Telegram user ID is: {update.effective_user.id}\n"
        "Keep this number private; your administrator can add it to the approved users."
    )


async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        if not _admin_ids(context):
            message = (
                "Telegram admin access is not configured yet. Ask the service owner to add "
                "ADMIN_TELEGRAM_IDS in Render Environment."
            )
        else:
            message = "This Telegram account is not configured as an administrator."
        await update.effective_message.reply_text(message)
        return
    await update.effective_message.reply_text("Petgenics admin menu:", reply_markup=_menu())


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    if not _is_admin(context, user_id):
        await query.edit_message_text("Administrator access is required.")
        return
    data = query.data or ""

    if data == "admin:menu":
        await query.edit_message_text("Petgenics admin menu:", reply_markup=_menu())
        return
    if data == "admin:orders":
        await _show_orders(query)
        return
    if data == "admin:people":
        await _show_salespersons(query)
        return
    if data == "admin:downloads":
        await query.edit_message_text(
            "Choose a download:",
            reply_markup=_keyboard([
                [("Today's orders Excel", "admin:daily")],
                [("Full products Excel", "admin:products"), ("Full parties Excel", "admin:parties")],
                [("Product template", "admin:template:products"), ("Party template", "admin:template:parties")],
                [("Assignments template", "admin:template:assignments")],
                [("Back to admin menu", "admin:menu")],
            ]),
        )
        return
    if data.startswith("admin:import:"):
        kind = data.rsplit(":", 1)[1]
        if kind not in {"products", "parties", "assignments"}:
            await query.edit_message_text("Unknown import type.")
            return
        context.user_data["admin_import_kind"] = kind
        await query.edit_message_text(
            f"Now upload the {kind} .xlsx file here. Use the template headers for the easiest import. "
            "The upload is processed in memory and is not saved as a file."
        )
        return
    if data.startswith("admin:template:"):
        await _send_template(query, data.rsplit(":", 1)[1])
        return
    if data == "admin:products":
        async with SessionLocal() as session:
            products = list((await session.scalars(
                select(Product).order_by(Product.brand, Product.name)
            )).all())
        content = workbook_bytes(
            ["product_code", "brand", "name", "family", "mrp", "pack_size",
             "selling_rate", "stock", "active", "source_details"],
            [[p.product_code, p.brand, p.name, p.family, p.mrp, p.pack_size,
              p.selling_rate, p.stock, p.active, json.dumps(p.source_details, ensure_ascii=False)]
             for p in products],
        )
        await _send_file(query, content, "petgenics-products.xlsx", f"{len(products)} product(s).")
        return
    if data == "admin:parties":
        async with SessionLocal() as session:
            parties = list((await session.scalars(select(Party).order_by(Party.name))).all())
        content = workbook_bytes(
            ["party_code", "name", "active"],
            [[party.party_code, party.name, party.active] for party in parties],
        )
        await _send_file(query, content, "petgenics-parties.xlsx", f"{len(parties)} party/parties.")
        return
    if data == "admin:daily":
        await _send_daily_report(query, date.today())
        return
    if data == "admin:imports":
        await _show_imports(query)
        return
    if data.startswith("admin:errors:"):
        await _send_import_errors(query, data)
        return
    if data.startswith("admin:person:"):
        await _change_salesperson(query, data)
        return
    if data.startswith("admin:order:"):
        await _handle_order_action(query, data)
        return
    await query.edit_message_text("That admin action is not available.")


async def _show_orders(query) -> None:
    async with SessionLocal() as session:
        orders = list((await session.scalars(
            select(Order).options(selectinload(Order.items))
            .order_by(Order.created_at.desc()).limit(10)
        )).all())
        details = []
        for order in orders:
            party = await session.get(Party, order.party_id)
            person = await session.get(Salesperson, order.salesperson_id)
            details.append(
                f"{order.order_number} · {order.status} · "
                f"{party.name if party else order.party_id} · "
                f"{person.display_name if person else order.salesperson_id}"
            )
    if not orders:
        await query.edit_message_text("No orders found.", reply_markup=_menu())
        return
    rows: list[list[tuple[str, str]]] = []
    for order in orders:
        if order.status == "Submitted":
            rows.append([
                (f"Approve {order.order_number}", f"admin:order:approve:{order.id}"),
                (f"Reject {order.order_number}", f"admin:order:reject:{order.id}"),
            ])
        rows.append([
            (f"PDF {order.order_number}", f"admin:order:pdf:{order.id}"),
            (f"Excel {order.order_number}", f"admin:order:xlsx:{order.id}"),
        ])
        if order.status in {"Approved", "Exported"}:
            rows.append([(f"MARG Excel {order.order_number}", f"admin:order:marg:{order.id}")])
    rows.append([("Back to admin menu", "admin:menu")])
    await query.edit_message_text(
        "Latest orders:\n" + "\n".join(details),
        reply_markup=_keyboard(rows),
    )


async def _show_salespersons(query) -> None:
    async with SessionLocal() as session:
        people = list((await session.scalars(
            select(Salesperson).order_by(Salesperson.active, Salesperson.display_name).limit(30)
        )).all())
    if not people:
        await query.edit_message_text(
            "No salesperson accounts yet. Import an assignments Excel first.",
            reply_markup=_menu(),
        )
        return
    rows = []
    labels = []
    for person in people:
        labels.append(
            f"{person.display_name} · ID {person.telegram_user_id} · "
            f"{'Active' if person.active else 'Needs approval'}"
        )
        verb = "deactivate" if person.active else "approve"
        title = "Deactivate" if person.active else "Approve"
        rows.append([(f"{title} {person.display_name[:18]}", f"admin:person:{verb}:{person.id}")])
    rows.append([("Back to admin menu", "admin:menu")])
    await query.edit_message_text("\n".join(labels), reply_markup=_keyboard(rows))


async def _change_salesperson(query, data: str) -> None:
    try:
        _, _, action, raw_id = data.split(":")
        person_id = int(raw_id)
    except (ValueError, TypeError):
        await query.edit_message_text("Invalid salesperson action.")
        return
    if action not in {"approve", "deactivate"}:
        await query.edit_message_text("Unknown salesperson action.")
        return
    async with SessionLocal() as session:
        person = await session.get(Salesperson, person_id)
        if person is None:
            await query.edit_message_text("Salesperson was not found.")
            return
        person.active = action == "approve"
        await session.commit()
        name = person.display_name
    await query.edit_message_text(
        f"{name} is now {'approved' if action == 'approve' else 'deactivated'}.",
        reply_markup=_menu(),
    )


async def _handle_order_action(query, data: str) -> None:
    try:
        _, _, action, raw_id = data.split(":")
        order_id = int(raw_id)
    except (ValueError, TypeError):
        await query.edit_message_text("Invalid order action.")
        return
    if action in {"approve", "reject"}:
        status = "Approved" if action == "approve" else "Rejected"
        async with SessionLocal() as session:
            order = await session.get(Order, order_id)
            if order is None:
                await query.edit_message_text("Order not found.")
                return
            if order.status != "Submitted":
                await query.edit_message_text(f"Order is already {order.status}; no change was made.")
                return
            order.status = status
            session.add(OrderAudit(
                order_id=order.id,
                actor=str(query.from_user.id),
                action=status.lower(),
                details={"source": "telegram_admin"},
            ))
            await session.commit()
            order_number = order.order_number
        await query.edit_message_text(f"Order {order_number} {status.lower()}.", reply_markup=_menu())
        return
    if action in {"pdf", "xlsx", "marg"}:
        await _send_order_export(query, order_id, action)
        return
    await query.edit_message_text("Unknown order action.")


async def _send_order_export(query, order_id: int, export_kind: str) -> None:
    async with SessionLocal() as session:
        order = await session.scalar(
            select(Order).where(Order.id == order_id).options(selectinload(Order.items))
        )
        if order is None:
            await query.edit_message_text("Order not found.")
            return
        party = await session.get(Party, order.party_id)
        person = await session.get(Salesperson, order.salesperson_id)
        if party is None or person is None:
            await query.edit_message_text("Order is missing its party or salesperson record.")
            return
        if export_kind == "xlsx":
            content = internal_report(order, party, person)
            filename = f"{order.order_number}-internal-report.xlsx"
            caption = f"Internal Excel report · {order.order_number}"
        elif export_kind == "pdf":
            content = _order_pdf(order, party, person)
            filename = f"{order.order_number}-summary.pdf"
            caption = f"PDF order summary · {order.order_number}"
        else:
            mapping = await session.get(MargMapping, 1)
            if mapping is None:
                await query.edit_message_text(
                    "MARG export needs the actual MARG template mapping first. "
                    "No column names have been assumed."
                )
                return
            if order.status not in {"Approved", "Exported"}:
                await query.edit_message_text("Only approved orders can be exported to MARG.")
                return
            try:
                content = order_rows(order, party, person, mapping.columns, mapping.required_fields)
            except ValueError as exc:
                await query.edit_message_text(f"MARG mapping is incomplete: {exc}")
                return
            filename = f"{order.order_number}-marg.xlsx"
            caption = f"MARG Excel · {order.order_number}"
            if order.status == "Approved":
                order.status = "Exported"
                session.add(OrderAudit(
                    order_id=order.id,
                    actor=str(query.from_user.id),
                    action="exported",
                    details={"format": "marg", "source": "telegram_admin"},
                ))
                await session.commit()
    await _send_file(query, content, filename, caption)


def _order_pdf(order: Order, party: Party, person: Salesperson) -> bytes:
    import pymupdf as fitz

    lines = [
        f"Order: {order.order_number}    Status: {order.status}",
        f"Date: {order.created_at.isoformat()}",
        f"Salesperson: {person.display_name}    Party: {party.party_code} - {party.name}",
        f"Brands: {order.brand}",
        "",
    ]
    for item in order.items:
        lines.append(
            f"{item.brand} | {item.product_code} | {item.product_name} | {item.pack_size or '-'} | "
            f"Qty {item.quantity} + {item.free_quantity} free | Rate {item.rate} | "
            f"Discount {item.discount_percent}% | Total {item.line_total}"
        )
    if order.remarks:
        lines.extend(["", f"Remarks: {order.remarks}"])
    document = fitz.open()
    page = document.new_page()
    font_candidates = [
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    font_file = next((candidate for candidate in font_candidates if candidate.is_file()), None)
    font_name = "helv"
    if font_file:
        page.insert_font(fontname="PetgenicsUnicode", fontfile=str(font_file))
        font_name = "PetgenicsUnicode"
    y = 55
    for line in lines:
        for wrapped_line in textwrap.wrap(line, width=88) or [""]:
            if y > 780:
                page = document.new_page()
                if font_file:
                    page.insert_font(fontname="PetgenicsUnicode", fontfile=str(font_file))
                y = 55
            page.insert_text((45, y), wrapped_line, fontsize=10, fontname=font_name)
            y += 16
    return document.tobytes()


async def _send_daily_report(query, report_date: date) -> None:
    start = datetime.combine(report_date, time.min, timezone.utc)
    end = start + timedelta(days=1)
    async with SessionLocal() as session:
        orders = list((await session.scalars(
            select(Order).where(Order.created_at >= start, Order.created_at < end)
            .options(selectinload(Order.items)).order_by(Order.created_at)
        )).all())
        rows = []
        for order in orders:
            party = await session.get(Party, order.party_id)
            person = await session.get(Salesperson, order.salesperson_id)
            for item in order.items:
                rows.append([
                    order.order_number, order.status, order.created_at.isoformat(),
                    person.display_name if person else "", party.party_code if party else "",
                    party.name if party else "", item.brand, item.product_code, item.product_name,
                    item.pack_size, item.quantity, item.free_quantity, item.rate,
                    item.discount_percent, item.line_total, order.remarks,
                ])
    headers = [
        "order_number", "status", "date", "salesperson", "party_code", "party", "brand",
        "product_code", "product", "pack", "quantity", "free_quantity", "rate",
        "discount_percent", "line_total", "remarks",
    ]
    content = workbook_bytes(headers, rows)
    await _send_file(
        query, content, f"petgenics-orders-{report_date.isoformat()}.xlsx",
        f"{len(orders)} order(s) on {report_date.isoformat()}.",
    )


async def _show_imports(query) -> None:
    async with SessionLocal() as session:
        batches = list((await session.scalars(
            select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(10)
        )).all())
    if not batches:
        await query.edit_message_text("No imports yet.", reply_markup=_menu())
        return
    rows = []
    lines = []
    for batch in batches:
        summary = batch.summary or {}
        lines.append(
            f"#{batch.id} {batch.kind}: created {summary.get('created', 0)}, "
            f"updated {summary.get('updated', 0)}, "
            f"assigned {summary.get('assigned', 0)}, failed {summary.get('failed', 0)}"
        )
        if batch.errors:
            rows.append([(f"Error report #{batch.id}", f"admin:errors:{batch.id}")])
    rows.append([("Back to admin menu", "admin:menu")])
    await query.edit_message_text("\n".join(lines), reply_markup=_keyboard(rows))


async def _send_import_errors(query, data: str) -> None:
    try:
        batch_id = int(data.rsplit(":", 1)[1])
    except ValueError:
        await query.edit_message_text("Invalid import report.")
        return
    async with SessionLocal() as session:
        batch = await session.get(ImportBatch, batch_id)
        if batch is None:
            await query.edit_message_text("Import report not found.")
            return
        errors = batch.errors or []
    content = workbook_bytes(
        ["row", "code", "error"],
        [[
            row.get("row"),
            row.get("product_code", row.get("party_code", row.get("telegram_user_id", ""))),
            row.get("error", ""),
        ] for row in errors],
    )
    await _send_file(query, content, f"import-{batch_id}-errors.xlsx", "Rows that need fixing.")


async def _send_template(query, kind: str) -> None:
    if kind == "products":
        content = product_template_bytes()
        filename = "petgenics-products-template.xlsx"
    elif kind == "parties":
        content = workbook_bytes(["party_code", "name"], [["", ""]])
        filename = "petgenics-parties-template.xlsx"
    elif kind == "assignments":
        content = workbook_bytes(
            ["telegram_user_id", "display_name", "party_code", "brand"],
            [["", "", "", ""]],
        )
        filename = "petgenics-assignments-template.xlsx"
    else:
        await query.edit_message_text("Unknown template.")
        return
    await _send_file(query, content, filename, "Fill this Excel and upload it using the matching import option.")


async def _send_file(query, content: bytes, filename: str, caption: str) -> None:
    await query.message.reply_document(
        document=io.BytesIO(content),
        filename=filename,
        caption=caption,
    )


async def handle_admin_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    kind = context.user_data.get("admin_import_kind")
    if not kind or not _is_admin(context, user_id):
        return False
    message = update.effective_message
    document = message.document
    if document is None:
        await message.reply_text("Please upload an .xlsx or .xls Excel workbook.")
        return True
    filename = document.file_name or "upload.xlsx"
    if Path(filename).suffix.lower() not in {".xlsx", ".xls"}:
        await message.reply_text("Please upload an .xlsx or .xls Excel workbook.")
        return True
    max_bytes = context.bot_data["max_upload_bytes"]
    if document.file_size and document.file_size > max_bytes:
        await message.reply_text("That Excel file exceeds the configured upload-size limit.")
        context.user_data.pop("admin_import_kind", None)
        return True
    telegram_file = await context.bot.get_file(document.file_id)
    content = bytes(await telegram_file.download_as_bytearray())
    if len(content) > max_bytes:
        await message.reply_text("That Excel file exceeds the configured upload-size limit.")
        context.user_data.pop("admin_import_kind", None)
        return True
    try:
        result = await _import_workbook(kind, content, filename)
    except (ValueError, OSError) as exc:
        await message.reply_text(f"Import failed: {exc}")
        return True
    finally:
        context.user_data.pop("admin_import_kind", None)
    await message.reply_text(
        "Import finished:\n"
        f"Created: {result.get('created', result.get('assigned', 0))}\n"
        f"Updated: {result.get('updated', 0)}\n"
        f"Skipped: {result.get('skipped', 0)}\n"
        f"Failed: {result.get('failed', 0)}"
    )
    errors = result.get("errors", [])
    if errors:
        error_bytes = workbook_bytes(
            ["row", "code", "error"],
            [[row.get("row"), row.get("product_code", row.get("party_code", row.get("telegram_user_id", ""))),
              row.get("error", "")] for row in errors],
        )
        await _send_file(
            _MessageQuery(message), error_bytes,
            f"import-{result.get('batch_id', 'errors')}-errors.xlsx",
            "Import rows that need fixing.",
        )
    return True


async def _import_workbook(kind: str, content: bytes, filename: str) -> dict:
    preview = preview_workbook(content, filename)
    aliases = _column_aliases(kind)
    mapping = {}
    targets = set()
    for source in preview["columns"]:
        target = aliases.get(" ".join(source.casefold().replace("_", " ").split()))
        if target is None:
            continue
        if target in targets:
            raise ValueError(f"More than one Excel column matches {target}; use the provided template.")
        targets.add(target)
        mapping[source] = target
    if kind == "products":
        if "product_code" not in mapping.values():
            raise ValueError("The Excel needs a product_code column. Download the product template from /admin.")
        async with SessionLocal() as session:
            return await import_products(
                session, content, filename, mapping, header_row=preview["header_row"]
            )
    if kind == "parties":
        if "party_code" not in mapping.values():
            raise ValueError("The Excel needs a party_code column. Download the party template from /admin.")
        async with SessionLocal() as session:
            return await import_parties(
                session, content, filename, mapping, header_row=preview["header_row"]
            )
    return await _import_assignments(content, filename, mapping, preview["header_row"])


def _column_aliases(kind: str) -> dict[str, str]:
    if kind == "products":
        fields = {
            "product_code": ("product_code", "product code", "products code", "code", "sku"),
            "name": ("name", "product name", "product", "description", "product description"),
            "brand": ("brand", "company", "brand/company"),
            "family": ("family", "product family"),
            "mrp": ("mrp", "mrp rs", "mrp (rs)"),
            "pack_size": ("pack_size", "pack size", "pack", "size"),
            "selling_rate": ("selling_rate", "selling rate", "rate", "your rate"),
            "stock": ("stock", "available stock", "quantity"),
        }
    elif kind == "parties":
        fields = {
            "party_code": ("party_code", "party code", "code"),
            "name": ("name", "party name", "customer name"),
        }
    else:
        fields = {
            "telegram_user_id": ("telegram_user_id", "telegram user id", "telegram id", "user id"),
            "display_name": ("display_name", "display name", "name"),
            "party_code": ("party_code", "party code"),
            "brand": ("brand", "company"),
        }
    return {
        " ".join(alias.casefold().replace("_", " ").split()): field
        for field, names in fields.items()
        for alias in names
    }


async def _import_assignments(
    content: bytes, filename: str, mapping: dict[str, str], header_row: int
) -> dict:
    if (
        "telegram_user_id" not in mapping.values()
        or not ({"party_code", "brand"} & set(mapping.values()))
    ):
        raise ValueError(
            "Assignments Excel must include telegram_user_id and at least party_code or brand."
        )
    rows = read_mapped_rows(content, filename, mapping, header_row=header_row)
    counts = {"assigned": 0, "skipped": 0, "failed": 0}
    errors = []
    successful_rows = []
    async with SessionLocal() as session:
        for row in rows:
            try:
                raw_id = Decimal(str(row.get("telegram_user_id", "")).strip())
                if raw_id <= 0 or raw_id != raw_id.to_integral_value():
                    raise ValueError("Telegram user ID must be a positive whole number.")
                party_code = str(row.get("party_code") or "").strip()
                brand = str(row.get("brand") or "").strip()
                name = str(row.get("display_name") or "").strip()
                if not party_code and not brand:
                    raise ValueError("Provide a party_code or brand for this salesperson.")
                if len(brand) > 200:
                    raise ValueError("Brand exceeds 200 characters.")
                if len(name) > 200:
                    raise ValueError("Display name exceeds 200 characters.")
                party = None
                if party_code:
                    party = await session.scalar(select(Party).where(
                        Party.party_code == party_code, Party.active.is_(True)
                    ))
                    if party is None:
                        raise ValueError(f"Active party code not found: {party_code}")
                if brand and await session.scalar(select(Product.id).where(
                    Product.brand == brand, Product.active.is_(True)
                ).limit(1)) is None:
                    raise ValueError(f"Active brand not found in product master: {brand}")
                person = await session.scalar(select(Salesperson).where(
                    Salesperson.telegram_user_id == int(raw_id)
                ))
                new_person = person is None
                if person is None:
                    if not name:
                        raise ValueError("display_name is required for a new Telegram user.")
                    person = Salesperson(telegram_user_id=int(raw_id), display_name=name, active=False)
                    session.add(person)
                    await session.flush()
                changed = new_person
                if name:
                    if name != person.display_name:
                        person.display_name = name
                        changed = True
                if party is not None and await session.scalar(select(SalespersonParty.id).where(
                    SalespersonParty.salesperson_id == person.id, SalespersonParty.party_id == party.id
                )) is None:
                    session.add(SalespersonParty(salesperson_id=person.id, party_id=party.id))
                    changed = True
                if brand and await session.scalar(select(SalespersonBrand.id).where(
                    SalespersonBrand.salesperson_id == person.id, SalespersonBrand.brand == brand
                )) is None:
                    session.add(SalespersonBrand(salesperson_id=person.id, brand=brand))
                    changed = True
                counts["assigned"] += 1 if changed else 0
                counts["skipped"] += 0 if changed else 1
                successful_rows.append({
                    "row": row.get("_row"),
                    "telegram_user_id": int(raw_id),
                    "action": "created_inactive" if new_person else ("assigned" if changed else "unchanged"),
                })
            except (ValueError, ArithmeticError) as exc:
                counts["failed"] += 1
                errors.append({
                    "row": row.get("_row"),
                    "telegram_user_id": row.get("telegram_user_id", ""),
                    "error": str(exc),
                })
        batch = ImportBatch(
            kind="assignments",
            filename=filename[:255],
            summary=counts,
            errors=errors,
            successful_rows=successful_rows,
        )
        session.add(batch)
        await session.commit()
    return {"batch_id": batch.id, **counts, "errors": errors}


class _MessageQuery:
    def __init__(self, message):
        self.message = message
