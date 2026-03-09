"""
Entry point for all Kalshi 15-min streak reversal bots.

Usage:
    python run_bot.py --asset eth          # ETH bot (dry run by default)
    DRY_RUN=false python run_bot.py --asset eth
    python run_bot.py --asset btc
    python run_bot.py --asset sol
    python run_bot.py --asset xrp

Configuration via environment variables (see .env.example):
    DRY_RUN, ETH_MIN_STREAK, ENABLE_DYNAMIC_EXIT, TP_CENTS, SL_CENTS, ...
"""

import argparse
from bots.bot import run_bot

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Kalshi 15-min streak reversal bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_bot.py --asset eth
  DRY_RUN=false python run_bot.py --asset eth
  ENABLE_DYNAMIC_EXIT=true TP_CENTS=10 SL_CENTS=10 python run_bot.py --asset eth
        """,
    )
    parser.add_argument(
        "--asset",
        required=True,
        choices=["btc", "eth", "sol", "xrp"],
        help="Asset to trade (btc, eth, sol, xrp)",
    )
    args = parser.parse_args()
    run_bot(args.asset)
