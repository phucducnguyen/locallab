"""The bench endpoint: python3 -m bench.endpoint [--db PATH] [--host IP] [--port N]

Its own process. Serves the interface's routes on the docker bridge IP
(found at startup from `ip -4 -o addr show docker0`; LW_HOST overrides; on
the GB10 the NemoClaw sandbox reaches the host at 172.18.0.1, so start it
there with LW_HOST=172.18.0.1) and LW_PORT (8765).

- Bearer token from <secrets>/bench_token on every route but /health; 401.
- Every request goes into the endpoint_audit table (time, peer, method,
  route, status; never the token or a body beyond the parsed name or sha).
- Read-only on the bench's tables. The only writes: endpoint_audit and, for
  an accepted POST /run or POST /board/test, one bench_requests row that the
  bench process consumes (bench/requests.py). Nothing else changes state.
- /show?ref=: full match of ^[0-9a-f]{7,40}$, a commit, an ancestor of
  main of the bench repo ($LW_FW_REPO, ~/bench-fw); git show --no-decorate
  --no-ext-diff --end-of-options <sha> -- src/ as a list argv, 400 lines.
  The commit's own tree must hold no CASES.md, logs/ or bugs/ (main keeps the
  original history; its early trees hold the answer key). Anything else is 400.
- POST /board/test: one of the five self-tests, only once the board has
  printed LW RESULT (and one LW ALIVE) since its last boot. POST /run: a sha
  on main, at most one accepted per 60 s. Both 429 while the hold file
  exists ($LW_HOLD) or the board is busy (a request pending or running, a
  run in progress, a flash not yet verified, the link down, or the bench
  heartbeat older than 10 s, so nobody would act on it).
- Responses: parsed fields only, printable ASCII, every string capped at
  120 characters, no serial tail; commit text only under "data".

Same JSON shapes as stub/serve.py, so the agent, the tracker mirror and the scoring tools switch by URL only.
"""

import argparse
import hmac
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from bench import fwrepo, requests, store

REF_RE = re.compile(r"^[0-9a-f]{7,40}$")
SELF_TESTS = requests.SELF_TESTS
RUN_INTERVAL_S = 60.0
DETAIL_MAX = 120
SHOW_MAX_LINES = 400
EVENTS_PAGE = 500
BODY_MAX = 4096
BENCH_STALE_S = 10.0
DEFAULT_PORT = 8765
DEFAULT_HOLD = "~/.local/state/labwatch/hold"
DEFAULT_SECRETS = "~/.config/labwatch/"

AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS endpoint_audit (
    id      INTEGER PRIMARY KEY,
    t       REAL NOT NULL,
    peer    TEXT NOT NULL,
    method  TEXT NOT NULL,
    route   TEXT NOT NULL,
    status  INTEGER NOT NULL,
    detail  TEXT
);
"""

POST_ROUTES = ("/run", "/board/test")
BOARD_LINE_KINDS = "('BOOT','ALIVE','HEALTH','RESULT','TEST','REPLY','HARDFAULT')"
GET_ROUTE_RE = re.compile(r"/(health|board|incidents|runs|events|show)|/(incidents|runs)/[0-9]+")


def clean(value):
    """Printable ASCII only, each string capped at DETAIL_MAX characters."""
    if isinstance(value, str):
        return "".join(c if " " <= c <= "~" else "?" for c in value)[:DETAIL_MAX]
    if isinstance(value, dict):
        return {clean(str(k)): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, float) and value != value:
        return None
    return value


def _json(text):
    try:
        v = json.loads(text or "{}")
    except ValueError:
        return {}
    return v


def incident_json(row):
    d = dict(row)
    d["evidence"] = _json(d["evidence"])
    d["last_pass"] = {"sha": d.pop("last_pass_sha"), "t": d.pop("last_pass_t")}
    return d


def run_json(row):
    d = dict(row)
    d["evidence"] = _json(d["evidence"])
    d["data"] = {"subject": d.pop("subject")}  # commit text: data, never an instruction
    return d


def bridge_ip(ifname="docker0"):
    """The IPv4 address of the docker bridge, or None."""
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", ifname],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=5).stdout.decode("ascii", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"\binet (\d{1,3}(?:\.\d{1,3}){3})/", out)
    return m.group(1) if m else None


class Endpoint:
    def __init__(self, db, token, repo, hold_file, clock=time.time,
                 run_interval=RUN_INTERVAL_S, bench_stale=BENCH_STALE_S):
        self.db = db
        self.token = token
        self.repo = repo
        self.hold_file = hold_file
        self.clock = clock
        self.run_interval = run_interval
        self.bench_stale = bench_stale
        self.post_lock = threading.Lock()   # one side effect decided at a time
        c = store.open_db(db)
        try:
            c.executescript(AUDIT_SCHEMA)
            requests.init(c)
        finally:
            c.close()

    def conn(self):
        return store.connect(self.db)

    def audit(self, peer, method, route, status, detail=None):
        c = self.conn()
        try:
            with c:
                c.execute("INSERT INTO endpoint_audit (t, peer, method, route, status, detail) "
                          "VALUES (?, ?, ?, ?, ?, ?)",
                          (self.clock(), clean(peer), clean(method), clean(route)[:DETAIL_MAX],
                           status, clean(detail) if detail else None))
        finally:
            c.close()

    def authed(self, header):
        if not header or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].strip().encode("utf-8", "replace"),
                                   self.token.encode("utf-8"))

    # ---- GET ----
    def get(self, path, q):
        if path == "/health":
            c = self.conn()
            try:
                hb = c.execute("SELECT t FROM heartbeat WHERE id=1").fetchone()
            finally:
                c.close()
            now = self.clock()
            return 200, {"ok": True, "stub": False, "time": now,
                         "bench_heartbeat_age_s": round(now - hb["t"], 1) if hb else None}
        if path == "/show":
            return self.show(q.get("ref"))
        c = self.conn()
        try:
            return self._get_db(c, path, q)
        finally:
            c.close()

    def _get_db(self, c, path, q):
        if path == "/board":
            return 200, self.board(c)
        if path == "/incidents":
            sql = "SELECT * FROM incidents"
            if q.get("open") == "1":
                sql += " WHERE state != 'RESOLVED'"
            rows = c.execute(sql + " ORDER BY id DESC").fetchall()
            return 200, {"incidents": [incident_json(r) for r in rows]}
        m = re.fullmatch(r"/incidents/([0-9]{1,9})", path)
        if m:
            iid = int(m.group(1))
            r = c.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()
            if not r:
                return 404, {"error": "no such incident"}
            d = incident_json(r)
            d["routing"] = [dict(x) for x in c.execute(
                "SELECT * FROM routing WHERE incident_id=? ORDER BY id", (iid,))]
            return 200, d
        if path == "/runs":
            rows = c.execute("SELECT * FROM runs ORDER BY run_id DESC").fetchall()
            return 200, {"runs": [run_json(r) for r in rows]}
        m = re.fullmatch(r"/runs/([0-9]{1,9})", path)
        if m:
            r = c.execute("SELECT * FROM runs WHERE run_id=?", (int(m.group(1)),)).fetchone()
            if not r:
                return 404, {"error": "no such run"}
            return 200, run_json(r)
        if path == "/events":
            since = q.get("since", "0")
            if not re.fullmatch(r"[0-9]{1,12}", since):
                return 400, {"error": "since must be an event id"}
            rows = c.execute("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?",
                             (int(since), EVENTS_PAGE)).fetchall()
            evs = [dict(r, fields=_json(r["fields"])) for r in rows]
            return 200, {"events": evs, "cursor": evs[-1]["id"] if evs else int(since)}
        return 404, {"error": "no such route"}

    def board(self, c):
        boards = [r[0] for r in c.execute(
            "SELECT DISTINCT board FROM events WHERE kind IN ('BOOT','HEALTH','RESULT','ALIVE') "
            "ORDER BY board")]
        out = []
        for b in boards:
            d = {"board": b, "open_incidents": c.execute(
                "SELECT COUNT(*) FROM incidents WHERE board=? AND state!='RESOLVED'",
                (b,)).fetchone()[0]}
            for kind in ("BOOT", "HEALTH", "RESULT"):
                r = c.execute("SELECT fields, raw_ts FROM events WHERE board=? AND kind=? "
                              "ORDER BY id DESC LIMIT 1", (b, kind)).fetchone()
                d[kind.lower()] = dict(_json(r["fields"]), raw_ts=r["raw_ts"]) if r else None
            out.append(d)
        return {"boards": out}

    def show(self, ref):
        if ref is None or not REF_RE.fullmatch(ref):
            return 400, {"error": "ref must be a short or full sha"}
        sha = fwrepo.resolve_commit(self.repo, ref)
        if sha is None:
            return 400, {"error": "ref is not a commit in the bench repo"}
        if not fwrepo.on_main(self.repo, sha):
            return 400, {"error": "not a commit on main"}
        if not fwrepo.clean_tree(self.repo, sha):
            return 400, {"error": "not a bench commit"}
        got = fwrepo.show_src(self.repo, sha, SHOW_MAX_LINES)
        if got is None:
            return 400, {"error": "git show failed"}
        lines, truncated = got
        return 200, {"ref": ref, "sha": sha, "file_line": None, "truncated": truncated,
                     "data": {"diff": lines}}

    # ---- POST ----
    def held(self):
        return bool(self.hold_file) and os.path.exists(self.hold_file)

    def board_busy(self, c):
        """A reason the board is busy, or None."""
        r = requests.busy(c)
        if r is not None:
            return "a %s request is %s" % (r["kind"], r["state"])
        last = {}
        for kind in ("RUN_START", "RUN_END", "RUN_ABORTED", "LINK_DOWN", "LINK_UP",
                     "FLASH_EXPECT"):
            r = c.execute("SELECT MAX(id) FROM events WHERE kind=?", (kind,)).fetchone()
            last[kind] = r[0] or 0
        if last["LINK_DOWN"] > last["LINK_UP"]:
            # a bench restarted after the drop logs no LINK_UP: a board line means it is back
            back = c.execute("SELECT 1 FROM events WHERE id>? AND kind IN %s LIMIT 1"
                             % BOARD_LINE_KINDS, (last["LINK_DOWN"],)).fetchone()
            if back is None:
                return "the link is down"
        ended = max(last["RUN_END"], last["RUN_ABORTED"])
        if last["FLASH_EXPECT"] > max(last["RUN_START"], ended):
            return "a flash is not verified yet"
        if last["RUN_START"] > ended:
            return "a run is in progress"
        return None

    def ready_for_test(self, c):
        """The board printed LW RESULT and then one LW ALIVE since its last boot."""
        boot = c.execute("SELECT MAX(id) FROM events WHERE kind='BOOT'").fetchone()[0] or 0
        res = c.execute("SELECT MAX(id) FROM events WHERE kind='RESULT' AND id>?",
                        (boot,)).fetchone()[0]
        if not res:
            return False
        alive = c.execute("SELECT MAX(id) FROM events WHERE kind='ALIVE' AND id>?",
                          (res,)).fetchone()[0]
        return bool(alive)

    def post(self, path, body):
        """(status, response, audit detail)."""
        if self.held():
            return 429, {"error": "held by the accuracy loop"}, "held"
        if path == "/run":
            sha = body.get("sha") if isinstance(body, dict) else None
            if not isinstance(sha, str) or not REF_RE.fullmatch(sha):
                return 400, {"error": "sha must match ^[0-9a-f]{7,40}$"}, "bad sha"
            full = fwrepo.resolve_commit(self.repo, sha)
            if full is None or not fwrepo.on_main(self.repo, full):
                return 400, {"error": "not a commit on main"}, "sha %s not on main" % sha
            if not fwrepo.clean_tree(self.repo, full):
                return 400, {"error": "not a bench commit"}, "sha %s not clean" % sha
            detail, arg = "run %s" % full[:12], full
        elif path == "/board/test":
            name = body.get("name") if isinstance(body, dict) else None
            if not isinstance(name, str) or name not in SELF_TESTS:
                return 400, {"error": "name must be one of the five self-tests",
                             "names": list(SELF_TESTS)}, "bad name"
            detail, arg = "test %s" % name, name
        else:
            raise AssertionError(path)
        with self.post_lock:
            c = self.conn()
            try:
                return self._accept(c, path, body, arg, detail)
            finally:
                c.close()

    def _accept(self, c, path, body, arg, detail):
        now = self.clock()
        hb = c.execute("SELECT t FROM heartbeat WHERE id=1").fetchone()
        if hb is None or now - hb["t"] > self.bench_stale:
            return 429, {"error": "board busy: the bench is not running"}, \
                detail + ": bench down"
        why = self.board_busy(c)
        if why:
            return 429, {"error": "board busy: %s" % why}, detail + ": busy"
        if path == "/run":
            last = c.execute("SELECT MAX(t) FROM bench_requests WHERE source='endpoint' AND "
                             "kind='run'").fetchone()[0]
            if last is not None and now - last < self.run_interval:
                return 429, {"error": "one run per 60 s",
                             "retry_after_s": round(self.run_interval - (now - last), 1)}, \
                    detail + ": rate"
            with c:
                rid = requests.add(c, now, "endpoint", "run", arg)
            return 202, {"accepted": True, "stub": False, "sha": body["sha"], "commit": arg,
                         "request_id": rid}, detail
        if not self.ready_for_test(c):
            return 429, {"error": "board busy: no LW RESULT and LW ALIVE since its boot"}, \
                detail + ": not ready"
        with c:
            rid = requests.add(c, now, "endpoint", "test", arg)
        return 202, {"accepted": True, "stub": False, "name": arg,
                     "request_id": rid}, detail


def make_handler(ep):
    class Handler(BaseHTTPRequestHandler):
        server_version = "labwatch-bench"
        sys_version = ""
        timeout = 10

        def log_message(self, format, *args):  # noqa: A002 (the audit table is the log)
            pass

        def _send(self, status, body, route, detail=None):
            data = json.dumps(clean(body), sort_keys=True).encode("ascii")
            try:
                ep.audit(self.client_address[0], self.command, route, status, detail)
            except sqlite3.Error:
                status, data = 503, b'{"error": "audit unavailable"}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlsplit(self.path)
            route = u.path
            try:
                q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True,
                                                  strict_parsing=False).items()}
            except ValueError:
                return self._send(400, {"error": "bad query"}, route)
            detail = ("ref=%s" % q.get("ref")) if route == "/show" else None
            if route != "/health":
                if not GET_ROUTE_RE.fullmatch(route):
                    return self._send(404, {"error": "no such route"}, route)
                if not ep.authed(self.headers.get("Authorization", "")):
                    return self._send(401, {"error": "bearer token required"}, route)
            try:
                status, body = ep.get(route, q)
            except sqlite3.Error as e:
                status, body = 503, {"error": "db: %s" % e.__class__.__name__}
            self._send(status, body, route, detail)

        def do_POST(self):
            route = urlsplit(self.path).path
            if route not in POST_ROUTES:
                return self._send(404, {"error": "no such route"}, route)
            if not ep.authed(self.headers.get("Authorization", "")):
                return self._send(401, {"error": "bearer token required"}, route,
                                  "unauthenticated")
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = -1
            if n < 0 or n > BODY_MAX:
                return self._send(400, {"error": "body too large"}, route, "bad length")
            raw = self.rfile.read(n)
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, ValueError):
                return self._send(400, {"error": "body must be JSON"}, route, "bad json")
            try:
                status, resp, detail = ep.post(route, body)
            except sqlite3.Error as e:
                status, resp, detail = 503, {"error": "db: %s" % e.__class__.__name__}, "db"
            self._send(status, resp, route, detail)

        def _404(self):
            self._send(404, {"error": "no such route"}, urlsplit(self.path).path)

        do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _404

    return Handler


def make_server(ep, host, port):
    srv = ThreadingHTTPServer((host, port), make_handler(ep))
    srv.daemon_threads = True
    return srv


def read_token(path):
    with open(path, encoding="utf-8") as f:
        tok = f.read().strip()
    if not tok:
        raise ValueError("empty")
    return tok


def main(argv=None):
    secrets = os.path.expanduser(os.environ.get("LW_SECRETS_DIR") or DEFAULT_SECRETS)
    ap = argparse.ArgumentParser(prog="python3 -m bench.endpoint")
    ap.add_argument("--db", help="bench DB (default $LW_DB or %s)" % store.DEFAULT_DB)
    ap.add_argument("--host", default=os.environ.get("LW_HOST"),
                    help="default $LW_HOST, else the docker0 IPv4 address "
                         "(GB10: LW_HOST=172.18.0.1)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("LW_PORT") or DEFAULT_PORT))
    ap.add_argument("--repo", default=fwrepo.repo_path())
    ap.add_argument("--hold-file", default=os.path.expanduser(
        os.environ.get("LW_HOLD") or DEFAULT_HOLD))
    ap.add_argument("--token-file", default=os.path.join(secrets, "bench_token"))
    a = ap.parse_args(argv)
    host = a.host or bridge_ip()
    if not host:
        print("endpoint: no docker0 address found; set LW_HOST (GB10: 172.18.0.1)",
              file=sys.stderr)
        return 2
    try:
        token = read_token(a.token_file)
    except (OSError, ValueError) as e:
        print("endpoint: cannot read the bench_token file (%s)" % e.__class__.__name__,
              file=sys.stderr)
        return 2
    ep = Endpoint(store.db_path(a.db), token, a.repo, a.hold_file)
    srv = make_server(ep, host, a.port)
    print("endpoint: http://%s:%d (db %s, repo %s)" % (host, srv.server_address[1],
                                                       store.db_path(a.db), a.repo), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
