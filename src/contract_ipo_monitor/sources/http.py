from __future__ import annotations

import asyncio
import random
import ipaddress
import socket
import zlib
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from collections.abc import Awaitable, Callable
from typing import Any

import httpx


class PermanentHTTPError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code


class TransientHTTPError(RuntimeError):
    pass


class ResilientClient:
    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 20.0,
        max_attempts: int = 3,
        base_delay: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        allowed_hosts: tuple[str, ...] | None = None,
        max_response_bytes: int = 20 * 1024 * 1024,
    ):
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.sleeper = sleeper
        self.allowed_hosts = allowed_hosts
        self.resolve_hosts = transport is None
        self.max_response_bytes = max_response_bytes
        if max_attempts < 1 or timeout <= 0 or base_delay < 0 or max_response_bytes < 1:
            raise ValueError("HTTP retry and timeout settings must be positive")
        self.client = httpx.AsyncClient(headers={"Accept-Encoding": "gzip, deflate", **(headers or {})}, timeout=timeout, transport=transport, follow_redirects=False)

    async def _check_url(self, url: str | httpx.URL) -> None:
        parsed = httpx.URL(url)
        host = parsed.host.lower().rstrip(".")
        if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise PermanentHTTPError(400, "source URL must use HTTPS without credentials or a custom port")
        if self.allowed_hosts and host not in self.allowed_hosts:
            raise PermanentHTTPError(400, "source URL host is outside the collector allowlist")
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
            raise PermanentHTTPError(400, "source URL cannot target a local host")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if self.resolve_hosts:
                addresses = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
                if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                    raise PermanentHTTPError(400, "source URL cannot resolve to a private or reserved address")
        else:
            if not address.is_global:
                raise PermanentHTTPError(400, "source URL cannot target a private or reserved address")

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        await self._check_url(url)
        request = self.client.build_request(method, url, **kwargs)
        for redirect in range(6):
            response = await self.client.send(request, follow_redirects=False, stream=True)
            try:
                if response.next_request is not None:
                    if redirect == 5:
                        raise PermanentHTTPError(400, "source URL exceeded the redirect limit")
                    request = response.next_request
                    await self._check_url(request.url)
                    continue
                length = response.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > self.max_response_bytes:
                    raise PermanentHTTPError(413, "source response exceeds configured byte limit")
                chunks = []
                total = 0
                wire_total = 0
                encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
                decoder = None
                if not response.is_stream_consumed:
                    if encoding in {"gzip", "deflate"}:
                        decoder = zlib.decompressobj(31 if encoding == "gzip" else zlib.MAX_WBITS)
                    elif encoding != "identity":
                        raise PermanentHTTPError(415, "unsupported source response encoding")
                iterator = response.aiter_bytes() if response.is_stream_consumed else response.aiter_raw()
                async for chunk in iterator:
                    wire_total += len(chunk)
                    if wire_total > self.max_response_bytes:
                        raise PermanentHTTPError(413, "source response exceeds configured byte limit")
                    if decoder is not None:
                        try:
                            chunk = decoder.decompress(chunk, self.max_response_bytes - total + 1)
                        except zlib.error as exc:
                            raise PermanentHTTPError(415, "invalid compressed source response") from exc
                    total += len(chunk)
                    if total > self.max_response_bytes:
                        raise PermanentHTTPError(413, "source response exceeds configured byte limit")
                    chunks.append(chunk)
                if decoder is not None and (not decoder.eof or decoder.unused_data):
                    raise PermanentHTTPError(415, "incomplete or concatenated compressed source response")
                # Content is already decompressed. Do not decode it twice.
                headers = dict(response.headers)
                headers.pop("content-encoding", None)
                headers["content-length"] = str(total)
                return httpx.Response(response.status_code, headers=headers, content=b"".join(chunks), request=response.request, extensions=response.extensions)
            finally:
                await response.aclose()
        raise PermanentHTTPError(400, "source URL exceeded the redirect limit")

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                stamp = parsedate_to_datetime(value)
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=UTC)
                return max(0.0, (stamp - datetime.now(UTC)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                return None

    def _backoff(self, attempt: int) -> float:
        return self.base_delay * (2 ** (attempt - 1)) + random.uniform(0, self.base_delay)

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = await self._send(method, url, **kwargs)
            except (httpx.RequestError, socket.gaierror) as exc:
                last_error = TransientHTTPError(type(exc).__name__)
            else:
                if 200 <= response.status_code < 300:
                    return response
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    raise PermanentHTTPError(response.status_code, response.reason_phrase)
                last_error = TransientHTTPError(f"HTTP {response.status_code}: {response.reason_phrase}")
                retry_after = self._retry_after(response.headers.get("Retry-After"))
                delay = retry_after if retry_after is not None else self._backoff(attempt)
                if delay > 60:
                    raise TransientHTTPError(f"HTTP {response.status_code}: retry requested after {delay:.0f} seconds; defer to next scheduled run")
                if attempt < self.max_attempts:
                    await self.sleeper(delay)
                    continue
            if attempt < self.max_attempts:
                await self.sleeper(self._backoff(attempt))
        raise TransientHTTPError(str(last_error or "request failed"))

    async def request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        response = await self._request(method, url, **kwargs)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def request_text(self, method: str, url: str, **kwargs: Any) -> str:
        response = await self._request(method, url, **kwargs)
        return response.text

    async def aclose(self) -> None:
        await self.client.aclose()
