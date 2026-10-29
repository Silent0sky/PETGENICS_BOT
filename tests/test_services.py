import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import io

import httpx
from openpyxl import Workbook
from sqlalchemy import create_mock_engine

from petgenics.api import create_app
from petgenics.config import Settings
from petgenics.excel_io import preview_workbook, read_mapped_rows, workbook_bytes
from petgenics.imports import duplicate_code_rows
from petgenics.marg import order_rows, validate_mapping
from petgenics.money import free_quantity_value, line_total, order_quantity, rate_value
from petgenics.models import Base, Order, OrderItem, Party, Product, Salesperson
from petgenics.scanner import extract_document, match_lines


def test_decimal_order_total_and_discount_rounding():
    assert line_total(Decimal("3"), Decimal("10.10"), Decimal("12.5")) == Decimal("26.51")


def test_negative_quantity_and_out_of_range_discount_are_rejected():
    for quantity, discount in [(Decimal("-1"), Decimal("0")), (Decimal("1"), Decimal("101"))]:
        try:
            line_total(quantity, Decimal("10"), discount)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid order calculation was accepted.")


def test_order_quantity_rejects_unrepresentable_precision_and_size():
    for quantity in ["0.0001", "1000000000"]:
        try:
            order_quantity(quantity)
        except ValueError:
            pass
        else:
            raise AssertionError("Unrepresentable quantity was accepted.")


def test_excel_header_detection_and_exact_numeric_product_code():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Brand list - current prices"])
    sheet.append(["Product Code", "Product", "MRP", "Pack", "Brand"])
    sheet.append([123, "Sensitive Adult", 99.5, "1 KG", "Example Brand"])
    sheet["A3"].number_format = "000000"
    content = io.BytesIO()
    workbook.save(content)
    data = content.getvalue()

    preview = preview_workbook(data, "products.xlsx")
    assert preview["header_row"] == 2
    assert preview["columns"][:3] == ["Product Code", "Product", "MRP"]
    rows = read_mapped_rows(data, "products.xlsx", {
        "Product Code": "product_code",
        "Product": "name",
        "Brand": "brand",
    })
    assert rows[0]["product_code"] == "000123"
    assert rows[0]["_row"] == 3


def test_stock_only_workbook_with_a_single_named_header_is_detected():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["PRODUCTS CODE", None])
    sheet.append(["SKU-1", 7])
    content = io.BytesIO()
    workbook.save(content)
    rows = read_mapped_rows(content.getvalue(), "stock.xlsx", {
        "PRODUCTS CODE": "product_code",
        "Unnamed: 1": "stock",
    })
    assert rows[0]["product_code"] == "SKU-1"
    assert rows[0]["stock"] == 7


def test_party_import_mapping_reads_selected_worksheet_and_preserves_codes():
    workbook = Workbook()
    workbook.active.title = "Summary"
    sheet = workbook.create_sheet("Party Data")
    sheet.append(["Party Code", "Party Name"])
    sheet.append([123, "Test Pet Shop"])
    sheet["A2"].number_format = "00000"
    content = io.BytesIO()
    workbook.save(content)
    data = content.getvalue()

    rows = read_mapped_rows(
        data,
        "parties.xlsx",
        {"Party Code": "party_code", "Party Name": "name"},
        sheet_name=1,
    )
    assert rows == [{
        "_row": 2,
        "party_code": "00123",
        "name": "Test Pet Shop",
        "_source_details": {"Party Code": "00123", "Party Name": "Test Pet Shop"},
    }]


def test_duplicate_product_codes_are_flagged_without_dropping_source_rows():
    rows = [
        {"_row": 2, "product_code": "SKU-1"},
        {"_row": 3, "product_code": "SKU-2"},
        {"_row": 4, "product_code": "SKU-1"},
    ]
    assert duplicate_code_rows(rows) == {4}


def test_marg_mapping_requires_configured_headers_and_required_fields():
    columns = {
        "Party Key": "party_code",
        "Order Ref": "order_number",
        "Date": "order_date",
        "Item Key": "product_code",
        "Units": "quantity",
    }
    validate_mapping(columns, ["party_code", "product_code", "quantity"])
    try:
        validate_mapping(columns, ["party_code", "rate"])
    except ValueError:
        pass
    else:
        raise AssertionError("An unmapped required field was accepted.")


def test_marg_export_uses_configured_headers_and_order_values():
    order = Order(
        order_number="PG-TEST-1",
        salesperson_id=1,
        party_id=1,
        brand="Test Brand",
        status="Approved",
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    order.items.append(OrderItem(
        product_id=1,
        product_code="SKU-1",
        product_name="Test product",
        brand="Test Brand",
        pack_size="1 KG",
        quantity=Decimal("2"),
        free_quantity=Decimal("1"),
        rate=Decimal("10.125"),
        discount_percent=Decimal("5"),
        line_total=Decimal("19.24"),
    ))
    content = order_rows(
        order,
        Party(party_code="P-1", name="Test party"),
        Salesperson(display_name="Test seller"),
        {
            "ERP Party": "party_code",
            "ERP Order": "order_number",
            "ERP Date": "order_date",
            "ERP Item": "product_code",
            "ERP Qty": "quantity",
            "ERP Free": "free_quantity",
            "ERP Rate": "rate",
            "ERP Discount": "discount_percent",
        },
        ["party_code", "order_number", "order_date", "product_code", "quantity"],
    )
    from openpyxl import load_workbook

    sheet = load_workbook(io.BytesIO(content), data_only=True).active
    assert [cell.value for cell in sheet[1]] == [
        "ERP Party", "ERP Order", "ERP Date", "ERP Item", "ERP Qty", "ERP Free", "ERP Rate", "ERP Discount"
    ]
    assert sheet["A2"].value == "P-1"
    assert sheet["E2"].value == 2
    assert sheet["F2"].value == 1


def test_scanner_does_not_infer_missing_quantity_or_unmatched_product():
    result = match_lines("unknown item 12", [])
    assert result[0]["product_code"] is None
    assert result[0]["quantity"] is None
    assert result[0]["confidence"] == 0
    assert result[0]["warning"]


def test_digital_pdf_text_is_extracted_across_pages():
    import pymupdf

    document = pymupdf.open()
    document.new_page().insert_text((50, 50), "SKU-1 qty 2")
    document.new_page().insert_text((50, 50), "SKU-2 quantity 3")
    result = extract_document(document.tobytes(), "order.pdf")
    assert len(result["pages"]) == 2
    assert "SKU-1 qty 2" in result["pages"][0]["text"]
    assert "SKU-2 quantity 3" in result["pages"][1]["text"]
    assert result["unreadable_pages"] == []


def test_excel_export_is_a_readable_workbook():
    content = workbook_bytes(["product_code"], [["SKU-1"]])
    assert content.startswith(b"PK")


def test_excel_export_treats_formula_like_strings_as_text():
    from openpyxl import load_workbook

    content = workbook_bytes(["name"], [["=1+1"]])
    workbook = load_workbook(io.BytesIO(content), data_only=False)
    assert workbook.active["A2"].data_type == "s"
    assert workbook.active["A2"].value == "=1+1"


def test_free_quantity_is_nonnegative_and_bounded():
    assert free_quantity_value("2.5") == Decimal("2.5")
    for quantity in ["-1", "1000000000", "0.0001"]:
        try:
            free_quantity_value(quantity)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid free quantity was accepted.")


def test_high_precision_imported_rate_is_preserved_and_product_codes_are_matched_exactly():
    rate = Decimal("118.83983050847459")
    assert rate_value(rate) == rate
    product = Product(id=1, product_code="SKU-123", name="Test food", brand="Test")
    assert match_lines("SKU-123 qty 2", [product])[0]["product_code"] == "SKU-123"
    assert match_lines("SKU-1234 qty 2", [product])[0]["product_code"] is None


def test_api_health_and_admin_routes_enforce_separate_secrets():
    settings = Settings(
        None, "", None, "postgresql+asyncpg://user:pass@localhost/db",
        "admin-test", "health-test", 10000, 24, 15, "INR", None,
    )
    app = create_app(settings)

    async def exercise_routes():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/healthz")).status_code == 200
            assert (await client.get("/health")).status_code == 401
            assert (await client.get("/admin/orders")).status_code == 401

    asyncio.run(exercise_routes())


def test_postgresql_schema_compiles_order_and_stable_code_constraints():
    statements = []
    engine = create_mock_engine(
        "postgresql://",
        lambda statement, *args, **kwargs: statements.append(
            str(statement.compile(dialect=engine.dialect))
        ),
    )
    Base.metadata.create_all(engine)
    sql = "\n".join(statements)
    assert "CREATE TABLE orders" in sql
    assert "CREATE TABLE order_items" in sql
    assert "CREATE UNIQUE INDEX ix_products_product_code" in sql
    assert "ck_order_item_quantity" in sql
    assert "CREATE TABLE scanner_runs" in sql
