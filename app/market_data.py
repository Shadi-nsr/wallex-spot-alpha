"""Read-only public Wallex market candles for proposal generation."""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any

from .engine import Candle
from .markets import normalize_symbol


class MarketDataError(ValueError):
    pass


def fetch_candles(symbol: str, *, resolution: int = 60, limit: int = 120) -> list[Candle]:
    """Fetch public OHLCV candles; this function never sends an order."""
    if resolution <= 0 or limit < 30 or limit > 500:
        raise MarketDataError("resolution or candle limit is invalid")
    normalized = normalize_symbol(symbol).replace("/", "")
    now = int(time.time())
    span = resolution * 60 * (limit + 2)
    query = urllib.parse.urlencode({"symbol": normalized, "resolution": str(resolution), "from": now - span, "to": now})
    base = os.getenv("WALLEX_API_BASE_URL", "https://api.wallex.ir").rstrip("/")
    request = urllib.request.Request(f"{base}/v1/udf/history?{query}", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            payload: dict[str, Any] = json.loads(response.read().decode() or "{}")
    except Exception as exc:
        raise MarketDataError("Wallex market data is unavailable") from exc
    if payload.get("s") != "ok":
        raise MarketDataError("Wallex returned no market candles")
    arrays = [payload.get(key) for key in ("t", "o", "h", "l", "c", "v")]
    if any(not isinstance(values, list) for values in arrays):
        raise MarketDataError("Wallex candle response is invalid")
    rows: list[Candle] = []
    for values in zip(*arrays):
        try:
            rows.append(Candle.from_mapping(dict(zip(("timestamp", "open", "high", "low", "close", "volume"), values))))
        except (TypeError, ValueError):
            continue
    if len(rows) < 30:
        raise MarketDataError("not enough market candles for a proposal")
    return rows[-limit:]
