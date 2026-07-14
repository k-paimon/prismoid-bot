"""
Bot backend API (pure stdlib) — the service the web dashboard talks to.

Wraps bot.py as a small JSON HTTP API on http://localhost:8801:

  GET    /health           -> {ok, bot_running}
  GET    /api/status       -> {running, mode, pid, uptime_s, log_count}
  GET    /api/logs?since=N -> {next, lines: [...]}   (incremental console feed)
  GET    /api/credentials  -> {set, api_key_masked}
  POST   /api/credentials  {api_key, api_secret}     (held in memory only)
  DELETE /api/credentials
  POST   /api/check        {symbol, exchange}        (connection + request-cost check)
  POST   /api/start        {symbol, exchange, strategies, trade, grid_*, ...}
  POST   /api/stop

exchange is "binance" (Spot Demo Mode) or "gate" (Gate.com spot testnet —
demo funds, watch at testnet.gate.com). Saved credentials are handed to the
bot as BINANCE_* or GATE_* env vars to match the selected exchange.

The bot itself runs as a bot.py subprocess (same graceful CTRL_BREAK/SIGINT
stop as the launcher/GUI, so open orders are cancelled on stop) and its stdout
is buffered here for the dashboard's console. CORS is open for localhost use.

  py api_server.py [--port 8801]
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from binance_client import BinanceClient  # noqa: E402
from gate_client import GateClient, GateFuturesClient  # noqa: E402

VALID_EXCHANGES = {"binance", "gate", "gate_futures"}

BOT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py")
DAILY_FILE = os.path.join(os.path.expanduser("~"), ".gridbot-daily.json")
STABLECOINS = {"USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI"}


def bot_cmd_prefix():
    """How to launch the bot: overridable by the launcher (frozen .exe/.app
    re-invokes itself with --service bot; a python script just runs bot.py)."""
    override = os.environ.get("GRIDBOT_BOT_CMD")
    if override:
        try:
            return json.loads(override)
        except ValueError:
            pass
    if getattr(sys, "frozen", False):
        return [sys.executable, "--service", "bot"]
    return [sys.executable, "-u", BOT_SCRIPT]


def backtest_cmd_prefix():
    override = os.environ.get("GRIDBOT_BACKTEST_CMD")
    if override:
        try:
            return json.loads(override)
        except ValueError:
            pass
    if getattr(sys, "frozen", False):
        return [sys.executable, "--service", "backtest"]
    return [sys.executable, "-u",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest.py")]


def price_spec(value):
    """Absolute price ('59000') or percent offset from the mid ('-3%')."""
    s = str(value).strip()
    float(s[:-1] if s.endswith("%") else s)     # raises ValueError if not numeric
    return s


def spreads_spec(value):
    """Comma list of spreads, each a percent ('0.1%') or fraction ('0.001')."""
    tokens = [t.strip() for t in str(value).split(",") if t.strip()]
    if not tokens:
        raise ValueError("empty")
    for t in tokens:
        float(t[:-1] if t.endswith("%") else t)
    return ",".join(tokens)


NUMERIC_FLAGS = {
    "grid_start": ("--grid-start", price_spec),
    "grid_end": ("--grid-end", price_spec),
    "grid_limit": ("--grid-limit", price_spec),
    "grid_levels": ("--grid-levels", int),
    "grid_max_open": ("--grid-max-open", int),
    "pmm_spreads": ("--pmm-spreads", spreads_spec),
    "pmm_refresh": ("--pmm-refresh", float),
    "pmm_skew": ("--pmm-skew", float),
    "pmm_max_inventory": ("--pmm-max-inventory", float),
    "max_loss": ("--max-loss", float),
    "st_length": ("--st-length", int),
    "st_multiplier": ("--st-multiplier", float),
    "st_threshold": ("--st-threshold", price_spec),
    "total_quote": ("--total-quote", float),
    "leverage": ("--leverage", int),
    "fee": ("--fee", price_spec),           # backtests only
    "interval": ("--interval", float),
    "duration": ("--duration", float),
}
VALID_STRATEGIES = {"grid", "pmm", "supertrend"}


class BotManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.mode = None                # "check" | "dry-run" | "trading"
        self.started_at = None
        self.lines = deque(maxlen=5000)
        self.total_lines = 0
        self.api_key = None
        self.api_secret = None
        self.bot_stats = None       # last @STATS payload from the bot
        self.backtest_result = None  # last @BACKTEST payload (comparison table)
        self.last_exit_code = None  # how the previous bot process ended

    # ---------------------------------------------------------------- logging

    def log(self, line):
        with self.lock:
            self.lines.append(line.rstrip("\n"))
            self.total_lines += 1

    def get_logs(self, since):
        with self.lock:
            first = self.total_lines - len(self.lines)
            offset = max(0, since - first)
            return {"next": self.total_lines,
                    "lines": list(self.lines)[offset:] if since < self.total_lines else []}

    # ------------------------------------------------------------ credentials

    def set_credentials(self, key, secret):
        with self.lock:
            self.api_key, self.api_secret = key or None, secret or None

    def exchange_summary(self, symbol, exchange="binance"):
        """Live proof from the exchange itself: balances, resting orders, and
        executed trades queried straight from the exchange (not our records)."""
        with self.lock:
            key, secret = self.api_key, self.api_secret
        if not (key and secret):
            return {"error": "no credentials saved"}
        client = {"gate": lambda: GateClient("testnet", key, secret),
                  "gate_futures": lambda: GateFuturesClient("testnet", key, secret),
                  }.get(exchange, lambda: BinanceClient("demo", key, secret))()
        out = {"symbol": symbol, "exchange": exchange}
        acct = client.account()
        if acct["status"] == 200:
            out["balances"] = [b for b in acct["body"].get("balances", [])
                               if float(b.get("free", 0)) or float(b.get("locked", 0))]
        else:
            out["error"] = f"account: {acct['body'].get('msg', acct['body'])}"
            return out
        oo = client.open_orders(symbol)
        out["open_orders"] = ([
            {"orderId": o["orderId"], "clientOrderId": o["clientOrderId"],
             "side": o["side"], "price": o["price"], "qty": o["origQty"],
             "time": o["time"]}
            for o in oo["body"]] if oo["status"] == 200 else [])
        # total account value in USD + change since the first reading today
        prices_resp = client.all_prices()
        if prices_resp["status"] == 200:
            prices = {p["symbol"]: float(p["price"]) for p in prices_resp["body"]}
            total_usd = 0.0
            for b in out["balances"]:
                amount = float(b.get("free", 0)) + float(b.get("locked", 0))
                if b["asset"] in STABLECOINS:
                    total_usd += amount
                elif prices.get(b["asset"] + "USDT"):
                    total_usd += amount * prices[b["asset"] + "USDT"]
            out["total_usd"] = round(total_usd, 2)
            out["daily_pnl"] = self._daily_pnl(total_usd, exchange)
        if exchange == "gate_futures":
            # the futures account's ground truth: the open position and the
            # wallet ledger — this is where legacy positions, funding, and
            # maker rebates live (none of them appear in the session tiles)
            pos = client.position(symbol)
            if pos["status"] == 200 and float(pos["body"].get("size_base", 0)):
                out["position"] = pos["body"]
            midnight = time.mktime(time.strptime(
                time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
            book = client.account_book(limit=500, since=midnight)
            if book["status"] == 200:
                by_type = {}
                for entry in book["body"]:
                    kind = entry.get("type", "?")
                    by_type[kind] = by_type.get(kind, 0.0) + float(
                        entry.get("change", 0) or 0)
                out["today_by_type"] = {k: round(v, 4)
                                        for k, v in sorted(by_type.items())}
        tr = client.my_trades(symbol, limit=15)
        out["trades"] = ([
            {"tradeId": t["id"], "orderId": t["orderId"],
             "side": "BUY" if t["isBuyer"] else "SELL", "price": t["price"],
             "qty": t["qty"], "quoteQty": t["quoteQty"],
             "commission": t["commission"], "time": t["time"]}
            for t in sorted(tr["body"], key=lambda t: -t["time"])]
            if tr["status"] == 200 else [])
        return out

    @staticmethod
    def _daily_pnl(equity, exchange):
        """Equity change since the first reading of the local calendar day,
        tracked per exchange (a 1000-USDT futures wallet must not be compared
        against a 10000-USD spot baseline). Persists in DAILY_FILE."""
        today = time.strftime("%Y-%m-%d")
        state = {}
        try:
            with open(DAILY_FILE) as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            pass
        if state.get("date") != today or "baselines" not in state:
            state = {"date": today, "baselines": {}}
        if exchange not in state["baselines"]:
            state["baselines"][exchange] = equity
            try:
                with open(DAILY_FILE, "w") as fh:
                    json.dump(state, fh)
            except OSError:
                pass
        return round(equity - state["baselines"][exchange], 2)

    def credentials_info(self):
        with self.lock:
            if not (self.api_key and self.api_secret):
                return {"set": False}
            masked = self.api_key[:4] + "..." + self.api_key[-4:] \
                if len(self.api_key) > 8 else "****"
            return {"set": True, "api_key_masked": masked}

    # -------------------------------------------------------------- lifecycle

    def status(self):
        with self.lock:
            running = self.proc is not None and self.proc.poll() is None
            return {
                "running": running,
                "mode": self.mode if running else None,
                "pid": self.proc.pid if running else None,
                "uptime_s": round(time.time() - self.started_at, 1)
                if running and self.started_at else None,
                "log_count": self.total_lines,
                "credentials_set": bool(self.api_key and self.api_secret),
                "stats": self.bot_stats,
                "backtest": self.backtest_result,
                "last_exit_code": self.last_exit_code,
            }

    def start(self, params, check=False, backtest=False):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return False, "bot is already running - stop it first"
            symbol = (params.get("symbol") or "BTCUSDT").strip().upper()
            exchange = str(params.get("exchange") or "binance").strip().lower()
            if exchange not in VALID_EXCHANGES:
                return False, f"bad exchange: {exchange!r}"
            if backtest:
                cmd = backtest_cmd_prefix() + ["--symbol", symbol,
                                               "--exchange", exchange]
                strategies = [s for s in (params.get("strategies") or ["grid"])
                              if s in VALID_STRATEGIES]
                if not strategies:
                    return False, "no valid strategies selected"
                cmd += ["--strategies", ",".join(strategies)]
                try:
                    cmd += ["--days", str(float(params.get("days") or 7))]
                except (TypeError, ValueError):
                    return False, f"days must be a number: {params.get('days')!r}"
                for key, (flag, cast) in NUMERIC_FLAGS.items():
                    if key in ("interval", "duration", "max_loss"):
                        continue        # live-loop flags; the backtester has none
                    value = params.get(key)
                    if value not in (None, ""):
                        try:
                            cmd.append(f"{flag}={cast(value)}")
                        except (TypeError, ValueError):
                            return False, (f"parameter '{key}' must be a number "
                                           f"or a percent like -3%: {value!r}")
                self.mode = "backtest"
            elif check:
                cmd = bot_cmd_prefix() + ["--symbol", symbol,
                                          "--exchange", exchange, "--check"]
                self.mode = "check"
            else:
                cmd = bot_cmd_prefix() + ["--symbol", symbol,
                                          "--exchange", exchange]
                strategies = [s for s in (params.get("strategies") or ["grid"])
                              if s in VALID_STRATEGIES]
                if not strategies:
                    return False, "no valid strategies selected"
                cmd += ["--strategies", ",".join(strategies)]
                for key, (flag, cast) in NUMERIC_FLAGS.items():
                    if key == "fee":
                        continue        # backtest-only; bot.py has no --fee
                    value = params.get(key)
                    if value not in (None, ""):
                        try:
                            # --flag=value form: "-3%" as a separate argv token
                            # would be parsed as an option by argparse
                            cmd.append(f"{flag}={cast(value)}")
                        except (TypeError, ValueError):
                            return False, (f"parameter '{key}' must be a number "
                                           f"or a percent like -3%: {value!r}")
                if params.get("trade"):
                    if not (self.api_key and self.api_secret):
                        if exchange.startswith("gate"):
                            return False, ("trading needs credentials - create a "
                                           "key in Testnet API Key Management on "
                                           "gate.com (spot trade permission), "
                                           "then save it here")
                        return False, ("trading needs credentials - create a key in "
                                       "API Management while in Demo Trading on "
                                       "binance.com, then save it here")
                    cmd.append("--trade")
                    self.mode = {"gate": "trading (gate testnet)",
                                 "gate_futures": "trading (gate futures testnet)",
                                 }.get(exchange, "trading")
                else:
                    self.mode = "dry-run"

            env = os.environ.copy()
            for var in ("BINANCE_API_KEY", "BINANCE_API_SECRET",
                        "GATE_API_KEY", "GATE_API_SECRET"):
                env.pop(var, None)
            if self.api_key and self.api_secret:
                prefix = "GATE" if exchange.startswith("gate") else "BINANCE"
                env[f"{prefix}_API_KEY"] = self.api_key
                env[f"{prefix}_API_SECRET"] = self.api_secret
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["GRIDBOT_MANAGED"] = "1"        # bot honours "STOP" on stdin
            creationflags = (subprocess.CREATE_NEW_PROCESS_GROUP
                             if sys.platform == "win32" else 0)
            try:
                self.proc = subprocess.Popen(
                    cmd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                    errors="replace", creationflags=creationflags,
                    cwd=os.path.dirname(BOT_SCRIPT))
            except OSError as e:
                self.mode = None
                return False, f"failed to launch bot: {e}"
            self.started_at = time.time()
            self.bot_stats = None       # fresh session, fresh numbers
            self.last_exit_code = None
            proc = self.proc
        self.log(f"[api] started bot pid={proc.pid} mode={self.mode}")
        threading.Thread(target=self._pump, args=(proc,), daemon=True).start()
        return True, self.mode

    def _pump(self, proc):
        for line in proc.stdout:
            if line.startswith("@STATS "):
                try:
                    with self.lock:
                        self.bot_stats = json.loads(line[7:])
                except ValueError:
                    pass
                continue        # machine channel — keep it out of the console
            if line.startswith("@BACKTEST "):
                try:
                    with self.lock:
                        self.backtest_result = json.loads(line[10:])
                except ValueError:
                    pass
                continue
            self.log(line)
        proc.wait()
        self.log(f"[api] bot exited with code {proc.returncode}")
        with self.lock:
            self.last_exit_code = proc.returncode
            if self.proc is proc:
                self.proc = None
                self.mode = None

    def stop(self):
        with self.lock:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                return False, "bot is not running"
        self.log("[api] stop requested - bot will cancel its open orders")
        try:                                    # preferred: works even frozen
            proc.stdin.write("STOP\n")
            proc.stdin.flush()
        except (OSError, ValueError, AttributeError):
            try:                                # fallback: console signal
                if sys.platform == "win32":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    proc.send_signal(signal.SIGINT)
            except OSError:
                proc.terminate()

        def hard_kill():
            if proc.poll() is None:
                self.log("[api] graceful stop timed out - killing bot")
                proc.kill()
        threading.Timer(15, hard_kill).start()
        return True, "stopping"


MANAGER = BotManager()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):        # quiet the per-request noise
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return {}

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self._send(200, {"ok": True, "bot_running": MANAGER.status()["running"]})
        elif parsed.path == "/api/status":
            self._send(200, MANAGER.status())
        elif parsed.path == "/api/logs":
            qs = urllib.parse.parse_qs(parsed.query)
            since = int(qs.get("since", ["0"])[0])
            self._send(200, MANAGER.get_logs(since))
        elif parsed.path == "/api/credentials":
            self._send(200, MANAGER.credentials_info())
        elif parsed.path == "/api/exchange":
            qs = urllib.parse.parse_qs(parsed.query)
            symbol = (qs.get("symbol", ["BTCUSDT"])[0] or "BTCUSDT").upper()
            exchange = (qs.get("exchange", ["binance"])[0] or "binance").lower()
            if exchange not in VALID_EXCHANGES:
                exchange = "binance"
            self._send(200, MANAGER.exchange_summary(symbol, exchange))
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        body = self._body()
        if self.path == "/api/credentials":
            MANAGER.set_credentials(body.get("api_key", "").strip(),
                                    body.get("api_secret", "").strip())
            self._send(200, MANAGER.credentials_info())
        elif self.path == "/api/start":
            ok, msg = MANAGER.start(body)
            self._send(200 if ok else 409, {"ok": ok, "message": msg})
        elif self.path == "/api/check":
            ok, msg = MANAGER.start(body, check=True)
            self._send(200 if ok else 409, {"ok": ok, "message": msg})
        elif self.path == "/api/backtest":
            ok, msg = MANAGER.start(body, backtest=True)
            self._send(200 if ok else 409, {"ok": ok, "message": msg})
        elif self.path == "/api/stop":
            ok, msg = MANAGER.stop()
            self._send(200 if ok else 409, {"ok": ok, "message": msg})
        else:
            self._send(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path == "/api/credentials":
            MANAGER.set_credentials(None, None)
            self._send(200, {"set": False})
        else:
            self._send(404, {"error": "not found"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8801)
    args = parser.parse_args()

    if hasattr(signal, "SIGBREAK"):       # graceful stop from the launcher
        signal.signal(signal.SIGBREAK, signal.default_int_handler)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)

    # managed by the launcher: "STOP" on stdin (or launcher death) shuts us down
    if os.environ.get("GRIDBOT_MANAGED") == "1":
        def _watch_stdin():
            try:
                for line in sys.stdin:
                    if line.strip().upper() == "STOP":
                        break
            except Exception:
                pass
            server.shutdown()
        threading.Thread(target=_watch_stdin, daemon=True).start()

    print(f"bot API listening on http://localhost:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("bot API shutting down...")
        if MANAGER.status()["running"]:
            MANAGER.stop()
            deadline = time.time() + 18
            while MANAGER.status()["running"] and time.time() < deadline:
                time.sleep(0.5)
        server.server_close()


if __name__ == "__main__":
    main()
