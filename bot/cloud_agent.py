"""
Cloud agent — the VM-side daemon of the Vercel + Supabase architecture.

Replaces the localhost api_server for cloud deployments: instead of serving
HTTP, it polls Supabase for queued commands and mirrors bot state back:

    bot_settings     read   defaults (exchange, symbol, strategy params)
    bot_credentials  read   exchange API keys saved from the webapp
    bot_commands     claim  start / stop / check / backtest, mark done/error
    bot_state        write  heartbeat + status + @STATS + exchange snapshot
    bot_logs         write  console lines (idempotent by seq, pruned)

Reuses BotManager from api_server.py, so the bot subprocess lifecycle
(graceful STOP -> orders cancelled, @STATS parsing) is identical to the
local dashboard's.

    py cloud_agent.py [--poll 2]

Env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY (service role — trusted VM only).
Runs until SIGTERM/CTRL+C; on shutdown it stops the bot first so open orders
are cancelled, then writes a final running=false state row.
"""
import argparse
import os
import signal
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_server import BotManager          # noqa: E402
from supabase_client import Supabase, SupabaseError  # noqa: E402

AGENT_VERSION = "0.1"
LOG_RETAIN = 5000          # keep this many console lines in bot_logs
LOG_BATCH = 500            # rows per insert
SUMMARY_EVERY_S = 30       # exchange snapshot cadence (several REST calls)
PRUNE_EVERY_S = 300


def iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() if ts is None else ts))


class CloudAgent:
    def __init__(self, poll_s=2.0):
        self.sb = Supabase()
        self.poll_s = poll_s
        self.manager = BotManager()
        self.pushed_seq = 0
        self.summary = None
        self.summary_lock = threading.Lock()
        self.last_prune = 0.0
        self.stopping = False

    # ------------------------------------------------------------- supabase io

    def settings(self):
        rows = self.sb.select("bot_settings", "id=eq.1")
        return rows[0] if rows else {"exchange": "gate_futures",
                                     "symbol": "BTCUSDT", "params": {}}

    def sync_credentials(self, exchange):
        """Supabase is the source of truth for keys; gate spot and futures
        share one Gate key pair, mirroring api_server's GATE_*/BINANCE_* split."""
        cred_key = "gate" if str(exchange).startswith("gate") else "binance"
        rows = self.sb.select("bot_credentials", f"exchange=eq.{cred_key}")
        if rows:
            self.manager.set_credentials(rows[0]["api_key"], rows[0]["api_secret"])
        else:
            self.manager.set_credentials(None, None)

    # --------------------------------------------------------------- commands

    def handle_commands(self):
        pending = self.sb.select("bot_commands",
                                 "status=eq.pending&order=id.asc&limit=5")
        for cmd in pending:
            # conditional claim: no-op if another agent instance got it first
            claimed = self.sb.update(
                "bot_commands", f"id=eq.{cmd['id']}&status=eq.pending",
                {"status": "running"})
            if not claimed:
                continue
            action = cmd["action"]
            try:
                if action == "stop":
                    ok, msg = self.manager.stop()
                else:
                    s = self.settings()
                    merged = {"symbol": s.get("symbol"),
                              "exchange": s.get("exchange")}
                    merged.update(s.get("params") or {})
                    merged.update(cmd.get("params") or {})
                    self.sync_credentials(merged.get("exchange") or "binance")
                    ok, msg = self.manager.start(merged,
                                                 check=(action == "check"),
                                                 backtest=(action == "backtest"))
            except Exception as e:      # a bad command must not kill the agent
                ok, msg = False, f"{type(e).__name__}: {e}"
            self.manager.log(f"[agent] command #{cmd['id']} {action}: {msg}")
            self.sb.update("bot_commands", f"id=eq.{cmd['id']}", {
                "status": "done" if ok else "error",
                "result": {"ok": ok, "message": msg},
                "handled_at": iso()})

    # ------------------------------------------------------------ state + logs

    def push_logs(self):
        res = self.manager.get_logs(self.pushed_seq)
        lines = res["lines"]
        if lines:
            start = res["next"] - len(lines)
            rows = [{"seq": start + i, "line": ln}
                    for i, ln in enumerate(lines)]
            for i in range(0, len(rows), LOG_BATCH):
                self.sb.insert("bot_logs", rows[i:i + LOG_BATCH], upsert=True)
        self.pushed_seq = res["next"]

    def push_state(self):
        st = self.manager.status()
        with self.summary_lock:
            summary = self.summary
        self.sb.upsert("bot_state", {
            "id": 1,
            "running": st["running"],
            "mode": st["mode"],
            "pid": st["pid"],
            "started_at": iso(self.manager.started_at)
            if st["running"] and self.manager.started_at else None,
            "credentials_set": st["credentials_set"],
            "stats": st["stats"],
            "backtest": st["backtest"],
            "exchange_summary": summary,
            "log_next": self.pushed_seq,
            "heartbeat_at": iso(),
            "agent_version": AGENT_VERSION,
        })

    def prune_logs(self):
        cutoff = self.pushed_seq - LOG_RETAIN
        if cutoff > 0:
            self.sb.delete("bot_logs", f"seq=lt.{cutoff}")

    # ------------------------------------------------- exchange snapshot thread

    def summary_loop(self):
        while not self.stopping:
            try:
                s = self.settings()
                exchange = s.get("exchange") or "binance"
                self.sync_credentials(exchange)
                if self.manager.credentials_info().get("set"):
                    out = self.manager.exchange_summary(
                        s.get("symbol") or "BTCUSDT", exchange)
                    out["as_of"] = iso()
                    with self.summary_lock:
                        self.summary = out
            except Exception:
                traceback.print_exc()
            for _ in range(int(SUMMARY_EVERY_S / 0.5)):
                if self.stopping:
                    return
                time.sleep(0.5)

    # -------------------------------------------------------------------- main

    def run(self):
        # a fresh agent means a fresh console: old seq numbering is meaningless
        self.sb.delete("bot_logs", "seq=gte.0")
        # commands stuck in 'running' belong to a previous agent process
        self.sb.update("bot_commands", "status=eq.running", {
            "status": "error",
            "result": {"ok": False, "message": "agent restarted mid-command"},
            "handled_at": iso()})
        self.manager.log(f"[agent] cloud agent v{AGENT_VERSION} online "
                         f"(poll {self.poll_s}s)")
        threading.Thread(target=self.summary_loop, daemon=True).start()

        while not self.stopping:
            try:
                self.handle_commands()
                self.push_logs()
                self.push_state()
                if time.time() - self.last_prune > PRUNE_EVERY_S:
                    self.prune_logs()
                    self.last_prune = time.time()
            except SupabaseError as e:
                print(f"[agent] supabase unreachable, retrying: {e}",
                      file=sys.stderr)
            except Exception:
                traceback.print_exc()
            time.sleep(self.poll_s)

    def shutdown(self):
        """Stop the bot (cancels its open orders) and leave an honest state row."""
        self.stopping = True
        ok, _ = self.manager.stop()
        if ok:
            deadline = time.time() + 20
            while time.time() < deadline and self.manager.status()["running"]:
                time.sleep(0.5)
        try:
            self.manager.log("[agent] agent shutting down")
            self.push_logs()
            self.push_state()
        except SupabaseError:
            pass


def main():
    parser = argparse.ArgumentParser(description="Supabase cloud agent")
    parser.add_argument("--poll", type=float, default=2.0,
                        help="seconds between Supabase polls (default 2)")
    args = parser.parse_args()

    agent = CloudAgent(poll_s=args.poll)

    def on_sigterm(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        agent.run()
    except KeyboardInterrupt:
        pass
    finally:
        agent.shutdown()


if __name__ == "__main__":
    main()
