"""Main bot orchestrator for the Polymarket trading bot.

Coordinates market discovery, strategy execution, risk management,
position monitoring, and graceful shutdown.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from polymarket_bot.api.clob_client import PolymarketCLOB
from polymarket_bot.api.gamma_client import GammaClient, Market
from polymarket_bot.config.settings import Settings
from polymarket_bot.risk.manager import RiskManager
from polymarket_bot.strategies.arbitrage import ArbitrageStrategy
from polymarket_bot.strategies.base import BaseStrategy
from polymarket_bot.strategies.market_making import MarketMakingStrategy
from polymarket_bot.strategies.value import ValueStrategy
from polymarket_bot.utils.helpers import format_usd

logger = logging.getLogger("polymarket_bot")


class TradingBot:
    """Main trading bot that orchestrates strategies, risk, and execution."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._running = False

        # Core components
        self.clob = PolymarketCLOB(settings)
        self.gamma = GammaClient(settings)
        self.risk_manager = RiskManager(settings)

        # Strategies
        self.strategies: list[BaseStrategy] = []

        # Market cache
        self._markets: list[Market] = []
        self._last_market_refresh: float = 0.0

        # Stats
        self._cycle_count = 0
        self._start_time: float = 0.0

    def initialize(self) -> None:
        """Initialize all components and validate configuration."""
        errors = self.settings.validate()
        if errors:
            for err in errors:
                logger.error("Config error: %s", err)
            raise ValueError(f"Configuration has {len(errors)} errors")

        # Connect to CLOB API
        logger.info("Connecting to Polymarket CLOB...")
        self.clob.connect()

        if not self.clob.is_healthy():
            raise ConnectionError("Polymarket CLOB is not responding")

        logger.info("CLOB connection healthy")

        # Initialize strategies
        if self.settings.arbitrage.enabled:
            self.strategies.append(
                ArbitrageStrategy(self.settings, self.clob, self.gamma, self.risk_manager)
            )
            logger.info("Arbitrage strategy enabled")

        if self.settings.market_making.enabled:
            self.strategies.append(
                MarketMakingStrategy(self.settings, self.clob, self.gamma, self.risk_manager)
            )
            logger.info("Market making strategy enabled")

        if self.settings.value.enabled:
            self.strategies.append(
                ValueStrategy(self.settings, self.clob, self.gamma, self.risk_manager)
            )
            logger.info("Value strategy enabled")

        if not self.strategies:
            raise ValueError("No strategies enabled — enable at least one in config")

        # Initial market refresh
        self._refresh_markets()

        logger.info(
            "Bot initialized: %d strategies, %d markets, dry_run=%s",
            len(self.strategies),
            len(self._markets),
            self.settings.bot.dry_run,
        )

    def run(self) -> None:
        """Main event loop."""
        self._running = True
        self._start_time = time.time()

        # Register signal handlers for graceful shutdown (main thread only)
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)

        logger.info("=" * 60)
        logger.info("Polymarket Trading Bot started")
        if self.settings.bot.dry_run:
            logger.info("*** DRY RUN MODE — no real orders will be placed ***")
        logger.info("=" * 60)

        last_heartbeat = 0.0

        try:
            while self._running:
                cycle_start = time.time()
                self._cycle_count += 1

                try:
                    # Refresh markets periodically
                    if time.time() - self._last_market_refresh > self.settings.bot.market_refresh_interval_sec:
                        self._refresh_markets()

                    # Run each strategy
                    for strategy in self.strategies:
                        if not self._running:
                            break
                        if self.risk_manager.is_paused:
                            logger.debug("Skipping %s — risk paused", strategy.name)
                            continue

                        try:
                            signals = strategy.scan(self._markets)
                            if signals:
                                logger.info(
                                    "[%s] generated %d signals",
                                    strategy.name,
                                    len(signals),
                                )
                                strategy.execute_signals(signals)
                        except Exception as e:
                            logger.error(
                                "Strategy %s error: %s", strategy.name, e, exc_info=True
                            )

                    # Update position prices and check stop losses
                    self._update_positions()

                    # Handle stop losses
                    self._handle_stop_losses()

                    # Heartbeat logging
                    if time.time() - last_heartbeat > self.settings.bot.heartbeat_interval_sec:
                        self._log_heartbeat()
                        last_heartbeat = time.time()

                except Exception as e:
                    logger.error("Main loop error: %s", e, exc_info=True)

                # Sleep between cycles (use the fastest strategy interval)
                min_interval = min(
                    self.settings.arbitrage.scan_interval_sec if self.settings.arbitrage.enabled else 999,
                    self.settings.market_making.refresh_interval_sec if self.settings.market_making.enabled else 999,
                    self.settings.value.scan_interval_sec if self.settings.value.enabled else 999,
                )
                elapsed = time.time() - cycle_start
                sleep_time = max(0, min_interval - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Graceful shutdown: cancel orders, log final stats."""
        self._running = False
        logger.info("Shutting down...")

        # Stop all strategies
        for strategy in self.strategies:
            strategy.stop()

        # Cancel all open orders (if not dry run)
        if not self.settings.bot.dry_run:
            try:
                self.clob.cancel_all()
                logger.info("All open orders cancelled")
            except Exception as e:
                logger.error("Failed to cancel orders on shutdown: %s", e)

        # Close gamma client
        try:
            self.gamma.close()
        except Exception:
            pass

        # Final summary
        summary = self.risk_manager.get_summary()
        runtime = time.time() - self._start_time if self._start_time else 0

        logger.info("=" * 60)
        logger.info("SHUTDOWN SUMMARY")
        logger.info("  Runtime: %.0f seconds (%d cycles)", runtime, self._cycle_count)
        logger.info("  Total PnL: %s", format_usd(summary["total_pnl_usd"]))
        logger.info("  Unrealized PnL: %s", format_usd(summary["unrealized_pnl_usd"]))
        logger.info("  Daily trades: %d", summary["daily_trades"])
        logger.info("  Max drawdown: %.1f%%", summary["drawdown_pct"])
        logger.info("  Open positions: %d", summary["positions"])
        logger.info("=" * 60)

    def _refresh_markets(self) -> None:
        """Refresh the market cache from Gamma API."""
        logger.info("Refreshing market list...")
        try:
            markets = self.gamma.get_all_active_markets(
                min_liquidity=self.settings.bot.min_market_liquidity_usd,
            )

            # Filter out excluded tags
            excluded = set(self.settings.bot.excluded_tags)
            if excluded:
                markets = [
                    m for m in markets
                    if not any(t in excluded for t in m.tags)
                ]

            # Only keep markets with order books
            markets = [m for m in markets if m.enable_order_book and m.clob_token_ids]

            self._markets = markets
            self._last_market_refresh = time.time()
            logger.info("Loaded %d active markets with order books", len(markets))
        except Exception as e:
            logger.error("Failed to refresh markets: %s", e, exc_info=True)

    def _update_positions(self) -> None:
        """Fetch current prices for all positions and update PnL."""
        positions = self.risk_manager._positions
        if not positions:
            return

        price_updates: dict[str, float] = {}
        for token_id in positions:
            try:
                mid = self.clob.get_midpoint(token_id)
                price_updates[token_id] = mid
            except Exception as e:
                logger.debug("Failed to get price for %s: %s", token_id[:12], e)

        if price_updates:
            self.risk_manager.update_positions(price_updates)

    def _handle_stop_losses(self) -> None:
        """Close positions that hit stop losses."""
        stops = self.risk_manager.get_positions_to_stop()
        for token_id in stops:
            position = self.risk_manager._positions.get(token_id)
            if not position:
                continue

            logger.warning(
                "Executing stop loss for %s (entry=%.4f, current=%.4f)",
                token_id[:12],
                position.entry_price,
                position.current_price,
            )

            # Place market order to close
            close_side = "SELL" if position.side == "BUY" else "BUY"
            try:
                self.clob.place_market_order(
                    token_id=token_id,
                    side=close_side,
                    amount_usd=position.size_usd,
                )
                self.risk_manager.close_position(token_id, position.current_price)
            except Exception as e:
                logger.error("Failed to execute stop loss for %s: %s", token_id[:12], e)

    def _log_heartbeat(self) -> None:
        """Log periodic status update."""
        summary = self.risk_manager.get_summary()
        runtime = time.time() - self._start_time if self._start_time else 0

        logger.info(
            "HEARTBEAT [cycle=%d, runtime=%.0fs]: "
            "positions=%d, exposure=%s, pnl=%s, daily_pnl=%s, drawdown=%.1f%%",
            self._cycle_count,
            runtime,
            summary["positions"],
            format_usd(summary["total_exposure_usd"]),
            format_usd(summary["total_pnl_usd"]),
            format_usd(summary["daily_pnl_usd"]),
            summary["drawdown_pct"],
        )

    def _signal_handler(self, signum: int, frame: Any) -> None:
        logger.info("Signal %d received, initiating shutdown...", signum)
        self._running = False


def setup_logging(log_level: str, log_file: str) -> None:
    """Configure logging to both console and file."""
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG)
    console.setFormatter(formatter)
    root_logger.addHandler(console)

    # File handler
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Trading Bot")
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Force dry run mode (paper trading)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="Override log level",
    )
    args = parser.parse_args()

    # Load settings
    config_path = Path(args.config)
    settings = Settings.load(config_path if config_path.exists() else None)

    # CLI overrides
    if args.dry_run is not None:
        settings.bot.dry_run = True
    if args.log_level:
        settings.bot.log_level = args.log_level

    # Setup logging
    setup_logging(settings.bot.log_level, settings.bot.log_file)

    logger.info("Polymarket Trading Bot v1.0")
    logger.info("Config: %s", config_path)
    logger.info("Dry run: %s", settings.bot.dry_run)

    # Create and run bot
    bot = TradingBot(settings)

    try:
        bot.initialize()
        bot.run()
    except Exception as e:
        logger.critical("Fatal error: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
