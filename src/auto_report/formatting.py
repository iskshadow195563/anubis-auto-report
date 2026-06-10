from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def to_decimal(value: str | int | float | Decimal | None) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if text == "":
        return Decimal("0")
    text = (
        text.replace("US$", "")
        .replace("$", "")
        .replace(",", "")
        .replace(" ", "")
        .replace("\u00a0", "")
    )
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def fmt_usd(value: Decimal) -> str:
    q = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if q < 0 else ""
    q = abs(q)
    return f"{sign}US${q:,.2f}"


def fmt_qty(value: Decimal, places: int = 6) -> str:
    quant = Decimal("1").scaleb(-places)
    q = value.quantize(quant, rounding=ROUND_HALF_UP)
    return f"{q:,.{places}f}"


def fmt_number(value: Decimal, places: int = 2) -> str:
    quant = Decimal("1").scaleb(-places)
    q = value.quantize(quant, rounding=ROUND_HALF_UP)
    return f"{q:,.{places}f}"
