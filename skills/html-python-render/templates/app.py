#!/usr/bin/env python3
"""
app.py — serves index.html and an /api/, from one file.

Runs the same on a laptop and on a host. The only thing that changes is PORT:
when a platform sets it, this listens on every interface because the platform
has already decided the service is public and put its own proxy in front.

    python app.py                 # loopback, opens a browser
    python app.py --lan           # also answer phones on the same Wi-Fi
    PORT=10000 python app.py      # what Render and friends do

Replace do_work() with the real job. Everything else is the scaffolding that
took a while to get right the first time; the comments say why each piece is
there rather than what it does.
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

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVICE = "my-app"          # the page checks for this exact string

# Serve an allowlist, never a folder. SimpleHTTPRequestHandler pointed at a
# directory hands out everything in it, including .git and a browsable index of
# every subdirectory. Nothing may be secret today, but a private file dropped
# in here tomorrow would be public the moment it lands.
SERVABLE_FILES = {"/", "/index.html", "/favicon.ico"}
SERVABLE_DIRS = ("/data/",)

JOB_TTL = 900               # seconds to keep a finished job for collection
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# the work
# ---------------------------------------------------------------------------
def do_work(params: dict, report) -> dict:
    """Replace this. `report(pct, stage)` drives the page's progress bar.

    Report real progress. When downloading and the upstream sends no
    Content-Length — many CDNs stream chunked — use got/(got+k), which always
    advances and never claims a total it does not know.
    """
    total = 20
    for i in range(total):
        time.sleep(0.1)
        report(5 + 90 * (i + 1) / total, f"step {i + 1} of {total}")
    return {"echo": params, "ok": True}


# ---------------------------------------------------------------------------
# jobs, so the page can show progress instead of freezing
# ---------------------------------------------------------------------------
def _prune_jobs():
    now = time.time()
    with JOBS_LOCK:
        for k in [k for k, v in JOBS.items() if now - v["touched"] > JOB_TTL]:
            JOBS.pop(k, None)


def _run_job(job_id: str, params: dict):
    def report(pct, stage):
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if j:
                # never go backwards, and never reach 100 before there is a result
                j["pct"] = max(j["pct"], min(99, int(pct)))
                j["stage"] = stage
                j["touched"] = time.time()

    try:
        payload = do_work(params, report)
    except Exception as e:                                      # noqa: BLE001
        with JOBS_LOCK:
            JOBS[job_id].update(done=True, pct=100, stage="failed",
                                error=f"{type(e).__name__}: {e}", touched=time.time())
        print(f"  job failed: {type(e).__name__}: {e}", file=sys.stderr)
        return
    with JOBS_LOCK:
        JOBS[job_id].update(done=True, pct=100, stage="done",
                            payload=payload, touched=time.time())


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def _is_servable(self, path: str) -> bool:
        path = path.split("?")[0]
        if path in SERVABLE_FILES:
            return True
        if not path.startswith(SERVABLE_DIRS):
            return False
        # A directory prefix is not a licence to walk upwards out of it.
        return ".." not in path and not path.endswith("/")

    # A directory listing is a map of everything you did not mean to publish.
    def list_directory(self, path):
        self.send_error(404, "Not found")
        return None

    # Without this the browser keeps showing the old page after a deploy,
    # which is indistinguishable from the deploy having failed.
    def end_headers(self):
        if (self.path or "").split("?")[0].endswith((".html", "/", ".js", ".css")):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    # One line per data request, nothing for assets.
    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            print("  " + (fmt % args), file=sys.stderr)

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
            return self._json(200, {"ok": True, "service": SERVICE})
        if parts.path == "/api/start":
            return self._start(urllib.parse.parse_qs(parts.query))
        if parts.path == "/api/progress":
            return self._progress(urllib.parse.parse_qs(parts.query))
        if not self._is_servable(parts.path):
            return self.send_error(404, "Not found")
        return super().do_GET()

    def _start(self, q):
        params = {k: v[0] for k, v in q.items()}
        _prune_jobs()
        job_id = uuid.uuid4().hex
        with JOBS_LOCK:
            JOBS[job_id] = {"pct": 1, "stage": "starting", "done": False,
                            "touched": time.time()}
        threading.Thread(target=_run_job, args=(job_id, params), daemon=True).start()
        return self._json(200, {"id": job_id})

    def _progress(self, q):
        job_id = (q.get("id") or [""])[0]
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if not j:
                return self._json(404, {"error": "That job is gone. Try again."})
            j["touched"] = time.time()
            out = {"pct": j["pct"], "stage": j["stage"], "done": j["done"]}
            if j["done"]:
                if "error" in j:
                    out["error"] = j["error"]
                else:
                    out["payload"] = j["payload"]
                JOBS.pop(job_id, None)      # collected once, then forgotten
        return self._json(200, out)


class _Sink:
    def write(self, *a):
        return 0

    def flush(self):
        pass


def _open_output():
    """pythonw.exe runs with no console, so stdout and stderr are None.

    Every write then raises. Because the only writes are in the request path,
    static files keep serving while every /api/ call dies with an empty
    response — which from the page looks exactly like the backend being absent,
    and sends people off starting a server that is already running.

    A shell passes a console through, so this never reproduces when testing by
    hand. Use Start-Process -WindowStyle Hidden to get the real condition.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log = open(os.path.join(ROOT, "app.log"), "a", encoding="utf-8", buffering=1)
    except OSError:
        log = _Sink()
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


def main() -> int:
    _open_output()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

    # Every host hands the port over in the environment and expects every
    # interface. Honouring that is usually the only change a local tool needs
    # to become deployable.
    env_port = os.environ.get("PORT")
    hosted = bool(env_port)

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(env_port) if env_port else 8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--lan", action="store_true",
                    help="answer other devices on your network, so a phone can use it")
    ap.add_argument("--host", default=None)
    a = ap.parse_args()

    # Loopback by default: this is a helper for you, not for the network. --lan
    # is fine at home and a bad idea on shared Wi-Fi, since it serves this
    # folder and will work for anyone who asks.
    host = a.host or ("0.0.0.0" if (hosted or a.lan) else "127.0.0.1")
    try:
        srv = ThreadingHTTPServer((host, a.port), Handler)
    except OSError as e:
        print(f"Could not listen on {host}:{a.port}: {e}", file=sys.stderr)
        return 1

    if hosted:
        print(f"{SERVICE} listening on {host}:{a.port} (PORT was set)", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    url = f"http://127.0.0.1:{a.port}/index.html"
    print(f"{SERVICE}\n  {url}")
    if a.lan:
        import socket as _s
        probe = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        try:
            probe.connect(("10.255.255.255", 1))    # nothing is sent; this picks the route
            print(f"  http://{probe.getsockname()[0]}:{a.port}/index.html   <- on your phone")
        except OSError:
            pass
        finally:
            probe.close()
    print("  Ctrl+C to stop.\n")
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
