"""
Binance Spot REST client (pure stdlib) with first-class request accounting.

Every call is recorded by RequestMeter: per-endpoint counts, errors, latency,
a local request-weight estimate, and the rate-limit feedback headers Binance
attaches to responses (X-MBX-USED-WEIGHT-1M, X-MBX-ORDER-COUNT-10S/-1D).
429 responses honour Retry-After; a 418 (IP ban) raises immediately.
"""
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

BASES = {
    "demo": "https://demo-api.binance.com",        # binance.com Demo Mode — web
                                                   # trading UI at demo.binance.com
    "live": "https://api.binance.com",
}

# Local weight estimates per (method, path) — reconciled at runtime against the
# X-MBX-USED-WEIGHT-1M header, which is the exchange's own accounting.
WEIGHTS = {
    ("GET", "/api/v3/ping"): 1,
    ("GET", "/api/v3/time"): 1,
    ("GET", "/api/v3/exchangeInfo"): 20,
    ("GET", "/api/v3/ticker/bookTicker"): 2,
    ("GET", "/api/v3/ticker/price"): 4,       # all symbols
    ("GET", "/api/v3/klines"): 2,
    ("GET", "/api/v3/account"): 20,
    ("GET", "/api/v3/openOrders"): 6,     # with symbol
    ("GET", "/api/v3/myTrades"): 20,
    ("GET", "/api/v3/order"): 4,
    ("POST", "/api/v3/order"): 1,
    ("POST", "/api/v3/order/test"): 1,
    ("DELETE", "/api/v3/order"): 1,
}
ORDER_ENDPOINTS = {("POST", "/api/v3/order"), ("DELETE", "/api/v3/order")}


class IPBanError(RuntimeError):
    """HTTP 418 — Binance has temporarily banned this IP. Stop everything."""


class RequestMeter:
    def __init__(self):
        self.started = time.time()
        self.counts = defaultdict(int)          # (method, path) -> calls
        self.errors = defaultdict(int)          # (method, path) -> non-2xx count
        self.latency_sum = defaultdict(float)   # (method, path) -> seconds
        self.latency_max = defaultdict(float)
        self.local_weight = 0                   # our own running estimate
        self.used_weight_1m = 0                 # last header value seen
        self.peak_used_weight_1m = 0
        self.order_count_10s = 0
        self.peak_order_count_10s = 0
        self.order_count_1d = 0
        self.throttled_429 = 0

    @property
    def total_requests(self):
        return sum(self.counts.values())

    def record(self, method, path, status, elapsed, headers):
        key = (method, path)
        self.counts[key] += 1
        self.latency_sum[key] += elapsed
        self.latency_max[key] = max(self.latency_max[key], elapsed)
        self.local_weight += WEIGHTS.get(key, 1)
        if status >= 400:
            self.errors[key] += 1
        if headers:
            uw = headers.get("x-mbx-used-weight-1m")
            if uw:
                self.used_weight_1m = int(uw)
                self.peak_used_weight_1m = max(self.peak_used_weight_1m, int(uw))
            oc10 = headers.get("x-mbx-order-count-10s")
            if oc10:
                self.order_count_10s = int(oc10)
                self.peak_order_count_10s = max(self.peak_order_count_10s, int(oc10))
            oc1d = headers.get("x-mbx-order-count-1d")
            if oc1d:
                self.order_count_1d = int(oc1d)

    def report(self, weight_limit_1m=6000):
        elapsed_min = max((time.time() - self.started) / 60.0, 1e-9)
        lines = []
        lines.append("=" * 78)
        lines.append("REQUEST REPORT")
        lines.append("-" * 78)
        lines.append(f"{'endpoint':<38}{'calls':>6}{'errors':>7}{'weight':>8}"
                     f"{'avg ms':>8}{'max ms':>8}")
        for key in sorted(self.counts, key=lambda k: -self.counts[k]):
            method, path = key
            n = self.counts[key]
            avg_ms = self.latency_sum[key] / n * 1000
            lines.append(f"{method + ' ' + path:<38}{n:>6}{self.errors[key]:>7}"
                         f"{WEIGHTS.get(key, 1) * n:>8}{avg_ms:>8.0f}"
                         f"{self.latency_max[key] * 1000:>8.0f}")
        lines.append("-" * 78)
        lines.append(f"total requests: {self.total_requests}  "
                     f"({self.total_requests / elapsed_min:.1f}/min over "
                     f"{elapsed_min:.1f} min)")
        lines.append(f"local weight estimate: {self.local_weight} total  "
                     f"({self.local_weight / elapsed_min:.0f}/min)")
        pct = self.peak_used_weight_1m / weight_limit_1m * 100 if weight_limit_1m else 0
        lines.append(f"exchange-reported used weight (1m): last {self.used_weight_1m}, "
                     f"peak {self.peak_used_weight_1m} "
                     f"({pct:.1f}% of the {weight_limit_1m}/min budget)")
        lines.append(f"order count: peak {self.peak_order_count_10s}/10s, "
                     f"{self.order_count_1d} today; 429 throttles: {self.throttled_429}")
        lines.append("=" * 78)
        return "\n".join(lines)


class BinanceClient:
    def __init__(self, mode="demo", api_key=None, api_secret=None, meter=None):
        self.base = BASES[mode]
        self.mode = mode
        self.api_key = api_key
        self.api_secret = api_secret
        self.meter = meter or RequestMeter()
        self._time_offset_ms = None     # server clock minus local clock

    @property
    def can_sign(self):
        return bool(self.api_key and self.api_secret)

    # ------------------------------------------------------------- transport

    def _sync_time(self):
        """Binance rejects signed requests whose timestamp is >1s ahead of its
        clock (-1021), so signed calls use server time, not the local clock."""
        r = self._request("GET", "/api/v3/time")
        if r["status"] == 200:
            self._time_offset_ms = r["body"]["serverTime"] - int(time.time() * 1000)
        elif self._time_offset_ms is None:
            self._time_offset_ms = 0

    def _timestamp(self):
        if self._time_offset_ms is None:
            self._sync_time()
        return int(time.time() * 1000) + self._time_offset_ms

    def _request(self, method, path, params=None, signed=False, _resync=True):
        orig_params = params
        params = dict(params or {})
        headers = {}
        if signed:
            if not self.can_sign:
                raise RuntimeError(f"signed call {method} {path} requires API keys")
            params["timestamp"] = self._timestamp()
            params["recvWindow"] = 10000
        query = urllib.parse.urlencode(params)
        if signed:
            sig = hmac.new(self.api_secret.encode(), query.encode(),
                           hashlib.sha256).hexdigest()
            query += f"&signature={sig}"
        if self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key

        if method == "GET" and query:
            req = urllib.request.Request(f"{self.base}{path}?{query}", headers=headers)
        elif method == "GET":
            req = urllib.request.Request(f"{self.base}{path}", headers=headers)
        else:
            req = urllib.request.Request(f"{self.base}{path}", data=query.encode(),
                                         headers=headers, method=method)

        start = time.time()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                elapsed = time.time() - start
                body = resp.read()
                self.meter.record(method, path, resp.status, elapsed, resp.headers)
                result = {"status": resp.status,
                          "body": json.loads(body) if body else {}}
        except urllib.error.HTTPError as e:
            elapsed = time.time() - start
            self.meter.record(method, path, e.code, elapsed, e.headers)
            try:
                body = json.loads(e.read())
            except Exception:
                body = {}
            if e.code == 418:
                raise IPBanError(f"HTTP 418 from {path}: IP banned — stopping. {body}")
            if e.code == 429:
                self.meter.throttled_429 += 1
                retry_after = int(e.headers.get("Retry-After", "5") or "5")
                print(f"[rate-limit] 429 on {path}; backing off {retry_after}s")
                time.sleep(retry_after)
            result = {"status": e.code, "body": body}
        except urllib.error.URLError as e:
            elapsed = time.time() - start
            self.meter.record(method, path, 599, elapsed, None)
            result = {"status": 599, "body": {"msg": f"network error: {e.reason}"}}

        # -1021 = timestamp out of sync with the server; resync once and retry
        if (signed and _resync and isinstance(result["body"], dict)
                and result["body"].get("code") == -1021):
            self._sync_time()
            return self._request(method, path, orig_params, signed, _resync=False)
        return result

    # ------------------------------------------------------------ public api

    def ping(self):
        return self._request("GET", "/api/v3/ping")

    def server_time(self):
        return self._request("GET", "/api/v3/time")

    def exchange_info(self, symbol):
        return self._request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})

    def book_ticker(self, symbol):
        return self._request("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol})

    def all_prices(self):
        """Last price for every symbol — used to value balances in USD."""
        return self._request("GET", "/api/v3/ticker/price")

    def klines(self, symbol, interval, limit=100):
        return self._request("GET", "/api/v3/klines",
                             {"symbol": symbol, "interval": interval, "limit": limit})

    # ------------------------------------------------------------ signed api

    def account(self):
        return self._request("GET", "/api/v3/account",
                             {"omitZeroBalances": "true"}, signed=True)

    def open_orders(self, symbol):
        return self._request("GET", "/api/v3/openOrders", {"symbol": symbol},
                             signed=True)

    def my_trades(self, symbol, limit=20):
        return self._request("GET", "/api/v3/myTrades",
                             {"symbol": symbol, "limit": limit}, signed=True)

    def get_order(self, symbol, client_order_id):
        return self._request("GET", "/api/v3/order",
                             {"symbol": symbol, "origClientOrderId": client_order_id},
                             signed=True)

    def place_order(self, **params):
        return self._request("POST", "/api/v3/order", params, signed=True)

    def test_order(self, **params):
        return self._request("POST", "/api/v3/order/test", params, signed=True)

    def cancel_order(self, symbol, client_order_id):
        return self._request("DELETE", "/api/v3/order",
                             {"symbol": symbol, "origClientOrderId": client_order_id},
                             signed=True)
