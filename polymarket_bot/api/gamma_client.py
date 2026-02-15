"""Client for the Polymarket Gamma API (market discovery and metadata).

The Gamma API provides unauthenticated access to market and event data,
including liquidity, volume, outcomes, and pricing information.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from polymarket_bot.config.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class Market:
    """Represents a single Polymarket market."""

    id: str = ""
    question: str = ""
    condition_id: str = ""
    slug: str = ""
    outcomes: list[str] = field(default_factory=list)
    outcome_prices: list[float] = field(default_factory=list)
    clob_token_ids: list[str] = field(default_factory=list)
    active: bool = False
    closed: bool = False
    enable_order_book: bool = False
    volume: float = 0.0
    liquidity: float = 0.0
    end_date: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    minimum_order_size: float = 5.0
    minimum_tick_size: float = 0.01

    @classmethod
    def from_raw(cls, data: dict[str, Any]) -> Market:
        outcome_prices_raw = data.get("outcomePrices", data.get("outcome_prices", ""))
        if isinstance(outcome_prices_raw, str) and outcome_prices_raw:
            # Format: "[\"0.5\",\"0.5\"]" or "0.5,0.5"
            cleaned = outcome_prices_raw.strip("[]").replace('"', "")
            outcome_prices = [float(p) for p in cleaned.split(",") if p.strip()]
        elif isinstance(outcome_prices_raw, list):
            outcome_prices = [float(p) for p in outcome_prices_raw]
        else:
            outcome_prices = []

        clob_ids_raw = data.get("clobTokenIds", data.get("clob_token_ids", ""))
        if isinstance(clob_ids_raw, str) and clob_ids_raw:
            cleaned = clob_ids_raw.strip("[]").replace('"', "")
            clob_token_ids = [s.strip() for s in cleaned.split(",") if s.strip()]
        elif isinstance(clob_ids_raw, list):
            clob_token_ids = clob_ids_raw
        else:
            clob_token_ids = []

        outcomes_raw = data.get("outcomes", "")
        if isinstance(outcomes_raw, str) and outcomes_raw:
            cleaned = outcomes_raw.strip("[]").replace('"', "")
            outcomes = [s.strip() for s in cleaned.split(",") if s.strip()]
        elif isinstance(outcomes_raw, list):
            outcomes = outcomes_raw
        else:
            outcomes = []

        tags_raw = data.get("tags", [])
        if isinstance(tags_raw, list):
            tags = []
            for t in tags_raw:
                if isinstance(t, dict):
                    tags.append(t.get("label", t.get("slug", str(t))))
                else:
                    tags.append(str(t))
        else:
            tags = []

        return cls(
            id=str(data.get("id", "")),
            question=data.get("question", ""),
            condition_id=data.get("conditionId", data.get("condition_id", "")),
            slug=data.get("slug", ""),
            outcomes=outcomes,
            outcome_prices=outcome_prices,
            clob_token_ids=clob_token_ids,
            active=bool(data.get("active", False)),
            closed=bool(data.get("closed", False)),
            enable_order_book=bool(data.get("enableOrderBook", False)),
            volume=float(data.get("volume", 0) or 0),
            liquidity=float(data.get("liquidity", 0) or 0),
            end_date=data.get("endDate", data.get("end_date", "")),
            description=data.get("description", ""),
            tags=tags,
            minimum_order_size=float(data.get("minimum_order_size", 5.0) or 5.0),
            minimum_tick_size=float(data.get("minimum_tick_size", 0.01) or 0.01),
        )


@dataclass
class Event:
    """Represents a Polymarket event (can contain multiple markets)."""

    id: str = ""
    title: str = ""
    slug: str = ""
    markets: list[Market] = field(default_factory=list)
    volume: float = 0.0
    liquidity: float = 0.0

    @classmethod
    def from_raw(cls, data: dict[str, Any]) -> Event:
        markets = [
            Market.from_raw(m) for m in data.get("markets", [])
        ]
        return cls(
            id=str(data.get("id", "")),
            title=data.get("title", ""),
            slug=data.get("slug", ""),
            markets=markets,
            volume=float(data.get("volume", 0) or 0),
            liquidity=float(data.get("liquidity", 0) or 0),
        )


class GammaClient:
    """Client for the Polymarket Gamma (market data) API."""

    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.api.gamma_host
        self._http = httpx.Client(timeout=30.0)
        self._last_request = 0.0

    def _rate_limit(self) -> None:
        now = time.time()
        if now - self._last_request < 0.2:
            time.sleep(0.2 - (now - self._last_request))
        self._last_request = time.time()

    def _get(self, endpoint: str, params: dict | None = None) -> Any:
        self._rate_limit()
        url = f"{self.base_url}{endpoint}"
        resp = self._http.get(url, params=params or {})
        resp.raise_for_status()
        return resp.json()

    def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
        order: str = "liquidity",
        ascending: bool = False,
        liquidity_min: float | None = None,
        volume_min: float | None = None,
        tag_slug: str | None = None,
    ) -> list[Market]:
        """Fetch markets from Gamma API with filters."""
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "order": order,
            "ascending": str(ascending).lower(),
        }
        if liquidity_min is not None:
            params["liquidity_num_min"] = liquidity_min
        if volume_min is not None:
            params["volume_num_min"] = volume_min
        if tag_slug is not None:
            params["tag_slug"] = tag_slug

        data = self._get("/markets", params)
        if isinstance(data, list):
            return [Market.from_raw(m) for m in data]
        return []

    def get_all_active_markets(
        self,
        min_liquidity: float = 0,
        min_volume: float = 0,
        max_pages: int = 20,
    ) -> list[Market]:
        """Fetch all active markets with pagination."""
        all_markets: list[Market] = []
        offset = 0
        page_size = 100

        for _ in range(max_pages):
            batch = self.get_markets(
                limit=page_size,
                offset=offset,
                active=True,
                closed=False,
                liquidity_min=min_liquidity,
                volume_min=min_volume,
            )
            if not batch:
                break
            all_markets.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size

        return all_markets

    def get_market(self, market_id: str) -> Market | None:
        """Fetch a single market by ID or slug."""
        try:
            data = self._get(f"/markets/{market_id}")
            return Market.from_raw(data) if data else None
        except httpx.HTTPStatusError:
            return None

    def get_events(
        self,
        limit: int = 50,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
        order: str = "liquidity",
        ascending: bool = False,
        liquidity_min: float | None = None,
    ) -> list[Event]:
        """Fetch events from Gamma API."""
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "order": order,
            "ascending": str(ascending).lower(),
        }
        if liquidity_min is not None:
            params["liquidity_num_min"] = liquidity_min

        data = self._get("/events", params)
        if isinstance(data, list):
            return [Event.from_raw(e) for e in data]
        return []

    def get_multi_outcome_events(
        self,
        min_liquidity: float = 1000,
        min_markets: int = 3,
    ) -> list[Event]:
        """Find events with multiple markets — useful for arbitrage scanning."""
        events = self.get_events(limit=100, liquidity_min=min_liquidity)
        return [e for e in events if len(e.markets) >= min_markets]

    def close(self) -> None:
        self._http.close()
