"""Bounded GETs with validated, pinned DNS and verified TLS hostnames."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx


class AdapterError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        transient: bool = False,
        http_status: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.transient = transient
        self.http_status = http_status


def validate_public_url(url: str) -> httpx.URL:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as exc:
        raise AdapterError("unsafe_url") from exc
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.fragment
        or port not in (None, 80, 443)
    ):
        raise AdapterError("unsafe_url")
    return httpx.URL(url)


async def resolve_public(host: str, port: int) -> str:
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise AdapterError("dns_unavailable", transient=True) from exc
    ips = [str(address[4][0]) for address in addresses]
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise AdapterError("private_destination")
    # Pin one validated address: the transport cannot resolve a changed DNS result.
    return ips[0]


class PublicHTTP:
    def __init__(self, max_bytes: int = 2_000_000) -> None:
        self.max_bytes = max_bytes
        self.client = httpx.AsyncClient(
            timeout=10,
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=0),
        )
        self.lane = asyncio.Semaphore(4)

    async def close(self) -> None:
        await self.client.aclose()

    async def get(self, url: str) -> bytes:
        async with self.lane:
            try:
                async with asyncio.timeout(30):
                    return await self._get(url)
            except (TimeoutError, httpx.TimeoutException) as exc:
                raise AdapterError("timeout", transient=True) from exc
            except httpx.TransportError as exc:
                raise AdapterError("provider_unavailable", transient=True) from exc

    async def _get(self, url: str) -> bytes:
        for redirects in range(4):
            original = validate_public_url(url)
            port = original.port or (443 if original.scheme == "https" else 80)
            ip = await resolve_public(original.host, port)
            pinned = original.copy_with(host=ip)
            request = self.client.build_request(
                "GET",
                pinned,
                headers={
                    "Host": original.netloc.decode(),
                    "User-Agent": "linkedin-bot/0.1 read-only feasibility",
                },
                extensions={"sni_hostname": original.host},
            )
            # Cookies collected under the pinned IP must never cross origins.
            request.headers.pop("cookie", None)
            response = await self.client.send(request, stream=True)
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location or redirects == 3:
                        raise AdapterError("redirect_limit")
                    url = str(original.join(location))
                    continue
                if response.status_code == 429:
                    raise AdapterError("rate_limited", transient=True)
                if response.status_code >= 500:
                    raise AdapterError("provider_unavailable", transient=True)
                if response.status_code != 200:
                    raise AdapterError("http_status", http_status=response.status_code)
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > self.max_bytes:
                        raise AdapterError("response_too_large")
                return bytes(content)
            finally:
                await response.aclose()
        raise AdapterError("redirect_limit")
