"""
Backtester — replays historical Binance candles through the SAME strategy code
the live bot runs (strategies.py + PnLTracker), so a backtest result actually
predicts what your parameters would do live. No keys needed (public data).

  py backtest.py --strategies grid --days 7
  py backtest.py --strategies pmm "--pmm-spreads=0.1%,0.3%" --days 3 --candles 1m

Fill model (honest about its limits):
  - one candle per tick; strategies decide on the candle CLOSE
  - a resting BUY fills if a later candle's LOW touches its price; a SELL
    fills if the HIGH touches it — always at the order's own (maker) price
  - market orders fill at the deciding candle's close
  - intra-candle sequencing is unknowable from OHLC, so same-candle
    place-and-fill is disallowed (orders are only hit from the NEXT candle)
Fees default to 0.10% per fill (--fee), Binance spot's standard rate —
deliberately harsher than the demo account's 0%.
"""
import argparse
import os
import sys
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from binance_client import BinanceClient  # noqa: E402
from bot import (PnLTracker, STRATEGY_NAMES, add_strategy_args,  # noqa: E402
                 build_strategies, get_filters)
from strategies import MarketState, round_to  # noqa: E402

CANDLE_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000,
             "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000}


def fetch_candles(client, symbol, interval, days):
    step = CANDLE_MS[interval]
    total = int(days * 86_400_000 // step)
    start = int(time.time() * 1000) - total * step
    out = []
    while len(out) < total:
        r = client._request("GET", "/api/v3/klines",
                            {"symbol": symbol, "interval": interval,
                             "limit": 1000, "startTime": start})
        batch = r["body"] if r["status"] == 200 else []
        if not batch:
            break
        out.extend(batch)
        start = batch[-1][0] + step
        if len(batch) < 1000:
            break
    return out[:total]


class BacktestRunner:
    """Mirrors BotRunner's desired-vs-resting diff, against a virtual book."""

    def __init__(self, strategies, filters, fee_rate):
        self.strategies = strategies
        self.filters = filters
        self.fee_rate = fee_rate
        self.resting = {}           # cid -> {tag, side, price, qty, placed_ts}
        self.pnl_by = {}
        self.seq = 0
        self.fills = self.placements = self.cancels = 0

    def pnl_for(self, tag):
        return self.pnl_by.setdefault(tag.split("-")[0], PnLTracker())

    def round_qty(self, order, price):
        f = self.filters
        qty = order.get("qty")
        if qty is None:
            qty = order["quote_qty"] / price
        qty = round_to(Decimal(qty), f["step_size"], ROUND_DOWN)
        if qty * price < f["min_notional"]:
            qty = round_to(f["min_notional"] * Decimal("1.01") / price,
                           f["step_size"], ROUND_UP)
        return qty

    def fill(self, tag, side, qty, price):
        quote = qty * price
        self.pnl_for(tag).on_fill(side, qty, quote, quote * self.fee_rate)
        self.strategies[tag.split("-")[0]].on_fill(
            tag, {"executedQty": f"{qty:f}", "price": f"{price:f}", "side": side})
        self.fills += 1

    def step(self, candle, window):
        ts = candle[0] / 1000
        high, low = Decimal(candle[2]), Decimal(candle[3])
        close = Decimal(candle[4])

        # 1) does this candle's range hit any order resting from earlier candles?
        for cid, o in list(self.resting.items()):
            if (o["side"] == "BUY" and low <= o["price"]) or \
               (o["side"] == "SELL" and high >= o["price"]):
                del self.resting[cid]
                self.fill(o["tag"], o["side"], o["qty"], o["price"])

        # 2) strategies decide on the close
        state = MarketState(close, close, close, self.filters,
                            candles=window, ts=ts)
        desired = {}
        for strat in self.strategies.values():
            for order in strat.desired_orders(state):
                desired[order["tag"]] = order

        # 3) sync the virtual book (same drift/age rules as the live runner)
        by_tag = {o["tag"]: (cid, o) for cid, o in self.resting.items()}
        for tag, (cid, o) in by_tag.items():
            want = desired.get(tag)
            if want is None or want["type"] == "MARKET":
                del self.resting[cid]
                self.cancels += 1
                continue
            price = round_to(want["price"], self.filters["tick_size"])
            tol = (want["price"] * want["tolerance"] if want.get("tolerance")
                   else self.filters["tick_size"] / 2)
            too_old = "max_age" in want and ts - o["placed_ts"] > want["max_age"]
            if abs(price - o["price"]) > tol or too_old:
                del self.resting[cid]
                self.cancels += 1
            else:
                desired.pop(tag)        # keep as-is

        for tag, order in desired.items():
            if order["type"] == "MARKET":
                qty = self.round_qty(order, close)
                self.fill(tag, order["side"], qty, close)
                continue
            price = round_to(order["price"], self.filters["tick_size"])
            # LIMIT_MAKER semantics: an order that would cross is rejected live,
            # so don't rest it (and never fill inside its own placement candle)
            if (order["side"] == "BUY" and price >= close) or \
               (order["side"] == "SELL" and price <= close):
                continue
            self.seq += 1
            self.resting[f"{tag}_{self.seq}"] = {
                "tag": tag, "side": order["side"], "price": price,
                "qty": self.round_qty(order, price), "placed_ts": ts}
            self.placements += 1


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=float, default=7, help="history window to replay")
    p.add_argument("--candles", default="3m", choices=sorted(CANDLE_MS),
                   help="candle interval (finer = more accurate fills, more data)")
    p.add_argument("--fee", default="0.1%",
                   help="fee per fill, e.g. 0.1%% (demo charges 0, live spot 0.1%%)")
    add_strategy_args(p)
    args = p.parse_args()

    fee_tok = args.fee.strip()
    fee_rate = (Decimal(fee_tok[:-1]) / 100 if fee_tok.endswith("%")
                else Decimal(fee_tok))

    client = BinanceClient("demo")
    filters, _ = get_filters(client, args.symbol)
    strategies = build_strategies(args)
    if not strategies:
        sys.exit(f"no valid strategies in '{args.strategies}'")
    names = ", ".join(STRATEGY_NAMES.get(k, k) for k in strategies)

    print(f"BACKTEST {args.symbol} | {names} | {args.days:g} days of "
          f"{args.candles} candles | fee {fee_rate * 100:.2f}% per fill")
    candles = fetch_candles(client, args.symbol, args.candles, args.days)
    if len(candles) < 30:
        sys.exit(f"not enough candle history returned ({len(candles)})")
    first_close, last_close = Decimal(candles[0][4]), Decimal(candles[-1][4])
    span_pct = (last_close - first_close) / first_close * 100
    print(f"loaded {len(candles)} candles: "
          f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(candles[0][0] / 1000))} "
          f"-> {time.strftime('%Y-%m-%d %H:%M', time.localtime(candles[-1][0] / 1000))}, "
          f"price {first_close.normalize():f} -> {last_close.normalize():f} "
          f"({span_pct:+.2f}%)\n")

    runner = BacktestRunner(strategies, filters, fee_rate)
    window = []
    for candle in candles:
        runner.step(candle, window[-120:] if window else None)
        window.append(candle)

    base = filters.get("base_asset", "base")
    quote = filters.get("quote_asset", "quote")
    for strat in strategies.values():
        print(strat.summary())
    print()
    if not runner.pnl_by:
        print("no fills — the price never reached any order in this window")
    else:
        print(f"PnL by strategy, in {quote} (unrealized marked at the final close):")
        realized = unreal = fees = Decimal("0")
        for prefix, tracker in runner.pnl_by.items():
            print(f"  {STRATEGY_NAMES.get(prefix, prefix):<14} "
                  f"{tracker.summary_line(last_close, base)}")
            realized += tracker.realized
            unreal += tracker.unrealized(last_close)
            fees += tracker.fees
        total = realized + unreal - fees
        hold = args.total_quote * span_pct / 100
        print(f"  {'TOTAL':<14} {total:+.2f} {quote}  "
              f"({total / args.total_quote * 100:+.2f}% of the "
              f"{args.total_quote:.0f} {quote} budget)")
        print(f"\nbuy & hold the same {args.total_quote:.0f} {quote}: "
              f"{hold:+.2f} {quote} ({span_pct:+.2f}%) — "
              f"strategy {'beat' if total > hold else 'trailed'} it "
              f"by {abs(total - hold):.2f}")
    print(f"\nsimulated: {runner.placements} placements, {runner.cancels} cancels, "
          f"{runner.fills} fills over {len(candles)} candles")
    print("note: OHLC fill simulation is optimistic about queue position and "
          "ignores order-book depth — treat results as directional, not exact.")


if __name__ == "__main__":
    main()
