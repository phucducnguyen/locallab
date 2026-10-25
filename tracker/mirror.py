"""Mirror the bench's routing rows into Forgejo: one issue per problem.

    python3 -m tracker.mirror [--db PATH] [--state PATH] [--once] [--interval 2]

Each pass plans, then replays. Planning reads the bench DB only and queues
operations in an outbox table in the mirror's own state DB (default
tracker_state.db next to the bench DB); replay sends them in order and stops
while the tracker is down, so no create, comment or close is ever dropped.

- Problem key: board<board>/<device>/<state> (hardware and other triage) or
  board<board>/CODE/<file:line> (CODE; the kind when no file:line), written
  in the issue body as the exact line "lw-key: <key>". The state DB's
  problems table (key -> issue) is the index.
- Create: a routing row whose problem has no usable issue gets one issue
  titled "[lw-<incident id>] <board> <kind>: <triage>", labelled by route,
  body with the route, the reason, the verified sha and file:line, never
  source or diff text, and the lw-key line.
- Reuse: an open issue of the same problem gets "happened again at <UTC>,
  incident <id>, duration <s> s|still open"; one closed under 60 min ago is
  reopened with that comment. The number goes into routing.ticket and
  incidents.ticket, and one "ticket" alert row ("Ticket #<n>: <url>",
  replying to the open alert) is queued per incident; the URL base is
  LW_TRACKER_PUBLIC.
- Dedupe after a crash: only a create that was sent but whose number was
  never stored is searched for by q=lw-<id>, retried for up to 10 s for the
  indexer lag; only an exact title plus the exact lw-key line counts.
- Flaps: one comment with the flap count, at most once per 60 s per incident.
- Resolve: a comment with the duration, then close, but a shared issue closes
  only when its last open incident resolves. An incident that has flapped is
  closed once it has stayed resolved for the 60 s flap window; a closed issue
  whose incident reopens is reopened.
- Comments carry "(lab watch update <op id>)" so an ambiguous retry is checked
  against the issue's comments instead of posted twice.

Env: LW_DB (bench DB), LW_SECRETS_DIR (tracker_token, tracker_labels.json),
LW_TRACKER (base URL), LW_TRACKER_PUBLIC (link base in the ticket alert,
default http://127.0.0.1:3000), LW_TRACKER_STATE (state DB path).
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker.forgejo import DEFAULT_BASE, DEFAULT_REPO, Forgejo, TrackerError, secrets_dir  # noqa: E402

ROUTE_LABEL = {"Hardware team": "hardware", "Firmware team": "firmware", "Triage": "needs-triage"}
SEARCH_WAIT_S = 10.0
SEARCH_STEP_S = 1.0
FLAP_COMMENT_S = 60.0

_SHA = re.compile(r"[0-9a-f]{7,40}")
_FILE_LINE = re.compile(r"[A-Za-z0-9_./-]{1,100}:[0-9]{1,6}")
_WORD = re.compile(r"[A-Z_]{1,20}")
_BOARD = re.compile(r"[A-Za-z0-9_.-]{1,40}")


def default_db():
    return os.environ.get("LW_DB", os.path.expanduser("~/.local/state/labwatch/bench.db"))


def connect(path):
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def add_alert(db, iid, transition, text, t):
    """One outbox row replying to the incident's open alert; None if that transition exists."""
    r = db.execute("SELECT id FROM alerts WHERE incident_id=? AND transition='open'",
                   (iid,)).fetchone()
    cur = db.execute("INSERT OR IGNORE INTO alerts (incident_id, transition, text, "
                     "reply_to_alert_id, created) VALUES (?, ?, ?, ?, ?)",
                     (iid, transition, text, r["id"] if r else None, t))
    db.commit()
    return cur.lastrowid if cur.rowcount == 1 else None


def public_base():
    return os.environ.get("LW_TRACKER_PUBLIC", DEFAULT_BASE).rstrip("/")


def ticket_text(number, repo=DEFAULT_REPO):
    return "Ticket #%d: %s/%s/issues/%d" % (number, public_base(), repo, number)


def clean(text, cap=200):
    """Printable ASCII on one line, capped."""
    s = "".join(c if " " <= c <= "~" else " " for c in str(text or ""))
    s = " ".join(s.split())
    return s if len(s) <= cap else s[:cap - 3] + "..."


def _match(rx, value, default=None):
    value = "" if value is None else str(value)
    return value if rx.fullmatch(value) else default


def _file_line(*evidence_texts):
    for text in evidence_texts:
        try:
            ev = json.loads(text or "{}")
        except ValueError:
            continue
        fl = ev.get("file_line") if isinstance(ev, dict) else None
        fl = _match(_FILE_LINE, fl)
        if fl:
            return fl
    return None


_DEVICE = re.compile(r"[a-z_]{1,20}")
_STATE = re.compile(r"\b(OPEN|STUCK|ON|LOW)\b")
STATE_FALLBACK = "FAIL"
REOPEN_WITHIN_S = 3600.0


def _evidence(text):
    try:
        ev = json.loads(text or "{}")
    except ValueError:
        return {}
    return ev if isinstance(ev, dict) else {}


def _state_word(v):
    if isinstance(v, list) and v:
        v = v[-1]
    if not isinstance(v, str):
        return None
    m = _STATE.search(v)  # case-sensitive: "on" in a sentence is not ON
    return m.group(1) if m else None


def problem_key(row):
    """One key per problem across incidents, or None for a run-only row.

    Hardware (and any non-CODE triage): board<board>/<device>/<state>; device
    from the evidence ("loopback" for HEALTH_FAIL, else the kind when absent),
    state OPEN/STUCK/ON/LOW from the evidence, then the reason, else FAIL.
    Code: board<board>/CODE/<file:line>, or board<board>/CODE/<kind>.
    """
    if row["incident_id"] is None:
        return None
    board = "board" + _match(_BOARD, row["board"], "unknown")
    kind = _match(_WORD, row["kind"], "INCIDENT")
    ev = _evidence(row["i_evidence"])
    if _match(_WORD, row["triage"]) == "CODE":
        return "%s/CODE/%s" % (board, _file_line(row["i_evidence"]) or kind)
    device = _match(_DEVICE, ev.get("device"))
    if device is None:
        device = "loopback" if kind == "HEALTH_FAIL" else kind
    state = _state_word(ev.get("state")) or _state_word(ev.get(device))
    if state is None and (device == "power" or "LOW" in str(ev.get("cause") or "").upper()):
        state = "LOW"
    if state is None and (device == "estop" or kind == "ESTOP"):
        state = "ON"
    if state is None:
        state = _state_word(clean(row["reason"])) or STATE_FALLBACK
    return "%s/%s/%s" % (board, device, state)


def marker(pkey):
    return "lw-key: %s" % pkey


def utc(t):
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(float(t)))


ROWS = """
SELECT r.id, r.incident_id, r.run_id, r.triage, r.route, r.reason,
       i.board, i.kind, i.firmware, i.evidence AS i_evidence, i.opened,
       ru."commit" AS run_commit, ru.verdict, ru.evidence AS r_evidence
FROM routing r
LEFT JOIN incidents i ON i.id = r.incident_id
LEFT JOIN runs ru ON ru.run_id = r.run_id
WHERE r.ticket IS NULL AND r.route IS NOT NULL
ORDER BY r.id
"""


def issue_text(row):
    """(title, body) for a routing row. Only parsed, validated fields; no source or diff."""
    triage = _match(_WORD, row["triage"], "UNKNOWN")
    if row["incident_id"] is not None:
        key = "lw-%d" % row["incident_id"]
        title = "[%s] %s %s: %s" % (key, _match(_BOARD, row["board"], "board"),
                                    _match(_WORD, row["kind"], "INCIDENT"), triage)
    else:
        key = "lw-run-%d" % row["run_id"]
        title = "[%s] run %s: %s" % (key, _match(_WORD, row["verdict"], "FAIL"), triage)
    sha = _match(_SHA, row["firmware"]) or _match(_SHA, row["run_commit"])
    fl = _file_line(row["i_evidence"], row["r_evidence"])
    body = ["Routed to: %s" % clean(row["route"], 40),
            "Triage: %s" % triage,
            "Reason: %s" % clean(row["reason"])]
    if row["incident_id"] is not None:
        body.append("Board: %s" % _match(_BOARD, row["board"], "unknown"))
        body.append("Kind: %s" % _match(_WORD, row["kind"], "unknown"))
    body.append("Verified sha: %s" % (sha or "none"))
    if fl:
        body.append("File: %s" % fl)
    if row["incident_id"] is not None:
        body.append("Incident: %d" % row["incident_id"])
    if row["run_id"] is not None:
        body.append("Run: %d" % row["run_id"])
    body.append("Routing row: %d" % row["id"])
    pkey = problem_key(row)
    if pkey:
        body.append(marker(pkey))
    return key, title, "\n".join(body)


STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY,      -- replay order
    key        TEXT NOT NULL,            -- lw-<incident id> or lw-run-<run id>
    op         TEXT NOT NULL CHECK (op IN ('create', 'comment', 'close', 'reopen')),
    title      TEXT,
    body       TEXT,
    label_id   INTEGER,
    created    REAL NOT NULL,
    sent       REAL,                     -- set before the call; sent with done NULL = unknown
    attempts   INTEGER NOT NULL DEFAULT 0,
    done       REAL,
    how        TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(done, id);
CREATE TABLE IF NOT EXISTS issues (
    key         TEXT PRIMARY KEY,
    incident_id INTEGER,
    run_id      INTEGER,
    number      INTEGER,                 -- the issue number, once the create is done
    flaps_sent  INTEGER NOT NULL DEFAULT 0,
    flap_t      REAL,                    -- wall time the last flap comment was queued
    closed_for  REAL                     -- incidents.closed whose close is queued; NULL = open
);
CREATE TABLE IF NOT EXISTS problems (
    pkey     TEXT PRIMARY KEY,           -- board<b>/<device>/<state> or board<b>/CODE/<where>
    number   INTEGER,                    -- the problem's current issue; NULL while its create is queued
    closed_t REAL                        -- wall time its close was queued; NULL = open
);
"""


def connect_state(path):
    conn = connect(path)
    conn.executescript(STATE_SCHEMA)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(issues)").fetchall()]
    if "pkey" not in cols:  # a C2/C3 state DB
        conn.execute("ALTER TABLE issues ADD COLUMN pkey TEXT")
    conn.commit()
    return conn


def _down(e):
    """The tracker is unreachable or failing: stop the whole replay and keep the order."""
    return e.status is None or e.status >= 500 or e.status == 429


def _never_reached(e):
    """The call certainly did not change the tracker."""
    return e.refused or (e.status is not None and 400 <= e.status < 500)


def _secs(d):
    return ("%.1f" % float(d)).rstrip("0").rstrip(".")


class Mirror:
    """Plans tracker operations into the outbox (DB only), then replays it in order."""

    def __init__(self, conn, fj, label_ids, state, search_wait=SEARCH_WAIT_S,
                 search_step=SEARCH_STEP_S, sleep=time.sleep, clock=time.monotonic,
                 wall=time.time, log=print):
        self.conn = conn
        self.fj = fj
        self.label_ids = label_ids
        self.state = state
        self.search_wait = search_wait
        self.search_step = search_step
        self.sleep = sleep
        self.clock = clock
        self.wall = wall
        self.log = log

    # -- state ------------------------------------------------------------
    def _issue(self, key):
        return self.state.execute("SELECT * FROM issues WHERE key=?", (key,)).fetchone()

    def _enqueue(self, key, op, title=None, body=None, label_id=None):
        self.state.execute("INSERT INTO outbox (key, op, title, body, label_id, created) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (key, op, title, body, label_id,
                                                          self.wall()))

    def pending(self):
        return [dict(r) for r in self.state.execute(
            "SELECT * FROM outbox WHERE done IS NULL ORDER BY id").fetchall()]

    def _store_bench(self, key, incident_id, run_id, number):
        """Write the number into every routing row of this key and into the incident."""
        ticket = str(number)
        if incident_id is not None:
            self.conn.execute("UPDATE routing SET ticket=? WHERE incident_id=? AND ticket IS NULL",
                              (ticket, incident_id))
            self.conn.execute("UPDATE incidents SET ticket=? WHERE id=? AND ticket IS NULL",
                              (ticket, incident_id))
            r = self.conn.execute("SELECT ticket FROM incidents WHERE id=?",
                                  (incident_id,)).fetchone()
            if r is not None and str(r["ticket"]) == ticket:
                # The Telegram link, once per incident (UNIQUE(incident_id, transition)).
                add_alert(self.conn, incident_id, "ticket",
                          ticket_text(int(number), getattr(self.fj, "repo", DEFAULT_REPO)),
                          self.wall())
        else:
            self.conn.execute("UPDATE routing SET ticket=? WHERE run_id=? AND incident_id IS NULL "
                              "AND ticket IS NULL", (ticket, run_id))
        self.conn.commit()

    def _problem(self, pkey):
        return self.state.execute("SELECT * FROM problems WHERE pkey=?", (pkey,)).fetchone()

    def _reuse(self, key, row, pkey):
        """An open (or lately closed) issue for this problem: comment on it, no new issue."""
        p = self._problem(pkey)
        if p is None:
            return False
        now = self.wall()
        if p["closed_t"] is not None and now - p["closed_t"] >= REOPEN_WITHIN_S:
            return False  # closed an hour or more ago: a new issue
        inc = self.conn.execute("SELECT opened, duration_s FROM incidents WHERE id=?",
                                (row["incident_id"],)).fetchone()
        d = inc["duration_s"] if inc else None
        text = "happened again at %s, incident %d, duration %s" % (
            utc(inc["opened"] if inc else now), row["incident_id"],
            "%s s" % _secs(d) if d is not None else "still open")
        self.state.execute("INSERT INTO issues (key, incident_id, run_id, number, pkey) "
                           "VALUES (?, ?, ?, ?, ?)", (key, row["incident_id"], row["run_id"],
                                                      p["number"], pkey))
        if p["closed_t"] is not None:
            self._enqueue(key, "reopen")
            self.state.execute("UPDATE problems SET closed_t=NULL WHERE pkey=?", (pkey,))
        self._enqueue(key, "comment", body=text)
        self.state.commit()
        if p["number"] is not None:
            self._store_bench(key, row["incident_id"], row["run_id"], p["number"])
            self.log("routing %d -> issue #%d (%s happened again)" % (row["id"], p["number"],
                                                                     pkey))
        return True

    def _others_open(self, it):
        """Another incident on this incident's issue is not resolved yet."""
        if not it["pkey"]:
            return False
        for o in self.state.execute("SELECT * FROM issues WHERE pkey=? AND key<>? AND "
                                    "incident_id IS NOT NULL", (it["pkey"], it["key"])):
            if it["number"] is not None and o["number"] != it["number"]:
                continue
            if it["number"] is None and o["number"] is not None:
                continue
            inc = self.conn.execute("SELECT state FROM incidents WHERE id=?",
                                    (o["incident_id"],)).fetchone()
            if inc is not None and inc["state"] != "RESOLVED":
                return True
        return False

    def _current(self, it):
        """The problem row whose current issue is this incident's issue, or None."""
        if not it["pkey"]:
            return None
        p = self._problem(it["pkey"])
        if p is None or p["number"] != it["number"]:
            return None
        return p

    # -- planning (no network) ---------------------------------------------
    def plan(self):
        """Queue creates for new routing rows and updates for mirrored incidents."""
        linked = []
        for row in self.conn.execute(ROWS).fetchall():
            key, title, body = issue_text(row)
            have = self._issue(key)
            if have is not None:
                if have["number"] is not None:  # a further routing row of a mirrored key
                    self._store_bench(key, row["incident_id"], row["run_id"], have["number"])
                    linked.append((key, "link", have["number"], "linked"))
                    self.log("routing %d -> issue #%d (%s already mirrored)"
                             % (row["id"], have["number"], key))
                continue  # else its create is queued; the number lands in every row then
            existing = None
            if row["incident_id"] is not None:
                r = self.conn.execute("SELECT ticket FROM incidents WHERE id=?",
                                      (row["incident_id"],)).fetchone()
                existing = r["ticket"] if r else None
            pkey = problem_key(row)
            if existing and str(existing).isdigit():
                self.state.execute("INSERT INTO issues (key, incident_id, run_id, number, pkey) "
                                   "VALUES (?, ?, ?, ?, ?)", (key, row["incident_id"],
                                                              row["run_id"], int(existing), pkey))
                if pkey:
                    self.state.execute("INSERT OR IGNORE INTO problems (pkey, number) "
                                       "VALUES (?, ?)", (pkey, int(existing)))
                self.state.commit()
                self._store_bench(key, row["incident_id"], row["run_id"], int(existing))
                linked.append((key, "link", int(existing), "linked"))
                continue
            label = ROUTE_LABEL.get(row["route"])
            if label is None or label not in self.label_ids:
                self.log("routing %d: no label for route %r; skipped" % (row["id"],
                                                                        clean(row["route"])))
                continue
            if pkey and self._reuse(key, row, pkey):
                continue
            self.state.execute("INSERT INTO issues (key, incident_id, run_id, pkey) "
                               "VALUES (?, ?, ?, ?)", (key, row["incident_id"], row["run_id"],
                                                       pkey))
            if pkey:
                self.state.execute("INSERT OR REPLACE INTO problems (pkey, number, closed_t) "
                                   "VALUES (?, NULL, NULL)", (pkey,))
            self._enqueue(key, "create", title, body, self.label_ids[label])
            self.state.commit()
        now = self.wall()
        for it in self.state.execute("SELECT * FROM issues WHERE incident_id IS NOT NULL "
                                     "ORDER BY key").fetchall():
            inc = self.conn.execute("SELECT state, closed, duration_s, flaps FROM incidents "
                                    "WHERE id=?", (it["incident_id"],)).fetchone()
            if inc is not None:
                self._plan_updates(it, inc, now)
        return linked

    def _plan_updates(self, it, inc, now):
        key, closed_for = it["key"], it["closed_for"]
        flaps_sent, flap_t = it["flaps_sent"], it["flap_t"]
        flaps = int(inc["flaps"] or 0)
        resolved = inc["state"] == "RESOLVED"
        if not resolved and closed_for is not None:
            p = self._current(it)
            if p is None or p["closed_t"] is not None:  # an open shared issue stays as it is
                self._enqueue(key, "reopen")
                if p is not None:
                    self.state.execute("UPDATE problems SET closed_t=NULL WHERE pkey=?",
                                       (p["pkey"],))
            closed_for = None
        if flaps > flaps_sent and (flap_t is None or now - flap_t >= FLAP_COMMENT_S):
            self._enqueue(key, "comment", body="Flapping: %d flaps (the same board and kind "
                          "reopened within 60 s). State: %s." % (flaps, clean(inc["state"], 20)))
            flaps_sent, flap_t = flaps, now
        if resolved and inc["closed"] is not None and closed_for != inc["closed"]:
            # A flapping incident may reopen within the flap window: close it after the window.
            if flaps == 0 or now - inc["closed"] >= FLAP_COMMENT_S:
                d = inc["duration_s"]
                text = "Resolved after %s s." % (_secs(d) if d is not None else "unknown")
                if flaps:
                    text += " Flaps: %d." % flaps
                    flaps_sent = max(flaps_sent, flaps)
                self._enqueue(key, "comment", body=text)
                if not self._others_open(it):  # a shared issue closes with its last incident
                    self._enqueue(key, "close")
                    if it["pkey"]:
                        self.state.execute("UPDATE problems SET closed_t=? WHERE pkey=? AND "
                                           "(number IS ? OR number IS NULL)",
                                           (now, it["pkey"], it["number"]))
                closed_for = inc["closed"]
        if (closed_for, flaps_sent, flap_t) != (it["closed_for"], it["flaps_sent"], it["flap_t"]):
            self.state.execute("UPDATE issues SET closed_for=?, flaps_sent=?, flap_t=? WHERE key=?",
                               (closed_for, flaps_sent, flap_t, key))
        self.state.commit()

    # -- replay (network) ----------------------------------------------------
    def find_existing(self, key, title, pkey=None):
        """Exact title (and lw-key marker line) via the lagging, fuzzy search, retried."""
        deadline = self.clock() + self.search_wait
        while True:
            for issue in self.fj.search(q=key):
                if not isinstance(issue, dict) or issue.get("title") != title:
                    continue
                if pkey and marker(pkey) not in str(issue.get("body") or "").splitlines():
                    continue
                return int(issue["number"])
            if self.clock() + self.search_step > deadline:
                return None
            self.sleep(self.search_step)

    def _mark(self, op, **cols):
        sets = ", ".join("%s=?" % k for k in cols)
        self.state.execute("UPDATE outbox SET %s WHERE id=?" % sets,
                           list(cols.values()) + [op["id"]])
        self.state.commit()

    def _send(self, op, call):
        """Mark sent, call, and forget the mark when the call certainly changed nothing."""
        self._mark(op, sent=self.wall(), attempts=op["attempts"] + 1)
        try:
            return call()
        except TrackerError as e:
            if _never_reached(e):
                self._mark(op, sent=None)
            raise

    def _create(self, op, it):
        how = "created"
        number = None
        if it["incident_id"] is not None:  # stored in the bench DB before a crash?
            r = self.conn.execute("SELECT ticket FROM incidents WHERE id=?",
                                  (it["incident_id"],)).fetchone()
            if r and r["ticket"] and str(r["ticket"]).isdigit():
                number, how = int(r["ticket"]), "found"
        if number is None and op["sent"] is not None:  # sent before; reply never stored
            number = self.find_existing(op["key"], op["title"], it["pkey"])
            how = "found"
        if number is None:
            how = "created"
            number = self._send(op, lambda: self.fj.create_issue(op["title"], op["body"],
                                                                 [op["label_id"]]))
        self._store_bench(op["key"], it["incident_id"], it["run_id"], number)
        self.state.execute("UPDATE issues SET number=? WHERE key=?", (number, op["key"]))
        if it["pkey"]:
            self.state.execute("UPDATE problems SET number=? WHERE pkey=? AND number IS NULL",
                               (number, it["pkey"]))
            for o in self.state.execute("SELECT * FROM issues WHERE pkey=? AND number IS NULL",
                                        (it["pkey"],)).fetchall():
                self._store_bench(o["key"], o["incident_id"], o["run_id"], number)
                self.state.execute("UPDATE issues SET number=? WHERE key=?", (number, o["key"]))
        self._mark(op, done=self.wall(), how=how, last_error=None)
        return number, how

    def _update(self, op, number):
        if op["op"] == "comment":
            body = "%s\n\n(lab watch update %d)" % (op["body"], op["id"])
            if op["sent"] is not None and body in self.fj.comments(number):
                return "found"
            self._send(op, lambda: self.fj.comment(number, body))
        elif op["op"] == "close":
            self._send(op, lambda: self.fj.close(number))
        else:
            self._send(op, lambda: self.fj.reopen(number))
        return "sent"

    def replay(self):
        """Send queued operations in order. Stops while the tracker is down."""
        done, blocked = [], set()
        for op in self.pending():
            key = op["key"]
            if key in blocked:
                continue
            it = self._issue(key)
            try:
                if op["op"] == "create":
                    number, how = self._create(op, it)
                else:
                    number = it["number"] if it else None
                    if number is None:
                        blocked.add(key)  # its create is not done yet
                        continue
                    how = self._update(op, number)
                    self._mark(op, done=self.wall(), how=how, last_error=None)
            except TrackerError as e:
                self._mark(op, last_error=clean(e, 120))
                if _down(e):
                    self.log("tracker down (%s); %d queued, replay next pass"
                             % (e, len(self.pending())))
                    break
                blocked.add(key)
                self.log("%s %s: tracker refused (%s); kept, retry next pass" % (key, op["op"], e))
                continue
            done.append((key, op["op"], number, how))
            self.log("%s %s -> issue #%d (%s)" % (key, op["op"], number, how))
        return done

    def once(self):
        """Plan, then replay. Returns [(key, op, issue number, how)]."""
        linked = self.plan()
        return linked + self.replay()

def load_labels(path):
    with open(path, encoding="utf-8") as f:
        ids = json.load(f)
    return {k: int(v) for k, v in ids.items()}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=default_db())
    ap.add_argument("--secrets-dir", default=secrets_dir())
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--state", default=os.environ.get("LW_TRACKER_STATE"),
                    help="state DB path (default: tracker_state.db next to the DB)")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)
    state = args.state or os.path.join(os.path.dirname(os.path.abspath(args.db)),
                                       "tracker_state.db")
    labels = load_labels(os.path.join(args.secrets_dir, "tracker_labels.json"))
    fj = Forgejo(os.path.join(args.secrets_dir, "tracker_token"), repo=args.repo)
    conn = connect(args.db)
    sconn = connect_state(state)
    m = Mirror(conn, fj, labels, sconn, log=lambda s: print(s, flush=True))
    try:
        while True:
            try:
                m.once()
            except sqlite3.OperationalError as e:
                print("db busy: %s; retry next pass" % e, flush=True)
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        conn.close()
        sconn.close()


if __name__ == "__main__":
    sys.exit(main())
