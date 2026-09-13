import re
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Optional
from urllib.parse import urlsplit

from src.core.constants import (
    DOMAIN_REGEX,
    INVITE_LINK_PATTERN,
    TAG_REGEX,
    URL_PATTERN,
    USERNAME_PATTERN,
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value))


def is_valid_url(text: str) -> bool:
    if (
        not URL_PATTERN.fullmatch(text)
        or "\\" in text
        or any(character.isspace() for character in text)
    ):
        return False
    try:
        parsed = urlsplit(text)
        _ = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
    )


def is_public_unicast_address(value: str) -> bool:
    """Accept only an ordinary globally routable unicast IP address."""
    try:
        address: IPv4Address | IPv6Address = ip_address(value)
    except ValueError:
        return False
    # Python deliberately reports multicast as ``is_global``. Network source
    # validation must reject it alongside unspecified/reserved destinations.
    return bool(
        address.is_global
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
    )


def is_valid_public_https_url(text: str) -> bool:
    """Reject URL forms that can target local infrastructure before DNS lookup."""
    if not is_valid_url(text):
        return False
    parsed = urlsplit(text)
    if parsed.fragment:
        return False
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    if not hostname or "%" in hostname:
        return False
    try:
        ip_address(hostname)
    except ValueError:
        pass
    else:
        return is_public_unicast_address(hostname)

    if "." not in hostname:
        return False
    reserved_suffixes = (
        ".example",
        ".home.arpa",
        ".internal",
        ".invalid",
        ".local",
        ".localhost",
        ".test",
    )
    if hostname in {suffix[1:] for suffix in reserved_suffixes} or hostname.endswith(
        reserved_suffixes
    ):
        return False
    try:
        hostname.encode("idna")
    except UnicodeError:
        return False
    return True


def is_valid_username(text: str) -> bool:
    return bool(USERNAME_PATTERN.match(text))


def is_valid_domain(text: str) -> bool:
    return bool(DOMAIN_REGEX.match(text))


def is_invite_link(text: str) -> bool:
    return bool(INVITE_LINK_PATTERN.match(text))


def is_valid_tag(text: str) -> bool:
    return bool(TAG_REGEX.fullmatch(text))


def is_valid_int(value: Optional[str]) -> bool:
    if value is None:
        return False
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False


def parse_int(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def is_positive_int(value: Optional[str]) -> bool:
    parsed = parse_int(value)
    return parsed is not None and parsed > 0
