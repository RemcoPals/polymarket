"""
Streak reversal strategy and Kelly criterion bet sizing.

Signal: if the last N consecutive resolved markets all went the same direction
(streak ≥ min_streak), bet on the *opposite* direction in the next market.

Backtested accuracy:
  - Polymarket 15k dataset:  53.4%  (3,522 bets)
  - Binance 1y dataset:      52.4%  (25,364 bets)
  - Break-even with 2% fee:  50.51%
"""


def compute_streak(outcomes: list[str]) -> tuple[str | None, int]:
    """
    Returns (direction, streak_length) for the most recent unbroken run.
    outcomes: list of 'Up'/'Down' strings, oldest first.
    """
    if not outcomes:
        return None, 0
    direction = outcomes[-1]
    streak = 1
    for o in reversed(outcomes[:-1]):
        if o == direction:
            streak += 1
        else:
            break
    return direction, streak


def get_signal(outcomes: list[str], min_streak: int = 3) -> str | None:
    """
    Returns 'Up', 'Down', or None (no bet).
    Bets the reversal when current streak >= min_streak.
    """
    direction, streak = compute_streak(outcomes)
    if direction is None or streak < min_streak:
        return None
    return "Down" if direction == "Up" else "Up"


def kelly_bet_size(
    estimated_win_prob: float,
    current_price: float,
    bankroll_usdc: float,
    kelly_multiplier: float = 0.5,
    fee_rate: float = 0.02,
    max_bet_pct: float = 0.05,
) -> float:
    """
    Kelly criterion bet size in USDC.

    Args:
        estimated_win_prob: backtested win rate for our signal (e.g. 0.53)
        current_price:      price per share for the outcome we're betting (0–1 USDC)
        bankroll_usdc:      total available USDC balance
        kelly_multiplier:   fraction of Kelly to use (0.5 = half-Kelly, recommended)
        fee_rate:           Polymarket's fee on profits (0.02 = 2%)
        max_bet_pct:        hard cap as fraction of bankroll (e.g. 0.05 = 5%)

    Returns:
        Bet size in USDC, or 0.0 if Kelly says no edge.

    Example at p=0.53, price=0.50, bankroll=$1000:
        net_payout = 1.0 - 0.02 * 0.50 = 0.99
        b = (0.99 - 0.50) / 0.50 = 0.98
        kelly = (0.53 * 0.98 - 0.47) / 0.98 = 0.0194 / 0.98 ≈ 0.0198
        raw_bet = 0.0198 * 0.5 * 1000 = $9.90  →  capped at $50 (5%)
    """
    if current_price <= 0 or current_price >= 1:
        return 0.0

    # Net payout per share after fee (fee applies to profit = 1 - price)
    net_payout = 1.0 - fee_rate * (1.0 - current_price)
    # Net profit per unit of capital risked
    b = (net_payout - current_price) / current_price

    p = estimated_win_prob
    q = 1.0 - p
    kelly = (p * b - q) / b

    if kelly <= 0:
        return 0.0  # no mathematical edge at this price

    raw_bet = kelly * kelly_multiplier * bankroll_usdc
    return min(raw_bet, max_bet_pct * bankroll_usdc)
