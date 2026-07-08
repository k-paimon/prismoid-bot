"""
Futures trader for CJ's compound strategy — Binance USDT-M Futures TESTNET.

The system: open a LONG with (capital x leverage), exit when the trade's PnL
on capital hits +target (price move = target / leverage), optional stop loss,
then immediately re-enter with the grown capital. Exits live on the exchange
as TAKE_PROFIT_MARKET / STOP_MARKET closePosition orders, so they trigger
even if this process dies.

  py futures.py --check                                  # connectivity (keyless ok)
  py futures.py --leverage 3 "--target=3%" --capital 200 # dry-run (simulated fills)
  py futures.py --leverage 3 "--target=3%" --trade       # real testnet position

Keys: BINANCE_API_KEY / BINANCE_API_SECRET must be FUTURES TESTNET keys —
register at https://testnet.binancefuture.com (or Mock Trading on binance.com)
and copy the key from the API Key tab there. Spot demo keys will NOT work.
Watch positions live in the testnet's own trading UI.

Trading is testnet-only; the live futures API is refused.
"""
import argparse
import json
import os
import signal
import sys
import threading
import time
from decimal import Decimal, ROUND_DOWN

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from binance_client import BASES, WEIGHTS, BinanceClient, IPBanError, RequestMeter  # noqa: E402
from strategies import EntryFilter  # noqa: E402

BASES["futures-testnet"] = "https://testnet.binancefuture.com"
BASES["futures-live"] = "https://fapi.binance.com"
WEIGHTS.update({
    ("GET", "/fapi/v1/ping"): 1,
    ("GET", "/fapi/v1/time"): 1,
    ("GET", "/fapi/v1/exchangeInfo"): 1,
    ("GET", "/fapi/v1/ticker/bookTicker"): 2,
    ("GET", "/fapi/v1/klines"): 5,
    ("GET", "/fapi/v2/balance"): 5,
    ("GET", "/fapi/v2/positionRisk"): 5,
    ("POST", "/fapi/v1/leverage"): 1,
    ("POST", "/fapi/v1/order"): 1,
    ("GET", "/fapi/v1/openOrders"): 1,
    ("DELETE", "/fapi/v1/allOpenOrders"): 1,
    ("POST", "/fapi/v1/algoOrder"): 1,
    ("DELETE", "/fapi/v1/algoOrder"): 1,
})


def round_to(value, increment, mode=ROUND_DOWN):
    return (value / increment).quantize(Decimal("1"), rounding=mode) * increment


class FuturesClient(BinanceClient):
    def _sync_time(self):
        r = self._request("GET", "/fapi/v1/time")
        if r["status"] == 200:
            self._time_offset_ms = r["body"]["serverTime"] - int(time.time() * 1000)
        elif self._time_offset_ms is None:
            self._time_offset_ms = 0

    def ping(self):
        return self._request("GET", "/fapi/v1/ping")

    def server_time(self):
        return self._request("GET", "/fapi/v1/time")

    def exchange_info(self):
        return self._request("GET", "/fapi/v1/exchangeInfo")

    def book_ticker(self, symbol):
        return self._request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})

    def klines(self, symbol, interval, limit=100):
        return self._request("GET", "/fapi/v1/klines",
                             {"symbol": symbol, "interval": interval, "limit": limit})

    def balance(self):
        return self._request("GET", "/fapi/v2/balance", signed=True)

    def position(self, symbol):
        return self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol},
                             signed=True)

    def set_leverage(self, symbol, leverage):
        return self._request("POST", "/fapi/v1/leverage",
                             {"symbol": symbol, "leverage": leverage}, signed=True)

    def place_order(self, **params):
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def cancel_all(self, symbol):
        return self._request("DELETE", "/fapi/v1/allOpenOrders",
                             {"symbol": symbol}, signed=True)

    # conditional (TP/SL) orders live on the Algo Order service since 2025-12
    def place_algo_order(self, **params):
        params.setdefault("algoType", "CONDITIONAL")
        return self._request("POST", "/fapi/v1/algoOrder", params, signed=True)

    def cancel_algo_order(self, algo_id):
        return self._request("DELETE", "/fapi/v1/algoOrder",
                             {"algoId": algo_id}, signed=True)


def get_futures_filters(client, symbol):
    info = client.exchange_info()
    if info["status"] != 200:
        sys.exit(f"exchangeInfo failed: {info['body']}")
    sym = next((s for s in info["body"]["symbols"] if s["symbol"] == symbol), None)
    if sym is None:
        sys.exit(f"symbol {symbol} not on the futures exchange")
    filters = {f["filterType"]: f for f in sym["filters"]}
    return {
        "tick_size": Decimal(filters["PRICE_FILTER"]["tickSize"]),
        "step_size": Decimal(filters["LOT_SIZE"]["stepSize"]),
        "min_notional": Decimal(filters.get("MIN_NOTIONAL", {"notional": "100"})
                                .get("notional", "100")),
        "base_asset": sym.get("baseAsset", ""),
        "quote_asset": sym.get("quoteAsset", "USDT"),
    }


def usdt_wallet(client):
    r = client.balance()
    if r["status"] != 200:
        return None
    for b in r["body"]:
        if b.get("asset") == "USDT":
            return Decimal(b.get("crossWalletBalance", b.get("balance", "0")))
    return Decimal("0")


class CompoundFuturesTrader:
    def __init__(self, client, symbol, leverage, target, stop, capital, interval,
                 entry_filter=None):
        self.client = client
        self.symbol = symbol
        self.leverage = leverage
        self.target = target                    # PnL-on-capital fraction, e.g. 0.03
        self.stop = stop                        # same units, or None
        self.start_capital = capital
        self.capital = capital
        self.interval = interval
        self.dry_run = not client.can_sign
        self.filters = None
        self.entry_filter = entry_filter or EntryFilter("always")
        self.candles = None
        self.last_candles_fetch = 0
        self.filter_reason = ""
        # per-trade state
        self.position_qty = Decimal("0")
        self.entry_price = None
        self.tp_price = self.sl_price = None
        self.wallet_before = None
        self.wins = self.losses = 0
        self.seq = 0
        self.last_unrealized = Decimal("0")
        self.algo_ids = []      # exchange-side TP/SL orders of the open trade

    # price move needed on the CONTRACT for a capital-relative target
    def price_from_roe(self, roe, direction):
        return self.entry_price * (1 + direction * roe / self.leverage)

    def emit_stats(self, mid):
        realized = self.capital - self.start_capital
        unreal = self.last_unrealized
        print("@STATS " + json.dumps({
            "realized": float(realized), "unrealized": float(unreal), "fees": 0.0,
            "total": float(realized + unreal), "quote": "USDT",
            "base": self.filters.get("base_asset", ""), "mid": float(mid),
            "open_orders": 1 if self.position_qty else 0,
            "fills": self.wins + self.losses,
            "by_strategy": {"cj_compound_futures": float(realized + unreal)},
        }), flush=True)

    # ------------------------------------------------------------------ entry

    def enter(self, mid):
        qty = round_to(self.capital * self.leverage / mid, self.filters["step_size"])
        if qty * mid < self.filters["min_notional"]:
            sys.exit(f"position {qty * mid:.2f} USDT is under the futures minimum "
                     f"({self.filters['min_notional']}) — raise --capital or --leverage")
        self.seq += 1
        if self.dry_run:
            self.entry_price = mid
            self.position_qty = qty
        else:
            self.wallet_before = usdt_wallet(self.client)
            r = self.client.place_order(symbol=self.symbol, side="BUY", type="MARKET",
                                        quantity=f"{qty.normalize():f}",
                                        newOrderRespType="RESULT",
                                        newClientOrderId=f"CJF-IN_{self.seq}")
            if r["status"] != 200:
                print(f"    entry REJECTED: {r['body'].get('msg', r['body'])}")
                return
            body = r["body"]
            self.position_qty = Decimal(body.get("executedQty", "0"))
            self.entry_price = Decimal(body.get("avgPrice", "0") or "0") or mid
        self.tp_price = round_to(self.price_from_roe(self.target, +1),
                                 self.filters["tick_size"])
        self.sl_price = (round_to(self.price_from_roe(self.stop, -1),
                                  self.filters["tick_size"])
                         if self.stop is not None else None)
        print(f"    ENTER [cj_compound] LONG {self.position_qty.normalize():f} "
              f"@ {self.entry_price:.2f} (capital {self.capital:.2f} x{self.leverage}) "
              f"TP {self.tp_price} SL {self.sl_price or 'none'} "
              f"liq ~{self.entry_price * (1 - 1 / Decimal(self.leverage)):.0f}")
        if not self.dry_run:
            self.algo_ids = []
            tp = self.client.place_algo_order(
                symbol=self.symbol, side="SELL", type="TAKE_PROFIT_MARKET",
                triggerPrice=f"{self.tp_price.normalize():f}",
                closePosition="true", clientAlgoId=f"CJF-TP_{self.seq}")
            if tp["status"] == 200:
                self.algo_ids.append(tp["body"].get("algoId"))
            else:
                print(f"    TP order REJECTED: {tp['body'].get('msg', tp['body'])}")
            if self.sl_price is not None:
                sl = self.client.place_algo_order(
                    symbol=self.symbol, side="SELL", type="STOP_MARKET",
                    triggerPrice=f"{self.sl_price.normalize():f}",
                    closePosition="true", clientAlgoId=f"CJF-SL_{self.seq}")
                if sl["status"] == 200:
                    self.algo_ids.append(sl["body"].get("algoId"))
                else:
                    print(f"    SL order REJECTED: {sl['body'].get('msg', sl['body'])}")

    # ------------------------------------------------------------------- exit

    def settle(self, exit_price, how):
        if self.dry_run:
            pnl = (exit_price - self.entry_price) * self.position_qty
        else:
            wallet_now = usdt_wallet(self.client)
            pnl = (wallet_now - self.wallet_before
                   if wallet_now is not None and self.wallet_before is not None
                   else Decimal("0"))
            self.cancel_algos()                      # drop the surviving TP or SL
        self.capital += pnl
        if pnl > 0:
            self.wins += 1
        else:
            self.losses += 1
        print(f"    EXIT [cj_compound] {how} @ ~{exit_price:.2f}: "
              f"{pnl:+.2f} USDT -> capital {self.capital:.2f} "
              f"({(self.capital / self.start_capital - 1) * 100:+.2f}% total)")
        self.position_qty = Decimal("0")
        self.entry_price = None
        self.last_unrealized = Decimal("0")
        if self.capital <= 0:
            sys.exit("capital wiped out — stopping")

    def cancel_algos(self):
        for algo_id in self.algo_ids:
            if algo_id is not None:
                self.client.cancel_algo_order(algo_id)   # already-fired ids just 4xx
        self.algo_ids = []

    # ------------------------------------------------------------------- tick

    def tick(self):
        book = self.client.book_ticker(self.symbol)
        if book["status"] != 200:
            print(f"    bookTicker failed: {book['body']}")
            return
        mid = (Decimal(book["body"]["bidPrice"])
               + Decimal(book["body"]["askPrice"])) / 2

        if self.position_qty == 0:
            if self.entry_filter.mode != "always":
                if time.time() - self.last_candles_fetch > 60:
                    kl = self.client.klines(self.symbol, "3m", limit=100)
                    if kl["status"] == 200:
                        self.candles = kl["body"]
                        self.last_candles_fetch = time.time()
                allowed, self.filter_reason = self.entry_filter.ok(
                    self.candles, float(mid))
                if allowed:
                    self.enter(mid)
            else:
                self.enter(mid)
        elif self.dry_run:
            self.last_unrealized = (mid - self.entry_price) * self.position_qty
            if mid >= self.tp_price:
                self.settle(self.tp_price, "TAKE-PROFIT hit")
            elif self.sl_price is not None and mid <= self.sl_price:
                self.settle(self.sl_price, "STOP hit")
        else:
            pos = self.client.position(self.symbol)
            if pos["status"] == 200 and pos["body"]:
                p = pos["body"][0]
                amt = Decimal(p.get("positionAmt", "0"))
                self.last_unrealized = Decimal(p.get("unRealizedProfit", "0"))
                if amt == 0:        # a closePosition order (or liquidation) fired
                    self.settle(mid, "position closed on exchange")

        meter = self.client.meter
        state = (f"holding {self.position_qty.normalize():f} @ {self.entry_price:.2f} "
                 f"uPnL {self.last_unrealized:+.2f}" if self.position_qty
                 else "flat" + (f" [{self.filter_reason}]" if self.filter_reason else ""))
        print(f"[{time.strftime('%H:%M:%S')}] mid={mid.normalize():f} | {state} | "
              f"capital {self.capital:.2f} ({self.wins}W/{self.losses}L) | "
              f"weight-1m={meter.used_weight_1m} reqs={meter.total_requests}")
        self.emit_stats(mid)

    def run(self, duration, stop_event):
        self.filters = get_futures_filters(self.client, self.symbol)
        move = self.target / self.leverage * 100
        mode = ("DRY-RUN (no keys — fills simulated at the mark)" if self.dry_run
                else "TRADING on the FUTURES TESTNET")
        print(f"{mode} | {self.symbol} x{self.leverage} | target +{self.target * 100:.1f}% "
              f"on capital = {move:.2f}% price move | stop "
              f"{f'{self.stop * 100:.1f}%' if self.stop is not None else 'NONE'} | "
              f"capital {self.capital:.2f} USDT, always all-in")
        # entry (taker) + TP/SL trigger (taker) fees are charged on the NOTIONAL,
        # so as a share of capital they scale with leverage
        taker = Decimal("0.0005")
        fee_of_capital = 2 * taker * self.leverage * 100
        print(f"round-trip taker fees ~{fee_of_capital:.2f}% of capital per trade "
              f"vs the +{self.target * 100:.1f}% target"
              + ("  << WARNING: fees ALONE exceed the target — every 'win' loses; "
                 "lower the leverage or raise the target"
                 if fee_of_capital >= self.target * 100 else ""))
        if self.stop is None:
            print(f"WARNING: no stop at x{self.leverage} — liquidation sits only "
                  f"~{100 / self.leverage:.1f}% below entry; one dip that size "
                  f"ends the account\n")
        else:
            print()
        if not self.dry_run:
            r = self.client.set_leverage(self.symbol, self.leverage)
            if r["status"] != 200:
                sys.exit(f"failed to set leverage: {r['body']}")
        deadline = time.time() + duration if duration else None
        try:
            while ((deadline is None or time.time() < deadline)
                   and not stop_event.is_set()):
                start = time.time()
                try:
                    self.tick()
                except IPBanError:
                    raise
                except Exception as e:
                    print(f"    tick error (continuing): {type(e).__name__}: {e}")
                left = self.interval - (time.time() - start)
                if deadline:
                    left = min(left, max(0, deadline - time.time()))
                if left > 0:
                    stop_event.wait(left)
        except KeyboardInterrupt:
            print("\nstopped by user")
        finally:
            self.shutdown()

    def shutdown(self):
        if not self.dry_run and self.position_qty:
            print("closing the open position and cancelling exit orders...")
            self.client.place_order(symbol=self.symbol, side="SELL", type="MARKET",
                                    quantity=f"{self.position_qty.normalize():f}",
                                    reduceOnly="true", newOrderRespType="RESULT")
            self.cancel_algos()
            self.client.cancel_all(self.symbol)
            wallet_now = usdt_wallet(self.client)
            if wallet_now is not None and self.wallet_before is not None:
                self.capital = self.start_capital + (self.capital - self.start_capital) \
                    + (wallet_now - self.wallet_before)
        growth = (self.capital / self.start_capital - 1) * 100
        print(f"\ncj_compound futures: {self.wins} wins, {self.losses} losses; "
              f"capital {self.start_capital:.2f} -> {self.capital:.2f} ({growth:+.2f}%)")
        print()
        print(self.client.meter.report(2400))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--leverage", type=int, default=3)
    p.add_argument("--target", default="3%", help="take-profit as PnL on capital")
    p.add_argument("--stop", default=None, help="stop loss as PnL on capital, "
                                                "e.g. 3%% (default: none)")
    p.add_argument("--capital", type=Decimal, default=Decimal("200"),
                   help="starting capital in USDT (always all-in)")
    p.add_argument("--interval", type=float, default=5, help="tick seconds")
    p.add_argument("--duration", type=float, default=None)
    p.add_argument("--entry", default="always", choices=EntryFilter.MODES,
                   help="entry gate: always / trend / fvg / trend+fvg")
    p.add_argument("--trade", action="store_true",
                   help="place real futures-testnet orders (default: dry-run)")
    p.add_argument("--check", action="store_true")
    args = p.parse_args()

    def pct(tok):
        tok = str(tok).strip()
        return Decimal(tok[:-1]) / 100 if tok.endswith("%") else Decimal(tok)

    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    stop_event = threading.Event()
    if os.environ.get("GRIDBOT_MANAGED") == "1":
        def _watch():
            try:
                for line in sys.stdin:
                    if line.strip().upper() == "STOP":
                        break
            except Exception:
                pass
            stop_event.set()
        threading.Thread(target=_watch, daemon=True).start()

    key = os.environ.get("BINANCE_API_KEY")
    secret = os.environ.get("BINANCE_API_SECRET")
    client = FuturesClient("futures-testnet", key, secret, RequestMeter())

    if args.check:
        print(f"FUTURES TESTNET CHECK ({client.base})")
        r = client.ping()
        print(f"[1] ping: HTTP {r['status']}")
        st = client.server_time()
        drift = st["body"]["serverTime"] - int(time.time() * 1000)
        print(f"[2] server time OK — drift {drift:+d} ms (compensated when signing)")
        f = get_futures_filters(client, args.symbol)
        print(f"[3] {args.symbol}: tick {f['tick_size']}, step {f['step_size']}, "
              f"min notional {f['min_notional']} USDT")
        if client.can_sign:
            w = usdt_wallet(client)
            print(f"[4] auth OK — USDT wallet: {w}" if w is not None
                  else "[4] AUTH FAILED — are these futures-testnet keys?")
        else:
            print("[4] no keys — register at https://testnet.binancefuture.com")
        print()
        print(client.meter.report(2400))
        return

    if args.trade and not client.can_sign:
        sys.exit("--trade needs FUTURES TESTNET keys in BINANCE_API_KEY/"
                 "BINANCE_API_SECRET (register at https://testnet.binancefuture.com)")
    if not args.trade:
        client.api_key = client.api_secret = None

    trader = CompoundFuturesTrader(client, args.symbol, args.leverage,
                                   pct(args.target),
                                   pct(args.stop) if args.stop else None,
                                   args.capital, args.interval,
                                   entry_filter=EntryFilter(args.entry))
    trader.run(args.duration, stop_event)


if __name__ == "__main__":
    main()
