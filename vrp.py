#!/usr/bin/env python3
"""
vrp.py -- run this once, then just type a ticker and press Calculate.

    python vrp.py

It serves the analyzer from your own machine and opens it in a browser. The
page then has a ticker box: type AAPL, press Calculate VRP, and everything
fills in. No files to move around.

Why a local helper exists at all: browsers refuse cross-origin requests to
cdn.cboe.com, Yahoo and Stooq, because none of them send CORS headers. A page
on github.io simply cannot read them, whatever the code says. So the page asks
this helper, which lives on the same origin, and the helper does the fetching.

The analyzer still makes no third-party request of its own. Open index.html
straight from disk or from GitHub Pages and it behaves exactly as before:
load a CSV or an inputs JSON by hand. The ticker box only lights up when this
helper is running.

    python vrp.py --port 8765 --no-browser
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.parse
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(REPO, "fetch"))

import fetch_inputs  # noqa: E402


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=REPO, **kw)

    # keep the console readable: one line per data request, nothing for assets
    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            sys.stderr.write("  %s\n" % (fmt % args))

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        if parts.path == "/api/health":
            return self._json(200, {"ok": True, "service": "vrp-local-helper"})
        if parts.path == "/api/inputs":
            return self._inputs(urllib.parse.parse_qs(parts.query))
        return super().do_GET()

    def _inputs(self, q):
        def one(name, cast, default):
            v = (q.get(name) or [None])[0]
            if v in (None, ""):
                return default
            try:
                return cast(v)
            except ValueError:
                return default

        symbol = (q.get("symbol") or [""])[0].strip().upper()
        if not symbol:
            return self._json(400, {"error": "No ticker given."})
        if not all(c.isalnum() or c in "._-^" for c in symbol):
            return self._json(400, {"error": f"{symbol!r} does not look like a ticker."})

        dte = one("dte", int, 38)
        width = one("width", float, 5.0)
        delta = one("delta", float, 0.25)
        period = (q.get("period") or ["2y"])[0]

        sys.stderr.write(f"  fetching {symbol} (target {dte} DTE) ...\n")
        try:
            payload = fetch_inputs.build(
                symbol, dte, width, delta, period,
                os.path.join(REPO, "fetch", "iv_history"))
        except fetch_inputs.FetchError as e:
            return self._json(404, {"error": str(e)})
        except Exception as e:                                   # noqa: BLE001
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})

        i = payload["inputs"]
        filled = sum(1 for v in i.values() if v is not None)
        sys.stderr.write(f"  {symbol}: {filled}/{len(i)} fields, {len(payload['ohlc'])} price rows\n")
        return self._json(200, payload)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    # 127.0.0.1 rather than 0.0.0.0: this is a helper for you, not for the network
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError as e:
        print(f"Could not listen on port {a.port}: {e}", file=sys.stderr)
        print("Something else is probably using it. Try --port 8766.", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{a.port}/index.html"
    print("VRP Stock Analyzer")
    print(f"  {url}")
    print("  Type a ticker, press Calculate VRP. Ctrl+C to stop.\n")
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
