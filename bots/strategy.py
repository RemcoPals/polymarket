"""
Streak reversal strategy, Kelly criterion bet sizing, and dynamic exit manager.

Signal: if the last N consecutive resolved markets all went the same direction
(streak ≥ min_streak), bet on the *opposite* direction in the next market.

Backtested accuracy (ETH 15-min, streak ≥ 3):
  - Polymarket 16k dataset:  56.18%  (3,519 bets)
  - Binance 1y dataset:      56.73%  (35,042 candles)
  - Break-even with 2% fee:  50.51%

Regime detection (Strategy R -- 3-state):
  Safe      : rev_acc_30 >= 50% AND dir_bias <= 5%  ->  bet REVERSAL  (~60.5% acc)
  Pause     : rev_acc_30 45-50% OR dir_bias 5-8%    ->  SKIP          (~50% acc, noisy)
  Momentum  : rev_acc_30 < 45%                      ->  bet CONTINUATION (~57-69% acc)
"""

import json
import math
import urllib.request
from datetime import datetime, timezone


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
        estimated_win_prob: backtested win rate for our signal (e.g. 0.56)
        current_price:      price per share for the outcome we're betting (0–1 USDC)
        bankroll_usdc:      total available USDC balance
        kelly_multiplier:   fraction of Kelly to use (0.5 = half-Kelly, recommended)
        fee_rate:           effective fee rate on profits
        max_bet_pct:        hard cap as fraction of bankroll (e.g. 0.06 = 6%)

    Returns:
        Bet size in USDC, or 0.0 if Kelly says no edge.

    Example at p=0.56, price=0.50, bankroll=$1000:
        net_payout = 1.0 - 0.02 * 0.50 = 0.99
        b = (0.99 - 0.50) / 0.50 = 0.98
        kelly = (0.56 * 0.98 - 0.44) / 0.98 ≈ 0.110
        raw_bet = 0.110 * 0.5 * 1000 = $55  →  capped at $60 (6%)
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


class DynamicExitManager:
    """
    Tracks an open position and determines when to exit early.

    Monitors price during a 15-min window and signals take-profit or stop-loss
    when thresholds are hit. This reduces variance: losses are capped and gains
    can be locked in before resolution.

    All prices in Kalshi contract cents (integers 1-99).
    The exit is evaluated using the BID price (what sellers receive), not the ask.

    Args:
        entry_price_cents: price paid per contract (e.g. 50)
        side:              'yes' or 'no' — what side we hold
        count:             number of contracts held
        tp_cents:          take-profit: exit if contract gains this many cents
        sl_cents:          stop-loss: exit if contract loses this many cents
        min_hold_secs:     don't check exits before this many seconds (default 60)
        entry_time:        when the position was entered (defaults to now)
    """

    def __init__(
        self,
        entry_price_cents: int,
        side: str,
        count: int,
        tp_cents: int,
        sl_cents: int,
        min_hold_secs: int = 60,
        entry_time: datetime | None = None,
    ) -> None:
        self.entry_price_cents = entry_price_cents
        self.side              = side
        self.count             = count
        self.tp_cents          = tp_cents
        self.sl_cents          = sl_cents
        self.min_hold_secs     = min_hold_secs
        self.entry_time        = entry_time or datetime.now(timezone.utc)
        self._exited           = False

    def check_exit(self, current_bid_cents: int) -> str | None:
        """
        Check whether to exit the position based on current bid price.

        Returns:
            'TAKE_PROFIT' — price moved tp_cents in our favour; sell to lock in gain
            'STOP_LOSS'   — price moved sl_cents against us; sell to cap loss
            None          — hold (min hold time not elapsed, or thresholds not hit)

        For 'yes' holdings: gain = current_bid - entry_price  (up is good)
        For 'no' holdings:  gain = entry_price - current_bid  (down is good,
                            i.e. 'no' contracts are worth more when yes_bid drops)
        """
        if self._exited:
            return None

        elapsed = (datetime.now(timezone.utc) - self.entry_time).total_seconds()
        if elapsed < self.min_hold_secs:
            return None

        if current_bid_cents <= 0:
            return None

        if self.side == "yes":
            gain = current_bid_cents - self.entry_price_cents
        else:
            # For 'no' contracts: we profit when yes price falls
            # Kalshi no_bid ≈ 100 - yes_ask, but the caller passes no_bid directly
            gain = current_bid_cents - self.entry_price_cents

        if gain >= self.tp_cents:
            self._exited = True
            return "TAKE_PROFIT"
        if gain <= -self.sl_cents:
            self._exited = True
            return "STOP_LOSS"
        return None

    def unrealized_pnl_usdc(self, current_bid_cents: int) -> float:
        """Estimated unrealized P&L in USDC at current bid price (no fees)."""
        delta_cents = current_bid_cents - self.entry_price_cents
        return (delta_cents / 100) * self.count

    def realized_pnl_usdc(self, exit_price_cents: int) -> float:
        """
        Estimated net P&L in USDC at a given exit price.
        Does NOT include Kalshi fees (caller should subtract those separately).
        """
        if self.side == "yes":
            delta_cents = exit_price_cents - self.entry_price_cents
        else:
            delta_cents = exit_price_cents - self.entry_price_cents
        return (delta_cents / 100) * self.count


# ---------------------------------------------------------------------------
# Strategy F: Tiered Kelly — signal edge scales with streak length
# ---------------------------------------------------------------------------

def get_streak_signal_edge(streak_len: int, cfg) -> float:
    """
    Returns the signal edge for a given streak length using the tiered table.

    Longer streaks have higher backtested accuracy, so they warrant a larger
    edge estimate and therefore a larger Kelly bet.

    Streaks >= max key are capped at the highest tier. Falls back to
    cfg.signal_edge if the table is empty or streak is below all keys.

    Backtested (ETH Binance 1y):
        Streak 3-4 → ~56%  → edge 0.056
        Streak 5-6 → ~59%  → edge 0.080
        Streak 7+  → capped at streak-6 level (too few obs for reliable calib)
    """
    table = getattr(cfg, "streak_edge_table", None) or {}
    if not table:
        return cfg.signal_edge

    # Cap at the highest defined tier
    max_key = max(table.keys())
    effective = min(streak_len, max_key)

    # Find the highest key that is <= effective streak
    applicable = [k for k in table if k <= effective]
    if not applicable:
        return cfg.signal_edge  # streak below all table keys

    return table[max(applicable)]


# ---------------------------------------------------------------------------
# Strategy C: Magnitude filter — skip low-volatility candles
# ---------------------------------------------------------------------------

_BINANCE_SYMBOL = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
}


def get_binance_magnitude(asset: str, timeout: int = 5) -> float | None:
    """
    Fetch the previous completed 15-min Binance candle and return
    |log(close/open)| — a measure of how much price moved last period.

    Returns None on network failure or invalid data.

    Strategy C: if the returned value exceeds cfg.magnitude_threshold_sigma × sigma,
    add cfg.magnitude_bonus_edge to the signal edge before Kelly sizing.

    Backtested on ETH Binance 1y: threshold=1σ → OOS 58.8% (vs 55% baseline).
    """
    symbol = _BINANCE_SYMBOL.get(asset.lower(), "ETHUSDT")
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={symbol}&interval=15m&limit=2"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        # data[0] = previous completed candle, data[1] = current (incomplete)
        if len(data) < 2:
            return None
        prev = data[0]
        open_p  = float(prev[1])
        close_p = float(prev[4])
        if open_p <= 0:
            return None
        return abs(math.log(close_p / open_p))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Strategy R: Regime detection -- 3-state (reversal / pause / continuation)
# ---------------------------------------------------------------------------

# Regime state constants
REGIME_SAFE       = "safe"        # bet reversal as normal
REGIME_PAUSE      = "pause"       # skip this slot (noisy, ~50%)
REGIME_MOMENTUM   = "momentum"    # flip to continuation bet


class RegimeTracker:
    """
    Tracks two rolling indicators to classify the current market regime:

      1. Rolling reversal accuracy (last `rev_window` streak bets):
           rev_acc < pause_rev_thresh  -> pause
           rev_acc < momentum_rev_thresh -> momentum (flip to continuation)

      2. 1-day directional bias  |Up_fraction - 0.5|  over last `bias_window` outcomes:
           dir_bias > pause_bias_thresh -> pause

    Backtested thresholds (ETH Binance 1y, 35k windows):
      - pause_rev_thresh    = 0.50  (below this, reversal edge < 0.5%)
      - momentum_rev_thresh = 0.45  (below this, continuation wins 57-69%)
      - pause_bias_thresh   = 0.05  (>5% directional imbalance in 24h -> noisy)

    Usage:
        tracker = RegimeTracker()
        # after each settled bet:
        tracker.record_bet(correct=True/False)
        # after each resolved candle:
        tracker.record_outcome(winner="Up"/"Down")
        # before placing the next bet:
        regime = tracker.regime()
        if regime == REGIME_MOMENTUM:
            signal = direction  # bet continuation
        elif regime == REGIME_PAUSE:
            continue            # skip
        else:
            signal = reversal   # normal
    """

    def __init__(
        self,
        rev_window:            int   = 30,
        bias_window:           int   = 96,
        pause_rev_thresh:      float = 0.50,
        momentum_rev_thresh:   float = 0.45,
        pause_bias_thresh:     float = 0.05,
    ) -> None:
        self.rev_window          = rev_window
        self.bias_window         = bias_window
        self.pause_rev_thresh    = pause_rev_thresh
        self.momentum_rev_thresh = momentum_rev_thresh
        self.pause_bias_thresh   = pause_bias_thresh

        self._bet_outcomes: list[int] = []   # 1=correct, 0=wrong, oldest first
        self._candle_outcomes: list[str] = []  # 'Up'/'Down', oldest first

    # ---- update methods ------------------------------------------------

    def record_bet(self, correct: bool) -> None:
        """Call after each streak bet resolves (win or loss)."""
        self._bet_outcomes.append(1 if correct else 0)

    def record_outcome(self, winner: str) -> None:
        """Call after each 15-min candle resolves ('Up' or 'Down')."""
        self._candle_outcomes.append(winner)
        # Keep only what we need
        if len(self._candle_outcomes) > self.bias_window * 2:
            self._candle_outcomes = self._candle_outcomes[-self.bias_window * 2:]

    # ---- computed indicators -------------------------------------------

    def rolling_rev_acc(self) -> float | None:
        """Reversal accuracy over the last `rev_window` bets. None if insufficient data."""
        recent = self._bet_outcomes[-self.rev_window:]
        if len(recent) < max(10, self.rev_window // 3):
            return None
        return sum(recent) / len(recent)

    def directional_bias(self) -> float | None:
        """
        |Up_fraction - 0.5| over the last `bias_window` candles.
        Returns None if insufficient data.
        """
        recent = self._candle_outcomes[-self.bias_window:]
        if len(recent) < self.bias_window // 2:
            return None
        up_frac = recent.count("Up") / len(recent)
        return abs(up_frac - 0.5)

    # ---- main classification -------------------------------------------

    def regime(self) -> str:
        """
        Classify current regime as REGIME_SAFE, REGIME_PAUSE, or REGIME_MOMENTUM.

        Decision tree (in priority order):
          1. rev_acc < momentum_rev_thresh  -> MOMENTUM  (flip to continuation)
          2. rev_acc < pause_rev_thresh     -> PAUSE     (skip, noisy)
          3. dir_bias > pause_bias_thresh   -> PAUSE     (strong directional trend)
          4. otherwise                      -> SAFE      (reversal as normal)

        Falls back to SAFE when there is insufficient history.
        """
        rev_acc = self.rolling_rev_acc()
        bias    = self.directional_bias()

        if rev_acc is not None and rev_acc < self.momentum_rev_thresh:
            return REGIME_MOMENTUM

        if rev_acc is not None and rev_acc < self.pause_rev_thresh:
            return REGIME_PAUSE

        if bias is not None and bias > self.pause_bias_thresh:
            return REGIME_PAUSE

        return REGIME_SAFE

    def summary(self) -> str:
        """One-line status string for logging."""
        rev_acc = self.rolling_rev_acc()
        bias    = self.directional_bias()
        rev_s   = f"{rev_acc:.1%}" if rev_acc is not None else "n/a"
        bias_s  = f"{bias:.1%}"    if bias    is not None else "n/a"
        n_bets  = len(self._bet_outcomes)
        return (
            f"regime={self.regime().upper():<10} "
            f"rev_acc={rev_s}(n={n_bets}) "
            f"dir_bias={bias_s}"
        )
