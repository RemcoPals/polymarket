import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Kalshi API credentials (from .env)
    # How to get: kalshi.com -> Profile -> Settings -> API Keys -> Create Key
    kalshi_api_key:     str = ""   # UUID key ID shown in dashboard
    kalshi_private_key: str = ""   # full PEM string of your RSA private key

    # Strategy parameters
    min_streak: int = 2
    lookback:   int = 10   # number of recent resolved markets to fetch

    # Kelly criterion parameters
    estimated_win_prob: float = 0.53   # from backtests; tune conservatively
    kelly_multiplier:   float = 0.5    # half-Kelly for safety
    max_bet_pct:        float = 0.05   # never bet more than 5% of bankroll
    kalshi_fee_mult:    float = 0.07   # Kalshi taker fee: 0.07 × contracts × price × (1-price)

    # Safety
    dry_run:             bool  = True
    dry_run_bankroll:    float = 100.0   # simulated bankroll for dry-run Kelly sizing
    max_daily_loss_usdc: float = 50.0

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

        self.min_streak = int(os.getenv("MIN_STREAK", str(self.min_streak)))
        self.lookback   = int(os.getenv("LOOKBACK",   str(self.lookback)))

        self.estimated_win_prob = float(os.getenv("ESTIMATED_WIN_PROB", str(self.estimated_win_prob)))
        self.kelly_multiplier   = float(os.getenv("KELLY_MULTIPLIER",   str(self.kelly_multiplier)))
        self.max_bet_pct        = float(os.getenv("MAX_BET_PCT",        str(self.max_bet_pct)))

        dry_run_env = os.getenv("DRY_RUN", "true")
        self.dry_run = dry_run_env.lower() in ("true", "1", "yes")
        self.dry_run_bankroll = float(os.getenv("DRY_RUN_BANKROLL", str(self.dry_run_bankroll)))

        self.max_daily_loss_usdc = float(os.getenv("MAX_DAILY_LOSS_USDC", str(self.max_daily_loss_usdc)))

    def is_ready_to_trade(self) -> bool:
        """Returns True if credentials are present (for live trading)."""
        return bool(self.kalshi_api_key and self.kalshi_private_key)
