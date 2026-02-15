"""Configuration management for the Polymarket trading bot.

Loads settings from a YAML config file and environment variables.
Environment variables take precedence over config file values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass
class APIConfig:
    """Polymarket API connection settings."""

    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    ws_host: str = "wss://ws-subscriptions-clob.polymarket.com"
    chain_id: int = 137  # Polygon mainnet
    private_key: str = ""
    signature_type: int = 0  # 0=EOA, 1=Magic/Email, 2=Browser
    funder_address: str = ""
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""


@dataclass
class RiskConfig:
    """Risk management parameters."""

    max_position_size_usd: float = 100.0
    max_total_exposure_usd: float = 500.0
    max_positions: int = 10
    max_loss_per_trade_usd: float = 20.0
    max_daily_loss_usd: float = 50.0
    max_drawdown_pct: float = 10.0
    kelly_fraction: float = 0.25  # fraction of full Kelly to use
    min_edge_pct: float = 2.0  # minimum edge in % to take a trade
    stop_loss_pct: float = 15.0  # stop loss as % of position


@dataclass
class ArbitrageConfig:
    """Arbitrage strategy parameters."""

    enabled: bool = True
    min_profit_pct: float = 2.5  # minimum arbitrage spread to capture
    scan_interval_sec: float = 10.0
    max_position_per_arb_usd: float = 50.0


@dataclass
class MarketMakingConfig:
    """Market making strategy parameters."""

    enabled: bool = True
    spread_bps: int = 300  # spread in basis points (3%)
    order_size_usd: float = 10.0
    max_inventory_usd: float = 100.0
    refresh_interval_sec: float = 30.0
    min_liquidity_usd: float = 1000.0  # only make in markets with enough liquidity
    max_price_drift_pct: float = 5.0  # cancel and re-quote if price drifts


@dataclass
class ValueConfig:
    """Statistical value trading strategy parameters."""

    enabled: bool = True
    scan_interval_sec: float = 60.0
    min_volume_usd: float = 5000.0
    min_liquidity_usd: float = 2000.0
    lookback_trades: int = 100
    mean_reversion_threshold_pct: float = 5.0  # price deviation from fair value
    momentum_window: int = 20
    max_position_per_trade_usd: float = 30.0


@dataclass
class BotConfig:
    """General bot configuration."""

    dry_run: bool = True  # paper trading mode
    log_level: str = "INFO"
    log_file: str = "polymarket_bot.log"
    heartbeat_interval_sec: float = 60.0
    market_refresh_interval_sec: float = 300.0
    min_market_liquidity_usd: float = 500.0
    excluded_tags: list[str] = field(default_factory=list)


@dataclass
class Settings:
    """Top-level settings container."""

    api: APIConfig = field(default_factory=APIConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    arbitrage: ArbitrageConfig = field(default_factory=ArbitrageConfig)
    market_making: MarketMakingConfig = field(default_factory=MarketMakingConfig)
    value: ValueConfig = field(default_factory=ValueConfig)
    bot: BotConfig = field(default_factory=BotConfig)

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> Settings:
        """Load settings from YAML file and environment variables.

        Environment variables override YAML values. Sensitive credentials
        should always be set via environment variables, not config files.
        """
        load_dotenv()

        settings = cls()

        if config_path and Path(config_path).exists():
            with open(config_path) as f:
                data = yaml.safe_load(f) or {}
            settings._apply_yaml(data)

        settings._apply_env()
        return settings

    def _apply_yaml(self, data: dict[str, Any]) -> None:
        """Apply YAML config values to settings."""
        section_map = {
            "api": self.api,
            "risk": self.risk,
            "arbitrage": self.arbitrage,
            "market_making": self.market_making,
            "value": self.value,
            "bot": self.bot,
        }
        for section_name, section_obj in section_map.items():
            section_data = data.get(section_name, {})
            if not isinstance(section_data, dict):
                continue
            for key, value in section_data.items():
                if hasattr(section_obj, key):
                    current = getattr(section_obj, key)
                    try:
                        converted = type(current)(value)
                        setattr(section_obj, key, converted)
                    except (TypeError, ValueError):
                        setattr(section_obj, key, value)

    def _apply_env(self) -> None:
        """Override settings with environment variables.

        Mapping:
          POLYMARKET_PRIVATE_KEY -> api.private_key
          POLYMARKET_FUNDER_ADDRESS -> api.funder_address
          POLYMARKET_SIGNATURE_TYPE -> api.signature_type
          POLYMARKET_API_KEY -> api.api_key
          POLYMARKET_API_SECRET -> api.api_secret
          POLYMARKET_API_PASSPHRASE -> api.api_passphrase
          POLYMARKET_DRY_RUN -> bot.dry_run
        """
        env_map = {
            "POLYMARKET_PRIVATE_KEY": ("api", "private_key", str),
            "POLYMARKET_FUNDER_ADDRESS": ("api", "funder_address", str),
            "POLYMARKET_SIGNATURE_TYPE": ("api", "signature_type", int),
            "POLYMARKET_API_KEY": ("api", "api_key", str),
            "POLYMARKET_API_SECRET": ("api", "api_secret", str),
            "POLYMARKET_API_PASSPHRASE": ("api", "api_passphrase", str),
            "POLYMARKET_DRY_RUN": ("bot", "dry_run", _parse_bool),
            "POLYMARKET_LOG_LEVEL": ("bot", "log_level", str),
        }
        for env_var, (section, attr, converter) in env_map.items():
            val = os.environ.get(env_var)
            if val is not None:
                section_obj = getattr(self, section)
                setattr(section_obj, attr, converter(val))

    def validate(self) -> list[str]:
        """Return a list of validation errors, empty if config is valid."""
        errors = []
        if not self.bot.dry_run:
            if not self.api.private_key:
                errors.append("api.private_key is required for live trading")
        if self.risk.max_position_size_usd <= 0:
            errors.append("risk.max_position_size_usd must be positive")
        if self.risk.max_total_exposure_usd <= 0:
            errors.append("risk.max_total_exposure_usd must be positive")
        if not (0 < self.risk.kelly_fraction <= 1.0):
            errors.append("risk.kelly_fraction must be between 0 and 1")
        return errors


def _parse_bool(value: str) -> bool:
    return value.lower() in ("true", "1", "yes")
