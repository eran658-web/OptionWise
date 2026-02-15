# Polymarket Trading Bot

Automated trading bot for [Polymarket](https://polymarket.com) prediction markets. Implements arbitrage detection, market making, and statistical value trading with integrated risk management.

## Strategies

### 1. Arbitrage (lowest risk)

- **Binary arbitrage**: Detects when YES + NO prices sum to less than $1.00 and buys both sides to lock in risk-free profit
- **Multi-outcome arbitrage**: Finds events where all outcome prices sum to less than $1.00

Typical returns: 0.5–3% per opportunity. Requires minimum ~2.5% spread to be profitable after fees.

### 2. Market Making (medium risk)

- Places bid and ask limit orders around a calculated fair value
- Earns the spread between buy and sell prices
- Inventory management with quote skewing to reduce directional risk
- Automatically cancels stale quotes when price drifts

### 3. Statistical Value Trading (higher risk, higher reward)

- **Mean reversion**: Buys when price is significantly below recent average (z-score < -1.5), sells when above (z-score > 1.5)
- **Order book imbalance**: Detects large bid/ask imbalances that predict short-term price movement
- **Complement mispricing**: Finds NO tokens mispriced relative to their YES counterpart's fair value

## Risk Management

- **Position sizing**: Fractional Kelly criterion (default 25% Kelly)
- **Per-trade limits**: Maximum USD per trade and per position
- **Portfolio limits**: Maximum total exposure and concurrent positions
- **Daily loss limit**: Bot pauses trading if daily losses exceed threshold
- **Maximum drawdown**: Bot pauses if portfolio drawdown exceeds limit
- **Stop losses**: Automatic position closure at configurable loss percentage

## Project Structure

```
polymarket_bot/
├── main.py                  # Bot orchestrator and entry point
├── config/
│   ├── settings.py          # Configuration management
│   └── config.example.yaml  # Example configuration file
├── api/
│   ├── clob_client.py       # Polymarket CLOB API wrapper
│   └── gamma_client.py      # Gamma market data API client
├── strategies/
│   ├── base.py              # Base strategy interface
│   ├── arbitrage.py         # Arbitrage detection
│   ├── market_making.py     # Market making strategy
│   └── value.py             # Statistical value trading
├── risk/
│   └── manager.py           # Risk management system
├── utils/
│   └── helpers.py           # Utility functions (Kelly, rounding, etc.)
├── requirements.txt
├── .env.example
└── .gitignore
```

## Setup

### Prerequisites

- Python 3.9+
- A Polygon wallet with USDC for live trading
- Polymarket account with token allowances set

### Installation

```bash
cd polymarket_bot
pip install -r requirements.txt
```

### Configuration

1. Copy the example files:

```bash
cp .env.example .env
cp config/config.example.yaml config.yaml
```

2. Edit `.env` with your credentials:

```bash
# Your Polygon wallet private key
POLYMARKET_PRIVATE_KEY=0x...

# Optional: pre-generated API credentials
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...
```

3. Edit `config.yaml` to tune strategy parameters, risk limits, and which strategies to enable.

### Generating API Credentials

If you don't have API credentials yet, the bot will derive them on first run using your private key and print them to the console. Save these in your `.env` file for subsequent runs.

You can also get your private key from Polymarket.com: log in, click "Cash" → three dots → "Export Private Key".

### Setting Token Allowances

For EOA wallets (MetaMask, hardware wallets), you need to set allowances before trading. The Polymarket exchange contracts need permission to access your USDC and conditional tokens. See the [Polymarket docs](https://docs.polymarket.com/quickstart/overview) for details.

## Usage

### Dry Run (Paper Trading)

```bash
python -m polymarket_bot.main --dry-run
```

This fetches real market data and generates signals but does not place actual orders. Use this to validate your strategy configuration.

### Live Trading

```bash
python -m polymarket_bot.main -c config.yaml
```

Make sure `dry_run: false` is set in your config or `.env`:

```bash
POLYMARKET_DRY_RUN=false
```

### Command-Line Options

```
-c, --config PATH     Config file path (default: config.yaml)
--dry-run             Force paper trading mode
--log-level LEVEL     Override log level (DEBUG/INFO/WARNING/ERROR)
```

## Configuration Reference

See `config/config.example.yaml` for all available settings. Key parameters:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `bot.dry_run` | `true` | Paper trading mode |
| `risk.max_position_size_usd` | `100` | Max USD per position |
| `risk.max_total_exposure_usd` | `500` | Max total portfolio exposure |
| `risk.kelly_fraction` | `0.25` | Fraction of Kelly criterion |
| `risk.max_daily_loss_usd` | `50` | Daily loss limit (pauses bot) |
| `risk.stop_loss_pct` | `15` | Stop loss percentage |
| `arbitrage.min_profit_pct` | `2.5` | Min arb spread to capture |
| `market_making.spread_bps` | `300` | Quote spread in basis points |
| `value.mean_reversion_threshold_pct` | `5.0` | Min deviation for mean reversion |

## Important Notes

- **Start with dry run mode** to understand how the bot behaves before risking capital
- **Start with small position sizes** — the defaults are conservative
- Arbitrage opportunities are the safest but close quickly and require fast execution
- Market making requires monitoring — inventory can accumulate in adverse conditions
- Prediction markets have different dynamics than traditional financial markets
- Always verify your wallet has sufficient USDC on Polygon before live trading
- The bot logs all activity to both console and file for post-session analysis

## Disclaimer

This software is provided for educational and research purposes. Trading prediction markets involves financial risk. No strategy guarantees profits. Use at your own risk and never trade with funds you cannot afford to lose.
