from __future__ import annotations

import asyncio
import random
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
    ):
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.sleeper = sleeper
        self.client = httpx.AsyncClient(headers=headers, timeout=timeout, transport=transport, follow_redirects=True)

    def _backoff(self, attempt: int) -> float:
        return self.base_delay * (2 ** (attempt - 1)) + random.uniform(0, self.base_delay)

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = await self.client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
            else:
                if 200 <= response.status_code < 300:
                    return response
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    raise PermanentHTTPError(response.status_code, response.text[:500])
                last_error = TransientHTTPError(f"HTTP {response.status_code}: {response.text[:500]}")
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = float(retry_after)
                else:
                    delay = self._backoff(attempt)
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
