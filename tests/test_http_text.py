import httpx
import pytest

from contract_ipo_monitor.sources.http import ResilientClient


@pytest.mark.asyncio
async def test_text_requests_retry_transient_failure_then_succeed():
    responses = [httpx.Response(503, text="busy"), httpx.Response(200, text="ok")]
    sleeps = []

    def handler(_request):
        return responses.pop(0)

    async def no_sleep(seconds):
        sleeps.append(seconds)

    client = ResilientClient(
        transport=httpx.MockTransport(handler),
        sleeper=no_sleep,
        max_attempts=2,
        base_delay=0,
    )
    assert await client.request_text("GET", "https://example.test/file") == "ok"
    assert len(sleeps) == 1
    await client.aclose()
