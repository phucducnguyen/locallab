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


# ---------- the HEALTH line, parsed by key (older lines lack sensor, radio, estop) ----------

DEVICES = ("loopback", "sensor", "radio")
VDD_MIN_MV = 3000
_HEALTH_RE = re.compile(r"\bLW HEALTH\b(.*)")
_KV_RE = re.compile(r"([a-z_]+)=(\S+)")


def _num(v, kind):
    try:
        return kind(v)
    except (TypeError, ValueError):
        return None


def parse_health(line):
    """{n, loopback, sensor, radio, estop, vdd, temp} from one LW HEALTH line; None if absent.

    Returns None when the line is not a HEALTH line.
    """
    m = _HEALTH_RE.search(line or "")
    if not m:
        return None
    rest = m.group(1).split()
    kv = dict(_KV_RE.findall(" ".join(rest)))
    out = {"n": _num(rest[0], int) if rest and "=" not in rest[0] else None}
    for k in DEVICES:
        v = kv.get(k)
        out[k] = v if v in ("OK", "OPEN", "STUCK") else None
    e = kv.get("estop")
    out["estop"] = e if e in ("ON", "OFF") else None
    out["vdd"] = _num(kv.get("vdd"), int)
    out["temp"] = _num(kv.get("temp"), float)
    return out


def health_bad(h):
    """The faults one parsed HEALTH line shows: [(key, value)]."""
    bad = [(k, h[k]) for k in DEVICES if h.get(k) in ("OPEN", "STUCK")]
    if h.get("estop") == "ON":
        bad.append(("estop", "ON"))
    if h.get("vdd") is not None and h["vdd"] < VDD_MIN_MV:
        bad.append(("vdd", h["vdd"]))
    return bad


def latest_health(lines):
    """The newest parsed HEALTH line in a sequence of serial lines, or None."""
    for line in reversed(lines):
        h = parse_health(line)
        if h is not None:
            return h
    return None


_CAUSE_RE = re.compile(r"\bLW DUMP cause\b(.*)")


def dump_causes(line):
    """Cause names from an "LW DUMP cause <NAMES>" line, else None."""
    m = _CAUSE_RE.search(line or "")
    if not m:
        return None
    return [clean(w, 40) for w in re.split(r"[\s,|]+", m.group(1)) if clean(w, 40)]


CAUSE_BEFORE_S = 30.0   # the DUMP burst comes just before the first HARDFAULT line
CAUSE_AFTER_S = 2.0


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
              "heartbeat": None, "boards": [], "causes": {}}
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
        for inc in st["open"]:
            if inc.get("kind") == "HARDFAULT":
                try:
                    names = self.causes(inc)
                except sqlite3.Error:
                    names = None
                if names:
                    st["causes"][inc["id"]] = names
        try:  # lane A's own table; absent in the stub
            r = self.query("SELECT t FROM heartbeat WHERE id=1")
            st["heartbeat"] = r[0][0] if r else None
        except sqlite3.Error:
            pass
        return st


    def causes(self, inc):
        """The DUMP cause names for a HARDFAULT incident: from its evidence, else from the
        "LW DUMP cause" line logged just before its first bad line (matched on the line text,
        since its event kind depends on the parser version)."""
        ev = loads(inc.get("evidence"))
        for k in ("cause", "causes", "dump_cause"):
            v = ev.get(k)
            if isinstance(v, str) and v.strip():
                return [clean(w, 40) for w in re.split(r"[\s,|]+", v) if w]
            if isinstance(v, list) and v:
                return [clean(w, 40) for w in v if isinstance(w, str)]
        dump = ev.get("dump")
        if isinstance(dump, dict):
            v = dump.get("cause") or dump.get("causes")
            if isinstance(v, list) and v:
                return [clean(w, 40) for w in v if isinstance(w, str)]
            if isinstance(v, str) and v.strip():
                return [clean(w, 40) for w in v.split()]
        for c in ev.get("lines") or []:
            names = dump_causes(c.get("line") if isinstance(c, dict) else c
                                if isinstance(c, str) else None)
            if names:
                return names
        t0 = inc.get("t_first_bad_line") or inc.get("t_open") or inc.get("opened")
        if t0 is None:
            return None
        for r in self.query("SELECT fields FROM events WHERE board=? AND t_wall BETWEEN ? AND ? "
                            "AND fields LIKE '%LW DUMP cause%' ORDER BY t_wall DESC, id DESC "
                            "LIMIT 5", (inc.get("board"), t0 - CAUSE_BEFORE_S,
                                        t0 + CAUSE_AFTER_S)):
            names = dump_causes(loads(r["fields"]).get("line"))
            if names:
                return names
        return None


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
