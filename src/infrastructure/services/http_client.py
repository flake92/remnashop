import socket
from typing import Optional
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from loguru import logger

from src.core.utils.validators import (
    is_public_unicast_address,
    is_valid_public_https_url,
)

_HTTP_TEXT_MAX_BYTES = 2 * 1024 * 1024
_HTTP_MAX_REDIRECTS = 3
_HTTP_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class _PublicAddressResolver(AbstractResolver):
    """Resolve once for the connector and reject every non-public address."""

    def __init__(self, resolver: Optional[AbstractResolver] = None) -> None:
        self._resolver = resolver or aiohttp.ThreadedResolver()

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        results = await self._resolver.resolve(host, port, family)
        if not results or any(not is_public_unicast_address(item["host"]) for item in results):
            raise OSError("HTTP source resolved to a non-public address")
        return results

    async def close(self) -> None:
        await self._resolver.close()


def _safe_url_label(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "invalid-host").casefold()
    except ValueError:
        return "invalid-host"


async def _read_limited_text(response: aiohttp.ClientResponse) -> Optional[str]:
    content_length = response.content_length
    if content_length is not None and content_length > _HTTP_TEXT_MAX_BYTES:
        return None
    body = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        if len(body) + len(chunk) > _HTTP_TEXT_MAX_BYTES:
            return None
        body.extend(chunk)
    return body.decode(response.charset or "utf-8", errors="replace")


class AiohttpClient:
    async def get_text(self, url: str, timeout: float = 15.0) -> Optional[str]:
        if not is_valid_public_https_url(url):
            logger.error("Refused unsafe HTTPS source")
            return None

        resolver = _PublicAddressResolver()
        connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                current_url = url
                for redirect_count in range(_HTTP_MAX_REDIRECTS + 1):
                    if not is_valid_public_https_url(current_url):
                        logger.error("Refused unsafe HTTPS redirect")
                        return None
                    async with session.get(
                        current_url,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                        allow_redirects=False,
                    ) as response:
                        if response.status in _HTTP_REDIRECT_STATUSES:
                            location = response.headers.get("Location")
                            if location is None or redirect_count == _HTTP_MAX_REDIRECTS:
                                logger.error("HTTPS source exceeded the redirect limit")
                                return None
                            current_url = urljoin(str(response.url), location)
                            continue
                        if response.status != 200:
                            logger.error(
                                "Failed to fetch HTTPS source from '{}': HTTP {}",
                                _safe_url_label(current_url),
                                response.status,
                            )
                            return None
                        text = await _read_limited_text(response)
                        if text is None:
                            logger.error("HTTPS source response exceeded the size limit")
                        return text
                return None
        except Exception as exc:
            logger.error(
                "Failed to fetch HTTPS source from '{}' (error_type={})",
                _safe_url_label(url),
                type(exc).__name__,
            )
            return None
