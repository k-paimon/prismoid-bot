"""
bare-features trading bot — grid_strike + pmm_simple + supertrend_v1 on the
Binance Spot Testnet, with request accounting. See docs/trading-bot.md.

Usage (host, pure stdlib):
  py bare-features\\bot\\bot.py --check              # connection + request cost check
  py bare-features\\bot\\bot.py --duration 120       # dry-run loop (no keys needed)
  py bare-features\\bot\\bot.py --trade              # trade on testnet (keys required)

Keys: BINANCE_API_KEY / BINANCE_API_SECRET env vars, or --credentials-account
with CONFIG_PASSWORD set (reads the encrypted bare-features credential store).
Trading is refused outside testnet.
"""
import argparse
import os
import signal
import sys
import threading
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from binance_client import BinanceClient, IPBanError, RequestMeter, WEIGHTS  # noqa: E402
from strategies import GridStrike, MarketState, PMMSimple, Supertrend, round_to  # noqa: E402

MAX_PLACEMENTS_PER_TICK = 8     # keep well inside the 50-orders/10s bucket


# ------------------------------------------------------------------ key setup

def load_keys(args):
    key = os.environ.get("BINANCE_API_KEY")
    secret = os.environ.get("BINANCE_API_SECRET")
    if key and secret:
        return key, secret, "environment variables"
    if args.credentials_account:
        password = os.environ.get("CONFIG_PASSWORD")
        if not password:
            sys.exit("CONFIG_PASSWORD env var required to decrypt the credential store")
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "credentials"))
        from credential_manager import CredentialManager
        manager = CredentialManager(config_password=password,
                                    base_path=args.credentials_base)
        keys = manager.get_decrypted_keys(args.credentials_account, "binance")
        return (keys["binance_api_key"], keys["binance_api_secret"],
                f"credential store (account: {args.credentials_account})")
    return None, None, None


def get_filters(client, symbol):
    info = client.exchange_info(symbol)
    if info["status"] != 200:
        sys.exit(f"exchangeInfo failed: {info['body']}")
    sym = info["body"]["symbols"][0]
    filters = {f["filterType"]: f for f in sym["filters"]}
    rate_limits = info["body"].get("rateLimits", [])
    return {
        "tick_size": Decimal(filters["PRICE_FILTER"]["tickSize"]),
        "step_size": Decimal(filters["LOT_SIZE"]["stepSize"]),
        "min_notional": Decimal(filters.get("NOTIONAL", filters.get(
            "MIN_NOTIONAL", {"minNotional": "10"}))["minNotional"]),
        "base_asset": sym.get("baseAsset", ""),
        "quote_asset": sym.get("quoteAsset", ""),
    }, rate_limits


class PnLTracker:
    """Average-cost PnL over the bot's own fills, in the quote currency.
    realized = closed round trips; unrealized = open inventory marked to mid."""

    def __init__(self):
        self.position = Decimal("0")
        self.avg_cost = Decimal("0")
        self.realized = Decimal("0")
        self.fees = Decimal("0")
        self.buy_volume = Decimal("0")
        self.sell_volume = Decimal("0")
        self.buys = 0
        self.sells = 0

    def on_fill(self, side, qty, quote_amount, fee_quote=Decimal("0")):
        qty, quote_amount = Decimal(qty), Decimal(quote_amount)
        if qty <= 0 or quote_amount <= 0:
            return
        price = quote_amount / qty
        self.fees += Decimal(fee_quote)
        if side == "BUY":
            self.avg_cost = ((self.avg_cost * self.position + quote_amount)
                             / (self.position + qty))
            self.position += qty
            self.buy_volume += quote_amount
            self.buys += 1
        else:
            self.realized += (price - self.avg_cost) * qty
            self.position = max(Decimal("0"), self.position - qty)
            if self.position == 0:
                self.avg_cost = Decimal("0")
            self.sell_volume += quote_amount
            self.sells += 1

    def unrealized(self, mid):
        return (mid - self.avg_cost) * self.position if mid else Decimal("0")

    def brief(self, mid):
        total = self.realized + self.unrealized(mid) - self.fees
        return (f"PnL {total:+.2f} (R{self.realized:+.2f} "
                f"U{self.unrealized(mid):+.2f})")

    def summary(self, mid, base="base", quote="quote"):
        if not (self.buys or self.sells):
            return "PnL: no fills this session"
        unreal = self.unrealized(mid)
        total = self.realized + unreal - self.fees
        lines = [f"PnL ({quote}): total {total:+.2f} = realized {self.realized:+.2f} "
                 f"+ unrealized {unreal:+.2f} - fees {self.fees:.2f}",
                 f"  {self.buys} buy fills ({self.buy_volume:.2f} {quote}), "
                 f"{self.sells} sell fills ({self.sell_volume:.2f} {quote}); "
                 f"inventory {self.position.normalize():f} {base}"
                 + (f" @ avg cost {self.avg_cost:.2f}" if self.position else "")]
        return "\n".join(lines)


def weight_limit_from(rate_limits):
    for rl in rate_limits:
        if rl.get("rateLimitType") == "REQUEST_WEIGHT" and rl.get("interval") == "MINUTE":
            return rl["limit"] * rl.get("intervalNum", 1)
    return 6000


# ------------------------------------------------------------ connection check

def connection_check(client, args):
    print(f"CONNECTION CHECK — Binance Spot {client.mode.upper()} ({client.base})\n")

    rtts = []
    for _ in range(3):
        t0 = time.time()
        r = client.ping()
        rtts.append((time.time() - t0) * 1000)
        if r["status"] != 200:
            sys.exit(f"ping failed: HTTP {r['status']} {r['body']}")
    print(f"[1] ping OK — RTT {min(rtts):.0f}/{sum(rtts)/len(rtts):.0f}/{max(rtts):.0f} ms (min/avg/max)")

    st = client.server_time()
    drift = st["body"]["serverTime"] - int(time.time() * 1000)
    print(f"[2] server time OK — local clock drift {drift:+d} ms "
          f"({'fine' if abs(drift) < 1000 else 'compensated: signed calls use server time'})")

    filters, rate_limits = get_filters(client, args.symbol)
    print(f"[3] exchange rules for {args.symbol}: tick {filters['tick_size']}, "
          f"lot step {filters['step_size']}, min notional {filters['min_notional']}")
    print("    rate limits advertised by the exchange:")
    for rl in rate_limits:
        print(f"      {rl['rateLimitType']:<15} {rl['limit']:>7} per "
              f"{rl.get('intervalNum', 1)} {rl['interval'].lower()}")

    if client.can_sign:
        acct = client.account()
        if acct["status"] == 200:
            balances = {b["asset"]: b["free"] for b in acct["body"].get("balances", [])}
            print(f"[4] authentication OK — balances: {balances or 'none'}")
        else:
            print(f"[4] AUTH FAILED: HTTP {acct['status']} {acct['body']}")
        book = client.book_ticker(args.symbol)["body"]
        mid = (Decimal(book["bidPrice"]) + Decimal(book["askPrice"])) / 2
        price = round_to(mid * Decimal("0.98"), filters["tick_size"])
        qty = round_to(filters["min_notional"] * 2 / price, filters["step_size"], ROUND_UP)
        r = client.test_order(symbol=args.symbol, side="BUY", type="LIMIT",
                              timeInForce="GTC", quantity=f"{qty.normalize():f}",
                              price=f"{price.normalize():f}")
        verdict = "PASSED" if r["status"] == 200 else f"REJECTED {r['body']}"
        print(f"[5] exchange-validated test order (POST /api/v3/order/test): {verdict}")
    else:
        print("[4] no API keys — auth + test-order steps skipped")
        print("    get free testnet keys: https://testnet.binance.vision (GitHub login)")

    print()
    print(client.meter.report(weight_limit_from(rate_limits)))


# ----------------------------------------------------------------- the runner

class BotRunner:
    def __init__(self, client, symbol, strategies, interval, keep_orders=False):
        self.client = client
        self.symbol = symbol
        self.strategies = strategies                       # prefix -> strategy
        self.interval = interval
        self.keep_orders = keep_orders
        self.filters = None
        self.weight_limit = 6000
        self.tracked = {}       # clientOrderId -> {tag, status, placed_at}
        self.virtual_open = {}  # dry-run resting orders: clientOrderId -> info
        self.seq = 0
        self.candles = None
        self.last_candles_fetch = 0
        self.dry_run = not client.can_sign
        self.placements = self.cancels = self.rejections = 0
        self.pnl = PnLTracker()
        self.last_mid = None

    # -------------------------------------------------------------- utilities

    def strategy_for(self, tag):
        return self.strategies.get(tag.split("-")[0])

    def next_id(self, tag):
        # separator must satisfy Binance's clientOrderId charset [a-zA-Z0-9-_];
        # "_" never appears inside a tag, so tag = id.split("_")[0] round-trips
        self.seq += 1
        return f"{tag}_{self.seq}"

    def round_order(self, order, mid):
        """Round price/qty to exchange filters; returns None if unplaceable."""
        f = self.filters
        params = {"symbol": self.symbol, "side": order["side"], "type": order["type"]}
        if order["type"] == "MARKET":
            if "quote_qty" in order:
                params["quoteOrderQty"] = f"{round_to(order['quote_qty'], Decimal('0.01')).normalize():f}"
            else:
                qty = round_to(order["qty"], f["step_size"], ROUND_DOWN)
                if qty * mid < f["min_notional"]:
                    return None
                params["quantity"] = f"{qty.normalize():f}"
            return params
        price = round_to(order["price"], f["tick_size"])
        if "qty" in order:
            qty = round_to(order["qty"], f["step_size"], ROUND_DOWN)
        else:
            qty = round_to(order["quote_qty"] / price, f["step_size"], ROUND_DOWN)
        if qty * price < f["min_notional"]:
            qty = round_to(f["min_notional"] * Decimal("1.01") / price,
                           f["step_size"], ROUND_UP)
        params["quantity"] = f"{qty.normalize():f}"
        params["price"] = f"{price.normalize():f}"
        if order["type"] == "LIMIT":
            params["timeInForce"] = "GTC"
        return params

    # ------------------------------------------------------------- lifecycle

    def fetch_open_orders(self):
        """Our resting orders by tag. Adopts pre-existing orders with our prefixes."""
        if self.dry_run:
            return {info["tag"]: dict(info, clientOrderId=cid)
                    for cid, info in self.virtual_open.items()}
        resp = self.client.open_orders(self.symbol)
        if resp["status"] != 200:
            print(f"    openOrders failed: {resp['body']}")
            return None
        open_by_tag = {}
        open_ids = set()
        for o in resp["body"]:
            cid = o.get("clientOrderId", "")
            tag = cid.split("_")[0]
            if self.strategy_for(tag) is None:
                continue        # not ours — never touch it
            open_ids.add(cid)
            open_by_tag[tag] = {"tag": tag, "clientOrderId": cid,
                                "price": Decimal(o["price"]), "time": o["time"] / 1000}
            if cid not in self.tracked:
                self.tracked[cid] = {"tag": tag, "status": "resting",
                                     "placed_at": o["time"] / 1000}
        # anything we tracked as resting that is no longer open: filled or canceled?
        for cid, info in list(self.tracked.items()):
            if info["status"] == "resting" and cid not in open_ids:
                r = self.client.get_order(self.symbol, cid)
                status = r["body"].get("status", "UNKNOWN") if r["status"] == 200 else "UNKNOWN"
                info["status"] = status
                if status == "FILLED":
                    body = r["body"]
                    print(f"    FILL {info['tag']} ({body.get('executedQty')} @ "
                          f"{body.get('price')}, binance orderId {body.get('orderId')})")
                    self.pnl.on_fill(body.get("side"),
                                     Decimal(body.get("executedQty", "0")),
                                     Decimal(body.get("cummulativeQuoteQty", "0")))
                    self.strategy_for(info["tag"]).on_fill(info["tag"], body)
        return open_by_tag

    def place(self, order, mid):
        params = self.round_order(order, mid)
        if params is None:
            return
        cid = self.next_id(order["tag"])
        params["newClientOrderId"] = cid
        if self.dry_run:
            print(f"    [dry-run] PLACE {params}")
            self.placements += 1
            if order["type"] == "MARKET":       # simulate immediate fill at mid
                qty = order.get("qty") or (order["quote_qty"] / mid)
                self.pnl.on_fill(order["side"], Decimal(qty), Decimal(qty) * mid)
                self.strategy_for(order["tag"]).on_fill(
                    order["tag"], {"executedQty": f"{qty:f}", "price": f"{mid:f}"})
            else:
                self.virtual_open[cid] = {"tag": order["tag"],
                                          "price": Decimal(params["price"]),
                                          "time": time.time()}
            return
        r = self.client.place_order(**params)
        if r["status"] == 200:
            self.placements += 1
            body = r["body"]
            if body.get("status") == "FILLED":          # market orders fill inline
                fee = sum((Decimal(f.get("commission", "0"))
                           for f in body.get("fills", [])
                           if f.get("commissionAsset") == self.filters["quote_asset"]),
                          Decimal("0"))
                print(f"    FILL {order['tag']} (market, {body.get('executedQty')}, "
                      f"binance orderId {body.get('orderId')})")
                self.pnl.on_fill(body.get("side"),
                                 Decimal(body.get("executedQty", "0")),
                                 Decimal(body.get("cummulativeQuoteQty", "0")), fee)
                self.strategy_for(order["tag"]).on_fill(order["tag"], body)
            else:
                self.tracked[cid] = {"tag": order["tag"], "status": "resting",
                                     "placed_at": time.time()}
                print(f"    PLACED {order['tag']} {params['side']} "
                      f"{params.get('quantity', params.get('quoteOrderQty'))} "
                      f"@ {params.get('price', 'MKT')} "
                      f"(binance orderId {body.get('orderId')})")
        else:
            self.rejections += 1
            print(f"    REJECTED {order['tag']}: {r['body'].get('msg', r['body'])}")

    def cancel(self, open_info):
        cid = open_info["clientOrderId"]
        if self.dry_run:
            print(f"    [dry-run] CANCEL {cid}")
            self.virtual_open.pop(cid, None)
            self.cancels += 1
            return
        r = self.client.cancel_order(self.symbol, cid)
        if r["status"] == 200:
            self.cancels += 1
            if cid in self.tracked:
                self.tracked[cid]["status"] = "CANCELED"
        else:
            # already gone (maybe filled) — reconcile next tick
            print(f"    cancel {cid} failed: {r['body'].get('msg', r['body'])}")

    # ------------------------------------------------------------------ tick

    def tick(self):
        book = self.client.book_ticker(self.symbol)
        if book["status"] != 200:
            print(f"    bookTicker failed: {book['body']}")
            return
        bid, ask = Decimal(book["body"]["bidPrice"]), Decimal(book["body"]["askPrice"])
        mid = (bid + ask) / 2

        if time.time() - self.last_candles_fetch > 60:
            kl = self.client.klines(self.symbol, "3m", limit=100)
            if kl["status"] == 200:
                self.candles = kl["body"]
                self.last_candles_fetch = time.time()

        state = MarketState(mid, bid, ask, self.filters, candles=self.candles)
        open_by_tag = self.fetch_open_orders()
        if open_by_tag is None:
            return

        desired = []
        for strat in self.strategies.values():
            desired.extend(strat.desired_orders(state))
        desired_by_tag = {o["tag"]: o for o in desired}

        # cancel resting orders that are stale, drifted, or no longer desired
        for tag, open_info in open_by_tag.items():
            want = desired_by_tag.get(tag)
            if want is None or want["type"] == "MARKET":
                self.cancel(open_info)
                continue
            tolerance = want.get("tolerance")
            tol_abs = (want["price"] * tolerance if tolerance
                       else self.filters["tick_size"] / 2)
            drifted = abs(round_to(want["price"], self.filters["tick_size"])
                          - open_info["price"]) > tol_abs
            too_old = ("max_age" in want
                       and time.time() - open_info["time"] > want["max_age"])
            if drifted or too_old:
                self.cancel(open_info)
            else:
                desired_by_tag.pop(tag)     # keep it — nothing to do

        # place what's missing (rate-capped)
        placed = 0
        for tag, order in desired_by_tag.items():
            if tag in open_by_tag and order["type"] != "MARKET":
                continue    # just canceled this tick; replace next tick
            if placed >= MAX_PLACEMENTS_PER_TICK:
                print("    placement cap reached this tick; deferring the rest")
                break
            self.place(order, mid)
            placed += 1

        self.last_mid = mid
        st = self.strategies.get("ST")
        meter = self.client.meter
        print(f"[{time.strftime('%H:%M:%S')}] mid={mid.normalize():f} "
              f"open={len(open_by_tag)} placed={self.placements} "
              f"canceled={self.cancels} rejected={self.rejections} | "
              f"{self.pnl.brief(mid)} | "
              f"weight-1m={meter.used_weight_1m} reqs={meter.total_requests}"
              + (f" | ST {st.last_signal}" if st and st.last_signal != 'none' else ""))

    # ------------------------------------------------------------------- run

    def run(self, duration=None, stop_event=None):
        stop_event = stop_event or threading.Event()
        self.filters, rate_limits = get_filters(self.client, self.symbol)
        self.weight_limit = weight_limit_from(rate_limits)
        mode = "DRY-RUN (no keys — signed calls printed, not sent)" if self.dry_run \
            else f"TRADING on {self.client.mode}"
        print(f"{mode} | {self.symbol} | strategies: "
              f"{', '.join(type(s).__name__ for s in self.strategies.values())} | "
              f"tick {self.interval}s"
              + (f" | stopping after {duration:.0f}s" if duration else " | Ctrl+C to stop"))
        print(f"exchange budget: {self.weight_limit} weight/min; "
              f"placement cap {MAX_PLACEMENTS_PER_TICK}/tick\n")
        deadline = time.time() + duration if duration else None
        try:
            while ((deadline is None or time.time() < deadline)
                   and not stop_event.is_set()):
                tick_start = time.time()
                try:
                    self.tick()
                except IPBanError:
                    raise
                except Exception as e:
                    print(f"    tick error (continuing): {type(e).__name__}: {e}")
                sleep_left = self.interval - (time.time() - tick_start)
                if deadline:
                    sleep_left = min(sleep_left, max(0, deadline - time.time()))
                if sleep_left > 0:
                    stop_event.wait(sleep_left)
            if stop_event.is_set():
                print("\nstop requested")
        except KeyboardInterrupt:
            print("\nstopped by user")
        except IPBanError as e:
            print(f"\nFATAL: {e}")
        finally:
            self.shutdown()

    def shutdown(self):
        if not self.keep_orders:
            open_by_tag = self.fetch_open_orders() or {}
            if open_by_tag:
                print(f"cancelling {len(open_by_tag)} open orders...")
                for info in open_by_tag.values():
                    self.cancel(info)
        print()
        for strat in self.strategies.values():
            print(strat.summary())
        print(self.pnl.summary(self.last_mid,
                               base=self.filters.get("base_asset", "base"),
                               quote=self.filters.get("quote_asset", "quote")))
        print(f"session actions: {self.placements} placed, {self.cancels} canceled, "
              f"{self.rejections} rejected")
        print()
        print(self.client.meter.report(self.weight_limit))


# ------------------------------------------------------------------------ cli

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=["testnet", "demo", "live"], default="testnet",
                   help="testnet = testnet.binance.vision; demo = binance.com Demo "
                        "Mode (watch orders live at demo.binance.com); live = "
                        "read-only --check only")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--check", action="store_true",
                   help="connection + request-cost check, then exit")
    p.add_argument("--trade", action="store_true",
                   help="actually place orders (testnet only; default is dry-run)")
    p.add_argument("--strategies", default="grid,pmm,supertrend",
                   help="comma list: grid,pmm,supertrend")
    p.add_argument("--interval", type=float, default=10, help="tick seconds")
    p.add_argument("--duration", type=float, default=None,
                   help="stop after N seconds and print reports")
    p.add_argument("--total-quote", type=Decimal, default=Decimal("200"),
                   help="quote budget per maker strategy")
    p.add_argument("--grid-start", default=None,
                   help="grid lower bound: absolute price or %% offset from the "
                        "startup mid, e.g. 59000 or -3%% (default: -3%%)")
    p.add_argument("--grid-end", default=None,
                   help="grid upper bound: absolute price or %% offset, "
                        "e.g. 64000 or +3%% (default: +3%%)")
    p.add_argument("--grid-limit", default=None,
                   help="stop-trading price: absolute or %% offset, e.g. -5%% "
                        "(default: start - 2%%)")
    p.add_argument("--grid-levels", type=int, default=8, help="number of grid levels")
    p.add_argument("--grid-max-open", type=int, default=3,
                   help="max simultaneously resting grid orders")
    p.add_argument("--keep-orders", action="store_true",
                   help="do not cancel open orders on exit")
    p.add_argument("--credentials-account", default=None)
    p.add_argument("--credentials-base", default="bots")
    args = p.parse_args()

    # let a GUI/parent process stop us gracefully (order cleanup still runs):
    # SIGBREAK is what CTRL_BREAK_EVENT delivers on Windows process groups.
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)

    # managed mode (spawned by api_server/launcher, incl. frozen .exe/.app where
    # console signals don't exist): "STOP" on stdin requests a graceful shutdown.
    stop_event = threading.Event()
    if os.environ.get("GRIDBOT_MANAGED") == "1":
        def _watch_stdin():
            try:
                for line in sys.stdin:
                    if line.strip().upper() == "STOP":
                        break
            except Exception:
                pass
            stop_event.set()        # STOP received, or parent died (EOF)
        threading.Thread(target=_watch_stdin, daemon=True).start()

    api_key, api_secret, source = load_keys(args)
    meter = RequestMeter()
    client = BinanceClient(args.mode, api_key, api_secret, meter)

    if args.check:
        if source:
            print(f"using API keys from {source}\n")
        connection_check(client, args)
        return

    if args.mode == "live":
        sys.exit("refusing to run the trading loop against live Binance — "
                 "use --mode testnet (live is allowed only with --check)")
    if args.trade and not client.can_sign:
        sys.exit("--trade needs API keys: set BINANCE_API_KEY/BINANCE_API_SECRET "
                 "(free keys at https://testnet.binance.vision) or use "
                 "--credentials-account")
    if not args.trade:
        client.api_key = client.api_secret = None       # force dry-run
    elif source:
        print(f"using API keys from {source}")

    chosen = {}
    wanted = {s.strip() for s in args.strategies.split(",") if s.strip()}
    if "grid" in wanted:
        chosen["GS"] = GridStrike(start_price=args.grid_start, end_price=args.grid_end,
                                  limit_price=args.grid_limit, n_levels=args.grid_levels,
                                  total_amount_quote=args.total_quote,
                                  max_open_orders=args.grid_max_open)
    if "pmm" in wanted:
        chosen["PMM"] = PMMSimple(total_amount_quote=args.total_quote)
    if "supertrend" in wanted:
        chosen["ST"] = Supertrend()
    if not chosen:
        sys.exit(f"no valid strategies in '{args.strategies}'")

    BotRunner(client, args.symbol, chosen, args.interval,
              keep_orders=args.keep_orders).run(args.duration, stop_event)


if __name__ == "__main__":
    main()
