"""The bench's request queue: what the endpoint and the repo watcher ask the bench to do.

Neither the endpoint nor the watcher touches the board. They write one row
into bench_requests (in the bench DB); the bench process (bench.run) is the
only consumer: claim_next() takes the oldest pending row, finish() closes it.

    kind 'run'   arg = a full sha on main: flash and run it
    kind 'test'  arg = one of the five self-test names: send "test <name>"

state: pending -> running -> done | failed. The endpoint's busy check is
"a row is pending or running".
"""

import re

REQUESTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS bench_requests (
    id       INTEGER PRIMARY KEY,
    t        REAL NOT NULL,
    source   TEXT NOT NULL CHECK (source IN ('endpoint','watcher')),
    kind     TEXT NOT NULL CHECK (kind IN ('run','test')),
    arg      TEXT NOT NULL,
    state    TEXT NOT NULL DEFAULT 'pending'
               CHECK (state IN ('pending','running','done','failed')),
    t_start  REAL,
    t_done   REAL,
    result   TEXT,
    run_id   INTEGER
);
CREATE INDEX IF NOT EXISTS bench_requests_state ON bench_requests(state, id);
"""

SHA_FULL_RE = re.compile(r"[0-9a-f]{40}")
SELF_TESTS = ("crc32", "ringbuf", "systick", "cmd_dispatch", "loopback")


def init(conn):
    conn.executescript(REQUESTS_SCHEMA)
    conn.commit()


def add(conn, t, source, kind, arg):
    """Queue one request; returns its id. The caller commits (or uses `with conn`)."""
    if kind == "run" and not SHA_FULL_RE.fullmatch(arg):
        raise ValueError("a run request needs a full sha")
    if kind == "test" and arg not in SELF_TESTS:
        raise ValueError("not one of the five self-tests")
    cur = conn.execute("INSERT INTO bench_requests (t, source, kind, arg) VALUES (?, ?, ?, ?)",
                       (t, source, kind, arg))
    return cur.lastrowid


def busy(conn):
    """The oldest pending or running request, or None."""
    return conn.execute("SELECT * FROM bench_requests WHERE state IN ('pending','running') "
                        "ORDER BY id LIMIT 1").fetchone()


def claim_next(conn, t):
    """For bench.run: take the oldest pending request (marks it running), or None."""
    with conn:
        r = conn.execute("SELECT * FROM bench_requests WHERE state='pending' "
                         "ORDER BY id LIMIT 1").fetchone()
        if r is None:
            return None
        cur = conn.execute("UPDATE bench_requests SET state='running', t_start=? "
                           "WHERE id=? AND state='pending'", (t, r["id"]))
        if cur.rowcount != 1:
            return None
    return conn.execute("SELECT * FROM bench_requests WHERE id=?", (r["id"],)).fetchone()


def finish(conn, request_id, t, ok, result, run_id=None):
    """For bench.run: close a running request with a short result word or sentence."""
    with conn:
        conn.execute("UPDATE bench_requests SET state=?, t_done=?, result=?, run_id=? "
                     "WHERE id=? AND state='running'",
                     ("done" if ok else "failed", t, str(result)[:120], run_id, request_id))


def recover_running(conn, t):
    """For bench.run at startup: a request left running by a killed bench is failed, not redone."""
    with conn:
        return conn.execute("UPDATE bench_requests SET state='failed', t_done=?, "
                            "result='bench restarted during the request' "
                            "WHERE state='running'", (t,)).rowcount
