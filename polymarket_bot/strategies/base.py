"""Base strategy class defining the interface for all trading strategies."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polymarket_bot.api.clob_client import PolymarketCLOB
    from polymarket_bot.api.gamma_client import GammaClient, Market
    from polymarket_bot.config.settings import Settings
    from polymarket_bot.risk.manager import RiskManager


@dataclass
class Signal:
    """A trading signal produced by a strategy."""

    strategy: str
    market_id: str
    token_id: str
    side: str  # "BUY" or "SELL"
    price: float
    size_usd: float
    edge_pct: float
    confidence: float  # 0-1
    reason: str = ""
    metadata: dict = field(default_factory=dict)


class BaseStrategy(ABC):
    """Abstract base class for trading strategies."""

    name: str = "base"

    def __init__(
        self,
        settings: Settings,
        clob: PolymarketCLOB,
        gamma: GammaClient,
        risk_manager: RiskManager,
    ) -> None:
        self.settings = settings
        self.clob = clob
        self.gamma = gamma
        self.risk_manager = risk_manager
        self.logger = logging.getLogger(f"strategy.{self.name}")
        self._active = True

    @abstractmethod
    def scan(self, markets: list[Market]) -> list[Signal]:
        """Scan markets and return a list of trading signals.

        This is the main strategy loop. It should analyze the provided
        markets and return any signals that meet the strategy's criteria.
        """
        ...

    @abstractmethod
    def on_fill(self, order_id: str, signal: Signal) -> None:
        """Called when an order from this strategy is filled."""
        ...

    def execute_signals(self, signals: list[Signal]) -> list[dict]:
        """Execute a list of signals through the risk manager and CLOB.

        Returns list of order responses.
        """
        results = []
        for signal in signals:
            if not self._active:
                break

            # Ask the risk manager if this trade is allowed
            approved, reason = self.risk_manager.check_trade(signal)
            if not approved:
                self.logger.info(
                    "Signal rejected by risk manager: %s — %s",
                    signal.reason,
                    reason,
                )
                continue

            # Size the position through the risk manager
            sized_usd = self.risk_manager.size_position(signal)
            if sized_usd <= 0:
                self.logger.debug("Position sized to 0, skipping: %s", signal.reason)
                continue

            signal.size_usd = sized_usd

            try:
                # Convert USD size to shares
                shares = signal.size_usd / signal.price if signal.price > 0 else 0
                if shares <= 0:
                    continue

                resp = self.clob.place_limit_order(
                    token_id=signal.token_id,
                    side=signal.side,
                    price=signal.price,
                    size=shares,
                )

                if resp:
                    self.risk_manager.record_trade(signal)
                    results.append(resp)
                    self.logger.info(
                        "Executed: %s %s $%.2f @ %.4f (edge=%.1f%%)",
                        signal.side,
                        signal.token_id[:12],
                        signal.size_usd,
                        signal.price,
                        signal.edge_pct,
                    )
            except Exception as e:
                self.logger.error("Failed to execute signal: %s", e, exc_info=True)

        return results

    def stop(self) -> None:
        self._active = False
        self.logger.info("Strategy %s stopped", self.name)
