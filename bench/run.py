"""The bench: serial owner -> incidents -> triage -> SQLite and the alerts outbox.

    python3 -m bench.run [--port P] [--board nucleo-1] [--db PATH]
                         [--expect-flash <sha> [--flash-window 120]]

--expect-flash records a flash of <sha> made by hand (until stage A3 flashes
itself): everything is discarded until "LW BOOT fw=<sha>", that boot counts
as verified and the run's RESULT PASS enters the pass history. Without it
nothing is verified and triage is UNKNOWN.

The first alert is written to the outbox in the same transaction that opens
the incident; bench.sender (a separate process) posts it. After it, if the
board has printed LW RESULT and one LW ALIVE, the bench sends "test loopback"
and attaches the reply as a follow-up (transition "diag"). Then the
explanation worker runs "python3 -m agent.explain --incident <id>" (60 s,
one at a time, newest first) and queues its text, or "explanation
unavailable; triage stands", as the "explain" row replying to the first alert.

Board text is data: it is stored, quoted and matched, never executed.
"""

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time

from bench import alerts, store
from bench.incidents import Incident, Machine, FLASH_WINDOW_S, sha_match
from bench.serial_owner import wall_ts
from bench.triage import PassHistory, judge_incident, judge_run, route_for

DEFAULT_BOARD = "nucleo-1"
DIAG_CMD = "test loopback"
DIAG_TIMEOUT_S = 3.0
EXPLAIN_TIMEOUT_S = 60.0
HAND_FLASH_WINDOW_S = 120.0
SHA_RE = re.compile(r"[0-9a-f]{7,40}")


class ExplainWorker:
    """Runs the explanation command for one incident at a time, newest first."""

    def __init__(self, db, cmd=None, timeout=EXPLAIN_TIMEOUT_S, cwd=store.REPO, agent_dir=None,
                 log=print):
        self.db = db
        self.cmd = cmd or [sys.executable, "-m", "agent.explain"]
        self.timeout = timeout
        self.cwd = cwd
        self.agent_dir = agent_dir if agent_dir is not None else os.path.join(cwd, "agent")
        self.log = log
        self._pending = set()
        self._cv = threading.Condition()
        self._stop = False
        self._thread = None

    def queue(self, incident_id):
        with self._cv:
            self._pending.add(incident_id)
            self._cv.notify()

    def pending(self):
        with self._cv:
            return sorted(self._pending)

    def _pop(self, wait):
        with self._cv:
            while not self._pending and wait and not self._stop:
                self._cv.wait(0.5)
            if not self._pending:
                return None
            iid = max(self._pending)
            self._pending.discard(iid)
            return iid

    def explain(self, iid):
        """(ok, text) from the agent command."""
        if not os.path.isdir(self.agent_dir):
            return False, None
        try:
            p = subprocess.run(self.cmd + ["--incident", str(iid)], cwd=self.cwd,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=self.timeout)
        except (subprocess.TimeoutExpired, OSError):
            return False, None
        text = p.stdout.decode("utf-8", "replace")
        text = "".join(c for c in text if c in "\n\t" or (" " <= c < "\x7f"))
        if p.returncode != 0 or not text.strip():
            return False, None
        return True, text

    def run_one(self, wait=False):
        iid = self._pop(wait)
        if iid is None:
            return None
        ok, text = self.explain(iid)
        conn = store.connect(self.db)
        try:
            with conn:
                aid = store.add_alert(conn, iid, "explain", alerts.explain_text(iid, text),
                                      time.time())
        finally:
            conn.close()
        self.log("bench: incident %d explanation %s (alert %s)" % (
            iid, "queued" if ok else "unavailable", aid))
        return iid, ok

    def start(self):
        def loop():
            while not self._stop:
                try:
                    self.run_one(wait=True)
                except Exception as e:  # keep the worker alive
                    self.log("bench: explain worker error %s" % e.__class__.__name__)
        self._thread = threading.Thread(target=loop, name="explain", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        with self._cv:
            self._stop = True
            self._cv.notify()


class Bench:
    def __init__(self, conn, board=DEFAULT_BOARD, send=None, explainer=None,
                 diag_timeout=DIAG_TIMEOUT_S, log=print, machine=None):
        self.conn = conn
        self.board = board
        self.send = send
        self.explainer = explainer
        self.diag_timeout = diag_timeout
        self.log = log
        self.m = machine or Machine(board)
        self.history = PassHistory(
            (r["sha"], r["t"]) for r in conn.execute(
                "SELECT sha, t FROM pass_history WHERE board=? ORDER BY id", (board,)))
        self.diag = None
        self._last_flash_id = None
        self._load()

    # ---- startup ----
    def _load(self):
        r = self.conn.execute("SELECT verified_sha FROM bench_state WHERE board=?",
                              (self.board,)).fetchone()
        if r and r["verified_sha"]:
            self.m.verified_sha = r["verified_sha"]
        cutoff = time.time() - self.m.flap_s - self.m.resolve_hold_s
        rows = self.conn.execute(
            "SELECT * FROM incidents WHERE board=? AND (state!='RESOLVED' OR closed>=?) "
            "ORDER BY id", (self.board, cutoff)).fetchall()
        for row in rows:
            ev = json.loads(row["evidence"] or "{}")
            inc = Incident(0, self.board, row["kind"], row["opened"], row["t_first_bad_line"],
                           (ev.get("lines") or [None])[0], ev.get("facts"))
            inc.cites = [c for c in ev.get("lines") or [] if c]
            inc.state = row["state"]
            inc.closed = row["closed"]
            inc.flaps = inc.flaps_alerted = row["flaps"]
            inc.db_id = row["id"]
            inc.evidence = ev
            fa = self.conn.execute("SELECT MAX(created) AS t FROM alerts WHERE incident_id=? AND "
                                   "transition LIKE 'flap-%'", (row["id"],)).fetchone()
            inc.last_flap_alert = fa["t"] if fa else None
            has_res = self.conn.execute("SELECT 1 FROM alerts WHERE incident_id=? AND "
                                        "transition='resolve'", (row["id"],)).fetchone()
            inc.resolve_alerted = bool(has_res)
            if inc.state == "RESOLVED" and not has_res and row["kind"] != "REBOOT":
                inc.resolve_due = (row["closed"] or time.time()) + self.m.resolve_hold_s
            self.m.restore(inc)
            if inc.state != "RESOLVED":
                self.log("bench: reloaded open incident %d (%s)" % (row["id"], row["kind"]))

    # ---- inputs ----
    def expect_flash(self, sha, window=HAND_FLASH_WINDOW_S, t=None, how="hand"):
        t = time.time() if t is None else t
        with self.conn:
            self._last_flash_id = store.insert(self.conn, "flashes", {
                "board": self.board, "sha": sha, "how": how, "t": t})
            self._set_verified(None, t)
        self.apply(self.m.expect_flash(t, sha, window), t)
        self.log("bench: expecting LW BOOT fw=%s within %.0f s; discarding until then" % (
            sha, window))

    def on_event(self, ev):
        if ev.kind == "LINE":
            self.line(ev.t_wall, ev.raw_ts, ev.records)
        elif ev.kind == "LINK_UP":
            self.apply(self.m.link_up(ev.t_wall), ev.t_wall)
        elif ev.kind == "LINK_DOWN":
            if self.diag is not None:
                self._finish_diag(ev.t_wall)
            self.apply(self.m.link_down(ev.t_wall), ev.t_wall)

    def line(self, t, raw_ts, records):
        for rec in records:
            exp = self.m.expect
            if exp is not None and not (rec.get("kind") == "BOOT"
                                        and sha_match(rec.get("fw"), exp["sha"])):
                self.m.record(t, rec, None)  # discarded: counted, not stored
                continue
            fields = {k: v for k, v in rec.items()}
            with self.conn:
                eid = store.event(self.conn, t, self.board, rec.get("kind", "OTHER"), fields,
                                  raw_ts)
            cite = {"event_id": eid, "raw_ts": raw_ts, "line": rec.get("line", "")}
            self._diag_line(t, rec, cite)
            self.apply(self.m.record(t, rec, cite), t)

    def tick(self, t=None):
        t = time.time() if t is None else t
        if self.diag is not None and t >= self.diag["deadline"]:
            self._finish_diag(t)
        self.apply(self.m.tick(t), t)

    # ---- outputs ----
    def apply(self, outs, t):
        for o in outs:
            typ = o["type"]
            if typ == "open":
                self._open(o)
            elif typ == "flap":
                self._flap(o)
            elif typ == "flap_alert":
                self._flap_alert(o["inc"], o["t"], None)
            elif typ == "resolve":
                self._resolve(o)
            elif typ == "resolve_alert":
                self._resolve_alert(o["inc"], o["t"])
            elif typ == "run_start":
                self._run_start(o)
            elif typ == "run_end":
                self._run_end(o)
            elif typ == "log":
                self._log(o)
        for o in outs:  # failed runs that opened no incident still get a routing row
            run = o.get("run") if o["type"] == "run_end" else None
            if run and run["verdict"] not in ("PASS", "ABORTED") and not run.get("routed"):
                with self.conn:
                    self._route(o["t"], run["triage"], run["reason"], run_id=run["run_id"])
                run["routed"] = True

    def _set_verified(self, sha, t):
        self.conn.execute("INSERT INTO bench_state (board, verified_sha, t) VALUES (?, ?, ?) "
                          "ON CONFLICT(board) DO UPDATE SET verified_sha=excluded.verified_sha, "
                          "t=excluded.t", (self.board, sha, t))

    def _route(self, t, triage, reason, incident_id=None, run_id=None):
        if triage == "NONE":
            return None
        return store.insert(self.conn, "routing", {
            "incident_id": incident_id, "run_id": run_id, "triage": triage,
            "route": route_for(triage), "reason": reason, "created": t})

    def _row(self, iid):
        return self.conn.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()

    def _write_evidence(self, inc):
        ev = getattr(inc, "evidence", None) or {}
        ev["lines"] = inc.cites
        ev["facts"] = inc.facts
        ev["repeats"] = inc.repeats
        ev.setdefault("file_line", None)
        inc.evidence = ev
        return store.dumps(ev)

    def _open(self, o):
        inc, t = o["inc"], o["t"]
        run = inc.run
        sha = run["sha"] if run else self.m.verified_sha
        if inc.kind == "RUN_FAIL" or (inc.kind == "HARDFAULT" and run):
            triage, reason = run["triage"], run["reason"]
        else:
            triage, reason = judge_incident(inc.kind, inc.facts, inc.cites, sha, self.history)
        route = route_for(triage)
        t_pass = self.history.passed(sha) if sha else None
        lp_sha, lp_t = (sha, t_pass) if t_pass is not None else self.history.last()
        inc.evidence = {"run_id": run["run_id"] if run else None}
        with self.conn:
            iid = store.insert(self.conn, "incidents", {
                "board": self.board, "kind": inc.kind, "state": "OPEN", "opened": t,
                "flaps": 0, "firmware": sha, "last_pass_sha": lp_sha, "last_pass_t": lp_t,
                "triage": triage, "reason": reason, "evidence": self._write_evidence(inc),
                "route": route, "t_first_bad_line": inc.t_first_bad, "t_open": t})
            inc.db_id = iid
            self._route(t, triage, reason, incident_id=iid,
                        run_id=run["run_id"] if run else None)
            if run:
                run["routed"] = True
            store.event(self.conn, t, self.board, "INCIDENT_OPEN",
                        {"incident_id": iid, "incident_kind": inc.kind, "triage": triage,
                         "run_id": run["run_id"] if run else None})
            aid = store.add_alert(self.conn, iid, "open",
                                  alerts.alert_text("open", self._row(iid), o.get("cite")), t)
        self.log("bench: incident %d OPEN %s triage=%s route=%s (alert %s)" % (
            iid, inc.kind, triage, route or "none", aid))
        self._after_first_alert(inc, t)

    def _after_first_alert(self, inc, t):
        if self.send and self.diag is None and self.m.can_send() and self.send(DIAG_CMD):
            self.diag = {"inc": inc, "deadline": t + self.diag_timeout, "cites": [],
                         "cmd": DIAG_CMD, "t": t}
            return
        inc.evidence["diagnostic"] = {"cmd": DIAG_CMD, "sent": False,
                                      "why": "the board has not printed LW RESULT and one "
                                             "LW ALIVE since its boot, or the link is down"}
        with self.conn:
            self.conn.execute("UPDATE incidents SET evidence=? WHERE id=?",
                              (self._write_evidence(inc), inc.db_id))
        if self.explainer:
            self.explainer.queue(inc.db_id)

    def _diag_line(self, t, rec, cite):
        d = self.diag
        if d is None:
            return
        k = rec.get("kind")
        if k == "TEST" and rec.get("name") == "loopback":
            d["cites"].append(cite)
        elif k == "REPLY" and rec.get("cmd") == "test":
            d["cites"].append(cite)
            self._finish_diag(t)

    def _finish_diag(self, t):
        d, self.diag = self.diag, None
        inc = d["inc"]
        inc.evidence["diagnostic"] = {"cmd": d["cmd"], "sent": True, "reply": d["cites"]}
        with self.conn:
            self.conn.execute("UPDATE incidents SET evidence=? WHERE id=?",
                              (self._write_evidence(inc), inc.db_id))
            aid = store.add_alert(self.conn, inc.db_id, "diag",
                                  alerts.diag_text(inc.db_id, d["cmd"], d["cites"],
                                                   self.diag_timeout), t)
        self.log("bench: incident %d diagnostic %s (alert %s)" % (
            inc.db_id, "replied" if d["cites"] else "no reply", aid))
        if self.explainer:
            self.explainer.queue(inc.db_id)

    def _flap(self, o):
        inc, t = o["inc"], o["t"]
        with self.conn:
            self.conn.execute("UPDATE incidents SET state='FLAPPING', flaps=?, closed=NULL, "
                              "duration_s=NULL, evidence=? WHERE id=?",
                              (inc.flaps, self._write_evidence(inc), inc.db_id))
            store.event(self.conn, t, self.board, "INCIDENT_FLAP",
                        {"incident_id": inc.db_id, "flaps": inc.flaps})
            run = o.get("run")
            if run:
                self._route(t, run["triage"], run["reason"], incident_id=inc.db_id,
                            run_id=run["run_id"])
                run["routed"] = True
        self.log("bench: incident %d FLAPPING (%d flaps)%s" % (
            inc.db_id, inc.flaps, "" if o["alert"] else ", alert held (one per 60 s)"))
        if o["alert"]:
            self._flap_alert(inc, t, o.get("cite"))

    def _flap_alert(self, inc, t, cite):
        with self.conn:
            store.add_alert(self.conn, inc.db_id, "flap-%d" % inc.flaps,
                            alerts.alert_text("flap-%d" % inc.flaps, self._row(inc.db_id),
                                              cite or (inc.cites[-1] if inc.cites else None)), t)

    def _resolve(self, o):
        inc, t = o["inc"], o["t"]
        with self.conn:
            self.conn.execute("UPDATE incidents SET state='RESOLVED', closed=?, duration_s=?, "
                              "evidence=? WHERE id=?",
                              (t, o["duration"], self._write_evidence(inc), inc.db_id))
            store.event(self.conn, t, self.board, "INCIDENT_RESOLVE",
                        {"incident_id": inc.db_id, "duration_s": o["duration"]})
        inc.resolve_cite = o.get("cite")
        self.log("bench: incident %d RESOLVED after %.1f s" % (inc.db_id, o["duration"]))

    def _resolve_alert(self, inc, t):
        with self.conn:
            store.add_alert(self.conn, inc.db_id, "resolve",
                            alerts.alert_text("resolve", self._row(inc.db_id),
                                              getattr(inc, "resolve_cite", None)), t)

    def _run_start(self, o):
        t = o["t"]
        with self.conn:
            if self._last_flash_id:
                self.conn.execute("UPDATE flashes SET verified_t=?, result='VERIFIED' WHERE id=?",
                                  (t, self._last_flash_id))
            self._set_verified(o["sha"], t)
            store.event(self.conn, t, self.board, "RUN_START",
                        {"sha": o["sha"], "discarded": o["discarded"]})
        self.log("bench: verified LW BOOT fw=%s (%d earlier records discarded)" % (
            o["sha"], o["discarded"]))

    def _run_end(self, o):
        run, t = o["run"], o["t"]
        if run["verdict"] == "ABORTED":
            return
        sha = run["sha"] if run["verdict"] != "FLASH_ERROR" else None
        triage, reason = judge_run(run["verdict"], run["tests"], run["sha"], self.history,
                                   cites=run["cites"])
        run["triage"], run["reason"] = triage, reason
        evidence = {"lines": run["cites"][:20], "tests": run["tests"],
                    "discarded": run.get("discarded", 0)}
        with self.conn:
            run["run_id"] = store.insert(self.conn, "runs", {
                "commit": sha, "subject": None, "verdict": run["verdict"], "triage": triage,
                "evidence": store.dumps(evidence),
                "duration_s": round(t - run["t_start"], 3), "t_start": run["t_start"],
                "t_end": t})
            if run["verdict"] == "PASS":
                store.insert(self.conn, "pass_history", {"board": self.board, "sha": run["sha"],
                                                         "t": t, "run_id": run["run_id"]})
                self.history.add(run["sha"], t)
            if run["verdict"] == "FLASH_ERROR":
                if self._last_flash_id:
                    self.conn.execute("UPDATE flashes SET result='FLASH_ERROR' WHERE id=?",
                                      (self._last_flash_id,))
                self._set_verified(None, t)
            store.event(self.conn, t, self.board, "RUN_END",
                        {"run_id": run["run_id"], "verdict": run["verdict"], "triage": triage})
        self.log("bench: run %d of %s %s triage=%s" % (run["run_id"], run["sha"],
                                                       run["verdict"], triage))

    def _log(self, o):
        what = o["what"]
        fields = {k: v for k, v in o.items() if k not in ("type", "inc", "t")}
        if "inc" in o:
            fields["incident_id"] = o["inc"].db_id
        with self.conn:
            store.event(self.conn, o["t"], self.board, what, fields)
            if what == "UNVERIFIED":
                self._set_verified(None, o["t"])
        if what == "REPLUG":
            self.log("bench: replug (%s), no alert" % o.get("how"))
        elif what in ("LINK_DOWN", "LINK_UP", "UNVERIFIED", "RUN_ABORTED"):
            self.log("bench: %s" % what)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m bench.run")
    ap.add_argument("--port", help="serial port (default: first /dev/serial/by-id/*STM*)")
    ap.add_argument("--board", default=DEFAULT_BOARD)
    ap.add_argument("--db", help="bench DB (default $LW_DB or %s)" % store.DEFAULT_DB)
    ap.add_argument("--expect-flash", metavar="SHA",
                    help="a flash of SHA made by hand: verify it by its LW BOOT line")
    ap.add_argument("--flash-window", type=float, default=HAND_FLASH_WINDOW_S,
                    help="seconds to wait for LW BOOT fw=SHA (default %(default)s)")
    ap.add_argument("--explain-timeout", type=float, default=EXPLAIN_TIMEOUT_S)
    ap.add_argument("--no-explain", action="store_true")
    a = ap.parse_args(argv)
    if a.expect_flash is not None and not SHA_RE.fullmatch(a.expect_flash):
        print("bench: --expect-flash needs 7-40 lowercase hex characters", file=sys.stderr)
        return 2

    from bench.serial_owner import SerialOwner
    db = store.db_path(a.db)
    conn = store.open_db(db)
    log = lambda s: print("%s %s" % (wall_ts(time.time()), s), flush=True)  # noqa: E731
    explainer = None if a.no_explain else ExplainWorker(db, timeout=a.explain_timeout,
                                                        log=log).start()
    owner = SerialOwner(port=a.port)
    bench = Bench(conn, a.board, send=owner.send, explainer=explainer, log=log)
    if a.expect_flash:
        bench.expect_flash(a.expect_flash, a.flash_window)
    owner.start()
    last_hb = 0.0
    try:
        while True:
            try:
                ev = owner.events.get(timeout=0.1)
            except queue.Empty:
                ev = None
            if ev is not None:
                bench.on_event(ev)
            now = time.time()
            bench.tick(now)
            if now - last_hb >= 1.0:
                store.heartbeat(conn, now)
                last_hb = now
    except KeyboardInterrupt:
        pass
    finally:
        owner.stop()
        if explainer:
            explainer.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
