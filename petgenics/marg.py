from petgenics.excel_io import workbook_bytes
from petgenics.models import Order, Party, Salesperson

ALLOWED_FIELDS = {
    "party_code",
    "party_name",
    "order_number",
    "order_date",
    "salesperson",
    "brand",
    "product_code",
    "product_name",
    "pack_size",
    "quantity",
    "free_quantity",
    "rate",
    "discount_percent",
    "line_total",
}
def validate_mapping(columns: dict[str, str], required_fields: list[str]) -> None:
    if not columns:
        raise ValueError("Configure the actual MARG column headers before exporting.")
    invalid = set(columns.values()) - ALLOWED_FIELDS
    if invalid:
        raise ValueError(f"Unsupported MARG data fields: {', '.join(sorted(invalid))}")
    missing = set(required_fields) - set(columns.values())
    if missing:
        raise ValueError(f"Required mapped fields are missing: {', '.join(sorted(missing))}")
    if not required_fields:
        raise ValueError("Configure the required fields from the actual MARG template.")


def order_rows(
    order: Order,
    party: Party,
    salesperson: Salesperson,
    columns: dict[str, str],
    required_fields: list[str],
) -> bytes:
    validate_mapping(columns, required_fields)
    headers = list(columns.keys())
    values = {
        "party_code": party.party_code,
        "party_name": party.name,
        "order_number": order.order_number,
        "order_date": order.created_at.date().isoformat(),
        "salesperson": salesperson.display_name,
    }
    rows = []
    for item in order.items:
        item_values = {
            **values,
            "brand": item.brand,
            "product_code": item.product_code,
            "product_name": item.product_name,
            "pack_size": item.pack_size,
            "quantity": item.quantity,
            "free_quantity": item.free_quantity,
            "rate": item.rate,
            "discount_percent": item.discount_percent,
            "line_total": item.line_total,
        }
        rows.append([item_values[field] for field in columns.values()])
    return workbook_bytes(headers, rows)


def combined_order_rows(
    orders: list[tuple[Order, Party, Salesperson]],
    columns: dict[str, str],
    required_fields: list[str],
) -> bytes:
    validate_mapping(columns, required_fields)
    headers = list(columns)
    rows = []
    for order, party, salesperson in orders:
        common = {
            "party_code": party.party_code,
            "party_name": party.name,
            "order_number": order.order_number,
            "order_date": order.created_at.date().isoformat(),
            "salesperson": salesperson.display_name,
            "brand": order.brand,
        }
        for item in order.items:
            values = {
                **common,
                "brand": item.brand,
                "product_code": item.product_code,
                "product_name": item.product_name,
                "pack_size": item.pack_size,
                "quantity": item.quantity,
                "free_quantity": item.free_quantity,
                "rate": item.rate,
                "discount_percent": item.discount_percent,
                "line_total": item.line_total,
            }
            rows.append([values[field] for field in columns.values()])
    return workbook_bytes(headers, rows)


def internal_report(order: Order, party: Party, salesperson: Salesperson) -> bytes:
    headers = [
        "Order number", "Status", "Date", "Salesperson", "Party code", "Party",
        "Brand", "Product code", "Product", "Pack", "Quantity", "Free quantity",
        "Rate", "Discount %", "Line total", "Remarks",
    ]
    rows = [[
        order.order_number, order.status, order.created_at.isoformat(), salesperson.display_name,
        party.party_code, party.name,         item.brand, item.product_code, item.product_name,
        item.pack_size, item.quantity, item.free_quantity, item.rate,
        item.discount_percent, item.line_total, order.remarks,
    ] for item in order.items]
    return workbook_bytes(headers, rows)
