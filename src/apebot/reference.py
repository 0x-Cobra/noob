"""Reference (off-chain) price providers for the underlying stocks."""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import httpx

from .types import RefQuote


class ReferenceProvider(ABC):
    name = "abstract"

    @abstractmethod
    def quote(self, symbol: str) -> RefQuote: ...

    def close(self) -> None:  # pragma: no cover
        pass


class _Throttle:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last: dict[str, float] = {}
        self._cache: dict[str, RefQuote] = {}

    def get(self, key: str) -> RefQuote | None:
        t = self._last.get(key)
        if t is not None and time.monotonic() - t < self.min_interval:
            return self._cache.get(key)
        return None

    def put(self, key: str, q: RefQuote) -> None:
        self._last[key] = time.monotonic()
        self._cache[key] = q


class FinnhubProvider(ReferenceProvider):
    """https://finnhub.io/docs/api/quote  (free tier: 60 req/min, real-time US equities)."""
    name = "finnhub"

    def __init__(self, api_key: str, min_interval: float = 10.0, timeout: float = 10.0):
        if not api_key:
            raise ValueError("FINNHUB_API_KEY is required for the finnhub provider")
        self.key = api_key
        self.http = httpx.Client(timeout=timeout)
        self.throttle = _Throttle(min_interval)

    def quote(self, symbol: str) -> RefQuote:
        cached = self.throttle.get(symbol)
        if cached:
            return cached
        r = self.http.get("https://finnhub.io/api/v1/quote", params={"symbol": symbol, "token": self.key})
        r.raise_for_status()
        d = r.json()
        price = float(d.get("c") or 0)
        ts = int(d.get("t") or 0)
        if price <= 0 or ts <= 0:
            raise RuntimeError(f"finnhub returned no price for {symbol}: {d}")
        q = RefQuote(symbol=symbol, price=price, ts=datetime.fromtimestamp(ts, tz=timezone.utc),
                     prev_close=float(d["pc"]) if d.get("pc") else None, source=self.name)
        self.throttle.put(symbol, q)
        return q

    def close(self) -> None:
        self.http.close()


class PolygonProvider(ReferenceProvider):
    """https://polygon.io/docs/rest/stocks/trades/last-trade (needs a plan with last-trade access)."""
    name = "polygon"

    def __init__(self, api_key: str, min_interval: float = 10.0, timeout: float = 10.0):
        if not api_key:
            raise ValueError("POLYGON_API_KEY is required for the polygon provider")
        self.key = api_key
        self.http = httpx.Client(timeout=timeout)
        self.throttle = _Throttle(min_interval)

    def quote(self, symbol: str) -> RefQuote:
        cached = self.throttle.get(symbol)
        if cached:
            return cached
        r = self.http.get(f"https://api.polygon.io/v2/last/trade/{symbol}", params={"apiKey": self.key})
        r.raise_for_status()
        res = r.json().get("results") or {}
        price = float(res.get("p") or 0)
        ts_ns = int(res.get("t") or 0)
        if price <= 0 or ts_ns <= 0:
            raise RuntimeError(f"polygon returned no last trade for {symbol}")
        q = RefQuote(symbol=symbol, price=price, ts=datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc),
                     source=self.name)
        self.throttle.put(symbol, q)
        return q

    def close(self) -> None:
        self.http.close()


def build_reference_provider(provider: str, secrets, min_interval: float) -> ReferenceProvider:
    if provider == "finnhub":
        return FinnhubProvider(secrets.finnhub_api_key or "", min_interval=min_interval)
    if provider == "polygon":
        return PolygonProvider(secrets.polygon_api_key or "", min_interval=min_interval)
    raise ValueError(f"unknown reference provider {provider!r} (synthetic is only valid in simulate mode)")
