import re
from typing import Any

_PLATEGA_PAYMENT_METHOD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\- ]{0,63}")
_PLATEGA_PAYMENT_METHOD_IDS = frozenset({2, 3, 11, 12, 13, 14})


def normalize_platega_payment_method(value: Any) -> str | None:
    """Return the canonical provider method or reject unsafe webhook metadata."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        if value not in _PLATEGA_PAYMENT_METHOD_IDS:
            raise ValueError("Invalid Platega paymentMethod id")
        return str(value)
    if not isinstance(value, str):
        raise ValueError("Platega paymentMethod must be an integer id or string")

    payment_method = value.strip()
    if not payment_method:
        return None
    if _PLATEGA_PAYMENT_METHOD_RE.fullmatch(payment_method) is None:
        raise ValueError("Invalid Platega paymentMethod")
    return payment_method
