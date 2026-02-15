"""Market making strategy for Polymarket.

Provides liquidity by placing limit orders on both sides of the book.
Earns the spread between bid and ask while managing inventory risk.

This strategy is most profitable in:
- Markets with moderate liquidity (not too thin, not already tight)
- Markets with stable, mean-reverting price behavior
- Markets with sufficient trading volume to fill orders regularly
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from polymarket_bot.strategies.base import BaseStrategy, Signal
from polymarket_bot.utils.helpers import round_price, round_size, weighted_midpoint

if TYPE_CHECKING:
    from polymarket_bot.api.clob_client import PolymarketCLOB
    from polymarket_bot.api.gamma_client import GammaClient, Market
    from polymarket_bot.config.settings import Settings
    from polymarket_bot.risk.manager import RiskManager

logger = logging.getLogger(__name__)


class MarketMakingStrategy(BaseStrategy):
    """Market making: provide liquidity by quoting both sides."""

    name = "market_making"

    def __init__(
        self,
        settings: Settings,
        clob: PolymarketCLOB,
        gamma: GammaClient,
        risk_manager: RiskManager,
    ) -> None:
        super().__init__(settings, clob, gamma, risk_manager)
        self.cfg = settings.market_making
        # Track our active quotes per token
        self._active_quotes: dict[str, dict] = {}
        # Track inventory per token: positive = long, negative = short
        self._inventory: dict[str, float] = {}
        self._last_refresh: dict[str, float] = {}

    def scan(self, markets: list[Market]) -> list[Signal]:
        """Identify markets suitable for market making and generate quotes."""
        if not self.cfg.enabled:
            return []

        signals: list[Signal] = []

        for market in markets:
            if not self._is_suitable(market):
                continue

            token_signals = self._generate_quotes(market)
            signals.extend(token_signals)

        return signals

    def _is_suitable(self, market: Market) -> bool:
        """Check if a market is suitable for market making."""
        if not market.enable_order_book or market.closed:
            return False
        if len(market.clob_token_ids) < 2:
            return False
        if market.liquidity < self.cfg.min_liquidity_usd:
            return False
        # Avoid extreme prices (near 0 or 1) where risk/reward is skewed
        for price in market.outcome_prices:
            if price < 0.05 or price > 0.95:
                return False
        return True

    def _generate_quotes(self, market: Market) -> list[Signal]:
        """Generate bid/ask quotes for a market."""
        signals: list[Signal] = []

        # Work with the YES token (index 0)
        if not market.clob_token_ids:
            return signals

        token_id = market.clob_token_ids[0]

        # Check refresh timing
        now = time.time()
        last = self._last_refresh.get(token_id, 0)
        if now - last < self.cfg.refresh_interval_sec:
            return signals

        try:
            book = self.clob.get_order_book(token_id)
        except Exception as e:
            self.logger.debug("Failed to fetch book for %s: %s", token_id[:12], e)
            return signals

        if not book.bids or not book.asks:
            return signals

        # Calculate fair value using volume-weighted midpoint
        fair_value = weighted_midpoint(book.bids, book.asks, depth=5)

        # Check if the existing spread is already too tight for us
        current_spread_bps = (book.spread / fair_value * 10000) if fair_value > 0 else 0
        if current_spread_bps < self.cfg.spread_bps * 0.5:
            # Market is already tight, skip — we can't be competitive
            return signals

        # Calculate our quote prices
        half_spread = (self.cfg.spread_bps / 10000) / 2
        bid_price = round_price(fair_value * (1 - half_spread), market.minimum_tick_size)
        ask_price = round_price(fair_value * (1 + half_spread), market.minimum_tick_size)

        # Clamp prices to valid range
        bid_price = max(0.01, min(0.99, bid_price))
        ask_price = max(0.01, min(0.99, ask_price))

        if bid_price >= ask_price:
            return signals

        # Adjust for inventory — skew quotes to reduce inventory risk
        inventory = self._inventory.get(token_id, 0)
        max_inventory = self.cfg.max_inventory_usd
        if max_inventory > 0 and abs(inventory) > 0:
            skew = (inventory / max_inventory) * half_spread * 0.5
            bid_price = round_price(bid_price - skew, market.minimum_tick_size)
            ask_price = round_price(ask_price - skew, market.minimum_tick_size)

        order_size = self.cfg.order_size_usd
        bid_shares = round_size(order_size / bid_price) if bid_price > 0 else 0
        ask_shares = round_size(order_size / ask_price) if ask_price > 0 else 0

        # Check inventory limits before quoting
        if inventory < max_inventory and bid_shares > 0:
            signals.append(Signal(
                strategy=self.name,
                market_id=market.id,
                token_id=token_id,
                side="BUY",
                price=bid_price,
                size_usd=round_size(bid_shares * bid_price),
                edge_pct=self.cfg.spread_bps / 100,
                confidence=0.5,
                reason=f"MM bid: fair={fair_value:.4f}, spread={self.cfg.spread_bps}bps",
                metadata={"mm_type": "bid", "fair_value": fair_value},
            ))

        if inventory > -max_inventory and ask_shares > 0:
            signals.append(Signal(
                strategy=self.name,
                market_id=market.id,
                token_id=token_id,
                side="SELL",
                price=ask_price,
                size_usd=round_size(ask_shares * ask_price),
                edge_pct=self.cfg.spread_bps / 100,
                confidence=0.5,
                reason=f"MM ask: fair={fair_value:.4f}, spread={self.cfg.spread_bps}bps",
                metadata={"mm_type": "ask", "fair_value": fair_value},
            ))

        self._last_refresh[token_id] = now
        return signals

    def cancel_stale_quotes(self) -> None:
        """Cancel quotes that have drifted too far from current mid."""
        for token_id, quote_info in list(self._active_quotes.items()):
            try:
                mid = self.clob.get_midpoint(token_id)
                entry_mid = quote_info.get("mid", mid)
                drift_pct = abs(mid - entry_mid) / entry_mid * 100 if entry_mid > 0 else 0

                if drift_pct > self.cfg.max_price_drift_pct:
                    self.logger.info(
                        "Price drifted %.1f%% for %s, cancelling quotes",
                        drift_pct,
                        token_id[:12],
                    )
                    for oid in quote_info.get("order_ids", []):
                        self.clob.cancel_order(oid)
                    del self._active_quotes[token_id]
            except Exception as e:
                self.logger.debug("Error checking quote staleness: %s", e)

    def on_fill(self, order_id: str, signal: Signal) -> None:
        """Update inventory tracking when an order is filled."""
        token_id = signal.token_id
        current = self._inventory.get(token_id, 0)

        if signal.side == "BUY":
            self._inventory[token_id] = current + signal.size_usd
        else:
            self._inventory[token_id] = current - signal.size_usd

        self.logger.info(
            "MM fill: %s %s $%.2f — inventory now $%.2f",
            signal.side,
            token_id[:12],
            signal.size_usd,
            self._inventory[token_id],
        )
