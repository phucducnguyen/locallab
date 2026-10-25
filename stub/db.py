"""Shared helpers for the stub: open a DB with the day schema and write records.

Every value written here is invented for the stub. Nothing comes from the
firmware repo's logs or bug table.
"""

import json
import os
import sqlite3
import time

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "docs", "schema.sql")

BOARD = "nucleo-1"
SHA_PASS = "1111111"
SHA_NEW = "2222222"
FILE_LINE = "src/demo.c:42"

ROUTES = {"HARDWARE": "Hardware team", "CODE": "Firmware team", "UNKNOWN": "Triage", "NONE": None}


def connect(path):
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(conn):
    with open(SCHEMA, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()


def raw_ts(t):
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        t, ms = int(t) + 1, 0
    return time.strftime("%H:%M:%S", time.gmtime(int(t))) + ".%03d" % ms


def alert_text(transition, inc, line=None, line_ts=None):
    """The fixed alert wording (docs/01-day-contract.md, "Alerts")."""
    if transition == "open":
        head = "LAB WATCH incident %d OPEN" % inc["id"]
    elif transition == "resolve":
        head = "LAB WATCH incident %d RESOLVED after %.0f s" % (inc["id"], inc["duration_s"] or 0)
    elif transition.startswith("flap-"):
        head = "LAB WATCH incident %d FLAPPING (%d flaps)" % (inc["id"], inc["flaps"])
    else:
        raise ValueError(transition)
    out = [head,
           "Board: %s" % inc["board"],
           "Kind: %s" % inc["kind"],
           "Triage: %s" % inc["triage"],
           "Routed to: %s" % (inc["route"] or "none"),
           "Reason: %s" % inc["reason"]]
    if line:
        out.append("Line: [%s] %s" % (line_ts, line))
    fl = json.loads(inc["evidence"] or "{}").get("file_line")
    if fl:
        out.append("File: %s" % fl)
    out.append("Incident: %d" % inc["id"])
    return "\n".join(out)


class Writer:
    """Appends records the way the live bench would, one commit per call."""

    def __init__(self, conn, board=BOARD):
        self.conn = conn
        self.board = board

    def _insert(self, table, row):
        cols = ", ".join('"%s"' % k for k in row)
        marks = ", ".join("?" for _ in row)
        cur = self.conn.execute("INSERT INTO %s (%s) VALUES (%s)" % (table, cols, marks),
                                list(row.values()))
        self.conn.commit()
        return cur.lastrowid

    def event(self, t, kind, **fields):
        return self._insert("events", {"t_wall": t, "board": self.board, "kind": kind,
                                       "fields": json.dumps(fields, sort_keys=True),
                                       "raw_ts": raw_ts(t)})

    def serial(self, t, line, kind, **fields):
        """A parsed LW line; returns its citation {"event_id", "raw_ts", "line"}."""
        eid = self.event(t, kind, line=line, **fields)
        return {"event_id": eid, "raw_ts": raw_ts(t), "line": line}

    def health(self, t, n, state):
        return self.serial(t, "LW HEALTH %d loopback=%s" % (n, state), "HEALTH",
                           n=n, loopback=state)

    def open_incident(self, t, kind, triage, reason, evidence, firmware=SHA_PASS,
                      last_pass=(SHA_PASS, None), t_first_bad=None):
        row = {"board": self.board, "kind": kind, "state": "OPEN", "opened": t,
               "flaps": 0, "firmware": firmware, "last_pass_sha": last_pass[0],
               "last_pass_t": last_pass[1], "triage": triage, "reason": reason,
               "evidence": json.dumps(evidence, sort_keys=True), "route": ROUTES[triage],
               "t_first_bad_line": t_first_bad, "t_open": t}
        iid = self._insert("incidents", row)
        self.route(t, triage, reason, incident_id=iid)
        self.event(t, "INCIDENT_OPEN", incident_id=iid, incident_kind=kind, triage=triage)
        return iid

    def route(self, t, triage, reason, incident_id=None, run_id=None):
        if triage == "NONE":
            return None
        return self._insert("routing", {"incident_id": incident_id, "run_id": run_id,
                                        "triage": triage, "route": ROUTES[triage],
                                        "reason": reason, "created": t})

    def incident(self, iid):
        return self.conn.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()

    def update_incident(self, iid, **cols):
        sets = ", ".join("%s=?" % k for k in cols)
        self.conn.execute("UPDATE incidents SET %s WHERE id=?" % sets, list(cols.values()) + [iid])
        self.conn.commit()

    def alert(self, t, iid, transition, cite=None, sent_after=0.4):
        """Queue an alert and mark it sent, as the bench's sender would."""
        inc = self.incident(iid)
        reply_to = None
        if transition != "open":
            r = self.conn.execute("SELECT id FROM alerts WHERE incident_id=? AND transition='open'",
                                  (iid,)).fetchone()
            reply_to = r["id"] if r else None
        text = alert_text(transition, inc, cite and cite["line"], cite and cite["raw_ts"])
        aid = self._insert("alerts", {"incident_id": iid, "transition": transition, "text": text,
                                      "reply_to_alert_id": reply_to,
                                      "telegram_message_id": 5000 + iid * 10 + self._alert_count(iid),
                                      "created": t, "sent": t + sent_after})
        if transition == "open":
            self.update_incident(iid, t_alert_sent=t + sent_after)
        self.event(t + sent_after, "ALERT_SENT", incident_id=iid, alert_id=aid,
                   transition=transition)
        return aid

    def _alert_count(self, iid):
        return self.conn.execute("SELECT COUNT(*) FROM alerts WHERE incident_id=?",
                                 (iid,)).fetchone()[0]

    def resolve(self, t, iid):
        inc = self.incident(iid)
        self.update_incident(iid, state="RESOLVED", closed=t, duration_s=round(t - inc["opened"], 3))
        self.event(t, "INCIDENT_RESOLVE", incident_id=iid, duration_s=round(t - inc["opened"], 3))

    def run(self, t_start, commit, subject, verdict, triage, evidence, t_end):
        return self._insert("runs", {"commit": commit, "subject": subject, "verdict": verdict,
                                     "triage": triage,
                                     "evidence": json.dumps(evidence, sort_keys=True),
                                     "duration_s": round(t_end - t_start, 3),
                                     "t_start": t_start, "t_end": t_end})
