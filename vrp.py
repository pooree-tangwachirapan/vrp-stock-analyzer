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
import time
import urllib.parse
import uuid
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(REPO, "fetch"))

import fetch_inputs  # noqa: E402


# Jobs, so the page can show a real progress bar instead of a spinner. The
# fetch runs on its own thread and reports where it has got to; the page polls.
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOB_TTL = 900          # seconds to keep a finished job around for collection


def _prune_jobs():
    now = time.time()
    with JOBS_LOCK:
        for k in [k for k, v in JOBS.items() if now - v["touched"] > JOB_TTL]:
            JOBS.pop(k, None)


def _run_job(job_id: str, symbol: str, dte: int, width: float, delta: float, period: str):
    def report(pct, stage):
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if j:
                # never go backwards, and never claim completion before there is a result
                j["pct"] = max(j["pct"], min(99, int(pct)))
                j["stage"] = stage
                j["touched"] = time.time()

    try:
        payload = fetch_inputs.build(
            symbol, dte, width, delta, period,
            os.path.join(REPO, "fetch", "iv_history"), progress=report)
    except fetch_inputs.FetchError as e:
        with JOBS_LOCK:
            JOBS[job_id].update(done=True, pct=100, stage="failed",
                                error=str(e), touched=time.time())
        print(f"  {symbol}: {e}", file=sys.stderr)
        return
    except Exception as e:                                       # noqa: BLE001
        with JOBS_LOCK:
            JOBS[job_id].update(done=True, pct=100, stage="failed",
                                error=f"{type(e).__name__}: {e}", touched=time.time())
        print(f"  {symbol}: {type(e).__name__}: {e}", file=sys.stderr)
        return

    i = payload["inputs"]
    filled = sum(1 for v in i.values() if v is not None)
    print(f"  {symbol}: {filled}/{len(i)} fields, {len(payload['ohlc'])} price rows", file=sys.stderr)
    with JOBS_LOCK:
        JOBS[job_id].update(done=True, pct=100, stage="done",
                            payload=payload, touched=time.time())


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=REPO, **kw)

    # Serve the page fresh. Without this the browser keeps showing a cached
    # index.html after the file changes, which looks exactly like a bug.
    def end_headers(self):
        if (self.path or "").split("?")[0].endswith((".html", "/", ".js", ".css")):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

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
        if parts.path == "/api/start":
            return self._start(urllib.parse.parse_qs(parts.query))
        if parts.path == "/api/progress":
            return self._progress(urllib.parse.parse_qs(parts.query))
        return super().do_GET()

    def _start(self, q):
        try:
            symbol, dte, width, delta, period = self._args(q)
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        _prune_jobs()
        job_id = uuid.uuid4().hex
        with JOBS_LOCK:
            JOBS[job_id] = {"pct": 1, "stage": "starting", "done": False,
                            "symbol": symbol, "touched": time.time()}
        threading.Thread(target=_run_job, daemon=True,
                         args=(job_id, symbol, dte, width, delta, period)).start()
        print(f"  fetching {symbol} (target {dte} DTE) ...", file=sys.stderr)
        return self._json(200, {"id": job_id})

    def _progress(self, q):
        job_id = (q.get("id") or [""])[0]
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if not j:
                return self._json(404, {"error": "That job is gone. Press Calculate again."})
            j["touched"] = time.time()
            out = {"pct": j["pct"], "stage": j["stage"], "done": j["done"]}
            if j["done"]:
                if "error" in j:
                    out["error"] = j["error"]
                else:
                    out["payload"] = j["payload"]
                JOBS.pop(job_id, None)
        return self._json(200, out)

    def _args(self, q):
        symbol = (q.get("symbol") or [""])[0].strip().upper()
        if not symbol:
            raise ValueError("No ticker given.")
        if not all(c.isalnum() or c in "._-^" for c in symbol):
            raise ValueError(f"{symbol!r} does not look like a ticker.")

        def one(name, cast, default):
            v = (q.get(name) or [None])[0]
            if v in (None, ""):
                return default
            try:
                return cast(v)
            except ValueError:
                return default

        return (symbol, one("dte", int, 38), one("width", float, 5.0),
                one("delta", float, 0.25), (q.get("period") or ["2y"])[0])

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


class _Sink:
    """Somewhere harmless to write when there is no console."""
    def write(self, *a):
        return 0

    def flush(self):
        pass


def _open_output():
    """pythonw.exe runs without a console, so sys.stdout and sys.stderr are None.

    Every write then raises, and because the only writes sit in the request
    path, static files kept working while every /api/ call died with an empty
    response. From the page that looked exactly like the helper being absent,
    which sent people off starting a server that was already running.

    Send it to a file instead. That fixes the crash and gives somewhere to look
    when it is running with no window.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log = open(os.path.join(REPO, "vrp.log"), "a", encoding="utf-8", buffering=1)
    except OSError:
        log = _Sink()
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


def main() -> int:
    _open_output()
    # The Python reference carries Thai gate text, which the Windows console
    # renders as mojibake under the legacy codepage.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # Every host that runs a web process hands it a port in the environment and
    # expects it to listen on every interface. Honouring that is the whole
    # change needed to put this online: the same file serves the page and the
    # API, so nothing above it has to know where it is running.
    env_port = os.environ.get("PORT")
    hosted = bool(env_port)

    ap.add_argument("--port", type=int, default=int(env_port) if env_port else 8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--lan", action="store_true",
                    help="also answer other devices on your network, so a phone can use it")
    ap.add_argument("--host", default=None,
                    help="interface to bind; defaults to loopback, or 0.0.0.0 when PORT is set")
    a = ap.parse_args()

    # Loopback by default: this is a helper for you, not for the network. --lan
    # opens it to the rest of the Wi-Fi so a phone can reach it, which is fine on
    # a home network and a bad idea on a shared one -- it serves this folder and
    # will fetch quotes for anyone who asks. A platform that set PORT has already
    # decided it is public and put its own proxy in front.
    host = a.host or ("0.0.0.0" if (a.lan or hosted) else "127.0.0.1")
    try:
        srv = ThreadingHTTPServer((host, a.port), Handler)
    except OSError as e:
        print(f"Could not listen on port {a.port}: {e}", file=sys.stderr)
        print("Something else is probably using it. Try --port 8766.", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{a.port}/index.html"
    print("VRP Stock Analyzer")
    if hosted:
        print(f"  listening on {host}:{a.port} (PORT was set, so this is a hosted run)")
        print("  Type a ticker, press Calculate VRP.", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
    print(f"  {url}")
    if a.lan:
        import socket as _s
        probe = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        try:
            probe.connect(("10.255.255.255", 1))   # nothing is sent; this just picks the route
            lan_ip = probe.getsockname()[0]
        except OSError:
            lan_ip = None
        finally:
            probe.close()
        if lan_ip:
            print(f"  http://{lan_ip}:{a.port}/index.html   <- open this on your phone, same Wi-Fi")
        print("  Open to your local network. Fine at home, not on shared Wi-Fi.")
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
