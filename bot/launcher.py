"""
XAMPP-style control panel (tkinter) for the Grid Strike bot stack.

Two managed services, each started/stopped with a button and shown with a
status light, exactly like XAMPP's Apache/MySQL rows:

  Bot API   port 8801   (api_server.py - the backend the dashboard talks to)
  Web UI    port 8800   (web_server.py - serves the dashboard page)

Starting the Web UI opens http://localhost:8800 in the browser automatically.

This file is also the single entry point for the packaged .exe / .app builds:
  launcher                        -> the control panel GUI
  launcher --service api  [--port N]
  launcher --service web  [--port N]
  launcher --service bot  [bot.py args...]
A frozen build re-invokes its own executable with --service to run the
services, so the same binary is the GUI, both servers, and the bot.

Dev run:  py bare-features\\bot\\launcher.py
Build:    build_windows.ps1 (exe) / build_macos.sh (app + dmg)
"""
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
FROZEN = getattr(sys, "frozen", False)
WEB_PORT = 8800
API_PORT = 8801


def service_cmd(name, port=None):
    if FROZEN:
        cmd = [sys.executable, "--service", name]
    else:
        cmd = [sys.executable, "-u", os.path.abspath(__file__), "--service", name]
    if port:
        cmd += ["--port", str(port)]
    return cmd


# --------------------------------------------------------------- service rows

class Service:
    def __init__(self, key, label, port, admin_url=None):
        self.key = key
        self.label = label
        self.port = port
        self.admin_url = admin_url
        self.proc = None
        self.healthy = False

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def env(self):
        env = os.environ.copy()
        env["GRIDBOT_MANAGED"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if self.key == "api":
            env["GRIDBOT_BOT_CMD"] = json.dumps(service_cmd("bot"))
        return env

    def start(self, log):
        if self.running:
            return
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            if FROZEN:
                creationflags |= subprocess.CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            service_cmd(self.key, self.port), env=self.env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", creationflags=creationflags, cwd=HERE)
        log(f"[{self.key}] started (pid {self.proc.pid}, port {self.port})")
        threading.Thread(target=self._pump, args=(self.proc, log),
                         daemon=True).start()

    def _pump(self, proc, log):
        for line in proc.stdout:
            log(f"[{self.key}] {line.rstrip()}")
        proc.wait()
        log(f"[{self.key}] exited with code {proc.returncode}")

    def stop(self, log):
        if not self.running:
            return
        log(f"[{self.key}] stopping...")
        proc = self.proc
        try:
            proc.stdin.write("STOP\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            proc.terminate()

        def hard_kill():
            if proc.poll() is None:
                log(f"[{self.key}] graceful stop timed out - killing")
                proc.kill()
        # api gets longer: it waits for the bot to cancel its orders first
        threading.Timer(25 if self.key == "api" else 8, hard_kill).start()

    def check_health(self):
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.4):
                self.healthy = True
        except OSError:
            self.healthy = False
        return self.healthy


# ----------------------------------------------------------------------- GUI

def run_gui():
    import tkinter as tk
    from tkinter import messagebox, scrolledtext, ttk

    services = [
        Service("api", "Bot API", API_PORT),
        Service("web", "Web UI", WEB_PORT, admin_url=f"http://localhost:{WEB_PORT}"),
    ]
    log_queue = queue.Queue()

    def log(msg):
        log_queue.put(msg)

    root = tk.Tk()
    root.title("Grid Bot Control Panel")
    root.minsize(660, 420)
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "aqua")
    except tk.TclError:
        pass

    outer = ttk.Frame(root, padding=12)
    outer.pack(fill="both", expand=True)
    ttk.Label(outer, text="Grid Strike Bot - Binance Demo",
              font=("", 12, "bold")).pack(anchor="w")
    ttk.Label(outer, foreground="gray",
              text="start both services, then use the dashboard in your browser"
              ).pack(anchor="w", pady=(0, 8))

    table = ttk.Frame(outer)
    table.pack(fill="x")
    for col, text in enumerate(["Service", "Port", "PID", "Status", "", ""]):
        ttk.Label(table, text=text, font=("", 9, "bold")).grid(
            row=0, column=col, padx=6, sticky="w")

    rows = {}
    pending_browser_open = [False]

    def make_row(svc, row_index):
        ttk.Label(table, text=svc.label).grid(row=row_index, column=0,
                                              padx=6, pady=4, sticky="w")
        ttk.Label(table, text=str(svc.port)).grid(row=row_index, column=1, padx=6)
        pid_label = ttk.Label(table, text="-", width=8)
        pid_label.grid(row=row_index, column=2, padx=6)
        light = tk.Canvas(table, width=14, height=14, highlightthickness=0)
        dot = light.create_oval(2, 2, 12, 12, fill="#b0b0b0", outline="")
        light.grid(row=row_index, column=3, padx=6)

        def toggle():
            # immediate feedback: dim the button until the health poll confirms
            toggle_btn.configure(text="...", state="disabled")
            if svc.running:
                svc.stop(log)
            else:
                svc.start(log)
                if svc.key == "web":
                    pending_browser_open[0] = True

        toggle_btn = ttk.Button(table, text="Start", width=7, command=toggle)
        toggle_btn.grid(row=row_index, column=4, padx=4)
        open_btn = None
        if svc.admin_url:
            open_btn = ttk.Button(table, text="Open dashboard", state="disabled",
                                  command=lambda: webbrowser.open(svc.admin_url))
            open_btn.grid(row=row_index, column=5, padx=4)
        rows[svc.key] = {"pid": pid_label, "light": light, "dot": dot,
                         "toggle": toggle_btn, "open": open_btn}

    for i, svc in enumerate(services):
        make_row(svc, i + 1)

    controls = ttk.Frame(outer)
    controls.pack(fill="x", pady=8)

    def start_all():
        for svc in services:
            svc.start(log)
        pending_browser_open[0] = True

    def stop_all():
        for svc in reversed(services):
            svc.stop(log)

    ttk.Button(controls, text="Start all", command=start_all).pack(side="left")
    ttk.Button(controls, text="Stop all", command=stop_all).pack(side="left", padx=6)

    log_frame = ttk.LabelFrame(outer, text=" Service log ", padding=4)
    log_frame.pack(fill="both", expand=True)
    log_pane = scrolledtext.ScrolledText(
        log_frame, height=12, state="disabled",
        font=("Consolas", 9) if sys.platform == "win32" else ("Menlo", 11))
    log_pane.pack(fill="both", expand=True)

    def drain_logs():
        try:
            while True:
                line = log_queue.get_nowait()
                log_pane.configure(state="normal")
                log_pane.insert("end", line + "\n")
                log_pane.see("end")
                log_pane.configure(state="disabled")
        except queue.Empty:
            pass
        root.after(150, drain_logs)

    def poll_health():
        for svc in services:
            widgets = rows[svc.key]
            healthy = svc.check_health()
            running = svc.running
            color = "#2e9e5b" if healthy else ("#e0a03c" if running else "#b0b0b0")
            widgets["light"].itemconfigure(widgets["dot"], fill=color)
            widgets["pid"].configure(text=str(svc.proc.pid) if running else "-")
            if running and healthy:
                widgets["toggle"].configure(text="Stop", state="normal")
            elif not running:
                widgets["toggle"].configure(text="Start", state="normal")
            # else: mid-transition — leave the "..." from the click in place
            if widgets["open"]:
                widgets["open"].configure(state="normal" if healthy else "disabled")
        web = next(s for s in services if s.key == "web")
        if pending_browser_open[0] and web.healthy:
            pending_browser_open[0] = False
            webbrowser.open(web.admin_url)      # the XAMPP-style redirect
        root.after(1200, poll_health)

    def on_close():
        if any(s.running for s in services):
            if not messagebox.askyesno(
                    "Services running",
                    "Stop the web UI and bot API (and any running bot) and quit?"):
                return
            stop_all()
            root.after(400, wait_and_exit)
        else:
            root.destroy()

    def wait_and_exit(tries=(50,)):
        if any(s.running for s in services) and tries[0] > 0:
            tries = (tries[0] - 1,)
            root.after(400, lambda: wait_and_exit(tries))
        else:
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.after(150, drain_logs)
    root.after(300, poll_health)
    root.mainloop()


# ------------------------------------------------------------------ dispatch

def main():
    if "--service" in sys.argv:
        i = sys.argv.index("--service")
        name = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        rest = sys.argv[1:i] + sys.argv[i + 2:]
        sys.path.insert(0, HERE)
        if name == "api":
            import api_server
            sys.argv = ["api_server"] + rest
            api_server.main()
        elif name == "web":
            import web_server
            sys.argv = ["web_server"] + rest
            web_server.main()
        elif name == "bot":
            import bot
            sys.argv = ["bot"] + rest
            bot.main()
        else:
            sys.exit(f"unknown --service {name!r} (use api, web, or bot)")
        return
    run_gui()


if __name__ == "__main__":
    main()
