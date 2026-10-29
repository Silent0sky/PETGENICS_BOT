from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


CENT = Decimal("0.01")
MAX_MONEY = Decimal("999999999999.99")
MAX_RATE = Decimal("999999999999.999999999999999")


def decimal_value(value: object, field: str, *, optional: bool = True) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if optional:
            return None
        raise ValueError(f"{field} is required")
    try:
        result = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} must be a number") from None
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite number")
    return result


def line_total(quantity: Decimal, rate: Decimal, discount_percent: Decimal) -> Decimal:
    if quantity <= 0:
        raise ValueError("Quantity must be greater than zero")
    if rate < 0:
        raise ValueError("Rate cannot be negative")
    if discount_percent < 0 or discount_percent > 100:
        raise ValueError("Discount must be between 0 and 100 percent")
    total = (quantity * rate * (Decimal("1") - discount_percent / Decimal("100"))).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    if total > MAX_MONEY:
        raise ValueError("Order line total exceeds the supported money range.")
    return total


def rate_value(value: object) -> Decimal:
    rate = decimal_value(value, "Rate", optional=False)
    if rate < 0 or rate > MAX_RATE:
        raise ValueError("Rate must be non-negative and fit the order rate field.")
    if rate.as_tuple().exponent < -15:
        raise ValueError("Rate supports at most fifteen decimal places.")
    return rate


def discount_value(value: object) -> Decimal:
    discount = decimal_value(value, "Discount", optional=False)
    if discount < 0 or discount > 100:
        raise ValueError("Discount must be between 0 and 100 percent.")
    if discount.as_tuple().exponent < -2:
        raise ValueError("Discount supports at most two decimal places.")
    return discount


def order_quantity(value: object, field: str = "Quantity") -> Decimal:
    quantity = decimal_value(value, field, optional=False)
    if quantity <= 0 or quantity > Decimal("999999999.999"):
        raise ValueError(f"{field} must be greater than zero and fit the order quantity field.")
    if quantity.as_tuple().exponent < -3:
        raise ValueError(f"{field} supports at most three decimal places.")
    return quantity


def free_quantity_value(value: object) -> Decimal:
    quantity = decimal_value(value, "Free quantity", optional=False)
    if quantity < 0 or quantity > Decimal("999999999.999"):
        raise ValueError("Free quantity must be non-negative and fit the order quantity field.")
    if quantity.as_tuple().exponent < -3:
        raise ValueError("Free quantity supports at most three decimal places.")
    return quantity
