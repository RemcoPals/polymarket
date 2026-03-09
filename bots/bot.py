"""
Generic 15-minute streak reversal bot — works for any Kalshi asset.

Strategy: if the last N consecutive resolved markets all went the same direction
(streak >= min_streak, default 3), bet on the *opposite* direction in the next market.

With ENABLE_DYNAMIC_EXIT=true, the bot also monitors mid-market prices and can
exit early via take-profit or stop-loss to reduce variance.

Run via run_bot.py:
    python run_bot.py --asset eth          # ETH 15-min (dry run by default)
    DRY_RUN=false python run_bot.py --asset eth
"""

import random
import time
from datetime import datetime, timezone

from bots.config import Config
from bots.kalshi_client import KalshiClient
from bots.strategy import (
    DynamicExitManager,
    compute_streak,
    get_binance_magnitude,
    get_signal,
    get_streak_signal_edge,
    kelly_bet_size,
)

ASSET_NAMES = {
    "btc": "BTC",
    "eth": "ETH",
    "sol": "SOL",
    "xrp": "XRP",
}


def _sleep_until_next_slot(buffer_seconds: int = 5) -> None:
    """Sleep until the next 15-minute boundary + buffer_seconds."""
    now = datetime.now(timezone.utc)
    seconds_into_slot = now.timestamp() % 900
    sleep_secs = max(0, (900 - seconds_into_slot) + buffer_seconds)
    wake = datetime.fromtimestamp(now.timestamp() + sleep_secs, tz=timezone.utc)
    print(f"[{now:%H:%M:%S} UTC] Sleeping {sleep_secs:.0f}s -> next check at {wake:%H:%M:%S} UTC")
    time.sleep(sleep_secs)


def _net_profit_hold(bet_usdc: float, price: float, fee_mult: float) -> float:
    """Net profit (after fee) for a bet held to resolution and won."""
    count = bet_usdc / price
    gross = count * (1 - price)
    fee   = count * fee_mult * price * (1 - price)
    return gross - fee


def _monitor_and_exit(
    pending_bet: dict,
    client: KalshiClient,
    cfg: Config,
    slot_end_time: datetime,
) -> dict:
    """
    Monitor an open position during the 15-min window and exit early if
    take-profit or stop-loss thresholds are hit.

    In dry-run mode: polls real prices but logs sells without executing them.
    Returns the (potentially updated) pending_bet dict.
    If early exit occurred, adds 'early_exit' and 'usdc_received' keys.
    """
    if not cfg.enable_dynamic_exit:
        return pending_bet

    exit_mgr = DynamicExitManager(
        entry_price_cents = pending_bet["price_cents"],
        side              = pending_bet["side"],
        count             = pending_bet["count"],
        tp_cents          = cfg.tp_cents,
        sl_cents          = cfg.sl_cents,
        min_hold_secs     = cfg.min_hold_secs,
    )

    side_bid_key = f"{pending_bet['side']}_bid"

    while True:
        now            = datetime.now(timezone.utc)
        time_remaining = (slot_end_time - now).total_seconds()

        # Stop monitoring with 60s left — not enough time to fill a sell order
        if time_remaining < 60:
            print(f"  [EXIT MGR] <60s remaining — holding to resolution")
            break

        time.sleep(cfg.poll_interval_secs)

        try:
            prices = client.get_market_price(pending_bet["ticker"])
        except Exception as e:
            print(f"  [WARN] Price fetch failed: {e} — continuing")
            continue

        current_bid = prices.get(side_bid_key, 0)
        if current_bid == 0:
            print(f"  [WARN] Zero {side_bid_key} — skipping this check")
            continue

        now            = datetime.now(timezone.utc)
        time_remaining = (slot_end_time - now).total_seconds()
        gain           = current_bid - pending_bet["price_cents"]
        print(f"  [EXIT MGR] t-{time_remaining:.0f}s  "
              f"entry={pending_bet['price_cents']}c  bid={current_bid}c  "
              f"gain={gain:+d}c")

        exit_signal = exit_mgr.check_exit(current_bid)
        if exit_signal is None:
            continue

        # Threshold hit — attempt sell
        print(f"  [EXIT MGR] {exit_signal} triggered!")
        # 1c below bid for fast fill; floor at 1c
        sell_limit = max(1, current_bid - 1)
        sell_result = client.sell_position(
            ticker            = pending_bet["ticker"],
            side              = pending_bet["side"],
            count             = pending_bet["count"],
            limit_price_cents = sell_limit,
            reason            = exit_signal,
        )

        if sell_result.get("status") in ("PLACED", "DRY_RUN"):
            usdc_received = sell_result.get("usdc_received", 0.0)
            pnl = usdc_received - pending_bet["bet_usdc"]
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            print(f"  [EXIT MGR] Early exit: received ${usdc_received:.2f}  P&L {pnl_str}")
            pending_bet = {
                **pending_bet,
                "early_exit":    exit_signal,
                "usdc_received": usdc_received,
            }
        else:
            print(f"  [EXIT MGR] Sell failed — holding to resolution")
        break

    return pending_bet


def run_bot(asset: str) -> None:
    """Main bot loop for the specified asset."""
    asset_name = ASSET_NAMES.get(asset.lower(), asset.upper())
    cfg    = Config(asset=asset.lower())
    client = KalshiClient(asset, cfg)

    print("=" * 60)
    print(f"  {asset_name} 15-min Streak Reversal Bot  [Kalshi]")
    print("=" * 60)
    print(f"  asset          : {asset.upper()}")
    print(f"  series         : {client.series}")
    print(f"  dry_run        : {cfg.dry_run}")
    print(f"  min_streak     : {cfg.min_streak}")
    print(f"  lookback       : {cfg.lookback} markets")
    print(f"  kelly x        : {cfg.kelly_multiplier}")
    print(f"  max_bet_pct    : {cfg.max_bet_pct:.0%} of bankroll")
    print(f"  max_daily_loss : {cfg.max_daily_loss_pct:.0%} of day-open balance")
    print(f"  tiered_kelly   : streak→edge {dict(sorted(cfg.streak_edge_table.items()))}")
    mag_status = f"ENABLED (>={cfg.magnitude_threshold_sigma:.1f}σ +{cfg.magnitude_bonus_edge:.3f} edge)" if cfg.use_magnitude_filter else "disabled"
    print(f"  magnitude_filt : {mag_status}")
    print(f"  dynamic_exit   : {'ENABLED' if cfg.enable_dynamic_exit else 'disabled'}", end="")
    if cfg.enable_dynamic_exit:
        print(f"  (TP={cfg.tp_cents}c / SL={cfg.sl_cents}c / min_hold={cfg.min_hold_secs}s)", end="")
    print()
    if cfg.dry_run:
        print(f"  start balance  : ${cfg.dry_run_bankroll:.2f} (simulated)")
        print("  [DRY RUN -- no real orders will be placed]")
    elif not cfg.is_ready_to_trade():
        print("  [WARN] Credentials missing -- falling back to dry run")
        cfg.dry_run = True
    print("=" * 60)
    print()

    balance         = cfg.dry_run_bankroll if cfg.dry_run else client.get_bankroll()
    start_bal       = balance
    daily_start_bal = balance
    daily_halted    = False
    current_day     = datetime.now(timezone.utc).date()
    wins            = 0
    losses          = 0
    early_exits     = 0
    pending_bet: dict | None = None

    while True:
        _sleep_until_next_slot()
        time.sleep(random.uniform(0, 10))  # stagger API calls across concurrent bots
        now = datetime.now(timezone.utc)

        # Compute the end-time of the just-opened slot (for the monitoring loop)
        slot_start = datetime.fromtimestamp(
            int(now.timestamp() // 900) * 900, tz=timezone.utc
        )
        slot_end = datetime.fromtimestamp(slot_start.timestamp() + 900, tz=timezone.utc)

        # Reset daily tracking on a new UTC day
        if now.date() != current_day:
            current_day     = now.date()
            daily_start_bal = balance
            daily_halted    = False
            print(f"  New day! Daily loss limit reset. Day-open balance: ${daily_start_bal:.2f}")

        if daily_halted:
            print(f"  Daily loss limit reached — waiting for next day")
            continue

        if not cfg.dry_run:
            live_bal = client.get_bankroll()
            if live_bal > 0:
                balance = live_bal
            pf = client.get_portfolio_overview()
            pnl = balance - start_bal
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            open_str = (
                f"  open={pf['positions_count']} (${pf['positions_value']:.2f} at risk)"
                if pf["positions_count"] else ""
            )
            print(f"\n-- Slot {now:%Y-%m-%d %H:%M} UTC  |  Balance: ${balance:.2f}  |  "
                  f"P&L: {pnl_str}  |  W:{wins} L:{losses} E:{early_exits}{open_str} --")
        else:
            print(f"\n-- Slot {now:%Y-%m-%d %H:%M} UTC  |  Balance: ${balance:.2f}  |  "
                  f"W:{wins} L:{losses} E:{early_exits} --")

        # 1. Poll Kalshi until oracle has settled the market that just closed.
        outcomes     = None
        latest_close = None
        for attempt in range(24):
            try:
                settled, latest_close = client.fetch_recent_outcomes(cfg.lookback)
                if latest_close and latest_close >= slot_start:
                    outcomes = settled
                    break
            except Exception as e:
                print(f"  [WARN] fetch_recent_outcomes: {e}")
            elapsed = int((datetime.now(timezone.utc) - now).total_seconds())
            lc_str  = f"{latest_close:%H:%M:%S} UTC" if latest_close else "none"
            print(f"  Waiting for oracle... ({elapsed}s, latest close: {lc_str})")
            time.sleep(10)

        if outcomes is None:
            print("  Oracle did not settle in time — skipping slot")
            continue

        # 2. Resolve previous bet.
        if pending_bet is not None:
            # Early exit: P&L was already realised during the monitoring loop
            if pending_bet.get("early_exit"):
                usdc_received = pending_bet.get("usdc_received", 0.0)
                pnl = usdc_received - pending_bet["bet_usdc"]
                balance += pnl
                early_exits += 1
                pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
                print(f"  RESULT  : EARLY EXIT ({pending_bet['early_exit']})  {pnl_str}  "
                      f"(received ${usdc_received:.2f})")
                print(f"  Balance : ${balance:.2f}  (started at ${start_bal:.2f})")
                pending_bet = None
            else:
                try:
                    outcome = client.check_bet_result(
                        pending_bet["ticker"], pending_bet["signal"]
                    )
                    if outcome == "Win":
                        profit = _net_profit_hold(
                            pending_bet["bet_usdc"], pending_bet["price"],
                            cfg.kalshi_fee_mult
                        )
                        balance += profit
                        wins    += 1
                        print(f"  RESULT  : WIN  +${profit:.2f}  ({pending_bet['signal']} won)")
                        print(f"  Balance : ${balance:.2f}  (started at ${start_bal:.2f})")
                        pending_bet = None
                    elif outcome == "Loss":
                        balance -= pending_bet["bet_usdc"]
                        losses  += 1
                        print(f"  RESULT  : LOSS -${pending_bet['bet_usdc']:.2f}  "
                              f"({pending_bet['signal']} lost)")
                        print(f"  Balance : ${balance:.2f}  (started at ${start_bal:.2f})")
                        pending_bet = None
                    else:
                        print(f"  RESULT  : Still pending ({pending_bet['ticker']})")
                except Exception as e:
                    print(f"  [WARN] Could not check bet result: {e}")

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
        daily_loss = daily_start_bal - balance
        daily_loss_limit = daily_start_bal * cfg.max_daily_loss_pct
        if daily_loss >= daily_loss_limit:
            print(f"  Daily loss limit hit (-${daily_loss:.2f} / "
                  f"{daily_loss/daily_start_bal:.0%} of day-open) -- stopping for today")
            daily_halted = True
            continue

        # 5. Fetch current market
        market = None
        for attempt in range(4):
            try:
                market = client.get_active_market()
                break
            except Exception as e:
                if attempt < 3:
                    print(f"  [WARN] {e} — retrying in 15s ({attempt+1}/3)...")
                    time.sleep(15)
                else:
                    print(f"  [ERROR] get_active_market: {e} -- skipping slot")
        if market is None:
            continue

        # 6. Compute total signal edge and Kelly bet size
        price = market["up_price"] if signal == "Up" else market["down_price"]

        # Strategy F: tiered edge based on streak length
        base_edge  = get_streak_signal_edge(streak, cfg)
        total_edge = base_edge

        # Strategy C: magnitude bonus if previous candle was large
        if cfg.use_magnitude_filter:
            mag = get_binance_magnitude(cfg.asset)
            sigma     = cfg.get_asset_sigma()
            threshold = sigma * cfg.magnitude_threshold_sigma
            if mag is not None:
                if mag > threshold:
                    total_edge += cfg.magnitude_bonus_edge
                    print(f"  Magnitude: |logret|={mag:.4f} > {threshold:.4f} "
                          f"(>{cfg.magnitude_threshold_sigma:.1f}σ) → +{cfg.magnitude_bonus_edge:.3f} edge")
                else:
                    print(f"  Magnitude: |logret|={mag:.4f} ≤ {threshold:.4f} "
                          f"(<{cfg.magnitude_threshold_sigma:.1f}σ) → no bonus")
            else:
                print(f"  Magnitude: Binance fetch failed — using base edge only")

        win_prob = min(price + total_edge, 0.99)
        effective_fee_rate = cfg.kalshi_fee_mult * price
        bet_usdc = kelly_bet_size(
            win_prob,
            price,
            balance,
            cfg.kelly_multiplier,
            effective_fee_rate,
            cfg.max_bet_pct,
        )

        price_cents = round(price * 100)
        print(f"  Market  : {market['ticker']}")
        print(f"  Price   : {signal} @ {price:.2f} ({price_cents}c)  "
              f"edge={total_edge:.3f}  win_prob={win_prob:.0%}")
        print(f"  Balance : ${balance:.2f}  ->  Kelly bet: ${bet_usdc:.2f}")

        if bet_usdc == 0:
            print(f"  Kelly says no edge at price={price:.2f} -- skipping")
            continue

        # 7. Place bet (fill check → confirmed cancel → safe retry)
        result = client.place_order(signal, market, bet_usdc)

        if result.get("status") == "PLACED":
            order_id = result.get("order_id", "")
            print(f"  Waiting 10s to confirm fill...")
            time.sleep(10)
            fill_status = client.get_order_status(order_id)
            if fill_status != "executed":
                print(f"  Order not filled (status={fill_status}) — cancelling...")
                client.cancel_order(order_id)
                time.sleep(2)
                post_cancel = client.get_order_status(order_id)
                if post_cancel != "canceled":
                    print(f"  Cancel unconfirmed (status={post_cancel}) — "
                          "skipping to avoid duplicate")
                    continue
                try:
                    market = client.get_active_market()
                    result = client.place_order(signal, market, bet_usdc)
                    if result.get("status") == "PLACED":
                        print(f"  Retry order placed: {result.get('order_id', '')}")
                    else:
                        print(f"  Retry failed — skipping slot")
                        continue
                except Exception as e:
                    print(f"  [WARN] Retry failed: {e} — skipping slot")
                    continue

        if result.get("status") in ("PLACED", "DRY_RUN"):
            pending_bet = {
                "ticker":      market["ticker"],
                "signal":      signal,
                "side":        result["side"],
                "price":       result["price"],
                "price_cents": result["price_cents"],
                "count":       result["count"],
                "bet_usdc":    result["usdc_spent"],
            }

            # 8. Monitor mid-market for dynamic exit (if enabled)
            pending_bet = _monitor_and_exit(pending_bet, client, cfg, slot_end)
