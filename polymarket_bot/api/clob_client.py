"""Wrapper around the Polymarket CLOB API using py-clob-client.

Provides authenticated trading operations: placing orders, canceling orders,
fetching order books, and managing positions.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    BookParams,
    MarketOrderArgs,
    OpenOrderParams,
    OrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY, SELL

from polymarket_bot.config.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class OrderBook:
    """Parsed order book for a single token."""

    token_id: str
    bids: list[dict[str, float]]  # [{"price": ..., "size": ...}, ...]
    asks: list[dict[str, float]]
    best_bid: float = 0.0
    best_ask: float = 0.0
    mid: float = 0.0
    spread: float = 0.0

    @classmethod
    def from_raw(cls, token_id: str, raw: dict) -> OrderBook:
        bids = [
            {"price": float(b["price"]), "size": float(b["size"])}
            for b in raw.get("bids", [])
        ]
        asks = [
            {"price": float(a["price"]), "size": float(a["size"])}
            for a in raw.get("asks", [])
        ]
        best_bid = bids[0]["price"] if bids else 0.0
        best_ask = asks[0]["price"] if asks else 1.0
        mid = (best_bid + best_ask) / 2 if bids and asks else 0.5
        spread = best_ask - best_bid

        return cls(
            token_id=token_id,
            bids=bids,
            asks=asks,
            best_bid=best_bid,
            best_ask=best_ask,
            mid=mid,
            spread=spread,
        )


class PolymarketCLOB:
    """High-level wrapper around the Polymarket CLOB client."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._dry_run = settings.bot.dry_run
        self._client: ClobClient | None = None
        self._authenticated = False
        self._request_count = 0
        self._last_request_time = 0.0

    def connect(self) -> None:
        """Initialize the CLOB client and authenticate."""
        cfg = self.settings.api

        if self._dry_run and not cfg.private_key:
            # Read-only client for dry run without credentials
            self._client = ClobClient(cfg.clob_host)
            logger.info("Connected in read-only mode (dry run, no private key)")
            return

        self._client = ClobClient(
            cfg.clob_host,
            key=cfg.private_key,
            chain_id=cfg.chain_id,
            signature_type=cfg.signature_type,
            funder=cfg.funder_address or None,
        )

        # Use pre-existing API creds if available, otherwise derive them
        if cfg.api_key and cfg.api_secret and cfg.api_passphrase:
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key=cfg.api_key,
                api_secret=cfg.api_secret,
                api_passphrase=cfg.api_passphrase,
            )
            self._client.set_api_creds(creds)
        else:
            creds = self._client.create_or_derive_api_creds()
            self._client.set_api_creds(creds)
            logger.info(
                "Derived API credentials — save these in your .env:\n"
                "  POLYMARKET_API_KEY=%s\n"
                "  POLYMARKET_API_SECRET=%s\n"
                "  POLYMARKET_API_PASSPHRASE=%s",
                creds.api_key,
                creds.api_secret,
                creds.api_passphrase,
            )

        self._authenticated = True
        logger.info("Connected and authenticated to Polymarket CLOB")

    def _rate_limit(self) -> None:
        """Simple rate limiter: max ~40 requests per 10 seconds."""
        now = time.time()
        if now - self._last_request_time < 0.25:
            time.sleep(0.25 - (now - self._last_request_time))
        self._last_request_time = time.time()
        self._request_count += 1

    @property
    def client(self) -> ClobClient:
        if self._client is None:
            raise RuntimeError("Not connected — call connect() first")
        return self._client

    # ── Market Data ──────────────────────────────────────────────────

    def get_order_book(self, token_id: str) -> OrderBook:
        """Fetch and parse the order book for a single token."""
        self._rate_limit()
        raw = self.client.get_order_book(token_id)
        return OrderBook.from_raw(token_id, raw)

    def get_order_books(self, token_ids: list[str]) -> list[OrderBook]:
        """Fetch order books for multiple tokens in one call."""
        self._rate_limit()
        params = [BookParams(token_id=tid) for tid in token_ids]
        raw_books = self.client.get_order_books(params)
        books = []
        for raw in raw_books:
            tid = raw.get("asset_id", raw.get("market", ""))
            books.append(OrderBook.from_raw(tid, raw))
        return books

    def get_midpoint(self, token_id: str) -> float:
        self._rate_limit()
        return float(self.client.get_midpoint(token_id))

    def get_price(self, token_id: str, side: str = "BUY") -> float:
        self._rate_limit()
        return float(self.client.get_price(token_id, side))

    def get_spread(self, token_id: str) -> float:
        self._rate_limit()
        return float(self.client.get_spread(token_id))

    def get_tick_size(self, token_id: str) -> float:
        self._rate_limit()
        return float(self.client.get_tick_size(token_id))

    def get_markets(self, next_cursor: str = "") -> tuple[list[dict], str]:
        """Fetch paginated list of markets from CLOB."""
        self._rate_limit()
        if next_cursor:
            resp = self.client.get_markets(next_cursor=next_cursor)
        else:
            resp = self.client.get_markets()
        markets = resp.get("data", resp) if isinstance(resp, dict) else resp
        cursor = ""
        if isinstance(resp, dict):
            cursor = resp.get("next_cursor", "")
        return markets if isinstance(markets, list) else [], cursor

    def get_simplified_markets(self) -> list[dict]:
        self._rate_limit()
        return self.client.get_simplified_markets()

    # ── Order Management ─────────────────────────────────────────────

    def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        post_only: bool = False,
    ) -> dict[str, Any] | None:
        """Place a GTC limit order. Returns order response or None in dry run."""
        side_const = BUY if side.upper() == "BUY" else SELL

        logger.info(
            "LIMIT ORDER: %s %.2f shares of %s @ $%.4f (post_only=%s)",
            side,
            size,
            token_id[:16],
            price,
            post_only,
        )

        if self._dry_run:
            logger.info("[DRY RUN] Order not submitted")
            return {"dry_run": True, "side": side, "price": price, "size": size}

        if not self._authenticated:
            raise RuntimeError("Cannot place orders without authentication")

        self._rate_limit()
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side_const,
        )
        signed = self.client.create_order(order_args)
        resp = self.client.post_order(signed, OrderType.GTC)
        logger.info("Order placed: %s", resp)
        return resp

    def place_market_order(
        self,
        token_id: str,
        side: str,
        amount_usd: float,
    ) -> dict[str, Any] | None:
        """Place a FOK market order for a given USD amount."""
        side_const = BUY if side.upper() == "BUY" else SELL

        logger.info(
            "MARKET ORDER: %s $%.2f of %s",
            side,
            amount_usd,
            token_id[:16],
        )

        if self._dry_run:
            logger.info("[DRY RUN] Order not submitted")
            return {"dry_run": True, "side": side, "amount": amount_usd}

        if not self._authenticated:
            raise RuntimeError("Cannot place orders without authentication")

        self._rate_limit()
        mo_args = MarketOrderArgs(
            token_id=token_id,
            amount=amount_usd,
            side=side_const,
        )
        signed = self.client.create_market_order(mo_args)
        resp = self.client.post_order(signed, OrderType.FOK)
        logger.info("Market order placed: %s", resp)
        return resp

    def cancel_order(self, order_id: str) -> dict | None:
        if self._dry_run:
            logger.info("[DRY RUN] Cancel order %s", order_id)
            return {"dry_run": True, "cancelled": order_id}

        self._rate_limit()
        resp = self.client.cancel(order_id)
        logger.info("Cancelled order %s: %s", order_id, resp)
        return resp

    def cancel_all(self) -> dict | None:
        if self._dry_run:
            logger.info("[DRY RUN] Cancel all orders")
            return {"dry_run": True, "cancelled": "all"}

        self._rate_limit()
        resp = self.client.cancel_all()
        logger.info("Cancelled all orders: %s", resp)
        return resp

    def get_open_orders(self) -> list[dict]:
        self._rate_limit()
        return self.client.get_orders(OpenOrderParams())

    def get_trades(self) -> list[dict]:
        self._rate_limit()
        return self.client.get_trades()

    # ── Health ───────────────────────────────────────────────────────

    def is_healthy(self) -> bool:
        try:
            self._rate_limit()
            resp = self.client.get_ok()
            return resp == "OK" or resp is True
        except Exception as e:
            logger.error("CLOB health check failed: %s", e)
            return False
