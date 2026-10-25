"""Confirm before blaming (interface "Triage", the UNKNOWN rule).

An UNKNOWN result (NO_OUTPUT, GARBLED, RESET_LOOP, FLASH_ERROR, a silence, or
a failure confined to the loopback during a new sha's first run) is settled
by reflashing the last passing sha and watching whether output returns:

    the last passing sha still fails  -> HARDWARE  (Hardware team)
    the last passing sha runs clean   -> CODE      (Firmware team)

A reflash that could not be judged (no last passing sha, a build error, the
flash hook raising) leaves the triage UNKNOWN and says why.

The hook (bench.flash provides it):

    flash_and_capture(sha) -> Capture

builds sha if needed, copies fw.bin to the board, verifies LW BOOT fw=<sha>,
captures until LW RESULT / 3 s past the first HARDFAULT / 15 s, and returns
Capture(verdict, lines). verdict is a run verdict (PASS, FAIL, HARDFAULT,
RESET_LOOP, NO_OUTPUT, GARBLED, FLASH_ERROR, BUILD_ERROR); lines are the
captured LW lines (str or {"event_id", "raw_ts", "line"} cites). The board
then runs the last passing sha: the caller sets its verified sha from it.

Usage:

    if needs_confirm(triage, verdict=..., kind=..., tests=...):
        res = confirm(subject, last_pass_sha, flash_and_capture, conn=conn)

confirm() updates incidents / runs (triage, reason, route, evidence.confirm),
the routing rows in place, and writes the "confirmed" alert replying to the
incident's open alert, in one transaction. Board text is data.
"""

import dataclasses
import json
import time
import typing

from bench import store
from bench.triage import LOOPBACK_TEST, route_for, _q

UNKNOWN_VERDICTS = ("NO_OUTPUT", "GARBLED", "RESET_LOOP", "FLASH_ERROR")
UNJUDGED_VERDICTS = ("BUILD_ERROR",)   # says nothing about the board
TRANSITION = "confirmed"


@dataclasses.dataclass
class Capture:
    """What flash_and_capture(sha) returns."""
    verdict: str
    lines: typing.List[typing.Any] = dataclasses.field(default_factory=list)
    run_id: typing.Optional[int] = None
    detail: typing.Optional[str] = None


@dataclasses.dataclass
class ConfirmResult:
    status: str                    # CONFIRMED | NOT_CONFIRMED
    triage: str                    # HARDWARE | CODE | UNKNOWN
    route: typing.Optional[str]
    reason: str
    prior_triage: str
    original: str                  # what was UNKNOWN (verdict or incident kind)
    sha: typing.Optional[str]      # the sha that failed
    last_pass_sha: typing.Optional[str]
    reflash_verdict: typing.Optional[str]
    output_returned: typing.Optional[bool]
    reflash_lines: typing.List[typing.Any]
    reflash_run_id: typing.Optional[int]
    t: float

    def evidence(self):
        return {"status": self.status, "from": self.prior_triage, "to": self.triage,
                "original": self.original, "reflashed_sha": self.last_pass_sha,
                "reflash_verdict": self.reflash_verdict,
                "output_returned": self.output_returned,
                "reflash_run_id": self.reflash_run_id,
                "reflash_lines": self.reflash_lines[:10], "t": self.t}


def _line_text(x):
    if isinstance(x, dict):
        return x.get("line") or ""
    return x if isinstance(x, str) else ""


def output_returned(lines):
    return any(_line_text(x).startswith("LW ") for x in lines or ())


def needs_confirm(triage, verdict=None, kind=None, tests=None):
    """True for the UNKNOWN results the interface settles by a reflash of the last pass.

    verdict: a run verdict; kind: an incident kind; tests: {name: PASS|FAIL}.
    Other UNKNOWNs (no verified sha, a RESET-button reboot on an unpassed
    sha) are not settled this way.
    """
    if triage != "UNKNOWN":
        return False
    if verdict in UNKNOWN_VERDICTS:
        return True
    if kind in ("SILENT", "HEALTH_FAIL"):
        return True
    failed = [n for n, v in (tests or {}).items() if v == "FAIL"]
    return verdict == "FAIL" and bool(failed) and all(n == LOOPBACK_TEST for n in failed)


def _get(row, k, default=None):
    try:
        v = row[k]
    except (KeyError, IndexError, TypeError):
        return default
    return default if v is None else v


def confirm(subject, last_pass_sha, flash_and_capture, conn=None, t=None, now=time.time):
    """Reflash last_pass_sha and settle subject's UNKNOWN triage.

    subject: an incidents row/dict (has "id" and "kind") or a runs row/dict
    (has "run_id" and "verdict"), or a dict with both "incident_id" and
    "run_id". With conn, the result is written (write_confirm).
    """
    inc_id = _get(subject, "incident_id") or (_get(subject, "id") if _get(subject, "kind")
                                              else None)
    run_id = _get(subject, "run_id")
    if inc_id is not None and run_id is None:
        try:
            run_id = json.loads(_get(subject, "evidence", "{}")).get("run_id")
        except (ValueError, TypeError, AttributeError):
            run_id = None
    verdict = _get(subject, "verdict")
    sha = _get(subject, "firmware") or _get(subject, "commit") or _get(subject, "sha")
    if conn is not None and run_id is not None and not verdict:
        r = conn.execute('SELECT verdict, "commit" FROM runs WHERE run_id=?',
                         (run_id,)).fetchone()
        if r is not None:
            verdict, sha = r[0], sha or r[1]
    original = verdict or _get(subject, "kind") or "UNKNOWN"
    prior = _get(subject, "triage", "UNKNOWN")
    cap, err = None, None
    if not last_pass_sha:
        err = "no passing sha is recorded to reflash"
    else:
        try:
            cap = flash_and_capture(last_pass_sha)
        except Exception as e:  # the hook failing decides nothing
            err = "the reflash of %s failed to run (%s)" % (last_pass_sha, e.__class__.__name__)
    t = now() if t is None else t
    res = _judge(original, sha, last_pass_sha, prior, cap, err, t)
    if conn is not None:
        write_confirm(conn, res, incident_id=inc_id, run_id=run_id)
    return res


def _judge(original, sha, lp, prior, cap, err, t):
    what = "%s ended as %s" % ("Verified sha %s" % sha if sha else "The run", original)
    lines = list(cap.lines) if cap else []
    ret = output_returned(lines) if cap else None
    common = dict(prior_triage=prior, original=original, sha=sha, last_pass_sha=lp,
                  reflash_verdict=cap.verdict if cap else None, output_returned=ret,
                  reflash_lines=lines, reflash_run_id=cap.run_id if cap else None, t=t)
    if cap is not None and cap.verdict in UNJUDGED_VERDICTS:
        err = "the reflash of %s gave %s, which says nothing about the board" % (lp, cap.verdict)
    if err or cap is None:
        return ConfirmResult("NOT_CONFIRMED", "UNKNOWN", route_for("UNKNOWN"),
                             "%s; not confirmed: %s." % (what, err), **common)
    first = next((_line_text(x) for x in lines if _line_text(x).startswith("LW ")), None)
    if cap.verdict == "PASS":
        reason = ("%s; the reflash of last passing sha %s ran clean (LW RESULT PASS), so the "
                  "change is at fault." % (what, lp))
        triage = "CODE"
    else:
        seen = ("output returned, first %s" % _q(first)) if first else "no LW output returned"
        reason = ("%s; the reflash of last passing sha %s still failed as %s (%s), so the board "
                  "is at fault." % (what, lp, cap.verdict, seen))
        triage = "HARDWARE"
    return ConfirmResult("CONFIRMED", triage, route_for(triage), reason, **common)


def confirm_alert_text(iid, inc, res):
    """Fixed wording of the "confirmed" alert (a reply to the incident's open alert): 2 lines."""
    from bench.alerts import board_name
    b = board_name(_get(inc, "board", "?"))
    team = (res.route or "Triage").replace(" team", "").lower()
    if res.status == "CONFIRMED":
        what = {"CODE": "It is the code, not the board.",
                "HARDWARE": "It is the board, not the code."}.get(res.triage, res.triage + ".")
        head = "✔ %s: checked by putting the last good code back. %s (#%d)" % (b, what, iid)
    else:
        head = "✖ %s: the check could not tell if it is the board or the code. (#%d)" % (b, iid)
    return "%s\nTeam: %s. Ask: /ask %d" % (head, team, iid)


def _merge_evidence(raw, res):
    try:
        ev = json.loads(raw or "{}")
        if not isinstance(ev, dict):
            ev = {"previous": ev}
    except ValueError:
        ev = {}
    ev["confirm"] = res.evidence()
    return store.dumps(ev)


def write_confirm(conn, res, incident_id=None, run_id=None):
    """Write a ConfirmResult: incident and run triage/reason/route/evidence, routing rows
    (updated in place so the tracker mirror's ticket stays), an event, and the "confirmed" alert.

    Returns the alert id (None without an incident, or if already written).
    """
    aid = None
    with conn:
        inc = None
        if incident_id is not None:
            inc = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
            if inc is not None and run_id is None:
                try:
                    run_id = json.loads(inc["evidence"] or "{}").get("run_id")
                except (ValueError, AttributeError):
                    pass
        if inc is not None:
            conn.execute("UPDATE incidents SET triage=?, reason=?, route=?, evidence=? "
                         "WHERE id=?", (res.triage, res.reason, res.route,
                                        _merge_evidence(inc["evidence"], res), incident_id))
        if run_id is not None:
            r = conn.execute("SELECT evidence FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if r is not None:
                conn.execute("UPDATE runs SET triage=?, evidence=? WHERE run_id=?",
                             (res.triage, _merge_evidence(r[0], res), run_id))
        where, args = [], []
        if inc is not None:
            where.append("incident_id=?")
            args.append(incident_id)
        if run_id is not None:
            where.append("run_id=?")
            args.append(run_id)
        n = 0
        if where:
            n = conn.execute("UPDATE routing SET triage=?, route=?, reason=? WHERE " +
                             " OR ".join(where),
                             [res.triage, res.route, res.reason] + args).rowcount
            if n == 0:
                store.insert(conn, "routing", {
                    "incident_id": incident_id if inc is not None else None, "run_id": run_id,
                    "triage": res.triage, "route": res.route, "reason": res.reason,
                    "created": res.t})
        board = _get(inc, "board") if inc is not None else None
        store.event(conn, res.t, board or "?", "TRIAGE_CONFIRM",
                    {"incident_id": incident_id if inc is not None else None, "run_id": run_id,
                     "status": res.status, "from": res.prior_triage, "to": res.triage,
                     "reflashed_sha": res.last_pass_sha, "reflash_verdict": res.reflash_verdict,
                     "output_returned": res.output_returned})
        if inc is not None:
            aid = store.add_alert(conn, incident_id, TRANSITION,
                                  confirm_alert_text(incident_id, inc, res), res.t)
    return aid
