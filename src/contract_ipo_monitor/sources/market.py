from __future__ import annotations

import math
from datetime import UTC, datetime
from zoneinfo import ZoneInfo
from typing import Any

from ..models import MarketSnapshot
from .http import ResilientClient


def _float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


class TwelveDataNormalizer:
    def normalize(self, *, symbol: str, quote: dict[str, Any], statistics: dict[str, Any], observed_at: datetime) -> MarketSnapshot | None:
        if quote.get("symbol") not in (None, symbol.upper()):
            return None
        price = _float(quote.get("close") or quote.get("price"))
        shares = _float(statistics.get("shares_outstanding") or statistics.get("sharesOutstanding"))
        market_cap = _float(statistics.get("market_cap") or statistics.get("marketCapitalization"))
        if market_cap is None and price is not None and shares is not None:
            market_cap = price * shares
        if price is None or market_cap is None or price <= 0 or market_cap <= 0 or not math.isfinite(market_cap) or (shares is not None and shares <= 0):
            return None
        # /quote datetime and timestamp are candle OPEN times. last_quote_at
        # is the documented last minute candle, not an exact exchange trade.
        raw_time = _float(quote.get("last_quote_at"))
        if raw_time is None or raw_time <= 0 or observed_at.utcoffset() is None:
            return None
        try:
            quote_at = datetime.fromtimestamp(raw_time, UTC)
        except (ValueError, OverflowError, OSError):
            return None
        if quote_at > observed_at:
            return None
        timezone = quote.get("exchange_timezone")
        if timezone:
            try:
                ZoneInfo(timezone)
            except (ValueError, KeyError, TypeError):
                return None
        return MarketSnapshot(
            symbol=symbol.upper(), venue=quote.get("exchange"), quote_at=quote_at, price=price,
            market_cap=market_cap, shares_outstanding=shares, volume=_float(quote.get("volume")),
            source="twelve_data", delayed=True,
            observed_at=observed_at, currency=quote.get("currency"), exchange_timezone=timezone,
            timestamp_precision="minute", timestamp_basis="last_minute_candle",
            provider_timestamp=str(quote["last_quote_at"]),
        )


class TwelveDataCollector:
    BASE = "https://api.twelvedata.com"

    def __init__(self, client: ResilientClient, api_key: str):
        self.client = client
        self.api_key = api_key
        self.normalizer = TwelveDataNormalizer()

    async def snapshot(self, symbol: str, *, observed_at: datetime) -> MarketSnapshot | None:
        params = {"symbol": symbol, "apikey": self.api_key}
        quote = await self.client.request_json("GET", f"{self.BASE}/quote", params={**params, "interval": "1min", "timezone": "UTC"})
        statistics = await self.client.request_json("GET", f"{self.BASE}/statistics", params=params)
        return self.normalizer.normalize(symbol=symbol, quote=quote or {}, statistics=statistics or {}, observed_at=observed_at)
