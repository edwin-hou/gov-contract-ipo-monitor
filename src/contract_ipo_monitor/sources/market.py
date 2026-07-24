from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..models import MarketSnapshot
from .http import ResilientClient


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class TwelveDataNormalizer:
    def normalize(self, *, symbol: str, quote: dict[str, Any], statistics: dict[str, Any], observed_at: datetime) -> MarketSnapshot | None:
        price = _float(quote.get("close") or quote.get("price"))
        shares = _float(statistics.get("shares_outstanding") or statistics.get("sharesOutstanding"))
        market_cap = _float(statistics.get("market_cap") or statistics.get("marketCapitalization"))
        if market_cap is None and price is not None and shares is not None:
            market_cap = price * shares
        if price is None or market_cap is None:
            return None
        raw_time = quote.get("datetime")
        if raw_time:
            quote_at = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
            if quote_at.tzinfo is None:
                quote_at = quote_at.replace(tzinfo=UTC)
        else:
            quote_at = observed_at
        return MarketSnapshot(
            symbol=symbol.upper(), venue=quote.get("exchange"), quote_at=quote_at, price=price,
            market_cap=market_cap, shares_outstanding=shares, volume=_float(quote.get("volume")),
            source="twelve_data", delayed=True,
        )


class TwelveDataCollector:
    BASE = "https://api.twelvedata.com"

    def __init__(self, client: ResilientClient, api_key: str):
        self.client = client
        self.api_key = api_key
        self.normalizer = TwelveDataNormalizer()

    async def snapshot(self, symbol: str, *, observed_at: datetime) -> MarketSnapshot | None:
        params = {"symbol": symbol, "apikey": self.api_key}
        quote = await self.client.request_json("GET", f"{self.BASE}/quote", params=params)
        statistics = await self.client.request_json("GET", f"{self.BASE}/statistics", params=params)
        return self.normalizer.normalize(symbol=symbol, quote=quote or {}, statistics=statistics or {}, observed_at=observed_at)
