from __future__ import annotations

import hmac
import json
import asyncio
import logging
import textwrap
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import load_only, selectinload
from telegram import Update

from petgenics.config import Settings, get_settings
from petgenics.excel_io import product_template_bytes, read_mapped_rows, workbook_bytes
from petgenics.imports import import_parties, import_products
from petgenics.marg import combined_order_rows, order_rows, validate_mapping
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

logger = logging.getLogger(__name__)


class SalespersonRequest(BaseModel):
    telegram_user_id: int = Field(gt=0)
    display_name: str = Field(min_length=1, max_length=200)
    active: bool = True
    allow_discount: bool = False
    allow_free_quantity: bool = False
    max_discount_percent: Decimal = Field(default=Decimal("0"), ge=0, le=100, max_digits=5, decimal_places=2)
    brands: list[str] = Field(default_factory=list)
    party_codes: list[str] = Field(default_factory=list)


class OrderDecision(BaseModel):
    status: str
    remarks: str | None = None


class SalespersonApproval(BaseModel):
    active: bool


class MargConfig(BaseModel):
    columns: dict[str, str]
    required_fields: list[str]
    combined_allowed: bool = False


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from petgenics.db import SessionLocal, engine

        if not settings.admin_api_key:
            raise RuntimeError("ADMIN_API_KEY must be configured before starting the service.")
        if not settings.health_check_token:
            raise RuntimeError("HEALTH_CHECK_TOKEN must be configured before starting the service.")
        async with engine.connect() as connection:
            await connection.exec_driver_sql("SELECT 1")
        app.state.bot = None
        if settings.bot_token:
            if not settings.webhook_secret or len(settings.webhook_secret) < 16:
                raise RuntimeError("WEBHOOK_SECRET must be at least 16 characters when BOT_TOKEN is configured.")
            if not settings.webhook_url:
                raise RuntimeError("WEBHOOK_URL or RENDER_EXTERNAL_URL is required when BOT_TOKEN is configured.")
            from petgenics.telegram_bot import build_bot

            bot = build_bot(
                settings.bot_token,
                settings.max_upload_mb * 1024 * 1024,
                settings.scanner_retention_hours,
                settings.admin_telegram_ids,
            )
            await bot.initialize()
            await bot.start()
            app.state.bot = bot
            url = settings.webhook_url.rstrip("/") + f"/webhook/{settings.webhook_secret}"
            await bot.bot.set_webhook(
                url=url,
                secret_token=settings.webhook_secret,
                allowed_updates=Update.ALL_TYPES,
            )
        await _clean_expired_scans()
        cleanup_task = asyncio.create_task(_scanner_cleanup_loop())
        app.state.scanner_cleanup_task = cleanup_task
        yield
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        if app.state.bot is not None:
            await app.state.bot.stop()
            await app.state.bot.shutdown()
        await engine.dispose()

    app = FastAPI(title="Petgenics Order Bot", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings

    async def admin_auth(x_admin_key: str | None = Header(default=None)) -> None:
        if not settings.admin_api_key or not x_admin_key or not hmac.compare_digest(x_admin_key, settings.admin_api_key):
            raise HTTPException(status_code=401, detail="Invalid or missing admin key.")

    def database():
        from petgenics.db import SessionLocal

        return SessionLocal()

    @app.get("/healthz")
    async def liveness():
        return {"status": "ok"}

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_dashboard():
        dashboard = Path(__file__).with_name("dashboard.html")
        return HTMLResponse(
            dashboard.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/health")
    async def protected_health(x_health_token: str | None = Header(default=None)):
        if not settings.health_check_token or not x_health_token or not hmac.compare_digest(
            x_health_token, settings.health_check_token
        ):
            raise HTTPException(status_code=401, detail="Invalid or missing health-check token.")
        from petgenics.db import engine

        try:
            async with engine.connect() as connection:
                await connection.exec_driver_sql("SELECT 1")
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Database unavailable.") from exc
        return {"status": "ok", "database": "connected", "telegram": bool(settings.bot_token)}

    @app.post("/webhook/{secret}")
    async def telegram_webhook(secret: str, request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
        bot = request.app.state.bot
        if (
            bot is None
            or not settings.webhook_secret
            or not hmac.compare_digest(secret, settings.webhook_secret)
            or not x_telegram_bot_api_secret_token
            or not hmac.compare_digest(x_telegram_bot_api_secret_token, settings.webhook_secret)
        ):
            raise HTTPException(status_code=403, detail="Invalid webhook.")
        try:
            payload = await request.json()
            update = Update.de_json(payload, bot.bot)
            await bot.process_update(update)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="Invalid Telegram update.") from exc
        return {"ok": True}

    @app.post("/admin/imports/preview", dependencies=[Depends(admin_auth)])
    async def import_preview(
        file: UploadFile = File(...), sheet: int = Form(0), header_row: int | None = Form(default=None)
    ):
        from petgenics.excel_io import preview_workbook

        content = await _read_upload(file, settings.max_upload_mb)
        try:
            return preview_workbook(content, file.filename or "upload.xlsx", sheet, header_row)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/admin/imports/products", dependencies=[Depends(admin_auth)])
    async def products_import(
        file: UploadFile = File(...),
        mapping: str = Form(...),
        brand_override: str | None = Form(default=None),
        sheet: int = Form(0),
        header_row: int | None = Form(default=None),
    ):
        try:
            mapping_data = json.loads(mapping)
            if not isinstance(mapping_data, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in mapping_data.items()
            ):
                raise ValueError("Mapping must be a JSON object of Excel headers to product fields.")
            content = await _read_upload(file, settings.max_upload_mb)
            async with database() as session:
                result = await import_products(
                    session, content, file.filename or "products.xlsx", mapping_data, brand_override, sheet, header_row
                )
            return result
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/admin/imports/parties", dependencies=[Depends(admin_auth)])
    async def parties_import(
        file: UploadFile = File(...),
        mapping: str = Form(...),
        sheet: int = Form(0),
        header_row: int | None = Form(default=None),
    ):
        try:
            mapping_data = json.loads(mapping)
            if not isinstance(mapping_data, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in mapping_data.items()
            ):
                raise ValueError("Mapping must be a JSON object of Excel headers to party fields.")
            content = await _read_upload(file, settings.max_upload_mb)
            async with database() as session:
                return await import_parties(
                    session, content, file.filename or "parties.xlsx", mapping_data, sheet, header_row
                )
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/admin/imports/assignments", dependencies=[Depends(admin_auth)])
    async def assignments_import(
        file: UploadFile = File(...),
        mapping: str = Form(...),
        header_row: int | None = Form(default=None),
    ):
        try:
            mapping_data = json.loads(mapping)
            allowed = {"telegram_user_id", "display_name", "party_code", "brand"}
            if (
                not isinstance(mapping_data, dict)
                or not all(isinstance(key, str) and isinstance(value, str) for key, value in mapping_data.items())
                or not {"telegram_user_id"}.issubset(mapping_data.values())
                or not set(mapping_data.values()).issubset(allowed)
                or not ({"party_code", "brand"} & set(mapping_data.values()))
            ):
                raise ValueError("Map Telegram user ID and at least one of party_code or brand.")
            content = await _read_upload(file, settings.max_upload_mb)
            rows = read_mapped_rows(content, file.filename or "assignments.xlsx", mapping_data, header_row=header_row)
            counts = {"assigned": 0, "skipped": 0, "failed": 0}
            errors = []
            successful_rows = []
            pending_party_links: set[tuple[int, int]] = set()
            pending_brand_links: set[tuple[int, str]] = set()
            from petgenics.db import SessionLocal

            async with SessionLocal() as session:
                for item in rows:
                    row_number = item["_row"]
                    try:
                        raw_user_id = Decimal(str(item.get("telegram_user_id", "")).strip())
                        if raw_user_id <= 0 or raw_user_id != raw_user_id.to_integral_value():
                            raise ValueError("Telegram user ID must be a positive whole number.")
                        party_code = str(item.get("party_code") or "").strip()
                        party = None
                        if party_code:
                            party = await session.scalar(select(Party).where(Party.party_code == party_code))
                            if party is None or not party.active:
                                raise ValueError(f"Active party code not found: {party_code}")
                        brand = str(item.get("brand") or "").strip()
                        if brand:
                            exists = await session.scalar(select(Product.id).where(
                                Product.brand == brand, Product.active.is_(True)
                            ).limit(1))
                            if exists is None:
                                raise ValueError(f"Active brand not found in product master: {brand}")
                        person = await session.scalar(select(Salesperson).where(
                            Salesperson.telegram_user_id == int(raw_user_id)
                        ))
                        changed = person is None
                        if person is None:
                            display_name = str(item.get("display_name") or "").strip()
                            if not display_name:
                                raise ValueError("Display name is required when importing a new Telegram account.")
                            person = Salesperson(
                                telegram_user_id=int(raw_user_id),
                                display_name=display_name,
                                active=False,
                            )
                            session.add(person)
                            await session.flush()
                        if item.get("display_name"):
                            display_name = str(item["display_name"]).strip()
                            if display_name and display_name != person.display_name:
                                person.display_name = display_name
                                changed = True
                        if party is not None:
                            key = (person.id, party.id)
                            existing = await session.scalar(select(SalespersonParty.id).where(
                                SalespersonParty.salesperson_id == person.id,
                                SalespersonParty.party_id == party.id,
                            ))
                            if existing is None and key not in pending_party_links:
                                session.add(SalespersonParty(salesperson_id=person.id, party_id=party.id))
                                pending_party_links.add(key)
                                changed = True
                        if brand:
                            key = (person.id, brand)
                            existing = await session.scalar(select(SalespersonBrand.id).where(
                                SalespersonBrand.salesperson_id == person.id,
                                SalespersonBrand.brand == brand,
                            ))
                            if existing is None and key not in pending_brand_links:
                                session.add(SalespersonBrand(salesperson_id=person.id, brand=brand))
                                pending_brand_links.add(key)
                                changed = True
                        successful_rows.append({
                            "row": row_number,
                            "telegram_user_id": int(raw_user_id),
                            "action": "assigned" if changed else "unchanged",
                        })
                        counts["assigned" if changed else "skipped"] += 1
                    except (ValueError, InvalidOperation) as exc:
                        counts["failed"] += 1
                        errors.append({
                            "row": row_number,
                            "telegram_user_id": str(item.get("telegram_user_id", "")),
                            "error": str(exc),
                        })
                batch = ImportBatch(
                    kind="assignments",
                    filename=(file.filename or "assignments.xlsx")[:255],
                    summary=counts,
                    errors=errors,
                    successful_rows=successful_rows,
                )
                session.add(batch)
                await session.commit()
                return {"batch_id": batch.id, **counts, "successful_rows": successful_rows, "errors": errors}
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/admin/imports", dependencies=[Depends(admin_auth)])
    async def import_logs(limit: int = Query(default=50, ge=1, le=200)):
        from petgenics.db import SessionLocal

        async with SessionLocal() as session:
            rows = list((await session.scalars(
                select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(limit)
            )).all())
            return [{
                "id": row.id, "kind": row.kind, "filename": row.filename,
                "summary": row.summary, "successful_rows": row.successful_rows,
                "errors": row.errors, "created_at": row.created_at.isoformat(),
            } for row in rows]

    @app.get("/admin/imports/{batch_id}/errors.xlsx", dependencies=[Depends(admin_auth)])
    async def import_errors(batch_id: int):
        from petgenics.db import SessionLocal

        async with SessionLocal() as session:
            batch = await session.get(ImportBatch, batch_id)
            if batch is None:
                raise HTTPException(status_code=404, detail="Import batch not found.")
        content = workbook_bytes(["row", "code", "error"], [
            [
                error.get("row"),
                error.get("product_code", error.get("party_code", error.get("telegram_user_id", ""))),
                error.get("error", ""),
            ]
            for error in batch.errors
        ])
        return _xlsx_response(content, f"import-{batch_id}-errors.xlsx")

    @app.get("/admin/products/template.xlsx", dependencies=[Depends(admin_auth)])
    async def products_template():
        return _xlsx_response(product_template_bytes(), "petgenics-products-template.xlsx")

    @app.get("/admin/parties/template.xlsx", dependencies=[Depends(admin_auth)])
    async def parties_template():
        return _xlsx_response(
            workbook_bytes(["party_code", "name"], [["", ""]]),
            "petgenics-parties-template.xlsx",
        )

    @app.get("/admin/products.xlsx", dependencies=[Depends(admin_auth)])
    async def products_export():
        from petgenics.db import SessionLocal

        async with SessionLocal() as session:
            products = list((await session.scalars(select(Product).order_by(Product.brand, Product.name))).all())
        content = workbook_bytes(
            [
                "product_code", "brand", "name", "family", "mrp", "pack_size",
                "selling_rate", "stock", "active", "source_details",
            ],
            [[
                p.product_code, p.brand, p.name, p.family, p.mrp, p.pack_size,
                p.selling_rate, p.stock, p.active, json.dumps(p.source_details, ensure_ascii=False),
            ] for p in products],
        )
        return _xlsx_response(content, "petgenics-products.xlsx")

    @app.get("/admin/parties.xlsx", dependencies=[Depends(admin_auth)])
    async def parties_export():
        from petgenics.db import SessionLocal

        async with SessionLocal() as session:
            parties = list((await session.scalars(select(Party).order_by(Party.name))).all())
        content = workbook_bytes(
            ["party_code", "name", "active"],
            [[party.party_code, party.name, party.active] for party in parties],
        )
        return _xlsx_response(content, "petgenics-parties.xlsx")

    @app.put("/admin/salespersons", dependencies=[Depends(admin_auth)])
    async def manage_salesperson(data: SalespersonRequest):
        if data.max_discount_percent < 0 or data.max_discount_percent > 100:
            raise HTTPException(status_code=422, detail="Maximum discount must be between 0 and 100.")
        from petgenics.db import SessionLocal

        async with SessionLocal() as session:
            unique_brands = list(dict.fromkeys(value.strip() for value in data.brands if value.strip()))
            unique_codes = list(dict.fromkeys(value.strip() for value in data.party_codes if value.strip()))
            unknown_brands = []
            for brand in unique_brands:
                if await session.scalar(select(Product.id).where(Product.brand == brand, Product.active.is_(True)).limit(1)) is None:
                    unknown_brands.append(brand)
            parties = list((await session.scalars(
                select(Party).where(Party.party_code.in_(unique_codes), Party.active.is_(True))
            )).all()) if unique_codes else []
            found_codes = {party.party_code for party in parties}
            unknown_parties = sorted(set(unique_codes) - found_codes)
            if unknown_brands or unknown_parties:
                raise HTTPException(
                    status_code=422,
                    detail={"unknown_brands": unknown_brands, "unknown_party_codes": unknown_parties},
                )
            person = await session.scalar(
                select(Salesperson).where(Salesperson.telegram_user_id == data.telegram_user_id)
            )
            if person is None:
                person = Salesperson(telegram_user_id=data.telegram_user_id, display_name=data.display_name)
                session.add(person)
                await session.flush()
            person.display_name = data.display_name
            person.active = data.active
            person.allow_discount = data.allow_discount
            person.allow_free_quantity = data.allow_free_quantity
            person.max_discount_percent = data.max_discount_percent if data.allow_discount else Decimal("0")
            await session.execute(
                SalespersonBrand.__table__.delete().where(SalespersonBrand.salesperson_id == person.id)
            )
            await session.execute(
                SalespersonParty.__table__.delete().where(SalespersonParty.salesperson_id == person.id)
            )
            for brand in unique_brands:
                session.add(SalespersonBrand(salesperson_id=person.id, brand=brand))
            for party in parties:
                session.add(SalespersonParty(salesperson_id=person.id, party_id=party.id))
            await session.commit()
            return {"telegram_user_id": person.telegram_user_id, "active": person.active}

    @app.get("/admin/salespersons", dependencies=[Depends(admin_auth)])
    async def list_salespersons():
        from petgenics.db import SessionLocal

        async with SessionLocal() as session:
            people = list((await session.scalars(select(Salesperson).order_by(Salesperson.display_name))).all())
            result = []
            for person in people:
                brands = list((await session.scalars(
                    select(SalespersonBrand.brand).where(SalespersonBrand.salesperson_id == person.id)
                )).all())
                party_codes = list((await session.scalars(
                    select(Party.party_code).join(SalespersonParty).where(SalespersonParty.salesperson_id == person.id)
                )).all())
                result.append({
                    "telegram_user_id": person.telegram_user_id, "display_name": person.display_name,
                    "active": person.active, "allow_discount": person.allow_discount,
                    "allow_free_quantity": person.allow_free_quantity,
                    "max_discount_percent": str(person.max_discount_percent),
                    "brands": brands, "party_codes": party_codes,
                })
            return result

    @app.patch("/admin/salespersons/{telegram_user_id}/approval", dependencies=[Depends(admin_auth)])
    async def approve_salesperson(telegram_user_id: int, data: SalespersonApproval):
        from petgenics.db import SessionLocal

        async with SessionLocal() as session:
            person = await session.scalar(select(Salesperson).where(
                Salesperson.telegram_user_id == telegram_user_id
            ))
            if person is None:
                raise HTTPException(status_code=404, detail="Salesperson account not found.")
            person.active = data.active
            await session.commit()
            return {"telegram_user_id": person.telegram_user_id, "active": person.active}

    @app.get("/admin/orders", dependencies=[Depends(admin_auth)])
    async def list_orders(
        status: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        search: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ):
        from petgenics.db import SessionLocal

        async with SessionLocal() as session:
            query = select(Order).options(selectinload(Order.items)).order_by(Order.created_at.desc())
            if status:
                query = query.where(Order.status == status)
            if start_date:
                query = query.where(Order.created_at >= datetime.combine(start_date, time.min, timezone.utc))
            if end_date:
                query = query.where(Order.created_at < datetime.combine(end_date + timedelta(days=1), time.min, timezone.utc))
            if search:
                from sqlalchemy import or_
                query = query.join(Party, Party.id == Order.party_id).where(or_(
                    Order.order_number.ilike(f"%{search}%"),
                    Party.party_code.ilike(f"%{search}%"),
                    Party.name.ilike(f"%{search}%"),
                    Order.brand.ilike(f"%{search}%"),
                ))
            orders = list((await session.scalars(query.limit(limit))).all())
            return [{
                "id": order.id, "order_number": order.order_number, "status": order.status,
                "party_id": order.party_id, "salesperson_id": order.salesperson_id, "brand": order.brand,
                "created_at": order.created_at.isoformat(), "items": [
                    {"product_code": item.product_code, "name": item.product_name, "brand": item.brand,
                     "quantity": str(item.quantity), "free_quantity": str(item.free_quantity),
                     "rate": str(item.rate), "discount_percent": str(item.discount_percent),
                     "line_total": str(item.line_total)} for item in order.items
                ],
            } for order in orders]

    @app.post("/admin/orders/{order_id}/decision", dependencies=[Depends(admin_auth)])
    async def order_decision(order_id: int, data: OrderDecision):
        if data.status not in {"Approved", "Rejected"}:
            raise HTTPException(status_code=422, detail="Decision must be Approved or Rejected.")
        from petgenics.db import SessionLocal

        async with SessionLocal() as session:
            order = await session.get(Order, order_id)
            if order is None:
                raise HTTPException(status_code=404, detail="Order not found.")
            if order.status != "Submitted":
                raise HTTPException(status_code=409, detail=f"Order in {order.status} status cannot be decided.")
            order.status = data.status
            order.remarks = data.remarks or order.remarks
            session.add(OrderAudit(
                order_id=order.id, actor="admin", action=data.status.lower(),
                details={"remarks": data.remarks},
            ))
            await session.commit()
            return {"order_number": order.order_number, "status": order.status}

    @app.get("/admin/orders/{order_id}/audit", dependencies=[Depends(admin_auth)])
    async def order_audit(order_id: int):
        from petgenics.db import SessionLocal

        async with SessionLocal() as session:
            order = await session.get(Order, order_id)
            if order is None:
                raise HTTPException(status_code=404, detail="Order not found.")
            events = list((await session.scalars(
                select(OrderAudit).where(OrderAudit.order_id == order_id).order_by(OrderAudit.created_at)
            )).all())
            return [{
                "actor": event.actor, "action": event.action,
                "details": event.details, "created_at": event.created_at.isoformat(),
            } for event in events]

    @app.put("/admin/marg/mapping", dependencies=[Depends(admin_auth)])
    async def marg_mapping(data: MargConfig):
        try:
            validate_mapping(data.columns, data.required_fields)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        from petgenics.db import SessionLocal

        async with SessionLocal() as session:
            row = await session.get(MargMapping, 1)
            if row is None:
                row = MargMapping(id=1, columns=data.columns, required_fields=data.required_fields)
                session.add(row)
            row.columns = data.columns
            row.required_fields = data.required_fields
            row.combined_allowed = data.combined_allowed
            await session.commit()
        return {"columns": data.columns, "required_fields": data.required_fields, "combined_allowed": data.combined_allowed}

    @app.get("/admin/marg/mapping", dependencies=[Depends(admin_auth)])
    async def get_marg_mapping():
        from petgenics.db import SessionLocal

        async with SessionLocal() as session:
            row = await session.get(MargMapping, 1)
            if row is None:
                return {"configured": False, "columns": {}, "required_fields": []}
            return {"configured": True, "columns": row.columns, "required_fields": row.required_fields,
                    "combined_allowed": row.combined_allowed}

    @app.get("/admin/orders/{order_id}/marg.xlsx", dependencies=[Depends(admin_auth)])
    async def marg_export(order_id: int):
        from petgenics.db import SessionLocal

        async with SessionLocal() as session:
            order = await session.scalar(
                select(Order).where(Order.id == order_id).options(selectinload(Order.items))
            )
            if order is None:
                raise HTTPException(status_code=404, detail="Order not found.")
            mapping = await session.get(MargMapping, 1)
            if mapping is None:
                raise HTTPException(status_code=409, detail="Configure the actual MARG template mapping first.")
            if order.status not in {"Approved", "Exported"}:
                raise HTTPException(status_code=409, detail="Only approved orders can be exported to MARG.")
            party = await session.get(Party, order.party_id)
            person = await session.get(Salesperson, order.salesperson_id)
            content = order_rows(order, party, person, mapping.columns, mapping.required_fields)
            if order.status == "Approved":
                order.status = "Exported"
                session.add(OrderAudit(order_id=order.id, actor="admin", action="exported", details={"format": "marg"}))
                await session.commit()
        return _xlsx_response(content, f"{order.order_number}-marg.xlsx")

    @app.get("/admin/orders/marg-combined.xlsx", dependencies=[Depends(admin_auth)])
    async def combined_marg_export(order_ids: str = Query(..., description="Comma-separated order IDs")):
        from petgenics.db import SessionLocal

        try:
            ids = list(dict.fromkeys(int(value.strip()) for value in order_ids.split(",")))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Order IDs must be comma-separated integers.") from exc
        if not ids or len(ids) > 100:
            raise HTTPException(status_code=422, detail="Select between 1 and 100 orders.")
        async with SessionLocal() as session:
            mapping = await session.get(MargMapping, 1)
            if mapping is None or not mapping.combined_allowed:
                raise HTTPException(status_code=409, detail="Combined export is not enabled by the configured MARG mapping.")
            orders = list((await session.scalars(
                select(Order).where(Order.id.in_(ids)).options(selectinload(Order.items))
            )).all())
            if len(orders) != len(ids) or any(order.status not in {"Approved", "Exported"} for order in orders):
                raise HTTPException(status_code=409, detail="Every selected order must exist and be approved.")
            entries = []
            for order in orders:
                party = await session.get(Party, order.party_id)
                person = await session.get(Salesperson, order.salesperson_id)
                entries.append((order, party, person))
            content = combined_order_rows(entries, mapping.columns, mapping.required_fields)
            for order in orders:
                if order.status == "Approved":
                    order.status = "Exported"
                    session.add(OrderAudit(
                        order_id=order.id, actor="admin", action="exported",
                        details={"format": "marg-combined"},
                    ))
            await session.commit()
        return _xlsx_response(content, "petgenics-marg-combined.xlsx")

    @app.get("/admin/orders/{order_id}/report.xlsx", dependencies=[Depends(admin_auth)])
    async def order_internal_report(order_id: int):
        from petgenics.db import SessionLocal
        from petgenics.marg import internal_report

        async with SessionLocal() as session:
            order = await session.scalar(
                select(Order).where(Order.id == order_id).options(selectinload(Order.items))
            )
            if order is None:
                raise HTTPException(status_code=404, detail="Order not found.")
            party = await session.get(Party, order.party_id)
            person = await session.get(Salesperson, order.salesperson_id)
            content = internal_report(order, party, person)
        return _xlsx_response(content, f"{order.order_number}-internal-report.xlsx")

    @app.get("/admin/orders/{order_id}/summary.pdf", dependencies=[Depends(admin_auth)])
    async def order_pdf_summary(order_id: int):
        import pymupdf as fitz
        from petgenics.db import SessionLocal

        async with SessionLocal() as session:
            order = await session.scalar(
                select(Order).where(Order.id == order_id).options(selectinload(Order.items))
            )
            if order is None:
                raise HTTPException(status_code=404, detail="Order not found.")
            party = await session.get(Party, order.party_id)
            person = await session.get(Salesperson, order.salesperson_id)
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
        return Response(content=document.tobytes(), media_type="application/pdf", headers={
            "Content-Disposition": f'attachment; filename="{order.order_number}-summary.pdf"'
        })

    @app.get("/admin/scanner/runs", dependencies=[Depends(admin_auth)])
    async def scanner_runs(limit: int = Query(default=50, ge=1, le=200)):
        from petgenics.db import SessionLocal

        async with SessionLocal() as session:
            runs = list((await session.scalars(
                select(ScannerRun)
                .options(load_only(
                    ScannerRun.id, ScannerRun.telegram_user_id, ScannerRun.filename,
                    ScannerRun.result, ScannerRun.expires_at, ScannerRun.created_at,
                ))
                .order_by(ScannerRun.created_at.desc())
                .limit(limit)
            )).all())
            return [{
                "id": run.id, "telegram_user_id": run.telegram_user_id, "filename": run.filename,
                "result": run.result, "created_at": run.created_at.isoformat(),
            } for run in runs]

    @app.get("/admin/reports/daily.xlsx", dependencies=[Depends(admin_auth)])
    async def daily_report(report_date: date = Query(default_factory=date.today)):
        from petgenics.db import SessionLocal

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
        content = workbook_bytes(
            ["order_number", "status", "date", "salesperson", "party_code", "party", "brand",
             "product_code", "product", "pack", "quantity", "free_quantity", "rate", "discount_percent",
             "line_total", "remarks"], rows,
        )
        return _xlsx_response(content, f"petgenics-orders-{report_date.isoformat()}.xlsx")

    return app


async def _read_upload(file: UploadFile, max_mb: int) -> bytes:
    filename = file.filename or ""
    if Path(filename).suffix.lower() not in {".xlsx", ".xls"}:
        raise HTTPException(status_code=422, detail="Upload an .xlsx or .xls workbook.")
    content = await file.read(max_mb * 1024 * 1024 + 1)
    if len(content) > max_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File exceeds the {max_mb} MB upload limit.")
    if not content:
        raise HTTPException(status_code=422, detail="Uploaded workbook is empty.")
    return content


def _xlsx_response(content: bytes, filename: str) -> Response:
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _clean_expired_scans() -> None:
    from petgenics.db import SessionLocal
    from petgenics.models import ScannerRun

    async with SessionLocal() as session:
        result = await session.execute(
            update(ScannerRun)
            .where(
                ScannerRun.document_bytes.is_not(None),
                ScannerRun.expires_at <= datetime.now(timezone.utc),
            )
            .values(document_bytes=None, result={"expired": True})
        )
        if result.rowcount:
            await session.commit()


async def _scanner_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(600)
        try:
            await _clean_expired_scans()
        except SQLAlchemyError:
            logger.exception("Could not clean expired scanner documents.")
