"""The bench's SQLite: the shared tables of docs/schema.sql plus lane A's own.

Every connection uses WAL and a 5 s busy timeout (day contract). Lane A's own
tables never rename or change the shared ones.
"""

import json
import os
import sqlite3
import time

from bench.serial_owner import wall_ts

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA = os.path.join(REPO, "docs", "schema.sql")
DEFAULT_DB = "~/.local/state/labwatch/bench.db"

LANE_A_SCHEMA = """
CREATE TABLE IF NOT EXISTS pass_history (
    id      INTEGER PRIMARY KEY,
    board   TEXT NOT NULL,
    sha     TEXT NOT NULL,           -- verified shas only
    t       REAL NOT NULL,
    run_id  INTEGER REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS flashes (
    id          INTEGER PRIMARY KEY,
    board       TEXT NOT NULL,
    sha         TEXT NOT NULL,
    how         TEXT NOT NULL,       -- 'hand' until stage A3
    t           REAL NOT NULL,
    verified_t  REAL,                -- when LW BOOT fw=<sha> arrived
    result      TEXT                 -- VERIFIED | FLASH_ERROR
);
CREATE TABLE IF NOT EXISTS bench_state (
    board         TEXT PRIMARY KEY,
    verified_sha  TEXT,
    t             REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS heartbeat (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    t    REAL NOT NULL,
    pid  INTEGER
);
CREATE TABLE IF NOT EXISTS alerts_printed (
    alert_id  INTEGER PRIMARY KEY,
    t         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS watcher_posts (
    id                   INTEGER PRIMARY KEY,
    key                  TEXT NOT NULL UNIQUE,
    text                 TEXT NOT NULL,
    created              REAL NOT NULL,
    printed              REAL,
    sent                 REAL,
    telegram_message_id  INTEGER
);
"""


def db_path(path=None):
    return os.path.expanduser(path or os.environ.get("LW_DB") or DEFAULT_DB)


def connect(path=None):
    path = db_path(path)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init(conn):
    with open(SCHEMA, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.executescript(LANE_A_SCHEMA)
    conn.commit()


def open_db(path=None):
    conn = connect(path)
    init(conn)
    return conn


def dumps(obj):
    return json.dumps(obj, sort_keys=True, default=str)


def insert(conn, table, row):
    cols = ", ".join('"%s"' % k for k in row)
    marks = ", ".join("?" for _ in row)
    cur = conn.execute("INSERT INTO %s (%s) VALUES (%s)" % (table, cols, marks),
                       list(row.values()))
    return cur.lastrowid


def event(conn, t, board, kind, fields, raw_ts=None):
    return insert(conn, "events", {"t_wall": t, "board": board, "kind": kind,
                                   "fields": dumps(fields), "raw_ts": raw_ts or wall_ts(t)})


def heartbeat(conn, t, pid=None):
    conn.execute("INSERT INTO heartbeat (id, t, pid) VALUES (1, ?, ?) "
                 "ON CONFLICT(id) DO UPDATE SET t=excluded.t, pid=excluded.pid",
                 (t, pid if pid is not None else os.getpid()))
    conn.commit()


def open_alert_id(conn, incident_id):
    r = conn.execute("SELECT id FROM alerts WHERE incident_id=? AND transition='open'",
                     (incident_id,)).fetchone()
    return r["id"] if r else None


def add_alert(conn, incident_id, transition, text, t):
    """Queue one alert row; None if that (incident, transition) already exists."""
    reply_to = None if transition == "open" else open_alert_id(conn, incident_id)
    cur = conn.execute("INSERT OR IGNORE INTO alerts (incident_id, transition, text, "
                       "reply_to_alert_id, created) VALUES (?, ?, ?, ?, ?)",
                       (incident_id, transition, text, reply_to, t))
    return cur.lastrowid if cur.rowcount == 1 else None


def now():
    return time.time()
