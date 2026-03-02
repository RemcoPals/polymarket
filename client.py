"""
Kalshi API client — read (no auth) + trade (RSA-signed auth).
Series: KXBTC15M — BTC Up or Down, 15-minute markets.
"""

import base64
import time
import uuid

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config import Config

BASE   = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXBTC15M"


# ── Read-only helpers (no auth) ───────────────────────────────────────────────

def fetch_recent_outcomes(n: int = 10) -> list[str]:
    """
    Fetch the last `n` resolved BTC 15-min markets, oldest first.
    Returns a list of 'Up' / 'Down' strings.
    result='yes' → Up (BTC ended higher), result='no' → Down.
    """
    resp = requests.get(
        f"{BASE}/markets",
        params={"series_ticker": SERIES, "status": "settled", "limit": n},
        timeout=10,
    )
    resp.raise_for_status()
    markets = resp.json().get("markets", [])

    outcomes = []
    for m in markets:
        result = m.get("result")
        if result == "yes":
            outcomes.append("Up")
        elif result == "no":
            outcomes.append("Down")

    outcomes.reverse()   # API returns newest-first; we need oldest-first
    return outcomes


def get_active_market() -> dict:
    """
    Fetch the current open BTC 15-min market.
    Returns dict with: ticker, up_price, down_price (fractions 0-1).
    Raises RuntimeError if no open market is found.
    """
    resp = requests.get(
        f"{BASE}/markets",
        params={"series_ticker": SERIES, "status": "open", "limit": 1},
        timeout=10,
    )
    resp.raise_for_status()
    markets = resp.json().get("markets", [])

    if not markets:
        raise RuntimeError("No open BTC 15-min market found on Kalshi")

    m = markets[0]
    yes_ask = int(m.get("yes_ask", 50))   # cents (integer 1-99)
    no_ask  = int(m.get("no_ask",  50))

    return {
        "ticker":     m["ticker"],
        "up_price":   yes_ask / 100,   # fraction for Up (yes) bet
        "down_price": no_ask  / 100,   # fraction for Down (no) bet
    }


def check_bet_result(ticker: str, signal: str) -> str:
    """
    Check if a market has resolved. Returns 'Win', 'Loss', or 'Pending'.
    """
    resp = requests.get(f"{BASE}/markets/{ticker}", timeout=10)
    resp.raise_for_status()
    m = resp.json().get("market", {})

    result = m.get("result")
    if result == "yes":
        outcome = "Up"
    elif result == "no":
        outcome = "Down"
    else:
        return "Pending"

    return "Win" if outcome == signal else "Loss"


# ── RSA auth helper ────────────────────────────────────────────────────────────

def _auth_headers(cfg: Config, method: str, path: str) -> dict:
    """Build Kalshi RSA-signed request headers."""
    ts  = str(int(time.time() * 1000))
    msg = f"{ts}{method}{path.split('?')[0]}".encode()

    private_key = serialization.load_pem_private_key(
        cfg.kalshi_private_key.encode(), password=None
    )
    sig = private_key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY":       cfg.kalshi_api_key,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type":            "application/json",
    }


# ── Authenticated helpers ──────────────────────────────────────────────────────

def get_bankroll(cfg: Config) -> float:
    """Fetch current balance in dollars. Returns 0.0 on error."""
    path = "/trade-api/v2/portfolio/balance"
    headers = _auth_headers(cfg, "GET", path)
    try:
        resp = requests.get(
            f"https://api.elections.kalshi.com{path}",
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        cents = resp.json().get("balance", 0)
        return float(cents) / 100
    except Exception as e:
        print(f"[WARN] Could not fetch bankroll: {e}")
        return 0.0


def place_order(
    signal: str,
    market: dict,
    bet_usdc: float,
    cfg: Config,
) -> dict:
    """
    Place a limit buy order for `signal` ('Up' -> yes, 'Down' -> no).

    Kalshi contracts each pay $1 if they win.
    count = round(bet_usdc / price_fraction)
    price sent to API as integer cents (1-99).

    Dry-run mode logs without calling the API.
    """
    side        = "yes"  if signal == "Up" else "no"
    price_frac  = market["up_price"] if signal == "Up" else market["down_price"]
    price_cents = max(1, min(99, round(price_frac * 100)))
    count       = max(1, round(bet_usdc / price_frac))

    if cfg.dry_run:
        usdc_spent = count * price_frac
        print(f"  [DRY RUN] Would buy {count} '{side}' contracts @ {price_cents}c "
              f"(~${usdc_spent:.2f} USDC) on {market['ticker']}")
        return {
            "status":     "DRY_RUN",
            "signal":     signal,
            "ticker":     market["ticker"],
            "side":       side,
            "price":      price_frac,
            "count":      count,
            "usdc_spent": round(usdc_spent, 4),
        }

    # Live order
    path    = "/trade-api/v2/portfolio/orders"
    headers = _auth_headers(cfg, "POST", path)
    body    = {
        "ticker":           market["ticker"],
        "action":           "buy",
        "type":             "limit",
        "side":             side,
        f"{side}_price":    price_cents,
        "count":            count,
        "client_order_id":  str(uuid.uuid4()),
    }

    try:
        resp = requests.post(
            f"https://api.elections.kalshi.com{path}",
            headers=headers,
            json=body,
            timeout=10,
        )
        if resp.status_code not in (200, 201):
            return {"status": "ERROR", "error": resp.text, "ticker": market["ticker"]}

        order = resp.json().get("order", {})
        return {
            "status":     "PLACED",
            "signal":     signal,
            "order_id":   order.get("order_id", ""),
            "price":      price_frac,
            "count":      count,
            "usdc_spent": round(count * price_frac, 4),
            "ticker":     market["ticker"],
        }
    except Exception as e:
        return {"status": "ERROR", "error": str(e), "ticker": market["ticker"]}
