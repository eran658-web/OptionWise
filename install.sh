#!/bin/bash
set -e

echo "==> Installing OptionWise dependencies..."

# Step 1: Install packages with broken cross-constraints (skip resolver)
echo "==> Installing py-clob-client, web3, eth-account (no-deps)..."
pip install --no-deps py-clob-client web3 eth-account

# Step 2: Install everything else normally
echo "==> Installing remaining dependencies..."
pip install -r polymarket_bot/requirements.txt

echo "==> Done! Run the dashboard with:"
echo "    python -m polymarket_bot.run_dashboard --dry-run"
