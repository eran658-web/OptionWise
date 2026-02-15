"""Utility functions for the Polymarket trading bot.

Includes pricing math, Kelly criterion, position sizing, and formatting.
"""

from __future__ import annotations

import math


def round_price(price: float, tick_size: float = 0.01) -> float:
    """Round a price to the nearest valid tick size."""
    if tick_size <= 0:
        return price
    decimals = max(0, -int(math.floor(math.log10(tick_size))))
    return round(round(price / tick_size) * tick_size, decimals)


def round_size(size: float, decimals: int = 2) -> float:
    """Round an order size to the given number of decimals."""
    return round(size, decimals)


def calculate_kelly_fraction(
    win_prob: float,
    win_payout: float,
    loss_amount: float = 1.0,
    fractional: float = 0.25,
) -> float:
    """Calculate the (fractional) Kelly criterion bet size.

    Args:
        win_prob: Estimated probability of winning (0-1).
        win_payout: Net payout on a win per dollar risked.
        loss_amount: Amount lost per dollar risked (default 1.0 = lose entire stake).
        fractional: Fraction of full Kelly to use (0.25 = quarter Kelly, safer).

    Returns:
        Fraction of bankroll to bet (0 to 1). Returns 0 if edge is negative.
    """
    if win_prob <= 0 or win_prob >= 1 or win_payout <= 0:
        return 0.0

    lose_prob = 1.0 - win_prob
    # Kelly: f* = (bp - q) / b
    # where b = win_payout / loss_amount, p = win_prob, q = lose_prob
    b = win_payout / loss_amount
    kelly = (b * win_prob - lose_prob) / b

    if kelly <= 0:
        return 0.0

    return min(kelly * fractional, 1.0)


def calculate_expected_value(
    price: float,
    estimated_prob: float,
) -> float:
    """Calculate expected value of buying a contract at the given price.

    For a binary market, buying YES at price p with true probability q:
      EV = q * (1 - p) - (1 - q) * p = q - p

    Returns:
        Expected value per dollar risked. Positive = favorable.
    """
    return estimated_prob - price


def calculate_edge_pct(price: float, estimated_prob: float) -> float:
    """Calculate the edge as a percentage of the price.

    Edge = (estimated_prob - price) / price * 100
    """
    if price <= 0:
        return 0.0
    return (estimated_prob - price) / price * 100


def implied_probability(price: float) -> float:
    """Convert a market price to implied probability (0-1)."""
    return max(0.0, min(1.0, price))


def check_binary_arb(yes_price: float, no_price: float) -> float:
    """Check if a binary market has an arbitrage opportunity.

    In a correct binary market, YES + NO = $1.00.
    If YES + NO < 1.00, buying both locks in a profit.

    Returns:
        Profit per dollar if arb exists, 0.0 otherwise.
    """
    total_cost = yes_price + no_price
    if total_cost < 1.0:
        return 1.0 - total_cost
    return 0.0


def check_multi_outcome_arb(prices: list[float]) -> float:
    """Check if a multi-outcome market has an arbitrage opportunity.

    For a set of mutually exclusive outcomes, the sum of all prices
    should equal $1.00. If the sum < 1.00, buying all outcomes
    locks in a risk-free profit.

    Returns:
        Profit per dollar if arb exists, 0.0 otherwise.
    """
    if not prices or any(p <= 0 for p in prices):
        return 0.0
    total_cost = sum(prices)
    if total_cost < 1.0:
        return 1.0 - total_cost
    return 0.0


def calculate_position_size(
    bankroll: float,
    kelly_fraction: float,
    max_position_usd: float,
    min_order_size: float = 5.0,
) -> float:
    """Determine position size in USD, respecting limits.

    Args:
        bankroll: Total available capital in USD.
        kelly_fraction: Kelly fraction for this bet (0-1).
        max_position_usd: Hard cap on position size.
        min_order_size: Minimum order size for the market.

    Returns:
        Position size in USD, or 0 if below minimum.
    """
    size = bankroll * kelly_fraction
    size = min(size, max_position_usd)
    if size < min_order_size:
        return 0.0
    return round_size(size)


def format_usd(amount: float) -> str:
    """Format a dollar amount for display."""
    if abs(amount) >= 1000:
        return f"${amount:,.2f}"
    return f"${amount:.2f}"


def weighted_midpoint(bids: list[dict], asks: list[dict], depth: int = 3) -> float:
    """Calculate a volume-weighted midpoint using top N levels.

    More accurate than simple (best_bid + best_ask) / 2 for thin books.
    """
    bid_sum = 0.0
    bid_vol = 0.0
    for b in bids[:depth]:
        bid_sum += b["price"] * b["size"]
        bid_vol += b["size"]

    ask_sum = 0.0
    ask_vol = 0.0
    for a in asks[:depth]:
        ask_sum += a["price"] * a["size"]
        ask_vol += a["size"]

    if bid_vol == 0 and ask_vol == 0:
        return 0.5
    if bid_vol == 0:
        return ask_sum / ask_vol
    if ask_vol == 0:
        return bid_sum / bid_vol

    wbid = bid_sum / bid_vol
    wask = ask_sum / ask_vol
    # Weight by inverse volume (more weight to the thinner side)
    total = bid_vol + ask_vol
    return (wbid * ask_vol + wask * bid_vol) / total
