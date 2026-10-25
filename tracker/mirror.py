"""Mirror the bench's routing rows into Forgejo: one issue per incident.

    python3 -m tracker.mirror [--db PATH] [--once] [--interval 2]

For each routing row without a ticket (and with a route), create one issue
titled "[lw-<incident id>] <board> <kind>: <triage>", labelled by route, whose
body holds the route, the reason, the verified sha and file:line, and never any
source or diff text. The issue number goes into routing.ticket and
incidents.ticket at once.

Dedupe: the stored number is the key. Before each create the routing id is
written to a small journal next to the DB; it is removed once the number is
stored. A row still in the journal at the next attempt (the mirror died, or a
create timed out) is first searched for by q=lw-<id>, retried for up to 10 s
because the tracker's search lags, and only an exact title match is accepted.

Env: LW_DB (bench DB), LW_SECRETS_DIR (tracker_token, tracker_labels.json),
LW_TRACKER (base URL), LW_TRACKER_STATE (journal path).
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

from tracker.forgejo import DEFAULT_REPO, Forgejo, TrackerError, secrets_dir  # noqa: E402

ROUTE_LABEL = {"Hardware team": "hardware", "Firmware team": "firmware", "Triage": "needs-triage"}
SEARCH_WAIT_S = 10.0
SEARCH_STEP_S = 1.0

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


class Journal:
    """Routing ids whose create was sent but whose number is not yet stored."""

    def __init__(self, path):
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                self.ids = set(int(x) for x in json.load(f).get("pending", []))
        except FileNotFoundError:
            self.ids = set()

    def __contains__(self, rid):
        return rid in self.ids

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pending": sorted(self.ids)}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def add(self, rid):
        self.ids.add(rid)
        self._save()

    def discard(self, rid):
        if rid in self.ids:
            self.ids.discard(rid)
            self._save()


ROWS = """
SELECT r.id, r.incident_id, r.run_id, r.triage, r.route, r.reason,
       i.board, i.kind, i.firmware, i.evidence AS i_evidence,
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
    return key, title, "\n".join(body)


class Mirror:
    def __init__(self, conn, fj, label_ids, journal, search_wait=SEARCH_WAIT_S,
                 search_step=SEARCH_STEP_S, sleep=time.sleep, clock=time.monotonic,
                 log=print):
        self.conn = conn
        self.fj = fj
        self.label_ids = label_ids
        self.journal = journal
        self.search_wait = search_wait
        self.search_step = search_step
        self.sleep = sleep
        self.clock = clock
        self.log = log

    def _store(self, row, number):
        ticket = str(number)
        self.conn.execute("UPDATE routing SET ticket=? WHERE id=? AND ticket IS NULL",
                          (ticket, row["id"]))
        if row["incident_id"] is not None:
            self.conn.execute("UPDATE incidents SET ticket=? WHERE id=? AND ticket IS NULL",
                              (ticket, row["incident_id"]))
        self.conn.commit()

    def _incident_ticket(self, iid):
        if iid is None:
            return None
        r = self.conn.execute("SELECT ticket FROM incidents WHERE id=?", (iid,)).fetchone()
        return r["ticket"] if r else None

    def find_existing(self, key, title):
        """Exact title match via the lagging, fuzzy search, retried up to search_wait."""
        deadline = self.clock() + self.search_wait
        while True:
            for issue in self.fj.search(q=key):
                if isinstance(issue, dict) and issue.get("title") == title:
                    return int(issue["number"])
            if self.clock() + self.search_step > deadline:
                return None
            self.sleep(self.search_step)

    def once(self):
        """One pass. Returns [(routing id, issue number, how)]. Stops at the first tracker error."""
        done = []
        for row in self.conn.execute(ROWS).fetchall():
            rid = row["id"]
            existing = self._incident_ticket(row["incident_id"])
            if existing:
                self._store(row, existing)
                done.append((rid, existing, "linked"))
                self.log("routing %d -> issue #%s (incident already mirrored)" % (rid, existing))
                continue
            label = ROUTE_LABEL.get(row["route"])
            if label is None or label not in self.label_ids:
                self.log("routing %d: no label for route %r; skipped" % (rid, clean(row["route"])))
                continue
            key, title, body = issue_text(row)
            try:
                if rid in self.journal:
                    number = self.find_existing(key, title)
                    if number is not None:
                        self._store(row, number)
                        self.journal.discard(rid)
                        done.append((rid, number, "found"))
                        self.log("routing %d -> issue #%d (found after restart)" % (rid, number))
                        continue
                self.journal.add(rid)
                number = self.fj.create_issue(title, body, [self.label_ids[label]])
            except TrackerError as e:
                if e.refused or (e.status is not None and 400 <= e.status < 500):
                    self.journal.discard(rid)  # never reached, or refused: nothing created
                self.log("routing %d: tracker error: %s; retry next pass" % (rid, e))
                break
            self._store(row, number)
            self.journal.discard(rid)
            done.append((rid, number, "created"))
            self.log("routing %d -> issue #%d (created)" % (rid, number))
        return done


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
                    help="journal path (default: tracker_pending.json next to the DB)")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)
    state = args.state or os.path.join(os.path.dirname(os.path.abspath(args.db)),
                                       "tracker_pending.json")
    labels = load_labels(os.path.join(args.secrets_dir, "tracker_labels.json"))
    fj = Forgejo(os.path.join(args.secrets_dir, "tracker_token"), repo=args.repo)
    conn = connect(args.db)
    m = Mirror(conn, fj, labels, Journal(state), log=lambda s: print(s, flush=True))
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


if __name__ == "__main__":
    sys.exit(main())
