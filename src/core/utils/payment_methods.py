import re
from typing import Any

_PLATEGA_PAYMENT_METHOD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\- ]{0,63}")


def normalize_platega_payment_method(value: Any) -> str | None:
    """Return the canonical provider method or reject unsafe webhook metadata."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Platega paymentMethod must be a string")

    payment_method = value.strip()
    if not payment_method:
        return None
    if _PLATEGA_PAYMENT_METHOD_RE.fullmatch(payment_method) is None:
        raise ValueError("Invalid Platega paymentMethod")
    return payment_method
