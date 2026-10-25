"""Canned scenarios: a pulled jumper, its recovery, a flap and a HardFault run.

Each scenario takes a Writer and a clock. The clock's now() gives the time to
stamp; wait(dt) moves time on (make_db advances a fake clock, emit sleeps).
All register values, shas and file:line are invented.
"""

import json
import time

from stub.db import FILE_LINE, SHA_NEW, SHA_PASS


class FakeClock:
    def __init__(self, t0):
        self.t = float(t0)

    def now(self):
        return self.t

    def wait(self, dt):
        self.t += dt


class RealClock:
    def __init__(self, speed=1.0):
        self.speed = speed

    def now(self):
        return time.time()

    def wait(self, dt):
        time.sleep(dt * self.speed)


def _health_n(w):
    r = w.conn.execute("SELECT fields FROM events WHERE board=? AND kind='HEALTH' "
                       "ORDER BY id DESC LIMIT 1", (w.board,)).fetchone()
    if not r:
        return 100
    return json.loads(r["fields"]).get("n", 99) + 1


def _last_pass(w):
    r = w.conn.execute("SELECT \"commit\", t_end FROM runs WHERE verdict='PASS' "
                       "ORDER BY run_id DESC LIMIT 1").fetchone()
    return (r["commit"], r["t_end"]) if r else (SHA_PASS, None)


def passing_run(w, clock, sha=SHA_PASS, subject="demo: tidy the startup banner"):
    t0 = clock.now()
    w.event(t0, "RUN_START", sha=sha)
    clock.wait(2.0)
    lines = [w.serial(clock.now(), "LW BOOT fw=%s reset=PIN csr=0x04000000" % sha, "BOOT",
                      fw=sha, reset=["PIN"], csr="0x04000000")]
    for name in ("crc32", "ringbuf", "systick", "cmd_dispatch", "loopback"):
        clock.wait(0.05)
        lines.append(w.serial(clock.now(), "LW TEST %s PASS" % name, "TEST",
                              name=name, result="PASS"))
    clock.wait(0.05)
    lines.append(w.serial(clock.now(), "LW RESULT PASS", "RESULT", result="PASS"))
    clock.wait(1.0)
    w.serial(clock.now(), "LW ALIVE 1", "ALIVE", n=1)
    lines.append(w.health(clock.now(), 1, "OK"))
    rid = w.run(t0, sha, subject, "PASS", "NONE", {"lines": lines}, clock.now())
    w.event(clock.now(), "RUN_END", run_id=rid, verdict="PASS", triage="NONE")
    return rid


def jumper(w, clock):
    """(a) The A0-A1 jumper is pulled: HEALTH_FAIL, HARDWARE."""
    n = _health_n(w)
    sha, t_pass = _last_pass(w)
    w.health(clock.now(), n, "OK")
    clock.wait(1.0)
    first = w.health(clock.now(), n + 1, "OPEN")
    t_first = clock.now()
    clock.wait(1.0)
    second = w.health(clock.now(), n + 2, "OPEN")
    reason = ("loopback=OPEN on 2 lines in a row on %s, which passed with loopback OK; "
              "no reflash since." % sha)
    iid = w.open_incident(clock.now(), "HEALTH_FAIL", "HARDWARE", reason,
                          {"lines": [first, second], "fields": {"loopback": "OPEN"}},
                          firmware=sha, last_pass=(sha, t_pass), t_first_bad=t_first)
    w.alert(clock.now(), iid, "open", cite=first)
    clock.wait(0.5)
    diag = [w.serial(clock.now(), "LW TEST loopback FAIL open", "TEST",
                     name="loopback", result="FAIL", detail="open")]
    clock.wait(0.05)
    diag.append(w.serial(clock.now(), "LW REPLY test done", "REPLY", cmd="test", text="done"))
    ev = {"lines": [first, second], "fields": {"loopback": "OPEN"},
          "diagnostic": {"cmd": "test loopback", "lines": diag}}
    w.update_incident(iid, evidence=json.dumps(ev, sort_keys=True))
    return iid


def recover(w, clock, iid=None):
    """(c) The jumper goes back: 3 OK lines resolve the open HEALTH_FAIL incident."""
    if iid is None:
        r = w.conn.execute("SELECT id FROM incidents WHERE board=? AND kind='HEALTH_FAIL' "
                           "AND state!='RESOLVED' ORDER BY id DESC LIMIT 1",
                           (w.board,)).fetchone()
        if not r:
            raise LookupError("no open HEALTH_FAIL incident to recover")
        iid = r["id"]
    n = _health_n(w)
    cite = None
    for i in range(3):
        if i:
            clock.wait(1.0)
        cite = w.health(clock.now(), n + i, "OK")
    w.resolve(clock.now(), iid)
    w.alert(clock.now(), iid, "resolve", cite=cite)
    return iid


def flap(w, clock, flaps=4):
    """(d) The jumper is loose: the same incident reopens within 60 s, FLAPPING."""
    sha, t_pass = _last_pass(w)
    n = _health_n(w)
    w.health(clock.now(), n, "OK")
    n += 1
    iid = None
    for k in range(flaps + 1):
        clock.wait(1.0)
        first = w.health(clock.now(), n, "OPEN")
        t_first = clock.now()
        clock.wait(1.0)
        second = w.health(clock.now(), n + 1, "OPEN")
        n += 2
        if iid is None:
            reason = ("loopback=OPEN on 2 lines in a row on %s, which passed with loopback OK; "
                      "no reflash since." % sha)
            iid = w.open_incident(clock.now(), "HEALTH_FAIL", "HARDWARE", reason,
                                  {"lines": [first, second], "fields": {"loopback": "OPEN"}},
                                  firmware=sha, last_pass=(sha, t_pass), t_first_bad=t_first)
            w.alert(clock.now(), iid, "open", cite=first)
        else:
            w.update_incident(iid, state="FLAPPING", flaps=k, closed=None)
            w.event(clock.now(), "INCIDENT_FLAP", incident_id=iid, flaps=k)
            if k == 1:  # at most one flap update per 60 s; this whole loop is well inside 60 s
                w.alert(clock.now(), iid, "flap-%d" % k, cite=first)
        if k == flaps:
            break
        for _ in range(3):
            clock.wait(1.0)
            w.health(clock.now(), n, "OK")
            n += 1
        w.resolve(clock.now(), iid)
    return iid


def hardfault(w, clock, sha=SHA_NEW, subject="demo: add the example handler"):
    """(b) A new commit faults in its own run: HARDFAULT, CODE, file:line."""
    last_pass = _last_pass(w)
    t0 = clock.now()
    w.event(t0, "RUN_START", sha=sha)
    clock.wait(2.0)
    lines = [w.serial(clock.now(), "LW BOOT fw=%s reset=PIN csr=0x04000000" % sha, "BOOT",
                      fw=sha, reset=["PIN"], csr="0x04000000")]
    for name in ("crc32", "ringbuf"):
        clock.wait(0.05)
        lines.append(w.serial(clock.now(), "LW TEST %s PASS" % name, "TEST",
                              name=name, result="PASS"))
    regs = {"pc": "0x08001234", "lr": "0x08001201", "psr": "0x01000000", "cfsr": "0x00000082",
            "hfsr": "0x40000000", "bfar": "0x60000010", "mmfar": "0x60000010",
            "sp": "0x2001ffa0"}
    hf_line = "LW HARDFAULT " + " ".join("%s=%s" % kv for kv in regs.items())
    clock.wait(0.05)
    first = w.serial(clock.now(), hf_line, "HARDFAULT", **regs)
    t_first = clock.now()
    clock.wait(1.0)
    lines += [first, w.serial(clock.now(), hf_line, "HARDFAULT", **regs)]
    evidence = {"lines": lines, "registers": regs, "cfsr_bits": ["DACCVIOL", "MMARVALID"],
                "hfsr_bits": ["FORCED"], "file_line": FILE_LINE}
    rid = w.run(t0, sha, subject, "HARDFAULT", "CODE", evidence, clock.now())
    w.event(clock.now(), "RUN_END", run_id=rid, verdict="HARDFAULT", triage="CODE")
    reason = ("This sha's own run faulted: cfsr=0x00000082 (DACCVIOL) mmfar=0x60000010, "
              "%s; last pass %s." % (FILE_LINE, last_pass[0]))
    iid = w.open_incident(clock.now(), "HARDFAULT", "CODE", reason, evidence, firmware=sha,
                          last_pass=last_pass, t_first_bad=t_first)
    w.conn.execute("UPDATE routing SET run_id=? WHERE incident_id=?", (rid, iid))
    w.conn.commit()
    w.alert(clock.now(), iid, "open", cite=first)
    return iid


SCENARIOS = {"jumper": jumper, "recover": recover, "flap": flap, "hardfault": hardfault}
