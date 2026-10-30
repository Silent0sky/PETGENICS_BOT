from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
import json

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import load_only, selectinload
from telegram.error import TelegramError

from petgenics.admin_ui import (
    inline_keyboard,
    period_range,
    report_day_bounds,
    report_timezone,
)
from petgenics.db import SessionLocal
from petgenics.excel_io import workbook_bytes
from petgenics.marg import combined_order_rows, validate_mapping
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
    ScannerRun,
)


def _report_date(offset_minutes: int) -> date:
    return datetime.now(timezone.utc).astimezone(report_timezone(offset_minutes)).date()


async def show_dashboard(query, offset_minutes: int) -> None:
    today = _report_date(offset_minutes)
    start, end = report_day_bounds(today, offset_minutes)
    month_start, _ = report_day_bounds(today.replace(day=1), offset_minutes)
    async with SessionLocal() as session:
        orders = list((await session.scalars(
            select(Order).where(Order.created_at >= month_start, Order.created_at < end)
            .options(selectinload(Order.items))
        )).all())
        counts = Counter(order.status for order in orders)
        today_orders = [order for order in orders if start <= order.created_at < end]
        today_total = sum(
            (item.line_total for order in today_orders for item in order.items), Decimal("0")
        )
        month_total = sum((item.line_total for order in orders for item in order.items), Decimal("0"))
        active_people = await session.scalar(select(func.count()).select_from(Salesperson).where(Salesperson.active.is_(True))) or 0
        inactive_people = await session.scalar(select(func.count()).select_from(Salesperson).where(Salesperson.active.is_(False))) or 0
        product_count = await session.scalar(select(func.count()).select_from(Product)) or 0
        active_products = await session.scalar(select(func.count()).select_from(Product).where(Product.active.is_(True))) or 0
        missing_rates = await session.scalar(select(func.count()).select_from(Product).where(Product.selling_rate.is_(None))) or 0
        party_count = await session.scalar(select(func.count()).select_from(Party)) or 0
        last_import = await session.scalar(
            select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(1)
        )
    status_text = ", ".join(f"{key}: {value}" for key, value in sorted(counts.items())) or "none"
    text = (
        f"Dashboard · {today.isoformat()} (UTC{offset_minutes / 60:+g})\n"
        f"Today's orders: {len(today_orders)} · total ₹{today_total:.2f}\n"
        f"Today's status: {', '.join(f'{key}: {sum(o.status == key for o in today_orders)}' for key in ('Submitted','Approved','Rejected','Exported','Draft'))}\n"
        f"This month: {len(orders)} orders · total ₹{month_total:.2f}\n"
        f"Month status: {status_text}\n\n"
        f"Salespersons: {active_people} active / {inactive_people} inactive\n"
        f"Products: {product_count} total / {active_products} active / {missing_rates} without rate\n"
        f"Parties: {party_count}\n"
        f"Last import: {last_import.kind} · {last_import.filename} · {last_import.created_at.isoformat()}"
        if last_import else
        f"Dashboard · {today.isoformat()} (UTC{offset_minutes / 60:+g})\n"
        f"Today's orders: {len(today_orders)} · total ₹{today_total:.2f}\n"
        f"Today's status: {', '.join(f'{key}: {sum(o.status == key for o in today_orders)}' for key in ('Submitted','Approved','Rejected','Exported','Draft'))}\n"
        f"This month: {len(orders)} orders · total ₹{month_total:.2f}\n"
        f"Month status: {status_text}\n\n"
        f"Salespersons: {active_people} active / {inactive_people} inactive\n"
        f"Products: {product_count} total / {active_products} active / {missing_rates} without rate\n"
        f"Parties: {party_count}\nLast import: none"
    )
    await query.edit_message_text(
        text[:3900],
        reply_markup=inline_keyboard([
            [("Orders", "admin:orders"), ("Downloads & reports", "admin:reports")],
            [("Admin menu", "admin:menu")],
        ]),
    )


async def show_report_menu(query) -> None:
    await query.edit_message_text(
        "Downloads and reports:",
        reply_markup=inline_keyboard([
            [("Today", "admin:report:today"), ("Yesterday", "admin:report:yesterday")],
            [("Last 7 days", "admin:report:last7"), ("This month", "admin:report:month")],
            [("Last month", "admin:report:lastmonth"), ("Custom dates", "admin:report:custom")],
            [("In-chat summary", "admin:summary:today"), ("Full products", "admin:export:products")],
            [("Full parties", "admin:export:parties"), ("Salespersons", "admin:export:people")],
            [("Assignments backup", "admin:export:assignments"), ("Audit log", "admin:report:audit")],
            [("Admin menu", "admin:menu")],
        ]),
    )


async def route_report_callback(update, context, data: str) -> bool:
    query = update.callback_query
    offset = context.bot_data.get("report_utc_offset_minutes", 330)
    if data == "admin:reports":
        await show_report_menu(query)
        return True
    if data.startswith("admin:report:"):
        action = data.rsplit(":", 1)[1]
        if action == "custom":
            context.user_data["admin_input"] = {"kind": "report_range"}
            await query.edit_message_text(
                "Send a date (YYYY-MM-DD or DD-MM-YYYY) or range "
                "(YYYY-MM-DD to YYYY-MM-DD). Use /cancel to stop."
            )
            return True
        if action == "audit":
            context.user_data["admin_input"] = {"kind": "audit_range"}
            await query.edit_message_text("Send the audit-log date range: YYYY-MM-DD to YYYY-MM-DD.")
            return True
        try:
            dates = period_range(action, _report_date(offset))
        except ValueError:
            await query.edit_message_text("Unknown report period.")
            return True
        await send_orders_export(query, *dates, offset)
        return True
    if data.startswith("admin:summary:"):
        period = data.rsplit(":", 1)[1]
        try:
            dates = period_range(period, _report_date(offset))
        except ValueError:
            await query.edit_message_text("Unknown report period.")
            return True
        await send_summary(query, *dates, offset)
        return True
    if data.startswith("admin:export:"):
        export_kind = data.rsplit(":", 1)[1]
        if export_kind not in {"products", "parties", "people", "assignments"}:
            await query.edit_message_text("Unknown export type.")
            return True
        await send_master_export(query, export_kind)
        return True
    if data == "admin:marg":
        await show_marg_settings(query)
        return True
    if data == "admin:marg:combined":
        await send_combined_marg(query)
        return True
    if data == "admin:scanner":
        await show_scanner_runs(query)
        return True
    if data.startswith("admin:scanner:"):
        await scanner_action(query, context, data)
        return True
    if data == "admin:status":
        await show_status(query, context)
        return True
    return False


async def send_orders_export(query, start_date: date, end_date: date, offset_minutes: int) -> None:
    start, _ = report_day_bounds(start_date, offset_minutes)
    _, end = report_day_bounds(end_date, offset_minutes)
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
    filename = f"petgenics-orders-{start_date.isoformat()}-to-{end_date.isoformat()}.xlsx"
    await _send_file(query, workbook_bytes(headers, rows), filename, f"{len(orders)} order(s).")


async def send_summary(query, start_date: date, end_date: date, offset_minutes: int) -> None:
    start, _ = report_day_bounds(start_date, offset_minutes)
    _, end = report_day_bounds(end_date, offset_minutes)
    async with SessionLocal() as session:
        orders = list((await session.scalars(
            select(Order).where(Order.created_at >= start, Order.created_at < end)
            .options(selectinload(Order.items)).order_by(Order.created_at)
        )).all())
        people: dict[str, Decimal] = defaultdict(Decimal)
        brands: dict[str, Decimal] = defaultdict(Decimal)
        parties: dict[str, Decimal] = defaultdict(Decimal)
        products: dict[str, Decimal] = defaultdict(Decimal)
        for order in orders:
            party = await session.get(Party, order.party_id)
            person = await session.get(Salesperson, order.salesperson_id)
            for item in order.items:
                people[person.display_name if person else "Unknown"] += item.line_total
                brands[item.brand] += item.line_total
                parties[party.name if party else "Unknown"] += item.line_total
                products[f"{item.product_code} · {item.product_name}"] += item.line_total
        total = sum((item.line_total for order in orders for item in order.items), Decimal("0"))
        counts = Counter(order.status for order in orders)
    sections = [
        f"Order summary · {start_date.isoformat()} to {end_date.isoformat()}",
        f"Orders: {len(orders)} · Total: ₹{total:.2f}",
        "By status: " + (", ".join(f"{status} {count}" for status, count in sorted(counts.items())) or "none"),
    ]
    for label, totals in (
        ("Salesperson", people), ("Brand", brands), ("Top parties", parties), ("Top products", products)
    ):
        sections.append(f"\n{label}:")
        sections.extend(f"{name}: ₹{amount:.2f}" for name, amount in sorted(
            totals.items(), key=lambda entry: entry[1], reverse=True
        )[:5])
    await query.edit_message_text(
        "\n".join(sections)[:3900],
        reply_markup=inline_keyboard([[("Downloads & reports", "admin:reports")]]),
    )


async def send_master_export(query, export_kind: str) -> None:
    async with SessionLocal() as session:
        if export_kind == "products":
            products = list((await session.scalars(select(Product).order_by(Product.brand, Product.name))).all())
            content = workbook_bytes(
                ["product_code", "brand", "name", "family", "mrp", "pack_size", "selling_rate", "stock", "active"],
                [[p.product_code, p.brand, p.name, p.family, p.mrp, p.pack_size, p.selling_rate, p.stock, p.active]
                 for p in products],
            )
            filename = "petgenics-products.xlsx"
            caption = f"{len(products)} product(s)."
        elif export_kind == "parties":
            parties = list((await session.scalars(select(Party).order_by(Party.name))).all())
            content = workbook_bytes(
                ["party_code", "name", "active"],
                [[p.party_code, p.name, p.active] for p in parties],
            )
            filename = "petgenics-parties.xlsx"
            caption = f"{len(parties)} party/parties."
        elif export_kind == "people":
            people = list((await session.scalars(select(Salesperson).order_by(Salesperson.display_name))).all())
            content = workbook_bytes(
                ["telegram_user_id", "display_name", "active", "allow_discount",
                 "max_discount_percent", "allow_free_quantity"],
                [[p.telegram_user_id, p.display_name, p.active, p.allow_discount,
                  p.max_discount_percent, p.allow_free_quantity] for p in people],
            )
            filename = "petgenics-salespersons.xlsx"
            caption = f"{len(people)} salesperson(s)."
        elif export_kind == "assignments":
            people = list((await session.scalars(select(Salesperson).order_by(Salesperson.display_name))).all())
            rows = []
            for person in people:
                party_codes = list((await session.scalars(
                    select(Party.party_code).join(SalespersonParty).where(
                        SalespersonParty.salesperson_id == person.id
                    )
                )).all())
                brands = list((await session.scalars(
                    select(SalespersonBrand.brand).where(SalespersonBrand.salesperson_id == person.id)
                )).all())
                if party_codes and brands:
                    links = [(code, brand) for code in party_codes for brand in brands]
                else:
                    links = [(code, "") for code in party_codes] + [("", brand) for brand in brands]
                if not links:
                    links = [("", "")]
                rows.extend([[person.telegram_user_id, person.display_name, code, brand] for code, brand in links])
            content = workbook_bytes(
                ["telegram_user_id", "display_name", "party_code", "brand"], rows
            )
            filename = "petgenics-assignments.xlsx"
            caption = f"Assignment backup for {len(people)} salesperson(s)."
        else:
            return
    await _send_file(query, content, filename, caption)


async def send_audit_export(query, start_date: date, end_date: date, offset_minutes: int) -> None:
    start, _ = report_day_bounds(start_date, offset_minutes)
    _, end = report_day_bounds(end_date, offset_minutes)
    async with SessionLocal() as session:
        events = list((await session.scalars(
            select(OrderAudit).where(OrderAudit.created_at >= start, OrderAudit.created_at < end)
            .order_by(OrderAudit.created_at).limit(10000)
        )).all())
    rows = [[event.order_id, event.created_at.isoformat(), event.actor, event.action,
             json.dumps(event.details, ensure_ascii=False)] for event in events]
    content = workbook_bytes(["order_id", "date", "actor", "action", "details"], rows)
    await _send_file(
        query, content, f"petgenics-audit-{start_date.isoformat()}-to-{end_date.isoformat()}.xlsx",
        f"{len(events)} audit event(s).",
    )


async def handle_report_text(update, context, value: str) -> bool:
    pending = context.user_data.get("admin_input")
    if not pending or pending.get("kind") not in {"report_range", "audit_range"}:
        return False
    from petgenics.admin_ui import parse_report_range

    try:
        start, end = parse_report_range(value)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc) + " Try again or send /cancel.")
        return True
    kind = pending["kind"]
    context.user_data.pop("admin_input", None)
    offset = context.bot_data.get("report_utc_offset_minutes", 330)
    query = _MessageQuery(update.effective_message)
    if kind == "audit_range":
        await send_audit_export(query, start, end, offset)
    else:
        await send_orders_export(query, start, end, offset)
    return True


async def show_marg_settings(query) -> None:
    async with SessionLocal() as session:
        mapping = await session.get(MargMapping, 1)
    if mapping is None:
        text = "MARG template mapping is not configured. Upload the real ERP template and map its columns; no field names are assumed."
    else:
        text = (
            f"MARG mapping configured\nColumns: {json.dumps(mapping.columns, ensure_ascii=False)}\n"
            f"Required: {', '.join(mapping.required_fields)}\n"
            f"Combined export: {'enabled' if mapping.combined_allowed else 'disabled'}"
        )
    await query.edit_message_text(
        text[:3900],
        reply_markup=inline_keyboard([
            [("Set mapping JSON", "admin:marg:set"), ("Toggle combined", "admin:marg:toggle")],
            [("Combined approved export", "admin:marg:combined")],
            [("Downloads & reports", "admin:reports")],
        ]),
    )


async def handle_marg_callback(query, context, data: str) -> bool:
    if data == "admin:marg:set":
        context.user_data["admin_input"] = {"kind": "marg_mapping"}
        await query.edit_message_text(
            'Send JSON: {"columns":{"ERP Party":"party_code"},"required_fields":["party_code"],"combined_allowed":false}'
        )
        return True
    if data == "admin:marg:toggle":
        async with SessionLocal() as session:
            mapping = await session.get(MargMapping, 1)
            if mapping is None:
                await query.edit_message_text("Set the actual MARG mapping first.")
                return True
            mapping.combined_allowed = not mapping.combined_allowed
            await session.commit()
        await show_marg_settings(query)
        return True
    if data == "admin:marg:combined":
        await send_combined_marg(query)
        return True
    return False


async def handle_marg_text(update, context, value: str) -> bool:
    pending = context.user_data.get("admin_input")
    if not pending or pending.get("kind") != "marg_mapping":
        return False
    try:
        payload = json.loads(value)
        if not isinstance(payload, dict) or not isinstance(payload.get("columns"), dict):
            raise ValueError("Provide a JSON object with a columns object.")
        columns = payload["columns"]
        required = payload.get("required_fields", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise ValueError("required_fields must be a list of field names.")
        validate_mapping(columns, required)
        combined = payload.get("combined_allowed", False)
        if not isinstance(combined, bool):
            raise ValueError("combined_allowed must be true or false.")
    except (json.JSONDecodeError, ValueError) as exc:
        await update.effective_message.reply_text(f"Invalid mapping: {exc}")
        return True
    async with SessionLocal() as session:
        mapping = await session.get(MargMapping, 1)
        if mapping is None:
            mapping = MargMapping(id=1, columns=columns, required_fields=required)
            session.add(mapping)
        mapping.columns = columns
        mapping.required_fields = required
        mapping.combined_allowed = combined
        await session.commit()
    context.user_data.pop("admin_input", None)
    await update.effective_message.reply_text("MARG mapping saved.")
    return True


async def send_combined_marg(query) -> None:
    async with SessionLocal() as session:
        mapping = await session.get(MargMapping, 1)
        if mapping is None or not mapping.combined_allowed:
            await query.edit_message_text("Combined MARG export is not enabled in the configured mapping.")
            return
        orders = list((await session.scalars(
            select(Order).where(Order.status == "Approved")
            .options(selectinload(Order.items)).order_by(Order.created_at).limit(101)
        )).all())
        if not orders:
            await query.edit_message_text("No approved orders are ready for export.")
            return
        if len(orders) > 100:
            await query.edit_message_text("More than 100 approved orders exist. Export them in smaller batches.")
            return
        entries = []
        for order in orders:
            party = await session.get(Party, order.party_id)
            person = await session.get(Salesperson, order.salesperson_id)
            if party is None or person is None:
                await query.edit_message_text("An order is missing party or salesperson information.")
                return
            entries.append((order, party, person))
        try:
            content = combined_order_rows(entries, mapping.columns, mapping.required_fields)
        except ValueError as exc:
            await query.edit_message_text(f"MARG mapping is incomplete: {exc}")
            return
        for order in orders:
            order.status = "Exported"
            session.add(OrderAudit(
                order_id=order.id,
                actor=str(query.from_user.id),
                action="exported",
                details={"format": "marg-combined", "source": "telegram_admin"},
            ))
        await session.commit()
    await _send_file(query, content, "petgenics-marg-combined.xlsx", f"{len(orders)} order(s).")


async def show_scanner_runs(query) -> None:
    async with SessionLocal() as session:
        results = list((await session.execute(
            select(ScannerRun, ScannerRun.document_bytes.is_not(None).label("has_document"))
            .options(load_only(
                ScannerRun.id, ScannerRun.telegram_user_id, ScannerRun.filename,
                ScannerRun.expires_at, ScannerRun.created_at,
            ))
            .order_by(ScannerRun.created_at.desc()).limit(8)
        )).all())
    if not results:
        await query.edit_message_text("No scanner uploads.", reply_markup=inline_keyboard([[("Admin menu", "admin:menu")]]))
        return
    lines = []
    rows = []
    for run, has_document in results:
        stored = "file stored" if has_document else "metadata only"
        lines.append(
            f"#{run.id} · user {run.telegram_user_id} · {run.filename} · "
            f"{run.created_at.isoformat()} · expires {run.expires_at.isoformat()} · {stored}"
        )
        rows.append([("Delete file " + str(run.id), f"admin:scanner:delete:{run.id}")])
    rows.append([("Purge expired files", "admin:scanner:purge:confirm"), ("Admin menu", "admin:menu")])
    await query.edit_message_text("\n".join(lines)[:3500], reply_markup=inline_keyboard(rows))


async def scanner_action(query, context, data: str) -> None:
    if data == "admin:scanner:purge:confirm":
        await query.edit_message_text(
            "Purge expired scanner files?",
            reply_markup=inline_keyboard([[
                ("Confirm purge", "admin:scanner:purge:yes"),
                ("Cancel", "admin:scanner"),
            ]]),
        )
        return
    if data == "admin:scanner:purge:yes":
        async with SessionLocal() as session:
            result = await session.execute(
                update(ScannerRun).where(
                    ScannerRun.document_bytes.is_not(None),
                    ScannerRun.expires_at <= datetime.now(timezone.utc),
                ).values(document_bytes=None, result={"expired": True})
            )
            await session.commit()
        await query.edit_message_text(f"Removed {result.rowcount or 0} expired scanner file(s).")
        return
    if data.startswith("admin:scanner:delete:confirm:"):
        try:
            run_id = int(data.rsplit(":", 1)[1])
        except ValueError:
            await query.edit_message_text("Invalid scanner upload.")
            return
        if context.user_data.pop("admin_scanner_delete_id", None) != run_id:
            await query.edit_message_text("That confirmation expired.")
            return
        async with SessionLocal() as session:
            result = await session.execute(
                update(ScannerRun).where(ScannerRun.id == run_id)
                .values(document_bytes=None, result={"discarded": True})
            )
            await session.commit()
        await query.edit_message_text(
            "Scanner file deleted." if result.rowcount else "Scanner upload not found."
        )
        return
    try:
        run_id = int(data.rsplit(":", 1)[1])
    except ValueError:
        await query.edit_message_text("Invalid scanner upload.")
        return
    context.user_data["admin_scanner_delete_id"] = run_id
    await query.edit_message_text(
        f"Delete scanner document #{run_id}?",
        reply_markup=inline_keyboard([[
            ("Confirm delete", f"admin:scanner:delete:confirm:{run_id}"),
            ("Cancel", "admin:scanner"),
        ]]),
    )


async def show_status(query, context) -> None:
    from petgenics.db import engine

    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql("SELECT 1")
        db_status = "connected"
    except SQLAlchemyError:
        db_status = "unavailable"
    async with SessionLocal() as session:
        counts = {}
        for name, model in (
            ("orders", Order), ("products", Product), ("parties", Party),
            ("salespersons", Salesperson), ("scanner uploads", ScannerRun),
        ):
            counts[name] = await session.scalar(select(func.count()).select_from(model)) or 0
    try:
        info = await context.bot.get_webhook_info()
        pending_updates = info.pending_update_count
        last_error = info.last_error_message
    except TelegramError:
        pending_updates = "unavailable"
        last_error = "unavailable"
    text = (
        "Status & help\n"
        f"Admin IDs: {', '.join(str(item) for item in sorted(context.bot_data.get('admin_telegram_ids', set())))}\n"
        "Add/remove admins in Render's ADMIN_TELEGRAM_IDS environment setting.\n"
        f"Database: {db_status}\n"
        + "\n".join(f"{name}: {count}" for name, count in counts.items())
        + f"\nWebhook pending updates: {pending_updates}"
        + f"\nWebhook last error: {last_error or 'none'}"
    )
    await query.edit_message_text(
        text[:3900],
        reply_markup=inline_keyboard([[("Admin menu", "admin:menu")]]),
    )


async def _send_file(query, content: bytes, filename: str, caption: str) -> None:
    import io

    await query.message.reply_document(
        document=io.BytesIO(content), filename=filename, caption=caption
    )


class _MessageQuery:
    def __init__(self, message):
        self.message = message
