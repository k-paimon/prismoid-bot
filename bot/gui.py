"""
Native desktop UI (tkinter — ships with Python on Windows and macOS) for running
the Grid Strike strategy on Binance Spot Demo Mode.

  py bare-features\\bot\\gui.py          (Windows)
  python3 bare-features/bot/gui.py       (macOS)

The GUI is a front-end for bot.py: it collects credentials + grid parameters,
launches `bot.py --strategies grid` as a subprocess (keys passed via the child's
environment, never written to disk), streams its output into the log pane, and
stops it gracefully (SIGINT / CTRL_BREAK) so open orders get cancelled on stop.
Non-secret parameters persist in ~/.bare-features-grid-gui.json between runs.
"""
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

BOT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py")
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".bare-features-grid-gui.json")

PARAM_FIELDS = [
    # (settings key, label, default, tooltip-ish help)
    ("symbol", "Trading pair", "BTCUSDT", "e.g. BTCUSDT, ETHUSDT"),
    ("grid_start", "Grid start price", "", "lower bound; blank = mid - 3% at start"),
    ("grid_end", "Grid end price", "", "upper bound; blank = mid + 3% at start"),
    ("grid_limit", "Limit (stop) price", "", "pull orders below this; blank = start - 2%"),
    ("grid_levels", "Grid levels", "8", "number of price levels"),
    ("grid_max_open", "Max open orders", "3", "simultaneously resting orders"),
    ("total_quote", "Total amount (quote)", "200", "budget split across levels, in USDT"),
    ("interval", "Tick interval (s)", "10", "seconds between strategy ticks"),
    ("duration", "Duration (s)", "", "blank = run until Stop"),
]


class GridStrikeGUI:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.log_queue = queue.Queue()
        root.title("Grid Strike - Binance Demo")
        root.minsize(760, 560)

        outer = ttk.Frame(root, padding=10)
        outer.pack(fill="both", expand=True)
        left = ttk.Frame(outer)
        left.pack(side="left", fill="y", padx=(0, 10))

        # ----------------------------------------------------- credentials
        creds = ttk.LabelFrame(left, text=" Binance Demo credentials ", padding=8)
        creds.pack(fill="x")
        ttk.Label(creds, text="API key").grid(row=0, column=0, sticky="w")
        self.api_key = ttk.Entry(creds, width=34)
        self.api_key.grid(row=0, column=1, pady=2)
        ttk.Label(creds, text="API secret").grid(row=1, column=0, sticky="w")
        self.api_secret = ttk.Entry(creds, width=34, show="*")
        self.api_secret.grid(row=1, column=1, pady=2)
        ttk.Label(creds, foreground="gray",
                  text="keys: binance.com > Demo Trading > API Management\n"
                       "keys are kept in memory only, never saved").grid(
            row=2, column=0, columnspan=2, sticky="w")

        # ------------------------------------------------------ parameters
        params = ttk.LabelFrame(left, text=" Grid Strike parameters ", padding=8)
        params.pack(fill="x", pady=(10, 0))
        self.fields = {}
        for row, (key, label, default, help_text) in enumerate(PARAM_FIELDS):
            ttk.Label(params, text=label).grid(row=row, column=0, sticky="w")
            entry = ttk.Entry(params, width=16)
            entry.insert(0, default)
            entry.grid(row=row, column=1, pady=2, sticky="w")
            ttk.Label(params, text=help_text, foreground="gray").grid(
                row=row, column=2, sticky="w", padx=(6, 0))
            self.fields[key] = entry

        self.trade_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            params, variable=self.trade_var,
            text="Place real demo orders (off = dry-run, prints actions only)",
        ).grid(row=len(PARAM_FIELDS), column=0, columnspan=3, sticky="w", pady=(6, 0))

        # --------------------------------------------------------- buttons
        buttons = ttk.Frame(left)
        buttons.pack(fill="x", pady=10)
        self.check_btn = ttk.Button(buttons, text="Test connection",
                                    command=self.test_connection)
        self.check_btn.pack(side="left")
        self.start_btn = ttk.Button(buttons, text="Start bot", command=self.start_bot)
        self.start_btn.pack(side="left", padx=6)
        self.stop_btn = ttk.Button(buttons, text="Stop", command=self.stop_bot,
                                   state="disabled")
        self.stop_btn.pack(side="left")

        self.status = ttk.Label(left, text="idle", foreground="gray")
        self.status.pack(anchor="w")

        # -------------------------------------------------------- log pane
        log_frame = ttk.LabelFrame(outer, text=" Bot output ", padding=4)
        log_frame.pack(side="left", fill="both", expand=True)
        self.log = scrolledtext.ScrolledText(log_frame, width=80, height=30,
                                             state="disabled", font=("Consolas", 9)
                                             if sys.platform == "win32"
                                             else ("Menlo", 11))
        self.log.pack(fill="both", expand=True)

        self.load_settings()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.drain_log_queue)

    # -------------------------------------------------------------- settings

    def load_settings(self):
        try:
            with open(SETTINGS_FILE) as fh:
                saved = json.load(fh)
            for key, entry in self.fields.items():
                if key in saved:
                    entry.delete(0, "end")
                    entry.insert(0, saved[key])
        except (OSError, ValueError):
            pass

    def save_settings(self):
        try:
            with open(SETTINGS_FILE, "w") as fh:
                json.dump({k: e.get().strip() for k, e in self.fields.items()}, fh)
        except OSError:
            pass

    # ------------------------------------------------------------ subprocess

    def build_env(self):
        env = os.environ.copy()
        key, secret = self.api_key.get().strip(), self.api_secret.get().strip()
        if key and secret:
            env["BINANCE_API_KEY"] = key
            env["BINANCE_API_SECRET"] = secret
        else:
            env.pop("BINANCE_API_KEY", None)
            env.pop("BINANCE_API_SECRET", None)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def launch(self, cmd_args, status):
        if self.proc is not None:
            messagebox.showinfo("Busy", "The bot is already running - stop it first.")
            return
        cmd = [sys.executable, BOT_SCRIPT] + cmd_args
        creationflags = (subprocess.CREATE_NEW_PROCESS_GROUP
                         if sys.platform == "win32" else 0)
        try:
            self.proc = subprocess.Popen(
                cmd, env=self.build_env(), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", creationflags=creationflags,
                cwd=os.path.dirname(BOT_SCRIPT))
        except OSError as e:
            messagebox.showerror("Launch failed", str(e))
            self.proc = None
            return
        self.append_log(f"$ {' '.join(cmd_args)}\n")
        self.set_running(True, status)
        threading.Thread(target=self.reader_thread, args=(self.proc,),
                         daemon=True).start()

    def reader_thread(self, proc):
        for line in proc.stdout:
            self.log_queue.put(line)
        proc.wait()
        self.log_queue.put(f"\n[process exited with code {proc.returncode}]\n")
        self.log_queue.put(None)       # sentinel: back to idle

    def drain_log_queue(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                if line is None:
                    self.proc = None
                    self.set_running(False, "idle")
                else:
                    self.append_log(line)
        except queue.Empty:
            pass
        self.root.after(100, self.drain_log_queue)

    def append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_running(self, running, status):
        self.status.configure(text=status,
                              foreground="dark green" if running else "gray")
        self.start_btn.configure(state="disabled" if running else "normal")
        self.check_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")

    # --------------------------------------------------------------- actions

    def test_connection(self):
        self.save_settings()
        symbol = self.fields["symbol"].get().strip() or "BTCUSDT"
        self.launch(["--check", "--symbol", symbol], "running connection check...")

    def start_bot(self):
        self.save_settings()
        get = lambda k: self.fields[k].get().strip()  # noqa: E731
        cmd = ["--strategies", "grid", "--symbol", get("symbol") or "BTCUSDT"]
        try:
            def price_spec(v):      # absolute price or "%" offset from the mid
                float(v[:-1] if v.endswith("%") else v)
                return v
            numeric = {"grid_levels": ("--grid-levels", int),
                       "grid_max_open": ("--grid-max-open", int),
                       "total_quote": ("--total-quote", float),
                       "interval": ("--interval", float),
                       "duration": ("--duration", float),
                       "grid_start": ("--grid-start", price_spec),
                       "grid_end": ("--grid-end", price_spec),
                       "grid_limit": ("--grid-limit", price_spec)}
            for key, (flag, cast) in numeric.items():
                value = get(key)
                if value:
                    cmd.append(f"{flag}={cast(value)}")     # "=": values may start with "-"
        except ValueError as e:
            messagebox.showerror("Bad parameter", f"Not a number: {e}")
            return
        if self.trade_var.get():
            if not (self.api_key.get().strip() and self.api_secret.get().strip()):
                messagebox.showerror(
                    "Missing credentials",
                    "Real demo orders need an API key and secret.\n"
                    "Create one in API Management while in Demo Trading.")
                return
            cmd.append("--trade")
        self.launch(cmd, "bot running (trading)" if self.trade_var.get()
                    else "bot running (dry-run)")

    def stop_bot(self):
        if self.proc is None:
            return
        self.append_log("\n[stopping - the bot will cancel its open orders]\n")
        try:
            if sys.platform == "win32":
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.proc.send_signal(signal.SIGINT)
        except OSError:
            self.proc.terminate()
        # hard-kill fallback if graceful shutdown hangs
        self.root.after(15000, self.force_kill)

    def force_kill(self):
        if self.proc is not None and self.proc.poll() is None:
            self.append_log("[graceful stop timed out - killing process]\n")
            self.proc.kill()

    def on_close(self):
        if self.proc is not None and self.proc.poll() is None:
            if not messagebox.askyesno(
                    "Bot is running",
                    "Stop the bot and quit? Open orders will be cancelled."):
                return
            self.stop_bot()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.save_settings()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:                                    # nicer widgets on Windows
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "aqua")
    except tk.TclError:
        pass
    GridStrikeGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
