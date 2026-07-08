"""
Exchange connection proof-of-concept: derive one representative order from each
strategy (grid_strike, pmm_simple, supertrend_v1) using live market data, then
submit it to Binance's official TEST endpoint — POST /api/v3/order/test — which
runs full validation (signature, filters, balance checks on testnet) without
ever executing a trade.

Modes:
  --mode testnet  (default) Binance Spot Testnet, https://testnet.binance.vision
                  Free sandbox keys: log in with GitHub at https://testnet.binance.vision
                  and click "Generate HMAC-SHA-256 Key".
  --mode live     https://api.binance.com — still ONLY calls /order/test (validate,
                  never executes) plus read-only account info.

Credentials (either source):
  1. Environment:  BINANCE_API_KEY / BINANCE_API_SECRET
  2. bare-features credential store:  --credentials-account <name> with
     CONFIG_PASSWORD env set (decrypts credentials/<name>/connectors/binance.yml)

Pure standard library (the supertrend indicator is implemented inline), so it
runs with plain `py exchange_test_trade.py` on the host — no hummingbot needed.
Without keys it still builds and prints every order, and marks the signed calls
as SKIPPED.
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

BASES = {
    "testnet": "https://testnet.binance.vision",
    "live": "https://api.binance.com",
}


# ---------------------------------------------------------------- http layer

def http_get(base: str, path: str, params: dict = None, headers: dict = None):
    qs = f"?{urllib.parse.urlencode(params)}" if params else ""
    req = urllib.request.Request(base + path + qs, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


class BinanceClient:
    def __init__(self, base: str, api_key: str = None, api_secret: str = None):
        self.base = base
        self.api_key = api_key
        self.api_secret = api_secret

    @property
    def can_sign(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _signed_request(self, method: str, path: str, params: dict):
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 10000
        query = urllib.parse.urlencode(params)
        signature = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        query += f"&signature={signature}"
        headers = {"X-MBX-APIKEY": self.api_key}
        if method == "GET":
            req = urllib.request.Request(f"{self.base}{path}?{query}", headers=headers)
        else:
            req = urllib.request.Request(f"{self.base}{path}", data=query.encode(),
                                         headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
                return {"http_status": resp.status, "body": json.loads(body) if body else {}}
        except urllib.error.HTTPError as e:
            return {"http_status": e.code, "body": json.loads(e.read())}

    def account(self):
        return self._signed_request("GET", "/api/v3/account", {"omitZeroBalances": "true"})

    def test_order(self, **params):
        return self._signed_request("POST", "/api/v3/order/test", params)


# ------------------------------------------------------------ exchange rules

def get_symbol_filters(base: str, symbol: str) -> dict:
    info = http_get(base, "/api/v3/exchangeInfo", {"symbol": symbol})
    filters = {f["filterType"]: f for f in info["symbols"][0]["filters"]}
    return {
        "tick_size": Decimal(filters["PRICE_FILTER"]["tickSize"]),
        "step_size": Decimal(filters["LOT_SIZE"]["stepSize"]),
        "min_notional": Decimal(filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {"minNotional": "10"}))["minNotional"]),
    }


def round_to(value: Decimal, increment: Decimal, mode=ROUND_HALF_UP) -> Decimal:
    return (value / increment).quantize(Decimal("1"), rounding=mode) * increment


# ---------------------------------------------------- pure-python supertrend

def supertrend(highs, lows, closes, length=20, multiplier=4.0):
    """Standard supertrend (Wilder ATR); returns (trend_value, direction) for the last bar."""
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    atr = sum(trs[:length]) / length
    upper = lower = None
    direction = 1
    trend = None
    for i in range(length, len(closes)):
        atr = (atr * (length - 1) + trs[i - 1]) / length  # Wilder smoothing
        hl2 = (highs[i] + lows[i]) / 2
        basic_upper = hl2 + multiplier * atr
        basic_lower = hl2 - multiplier * atr
        upper = basic_upper if upper is None or basic_upper < upper or closes[i - 1] > upper else upper
        lower = basic_lower if lower is None or basic_lower > lower or closes[i - 1] < lower else lower
        if closes[i] > upper:
            direction = 1
        elif closes[i] < lower:
            direction = -1
        trend = lower if direction == 1 else upper
    return trend, direction


# ------------------------------------------------- strategy order derivation

def derive_orders(base: str, symbol: str, notional: Decimal) -> list:
    """Build one or two representative orders per strategy from live market data."""
    book = http_get(base, "/api/v3/ticker/bookTicker", {"symbol": symbol})
    mid = (Decimal(book["bidPrice"]) + Decimal(book["askPrice"])) / 2
    f = get_symbol_filters(base, symbol)
    notional = max(notional, f["min_notional"] * 2)

    def limit_order(side: str, price: Decimal, tag: str):
        price = round_to(price, f["tick_size"])
        qty = round_to(notional / price, f["step_size"], ROUND_DOWN)
        return {"strategy": tag, "params": {
            "symbol": symbol, "side": side, "type": "LIMIT", "timeInForce": "GTC",
            "quantity": f"{qty.normalize():f}", "price": f"{price.normalize():f}",
        }}

    orders = []

    # grid_strike: grid between 0.97*mid and 1.03*mid; submit the first grid buy
    # level below the mid price (the order the GridExecutor would rest first).
    start_price, end_price = mid * Decimal("0.97"), mid * Decimal("1.03")
    n_levels = 8
    grid_step = (end_price - start_price) / n_levels
    level_below_mid = start_price + grid_step * int((mid - start_price) / grid_step)
    orders.append(limit_order("BUY", level_below_mid, "grid_strike (grid level below mid)"))

    # pmm_simple: symmetric maker quotes at +/- 0.1% around mid (first spread level).
    orders.append(limit_order("BUY", mid * Decimal("0.999"), "pmm_simple (bid @ -0.1%)"))
    orders.append(limit_order("SELL", mid * Decimal("1.001"), "pmm_simple (ask @ +0.1%)"))

    # supertrend_v1: compute the indicator on live 3m candles; trade with the trend.
    klines = http_get(base, "/api/v3/klines", {"symbol": symbol, "interval": "3m", "limit": 100})
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]
    trend_value, direction = supertrend(highs, lows, closes, length=20, multiplier=4.0)
    distance = abs(closes[-1] - trend_value) / closes[-1]
    side = "BUY" if direction == 1 else "SELL"
    signal_active = distance < 0.01
    orders.append({
        "strategy": (f"supertrend_v1 ({'signal ACTIVE' if signal_active else 'signal idle'}: "
                     f"direction {'LONG' if direction == 1 else 'SHORT'}, "
                     f"distance {distance:.4%} vs 1% threshold)"),
        "params": {"symbol": symbol, "side": side, "type": "MARKET",
                   "quoteOrderQty": f"{round_to(notional, Decimal('0.01')).normalize():f}"},
    })
    return orders


# ----------------------------------------------------------------- key loading

def load_keys(args):
    key, secret = os.environ.get("BINANCE_API_KEY"), os.environ.get("BINANCE_API_SECRET")
    if key and secret:
        return key, secret, "environment variables"
    if args.credentials_account:
        password = os.environ.get("CONFIG_PASSWORD")
        if not password:
            sys.exit("CONFIG_PASSWORD env var required to decrypt the credential store")
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                        "credentials"))
        from credential_manager import CredentialManager
        manager = CredentialManager(config_password=password, base_path=args.credentials_base)
        keys = manager.get_decrypted_keys(args.credentials_account, "binance")
        return keys["binance_api_key"], keys["binance_api_secret"], \
            f"credential store (account: {args.credentials_account})"
    return None, None, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["testnet", "live"], default="testnet")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--notional", type=Decimal, default=Decimal("25"), help="order size in quote")
    parser.add_argument("--credentials-account", default=None,
                        help="decrypt binance keys from the bare-features credential store")
    parser.add_argument("--credentials-base", default="bots", help="credential store base path")
    args = parser.parse_args()

    base = BASES[args.mode]
    print(f"Exchange: Binance Spot {args.mode.upper()} ({base})")

    server_time = http_get(base, "/api/v3/time")["serverTime"]
    print(f"[1] connectivity OK — exchange server time {server_time}")

    orders = derive_orders(base, args.symbol, args.notional)
    print(f"[2] derived {len(orders)} orders from live {args.symbol} market data\n")

    api_key, api_secret, source = load_keys(args)
    client = BinanceClient(base, api_key, api_secret)

    if client.can_sign:
        print(f"[3] using API keys from {source}")
        acct = client.account()
        if acct["http_status"] == 200:
            balances = {b["asset"]: b["free"] for b in acct["body"].get("balances", [])}
            print(f"[4] authenticated — account reachable, balances: {balances or 'none'}\n")
        else:
            print(f"[4] WARNING account call failed: {acct['body']}\n")
    else:
        print("[3] no API keys found — orders will be printed but signed test calls SKIPPED")
        print("    get free sandbox keys: https://testnet.binance.vision (GitHub login)")
        print("    then: $env:BINANCE_API_KEY='...'; $env:BINANCE_API_SECRET='...'\n")

    passed = failed = skipped = 0
    for order in orders:
        print(f"--- {order['strategy']}")
        print(f"    {order['params']}")
        if not client.can_sign:
            print("    -> SKIPPED (no keys)\n")
            skipped += 1
            continue
        result = client.test_order(**order["params"])
        if result["http_status"] == 200:
            print("    -> TEST ORDER VALIDATED by exchange (HTTP 200, not executed)\n")
            passed += 1
        else:
            print(f"    -> REJECTED {result['http_status']}: {result['body']}\n")
            failed += 1

    print("=" * 60)
    print(f"RESULT: {passed} validated, {failed} rejected, {skipped} skipped (of {len(orders)})")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
