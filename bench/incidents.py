"""Incident states and timers for one board: a pure state machine.

Fed (timestamp, parsed LW record, citation) for every board record, link
events (link_up / link_down), the bench's own flash (expect_flash) and clock
ticks. Every call returns a list of outputs (dicts with a "type"); the
machine itself writes nothing, sends nothing and reads no clock.

The timers are the interface's ("Incident states and timers"):
- HEALTH_FAIL opens on 2 non-OK HEALTH lines in a row and resolves on 3 OK
  lines in a row (with the duration).
- Flap: the same kind reopening within 60 s of its close reuses the incident,
  marks it FLAPPING and counts the flap; at most one flap alert per 60 s (a
  flap held back goes out once the 60 s have passed, if still open).
- SILENT: link up, no LW line for 3 s; resolves on the next LW line.
- LINK_DOWN: the port has been gone for 10 s (shorter drops open nothing);
  resolves when the port is back. A counter that restarts, or a BOOT line,
  within 10 s of the port coming back is a replug: logged, no incident.
- REBOOT: a BOOT line the bench did not cause, with the link up (or a
  counter restart with no BOOT line, outside a replug). Further boots while
  it is open are counted in it. It resolves silently on 3 OK HEALTH lines.
- HARDFAULT: the first HARDFAULT line; the once-a-second repeats are the
  same incident. Resolves on the next BOOT line.
- RUN_FAIL: a bench run (after the bench's own flash, verified by
  "LW BOOT fw=<sha>") that ends in RESULT FAIL, a reset loop (a further
  BOOT with IWDG in reset=), or no RESULT within the capture window
  (NO_OUTPUT); or a flash after which the board printed nothing
  (NO_OUTPUT) or only unreadable bytes (GARBLED). Resolves on the next
  verified boot.

The bench's flash (interface "Verified firmware"): expect_flash pauses the
board and discards every record until "LW BOOT fw=<sha>"; flash_copied arms
the 15 s window from the end of the copy. No matching BOOT in the window:
nothing at all after the copy is NO_OUTPUT, unreadable bytes and no LW line
is GARBLED, anything else (LW lines of another build, a BOOT of another sha)
is FLASH_ERROR; all three unverified, never a pass. A flash by hand
(copied=True at once) cannot know when its copy ended, so silence there
stays FLASH_ERROR. LINK_DOWN opens no
incident while the bench is flashing (the board re-enumerates). After a
verified BOOT the run is captured until RESULT, the first HARDFAULT (the
bench keeps 3 s more of lines) or 15 s.

Resolve alerts are held for the flap window (60 s) after the close, so a
flapping incident gets one resolve alert at its real end (the record itself
is RESOLVED at once).

Output types:
  open       {inc, t, cite}                    a new incident (first alert)
  flap       {inc, t, cite, alert}             a reopen; alert=True to send flap-<n>
  flap_alert {inc, t}                          a held-back flap update now due
  resolve    {inc, t, cite, duration}          the record closes
  resolve_alert {inc, t}                       the resolve alert is due
  run_start  {sha, t, discarded}               the bench's flash was verified
  run_end    {run, t}                          a bench run ended (verdict)
  log        {what, t, ...}                    REPLUG, UNVERIFIED, BOOT_ABSORBED, ...
"""

HEALTH_FAIL = "HEALTH_FAIL"
SILENT = "SILENT"
LINK_DOWN = "LINK_DOWN"
REBOOT = "REBOOT"
HARDFAULT = "HARDFAULT"
RUN_FAIL = "RUN_FAIL"
KINDS = (HEALTH_FAIL, SILENT, LINK_DOWN, REBOOT, HARDFAULT, RUN_FAIL)

OPEN_AFTER = 2          # non-OK HEALTH lines in a row
RESOLVE_AFTER = 3       # OK HEALTH lines in a row
FLAP_S = 60.0
SILENT_S = 3.0
LINK_ALERT_S = 10.0
REPLUG_S = 10.0
FLASH_WINDOW_S = 15.0    # copy end -> LW BOOT fw=<sha>
CAPTURE_S = 15.0         # verified BOOT -> RESULT / HARDFAULT
COPY_WINDOW_S = 120.0    # pause start -> copy end
STALE_GRACE_S = 2.0      # LW lines this soon after the copy may be the old build's
GARBLED_MIN_BYTES = 8
MAX_CITES = 12


def sha_match(fw, sha):
    """The BOOT line's fw= matches the sha the bench flashed (7+ hex, prefix)."""
    if not fw or not sha:
        return False
    fw, sha = fw.lower(), sha.lower()
    n = min(len(fw), len(sha))
    return n >= 7 and fw[:n] == sha[:n]


class Incident:
    def __init__(self, key, board, kind, t, t_first_bad, cite, facts=None):
        self.key = key
        self.board = board
        self.kind = kind
        self.state = "OPEN"
        self.opened = t
        self.closed = None
        self.flaps = 0
        self.flaps_alerted = 0
        self.last_flap_alert = None
        self.t_first_bad = t_first_bad
        self.first_cite = cite
        self.cites = [cite] if cite else []
        self.facts = dict(facts or {})
        self.repeats = 0
        self.resolve_due = None
        self.resolve_alerted = False
        self.run = None
        self.db_id = None
        self.evidence = {}

    def cite(self, c):
        if c and len(self.cites) < MAX_CITES:
            self.cites.append(c)

    def __repr__(self):
        return "Incident(%s %s %s flaps=%d)" % (self.key, self.kind, self.state, self.flaps)


class Machine:
    def __init__(self, board, silent_s=SILENT_S, flap_s=FLAP_S, link_alert_s=LINK_ALERT_S,
                 replug_s=REPLUG_S, resolve_hold_s=None, capture_s=CAPTURE_S):
        self.board = board
        self.silent_s = silent_s
        self.flap_s = flap_s
        self.link_alert_s = link_alert_s
        self.replug_s = replug_s
        self.resolve_hold_s = flap_s if resolve_hold_s is None else resolve_hold_s
        self.capture_s = capture_s
        self._next_key = 1
        self.active = {}        # kind -> Incident not RESOLVED
        self.recent = {}        # kind -> the last Incident of that kind
        self.link = None        # None until the first link event
        self.down_since = None
        self.link_back_at = None
        self.last_lw_t = None
        self.last_cite = None
        self.bad_run = []
        self.ok_run = 0
        self.last_n = None
        self.boot_since_n = False
        self.seen_result = False
        self.seen_alive = False
        self.verified_sha = None
        self.expect = None      # {"sha", "deadline", "discarded", "t"}
        self.run = None         # the bench run in progress

    # ---- helpers ----
    def can_send(self):
        """The board printed LW RESULT and then one LW ALIVE since its boot."""
        return bool(self.link and self.seen_result and self.seen_alive and not self.expect)

    def _in_replug(self, t):
        return self.link_back_at is not None and t - self.link_back_at <= self.replug_s

    def _open(self, t, kind, cite, t_first_bad=None, facts=None, run=None):
        if kind in self.active:
            return []
        prev = self.recent.get(kind)
        if (prev is not None and prev.state == "RESOLVED" and prev.closed is not None
                and t - prev.closed <= self.flap_s):
            prev.state = "FLAPPING"
            prev.flaps += 1
            prev.closed = None
            prev.resolve_due = None
            prev.cite(cite)
            if facts:
                prev.facts.update(facts)
            self.active[kind] = prev
            alert = prev.last_flap_alert is None or t - prev.last_flap_alert >= self.flap_s
            if alert:
                prev.last_flap_alert = t
                prev.flaps_alerted = prev.flaps
            return [{"type": "flap", "inc": prev, "t": t, "cite": cite, "alert": alert,
                     "run": run}]
        inc = Incident(self._next_key, self.board, kind, t,
                       t if t_first_bad is None else t_first_bad, cite, facts)
        inc.run = run
        self._next_key += 1
        self.active[kind] = inc
        self.recent[kind] = inc
        return [{"type": "open", "inc": inc, "t": t, "cite": cite}]

    def _resolve(self, t, kind, cite=None):
        inc = self.active.pop(kind, None)
        if inc is None:
            return []
        inc.state = "RESOLVED"
        inc.closed = t
        inc.cite(cite)
        if kind != REBOOT and not inc.resolve_alerted:
            inc.resolve_due = t + self.resolve_hold_s
        return [{"type": "resolve", "inc": inc, "t": t, "cite": cite,
                 "duration": round(t - inc.opened, 3)}]

    def _end_run(self, t, verdict, cite=None):
        run, self.run = self.run, None
        run["verdict"] = verdict
        run["t_end"] = t
        if cite:
            run["cites"].append(cite)
        return run

    # ---- inputs ----
    def restore(self, inc):
        """Put back an incident loaded from the DB (a bench restart)."""
        inc.key = self._next_key
        self._next_key += 1
        self.recent[inc.kind] = inc
        if inc.state != "RESOLVED":
            self.active[inc.kind] = inc

    def expect_flash(self, t, sha, window=FLASH_WINDOW_S, copied=True):
        """The bench flashes sha: pause and discard until its BOOT.

        copied=True (a flash by hand) starts the BOOT window now; the bench's
        own flash passes copied=False and calls flash_copied when the copy ends.
        """
        out = []
        if self.run is not None:
            run = self._end_run(t, "ABORTED")
            out.append({"type": "log", "what": "RUN_ABORTED", "t": t, "sha": run["sha"]})
        self.expect = {"sha": sha, "t": t, "deadline": t + window, "discarded": 0,
                       "copied": copied, "hand": copied, "t_copied": t if copied else None,
                       "late": 0,
                       "bytes": 0, "bad_bytes": 0, "nonlw": 0, "other_boot": None}
        self.verified_sha = None
        out.append({"type": "log", "what": "FLASH_EXPECT", "t": t, "sha": sha})
        return out

    def flash_copied(self, t, window=FLASH_WINDOW_S, copy_error=None):
        """The copy finished: the BOOT window starts now; earlier bytes are stale.

        copy_error: every byte was written but fsync/close failed (the drive may
        be gone); then silence in the window is FLASH_ERROR, not NO_OUTPUT.
        """
        if self.expect is None:
            return []
        e = self.expect
        e.update(copied=True, t_copied=t, deadline=t + window, late=0, bytes=0, bad_bytes=0,
                 nonlw=0, other_boot=None, copy_error=copy_error)
        return [{"type": "log", "what": "FLASH_COPIED", "t": t, "sha": e["sha"]}]

    def _unverified_run(self, t, sha, verdict, t_start, **extra):
        run = {"sha": sha, "verified": False, "t_start": t_start, "t_end": t, "tests": {},
               "cites": [], "verdict": verdict, "discarded": 0}
        run.update(extra)
        return run

    def flash_failed(self, t, why, verdict="FLASH_ERROR", sha=None):
        """The flash (or the build) failed: a run that never ran, never a pass."""
        out = []
        if self.run is not None:
            run = self._end_run(t, "ABORTED")
            out.append({"type": "log", "what": "RUN_ABORTED", "t": t, "sha": run["sha"]})
        e, self.expect = self.expect, None
        self.last_lw_t = t
        sha = sha or (e["sha"] if e else None)
        run = self._unverified_run(t, sha, verdict, e["t"] if e else t,
                                   discarded=e["discarded"] if e else 0, why=why)
        return out + [{"type": "run_end", "run": run, "t": t}]

    def verifies(self, t, rec):
        """This record is LW BOOT fw=<the flashed sha>, at or after the copy's end."""
        e = self.expect
        return bool(e is not None and rec.get("kind") == "BOOT"
                    and sha_match(rec.get("fw"), e["sha"]) and e["copied"]
                    and e["t_copied"] is not None and t >= e["t_copied"])

    def noise(self, t, nbytes, bad):
        """A non-LW line (nbytes long, bad unreadable bytes) while flashing."""
        e = self.expect
        if e is not None and e["copied"]:
            e["bytes"] += nbytes
            e["bad_bytes"] += bad
            e["nonlw"] += 1

    def _expect_timeout(self, t):
        e, self.expect = self.expect, None
        self.last_lw_t = t
        facts = {"bytes": e["bytes"], "bad_bytes": e["bad_bytes"], "nonlw_lines": e["nonlw"],
                 "lw_after_copy": e["late"], "other_boot": e["other_boot"],
                 "window_s": round(t - (e["t_copied"] or e["t"]), 3)}
        board_said = e["late"] == 0 and (e["bad_bytes"] >= GARBLED_MIN_BYTES or e["bytes"] == 0)
        if not e["copied"]:
            verdict, why = "FLASH_ERROR", "the copy did not finish in time"
        elif board_said and e.get("copy_error"):
            verdict, why = "FLASH_ERROR", "the copy reported %s and no LW BOOT arrived" % (
                e["copy_error"])
        elif board_said and not self.link:
            verdict, why = "FLASH_ERROR", "the serial port was gone when the window closed"
        elif e["other_boot"]:
            verdict, why = "FLASH_ERROR", "the board booted fw=%s instead" % e["other_boot"]
        elif e["late"] == 0 and e["bad_bytes"] >= GARBLED_MIN_BYTES:
            verdict, why = "GARBLED", "only unreadable bytes after the copy"
        elif e["late"] == 0 and e["bytes"] == 0 and not e["hand"]:
            verdict, why = "NO_OUTPUT", "nothing at all after the copy"
        else:
            verdict, why = "FLASH_ERROR", "LW lines arrived but no LW BOOT fw=%s" % e["sha"]
        run = self._unverified_run(t, e["sha"], verdict, e["t"], discarded=e["discarded"],
                                   why=why, capture=facts)
        out = [{"type": "run_end", "run": run, "t": t}]
        if verdict in ("NO_OUTPUT", "GARBLED"):
            out += self._open(t, RUN_FAIL, None, facts={"verdict": verdict}, run=run)
        return out

    def link_up(self, t):
        out = []
        if self.link is False:
            self.link_back_at = t
            out += self._resolve(t, LINK_DOWN)
            out.append({"type": "log", "what": "LINK_UP", "t": t,
                        "down_s": round(t - self.down_since, 3) if self.down_since else None})
        self.link = True
        self.down_since = None
        self.last_lw_t = t
        return out

    def link_down(self, t):
        out = []
        if self.link is not False:
            self.down_since = t
            out.append({"type": "log", "what": "LINK_DOWN", "t": t})
        self.link = False
        self.bad_run = []
        self.ok_run = 0
        self.seen_result = self.seen_alive = False
        if self.run is not None:
            run = self._end_run(t, "ABORTED")
            out.append({"type": "log", "what": "RUN_ABORTED", "t": t, "sha": run["sha"]})
        return out

    def record(self, t, rec, cite=None):
        """One parsed LW record (bench.parser.parse_record) with its citation."""
        kind = rec.get("kind")
        e = self.expect
        if e is not None:
            if self.verifies(t, rec):
                return self._verified_boot(t, rec, cite)
            e["discarded"] += 1
            if e["copied"]:
                if kind == "BOOT" and rec.get("fw"):
                    e["other_boot"] = rec["fw"][:40]
                if t - e["t_copied"] > STALE_GRACE_S or kind == "BOOT":
                    e["late"] += 1
            return []
        out = []
        self.last_lw_t = t
        self.last_cite = cite
        if self.run is not None:
            self.run["records"] += 1
        out += self._resolve(t, SILENT, cite)
        if kind == "BOOT":
            out += self._boot(t, rec, cite)
        elif kind in ("ALIVE", "HEALTH"):
            out += self._counter(t, rec, cite)
            if kind == "ALIVE":
                if self.seen_result:
                    self.seen_alive = True
            else:
                out += self._health(t, rec, cite)
        elif kind == "TEST":
            if self.run is not None and rec.get("name"):
                self.run["tests"][rec["name"]] = rec.get("status")
                self.run["cites"].append(cite)
        elif kind == "RESULT":
            self.seen_result = True
            self.seen_alive = False
            if self.run is not None and rec.get("status") in ("PASS", "FAIL"):
                run = self._end_run(t, rec["status"], cite)
                out.append({"type": "run_end", "run": run, "t": t})
                if run["verdict"] == "FAIL":
                    out += self._open(t, RUN_FAIL, cite, facts={"verdict": "FAIL"}, run=run)
        elif kind == "HARDFAULT":
            out += self._hardfault(t, rec, cite)
        return out

    def tick(self, t):
        out = []
        if self.expect is not None and t >= self.expect["deadline"]:
            out += self._expect_timeout(t)
        if self.run is not None and t >= self.run["deadline"]:
            run = self._end_run(t, "NO_OUTPUT")
            run["why"] = "no LW RESULT or HARDFAULT within %.0f s of the BOOT line" % (
                self.capture_s)
            out.append({"type": "run_end", "run": run, "t": t})
            out += self._open(t, RUN_FAIL, None, facts={"verdict": "NO_OUTPUT"}, run=run)
        if (self.link is False and self.down_since is not None and self.expect is None
                and t - self.down_since >= self.link_alert_s and LINK_DOWN not in self.active):
            prev = self.recent.get(LINK_DOWN)
            if prev is None or prev.opened < self.down_since:
                out += self._open(t, LINK_DOWN, None, t_first_bad=self.down_since,
                                  facts={"down_since": self.down_since})
        quiet_run = self.active.get(RUN_FAIL)
        quiet_run = quiet_run is not None and quiet_run.facts.get("verdict") in (
            "NO_OUTPUT", "GARBLED")  # that incident already says the board is quiet
        if (self.link and self.expect is None and self.last_lw_t is not None and not quiet_run
                and t - self.last_lw_t >= self.silent_s and SILENT not in self.active):
            prev = self.recent.get(SILENT)
            if prev is None or prev.opened <= self.last_lw_t:
                out += self._open(t, SILENT, self.last_cite, t_first_bad=self.last_lw_t,
                                  facts={"silent_s": self.silent_s})
        for inc in list(self.recent.values()):
            if (inc.state == "RESOLVED" and inc.resolve_due is not None
                    and t >= inc.resolve_due and not inc.resolve_alerted):
                inc.resolve_alerted = True
                inc.resolve_due = None
                out.append({"type": "resolve_alert", "inc": inc, "t": t})
            elif (inc.state == "FLAPPING" and inc.flaps > inc.flaps_alerted
                    and inc.last_flap_alert is not None
                    and t - inc.last_flap_alert >= self.flap_s):
                inc.last_flap_alert = t
                inc.flaps_alerted = inc.flaps
                out.append({"type": "flap_alert", "inc": inc, "t": t})
        return out

    # ---- record kinds ----
    def _verified_boot(self, t, rec, cite):
        exp, self.expect = self.expect, None
        self.verified_sha = exp["sha"]
        self.last_lw_t = t
        self.last_cite = cite
        self._reset_boot_state()
        self.run = {"sha": exp["sha"], "verified": True, "t_start": exp["t"], "t_boot": t,
                    "tests": {}, "cites": [cite] if cite else [], "verdict": None,
                    "discarded": exp["discarded"], "reset": rec.get("reset", []),
                    "deadline": t + self.capture_s, "records": 0}
        out = [{"type": "run_start", "sha": exp["sha"], "t": t, "discarded": exp["discarded"]}]
        out += self._resolve(t, HARDFAULT, cite)
        out += self._resolve(t, RUN_FAIL, cite)
        out += self._resolve(t, SILENT, cite)
        return out

    def _reset_boot_state(self):
        self.seen_result = self.seen_alive = False
        self.boot_since_n = True
        self.bad_run = []
        self.ok_run = 0

    def _boot(self, t, rec, cite):
        out = []
        self._reset_boot_state()
        out += self._resolve(t, HARDFAULT, cite)
        fw = rec.get("fw")
        if self.verified_sha and not sha_match(fw, self.verified_sha):
            out.append({"type": "log", "what": "UNVERIFIED", "t": t, "was": self.verified_sha,
                        "fw": fw})
            self.verified_sha = None
        if self.run is not None:
            if "IWDG" in (rec.get("reset") or []):
                # A watchdog boot inside the bench's run: the run never finished.
                run = self._end_run(t, "RESET_LOOP", cite)
                out.append({"type": "run_end", "run": run, "t": t})
                out += self._open(t, RUN_FAIL, cite, facts={"verdict": "RESET_LOOP",
                                                             "reset": rec.get("reset", [])},
                                  run=run)
                return out
            # Another boot (the RESET button, say): the run is abandoned, the
            # boot is judged like any other.
            run = self._end_run(t, "ABORTED", cite)
            out.append({"type": "log", "what": "RUN_ABORTED", "t": t, "sha": run["sha"]})
        if self._in_replug(t):
            out.append({"type": "log", "what": "REPLUG", "t": t, "how": "boot", "cite": cite})
            return out
        for k in (RUN_FAIL, REBOOT):
            inc = self.active.get(k)
            if inc is not None and (k == REBOOT or inc.facts.get("verdict") == "RESET_LOOP"):
                inc.repeats += 1
                inc.cite(cite)
                out.append({"type": "log", "what": "BOOT_ABSORBED", "t": t, "inc": inc})
                return out
        out += self._open(t, REBOOT, cite, facts={"reset": rec.get("reset", []), "fw": fw,
                                                  "how": "boot"})
        return out

    def _counter(self, t, rec, cite):
        out = []
        n = rec.get("n")
        if n is None:
            return out
        if self.last_n is not None and n < self.last_n and not self.boot_since_n:
            # The board restarted and its BOOT line was missed.
            self.seen_result = self.seen_alive = False
            if self._in_replug(t):
                out.append({"type": "log", "what": "REPLUG", "t": t, "how": "counter",
                            "before": self.last_n, "after": n, "cite": cite})
            elif REBOOT in self.active:
                self.active[REBOOT].repeats += 1
                self.active[REBOOT].cite(cite)
            else:
                out += self._open(t, REBOOT, cite, facts={"reset": None, "how": "counter",
                                                          "before": self.last_n, "after": n})
        self.last_n = n
        self.boot_since_n = False
        return out

    def _health(self, t, rec, cite):
        out = []
        lb = rec.get("loopback")
        if lb == "OK":
            self.bad_run = []
            self.ok_run += 1
            if self.ok_run >= RESOLVE_AFTER:
                out += self._resolve(t, HEALTH_FAIL, cite)
                out += self._resolve(t, REBOOT, cite)
        elif lb in ("OPEN", "STUCK"):
            self.ok_run = 0
            self.bad_run.append((t, cite, lb))
            inc = self.active.get(HEALTH_FAIL)
            if inc is not None:
                inc.cite(cite)
                inc.repeats += 1
            elif len(self.bad_run) >= OPEN_AFTER:
                first_t, first_cite, _ = self.bad_run[0]
                facts = {"loopback": [b[2] for b in self.bad_run[-OPEN_AFTER:]],
                         "lines": [b[1]["line"] for b in self.bad_run[-OPEN_AFTER:] if b[1]]}
                opened = self._open(t, HEALTH_FAIL, first_cite, t_first_bad=first_t, facts=facts)
                for o in opened:
                    o["inc"].cite(cite)
                    o["cite"] = first_cite
                out += opened
        return out

    def _hardfault(self, t, rec, cite):
        out = []
        fields = {k: rec[k] for k in ("pc", "lr", "psr", "cfsr", "hfsr", "bfar", "mmfar", "sp")
                  if k in rec}
        run = None
        if self.run is not None:
            run = self._end_run(t, "HARDFAULT", cite)
            out.append({"type": "run_end", "run": run, "t": t})
        inc = self.active.get(HARDFAULT)
        if inc is not None:
            inc.repeats += 1
            if len(inc.cites) < 3:
                inc.cite(cite)
            return out
        out += self._open(t, HARDFAULT, cite, facts={"fault": fields,
                                                     "partial": bool(rec.get("partial"))},
                          run=run)
        return out
