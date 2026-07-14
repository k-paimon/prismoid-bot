"""
Gate.com Spot REST client (pure stdlib), API v4 — a drop-in for BinanceClient.

Every public method accepts Binance-style arguments and returns Binance-shaped
bodies (exchangeInfo filters, bookTicker bid/ask, klines rows, order objects
with executedQty/cummulativeQuoteQty/status, ...), so BotRunner, the
backtester, and the dashboard API work unchanged against either exchange.

Two modes, mirroring the Binance client:
  testnet  https://api-testnet.gateapi.io — Gate's demo trading environment;
           watch orders live at https://testnet.gate.com. Keys come from the
           Testnet API Key Management page (up to 20 v4 keys per user; keys
           not bound to an IP are auto-disabled after 90 days).
  live     https://api.gateio.ws — the real exchange (API host kept its
           pre-rebrand name). Public market data only, unless you know
           exactly what you are doing: signed calls here move REAL funds.

Keys: GATE_API_KEY / GATE_API_SECRET (spot trade permission), created in the
matching environment — testnet keys do not work on live and vice versa.

Auth (docs: https://www.gate.com/docs/developers/apiv4/):
  SIGN = HMAC-SHA512(secret, "METHOD\nPATH\nQUERY\nSHA512(body)\nTIMESTAMP")
  headers: KEY, Timestamp (unix seconds), SIGN
"""
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from binance_client import RequestMeter

BASES = {
    "testnet": "https://api-testnet.gateapi.io",   # web UI: testnet.gate.com
    "live": "https://api.gateio.ws",
}
PREFIX = "/api/v4"

# quote assets tried (longest first) when splitting a Binance-style symbol
# like BTCUSDT into Gate's BTC_USDT currency-pair form
QUOTE_ASSETS = ("FDUSD", "USDT", "USDC", "TUSD", "BUSD", "DAI", "EUR", "TRY",
                "BTC", "ETH", "BNB", "USD")

# candle intervals Gate serves natively; 3m (the bot's default indicator
# timeframe) is built by aggregating 1m candles locally
NATIVE_INTERVALS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
                    "1h": 3600, "4h": 14400, "8h": 28800, "1d": 86400}
AGGREGATED = {"3m": ("1m", 3)}

ORDER_STATUS = {"open": "NEW", "closed": "FILLED", "cancelled": "CANCELED"}


def to_pair(symbol):
    """BTCUSDT / BTC_USDT / BTC-USDT -> BTC_USDT."""
    s = symbol.strip().upper().replace("-", "_")
    if "_" in s:
        return s
    for quote in QUOTE_ASSETS:
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[:-len(quote)]}_{quote}"
    raise ValueError(f"cannot split symbol {symbol!r} into base_quote — "
                     f"use the BASE_QUOTE form, e.g. BTC_USDT")


class GateClient:
    """Same surface as BinanceClient: mode 'testnet' (demo) or 'live'."""

    def __init__(self, mode="testnet", api_key=None, api_secret=None, meter=None):
        self.base = BASES[mode]
        self.mode = mode
        self.label = f"Gate.com Spot {mode.upper()}"
        self.api_key = api_key
        self.api_secret = api_secret
        self.meter = meter or RequestMeter()
        self._order_ids = {}    # clientOrderId -> Gate numeric order id

    @property
    def can_sign(self):
        return bool(self.api_key and self.api_secret)

    # ------------------------------------------------------------- transport

    def _request(self, method, path, params=None, body=None, signed=False):
        query = urllib.parse.urlencode(params or {})
        full_path = PREFIX + path
        payload = json.dumps(body).encode() if body is not None else b""
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if signed:
            if not self.can_sign:
                raise RuntimeError(f"signed call {method} {path} requires API keys")
            ts = str(int(time.time()))
            body_hash = hashlib.sha512(payload).hexdigest()
            to_sign = f"{method}\n{full_path}\n{query}\n{body_hash}\n{ts}"
            sign = hmac.new(self.api_secret.encode(), to_sign.encode(),
                            hashlib.sha512).hexdigest()
            headers.update({"KEY": self.api_key, "Timestamp": ts, "SIGN": sign})

        url = f"{self.base}{full_path}" + (f"?{query}" if query else "")
        req = urllib.request.Request(url, data=payload if body is not None else None,
                                     headers=headers, method=method)
        start = time.time()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                elapsed = time.time() - start
                raw = resp.read()
                self.meter.record(method, full_path, resp.status, elapsed,
                                  resp.headers)
                return {"status": resp.status,
                        "body": json.loads(raw) if raw else {}}
        except urllib.error.HTTPError as e:
            elapsed = time.time() - start
            self.meter.record(method, full_path, e.code, elapsed, e.headers)
            try:
                err = json.loads(e.read())
            except Exception:
                err = {}
            if isinstance(err, dict) and "msg" not in err:
                err["msg"] = err.get("message", err.get("label", f"HTTP {e.code}"))
            if e.code == 429:
                self.meter.throttled_429 += 1
                retry_after = int(e.headers.get("Retry-After", "2") or "2")
                print(f"[rate-limit] 429 on {path}; backing off {retry_after}s")
                time.sleep(retry_after)
            return {"status": e.code, "body": err}
        except urllib.error.URLError as e:
            elapsed = time.time() - start
            self.meter.record(method, full_path, 599, elapsed, None)
            return {"status": 599, "body": {"msg": f"network error: {e.reason}"}}

    # ------------------------------------------------------------ public api

    def ping(self):
        r = self.server_time()
        return {"status": r["status"], "body": {}}

    def server_time(self):
        r = self._request("GET", "/spot/time")
        if r["status"] == 200:
            r["body"] = {"serverTime": int(r["body"].get("server_time", 0))}
        return r

    def exchange_info(self, symbol):
        """Binance exchangeInfo shape built from Gate's currency-pair rules."""
        pair = to_pair(symbol)
        r = self._request("GET", f"/spot/currency_pairs/{pair}")
        if r["status"] != 200:
            return r
        p = r["body"]
        tick = f"{10 ** -int(p.get('precision', 2)):.{int(p.get('precision', 2))}f}" \
            if int(p.get("precision", 2)) else "1"
        step_prec = int(p.get("amount_precision", 6))
        step = f"{10 ** -step_prec:.{step_prec}f}" if step_prec else "1"
        return {"status": 200, "body": {
            "symbols": [{
                "symbol": symbol,
                "baseAsset": p.get("base", ""),
                "quoteAsset": p.get("quote", ""),
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": tick},
                    {"filterType": "LOT_SIZE", "stepSize": step},
                    {"filterType": "NOTIONAL",
                     "minNotional": p.get("min_quote_amount") or "1"},
                ],
            }],
            # Gate advertises per-endpoint limits (200 req/10s public,
            # 10 orders/s); expressed here in Binance's shape for the meter
            "rateLimits": [
                {"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE",
                 "intervalNum": 1, "limit": 1200},
                {"rateLimitType": "ORDERS", "interval": "SECOND",
                 "intervalNum": 1, "limit": 10},
            ],
        }}

    def book_ticker(self, symbol):
        # top of the real book WITH sizes — the runner's microprice needs
        # both quantities, which /spot/tickers does not carry
        r = self._request("GET", "/spot/order_book",
                          {"currency_pair": to_pair(symbol), "limit": 1})
        if r["status"] != 200:
            return r
        bids, asks = r["body"].get("bids"), r["body"].get("asks")
        if not bids or not asks:
            return {"status": 599, "body": {"msg": f"empty book for {symbol}"}}
        return {"status": 200, "body": {
            "bidPrice": bids[0][0], "bidQty": bids[0][1],
            "askPrice": asks[0][0], "askQty": asks[0][1],
        }}

    def all_prices(self):
        """Last price for every pair, keyed like Binance (BTC_USDT -> BTCUSDT)."""
        r = self._request("GET", "/spot/tickers")
        if r["status"] != 200:
            return r
        return {"status": 200, "body": [
            {"symbol": t["currency_pair"].replace("_", ""), "price": t.get("last", "0")}
            for t in r["body"]]}

    @staticmethod
    def _to_binance_candle(c):
        # Gate: [t_seconds, quote_vol, close, high, low, open, base_vol, ...]
        base_vol = c[6] if len(c) > 6 else c[1]
        return [int(c[0]) * 1000, c[5], c[3], c[4], c[2], base_vol]

    @staticmethod
    def _aggregate(candles, n):
        """Merge Binance-shaped 1m candles into n-minute buckets."""
        out = []
        span = n * 60_000
        for c in candles:
            bucket = c[0] - c[0] % span
            if out and out[-1][0] == bucket:
                last = out[-1]
                last[2] = f"{max(float(last[2]), float(c[2])):f}"     # high
                last[3] = f"{min(float(last[3]), float(c[3])):f}"     # low
                last[4] = c[4]                                        # close
                last[5] = f"{float(last[5]) + float(c[5]):f}"         # volume
            else:
                out.append([bucket, c[1], c[2], c[3], c[4], c[5]])
        return out

    def _candles(self, pair, interval, params):
        params = dict(params, currency_pair=pair, interval=interval)
        r = self._request("GET", "/spot/candlesticks", params)
        if r["status"] != 200:
            return r
        return {"status": 200,
                "body": [self._to_binance_candle(c) for c in r["body"]]}

    def klines(self, symbol, interval, limit=100):
        pair = to_pair(symbol)
        if interval in AGGREGATED:
            native, n = AGGREGATED[interval]
            r = self._candles(pair, native, {"limit": min(limit * n, 1000)})
            if r["status"] == 200:
                r["body"] = self._aggregate(r["body"], n)[-limit:]
            return r
        return self._candles(pair, interval, {"limit": limit})

    def klines_range(self, symbol, interval, start_ms, limit=1000):
        """Up to `limit` candles from start_ms — the backtester's fetch unit."""
        pair = to_pair(symbol)
        native, n = AGGREGATED.get(interval, (interval, 1))
        step_s = NATIVE_INTERVALS[native]
        want = min(limit * n, 1000)
        frm = start_ms // 1000
        r = self._candles(pair, native,
                          {"from": frm, "to": frm + (want - 1) * step_s})
        if r["status"] == 200 and n > 1:
            r["body"] = self._aggregate(r["body"], n)
        return r

    # ------------------------------------------------------------ signed api

    def account(self):
        r = self._request("GET", "/spot/accounts", signed=True)
        if r["status"] != 200:
            return r
        return {"status": 200, "body": {"balances": [
            {"asset": b["currency"], "free": b.get("available", "0"),
             "locked": b.get("locked", "0")}
            for b in r["body"]
            if float(b.get("available", 0)) or float(b.get("locked", 0))]}}

    @staticmethod
    def _map_order(o):
        """Gate order -> Binance order body (the fields our runner reads)."""
        side = o.get("side", "").upper()
        otype = o.get("type", "limit")
        amount = float(o.get("amount", 0) or 0)
        left = float(o.get("left", 0) or 0)
        filled_quote = o.get("filled_total", "0") or "0"
        if otype == "market" and o.get("side") == "buy":
            # market-buy amounts are denominated in quote; recover base qty
            avg = float(o.get("avg_deal_price", 0) or 0)
            executed = float(filled_quote) / avg if avg else 0.0
        else:
            executed = amount - left
        text = o.get("text", "")
        fills = []
        if o.get("fee") is not None:
            fills = [{"commission": o.get("fee", "0"),
                      "commissionAsset": o.get("fee_currency", "")}]
        return {
            "orderId": o.get("id"),
            "clientOrderId": text[2:] if text.startswith("t-") else text,
            "status": ORDER_STATUS.get(o.get("status", ""), "UNKNOWN"),
            "side": side,
            "price": o.get("price", "0") or "0",
            "origQty": o.get("amount", "0"),
            "executedQty": f"{executed:.12f}".rstrip("0").rstrip(".") or "0",
            "cummulativeQuoteQty": filled_quote,
            "time": int(o.get("create_time_ms",
                              float(o.get("create_time", 0)) * 1000)),
            "fills": fills,
        }

    def open_orders(self, symbol):
        r = self._request("GET", "/spot/orders",
                          {"currency_pair": to_pair(symbol), "status": "open"},
                          signed=True)
        if r["status"] != 200:
            return r
        orders = [self._map_order(o) for o in r["body"]]
        for o in orders:                        # adopt ids for cancel/get later
            if o["clientOrderId"]:
                self._order_ids[o["clientOrderId"]] = o["orderId"]
        return {"status": 200, "body": orders}

    def my_trades(self, symbol, limit=20):
        r = self._request("GET", "/spot/my_trades",
                          {"currency_pair": to_pair(symbol), "limit": limit},
                          signed=True)
        if r["status"] != 200:
            return r
        return {"status": 200, "body": [
            {"id": t.get("id"), "orderId": t.get("order_id"),
             "isBuyer": t.get("side") == "buy", "price": t.get("price", "0"),
             "qty": t.get("amount", "0"),
             "quoteQty": f"{float(t.get('amount', 0)) * float(t.get('price', 0)):.8f}",
             "commission": t.get("fee", "0"),
             "time": int(t.get("create_time_ms",
                               float(t.get("create_time", 0)) * 1000))}
            for t in r["body"]]}

    def _order_ref(self, client_order_id):
        return self._order_ids.get(client_order_id, f"t-{client_order_id}")

    def get_order(self, symbol, client_order_id):
        r = self._request("GET", f"/spot/orders/{self._order_ref(client_order_id)}",
                          {"currency_pair": to_pair(symbol)}, signed=True)
        if r["status"] != 200:
            return r
        return {"status": 200, "body": self._map_order(r["body"])}

    def place_order(self, **params):
        """Binance-style params in, Binance-shaped order body out."""
        symbol = params["symbol"]
        pair = to_pair(symbol)
        side = params["side"].lower()
        otype = params.get("type", "LIMIT")
        cid = params.get("newClientOrderId", "")
        body = {"currency_pair": pair, "side": side, "account": "spot"}
        if cid:
            body["text"] = f"t-{cid}"
        if otype == "MARKET":
            body["type"] = "market"
            body["time_in_force"] = "ioc"
            if side == "buy":
                amount = params.get("quoteOrderQty")
                if amount is None:      # base qty given: convert at the ask
                    book = self.book_ticker(symbol)
                    if book["status"] != 200:
                        return book
                    amount = f"{float(params['quantity']) * float(book['body']['askPrice']):.2f}"
                body["amount"] = str(amount)
            else:
                body["amount"] = str(params["quantity"])
        else:
            body["type"] = "limit"
            # LIMIT_MAKER = post-only, which Gate calls "poc"
            body["time_in_force"] = "poc" if otype == "LIMIT_MAKER" else "gtc"
            body["amount"] = str(params["quantity"])
            body["price"] = str(params["price"])
        r = self._request("POST", "/spot/orders", body=body, signed=True)
        if r["status"] not in (200, 201):
            return r
        order = self._map_order(r["body"])
        if cid:
            self._order_ids[cid] = order["orderId"]
        return {"status": 200, "body": order}

    def test_order(self, **params):
        """Gate has no test-order endpoint; validate locally against the rules."""
        try:
            info = self.exchange_info(params["symbol"])
            if info["status"] != 200:
                return info
            notional = float(params.get("quantity", 0)) * float(params.get("price", 0))
            filters = {f["filterType"]: f for f in info["body"]["symbols"][0]["filters"]}
            min_notional = float(filters["NOTIONAL"]["minNotional"])
            if notional and notional < min_notional:
                return {"status": 400, "body": {
                    "msg": f"order value {notional:.2f} is under Gate's minimum "
                           f"{min_notional}"}}
        except (KeyError, ValueError, TypeError) as e:
            return {"status": 400, "body": {"msg": f"bad order params: {e}"}}
        return {"status": 200, "body": {
            "msg": "validated locally (Gate.com has no test-order endpoint)"}}

    def cancel_order(self, symbol, client_order_id):
        r = self._request("DELETE",
                          f"/spot/orders/{self._order_ref(client_order_id)}",
                          {"currency_pair": to_pair(symbol)}, signed=True)
        if r["status"] != 200:
            return r
        self._order_ids.pop(client_order_id, None)
        return {"status": 200, "body": self._map_order(r["body"])}


# ---------------------------------------------------------------- USDT perps

class GateFuturesClient(GateClient):
    """Gate.com USDT-settled perpetual futures, same Binance-shaped surface.

    Futures quirks the adapter hides from the runner:
      - order sizes are INTEGER CONTRACTS (1 contract = quanto_multiplier of
        the base asset, e.g. 0.0001 BTC); base quantities are converted and
        must round to >= 1 contract
      - there is no side field — positive size is a buy/long, negative a
        sell/short (so pmm_simple's ask side opens shorts instead of being
        limited by spot inventory)
      - market orders are price="0" with tif=ioc
      - candles come back as {t,v,c,h,l,o} objects, not arrays
      - maker fee is a REBATE (-0.01%) on futures; taker pays 0.075%
    Leverage is set on the contract before the first real order (default 1x,
    where margin behaves like the spot budget). At Nx the liquidation
    distance shrinks to roughly 100%/N of the entry price.
    """

    FUT = "/futures/usdt"

    def __init__(self, mode="testnet", api_key=None, api_secret=None, meter=None,
                 leverage=1):
        super().__init__(mode, api_key, api_secret, meter)
        self.label = f"Gate.com Futures {mode.upper()}"
        self.leverage = max(1, int(leverage))
        self._contracts = {}        # pair -> contract meta (multiplier, ...)
        self._leverage_set = set()  # contracts whose leverage is already set

    # ------------------------------------------------------------ market data

    def _contract(self, pair):
        if pair not in self._contracts:
            r = self._request("GET", f"{self.FUT}/contracts/{pair}")
            if r["status"] != 200:
                return None, r
            c = r["body"]
            self._contracts[pair] = {
                "multiplier": float(c.get("quanto_multiplier", "1") or "1"),
                "tick": c.get("order_price_round", "0.1"),
                "min_size": int(c.get("order_size_min", 1)),
            }
        return self._contracts[pair], None

    def exchange_info(self, symbol):
        pair = to_pair(symbol)
        meta, err = self._contract(pair)
        if err:
            return err
        base, _, quote = pair.partition("_")
        mult = meta["multiplier"]
        return {"status": 200, "body": {
            "symbols": [{
                "symbol": symbol, "baseAsset": base, "quoteAsset": quote,
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": meta["tick"]},
                    # one contract is the real lot; any positive min notional
                    # makes the runner round small orders UP to it
                    {"filterType": "LOT_SIZE", "stepSize": f"{mult:.10f}".rstrip("0")},
                    {"filterType": "NOTIONAL", "minNotional": "1"},
                ],
            }],
            "rateLimits": [
                {"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE",
                 "intervalNum": 1, "limit": 1200},
                {"rateLimitType": "ORDERS", "interval": "SECOND",
                 "intervalNum": 1, "limit": 10},
            ],
        }}

    def book_ticker(self, symbol):
        r = self._request("GET", f"{self.FUT}/order_book",
                          {"contract": to_pair(symbol), "limit": 1})
        if r["status"] != 200:
            return r
        bids, asks = r["body"].get("bids"), r["body"].get("asks")
        if not bids or not asks:
            return {"status": 599, "body": {"msg": f"empty book for {symbol}"}}
        return {"status": 200, "body": {
            "bidPrice": bids[0]["p"], "bidQty": str(bids[0].get("s", 0)),
            "askPrice": asks[0]["p"], "askQty": str(asks[0].get("s", 0)),
        }}

    def all_prices(self):
        r = self._request("GET", f"{self.FUT}/tickers")
        if r["status"] != 200:
            return r
        return {"status": 200, "body": [
            {"symbol": t["contract"].replace("_", ""), "price": t.get("last", "0")}
            for t in r["body"]]}

    @staticmethod
    def _to_binance_candle(c):
        # futures candles are objects: {t, v, c, h, l, o, sum}
        return [int(c["t"]) * 1000, c["o"], c["h"], c["l"], c["c"],
                str(c.get("v", 0))]

    def _candles(self, pair, interval, params):
        params = dict(params, contract=pair, interval=interval)
        r = self._request("GET", f"{self.FUT}/candlesticks", params)
        if r["status"] != 200:
            return r
        return {"status": 200,
                "body": [self._to_binance_candle(c) for c in r["body"]]}

    # ------------------------------------------------------------ signed api

    def account(self):
        r = self._request("GET", f"{self.FUT}/accounts", signed=True)
        if r["status"] != 200:
            return r
        a = r["body"]
        total = float(a.get("total", 0) or 0)
        avail = float(a.get("available", 0) or 0)
        return {"status": 200, "body": {"balances": [
            {"asset": a.get("currency", "USDT"), "free": f"{avail:.8f}",
             "locked": f"{max(0.0, total - avail):.8f}"}]}}

    def _map_forder(self, o, mult):
        size = int(o.get("size", 0))
        left = int(o.get("left", 0))
        filled = abs(size) - abs(left)
        avg = float(o.get("fill_price", 0) or 0) or float(o.get("price", 0) or 0)
        status = o.get("status", "")
        if status == "open":
            mapped = "NEW"
        elif status == "finished":
            mapped = "FILLED" if o.get("finish_as") in ("filled", "ioc") and filled \
                else "CANCELED"
        else:
            mapped = "UNKNOWN"
        text = o.get("text", "")
        return {
            "orderId": o.get("id"),
            "clientOrderId": text[2:] if text.startswith("t-") else text,
            "status": mapped,
            "side": "BUY" if size > 0 else "SELL",
            "price": o.get("price", "0") or "0",
            "origQty": f"{abs(size) * mult:.10f}".rstrip("0").rstrip(".") or "0",
            "executedQty": f"{filled * mult:.10f}".rstrip("0").rstrip(".") or "0",
            "cummulativeQuoteQty": f"{filled * mult * avg:.8f}",
            "time": int(float(o.get("create_time", 0)) * 1000),
            "fills": [],
        }

    def open_orders(self, symbol):
        pair = to_pair(symbol)
        meta, err = self._contract(pair)
        if err:
            return err
        r = self._request("GET", f"{self.FUT}/orders",
                          {"contract": pair, "status": "open"}, signed=True)
        if r["status"] != 200:
            return r
        orders = [self._map_forder(o, meta["multiplier"]) for o in r["body"]]
        for o in orders:
            if o["clientOrderId"]:
                self._order_ids[o["clientOrderId"]] = o["orderId"]
        return {"status": 200, "body": orders}

    def my_trades(self, symbol, limit=20):
        pair = to_pair(symbol)
        meta, err = self._contract(pair)
        if err:
            return err
        r = self._request("GET", f"{self.FUT}/my_trades",
                          {"contract": pair, "limit": limit}, signed=True)
        if r["status"] != 200:
            return r
        mult = meta["multiplier"]
        return {"status": 200, "body": [
            {"id": t.get("id"), "orderId": t.get("order_id"),
             "isBuyer": int(t.get("size", 0)) > 0, "price": t.get("price", "0"),
             "qty": f"{abs(int(t.get('size', 0))) * mult:.10f}".rstrip("0").rstrip(".") or "0",
             "quoteQty": f"{abs(int(t.get('size', 0))) * mult * float(t.get('price', 0) or 0):.8f}",
             "commission": "0",
             "time": int(float(t.get("create_time", 0)) * 1000)}
            for t in r["body"]]}

    def get_order(self, symbol, client_order_id):
        pair = to_pair(symbol)
        meta, err = self._contract(pair)
        if err:
            return err
        r = self._request("GET",
                          f"{self.FUT}/orders/{self._order_ref(client_order_id)}",
                          signed=True)
        if r["status"] != 200:
            return r
        return {"status": 200, "body": self._map_forder(r["body"],
                                                        meta["multiplier"])}

    def position(self, symbol):
        """The open position on the contract — the exchange's own view."""
        pair = to_pair(symbol)
        meta, err = self._contract(pair)
        if err:
            return err
        r = self._request("GET", f"{self.FUT}/positions/{pair}", signed=True)
        if r["status"] != 200:
            return r
        p = r["body"][0] if isinstance(r["body"], list) and r["body"] else r["body"]
        size = int(p.get("size", 0) or 0)
        return {"status": 200, "body": {
            "size_base": size * meta["multiplier"],
            "entry_price": p.get("entry_price", "0") or "0",
            "mark_price": p.get("mark_price", "0") or "0",
            "liq_price": p.get("liq_price", "0") or "0",
            "leverage": p.get("leverage", "0") or "0",
            "unrealised_pnl": p.get("unrealised_pnl", "0") or "0",
            "realised_pnl": p.get("realised_pnl", "0") or "0",
        }}

    def account_book(self, limit=500, since=None):
        """Wallet ledger entries: type pnl / fee (rebates negative-fee) /
        fund (funding) / dnw (deposits), newest first."""
        params = {"limit": limit}
        if since:
            params["from"] = int(since)
        return self._request("GET", f"{self.FUT}/account_book", params,
                             signed=True)

    def _ensure_leverage(self, pair):
        if pair in self._leverage_set:
            return
        r = self._request("POST", f"{self.FUT}/positions/{pair}/leverage",
                          {"leverage": str(self.leverage)}, signed=True)
        if r["status"] == 200:
            self._leverage_set.add(pair)
        else:
            print(f"    [gate-futures] could not set {pair} leverage to "
                  f"{self.leverage}x: {r['body'].get('msg', r['body'])}")

    def place_order(self, **params):
        symbol = params["symbol"]
        pair = to_pair(symbol)
        meta, err = self._contract(pair)
        if err:
            return err
        mult = meta["multiplier"]
        side = params["side"].upper()
        otype = params.get("type", "LIMIT")
        cid = params.get("newClientOrderId", "")

        qty = params.get("quantity")
        if qty is None:     # market buy sized in quote: convert at the ask
            book = self.book_ticker(symbol)
            if book["status"] != 200:
                return book
            qty = float(params["quoteOrderQty"]) / float(book["body"]["askPrice"])
        contracts = int(float(qty) / mult)
        if contracts < meta["min_size"]:
            return {"status": 400, "body": {
                "msg": f"order of {qty} base is under 1 contract "
                       f"({mult} base) — raise the order size"}}
        body = {"contract": pair,
                "size": contracts if side == "BUY" else -contracts}
        if cid:
            body["text"] = f"t-{cid}"
        if otype == "MARKET":
            body["price"] = "0"
            body["tif"] = "ioc"
        else:
            body["price"] = str(params["price"])
            body["tif"] = "poc" if otype == "LIMIT_MAKER" else "gtc"
        self._ensure_leverage(pair)
        r = self._request("POST", f"{self.FUT}/orders", body=body, signed=True)
        if r["status"] not in (200, 201):
            return r
        order = self._map_forder(r["body"], mult)
        if cid:
            self._order_ids[cid] = order["orderId"]
        return {"status": 200, "body": order}

    def test_order(self, **params):
        pair = to_pair(params["symbol"])
        meta, err = self._contract(pair)
        if err:
            return err
        try:
            contracts = int(float(params.get("quantity", 0)) / meta["multiplier"])
            if contracts < meta["min_size"]:
                return {"status": 400, "body": {
                    "msg": f"under the 1-contract minimum "
                           f"({meta['multiplier']} base)"}}
        except (ValueError, TypeError) as e:
            return {"status": 400, "body": {"msg": f"bad order params: {e}"}}
        return {"status": 200, "body": {
            "msg": "validated locally (Gate.com has no test-order endpoint)"}}

    def cancel_order(self, symbol, client_order_id):
        pair = to_pair(symbol)
        meta, err = self._contract(pair)
        if err:
            return err
        r = self._request("DELETE",
                          f"{self.FUT}/orders/{self._order_ref(client_order_id)}",
                          signed=True)
        if r["status"] != 200:
            return r
        self._order_ids.pop(client_order_id, None)
        return {"status": 200, "body": self._map_forder(r["body"],
                                                        meta["multiplier"])}
