"""Risk management system for the Polymarket trading bot.

Enforces position limits, daily loss limits, drawdown limits,
and portfolio-level diversification constraints before any trade is executed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from polymarket_bot.utils.helpers import calculate_kelly_fraction, calculate_position_size

if TYPE_CHECKING:
    from polymarket_bot.config.settings import Settings
    from polymarket_bot.strategies.base import Signal

logger = logging.getLogger(__name__)

# Seconds in a day
SECONDS_PER_DAY = 86400


@dataclass
class Position:
    """Tracks an open position."""

    market_id: str
    token_id: str
    side: str
    entry_price: float
    size_usd: float
    current_price: float = 0.0
    pnl: float = 0.0
    timestamp: float = 0.0
    strategy: str = ""


@dataclass
class DailyStats:
    """Daily trading statistics."""

    date: str = ""
    trades_count: int = 0
    total_volume_usd: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    max_drawdown_pct: float = 0.0
    peak_equity: float = 0.0


class RiskManager:
    """Centralized risk management for all strategies.

    Checks and enforces:
    - Per-trade size limits
    - Per-market position limits
    - Total portfolio exposure limits
    - Concurrent position count limits
    - Daily loss limits
    - Maximum drawdown limits
    - Minimum edge requirements
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cfg = settings.risk

        # Portfolio state
        self._positions: dict[str, Position] = {}  # token_id -> Position
        self._daily_stats = DailyStats()
        self._total_pnl: float = 0.0
        self._peak_equity: float = 0.0
        self._starting_equity: float = self.cfg.max_total_exposure_usd

        # Trade log for analysis
        self._trade_log: list[dict] = []

        # Cooldown: pause trading if limits hit
        self._paused = False
        self._pause_reason = ""
        self._pause_until: float = 0.0

    @property
    def total_exposure(self) -> float:
        """Total USD exposure across all open positions."""
        return sum(p.size_usd for p in self._positions.values())

    @property
    def position_count(self) -> int:
        return len(self._positions)

    @property
    def is_paused(self) -> bool:
        if self._paused and time.time() > self._pause_until:
            self._paused = False
            self._pause_reason = ""
            logger.info("Risk pause expired, resuming trading")
        return self._paused

    def check_trade(self, signal: Signal) -> tuple[bool, str]:
        """Check if a proposed trade passes all risk checks.

        Returns (approved: bool, reason: str).
        """
        # Check if trading is paused
        if self.is_paused:
            return False, f"Trading paused: {self._pause_reason}"

        # 1. Minimum edge check
        if signal.edge_pct < self.cfg.min_edge_pct:
            return False, f"Edge {signal.edge_pct:.1f}% below minimum {self.cfg.min_edge_pct}%"

        # 2. Per-trade size limit
        if signal.size_usd > self.cfg.max_position_size_usd:
            return False, f"Size ${signal.size_usd:.2f} exceeds max ${self.cfg.max_position_size_usd:.2f}"

        # 3. Total exposure limit
        new_exposure = self.total_exposure + signal.size_usd
        if new_exposure > self.cfg.max_total_exposure_usd:
            return False, (
                f"Would exceed total exposure: ${new_exposure:.2f} > "
                f"${self.cfg.max_total_exposure_usd:.2f}"
            )

        # 4. Max concurrent positions
        if signal.token_id not in self._positions:
            if self.position_count >= self.cfg.max_positions:
                return False, f"At max positions: {self.position_count}/{self.cfg.max_positions}"

        # 5. Daily loss limit
        if self._daily_stats.realized_pnl < -self.cfg.max_daily_loss_usd:
            self._pause_trading(
                f"Daily loss limit hit: ${abs(self._daily_stats.realized_pnl):.2f}",
                duration_sec=3600,  # pause for 1 hour
            )
            return False, f"Daily loss limit exceeded"

        # 6. Maximum drawdown
        current_equity = self._starting_equity + self._total_pnl
        if self._peak_equity > 0:
            drawdown_pct = (self._peak_equity - current_equity) / self._peak_equity * 100
            if drawdown_pct > self.cfg.max_drawdown_pct:
                self._pause_trading(
                    f"Max drawdown hit: {drawdown_pct:.1f}%",
                    duration_sec=7200,  # pause for 2 hours
                )
                return False, f"Max drawdown {drawdown_pct:.1f}% exceeded limit {self.cfg.max_drawdown_pct}%"

        # 7. Per-trade loss limit
        max_loss = signal.size_usd  # worst case: lose entire position
        if max_loss > self.cfg.max_loss_per_trade_usd:
            return False, f"Potential loss ${max_loss:.2f} exceeds per-trade limit ${self.cfg.max_loss_per_trade_usd:.2f}"

        return True, "approved"

    def size_position(self, signal: Signal) -> float:
        """Determine the appropriate position size for a signal.

        Uses fractional Kelly criterion, capped by risk limits.
        """
        # Calculate Kelly fraction based on signal confidence
        if signal.confidence <= 0 or signal.price <= 0 or signal.price >= 1:
            return 0.0

        win_payout = (1 - signal.price) / signal.price
        kelly = calculate_kelly_fraction(
            win_prob=signal.confidence,
            win_payout=win_payout,
            fractional=self.cfg.kelly_fraction,
        )

        # Calculate position size
        available = self.cfg.max_total_exposure_usd - self.total_exposure
        size = calculate_position_size(
            bankroll=available,
            kelly_fraction=kelly,
            max_position_usd=min(
                self.cfg.max_position_size_usd,
                signal.size_usd,  # respect strategy's own limit
                self.cfg.max_loss_per_trade_usd,
            ),
        )

        return size

    def record_trade(self, signal: Signal) -> None:
        """Record a trade execution for tracking."""
        position = Position(
            market_id=signal.market_id,
            token_id=signal.token_id,
            side=signal.side,
            entry_price=signal.price,
            size_usd=signal.size_usd,
            current_price=signal.price,
            timestamp=time.time(),
            strategy=signal.strategy,
        )

        # Update or create position
        existing = self._positions.get(signal.token_id)
        if existing:
            # Average into position
            total_size = existing.size_usd + signal.size_usd
            existing.entry_price = (
                existing.entry_price * existing.size_usd
                + signal.price * signal.size_usd
            ) / total_size
            existing.size_usd = total_size
        else:
            self._positions[signal.token_id] = position

        # Update daily stats
        self._daily_stats.trades_count += 1
        self._daily_stats.total_volume_usd += signal.size_usd

        # Trade log
        self._trade_log.append({
            "timestamp": time.time(),
            "strategy": signal.strategy,
            "market_id": signal.market_id,
            "token_id": signal.token_id,
            "side": signal.side,
            "price": signal.price,
            "size_usd": signal.size_usd,
            "edge_pct": signal.edge_pct,
        })

        logger.info(
            "Trade recorded: %s %s $%.2f @ %.4f (edge=%.1f%%, positions=%d, exposure=$%.2f)",
            signal.side,
            signal.token_id[:12],
            signal.size_usd,
            signal.price,
            signal.edge_pct,
            self.position_count,
            self.total_exposure,
        )

    def update_positions(self, price_updates: dict[str, float]) -> None:
        """Update position PnL with current prices and check stop losses.

        Args:
            price_updates: mapping of token_id -> current_price
        """
        positions_to_close: list[str] = []

        for token_id, position in self._positions.items():
            if token_id in price_updates:
                current_price = price_updates[token_id]
                position.current_price = current_price

                if position.side == "BUY":
                    position.pnl = (current_price - position.entry_price) * (
                        position.size_usd / position.entry_price
                    )
                else:
                    position.pnl = (position.entry_price - current_price) * (
                        position.size_usd / position.entry_price
                    )

                # Check stop loss
                loss_pct = -position.pnl / position.size_usd * 100 if position.size_usd > 0 else 0
                if loss_pct > self.cfg.stop_loss_pct:
                    logger.warning(
                        "STOP LOSS triggered for %s: loss=%.1f%% ($%.2f)",
                        token_id[:12],
                        loss_pct,
                        position.pnl,
                    )
                    positions_to_close.append(token_id)

        # Mark positions for closing (actual closing done by bot)
        for token_id in positions_to_close:
            pos = self._positions[token_id]
            self._total_pnl += pos.pnl
            self._daily_stats.realized_pnl += pos.pnl
            del self._positions[token_id]

        # Update peak equity for drawdown tracking
        current_equity = self._starting_equity + self._total_pnl
        self._peak_equity = max(self._peak_equity, current_equity)

    def close_position(self, token_id: str, exit_price: float) -> float:
        """Close a position and realize PnL."""
        position = self._positions.pop(token_id, None)
        if not position:
            return 0.0

        if position.side == "BUY":
            pnl = (exit_price - position.entry_price) * (
                position.size_usd / position.entry_price
            )
        else:
            pnl = (position.entry_price - exit_price) * (
                position.size_usd / position.entry_price
            )

        self._total_pnl += pnl
        self._daily_stats.realized_pnl += pnl

        logger.info(
            "Position closed: %s %s entry=%.4f exit=%.4f pnl=$%.2f",
            position.side,
            token_id[:12],
            position.entry_price,
            exit_price,
            pnl,
        )

        return pnl

    def get_positions_to_stop(self) -> list[str]:
        """Return token_ids of positions that hit stop loss."""
        stops = []
        for token_id, pos in self._positions.items():
            if pos.size_usd > 0:
                loss_pct = -pos.pnl / pos.size_usd * 100
                if loss_pct > self.cfg.stop_loss_pct:
                    stops.append(token_id)
        return stops

    def _pause_trading(self, reason: str, duration_sec: float = 3600) -> None:
        """Pause all trading for a specified duration."""
        self._paused = True
        self._pause_reason = reason
        self._pause_until = time.time() + duration_sec
        logger.warning("TRADING PAUSED for %ds: %s", duration_sec, reason)

    def reset_daily_stats(self) -> None:
        """Reset daily statistics (call at start of each trading day)."""
        self._daily_stats = DailyStats()
        logger.info("Daily stats reset")

    def get_summary(self) -> dict:
        """Return a summary of current risk state."""
        current_equity = self._starting_equity + self._total_pnl
        drawdown_pct = 0.0
        if self._peak_equity > 0:
            drawdown_pct = (self._peak_equity - current_equity) / self._peak_equity * 100

        unrealized_pnl = sum(p.pnl for p in self._positions.values())

        return {
            "positions": self.position_count,
            "total_exposure_usd": self.total_exposure,
            "total_pnl_usd": self._total_pnl,
            "unrealized_pnl_usd": unrealized_pnl,
            "daily_pnl_usd": self._daily_stats.realized_pnl,
            "daily_trades": self._daily_stats.trades_count,
            "drawdown_pct": drawdown_pct,
            "paused": self.is_paused,
            "pause_reason": self._pause_reason if self.is_paused else "",
        }
