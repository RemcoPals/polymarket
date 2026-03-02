# BTC 15-Minute Streak Reversal Bot

An automated trading bot for [Kalshi](https://kalshi.com) that bets on BTC price reversals using a streak-based mean-reversion strategy. When BTC has moved in the same direction for 3 or more consecutive 15-minute windows, the bot bets on a reversal in the next window.

---

## Strategy

### How it works

Every 15 minutes, Kalshi opens a new market asking: *"Will BTC be higher or lower than it was 15 minutes ago?"* The bot:

1. Fetches the last 10 resolved markets
2. Counts the current streak (e.g. 4 consecutive "Up" outcomes)
3. If streak ≥ 3 → bets on the **opposite** direction (mean reversion)
4. Sizes the bet using the **Kelly criterion** (half-Kelly for safety, capped at 5% of bankroll)

### Backtest results

Tested on **6,977 Kalshi markets** (Dec 10 2025 – Mar 2026) and independently validated on **105,121 Binance 5-min candles** (1 full year):

| Dataset | Win rate | Bets | Break-even needed |
|---|---|---|---|
| Kalshi 15-min (streak ≥ 3) | **56.8%** | 1,469 | 50.9% |
| Kalshi 15-min (streak ≥ 4) | **58.4%** | 635 | 50.9% |
| Binance 1-year (streak ≥ 3) | **52.4%** | 25,361 | 50.9% |

**Simulated $100 compound Kelly backtest** (Kalshi data, $20 max bet cap):

| Period | End Balance | Monthly P&L |
|---|---|---|
| Dec 2025 | $235 | +$135 |
| Jan 2026 | $1,548 | +$1,313 |
| Feb 2026 | $2,612 | +$1,064 |
| 62% of trading days profitable | | |

> **Disclaimer:** Past backtest performance does not guarantee future results. Always start with dry-run mode and only risk money you can afford to lose.

---

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- A [Kalshi](https://kalshi.com) account with an API key

---

## Setup

### 1. Clone the repo

```bash
git clone git@github.com:RemcoPals/polymarket.git
cd polymarket
```

### 2. Install dependencies

```bash
uv sync
```

### 3. Get Kalshi API credentials

1. Log in to [kalshi.com](https://kalshi.com)
2. Go to **Profile → Settings → API Keys → Create Key**
3. Copy the **Key ID** (a UUID)
4. Download the **private key** `.pem` file and store it somewhere safe

### 4. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in your credentials:

```env
KALSHI_API_KEY=your-uuid-key-id-here
KALSHI_PRIVATE_KEY_PATH=/path/to/your/private-key.pem
```

### 5. Run in dry-run mode first

```bash
python main.py
```

The bot will print signals and simulated bets every 15 minutes without placing real orders. Verify it looks correct before going live.

### 6. Go live

Once satisfied, set `DRY_RUN=false` in your `.env`:

```bash
DRY_RUN=false python main.py
```

---

## Configuration

All settings are controlled via `.env` (see `.env.example` for full documentation):

| Variable | Default | Description |
|---|---|---|
| `KALSHI_API_KEY` | — | Your Kalshi Key ID (UUID) |
| `KALSHI_PRIVATE_KEY_PATH` | — | Path to your RSA private key `.pem` file |
| `KALSHI_PRIVATE_KEY` | — | Inline PEM string (alternative to path, useful for cloud) |
| `MIN_STREAK` | `3` | Minimum streak length before placing a bet |
| `LOOKBACK` | `10` | Number of past markets to consider for streak |
| `ESTIMATED_WIN_PROB` | `0.53` | Backtested win probability (used in Kelly formula) |
| `KELLY_MULTIPLIER` | `0.5` | Fraction of full Kelly (0.5 = half-Kelly) |
| `MAX_BET_PCT` | `0.05` | Max bet as fraction of bankroll (5%) |
| `DRY_RUN` | `true` | If true, logs bets without placing real orders |
| `DRY_RUN_BANKROLL` | `100` | Simulated balance for dry-run Kelly sizing |
| `MAX_DAILY_LOSS_USDC` | `50` | Bot stops for the day after losing this amount |

---

## Deploying to fly.io

The bot includes a `Dockerfile` and `fly.toml` for one-command cloud deployment so it runs 24/7 without your laptop.

### 1. Install flyctl

```bash
brew install flyctl
fly auth login
```

### 2. Create the app

```bash
fly launch --no-deploy
```

### 3. Set secrets

Instead of a `.env` file, set your credentials as fly.io secrets:

```bash
# If using a key file, first convert the PEM to a single-line string:
awk 'NF {printf "%s\\n", $0}' private-key.pem

# Then set all secrets:
fly secrets set \
  KALSHI_API_KEY=your-uuid-here \
  KALSHI_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----" \
  DRY_RUN=false \
  DRY_RUN_BANKROLL=100 \
  MAX_DAILY_LOSS_USDC=50
```

### 4. Deploy

```bash
fly deploy
```

### 5. Monitor logs

```bash
fly logs
```

---

## Project structure

```
polymarket/
├── main.py          # Bot loop: wakes every 15 min, bets, tracks results
├── strategy.py      # Streak detection + Kelly criterion bet sizing
├── client.py        # Kalshi API: read markets (no auth) + place orders (RSA auth)
├── config.py        # Settings loaded from .env
├── .env.example     # Credential + config template
├── Dockerfile       # Container definition for cloud deployment
└── fly.toml         # fly.io app configuration
```

---

## How the Kelly formula works

The bet size is calculated as:

```
kelly_fraction = (p × b - q) / b
bet = kelly_fraction × kelly_multiplier × bankroll
bet = min(bet, max_bet_pct × bankroll)
```

Where:
- `p` = estimated win probability (e.g. 0.53)
- `q` = 1 - p (loss probability)
- `b` = net profit per dollar risked after Kalshi's fee
- `kelly_multiplier` = 0.5 (half-Kelly, halves the bet for safety)

**Kalshi's fee** is quadratic: `0.07 × price × (1 - price)` per contract. At a 50-cent market this is ~1.75 cents per contract (3.5% of profit) — the break-even win rate is ~50.9%.

---

## Risk warning

- This bot places real money bets. Always test with `DRY_RUN=true` first.
- Backtested win rates (56.8%) are not guaranteed to persist.
- The `MAX_DAILY_LOSS_USDC` setting is your last line of defence — set it to an amount you are comfortable losing in a single day.
- Never deposit more than you can afford to lose entirely.
