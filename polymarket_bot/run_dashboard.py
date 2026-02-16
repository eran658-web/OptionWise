"""Entry point for the OptionWise web dashboard.

Launches the web dashboard and optionally auto-starts the trading bot.

Usage:
    python -m polymarket_bot.run_dashboard [OPTIONS]
    python -m polymarket_bot.run_dashboard --dry-run --port 8080
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import uvicorn

from polymarket_bot.config.settings import Settings
from polymarket_bot.main import TradingBot, setup_logging
from polymarket_bot.web.app import app, set_bot, setup_web_logging, start_bot_thread


def main():
    parser = argparse.ArgumentParser(description="OptionWise Trading Dashboard")
    parser.add_argument(
        "-c", "--config", default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=None,
        help="Force dry run mode (paper trading)",
    )
    parser.add_argument(
        "--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None, help="Override log level",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Dashboard host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=8000,
        help="Dashboard port (default: 8000)",
    )
    parser.add_argument(
        "--no-auto-start", action="store_true",
        help="Don't auto-start the trading bot",
    )
    args = parser.parse_args()

    # Load settings
    config_path = Path(args.config)
    settings = Settings.load(config_path if config_path.exists() else None)

    if args.dry_run is not None:
        settings.bot.dry_run = True
    if args.log_level:
        settings.bot.log_level = args.log_level

    setup_logging(settings.bot.log_level, settings.bot.log_file)
    setup_web_logging()

    logger = logging.getLogger("dashboard")
    logger.info("OptionWise Trading Dashboard")
    logger.info("Config: %s", config_path)
    logger.info("Dry run: %s", settings.bot.dry_run)

    # Create bot and hand it to the web app
    bot = TradingBot(settings)
    set_bot(bot, settings)

    if not args.no_auto_start:
        logger.info("Auto-starting bot in background...")
        start_bot_thread()

    logger.info("Dashboard: http://%s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
