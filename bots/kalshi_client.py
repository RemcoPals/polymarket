"""
Kalshi API client — parametrized by asset (BTC / ETH / SOL / XRP).

Read-only helpers require no authentication.
Trade helpers use RSA-signed request headers.
"""

import base64
import time
import uuid
from datetime import datetime, timezone

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from bots.config import Config

BASE = "https://api.elections.kalshi.com/trade-api/v2"

SERIES_MAP: dict[str, str] = {
    "btc": "KXBTC15M",
    "eth": "KXETH15M",
    "sol": "KXSOL15M",
    "xrp": "KXXRP15M",
}


class KalshiClient:
    """
    Kalshi API client for a specific asset's 15-min market series.

    Usage:
        client = KalshiClient("eth", cfg)
        outcomes, latest = client.fetch_recent_outcomes()
        market = client.get_active_market()
        result = client.place_order("Down", market, 5.00)
    """

    def __init__(self, asset: str, cfg: Config) -> None:
        asset_lower = asset.lower()
        if asset_lower not in SERIES_MAP:
            raise ValueError(f"Unknown asset '{asset}'. Valid: {sorted(SERIES_MAP)}")
        self.asset  = asset_lower
        self.series = SERIES_MAP[asset_lower]
        self.cfg    = cfg

    # ── Read-only helpers (no auth) ───────────────────────────────────────────

    def fetch_recent_outcomes(self, n: int = 10) -> tuple[list[str], datetime | None]:
        """
        Fetch the last `n` resolved markets for this asset's series, oldest first.
        Returns (outcomes, latest_close_time).
        outcomes: list of 'Up' / 'Down' strings.
        latest_close_time: close_time of the most recently settled market (UTC), or None.
        result='yes' → Up (asset ended higher), result='no' → Down.
        """
        all_markets: list[dict] = []
        for status in ("settled", "closed"):
            resp = requests.get(
                f"{BASE}/markets",
                params={"series_ticker": self.series, "status": status, "limit": n},
                timeout=10,
            )
            resp.raise_for_status()
            all_markets.extend(resp.json().get("markets", []))

        # Deduplicate by ticker, sort newest-first, keep only markets with a result
        seen: set[str] = set()
        markets: list[dict] = []
        for m in all_markets:
            if m["ticker"] not in seen and m.get("result") in ("yes", "no"):
                seen.add(m["ticker"])
                markets.append(m)
        markets.sort(key=lambda m: m.get("close_time", ""), reverse=True)
        markets = markets[:n]

        outcomes: list[str] = []
        latest_close_time: datetime | None = None
        for i, m in enumerate(markets):
            result = m.get("result")
            if result == "yes":
                outcomes.append("Up")
            elif result == "no":
                outcomes.append("Down")
            if i == 0 and m.get("close_time"):
                latest_close_time = datetime.fromisoformat(
                    m["close_time"].replace("Z", "+00:00")
                )

        outcomes.reverse()  # oldest-first
        return outcomes, latest_close_time

    def get_active_market(self) -> dict:
        """
        Fetch the current open market for this asset's series.
        Returns dict with: ticker, up_price, down_price (fractions 0-1).
        Raises RuntimeError if no open market is found or book is unreliable.
        """
        resp = requests.get(
            f"{BASE}/markets",
            params={"series_ticker": self.series, "status": "open", "limit": 1},
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json().get("markets", [])

        if not markets:
            raise RuntimeError(
                f"No open {self.asset.upper()} 15-min market found on Kalshi"
            )

        m = markets[0]

        # Reject markets whose close_time has already passed
        if m.get("close_time"):
            close_dt = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            if close_dt <= datetime.now(timezone.utc):
                raise RuntimeError(
                    f"Market {m['ticker']} close_time {close_dt:%H:%M:%S} UTC has passed — "
                    "new market not yet listed"
                )

        yes_ask = m.get("yes_ask") or 0
        no_ask  = m.get("no_ask")  or 0

        if not yes_ask or not no_ask or int(yes_ask) < 5 or int(no_ask) < 5:
            raise RuntimeError(
                f"Unreliable order book (yes_ask={yes_ask}, no_ask={no_ask}) — "
                "market may not have opened yet or book is empty"
            )

        return {
            "ticker":     m["ticker"],
            "up_price":   int(yes_ask) / 100,   # fraction for Up (yes) bet
            "down_price": int(no_ask)  / 100,   # fraction for Down (no) bet
            "close_time": m.get("close_time", ""),
        }

    def get_market_price(self, ticker: str) -> dict:
        """
        Fetch current bid/ask for a specific market ticker.
        Used by the monitoring loop to poll mid-market prices.

        Returns dict with integer cent values:
            yes_ask, yes_bid, no_ask, no_bid
        Returns zeros for any missing field (caller should guard against 0 bids).
        """
        resp = requests.get(f"{BASE}/markets/{ticker}", timeout=10)
        resp.raise_for_status()
        m = resp.json().get("market", {})
        return {
            "yes_ask": int(m.get("yes_ask") or 0),
            "yes_bid": int(m.get("yes_bid") or 0),
            "no_ask":  int(m.get("no_ask")  or 0),
            "no_bid":  int(m.get("no_bid")  or 0),
        }

    def check_bet_result(self, ticker: str, signal: str) -> str:
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

    # ── RSA auth helper ───────────────────────────────────────────────────────

    def _auth_headers(self, method: str, path: str) -> dict:
        """Build Kalshi RSA-signed request headers."""
        ts  = str(int(time.time() * 1000))
        msg = f"{ts}{method}{path.split('?')[0]}".encode()

        private_key = serialization.load_pem_private_key(
            self.cfg.kalshi_private_key.encode(), password=None
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
            "KALSHI-ACCESS-KEY":       self.cfg.kalshi_api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "Content-Type":            "application/json",
        }

    # ── Authenticated helpers ─────────────────────────────────────────────────

    def get_bankroll(self) -> float:
        """Fetch current balance in dollars. Returns 0.0 on error."""
        path = "/trade-api/v2/portfolio/balance"
        headers = self._auth_headers("GET", path)
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

    def get_portfolio_overview(self) -> dict:
        """
        Fetch open (unsettled) positions.
        Returns dict with: positions_count, positions_value (cost basis USD), positions (list).
        """
        path = "/trade-api/v2/portfolio/positions"
        headers = self._auth_headers("GET", path)
        try:
            resp = requests.get(
                f"https://api.elections.kalshi.com{path}",
                params={"settlement_status": "unsettled", "limit": 50},
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()
            positions = resp.json().get("market_positions", [])
            open_pos  = [p for p in positions if p.get("position", 0) != 0]
            total_exposure = sum(abs(p.get("market_exposure", 0)) for p in open_pos) / 100
            return {
                "positions":       open_pos,
                "positions_count": len(open_pos),
                "positions_value": total_exposure,
            }
        except Exception as e:
            print(f"[WARN] Could not fetch positions: {e}")
            return {"positions": [], "positions_count": 0, "positions_value": 0.0}

    def place_order(
        self,
        signal: str,
        market: dict,
        bet_usdc: float,
    ) -> dict:
        """
        Place a limit buy order for `signal` ('Up' -> yes, 'Down' -> no).

        Kalshi contracts each pay $1 if they win.
        count = round(bet_usdc / price_fraction)
        price sent to API as integer cents (1-99).

        Dry-run mode logs without calling the API.
        """
        side        = "yes" if signal == "Up" else "no"
        price_frac  = market["up_price"] if signal == "Up" else market["down_price"]
        price_cents = max(1, min(99, round(price_frac * 100)))
        count       = max(1, round(bet_usdc / price_frac))

        if self.cfg.dry_run:
            usdc_spent = count * price_frac
            print(f"  [DRY RUN] Would buy {count} '{side}' contracts @ {price_cents}c "
                  f"(~${usdc_spent:.2f} USDC) on {market['ticker']}")
            return {
                "status":     "DRY_RUN",
                "signal":     signal,
                "ticker":     market["ticker"],
                "side":       side,
                "price":      price_frac,
                "price_cents": price_cents,
                "count":      count,
                "usdc_spent": round(usdc_spent, 4),
            }

        path    = "/trade-api/v2/portfolio/orders"
        headers = self._auth_headers("POST", path)
        body    = {
            "ticker":          market["ticker"],
            "action":          "buy",
            "type":            "limit",
            "side":            side,
            f"{side}_price":   price_cents,
            "count":           count,
            "client_order_id": str(uuid.uuid4()),
        }

        try:
            resp = requests.post(
                f"https://api.elections.kalshi.com{path}",
                headers=headers,
                json=body,
                timeout=10,
            )
            if resp.status_code not in (200, 201):
                print(f"  [ERROR]   Order rejected ({resp.status_code}): {resp.text[:200]}")
                return {"status": "ERROR", "error": resp.text, "ticker": market["ticker"]}

            order = resp.json().get("order", {})
            usdc_spent = round(count * price_frac, 4)
            print(f"  [LIVE]    Bought {count} '{side}' contracts @ {price_cents}c "
                  f"(~${usdc_spent:.2f} USDC) on {market['ticker']}")
            return {
                "status":      "PLACED",
                "signal":      signal,
                "order_id":    order.get("order_id", ""),
                "price":       price_frac,
                "price_cents": price_cents,
                "count":       count,
                "usdc_spent":  usdc_spent,
                "ticker":      market["ticker"],
                "side":        side,
            }
        except Exception as e:
            return {"status": "ERROR", "error": str(e), "ticker": market["ticker"]}

    def sell_position(
        self,
        ticker: str,
        side: str,
        count: int,
        limit_price_cents: int,
        reason: str = "",
    ) -> dict:
        """
        Place a limit sell order to exit an existing position.

        Args:
            ticker:             market ticker
            side:               'yes' or 'no' — same side you originally bought
            count:              number of contracts to sell
            limit_price_cents:  minimum cents to accept (use current_bid - 1 for fast fill)
            reason:             'TAKE_PROFIT' or 'STOP_LOSS' for logging

        Dry-run mode logs without calling the API.
        """
        if self.cfg.dry_run:
            usdc_est = count * limit_price_cents / 100
            print(f"  [DRY RUN] Would SELL {count} '{side}' @ {limit_price_cents}c "
                  f"(~${usdc_est:.2f}) [{reason}] on {ticker}")
            return {
                "status":        "DRY_RUN",
                "ticker":        ticker,
                "side":          side,
                "price_cents":   limit_price_cents,
                "count":         count,
                "usdc_received": round(usdc_est, 4),
                "reason":        reason,
            }

        path    = "/trade-api/v2/portfolio/orders"
        headers = self._auth_headers("POST", path)
        body    = {
            "ticker":          ticker,
            "action":          "sell",
            "type":            "limit",
            "side":            side,
            f"{side}_price":   limit_price_cents,
            "count":           count,
            "client_order_id": str(uuid.uuid4()),
        }

        try:
            resp = requests.post(
                f"https://api.elections.kalshi.com{path}",
                headers=headers,
                json=body,
                timeout=10,
            )
            if resp.status_code not in (200, 201):
                print(f"  [ERROR] Sell rejected ({resp.status_code}): {resp.text[:200]}")
                return {"status": "ERROR", "error": resp.text}

            order = resp.json().get("order", {})
            usdc_est = round(count * limit_price_cents / 100, 4)
            print(f"  [LIVE]    SOLD {count} '{side}' @ {limit_price_cents}c "
                  f"(~${usdc_est:.2f}) [{reason}] on {ticker}")
            return {
                "status":        "PLACED",
                "order_id":      order.get("order_id", ""),
                "ticker":        ticker,
                "side":          side,
                "price_cents":   limit_price_cents,
                "count":         count,
                "usdc_received": usdc_est,
                "reason":        reason,
            }
        except Exception as e:
            return {"status": "ERROR", "error": str(e)}

    def get_order_status(self, order_id: str) -> str:
        """
        Fetch the current status of an order.
        Returns one of: 'resting', 'executed', 'canceled', 'pending', or 'unknown'.
        """
        path = f"/trade-api/v2/portfolio/orders/{order_id}"
        headers = self._auth_headers("GET", path)
        try:
            resp = requests.get(
                f"https://api.elections.kalshi.com{path}",
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()
            order = resp.json().get("order", {})
            return order.get("status", "unknown")
        except Exception as e:
            print(f"  [WARN] Could not check order status: {e}")
            return "unknown"

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order by ID. Returns True if successfully cancelled."""
        path = f"/trade-api/v2/portfolio/orders/{order_id}"
        headers = self._auth_headers("DELETE", path)
        try:
            resp = requests.delete(
                f"https://api.elections.kalshi.com{path}",
                headers=headers,
                timeout=10,
            )
            if resp.status_code in (200, 204):
                print(f"  [LIVE]    Order {order_id} cancelled")
                return True
            print(f"  [WARN]    Cancel failed ({resp.status_code}): {resp.text[:200]}")
            return False
        except Exception as e:
            print(f"  [WARN] Could not cancel order: {e}")
            return False
