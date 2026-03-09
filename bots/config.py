import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Asset identifier — set by run_bot.py; not read from env
    asset: str = "eth"

    # Kalshi API credentials (from .env)
    # How to get: kalshi.com -> Profile -> Settings -> API Keys -> Create Key
    kalshi_api_key:     str = ""   # UUID key ID shown in dashboard
    kalshi_private_key: str = ""   # full PEM string of your RSA private key

    # Strategy parameters
    min_streak: int = 3
    lookback:   int = 10   # number of recent resolved markets to fetch

    # Kelly criterion parameters
    signal_edge:      float = 0.06   # our edge ABOVE the market price (price + signal_edge = win_prob)
    kelly_multiplier: float = 0.5    # half-Kelly for safety
    max_bet_pct:      float = 0.06   # never bet more than 6% of bankroll
    kalshi_fee_mult:  float = 0.07   # Kalshi taker fee: 0.07 × contracts × price × (1-price)

    # Safety
    dry_run:            bool  = True
    dry_run_bankroll:   float = 100.0   # simulated bankroll for dry-run Kelly sizing
    max_daily_loss_pct: float = 0.33    # stop for the day if balance drops 33% from day-open

    # Dynamic exit — mid-market TP/SL within the 15-min window
    # Keep disabled until notebooks/dynamic_exit_analysis.ipynb validates the strategy
    enable_dynamic_exit: bool = False  # env: ENABLE_DYNAMIC_EXIT
    tp_cents:            int  = 10     # env: TP_CENTS  — exit if contract gains N cents
    sl_cents:            int  = 10     # env: SL_CENTS  — exit if contract loses N cents
    min_hold_secs:       int  = 60     # env: MIN_HOLD_SECS  — don't exit in first N seconds
    poll_interval_secs:  int  = 30     # env: POLL_INTERVAL_SECS — seconds between price checks

    # Strategy F: Tiered Kelly by streak length
    # Longer streaks have higher backtested accuracy → higher signal edge
    # streak 3-4: 56% accuracy → edge 0.056; streak 5-6+: ~59% → edge 0.080
    # Set to None here; populated in __post_init__ to avoid mutable default
    streak_edge_table: dict = None  # {streak_len: edge}

    # Strategy C: Previous-period magnitude filter (Binance candle)
    # Only bet when |log(close/open)| of previous 15m candle exceeds threshold.
    # Backtested: >1σ → 58.8% OOS accuracy (vs 55% baseline). Disabled by default.
    use_magnitude_filter:       bool  = False  # env: USE_MAGNITUDE_FILTER
    magnitude_threshold_sigma:  float = 1.0    # env: MAGNITUDE_THRESHOLD_SIGMA
    magnitude_bonus_edge:       float = 0.023  # env: MAGNITUDE_BONUS_EDGE
    # Per-asset 15-min |log return| standard deviation (calibrated from Binance 1y)
    eth_sigma_15min: float = 0.0037  # env: ETH_SIGMA_15MIN — recalibrate quarterly
    btc_sigma_15min: float = 0.0040  # env: BTC_SIGMA_15MIN
    sol_sigma_15min: float = 0.0060  # env: SOL_SIGMA_15MIN
    xrp_sigma_15min: float = 0.0055  # env: XRP_SIGMA_15MIN

    def __post_init__(self):
        self.kalshi_api_key = os.getenv("KALSHI_API_KEY", self.kalshi_api_key)

        # Private key: can be a file path or inline PEM string
        key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
        if key_path and os.path.exists(key_path):
            with open(key_path) as f:
                self.kalshi_private_key = f.read()
        else:
            # Inline PEM stored in env (fly.io / Railway may single- or double-escape \n)
            raw = os.getenv("KALSHI_PRIVATE_KEY", self.kalshi_private_key)
            # Handle both literal \n sequences and actual newlines
            self.kalshi_private_key = raw.replace("\\n", "\n").replace("\\\\n", "\n")

        # Asset-specific streak override: e.g. ETH_MIN_STREAK > MIN_STREAK > default
        asset_streak_key = f"{self.asset.upper()}_MIN_STREAK"
        self.min_streak = int(os.getenv(asset_streak_key, os.getenv("MIN_STREAK", str(self.min_streak))))
        self.lookback   = int(os.getenv("LOOKBACK", str(self.lookback)))

        self.signal_edge      = float(os.getenv("SIGNAL_EDGE",      str(self.signal_edge)))
        self.kelly_multiplier = float(os.getenv("KELLY_MULTIPLIER", str(self.kelly_multiplier)))
        self.max_bet_pct      = float(os.getenv("MAX_BET_PCT",      str(self.max_bet_pct)))

        dry_run_env = os.getenv("DRY_RUN", "true")
        self.dry_run = dry_run_env.lower() in ("true", "1", "yes")
        self.dry_run_bankroll = float(os.getenv("DRY_RUN_BANKROLL", str(self.dry_run_bankroll)))

        self.max_daily_loss_pct = float(os.getenv("MAX_DAILY_LOSS_PCT", str(self.max_daily_loss_pct)))

        self.enable_dynamic_exit = os.getenv("ENABLE_DYNAMIC_EXIT", "false").lower() in ("true", "1")
        self.tp_cents            = int(os.getenv("TP_CENTS",            str(self.tp_cents)))
        self.sl_cents            = int(os.getenv("SL_CENTS",            str(self.sl_cents)))
        self.min_hold_secs       = int(os.getenv("MIN_HOLD_SECS",       str(self.min_hold_secs)))
        self.poll_interval_secs  = int(os.getenv("POLL_INTERVAL_SECS",  str(self.poll_interval_secs)))

        # Strategy F defaults (no env overrides — recalibrate via notebook then update here)
        if self.streak_edge_table is None:
            self.streak_edge_table = {3: 0.056, 4: 0.056, 5: 0.080, 6: 0.080}

        # Strategy C
        self.use_magnitude_filter      = os.getenv("USE_MAGNITUDE_FILTER", "false").lower() in ("true", "1")
        self.magnitude_threshold_sigma = float(os.getenv("MAGNITUDE_THRESHOLD_SIGMA", str(self.magnitude_threshold_sigma)))
        self.magnitude_bonus_edge      = float(os.getenv("MAGNITUDE_BONUS_EDGE",      str(self.magnitude_bonus_edge)))
        self.eth_sigma_15min = float(os.getenv("ETH_SIGMA_15MIN", str(self.eth_sigma_15min)))
        self.btc_sigma_15min = float(os.getenv("BTC_SIGMA_15MIN", str(self.btc_sigma_15min)))
        self.sol_sigma_15min = float(os.getenv("SOL_SIGMA_15MIN", str(self.sol_sigma_15min)))
        self.xrp_sigma_15min = float(os.getenv("XRP_SIGMA_15MIN", str(self.xrp_sigma_15min)))

    def get_asset_sigma(self) -> float:
        """Returns the calibrated 15-min |log return| std dev for the current asset."""
        return getattr(self, f"{self.asset}_sigma_15min", self.eth_sigma_15min)

    def is_ready_to_trade(self) -> bool:
        """Returns True if credentials are present (for live trading)."""
        return bool(self.kalshi_api_key and self.kalshi_private_key)
