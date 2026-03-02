"""
BTC 15-minute streak reversal bot — Kalshi edition.

Strategy: if the last N consecutive resolved markets all went the same direction
(streak >= min_streak), bet on the *opposite* direction in the next market.

Run: python main.py
     DRY_RUN=false python main.py   (live trading — requires credentials in .env)

See .env.example for all configuration options.
"""

import time
from datetime import datetime, timezone

from config import Config
from strategy import compute_streak, get_signal, kelly_bet_size
from client import fetch_recent_outcomes, get_active_market, get_bankroll, place_order, check_bet_result


def _sleep_until_next_slot(buffer_seconds: int = 20) -> None:
    """Sleep until the next 15-minute boundary + buffer_seconds."""
    now = datetime.now(timezone.utc)
    seconds_into_slot = now.timestamp() % 900   # 900s = 15 minutes
    sleep_secs = (900 - seconds_into_slot) + buffer_seconds
    wake = datetime.fromtimestamp(now.timestamp() + sleep_secs, tz=timezone.utc)
    print(f"[{now:%H:%M:%S} UTC] Sleeping {sleep_secs:.0f}s -> next check at {wake:%H:%M:%S} UTC")
    time.sleep(sleep_secs)


def _net_profit(bet_usdc: float, price: float, fee_mult: float) -> float:
    """
    Net profit in USDC if a bet wins (after Kalshi quadratic fee).
    count  = bet_usdc / price
    gross  = count * (1 - price)
    fee    = count * fee_mult * price * (1 - price)
    """
    count = bet_usdc / price
    gross = count * (1 - price)
    fee   = count * fee_mult * price * (1 - price)
    return gross - fee


def main() -> None:
    cfg = Config()

    print("=" * 60)
    print("  BTC 15-min Streak Reversal Bot  [Kalshi]")
    print("=" * 60)
    print(f"  dry_run        : {cfg.dry_run}")
    print(f"  min_streak     : {cfg.min_streak}")
    print(f"  lookback       : {cfg.lookback} markets")
    print(f"  win_prob (est) : {cfg.estimated_win_prob:.1%}")
    print(f"  kelly x        : {cfg.kelly_multiplier}")
    print(f"  max_bet_pct    : {cfg.max_bet_pct:.0%} of bankroll")
    print(f"  max_daily_loss : ${cfg.max_daily_loss_usdc:.0f}")
    if cfg.dry_run:
        print(f"  start balance  : ${cfg.dry_run_bankroll:.2f} (simulated)")
        print("  [DRY RUN -- no real orders will be placed]")
    elif not cfg.is_ready_to_trade():
        print("  [WARN] Credentials missing -- falling back to dry run")
        cfg.dry_run = True
    print("=" * 60)
    print()

    # Running balance (simulated in dry-run, real otherwise)
    balance     = cfg.dry_run_bankroll if cfg.dry_run else get_bankroll(cfg)
    start_bal   = balance
    wins        = 0
    losses      = 0
    pending_bet = None   # dict: {ticker, signal, bet_usdc, price}

    while True:
        _sleep_until_next_slot()
        now = datetime.now(timezone.utc)
        print(f"\n-- Slot {now:%Y-%m-%d %H:%M} UTC  |  Balance: ${balance:.2f}  |  W:{wins} L:{losses} --")

        # 1. Resolve previous bet if one is pending
        if pending_bet is not None:
            try:
                outcome = check_bet_result(pending_bet["ticker"], pending_bet["signal"])
                if outcome == "Win":
                    profit = _net_profit(pending_bet["bet_usdc"], pending_bet["price"], cfg.kalshi_fee_mult)
                    balance += profit
                    wins += 1
                    print(f"  RESULT  : WIN  +${profit:.2f}  ({pending_bet['signal']} won)")
                    print(f"  Balance : ${balance:.2f}  (started at ${start_bal:.2f})")
                    pending_bet = None
                elif outcome == "Loss":
                    balance -= pending_bet["bet_usdc"]
                    losses += 1
                    print(f"  RESULT  : LOSS -${pending_bet['bet_usdc']:.2f}  ({pending_bet['signal']} lost)")
                    print(f"  Balance : ${balance:.2f}  (started at ${start_bal:.2f})")
                    pending_bet = None
                else:
                    print(f"  RESULT  : Still pending ({pending_bet['ticker']})")
            except Exception as e:
                print(f"  [WARN] Could not check bet result: {e}")

        # 2. Fetch recent resolved outcomes
        try:
            outcomes = fetch_recent_outcomes(cfg.lookback)
        except Exception as e:
            print(f"  [ERROR] fetch_recent_outcomes: {e} -- skipping slot")
            continue

        if len(outcomes) < cfg.min_streak:
            print(f"  Not enough data ({len(outcomes)} markets) -- skipping")
            continue

        # 3. Compute streak and signal
        direction, streak = compute_streak(outcomes)
        signal = get_signal(outcomes, cfg.min_streak)
        recent_display = " ".join("U" if o == "Up" else "D" for o in outcomes[-8:])
        print(f"  Recent  : [{recent_display}]")
        print(f"  Streak  : {direction} x{streak}  ->  signal: {signal or 'SKIP'}")

        if signal is None:
            continue

        # 4. Daily loss guard
        daily_loss = start_bal - balance
        if daily_loss >= cfg.max_daily_loss_usdc:
            print(f"  Daily loss limit hit (-${daily_loss:.2f}) -- stopping for today")
            break

        # 5. Fetch current market
        try:
            market = get_active_market()
        except Exception as e:
            print(f"  [ERROR] get_active_market: {e} -- skipping slot")
            continue

        # 6. Compute Kelly bet size
        price    = market["up_price"] if signal == "Up" else market["down_price"]
        bankroll = balance if cfg.dry_run else get_bankroll(cfg)

        # Kalshi quadratic fee: effective fee rate on winnings = fee_mult * price
        effective_fee_rate = cfg.kalshi_fee_mult * price
        bet_usdc = kelly_bet_size(
            cfg.estimated_win_prob,
            price,
            bankroll,
            cfg.kelly_multiplier,
            effective_fee_rate,
            cfg.max_bet_pct,
        )

        print(f"  Market  : {market['ticker']}")
        print(f"  Price   : {signal} @ {price:.2f} ({round(price*100)}c)")
        print(f"  Balance : ${bankroll:.2f}  ->  Kelly bet: ${bet_usdc:.2f}")

        if bet_usdc == 0:
            print(f"  Kelly says no edge at price={price:.2f} -- skipping")
            continue

        # 7. Place bet
        result = place_order(signal, market, bet_usdc, cfg)

        if result.get("status") in ("PLACED", "DRY_RUN"):
            pending_bet = {
                "ticker":   market["ticker"],
                "signal":   signal,
                "bet_usdc": result["usdc_spent"],
                "price":    result["price"],
            }


if __name__ == "__main__":
    main()
