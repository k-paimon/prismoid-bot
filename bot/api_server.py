"""
Bot backend API (pure stdlib) — the service the web dashboard talks to.

Wraps bot.py as a small JSON HTTP API on http://localhost:8801:

  GET    /health           -> {ok, bot_running}
  GET    /api/status       -> {running, mode, pid, uptime_s, log_count}
  GET    /api/logs?since=N -> {next, lines: [...]}   (incremental console feed)
  GET    /api/credentials  -> {set, api_key_masked}
  POST   /api/credentials  {api_key, api_secret}     (held in memory only)
  DELETE /api/credentials
  POST   /api/check        {symbol}                  (connection + request-cost check)
  POST   /api/start        {symbol, strategies, trade, grid_*, total_quote, ...}
  POST   /api/stop

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

BOT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py")


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

def price_spec(value):
    """Absolute price ('59000') or percent offset from the mid ('-3%')."""
    s = str(value).strip()
    float(s[:-1] if s.endswith("%") else s)     # raises ValueError if not numeric
    return s


NUMERIC_FLAGS = {
    "grid_start": ("--grid-start", price_spec),
    "grid_end": ("--grid-end", price_spec),
    "grid_limit": ("--grid-limit", price_spec),
    "grid_levels": ("--grid-levels", int),
    "grid_max_open": ("--grid-max-open", int),
    "total_quote": ("--total-quote", float),
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

    def exchange_summary(self, symbol):
        """Live proof from Binance itself: balances, resting orders, and
        executed trades queried straight from the exchange (not our records)."""
        with self.lock:
            key, secret = self.api_key, self.api_secret
        if not (key and secret):
            return {"error": "no credentials saved"}
        client = BinanceClient("demo", key, secret)
        out = {"symbol": symbol}
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
        tr = client.my_trades(symbol, limit=15)
        out["trades"] = ([
            {"tradeId": t["id"], "orderId": t["orderId"],
             "side": "BUY" if t["isBuyer"] else "SELL", "price": t["price"],
             "qty": t["qty"], "quoteQty": t["quoteQty"],
             "commission": t["commission"], "time": t["time"]}
            for t in sorted(tr["body"], key=lambda t: -t["time"])]
            if tr["status"] == 200 else [])
        return out

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
            }

    def start(self, params, check=False):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return False, "bot is already running - stop it first"
            symbol = (params.get("symbol") or "BTCUSDT").strip().upper()
            cmd = bot_cmd_prefix() + ["--symbol", symbol]
            if check:
                cmd.append("--check")
                self.mode = "check"
            else:
                strategies = [s for s in (params.get("strategies") or ["grid"])
                              if s in VALID_STRATEGIES]
                if not strategies:
                    return False, "no valid strategies selected"
                cmd += ["--strategies", ",".join(strategies)]
                for key, (flag, cast) in NUMERIC_FLAGS.items():
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
                        return False, ("trading needs credentials - create a key in "
                                       "API Management while in Demo Trading on "
                                       "binance.com, then save it here")
                    cmd.append("--trade")
                    self.mode = "trading"
                else:
                    self.mode = "dry-run"

            env = os.environ.copy()
            env.pop("BINANCE_API_KEY", None)
            env.pop("BINANCE_API_SECRET", None)
            if self.api_key and self.api_secret:
                env["BINANCE_API_KEY"] = self.api_key
                env["BINANCE_API_SECRET"] = self.api_secret
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
            proc = self.proc
        self.log(f"[api] started bot pid={proc.pid} mode={self.mode}")
        threading.Thread(target=self._pump, args=(proc,), daemon=True).start()
        return True, self.mode

    def _pump(self, proc):
        for line in proc.stdout:
            self.log(line)
        proc.wait()
        self.log(f"[api] bot exited with code {proc.returncode}")
        with self.lock:
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
            self._send(200, MANAGER.exchange_summary(symbol))
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
