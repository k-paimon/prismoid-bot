"""
Web UI server (pure stdlib) — serves the bot dashboard at http://localhost:8800.

The page (web/index.html) is a static single file whose JavaScript talks to the
bot backend API on port 8801 (api_server.py). Both are meant to be started from
the tkinter launcher (launcher.py), XAMPP-style.

  py web_server.py [--port 8800]
"""
import argparse
import functools
import os
import signal
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# frozen (.exe/.app) builds carry web/ inside the PyInstaller bundle
_BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(_BASE, "web")


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8800)
    args = parser.parse_args()

    if hasattr(signal, "SIGBREAK"):       # graceful stop from the launcher
        signal.signal(signal.SIGBREAK, signal.default_int_handler)

    handler = functools.partial(QuietHandler, directory=WEB_DIR)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)

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

    print(f"web UI listening on http://localhost:{args.port} (serving {WEB_DIR})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("web UI shutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
