# Polymarket API Reference

## Overview

Polymarket is a decentralized prediction market platform where users trade shares representing yes/no (or multi-outcome) outcomes of events. Shares price range from $0.00 to $1.00 USDC, directly reflecting the market-implied probability of an outcome.

**Infrastructure:** Polygon blockchain, USDC settlement, hybrid off-chain order matching + on-chain settlement.

---

## API Services

There are three main APIs:

| Service | Base URL | Auth Required | Purpose |
|---------|----------|---------------|---------|
| **Gamma API** | `https://gamma-api.polymarket.com` | No | Market/event discovery, metadata |
| **CLOB API** | `https://clob.polymarket.com` | No (read) / Yes (trade) | Order book, price history, trades |
| **Data API** | `https://data-api.polymarket.com` | No (public) | User positions, leaderboards |

---

## Gamma API

The primary API for discovering markets and reading metadata. No authentication needed.

### Endpoints

#### Markets

```
GET /markets
```
Query parameters:
- `slug` — filter by slug (partial match works)
- `active` — `true` / `false`
- `closed` — `true` / `false`
- `limit` — results per page (default 100)
- `offset` — pagination offset
- `order` — sort field: `volume_24hr`, `volume`, `liquidity`, `end_date`, `start_date`
- `ascending` — `true` / `false` (default `false`)
- `tag_id` — filter by tag

```
GET /markets/slug/{slug}
```
Retrieve a single market by its exact slug.

#### Events

```
GET /events
```
Same query parameters as `/markets`. Events contain their associated markets — prefer this for discovery.

```
GET /events/slug/{slug}
```

#### Tags

```
GET /tags
```
Returns all available tags with their IDs. Use tag IDs to filter markets by category.

### Market Object (key fields)

```json
{
  "id": "<market_id>",
  "slug": "btc-updown-5m-1771909800",
  "question": "Will BTC be up in the next 5 minutes?",
  "conditionId": "0x...",
  "outcomes": ["Up", "Down"],
  "outcomePrices": ["0.52", "0.48"],
  "clobTokenIds": ["<up_token_id>", "<down_token_id>"],
  "active": true,
  "closed": false,
  "volume": "12345.67",
  "volume24hr": "1234.56",
  "liquidity": "5000.00"
}
```

### Rate Limits

| Endpoint | Limit |
|----------|-------|
| General  | 4,000 req / 10s |
| `/markets` | 300 req / 10s |
| `/events` | 500 req / 10s |
| Search | 350 req / 10s |

---

## CLOB API

Central Limit Order Book API for real-time prices, order books, trade history, and placing orders.

### Public Endpoints (no auth)

```
GET /prices-history
```
Historical price timeseries for a token.

Query parameters:
- `market` (required) — token ID (from `clobTokenIds`)
- `startTs` — unix timestamp (filter start)
- `endTs` — unix timestamp (filter end)
- `interval` — aggregation: `max`, `all`, `1m`, `1w`, `1d`, `6h`, `1h`
- `fidelity` — data precision in minutes (default: 1)

Response:
```json
{
  "history": [
    { "t": 1234567890, "p": 0.52 }
  ]
}
```
`t` = unix timestamp (uint32), `p` = price (float, 0–1)

---

```
GET /trades
```
Executed trade history.

Query parameters:
- `market_id` — filter by market
- `limit`, `offset` — pagination

---

```
GET /book/{market_id}
```
Current order book (bids and asks).

---

```
GET /price/{token_id}
```
Best current price for a token on a given side.

---

```
GET /midpoint/{token_id}
```
Midpoint price between best bid and best ask.

### Rate Limits

| Endpoint | Limit |
|----------|-------|
| General | 9,000 req / 10s |
| `/book`, `/price`, `/midpoint` | 1,500 req / 10s each |
| Order posting | 3,500 burst / 36,000 per 10 min |

---

## Data API

User-specific data endpoints.

```
GET /activity          # User trade history
GET /positions         # User positions
GET /leaderboard       # Rankings
```

Rate limit: 1,000 req / 10s general; 200 req / 10s for trade data.

---

## Authentication (for trading)

Public read endpoints require no auth. For placing/cancelling orders:

**L2 (API Key) — HMAC-SHA256:**

Headers:
- `POLY_API_KEY` — your API key
- `POLY_SIGNATURE` — HMAC-SHA256 of `timestamp + method + path + body`
- `POLY_TIMESTAMP` — Unix timestamp (30-second window)
- `POLY_PASSPHRASE` — passphrase set during key creation

**L1 (Wallet) — EIP-712:**

Headers: `POLY_ADDRESS`, `POLY_SIGNATURE`, `POLY_TIMESTAMP`, `POLY_NONCE`

Generate API keys at: Polymarket Settings → API, or via:
```
GET https://clob.polymarket.com/auth/derive-api-key
```

---

## WebSocket Streams

Public stream:
```
wss://ws-subscriptions-clob.polymarket.com/ws/market
```

Channels:
- `price` — live price updates
- `orderbook` — bid/ask changes
- `trades` — executed trades in real time

Private (authenticated) stream:
```
wss://ws-subscriptions-clob.polymarket.com/ws/user
```
Channels: order fills, position updates, balance changes.

Max 20 subscriptions per connection.

---

## BTC 5-Minute Up/Down Market

Polymarket runs a continuous series of 5-minute Bitcoin price prediction markets. Each resolves automatically via Chainlink oracle.

### Market Identification

**Slug format:** `btc-updown-5m-{unix_timestamp}`

The timestamp is Unix time in seconds, **floored to the nearest 5-minute boundary** (i.e., divisible by 300).

```python
import time
ts = int(time.time() // 300) * 300
slug = f"btc-updown-5m-{ts}"
# Example: "btc-updown-5m-1771909800"
```

**Outcomes:**
- `Up` — resolves 1.0 if BTC end price ≥ BTC start price
- `Down` — resolves 1.0 if BTC end price < BTC start price

**Resolution source:** Chainlink BTC/USD high-frequency oracle (not exchange prices)

Each market has two ERC-1155 conditional tokens:
- `clobTokenIds[0]` → Up token
- `clobTokenIds[1]` → Down token

### Fetch the Current Active Market

```
GET https://gamma-api.polymarket.com/markets/slug/btc-updown-5m-{ts}
```

```python
import time, requests

ts = int(time.time() // 300) * 300
r = requests.get(f"https://gamma-api.polymarket.com/markets/slug/btc-updown-5m-{ts}")
market = r.json()

up_token = market["clobTokenIds"][0]
down_token = market["clobTokenIds"][1]
up_price = market["outcomePrices"][0]   # implied probability of Up
```

### Fetch Historical Outcomes

**Method 1: Events by series_id via Gamma API (recommended)**

All BTC 5m markets belong to the series `btc-up-or-down-5m` (series id `10684`).
Query closed events for this series, newest first, and paginate until you reach the desired cutoff:

```
GET https://gamma-api.polymarket.com/events?series_id=10684&closed=true&order=endDate&ascending=false&limit=100&offset=0
```

Each event contains its embedded market with `outcomePrices`.
⚠️ **`outcomePrices` is returned as a JSON-encoded string** in the events endpoint — use `json.loads()` to parse it.

```python
import json, requests

events = []
offset = 0
while True:
    r = requests.get(
        "https://gamma-api.polymarket.com/events",
        params={"series_id": "10684", "closed": "true", "limit": 100,
                "offset": offset, "order": "endDate", "ascending": "false"}
    )
    batch = r.json()
    if not batch:
        break
    events.extend(batch)
    offset += 100

# Parse winner from each event
for e in events:
    m = e["markets"][0]
    prices = json.loads(m["outcomePrices"])  # e.g. '["1", "0"]'
    winner = "Up" if float(prices[0]) > 0.5 else "Down"
```

Typical yield: ~4 000 resolved markets per 30-day window (~40 API requests).

**Method 2: Price history per token via CLOB API**

Get the full price history of the Up token. A final price near 1.0 = Up won; near 0.0 = Down won.

```
GET https://clob.polymarket.com/prices-history?market={up_token_id}&interval=all&fidelity=1
```

**Method 3: Trade history via CLOB API**

```
GET https://clob.polymarket.com/trades?market_id={market_id}&limit=100
```

**Method 4: Browse many past markets**

```
GET https://gamma-api.polymarket.com/events?slug=btc-updown-5m&closed=true&limit=100
```

Events contain their associated markets, so this also works well for bulk historical retrieval.

---

## Python SDK

Official client library:

```bash
pip install py-clob-client
```

GitHub: [Polymarket/py-clob-client](https://github.com/Polymarket/py-clob-client)

Unofficial simpler wrapper (read-only use cases):

```bash
pip install polymarket-apis
```

---

## Key Notes

- All prices are in USDC (0–1 range = 0%–100% probability)
- No geographic restrictions on API access (as of 2026)
- Markets auto-settle via Chainlink oracle immediately after close
- On-chain resolution events (`ConditionResolution`) are queryable on Polygon for fully trustless verification
- The platform uses ERC-1155 conditional tokens (Gnosis CTF standard)
- HTTP 429 = rate limit hit; implement exponential backoff
