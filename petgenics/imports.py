from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from petgenics.excel_io import PRODUCT_FIELDS, read_mapped_rows
from petgenics.models import ImportBatch, Party, Product
from petgenics.money import MAX_RATE, decimal_value

NUMERIC_PRODUCT_FIELDS = {"mrp", "selling_rate", "stock"}


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def duplicate_code_rows(rows: list[dict[str, Any]], field: str = "product_code") -> set[int]:
    seen: set[str] = set()
    duplicates: set[int] = set()
    for row in rows:
        code = _clean(row.get(field))
        if code is None:
            continue
        if code in seen:
            duplicates.add(int(row["_row"]))
        seen.add(code)
    return duplicates


async def import_products(
    session: AsyncSession,
    content: bytes,
    filename: str,
    mapping: dict[str, str],
    brand_override: str | None = None,
    sheet_name: str | int = 0,
    header_row: int | None = None,
) -> dict[str, Any]:
    if "product_code" not in mapping.values():
        raise ValueError("Map a stable product code before importing.")
    if any(field not in PRODUCT_FIELDS for field in mapping.values()):
        raise ValueError("Product import mappings can only target product fields.")
    rows = read_mapped_rows(content, filename, mapping, sheet_name, header_row)
    counts = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
    errors: list[dict[str, Any]] = []
    successful_rows: list[dict[str, Any]] = []
    duplicates = duplicate_code_rows(rows)
    for row in rows:
        code = _clean(row.get("product_code"))
        row_number = row["_row"]
        if not code:
            counts["failed"] += 1
            errors.append({"row": row_number, "product_code": "", "error": "Missing product code."})
            continue
        if len(code) > 120:
            counts["failed"] += 1
            errors.append({"row": row_number, "product_code": code, "error": "Product code exceeds 120 characters."})
            continue
        if row_number in duplicates:
            counts["skipped"] += 1
            errors.append({"row": row_number, "product_code": code, "error": "Duplicate product code in this workbook."})
            continue
        product = await session.scalar(select(Product).where(Product.product_code == code))
        name = _clean(row.get("name"))
        brand = _clean(brand_override) or _clean(row.get("brand"))
        if product is None and (not name or not brand):
            counts["failed"] += 1
            errors.append({
                "row": row_number,
                "product_code": code,
                "error": "Missing product name or brand; supply an explicit brand override if the workbook has no brand column.",
            })
            continue
        if name is not None and len(name) > 500:
            invalid = "Product name exceeds 500 characters."
        elif brand is not None and len(brand) > 200:
            invalid = "Brand exceeds 200 characters."
        else:
            invalid = None
        parsed: dict[str, Any] = {"product_code": code}
        if name is not None:
            parsed["name"] = name
        if brand is not None:
            parsed["brand"] = brand
        if row.get("_source_details"):
            parsed["source_details"] = row["_source_details"]
        for field in NUMERIC_PRODUCT_FIELDS:
            if field in row and _clean(row[field]) is not None:
                try:
                    number = decimal_value(row[field], field)
                    maximum = MAX_RATE if field == "selling_rate" else (
                        Decimal("999999999999.99") if field == "mrp" else Decimal("999999999.999")
                    )
                    if number is not None and abs(number) > maximum:
                        raise ValueError(f"{field} exceeds the supported field range")
                    if field in {"mrp", "selling_rate"} and number is not None and number < 0:
                        raise ValueError(f"{field} cannot be negative")
                    max_places = 2 if field == "mrp" else (15 if field == "selling_rate" else 3)
                    if number is not None and number.as_tuple().exponent < -max_places:
                        raise ValueError(f"{field} supports at most {max_places} decimal places")
                    parsed[field] = number
                except ValueError as exc:
                    invalid = str(exc)
                    break
        for field in {"family", "pack_size"}:
            if _clean(row.get(field)) is not None:
                parsed[field] = _clean(row[field])
        if parsed.get("family") is not None and len(parsed["family"]) > 200:
            invalid = "Family exceeds 200 characters."
        if parsed.get("pack_size") is not None and len(parsed["pack_size"]) > 120:
            invalid = "Pack size exceeds 120 characters."
        if invalid:
            counts["failed"] += 1
            errors.append({"row": row_number, "product_code": code, "error": invalid})
            continue
        if product is None:
            product = Product(**parsed)
            session.add(product)
            counts["created"] += 1
            action = "created"
        else:
            for field, value in parsed.items():
                if field == "source_details" and value:
                    product.source_details = {**(product.source_details or {}), **value}
                elif field != "product_code" and value is not None:
                    setattr(product, field, value)
            counts["updated"] += 1
            action = "updated"
        successful_rows.append({"row": row_number, "product_code": code, "action": action})
    batch = ImportBatch(
        kind="products",
        filename=filename[:255],
        summary=counts,
        errors=errors,
        successful_rows=successful_rows,
    )
    session.add(batch)
    await session.commit()
    return {"batch_id": batch.id, **counts, "successful_rows": successful_rows, "errors": errors}


async def import_parties(
    session: AsyncSession,
    content: bytes,
    filename: str,
    mapping: dict[str, str],
    sheet_name: str | int = 0,
    header_row: int | None = None,
) -> dict[str, Any]:
    if "party_code" not in mapping.values():
        raise ValueError("Map a stable party code before importing.")
    if not set(mapping.values()).issubset({"party_code", "name"}):
        raise ValueError("Party imports can only map party_code and name.")
    rows = read_mapped_rows(content, filename, mapping, sheet_name=sheet_name, header_row=header_row)
    counts = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
    errors: list[dict[str, Any]] = []
    successful_rows: list[dict[str, Any]] = []
    duplicates = duplicate_code_rows(rows, "party_code")
    for row in rows:
        code, name = _clean(row.get("party_code")), _clean(row.get("name"))
        if not code:
            counts["failed"] += 1
            errors.append({"row": row["_row"], "party_code": "", "error": "Party code is required."})
            continue
        if len(code) > 120:
            counts["failed"] += 1
            errors.append({"row": row["_row"], "party_code": code, "error": "Party code exceeds 120 characters."})
            continue
        if row["_row"] in duplicates:
            counts["skipped"] += 1
            errors.append({"row": row["_row"], "party_code": code, "error": "Duplicate party code in this workbook."})
            continue
        party = await session.scalar(select(Party).where(Party.party_code == code))
        if party is None:
            if not name:
                counts["failed"] += 1
                errors.append({"row": row["_row"], "party_code": code, "error": "Party name is required for a new party."})
                continue
            if len(name) > 300:
                counts["failed"] += 1
                errors.append({"row": row["_row"], "party_code": code, "error": "Party name exceeds 300 characters."})
                continue
            session.add(Party(party_code=code, name=name))
            counts["created"] += 1
            action = "created"
        elif name is None:
            counts["skipped"] += 1
            successful_rows.append({"row": row["_row"], "party_code": code, "action": "unchanged"})
            continue
        else:
            party.name = name
            counts["updated"] += 1
            action = "updated"
        successful_rows.append({"row": row["_row"], "party_code": code, "action": action})
    batch = ImportBatch(
        kind="parties", filename=filename[:255], summary=counts, errors=errors, successful_rows=successful_rows
    )
    session.add(batch)
    await session.commit()
    return {"batch_id": batch.id, **counts, "successful_rows": successful_rows, "errors": errors}
