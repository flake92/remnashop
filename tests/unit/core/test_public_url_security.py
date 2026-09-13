import socket
from typing import Any

import pytest

from src.core.utils.validators import is_valid_public_https_url, is_valid_url
from src.infrastructure.services.http_client import (
    _HTTP_TEXT_MAX_BYTES,
    _PublicAddressResolver,
    _read_limited_text,
)


class _StaticResolver:
    def __init__(self, addresses: list[str]) -> None:
        self.addresses = addresses
        self.closed = False

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": int(family),
                "proto": 0,
                "flags": 0,
            }
            for address in self.addresses
        ]

    async def close(self) -> None:
        self.closed = True


class _StaticContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def iter_chunked(self, size: int):
        del size
        for chunk in self.chunks:
            yield chunk


class _StaticResponse:
    def __init__(self, chunks: list[bytes], content_length: int | None = None) -> None:
        self.content = _StaticContent(chunks)
        self.content_length = content_length
        self.charset = "utf-8"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/list.txt",
        "https://localhost/list.txt",
        "https://redis.internal/list.txt",
        "https://127.0.0.1/list.txt",
        "https://10.0.0.5/list.txt",
        "https://[::1]/list.txt",
        "https://224.0.0.1/list.txt",
        "https://[ff02::1]/list.txt",
        "https://user:password@example.com/list.txt",
        "https://example.com/list.txt#fragment",
        "https://single-label/list.txt",
    ],
)
def test_public_https_url_rejects_ssrf_targets(url: str) -> None:
    assert is_valid_public_https_url(url) is False


def test_url_validation_accepts_public_https_source_with_query() -> None:
    url = "https://example.com/blacklist.txt?revision=42"
    assert is_valid_url(url) is True
    assert is_valid_public_https_url(url) is True


async def test_public_resolver_rejects_mixed_private_dns_answers() -> None:
    resolver = _PublicAddressResolver(
        _StaticResolver(["93.184.216.34", "127.0.0.1"])  # type: ignore[arg-type]
    )

    with pytest.raises(OSError, match="non-public"):
        await resolver.resolve("example.com", 443)


@pytest.mark.parametrize("address", ["224.0.0.1", "ff02::1"])
async def test_public_resolver_rejects_multicast_dns_answers(address: str) -> None:
    resolver = _PublicAddressResolver(
        _StaticResolver([address])  # type: ignore[arg-type]
    )

    with pytest.raises(OSError, match="non-public"):
        await resolver.resolve("example.com", 443)


async def test_limited_reader_rejects_decompressed_oversize_response() -> None:
    response = _StaticResponse([b"a" * _HTTP_TEXT_MAX_BYTES, b"b"])

    assert await _read_limited_text(response) is None  # type: ignore[arg-type]


async def test_limited_reader_decodes_bounded_response() -> None:
    response = _StaticResponse(["123\n456".encode()])

    assert await _read_limited_text(response) == "123\n456"  # type: ignore[arg-type]
