"""Read-only data for the status view: the bench DB and the serial line tail.

Everything here only reads. The DB is opened with a `file:...?mode=ro` URI,
so any write through this connection fails. The serial port is never opened
(lane A's serial owner is its only user); serial lines come from a log file
the bench writes or from the events table (fields.line, raw_ts).

Board text and git text are data: every string that leaves this module for
the screen goes through clean(), which drops ANSI escapes and control
characters.
"""

import collections
import json
import os
import re
import sqlite3
import time
import urllib.parse

DEFAULT_DB = "~/.local/state/labwatch/bench.db"
BUSY_MS = 5000
MAX_LINES = 500
RECENT = 5

# ESC [ ... final, ESC ] ... BEL/ST, other two-byte ESC sequences, 8-bit CSI.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]"
                      r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
                      r"|\x1b[@-Z\\-_]"
                      r"|\x9b[0-?]*[ -/]*[@-~]")


def clean(s, limit=400):
    """Printable ASCII only: ANSI escapes removed, control chars dropped, tabs to spaces."""
    if s is None:
        return ""
    if isinstance(s, bytes):
        s = s.decode("ascii", "replace")
    s = _ANSI_RE.sub("", str(s)).replace("\t", " ")
    out = []
    for ch in s:
        o = ord(ch)
        if 32 <= o < 127:
            out.append(ch)
        elif o >= 160:
            out.append("?")
        # C0, DEL and C1 control characters are dropped.
    return "".join(out)[:limit]


def db_path(path=None):
    return os.path.expanduser(path or os.environ.get("LW_DB") or DEFAULT_DB)


def open_ro(path):
    """A read-only connection (mode=ro URI, busy timeout); raises if the file is missing."""
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    uri = "file:%s?mode=ro" % urllib.parse.quote(path)
    conn = sqlite3.connect(uri, uri=True, timeout=BUSY_MS / 1000.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=%d" % BUSY_MS)
    conn.execute("PRAGMA query_only=ON")
    return conn


def loads(text):
    try:
        v = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


class Db:
    """Reconnecting read-only access; read() never raises, it reports an error string."""

    def __init__(self, path):
        self.path = db_path(path)
        self.conn = None

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
            self.conn = None

    def _connect(self):
        if self.conn is None:
            self.conn = open_ro(self.path)
        return self.conn

    def query(self, sql, args=()):
        return self._connect().execute(sql, args).fetchall()

    def read(self):
        """A snapshot dict: incidents, alerts per incident, last sent alert, heartbeat."""
        st = {"error": None, "open": [], "recent": [], "alerts": {}, "last_sent": None,
              "heartbeat": None, "boards": []}
        try:
            st["open"] = [dict(r) for r in self.query(
                "SELECT * FROM incidents WHERE state != 'RESOLVED' ORDER BY id DESC LIMIT 6")]
            st["recent"] = [dict(r) for r in self.query(
                "SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (RECENT,))]
            ids = sorted({r["id"] for r in st["open"] + st["recent"]})
            if ids:
                marks = ",".join("?" for _ in ids)
                for r in self.query("SELECT id, incident_id, transition, text, created, sent "
                                    "FROM alerts WHERE incident_id IN (%s) ORDER BY id" % marks,
                                    ids):
                    st["alerts"].setdefault(r["incident_id"], []).append(dict(r))
            r = self.query("SELECT id, incident_id, transition, text, sent FROM alerts "
                           "WHERE sent IS NOT NULL ORDER BY sent DESC, id DESC LIMIT 1")
            st["last_sent"] = dict(r[0]) if r else None
            st["boards"] = [r[0] for r in self.query(
                "SELECT DISTINCT board FROM incidents ORDER BY board LIMIT 4")]
        except FileNotFoundError:
            self.close()
            st["error"] = "DB not found: %s" % self.path
            return st
        except sqlite3.Error as e:
            self.close()
            msg = str(e)
            if "no such table" in msg:
                msg = "DB has no bench tables yet (%s)" % msg
            st["error"] = "DB: %s" % msg
            return st
        try:  # lane A's own table; absent in the stub
            r = self.query("SELECT t FROM heartbeat WHERE id=1")
            st["heartbeat"] = r[0][0] if r else None
        except sqlite3.Error:
            pass
        return st


class EventTail:
    """Serial lines from the events table: rows whose fields carry a line."""

    def __init__(self, db, keep=MAX_LINES):
        self.db = db
        self.cursor = None
        self.lines = collections.deque(maxlen=keep)
        self.note = None

    def _add(self, rows):
        for r in rows:
            self.cursor = r["id"]
            line = loads(r["fields"]).get("line")
            if isinstance(line, str):
                self.lines.append("%s %s" % (clean(r["raw_ts"], 12), clean(line)))

    def poll(self):
        try:
            if self.cursor is None:
                rows = self.db.query("SELECT id, raw_ts, fields FROM events "
                                     "ORDER BY id DESC LIMIT ?", (self.lines.maxlen,))
                self.cursor = 0
                self._add(reversed(rows))
            else:
                top = self.db.query("SELECT MAX(id) FROM events")[0][0] or 0
                if top < self.cursor:  # the DB was replaced
                    self.cursor, self.lines = None, collections.deque(maxlen=self.lines.maxlen)
                    return self.poll()
                self._add(self.db.query("SELECT id, raw_ts, fields FROM events WHERE id > ? "
                                        "ORDER BY id LIMIT 2000", (self.cursor,)))
            self.note = None if self.lines else "no serial lines in the events table yet"
        except FileNotFoundError:
            self.db.close()
            self.note = "DB not found: %s" % self.db.path
        except sqlite3.Error as e:
            self.db.close()
            self.cursor = None
            self.note = "events: %s" % clean(str(e), 100)


class LogTail:
    """Follows a text log the bench writes; survives a missing, truncated or replaced file."""

    START_BYTES = 64 * 1024

    def __init__(self, path, keep=MAX_LINES):
        self.path = os.path.expanduser(path)
        self.lines = collections.deque(maxlen=keep)
        self.f = None
        self.ino = None
        self.buf = b""
        self.note = None

    def close(self):
        if self.f is not None:
            self.f.close()
            self.f = None

    def _open(self, first):
        self.f = open(self.path, "rb")
        st = os.fstat(self.f.fileno())
        self.ino = st.st_ino
        self.buf = b""
        if first and st.st_size > self.START_BYTES:
            self.f.seek(st.st_size - self.START_BYTES)
            self.f.readline()  # drop the partial first line

    def poll(self):
        try:
            st = os.stat(self.path)
        except OSError:
            self.close()
            self.note = "log file not found: %s" % clean(self.path, 200)
            return
        try:
            if self.f is None:
                self._open(first=self.ino is None)
            elif st.st_ino != self.ino or st.st_size < self.f.tell():
                self.close()
                self._open(first=False)
            data = self.f.read(1 << 20)
        except OSError as e:
            self.close()
            self.note = "log: %s" % clean(str(e), 100)
            return
        self.buf += data
        parts = re.split(rb"\r\n|\n|\r", self.buf)
        self.buf = parts.pop()
        if len(self.buf) > 4096:
            parts.append(self.buf)
            self.buf = b""
        for p in parts:
            if p.strip():
                self.lines.append(clean(p))
        self.note = None if self.lines else "log file is empty: %s" % clean(self.path, 200)


def now():
    return time.time()
