#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Arjun Bhagavatula
"""SamRegionHop UI: a local web page that drives samregionhop.py.

The page only starts samregionhop.py commands and shows their output; every safety
check stays in samregionhop.py. The UI adds two gates of its own before a real
change: a passing dry run for the same CSC in this session, and a checklist.

The server listens on 127.0.0.1 only, and every request must carry a random
per-launch token, so no other web page can drive it.
"""

import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.environ.get("SAMREGIONHOP_CLI", os.path.join(HERE, "samregionhop.py"))
CSC_RE = re.compile(r"[A-Z0-9]{3}")
PROMPT_RE = re.compile(r"Type ([A-Z0-9]{3}) to change the CSC \(anything else aborts\): $")
CHECKLIST = ("backup", "passwords", "firmware")
NEEDS_CSC = ("fota", "dryrun", "change")  # steps that act on a target region


class Job:
    def __init__(self, job_id, kind, csc, argv, on_exit):
        self.id, self.kind, self.csc, self.on_exit = job_id, kind, csc, on_exit
        self.text, self.code = "", None
        self.lock = threading.Lock()
        # Windows: own process group, so Stop can send Ctrl-Break to the tool alone.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, bufsize=0, creationflags=flags)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while True:
            chunk = self.proc.stdout.read(4096)  # raw read: returns as soon as data arrives
            if not chunk:
                break
            with self.lock:
                self.text += chunk.decode(errors="replace")
        code = self.proc.wait()
        self.on_exit(self, code)
        with self.lock:
            self.code = code

    @property
    def running(self):
        return self.code is None

    def awaiting(self):
        """The CSC the CLI is asking the user to type, if it is waiting for that."""
        with self.lock:
            m = PROMPT_RE.search(self.text) if self.running else None
        return m.group(1) if m else None


class State:
    def __init__(self):
        self.job = None
        self.next_id = 1
        self.dryrun_ok = set()
        self.lock = threading.Lock()

    def start(self, kind, csc, checklist):
        with self.lock:
            if self.job and self.job.running:
                return "another step is still running"
            py = [sys.executable, "-u", CLI]
            if kind == "info":
                argv = py + ["info"]
            elif kind == "targets":
                argv = py + ["targets"]
            elif kind == "report":
                argv = py + ["report"]
            elif kind == "fota":
                argv = py + ["fota", csc]  # the CLI adds the phone's own region
            elif kind == "probe":
                argv = py + ["probe"]
            elif kind == "dryrun":
                argv = py + ["change", csc, "--dry-run"]
            elif kind == "change":
                if csc not in self.dryrun_ok:
                    return f"run the dry run for {csc} first"
                if not all(checklist.get(k) is True for k in CHECKLIST):
                    return "tick every item on the checklist first"
                argv = py + ["change", csc]
            else:
                return "unknown step"
            self.job = Job(self.next_id, kind, csc, argv, self._finished)
            self.next_id += 1
            return None

    def _finished(self, job, code):
        with self.lock:
            if job.kind == "dryrun" and code == 0:
                self.dryrun_ok.add(job.csc)
            if job.kind == "change":
                self.dryrun_ok.discard(job.csc)  # a new attempt needs a fresh dry run

    def snapshot(self, since):
        with self.lock:
            job, ok = self.job, sorted(self.dryrun_ok)
        if not job:
            return {"job": None, "dryrun_ok": ok}
        with job.lock:
            text, code = job.text, job.code
        return {"job": job.id, "kind": job.kind, "csc": job.csc, "running": code is None,
                "code": code, "len": len(text), "text": text[since:],
                "awaiting": job.awaiting(), "dryrun_ok": ok}


def make_handler(state, token, page, regions):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _allowed(self, query_token=None):
            host = self.headers.get("Host", "")
            port = self.server.server_address[1]
            if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                return False  # DNS-rebinding guard
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                return False
            given = query_token or self.headers.get("X-Token", "")
            return secrets.compare_digest(given, token)

        def _send(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if url.path == "/":
                if not self._allowed(q.get("t", [""])[0]):
                    return self._send(403, "Forbidden", "text/plain")
                return self._send(200, page, "text/html; charset=utf-8")
            if url.path == "/regions":
                if not self._allowed():
                    return self._send(403, {"error": "forbidden"})
                return self._send(200, regions)
            if url.path == "/state":
                if not self._allowed():
                    return self._send(403, {"error": "forbidden"})
                try:
                    since = max(0, int(q.get("since", ["0"])[0]))
                except ValueError:
                    since = 0
                return self._send(200, state.snapshot(since))
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._allowed():
                return self._send(403, {"error": "forbidden"})
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 10000)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._send(400, {"error": "bad request"})
            path = urlparse(self.path).path
            if path == "/run":
                kind, csc = str(body.get("kind")), str(body.get("csc") or "").upper()
                if kind in NEEDS_CSC and not CSC_RE.fullmatch(csc):
                    return self._send(400, {"error": "pick a target region first"})
                err = state.start(kind, csc if kind in NEEDS_CSC else "", body.get("checklist") or {})
                return self._send(409 if err else 200, {"error": err} if err else {"ok": True})
            if path == "/input":
                job = state.job
                if not job or not job.awaiting():
                    return self._send(409, {"error": "nothing is waiting for input"})
                text = str(body.get("text", ""))[:20].replace("\n", " ").replace("\r", " ")
                job.proc.stdin.write((text + "\n").encode())
                job.proc.stdin.flush()
                return self._send(200, {"ok": True})
            if path == "/stop":
                job = state.job
                if job and job.running:
                    # The CLI turns either signal into its safe Ctrl-C handling.
                    job.proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32"
                                         else signal.SIGINT)
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

    return Handler


def serve(port=0, open_browser=True):
    token = secrets.token_urlsafe(24)
    with open(os.path.join(HERE, "ui.html")) as f:
        page = f.read()
    try:
        with open(os.path.join(HERE, "regions.json"), encoding="utf-8") as f:
            regions = json.load(f)["regions"]
    except (OSError, ValueError, KeyError):
        regions = {}
    state = State()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state, token, page, regions))
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={token}"
    return server, url, state


def main():
    server, url, _ = serve()
    print(f"SamRegionHop is open in your browser. If it isn't: {url}\nPress Ctrl-C here to quit.")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
