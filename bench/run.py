"""The bench: serial owner -> incidents -> triage -> SQLite and the alerts outbox.

    python3 -m bench.run [--port P] [--board nucleo-1] [--db PATH]
                         [--flash <sha> [--repo ~/bench-fw] [--then-exit]]
                         [--expect-flash <sha> [--flash-window 120]]

--flash builds <sha> from the bench repo's main (bench.flash: git archive,
make FW_VERSION=<short sha>, elf/<sha>.elf), pauses incident detection for
the board, copies build/fw.bin to the NUCLEO drive and syncs. Everything the
board prints is discarded until "LW BOOT fw=<sha>"; none within 15 s of the
copy is FLASH_ERROR (or NO_OUTPUT / GARBLED, by what did arrive), never a
pass. The run is then captured until RESULT, 3 s past the first HARDFAULT,
or 15 s. The bench repo is checked first (bench.check_bench_repo) and the
bench refuses to start on a repo that fails. --then-exit stops after that
run (exit 0 on PASS, 1 otherwise).

--expect-flash is the fallback for a flash made by hand: the same discard
and verification, with no build or copy. Without either, nothing is
verified and triage is UNKNOWN.

The first alert is written to the outbox in the same transaction that opens
the incident; bench.sender (a separate process) posts it. After it, if the
board has printed LW RESULT and one LW ALIVE, the bench sends "test loopback"
and attaches the reply as a follow-up (transition "diag"). Then the
explanation worker runs "python3 -m agent.explain --incident <id>" (60 s,
one at a time, newest first) and queues its text, or "explanation
unavailable; triage stands", as the "explain" row replying to the first alert.

Integration (I1):
- Requests (bench.requests): at startup running rows are failed (a killed
  bench never redoes them). When nothing is being flashed or run and the link
  is up, the oldest pending row is claimed: "run <sha>" goes through the same
  FlashJob as --flash and is finished with its run's verdict and run_id;
  "test <name>" is sent only when the board allows it (LW RESULT then one
  LW ALIVE) and is finished on its LW REPLY test or after 3 s.
- --watch starts bench.watcher in a thread with its own DB connection: a new
  head of ~/bench-fw main queues a run request.
- A HARDFAULT (incident or run) gets bench.decode.fault_evidence merged into
  its evidence before the insert, so the first alert carries Fault:/File:.
  addr2line gets 300 ms; slower, the alert goes first and the decode follows
  as a "decode" alert.
- An UNKNOWN result that bench.confirm settles is confirmed after the first
  alert and the diagnostic, on a worker thread: the last passing sha is
  reflashed through the same FlashJob (detection is paused by the flash) and
  the "confirmed" alert written; bench_state then names the reflashed sha.
- LW DUMP bursts (bench.dump) are paired with the HARDFAULT record they
  precede, which then carries rec["dump"].
- agent.fix: bench.flash has no flash_and_capture(src_dir, ...) (a patched
  tree is not a commit on main and the serial port belongs to this process).
  Use agent.fix's verify_cmd with the bench stopped for the fix beat.

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

from bench import alerts, confirm, decode, fwrepo, requests, store
from bench.dump import DumpCollector
from bench.parser import FAULT_FIELDS
from bench.incidents import Incident, Machine, COPY_WINDOW_S, FLASH_WINDOW_S, sha_match
from bench.serial_owner import wall_ts
from bench.triage import PassHistory, judge_incident, judge_run, route_for

DEFAULT_BOARD = "nucleo-1"
DIAG_CMD = "test loopback"
DIAG_TIMEOUT_S = 3.0
EXPLAIN_TIMEOUT_S = 60.0
HAND_FLASH_WINDOW_S = 120.0
FAULT_TAIL_S = 3.0
TEST_TIMEOUT_S = 3.0
DECODE_BUDGET_S = 0.3
CONFIRM_TIMEOUT_S = 420.0
ELF_DIR = os.path.join(os.path.dirname(store.DEFAULT_DB), "elf")
BOOTED = ("PASS", "FAIL", "HARDFAULT", "RESET_LOOP")
TAIL_MAX = 20
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
                 diag_timeout=DIAG_TIMEOUT_S, log=print, machine=None,
                 fault_tail_s=FAULT_TAIL_S, repo=None, elf_dir=None,
                 decode_runner=subprocess.run, addr2line_tool=None,
                 decode_budget_s=DECODE_BUDGET_S, test_timeout=TEST_TIMEOUT_S):
        self.conn = conn
        self.repo = repo
        self.elf_dir = os.path.expanduser(elf_dir or ELF_DIR)
        self.decode_runner = decode_runner
        self.addr2line_tool = addr2line_tool
        self.decode_budget_s = decode_budget_s
        self.test_timeout = test_timeout
        self.dumps = DumpCollector()
        self.call = None         # Loop.call: run a function on the bench thread
        self.make_flash = None   # make_flash(sha) -> a started FlashJob
        self.job = None          # the FlashJob in progress (requests, --flash, confirm)
        self.request = None      # the bench_requests row being served
        self.confirming = False
        self._cur = None         # the record being applied (decode cache)
        self._decodes = []       # decodes that missed the budget
        self.board = board
        self.send = send
        self.explainer = explainer
        self.diag_timeout = diag_timeout
        self.fault_tail_s = fault_tail_s
        self.log = log
        self.m = machine or Machine(board)
        self.history = PassHistory(
            (r["sha"], r["t"]) for r in conn.execute(
                "SELECT sha, t FROM pass_history WHERE board=? ORDER BY id", (board,)))
        self.diag = None
        self._last_flash_id = None
        self._flash = None       # {"sha", "subject", "elf"} of the bench's own flash
        self.tail = None         # lines kept after the first HARDFAULT of a run
        self.finished = []       # (run_id, verdict) once a run's capture is over
        self.probe = None        # probe(run_id): ask the port which rate decodes (GARBLED)
        self._load()

    # ---- startup ----
    def _load(self):
        requests.init(self.conn)
        n = requests.recover_running(self.conn, time.time())
        if n:
            self.log("bench: %d request(s) left running by a restart marked failed" % n)
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
    def expect_flash(self, sha, window=HAND_FLASH_WINDOW_S, t=None, how="hand", copied=True):
        t = time.time() if t is None else t
        if how == "hand":
            self._flash = {"sha": sha, "subject": None, "elf": None}
        with self.conn:
            self._last_flash_id = store.insert(self.conn, "flashes", {
                "board": self.board, "sha": sha, "how": how, "t": t})
            self._set_verified(None, t)
        self.apply(self.m.expect_flash(t, sha, window, copied=copied), t)
        if copied:
            self.log("bench: expecting LW BOOT fw=%s within %.0f s; discarding until then" % (
                sha, window))
        else:
            self.log("bench: flashing %s; detection paused, discarding until its LW BOOT" % sha)

    def begin_flash(self, sha, subject=None, elf=None, t=None, window=COPY_WINDOW_S):
        """The bench's own flash starts (before the copy): pause and discard."""
        self._flash = {"sha": sha, "subject": subject, "elf": elf}
        self.expect_flash(sha, window, t, how="usb", copied=False)

    def flash_copied(self, msg="", t=None, window=FLASH_WINDOW_S, copy_error=None):
        """The copy finished and synced: LW BOOT fw=<sha> is due within window."""
        t = time.time() if t is None else t
        self.apply(self.m.flash_copied(t, window, copy_error), t)
        self.log("bench: copy done (%s); LW BOOT fw=%s due within %.0f s" % (
            msg, self.m.expect["sha"] if self.m.expect else "?", window))

    def flash_failed(self, why, t=None):
        t = time.time() if t is None else t
        self.log("bench: FLASH_ERROR: %s" % why)
        self.apply(self.m.flash_failed(t, why), t)

    def build_failed(self, b, t=None):
        """A BUILD_ERROR run for bench.flash.build's result b (nothing was flashed)."""
        t = time.time() if t is None else t
        bench_side = b.get("bench_fault") or b.get("toolchain_missing")
        why = ("bench: " if bench_side else "") + (b.get("why") or "build failed")
        if b.get("toolchain_missing") and not b.get("bench_fault"):
            why += " (toolchain %sgcc not found)" % (b.get("toolchain") or "arm-none-eabi-")
        self._flash = {"sha": b.get("sha"), "subject": b.get("subject"), "elf": None}
        run = self.m._unverified_run(t, b.get("sha"), "BUILD_ERROR", t, why=why[:200],
                                     build_log=b.get("log_tail", [])[-6:])
        self.apply([{"type": "run_end", "run": run, "t": t}], t)

    def attach_probe(self, run_id, result):
        """Record which standard rate (if any) decoded a GARBLED run's bytes."""
        row = self.conn.execute("SELECT evidence FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return
        ev = json.loads(row["evidence"] or "{}")
        ev["rate_probe"] = result
        with self.conn:
            self.conn.execute("UPDATE runs SET evidence=? WHERE run_id=?",
                              (store.dumps(ev), run_id))
        rate = (result or {}).get("decodes_at")
        self.log("bench: run %d GARBLED; %s" % (
            run_id, "bytes decode at %s baud" % rate if rate else
            "no standard rate decodes the bytes"))

    def on_event(self, ev):
        if ev.kind == "LINE":
            if ev.nonlw:
                self.m.noise(ev.t_wall, len(ev.raw), ev.bad_bytes)
            self.line(ev.t_wall, ev.raw_ts, ev.records)
        elif ev.kind == "LINK_UP":
            self.apply(self.m.link_up(ev.t_wall), ev.t_wall)
        elif ev.kind == "LINK_DOWN":
            if self.diag is not None:
                self._finish_diag(ev.t_wall)
            self.apply(self.m.link_down(ev.t_wall), ev.t_wall)

    def line(self, t, raw_ts, records):
        for rec in records:
            if self.m.expect is not None and not self.m.verifies(t, rec):
                self.m.record(t, rec, None)  # discarded: counted, not stored
                continue
            fields = {k: v for k, v in rec.items()}
            with self.conn:
                eid = store.event(self.conn, t, self.board, rec.get("kind", "OTHER"), fields,
                                  raw_ts)
            cite = {"event_id": eid, "raw_ts": raw_ts, "line": rec.get("line", "")}
            d = self.dumps.feed(t, rec, cite)
            if d is not None:
                rec["dump"] = d
            if self.tail is not None and len(self.tail["lines"]) < TAIL_MAX:
                self.tail["lines"].append(cite)
            self._diag_line(t, rec, cite)
            self._request_line(t, rec, cite)
            self._cur = {"rec": rec}
            try:
                self.apply(self.m.record(t, rec, cite), t)
            finally:
                self._cur = None

    def tick(self, t=None):
        t = time.time() if t is None else t
        if self.diag is not None and t >= self.diag["deadline"]:
            self._finish_diag(t)
        if self.tail is not None and t >= self.tail["until"]:
            self._finish_tail()
        self.dumps.expire(t)
        self.apply(self.m.tick(t), t)
        self._decode_followups(t)
        self._serve_requests(t)

    def _finish_tail(self):
        tail, self.tail = self.tail, None
        row = self.conn.execute("SELECT evidence FROM runs WHERE run_id=?",
                                (tail["run_id"],)).fetchone()
        ev = json.loads(row["evidence"] or "{}")
        ev["after_fault"] = tail["lines"]
        ev["capture_end"] = "%.1f s past the first HARDFAULT" % self.fault_tail_s
        with self.conn:
            self.conn.execute("UPDATE runs SET evidence=? WHERE run_id=?",
                              (store.dumps(ev), tail["run_id"]))
        self.finished.append((tail["run_id"], "HARDFAULT"))

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
                self._maybe_confirm_run(run)

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
        if run:
            sha = run["sha"] if run.get("verified") else None
        else:
            sha = self.m.verified_sha
        if inc.kind == "RUN_FAIL" or (inc.kind == "HARDFAULT" and run):
            triage, reason = run["triage"], run["reason"]
        else:
            triage, reason = judge_incident(inc.kind, inc.facts, inc.cites, sha, self.history)
        route = route_for(triage)
        t_pass = self.history.passed(sha) if sha else None
        lp_sha, lp_t = (sha, t_pass) if t_pass is not None else self.history.last()
        inc.evidence = {"run_id": run["run_id"] if run else None}
        pending = None
        if inc.kind == "HARDFAULT":
            dsha = sha or (run["sha"] if run else None)
            ev, pending = self._fault_decode((inc.facts or {}).get("fault") or {}, dsha)
            if ev:
                inc.evidence.update(ev)
            cur = (self._cur or {}).get("rec") or {}
            if cur.get("dump") is not None and "dump" not in (inc.facts or {}):
                inc.evidence["dump"] = cur["dump"]
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
        if pending is not None:
            pending["ids"].append(("incident", iid))
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
        self._maybe_confirm_incident(inc)

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
        self._maybe_confirm_incident(inc)

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
        verified = bool(run.get("verified"))
        sha = run["sha"] if verified else None
        fl = self._flash if self._flash and run["sha"] and sha_match(
            self._flash.get("sha"), run["sha"]) else {}
        triage, reason = judge_run(run["verdict"], run["tests"], sha, self.history,
                                   cites=run["cites"], flashed=run["sha"], why=run.get("why"))
        run["triage"], run["reason"] = triage, reason
        evidence = {"lines": run["cites"][:TAIL_MAX], "tests": run["tests"],
                    "discarded": run.get("discarded", 0), "verified": verified,
                    "flashed": run["sha"], "reason": reason}
        for k in ("why", "capture", "build_log", "records"):
            if run.get(k) is not None:
                evidence[k] = run[k]
        if fl.get("elf"):
            evidence["elf"] = fl["elf"]
        pending = None
        cur = (self._cur or {}).get("rec") or {}
        if run["verdict"] == "HARDFAULT" and cur.get("kind") == "HARDFAULT":
            fields = {k: cur[k] for k in FAULT_FIELDS if k in cur}
            ev, pending = self._fault_decode(fields, run["sha"], fl.get("elf"))
            if ev:
                evidence.update(ev)
            if cur.get("dump") is not None:
                evidence["dump"] = cur["dump"]
        subject = None
        if verified:
            subject = (fwrepo.subject(self.repo, run["sha"]) if self.repo else None) \
                or fl.get("subject")
        with self.conn:
            run["run_id"] = store.insert(self.conn, "runs", {
                "commit": sha, "subject": subject,
                "verdict": run["verdict"], "triage": triage,
                "evidence": store.dumps(evidence),
                "duration_s": round(t - run["t_start"], 3), "t_start": run["t_start"],
                "t_end": t})
            if run["verdict"] == "PASS" and verified:
                store.insert(self.conn, "pass_history", {"board": self.board, "sha": run["sha"],
                                                         "t": t, "run_id": run["run_id"]})
                self.history.add(run["sha"], t)
            if not verified:
                if self._last_flash_id and run["verdict"] != "BUILD_ERROR":
                    self.conn.execute("UPDATE flashes SET result=? WHERE id=?",
                                      (run["verdict"], self._last_flash_id))
                self._set_verified(None, t)
            store.event(self.conn, t, self.board, "RUN_END",
                        {"run_id": run["run_id"], "verdict": run["verdict"], "triage": triage})
        if pending is not None:
            pending["ids"].append(("run", run["run_id"]))
        self.log("bench: run %d of %s %s triage=%s" % (run["run_id"], run["sha"],
                                                       run["verdict"], triage))
        if run["verdict"] == "HARDFAULT":
            self.tail = {"run_id": run["run_id"], "until": t + self.fault_tail_s, "lines": []}
        else:
            self.finished.append((run["run_id"], run["verdict"]))
        if run["verdict"] == "GARBLED" and self.probe:
            self.probe(run["run_id"])

    # ---- decode (bench.decode) ----
    def _fault_decode(self, fields, sha, elf=None):
        """(evidence or None, pending box or None) for one HARDFAULT record.

        Shared by the run-end and incident-open paths of the same record. The
        decode gets decode_budget_s; slower, it finishes later as a "decode" alert.
        """
        cur = self._cur
        if cur is not None and "decode" in cur:
            return cur["decode"]
        if not elf and self._flash and sha and sha_match(self._flash.get("sha"), sha):
            elf = self._flash.get("elf")
        elf = elf or decode.elf_path(self.elf_dir, sha)
        box = {"done": threading.Event(), "ev": None, "ids": []}

        def work():
            try:
                box["ev"] = decode.fault_evidence(fields, elf, tool=self.addr2line_tool,
                                                  runner=self.decode_runner)
            except Exception:  # a decode never breaks the alert
                box["ev"] = None
            box["done"].set()
        threading.Thread(target=work, name="decode", daemon=True).start()
        if box["done"].wait(self.decode_budget_s):
            res = (box["ev"], None)
        else:
            self._decodes.append(box)
            res = (None, box)
            self.log("bench: addr2line slower than %.1f s; the decode follows the alert" %
                     self.decode_budget_s)
        if cur is not None:
            cur["decode"] = res
        return res

    def _decode_followups(self, t):
        for box in [b for b in self._decodes if b["done"].is_set()]:
            self._decodes.remove(box)
            ev = box["ev"]
            if not ev:
                continue
            with self.conn:
                for what, i in box["ids"]:
                    table, key = ("incidents", "id") if what == "incident" else ("runs", "run_id")
                    row = self.conn.execute("SELECT evidence FROM %s WHERE %s=?" % (table, key),
                                            (i,)).fetchone()
                    if row is None:
                        continue
                    old = json.loads(row["evidence"] or "{}")
                    old.update(ev)
                    self.conn.execute("UPDATE %s SET evidence=? WHERE %s=?" % (table, key),
                                      (store.dumps(old), i))
                    if what == "incident":
                        inc = next((x for x in list(self.m.active.values()) +
                                    list(self.m.recent.values()) if x.db_id == i), None)
                        if inc is not None and isinstance(getattr(inc, "evidence", None), dict):
                            inc.evidence.update(ev)
                        lines = ["LOCAL LAB incident %d decode" % i]
                        fault = decode.fault_alert_line(ev)
                        if fault:
                            lines.append(fault)
                        if ev.get("file_line"):
                            lines.append("File: %s" % ev["file_line"])
                        lines.append("Incident: %d" % i)
                        store.add_alert(self.conn, i, "decode", "\n".join(lines), t)

    # ---- requests (bench.requests) ----
    def _start_job(self, sha):
        """Start a flash of sha through make_flash; None while another job runs."""
        if self.make_flash is None or (self.job is not None and not self.job.done.is_set()):
            return None
        self.job = self.make_flash(sha)
        return self.job

    def idle(self):
        return bool(self.m.link and self.m.expect is None and self.m.run is None
                    and self.tail is None and self.diag is None and self.request is None
                    and not self.confirming
                    and (self.job is None or self.job.done.is_set()))

    def _finish_request(self, t, ok, result, run_id=None):
        r, self.request = self.request, None
        requests.finish(self.conn, r["id"], t, ok, result, run_id)
        self.log("bench: request %d %s %s: %s" % (r["id"], r["kind"], r["arg"][:12],
                                                  result))

    def _request_line(self, t, rec, cite):
        r = self.request
        if r is not None and r["kind"] == "test" and rec.get("kind") == "REPLY" \
                and rec.get("cmd") == "test":
            self._finish_request(t, True, "reply: %s" % rec.get("line", ""))

    def _serve_requests(self, t):
        if self.make_flash is None:
            return
        r = self.request
        if r is not None:
            if r["kind"] == "run" and len(self.finished) > r["n"]:
                run_id, verdict = self.finished[r["n"]]
                self._finish_request(t, verdict == "PASS", verdict, run_id)
            elif t >= r["deadline"]:
                self._finish_request(t, False, "no LW REPLY test within %.0f s" %
                                     self.test_timeout if r["kind"] == "test"
                                     else "no run was recorded")
            return
        if not self.idle():
            return
        row = requests.claim_next(self.conn, t)
        if row is None:
            return
        self.request = {"id": row["id"], "kind": row["kind"], "arg": row["arg"],
                        "n": len(self.finished), "deadline": t + CONFIRM_TIMEOUT_S}
        self.log("bench: request %d %s %s (%s)" % (row["id"], row["kind"], row["arg"][:12],
                                                   row["source"]))
        if row["kind"] == "run":
            if self._start_job(row["arg"]) is None:
                self._finish_request(t, False, "the bench could not start a flash")
        elif self.send and self.m.can_send() and self.send("test %s" % row["arg"]):
            self.request["deadline"] = t + self.test_timeout
        else:
            self._finish_request(t, False, "the board is not ready for a test "
                                 "(needs LW RESULT then one LW ALIVE)")

    # ---- confirm (bench.confirm) ----
    def _maybe_confirm_incident(self, inc):
        if self.make_flash is None or self.call is None or self.confirming:
            return
        row = self._row(inc.db_id)
        run = inc.run or {}
        if not confirm.needs_confirm(row["triage"], verdict=run.get("verdict"), kind=inc.kind,
                                     tests=run.get("tests")):
            return
        subject = {"incident_id": inc.db_id, "run_id": run.get("run_id"),
                   "verdict": run.get("verdict"), "triage": row["triage"], "kind": inc.kind,
                   "firmware": row["firmware"] or run.get("sha")}
        self._start_confirm(subject, row["last_pass_sha"] or self.history.last()[0],
                            inc.db_id, run.get("run_id"))

    def _maybe_confirm_run(self, run):
        if self.make_flash is None or self.call is None or self.confirming:
            return
        if not confirm.needs_confirm(run.get("triage"), verdict=run["verdict"],
                                     tests=run.get("tests")):
            return
        subject = {"run_id": run["run_id"], "verdict": run["verdict"],
                   "triage": run["triage"], "commit": run["sha"]}
        self._start_confirm(subject, self.history.last()[0], None, run["run_id"])

    def _start_confirm(self, subject, last_pass, iid, run_id):
        self.confirming = True
        self.log("bench: confirming %s by reflashing last pass %s" % (
            "incident %d" % iid if iid else "run %s" % run_id, last_pass))

        def work():
            try:
                res = confirm.confirm(subject, last_pass, self._flash_and_capture)
            except Exception as e:  # never kill the bench
                self.log("bench: confirm error %s" % e.__class__.__name__)
                res = None
            self.call(self._confirm_done, res, iid, run_id, wait=False)
        threading.Thread(target=work, name="confirm", daemon=True).start()

    def _flash_and_capture(self, sha, timeout=CONFIRM_TIMEOUT_S):
        """confirm's hook, on the confirm thread: flash sha through the bench, wait for its run."""
        end = time.monotonic() + timeout
        while True:
            n = self.call(lambda: len(self.finished))
            if self.call(self._start_job, sha) is not None:
                break
            if time.monotonic() > end:
                raise TimeoutError("the bench stayed busy")
            time.sleep(0.1)
        while self.call(lambda: len(self.finished)) <= n:
            if time.monotonic() > end:
                raise TimeoutError("the reflash recorded no run")
            time.sleep(0.1)
        run_id, verdict = self.call(lambda: self.finished[n])
        lines = self.call(self._run_lines, run_id)
        return confirm.Capture(verdict, lines, run_id)

    def _run_lines(self, run_id):
        row = self.conn.execute("SELECT evidence FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row["evidence"] or "{}").get("lines", []) if row else []

    def _confirm_done(self, res, iid, run_id):
        self.confirming = False
        if res is None:
            return
        aid = confirm.write_confirm(self.conn, res, incident_id=iid, run_id=run_id)
        if res.reflash_verdict in BOOTED and res.last_pass_sha:
            with self.conn:
                self._set_verified(res.last_pass_sha, time.time())
        self.log("bench: %s %s -> %s (alert %s)" % (
            "incident %d" % iid if iid else "run %s" % run_id, res.status, res.triage, aid))

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


class Loop:
    """The bench's one thread: serial events, clock ticks, heartbeat, and calls
    from other threads (the flash job, the rate probe) run in between."""

    def __init__(self, bench, events, conn):
        self.bench = bench
        self.events = events
        self.conn = conn
        self.calls = queue.Queue()
        self.last_hb = 0.0
        self.thread = threading.current_thread()
        bench.call = self.call

    def call(self, fn, *args, wait=True, timeout=60.0):
        if threading.current_thread() is self.thread:
            return fn(*args)
        box = {"done": threading.Event()}
        self.calls.put((fn, args, box))
        if not wait:
            return None
        if not box["done"].wait(timeout):
            raise TimeoutError("bench loop did not run %s" % getattr(fn, "__name__", fn))
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def step(self, timeout=0.1):
        while True:
            try:
                fn, args, box = self.calls.get_nowait()
            except queue.Empty:
                break
            try:
                box["result"] = fn(*args)
            except Exception as e:
                box["error"] = e
            box["done"].set()
        try:
            ev = self.events.get(timeout=timeout)
        except queue.Empty:
            ev = None
        if ev is not None:
            self.bench.on_event(ev)
        now = time.time()
        self.bench.tick(now)
        if now - self.last_hb >= 1.0:
            store.heartbeat(self.conn, now)
            self.last_hb = now


def start_watcher(db, repo, stop, interval=None, log=print):
    """bench.watcher in a daemon thread with its own DB connection."""
    from bench import watcher

    def run():
        conn = store.open_db(db)
        try:
            w = watcher.Watcher(conn, repo, log=log)
            w.loop(watcher.INTERVAL_S if interval is None else interval, stop=stop)
        finally:
            conn.close()
    th = threading.Thread(target=run, name="watcher", daemon=True)
    th.start()
    return th


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m bench.run")
    ap.add_argument("--port", help="serial port (default: first /dev/serial/by-id/*STM*)")
    ap.add_argument("--board", default=DEFAULT_BOARD)
    ap.add_argument("--db", help="bench DB (default $LW_DB or %s)" % store.DEFAULT_DB)
    ap.add_argument("--flash", metavar="SHA",
                    help="build SHA from the bench repo's main, copy it to the NUCLEO drive "
                         "and verify it by its LW BOOT line")
    ap.add_argument("--repo", help="bench firmware repo (default $LW_FW_REPO or ~/bench-fw)")
    ap.add_argument("--then-exit", action="store_true",
                    help="with --flash: exit when that run's capture ends (0 on PASS)")
    ap.add_argument("--expect-flash", metavar="SHA",
                    help="a flash of SHA made by hand: verify it by its LW BOOT line")
    ap.add_argument("--flash-window", type=float, default=HAND_FLASH_WINDOW_S,
                    help="--expect-flash: seconds to wait for LW BOOT fw=SHA "
                         "(default %(default)s)")
    ap.add_argument("--explain-timeout", type=float, default=EXPLAIN_TIMEOUT_S)
    ap.add_argument("--no-explain", action="store_true")
    ap.add_argument("--watch", action="store_true",
                    help="also run the repo watcher: a new head of main queues a run")
    a = ap.parse_args(argv)
    for opt in ("flash", "expect_flash"):
        v = getattr(a, opt)
        if v is not None and not SHA_RE.fullmatch(v):
            print("bench: --%s needs 7-40 lowercase hex characters" % opt.replace("_", "-"),
                  file=sys.stderr)
            return 2
    if a.flash and a.expect_flash:
        print("bench: --flash and --expect-flash exclude each other", file=sys.stderr)
        return 2
    from bench import check_bench_repo
    repo = check_bench_repo.repo_path(a.repo)
    problems = check_bench_repo.check(repo)
    if problems:
        for p in problems:
            print("bench: refusing to start, bench repo: %s" % p, file=sys.stderr)
        return 2

    from bench import flash as flashmod
    from bench.serial_owner import SerialOwner
    db = store.db_path(a.db)
    conn = store.open_db(db)
    log = lambda s: print("%s %s" % (wall_ts(time.time()), s), flush=True)  # noqa: E731
    explainer = None if a.no_explain else ExplainWorker(db, timeout=a.explain_timeout,
                                                        log=log).start()
    owner = SerialOwner(port=a.port)
    state_dir = os.path.dirname(db)
    bench = Bench(conn, a.board, send=owner.send, explainer=explainer, log=log, repo=repo,
                  elf_dir=os.path.join(state_dir, "elf"))
    loop = Loop(bench, owner.events, conn)
    bench.make_flash = lambda sha: flashmod.FlashJob(bench, loop.call, repo, sha, state_dir,
                                                     log=log).start()

    def probe(run_id):
        owner.request_probe(callback=lambda res: loop.call(bench.attach_probe, run_id, res,
                                                           wait=False))
    bench.probe = probe
    if a.expect_flash:
        bench.expect_flash(a.expect_flash, a.flash_window)
    owner.start()
    stop = threading.Event()
    if a.watch:
        start_watcher(db, repo, stop, log=log)
    job = None
    if a.flash:
        job = bench._start_job(a.flash)
    rc = 0
    try:
        while True:
            loop.step()
            if a.then_exit and job is not None and job.done.is_set() and bench.finished:
                verdict = bench.finished[-1][1]
                log("bench: run finished: %s" % verdict)
                rc = 0 if verdict == "PASS" else 1
                break
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        owner.stop()
        if explainer:
            explainer.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
