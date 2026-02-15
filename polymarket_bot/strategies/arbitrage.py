"""Arbitrage strategy for Polymarket.

Detects two types of arbitrage:
1. Binary arbitrage: YES + NO prices sum to less than $1.00
2. Multi-outcome arbitrage: All outcome prices in an event sum to less than $1.00

These are the lowest-risk opportunities in prediction markets since they
produce profit regardless of the outcome.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from polymarket_bot.strategies.base import BaseStrategy, Signal
from polymarket_bot.utils.helpers import (
    check_binary_arb,
    check_multi_outcome_arb,
    round_price,
    round_size,
)

if TYPE_CHECKING:
    from polymarket_bot.api.clob_client import PolymarketCLOB
    from polymarket_bot.api.gamma_client import GammaClient, Market
    from polymarket_bot.config.settings import Settings
    from polymarket_bot.risk.manager import RiskManager

logger = logging.getLogger(__name__)


class ArbitrageStrategy(BaseStrategy):
    """Detect and exploit arbitrage opportunities in Polymarket."""

    name = "arbitrage"

    def __init__(
        self,
        settings: Settings,
        clob: PolymarketCLOB,
        gamma: GammaClient,
        risk_manager: RiskManager,
    ) -> None:
        super().__init__(settings, clob, gamma, risk_manager)
        self.cfg = settings.arbitrage
        self._recent_arbs: dict[str, float] = {}  # market_id -> last_arb_time

    def scan(self, markets: list[Market]) -> list[Signal]:
        """Scan for arbitrage opportunities across all provided markets."""
        if not self.cfg.enabled:
            return []

        signals: list[Signal] = []

        # 1. Binary market arbitrage (YES + NO < $1.00)
        signals.extend(self._scan_binary_arb(markets))

        # 2. Multi-outcome event arbitrage
        signals.extend(self._scan_multi_outcome_arb())

        if signals:
            self.logger.info("Found %d arbitrage opportunities", len(signals))

        return signals

    def _scan_binary_arb(self, markets: list[Market]) -> list[Signal]:
        """Scan binary markets where YES + NO < $1.00."""
        signals: list[Signal] = []

        for market in markets:
            if not market.enable_order_book or market.closed:
                continue
            if len(market.clob_token_ids) != 2:
                continue
            if len(market.outcome_prices) != 2:
                continue

            yes_token = market.clob_token_ids[0]
            no_token = market.clob_token_ids[1]

            try:
                yes_book = self.clob.get_order_book(yes_token)
                no_book = self.clob.get_order_book(no_token)
            except Exception as e:
                self.logger.debug("Failed to fetch books for %s: %s", market.id, e)
                continue

            # For arb: we want the lowest ask for each side
            if not yes_book.asks or not no_book.asks:
                continue

            yes_ask = yes_book.asks[0]["price"]
            no_ask = no_book.asks[0]["price"]
            yes_ask_size = yes_book.asks[0]["size"]
            no_ask_size = no_book.asks[0]["size"]

            profit = check_binary_arb(yes_ask, no_ask)
            profit_pct = profit * 100

            if profit_pct < self.cfg.min_profit_pct:
                continue

            # Size the arb to the smaller side
            max_usd = self.cfg.max_position_per_arb_usd
            yes_available_usd = yes_ask * yes_ask_size
            no_available_usd = no_ask * no_ask_size
            arb_size = min(max_usd, yes_available_usd, no_available_usd)

            if arb_size < market.minimum_order_size:
                continue

            self.logger.info(
                "BINARY ARB: %s — YES@%.4f + NO@%.4f = %.4f (profit=%.2f%%) size=$%.2f",
                market.question[:60],
                yes_ask,
                no_ask,
                yes_ask + no_ask,
                profit_pct,
                arb_size,
            )

            # Generate BUY signals for both sides
            yes_shares = arb_size / (yes_ask + no_ask)  # equal allocation
            no_shares = yes_shares

            signals.append(Signal(
                strategy=self.name,
                market_id=market.id,
                token_id=yes_token,
                side="BUY",
                price=round_price(yes_ask, market.minimum_tick_size),
                size_usd=round_size(yes_shares * yes_ask),
                edge_pct=profit_pct,
                confidence=min(profit_pct / 5.0, 1.0),
                reason=f"Binary arb: YES+NO={yes_ask+no_ask:.4f}, profit={profit_pct:.2f}%",
                metadata={"arb_type": "binary", "pair_token": no_token},
            ))
            signals.append(Signal(
                strategy=self.name,
                market_id=market.id,
                token_id=no_token,
                side="BUY",
                price=round_price(no_ask, market.minimum_tick_size),
                size_usd=round_size(no_shares * no_ask),
                edge_pct=profit_pct,
                confidence=min(profit_pct / 5.0, 1.0),
                reason=f"Binary arb: YES+NO={yes_ask+no_ask:.4f}, profit={profit_pct:.2f}%",
                metadata={"arb_type": "binary", "pair_token": yes_token},
            ))

        return signals

    def _scan_multi_outcome_arb(self) -> list[Signal]:
        """Scan multi-outcome events where all outcome prices sum < $1.00."""
        signals: list[Signal] = []

        try:
            events = self.gamma.get_multi_outcome_events(
                min_liquidity=self.settings.bot.min_market_liquidity_usd,
                min_markets=3,
            )
        except Exception as e:
            self.logger.error("Failed to fetch events: %s", e)
            return signals

        for event in events:
            active_markets = [
                m for m in event.markets
                if m.enable_order_book and not m.closed and m.clob_token_ids
            ]
            if len(active_markets) < 3:
                continue

            # Fetch best ask prices for all outcomes
            ask_prices: list[tuple[Market, float, float]] = []  # (market, ask_price, ask_size)
            for m in active_markets:
                if not m.clob_token_ids:
                    continue
                # For multi-outcome, each market's YES token represents one outcome
                yes_token = m.clob_token_ids[0]
                try:
                    book = self.clob.get_order_book(yes_token)
                    if book.asks:
                        ask_prices.append((m, book.asks[0]["price"], book.asks[0]["size"]))
                except Exception:
                    continue

            if len(ask_prices) < 3:
                continue

            prices = [p for _, p, _ in ask_prices]
            profit = check_multi_outcome_arb(prices)
            profit_pct = profit * 100

            if profit_pct < self.cfg.min_profit_pct:
                continue

            self.logger.info(
                "MULTI-OUTCOME ARB: %s — sum=%.4f (profit=%.2f%%), %d outcomes",
                event.title[:60],
                sum(prices),
                profit_pct,
                len(ask_prices),
            )

            # Size: buy equal amounts of each outcome
            total_cost = sum(prices)
            max_usd = self.cfg.max_position_per_arb_usd
            max_sets = max_usd / total_cost if total_cost > 0 else 0
            min_available = min(sz for _, _, sz in ask_prices)
            sets_to_buy = min(max_sets, min_available)

            for market, ask_price, _ in ask_prices:
                yes_token = market.clob_token_ids[0]
                size_usd = round_size(sets_to_buy * ask_price)
                if size_usd < market.minimum_order_size:
                    continue

                signals.append(Signal(
                    strategy=self.name,
                    market_id=market.id,
                    token_id=yes_token,
                    side="BUY",
                    price=round_price(ask_price, market.minimum_tick_size),
                    size_usd=size_usd,
                    edge_pct=profit_pct,
                    confidence=min(profit_pct / 5.0, 1.0),
                    reason=f"Multi-outcome arb: sum={total_cost:.4f}, profit={profit_pct:.2f}%",
                    metadata={
                        "arb_type": "multi_outcome",
                        "event_id": event.id,
                        "num_outcomes": len(ask_prices),
                    },
                ))

        return signals

    def on_fill(self, order_id: str, signal: Signal) -> None:
        self.logger.info(
            "Arb order filled: %s %s $%.2f (edge=%.1f%%)",
            signal.side,
            signal.token_id[:12],
            signal.size_usd,
            signal.edge_pct,
        )
