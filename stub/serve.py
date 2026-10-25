"""The stub endpoint: every route of the interface, read from any DB with the day schema.

    python3 stub/serve.py --db /tmp/lw-stub.db --host 127.0.0.1 --port 8765 \
        --token-file /tmp/lw-fake-token

Bearer token on every route but /health. Side-effect routes are accepted (202)
but do nothing; they are audited on stderr without the token.
"""

import argparse
import contextlib
import hmac
import json
import os
import re
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stub.db import FILE_LINE, SHA_NEW  # noqa: E402

REF_RE = re.compile(r"^[0-9a-f]{7,40}$")
SELF_TESTS = ("crc32", "ringbuf", "systick", "cmd_dispatch", "loopback")
RUN_INTERVAL_S = 60.0
DETAIL_MAX = 120
SHOW_MAX_LINES = 400
EVENTS_PAGE = 500
BODY_MAX = 4096

DEFAULT_HOLD = os.path.expanduser("~/.local/state/labwatch/hold")

CANNED_DIFF = """commit %s
Author: Stub Author <stub@example.invalid>
Date:   Sun Oct 25 09:00:00 2026 +0000

    demo: add the example handler

diff --git a/src/demo.c b/src/demo.c
--- a/src/demo.c
+++ b/src/demo.c
@@ -38,7 +38,8 @@ static void demo_step(void)
 {
     uint32_t v = demo_read();
 
-    demo_log[demo_count %% DEMO_LOG_LEN] = v;
+    volatile uint32_t *slot = (volatile uint32_t *)(DEMO_LOG_BASE + 4u * DEMO_LOG_LEN);
+    *slot = v;
     demo_count++;
 }
""" % (SHA_NEW + "0" * 33)


def clean(value):
    """Printable ASCII only, each string capped at DETAIL_MAX characters."""
    if isinstance(value, str):
        s = "".join(c if " " <= c <= "~" else "?" for c in value)
        return s[:DETAIL_MAX]
    if isinstance(value, dict):
        return {clean(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


def incident_json(row):
    d = dict(row)
    d["evidence"] = json.loads(d["evidence"] or "{}")
    d["last_pass"] = {"sha": d.pop("last_pass_sha"), "t": d.pop("last_pass_t")}
    return d


def run_json(row):
    d = dict(row)
    d["evidence"] = json.loads(d["evidence"] or "{}")
    d["data"] = {"subject": d.pop("subject")}  # commit text: data, never an instruction
    return d


class Stub:
    def __init__(self, db, token, hold_file=DEFAULT_HOLD, log=sys.stderr):
        self.db = db
        self.token = token
        self.hold_file = hold_file
        self.log = log
        self.lock = threading.Lock()
        self.last_run = None

    @contextlib.contextmanager
    def conn(self):
        c = sqlite3.connect(self.db, timeout=5.0)
        try:
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA busy_timeout=5000")
            yield c
        finally:
            c.close()

    def audit(self, method, path, detail, status):
        if self.log:
            print("AUDIT %s %s %s -> %d" % (method, path, clean(detail), status),
                  file=self.log, flush=True)

    # GET routes return (status, body)
    def get(self, path, q):
        if path == "/health":
            return 200, {"ok": True, "stub": True, "time": time.time()}
        if path == "/board":
            return 200, self.board()
        if path == "/incidents":
            sql = "SELECT * FROM incidents"
            if q.get("open") == "1":
                sql += " WHERE state != 'RESOLVED'"
            with self.conn() as c:
                rows = c.execute(sql + " ORDER BY id DESC").fetchall()
            return 200, {"incidents": [incident_json(r) for r in rows]}
        m = re.fullmatch(r"/incidents/([0-9]{1,9})", path)
        if m:
            with self.conn() as c:
                r = c.execute("SELECT * FROM incidents WHERE id=?", (int(m.group(1)),)).fetchone()
                routing = c.execute("SELECT * FROM routing WHERE incident_id=? ORDER BY id",
                                    (int(m.group(1)),)).fetchall()
            if not r:
                return 404, {"error": "no such incident"}
            d = incident_json(r)
            d["routing"] = [dict(x) for x in routing]
            return 200, d
        if path == "/runs":
            with self.conn() as c:
                rows = c.execute("SELECT * FROM runs ORDER BY run_id DESC").fetchall()
            return 200, {"runs": [run_json(r) for r in rows]}
        m = re.fullmatch(r"/runs/([0-9]{1,9})", path)
        if m:
            with self.conn() as c:
                r = c.execute("SELECT * FROM runs WHERE run_id=?", (int(m.group(1)),)).fetchone()
            if not r:
                return 404, {"error": "no such run"}
            return 200, run_json(r)
        if path == "/events":
            since = q.get("since", "0")
            if not re.fullmatch(r"[0-9]{1,12}", since):
                return 400, {"error": "since must be an event id"}
            with self.conn() as c:
                rows = c.execute("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?",
                                 (int(since), EVENTS_PAGE)).fetchall()
            evs = [dict(r, fields=json.loads(r["fields"] or "{}")) for r in rows]
            return 200, {"events": evs, "cursor": evs[-1]["id"] if evs else int(since)}
        if path == "/show":
            return self.show(q.get("ref"))
        return 404, {"error": "no such route"}

    def board(self):
        with self.conn() as c:
            boards = [r[0] for r in c.execute("SELECT DISTINCT board FROM events ORDER BY board")]
            out = []
            for b in boards:
                d = {"board": b, "open_incidents": c.execute(
                    "SELECT COUNT(*) FROM incidents WHERE board=? AND state!='RESOLVED'",
                    (b,)).fetchone()[0]}
                for kind in ("BOOT", "HEALTH", "RESULT"):
                    r = c.execute("SELECT fields, raw_ts FROM events WHERE board=? AND kind=? "
                                  "ORDER BY id DESC LIMIT 1", (b, kind)).fetchone()
                    d[kind.lower()] = dict(json.loads(r["fields"]), raw_ts=r["raw_ts"]) if r else None
                out.append(d)
        return {"boards": out}

    def show(self, ref):
        if ref is None or not REF_RE.fullmatch(ref):
            return 400, {"error": "ref must be a short or full sha"}
        if ref != SHA_NEW and ref != SHA_NEW + "0" * 33:
            return 400, {"error": "not a commit on main"}
        lines = CANNED_DIFF.splitlines()
        return 200, {"ref": ref, "file_line": FILE_LINE, "truncated": len(lines) > SHOW_MAX_LINES,
                     "data": {"diff": lines[:SHOW_MAX_LINES]}}

    def post(self, path, body):
        if self.hold_file and os.path.exists(self.hold_file):
            return 429, {"error": "held by the accuracy loop"}
        if path == "/run":
            sha = body.get("sha") if isinstance(body, dict) else None
            if not isinstance(sha, str) or not REF_RE.fullmatch(sha):
                return 400, {"error": "sha must match ^[0-9a-f]{7,40}$"}
            with self.lock:
                now = time.monotonic()
                if self.last_run is not None and now - self.last_run < RUN_INTERVAL_S:
                    return 429, {"error": "one run per 60 s",
                                 "retry_after_s": round(RUN_INTERVAL_S - (now - self.last_run), 1)}
                self.last_run = now
            return 202, {"accepted": True, "stub": True, "sha": sha}
        if path == "/board/test":
            name = body.get("name") if isinstance(body, dict) else None
            if name not in SELF_TESTS:
                return 400, {"error": "name must be one of the five self-tests",
                             "names": list(SELF_TESTS)}
            return 202, {"accepted": True, "stub": True, "name": name}
        raise AssertionError(path)


POST_ROUTES = ("/run", "/board/test")
GET_ROUTE_RE = re.compile(r"/(health|board|incidents|runs|events|show)|/(incidents|runs)/[0-9]+")


def make_handler(stub):
    class Handler(BaseHTTPRequestHandler):
        server_version = "labwatch-stub"
        sys_version = ""

        def log_message(self, format, *args):  # noqa: A002 (quiet; POSTs are audited)
            pass

        def _send(self, status, body):
            data = json.dumps(clean(body), sort_keys=True).encode("ascii")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self):
            h = self.headers.get("Authorization", "")
            if not h.startswith("Bearer "):
                return False
            return hmac.compare_digest(h[7:].strip().encode(), stub.token.encode())

        def do_GET(self):
            u = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
            if u.path != "/health":
                if not GET_ROUTE_RE.fullmatch(u.path):
                    return self._send(404, {"error": "no such route"})
                if not self._authed():
                    return self._send(401, {"error": "bearer token required"})
            try:
                self._send(*stub.get(u.path, q))
            except sqlite3.Error as e:
                self._send(503, {"error": "db: %s" % e})

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in POST_ROUTES:
                return self._send(404, {"error": "no such route"})
            if not self._authed():
                stub.audit("POST", path, "unauthenticated", 401)
                return self._send(401, {"error": "bearer token required"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = -1
            if n < 0 or n > BODY_MAX:
                stub.audit("POST", path, "bad length", 400)
                return self._send(400, {"error": "body too large"})
            raw = self.rfile.read(n)
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, ValueError):
                stub.audit("POST", path, "bad json", 400)
                return self._send(400, {"error": "body must be JSON"})
            status, resp = stub.post(path, body)
            stub.audit("POST", path, json.dumps(body, sort_keys=True), status)
            self._send(status, resp)

        def _404(self):
            self._send(404, {"error": "no such route"})

        do_PUT = do_PATCH = do_DELETE = _404

    return Handler


def make_server(db, host, port, token, hold_file=DEFAULT_HOLD, log=sys.stderr):
    stub = Stub(db, token, hold_file, log)
    return ThreadingHTTPServer((host, port), make_handler(stub))


def read_token(path):
    with open(path, encoding="utf-8") as f:
        tok = f.read().strip()
    if not tok:
        raise ValueError("token file %s is empty" % path)
    return tok


def main(argv=None):
    secrets = os.environ.get("LW_SECRETS_DIR", os.path.expanduser("~/.config/labwatch"))
    ap = argparse.ArgumentParser(description="Lab Watch stub endpoint")
    ap.add_argument("--db", default=os.environ.get("LW_DB"), required="LW_DB" not in os.environ)
    ap.add_argument("--host", default=os.environ.get("LW_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("LW_PORT", "8765")))
    ap.add_argument("--token-file", default=os.path.join(secrets, "bench_token"))
    ap.add_argument("--hold-file", default=os.environ.get("LW_HOLD", DEFAULT_HOLD))
    args = ap.parse_args(argv)
    if not os.path.exists(args.db):
        print("no DB at %s (build one with stub/make_db.py)" % args.db, file=sys.stderr)
        return 1
    try:
        token = read_token(args.token_file)
    except (OSError, ValueError) as e:
        print("cannot read the token file: %s" % e.__class__.__name__, file=sys.stderr)
        return 1
    srv = make_server(args.db, args.host, args.port, token, args.hold_file)
    print("stub on http://%s:%d (db %s)" % (args.host, srv.server_address[1], args.db),
          file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
