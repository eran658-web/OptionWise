"""Statistical value trading strategy for Polymarket.

Identifies mispriced markets by combining:
1. Mean reversion: Markets that have deviated significantly from recent averages
2. Volume/price divergence: Markets where volume trends disagree with price direction
3. Fair value estimation: Using order book depth and historical data

This is a medium-frequency strategy that looks for structural inefficiencies
rather than short-term price movements.
"""

from __future__ import annotations

import logging
import statistics
from typing import TYPE_CHECKING

from polymarket_bot.strategies.base import BaseStrategy, Signal
from polymarket_bot.utils.helpers import (
    calculate_edge_pct,
    calculate_expected_value,
    calculate_kelly_fraction,
    calculate_position_size,
    round_price,
    round_size,
    weighted_midpoint,
)

if TYPE_CHECKING:
    from polymarket_bot.api.clob_client import PolymarketCLOB
    from polymarket_bot.api.gamma_client import GammaClient, Market
    from polymarket_bot.config.settings import Settings
    from polymarket_bot.risk.manager import RiskManager

logger = logging.getLogger(__name__)


class ValueStrategy(BaseStrategy):
    """Find and exploit statistical mispricings in prediction markets."""

    name = "value"

    def __init__(
        self,
        settings: Settings,
        clob: PolymarketCLOB,
        gamma: GammaClient,
        risk_manager: RiskManager,
    ) -> None:
        super().__init__(settings, clob, gamma, risk_manager)
        self.cfg = settings.value
        # Price history for mean reversion analysis
        self._price_history: dict[str, list[float]] = {}

    def scan(self, markets: list[Market]) -> list[Signal]:
        """Scan markets for statistical value opportunities."""
        if not self.cfg.enabled:
            return []

        signals: list[Signal] = []

        for market in markets:
            if not self._is_eligible(market):
                continue

            market_signals = self._analyze_market(market)
            signals.extend(market_signals)

        return signals

    def _is_eligible(self, market: Market) -> bool:
        """Filter markets that meet minimum criteria for analysis."""
        if not market.enable_order_book or market.closed:
            return False
        if market.volume < self.cfg.min_volume_usd:
            return False
        if market.liquidity < self.cfg.min_liquidity_usd:
            return False
        if len(market.clob_token_ids) < 2:
            return False
        # Skip extreme prices — insufficient edge
        for price in market.outcome_prices:
            if price < 0.05 or price > 0.95:
                return False
        return True

    def _analyze_market(self, market: Market) -> list[Signal]:
        """Run multiple analysis methods on a single market."""
        signals: list[Signal] = []

        token_id = market.clob_token_ids[0]

        try:
            book = self.clob.get_order_book(token_id)
        except Exception as e:
            self.logger.debug("Book fetch failed for %s: %s", token_id[:12], e)
            return signals

        if not book.bids or not book.asks:
            return signals

        fair_value = weighted_midpoint(book.bids, book.asks, depth=5)

        # Update price history
        history = self._price_history.setdefault(token_id, [])
        history.append(fair_value)
        if len(history) > self.cfg.lookback_trades:
            history[:] = history[-self.cfg.lookback_trades :]

        # 1. Mean reversion signal
        mr_signal = self._mean_reversion_signal(market, token_id, book, history)
        if mr_signal:
            signals.append(mr_signal)

        # 2. Order book imbalance signal
        obi_signal = self._order_book_imbalance_signal(market, token_id, book, fair_value)
        if obi_signal:
            signals.append(obi_signal)

        # 3. Binary complement mispricing
        comp_signal = self._complement_mispricing_signal(market, fair_value)
        if comp_signal:
            signals.append(comp_signal)

        return signals

    def _mean_reversion_signal(
        self,
        market: Market,
        token_id: str,
        book,
        history: list[float],
    ) -> Signal | None:
        """Detect mean reversion opportunities.

        When a price deviates significantly from its recent average,
        bet on it reverting toward the mean.
        """
        if len(history) < self.cfg.momentum_window:
            return None

        recent = history[-self.cfg.momentum_window :]
        mean_price = statistics.mean(recent)
        if len(recent) < 2:
            return None
        std_price = statistics.stdev(recent)

        current = book.mid
        if std_price == 0 or mean_price == 0:
            return None

        z_score = (current - mean_price) / std_price
        deviation_pct = abs(current - mean_price) / mean_price * 100

        if deviation_pct < self.cfg.mean_reversion_threshold_pct:
            return None

        # Price too high → expect reversion down → SELL (or buy NO)
        # Price too low → expect reversion up → BUY
        if z_score > 1.5:
            side = "SELL"
            price = round_price(book.best_bid, market.minimum_tick_size)
            estimated_fair = mean_price
        elif z_score < -1.5:
            side = "BUY"
            price = round_price(book.best_ask, market.minimum_tick_size)
            estimated_fair = mean_price
        else:
            return None

        edge = calculate_edge_pct(price, estimated_fair) if side == "BUY" else \
               calculate_edge_pct(1 - price, 1 - estimated_fair)

        if abs(edge) < self.settings.risk.min_edge_pct:
            return None

        ev = calculate_expected_value(price, estimated_fair) if side == "BUY" else \
             calculate_expected_value(1 - price, 1 - estimated_fair)

        kelly = calculate_kelly_fraction(
            win_prob=max(0.01, min(0.99, estimated_fair)) if side == "BUY"
                     else max(0.01, min(0.99, 1 - estimated_fair)),
            win_payout=(1 - price) / price if price > 0 else 0,
            fractional=self.settings.risk.kelly_fraction,
        )

        size_usd = calculate_position_size(
            bankroll=self.settings.risk.max_total_exposure_usd,
            kelly_fraction=kelly,
            max_position_usd=self.cfg.max_position_per_trade_usd,
            min_order_size=market.minimum_order_size,
        )

        if size_usd <= 0:
            return None

        self.logger.info(
            "MEAN REVERSION: %s z=%.2f dev=%.1f%% → %s @ %.4f (fair=%.4f, edge=%.1f%%)",
            market.question[:50],
            z_score,
            deviation_pct,
            side,
            price,
            estimated_fair,
            edge,
        )

        return Signal(
            strategy=self.name,
            market_id=market.id,
            token_id=token_id,
            side=side,
            price=price,
            size_usd=size_usd,
            edge_pct=abs(edge),
            confidence=min(abs(z_score) / 3.0, 1.0),
            reason=f"Mean reversion: z={z_score:.2f}, dev={deviation_pct:.1f}%, fair={estimated_fair:.4f}",
            metadata={"signal_type": "mean_reversion", "z_score": z_score, "ev": ev},
        )

    def _order_book_imbalance_signal(
        self,
        market: Market,
        token_id: str,
        book,
        fair_value: float,
    ) -> Signal | None:
        """Detect order book imbalance that predicts short-term price movement.

        A strong bid imbalance suggests buying pressure → price likely to rise.
        A strong ask imbalance suggests selling pressure → price likely to fall.
        """
        bid_depth = sum(b["size"] * b["price"] for b in book.bids[:5])
        ask_depth = sum(a["size"] * a["price"] for a in book.asks[:5])

        total = bid_depth + ask_depth
        if total == 0:
            return None

        imbalance = (bid_depth - ask_depth) / total  # -1 to +1

        # Only act on strong imbalances
        if abs(imbalance) < 0.4:
            return None

        if imbalance > 0.4:
            # Strong bid side → expect price to rise → BUY
            side = "BUY"
            price = round_price(book.best_ask, market.minimum_tick_size)
            edge = imbalance * 3  # Rough edge estimate based on imbalance strength
        else:
            # Strong ask side → expect price to fall → SELL
            side = "SELL"
            price = round_price(book.best_bid, market.minimum_tick_size)
            edge = abs(imbalance) * 3

        if edge < self.settings.risk.min_edge_pct:
            return None

        size_usd = min(self.cfg.max_position_per_trade_usd * abs(imbalance), self.cfg.max_position_per_trade_usd)

        if size_usd < market.minimum_order_size:
            return None

        self.logger.info(
            "OBI SIGNAL: %s imbalance=%.2f → %s @ %.4f",
            market.question[:50],
            imbalance,
            side,
            price,
        )

        return Signal(
            strategy=self.name,
            market_id=market.id,
            token_id=token_id,
            side=side,
            price=price,
            size_usd=round_size(size_usd),
            edge_pct=edge,
            confidence=min(abs(imbalance), 1.0),
            reason=f"Order book imbalance: {imbalance:.2f} (bid_depth=${bid_depth:.0f}, ask_depth=${ask_depth:.0f})",
            metadata={"signal_type": "obi", "imbalance": imbalance},
        )

    def _complement_mispricing_signal(
        self,
        market: Market,
        yes_fair_value: float,
    ) -> Signal | None:
        """Check if the NO side is mispriced relative to the YES side.

        In a binary market: YES + NO should = $1.00.
        If YES fair value is X, NO should trade near (1 - X).
        Deviations create value opportunities.
        """
        if len(market.clob_token_ids) != 2:
            return None

        no_token = market.clob_token_ids[1]

        try:
            no_book = self.clob.get_order_book(no_token)
        except Exception:
            return None

        if not no_book.bids or not no_book.asks:
            return None

        no_mid = no_book.mid
        no_fair = 1.0 - yes_fair_value

        mispricing = no_mid - no_fair
        mispricing_pct = abs(mispricing) / no_fair * 100 if no_fair > 0 else 0

        if mispricing_pct < self.settings.risk.min_edge_pct:
            return None

        if mispricing > 0:
            # NO is overpriced → SELL NO (or equivalently, BUY YES)
            side = "SELL"
            price = round_price(no_book.best_bid, market.minimum_tick_size)
        else:
            # NO is underpriced → BUY NO
            side = "BUY"
            price = round_price(no_book.best_ask, market.minimum_tick_size)

        size_usd = min(self.cfg.max_position_per_trade_usd * 0.5, self.cfg.max_position_per_trade_usd)

        if size_usd < market.minimum_order_size:
            return None

        self.logger.info(
            "COMPLEMENT MISPRICING: %s NO_mid=%.4f, NO_fair=%.4f, diff=%.1f%% → %s",
            market.question[:50],
            no_mid,
            no_fair,
            mispricing_pct,
            side,
        )

        return Signal(
            strategy=self.name,
            market_id=market.id,
            token_id=no_token,
            side=side,
            price=price,
            size_usd=round_size(size_usd),
            edge_pct=mispricing_pct,
            confidence=min(mispricing_pct / 10.0, 1.0),
            reason=f"Complement mispricing: NO_mid={no_mid:.4f} vs fair={no_fair:.4f} ({mispricing_pct:.1f}%)",
            metadata={"signal_type": "complement", "mispricing": mispricing},
        )

    def on_fill(self, order_id: str, signal: Signal) -> None:
        self.logger.info(
            "Value trade filled: %s %s $%.2f (edge=%.1f%%)",
            signal.side,
            signal.token_id[:12],
            signal.size_usd,
            signal.edge_pct,
        )
