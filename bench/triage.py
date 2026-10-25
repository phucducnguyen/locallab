"""The fixed triage rule (interface "Triage"), written by the bench, never by a model.

Judged only on the running verified sha, with a pass history of verified
shas only. Every verdict comes with a reason: one sentence quoting the facts
the rule used. Routing follows the triage.
"""

import time

from bench.incidents import sha_match

ROUTES = {"HARDWARE": "Hardware team", "CODE": "Firmware team", "UNKNOWN": "Triage",
          "NONE": None}
LOOPBACK_TEST = "loopback"
LINK_ALERT_S = 10
QUOTE_MAX = 100


def route_for(triage):
    return ROUTES[triage]


def _q(line):
    """A board line quoted for a reason (already printable ASCII, capped)."""
    line = (line or "").replace('"', "'")
    if len(line) > QUOTE_MAX:
        line = line[:QUOTE_MAX - 3] + "..."
    return '"%s"' % line


def _fmt_t(t):
    return time.strftime("%H:%M:%S UTC", time.gmtime(t)) if t is not None else "an unknown time"


class PassHistory:
    """Passing runs of verified shas: [(sha, t)], oldest first."""

    def __init__(self, rows=()):
        self.rows = [(s, t) for s, t in rows]

    def add(self, sha, t):
        self.rows.append((sha, t))

    def passed(self, sha):
        """Time of the last pass of exactly this sha, or None."""
        for s, t in reversed(self.rows):
            if sha_match(s, sha):
                return t
        return None

    def last(self, exclude=None):
        """(sha, t) of the last passing sha (optionally not this one), or (None, None)."""
        for s, t in reversed(self.rows):
            if exclude is None or not sha_match(s, exclude):
                return s, t
        return None, None


def _unverified(what):
    return ("UNKNOWN", "No verified sha is running (no bench flash was matched by its LW BOOT "
            "line), so %s decides nothing." % what)


def judge_incident(kind, facts, cites, sha, history, run=None):
    """(triage, reason) for an incident of kind on the running verified sha (or None).

    run is the bench run dict for RUN_FAIL (its verdict and TEST results).
    """
    facts = facts or {}
    first = cites[0]["line"] if cites and cites[0] else None
    if kind == "HEALTH_FAIL":
        quoted = " then ".join(_q(c["line"]) for c in cites[:2] if c)
        if not sha:
            return _unverified(quoted)
        t_pass = history.passed(sha)
        if t_pass is not None:
            return ("HARDWARE", "The loopback check passed on verified sha %s at %s and now "
                    "fails with %s, with no reflash since." % (sha, _fmt_t(t_pass), quoted))
        return ("UNKNOWN", "Verified sha %s has no passing run yet and the failure %s is confined "
                "to the loopback during its first run; confirm by reflashing the last passing "
                "sha%s." % (sha, quoted, _last_txt(history, sha)))
    if kind == "LINK_DOWN":
        if not sha:
            return _unverified("the serial port being gone for %d s" % LINK_ALERT_S)
        return ("HARDWARE", "The serial link to the board dropped (port gone for %d s) while "
                "verified sha %s was running." % (LINK_ALERT_S, sha))
    if kind == "REBOOT":
        reset = facts.get("reset")
        if reset is None:
            what = "a counter restart from %s to %s with no BOOT line" % (
                facts.get("before"), facts.get("after"))
        else:
            what = "%s, a reboot the bench did not cause" % _q(first)
        if not sha:
            return _unverified(what)
        t_pass = history.passed(sha)
        if reset == ["PIN"] and t_pass is not None:
            return ("HARDWARE", "The board printed %s (reset=PIN, the RESET pin) on verified sha "
                    "%s, which passed at %s, with no reflash since." % (_q(first), sha,
                                                                         _fmt_t(t_pass)))
        if reset is None:
            return ("UNKNOWN", "On verified sha %s the board showed %s; the cause is not "
                    "known." % (sha, what))
        if reset == ["PIN"]:
            return ("UNKNOWN", "The board printed %s on verified sha %s, which has no passing run "
                    "yet." % (_q(first), sha))
        return ("UNKNOWN", "The board printed %s on verified sha %s; reset=%s is not the RESET "
                "button, confirm by reflashing the last passing sha%s." % (
                    _q(first), sha, ",".join(reset) or "none", _last_txt(history, sha)))
    if kind == "HARDFAULT":
        if not sha:
            return _unverified(_q(first))
        return ("CODE", "Verified sha %s produced a fault in its own run: %s." % (sha, _q(first)))
    if kind == "SILENT":
        what = "%.0f s with no LW line (last line %s)" % (facts.get("silent_s", 3),
                                                         _q(first) if first else "none")
        if not sha:
            return _unverified(what)
        return ("UNKNOWN", "Verified sha %s went %s; a silence is UNKNOWN until a reflash of the "
                "last passing sha%s confirms it." % (sha, what, _last_txt(history, sha)))
    if kind == "RUN_FAIL":
        run = run or {}
        return judge_run(run.get("verdict") or facts.get("verdict", "FAIL"), run.get("tests", {}),
                         run.get("sha", sha), history, cites=cites)
    return ("UNKNOWN", "No rule covers incident kind %s." % kind)


def _last_txt(history, sha):
    s, t = history.last(exclude=sha)
    return " %s (passed at %s)" % (s, _fmt_t(t)) if s else " (none recorded yet)"


def judge_run(verdict, tests, sha, history, cites=None, flashed=None, why=None):
    """(triage, reason) for a bench run with this verdict and TEST results.

    sha is the verified sha (None when the flash was never verified); flashed
    is the sha the bench built or flashed; why is the bench's own note.
    """
    tests = tests or {}
    flashed = flashed or sha
    note = " (%s)" % why if why else ""
    if verdict == "PASS":
        if not sha:
            return _unverified("a passing run")
        return ("NONE", "Verified sha %s passed its run: %d tests PASS and LW RESULT PASS." % (
            sha, sum(1 for v in tests.values() if v == "PASS")))
    if verdict == "BUILD_ERROR":
        if why and why.startswith("bench:"):
            return ("UNKNOWN", "The bench could not build %s%s; nothing was flashed." % (
                flashed, note))
        return ("CODE", "Commit %s does not build%s; nothing was flashed." % (flashed, note))
    if verdict == "FLASH_ERROR":
        return ("UNKNOWN", "No LW BOOT line with fw=%s arrived within the capture window after "
                "the flash%s, so the flash is not verified." % (flashed, note))
    if not sha and verdict in ("NO_OUTPUT", "GARBLED"):
        what = ("printed nothing" if verdict == "NO_OUTPUT"
                else "printed only unreadable bytes")
        return ("UNKNOWN", "After the flash of %s the board %s and no LW BOOT line, so %s is not "
                "verified; confirm by reflashing the last passing sha%s." % (
                    flashed, what, flashed, _last_txt(history, flashed)))
    if not sha:
        return _unverified("the run's %s" % verdict)
    if verdict == "HARDFAULT":
        line = cites[-1]["line"] if cites and cites[-1] else "LW HARDFAULT"
        return ("CODE", "Verified sha %s produced a fault in its own run: %s." % (sha, _q(line)))
    if verdict in ("RESET_LOOP", "NO_OUTPUT", "GARBLED"):
        return ("UNKNOWN", "Verified sha %s ended its run as %s; confirm by reflashing the last "
                "passing sha%s." % (sha, verdict, _last_txt(history, sha)))
    failed = sorted(n for n, v in tests.items() if v == "FAIL")
    names = ", ".join(failed) or "none named"
    t_same = history.passed(sha)
    if t_same is not None and failed:
        return ("HARDWARE", "Test %s passed on verified sha %s at %s and now fails, with no "
                "reflash since." % (names, sha, _fmt_t(t_same)))
    if failed and all(n == LOOPBACK_TEST for n in failed):
        return ("UNKNOWN", "Only the loopback test failed, during the first run of verified sha "
                "%s; confirm by reflashing the last passing sha%s." % (
                    sha, _last_txt(history, sha)))
    last_sha, last_t = history.last(exclude=sha)
    if last_sha is None:
        return ("UNKNOWN", "Verified sha %s failed test %s but no passing sha is recorded to "
                "compare with." % (sha, names))
    return ("CODE", "Verified sha %s failed test %s, which last passing sha %s (at %s) did "
            "not fail." % (sha, names, last_sha, _fmt_t(last_t)))
