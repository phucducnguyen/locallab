"""The bench's alert wording (docs/01-day-contract.md, "Alerts"), fixed text, no model.

Alert rule (2026-10-25, set by the operator): every Telegram message is 2-3 short lines in common
everyday English. The first line says what happened, with the board and the incident
number; the next says the team and points to /ask. Technical words (HardFault, pins,
file:line, vdd) appear only after "Details:" and only when needed. Raw lines, sha and
timings stay in the DB, the dashboard and the issue. Example:

    🔴 Board 1: the sensor wire came loose. (#5)
    Team: hardware. Not a code problem. Ask: /ask 5

Board text inside an alert is quoted as data; alerts are sent as plain text
(no parse mode), so nothing in a board line can format or link anything.
"""

import json
import re

TEXT_MAX = 3800   # Telegram allows 4096 characters per message
LINE_MAX = 90     # the quoted board line
EXPLAIN_LINE_MAX = 200
UNAVAILABLE = "explanation unavailable; triage stands"
WIRE_NAMES = {"loopback": "test wire", "sensor": "sensor wire", "radio": "radio wire"}
DEFAULT_PINS = {"loopback": "A0-A1", "sensor": "A2-A3", "radio": "A4-A5"}


def _get(inc, k):
    try:
        return inc[k]
    except (KeyError, IndexError, TypeError):
        return None


def trim(s, n):
    """One line, whitespace collapsed, at most n characters (an ellipsis marks a cut)."""
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _ev(inc):
    try:
        ev = json.loads(_get(inc, "evidence") or "{}")
    except (ValueError, TypeError):
        return {}
    return ev if isinstance(ev, dict) else {}


def _facts(inc, ev=None):
    ev = _ev(inc) if ev is None else ev
    f = ev.get("facts") if isinstance(ev.get("facts"), dict) else {}
    out = dict(f)
    for k in ("device", "pins", "pin_in"):
        if ev.get(k) is not None and out.get(k) is None:
            out[k] = ev[k]
    return out


def _pins(p):
    return (p or "?").replace("-", "–")


def _last(x):
    if isinstance(x, list):
        return x[-1] if x else None
    return x


def board_name(board):
    """'nucleo-1' -> 'Board 1' (the full name stays in the DB)."""
    m = re.search(r"(\d+)$", board or "")
    return "Board %s" % m.group(1) if m else (board or "The board")


def _device(inc):
    """(device key or None, facts, evidence): loopback, sensor, radio, power, estop."""
    ev = _ev(inc)
    f = _facts(inc, ev)
    dev, kind = f.get("device"), _get(inc, "kind")
    if dev is None and kind == "ESTOP":
        dev = "estop"
    if dev is None and kind == "HEALTH_FAIL":
        dev = "loopback"
    return dev, f, ev


def _state(dev, f):
    if dev == "loopback":
        return _last(f.get("loopback"))
    return f.get("state") or _last(f.get(dev))


def _thing(inc):
    """The plain noun: 'sensor wire', 'test wire', 'radio wire', 'power', 'stop button'."""
    dev = _device(inc)[0]
    if dev in WIRE_NAMES:
        return WIRE_NAMES[dev]
    return {"power": "power", "estop": "stop button"}.get(dev)


def plain_what(inc, triage):
    """What happened, in everyday words."""
    dev, f, ev = _device(inc)
    kind = _get(inc, "kind")
    if dev in WIRE_NAMES:
        st = _state(dev, f)
        how = {"OPEN": "came loose", "STUCK": "is stuck to ground"}.get(st, "is not working")
        return "the %s %s" % (WIRE_NAMES[dev], how)
    if kind == "HARDFAULT":
        return "the new code made it crash" if triage == "CODE" else "it crashed"
    if kind == "RUN_FAIL":
        v = f.get("verdict")
        if v == "NO_OUTPUT":
            return "it stopped talking after the update"
        if v == "RESET_LOOP":
            return "it keeps restarting"
        if v == "HARDFAULT":
            return "the new code made it crash" if triage == "CODE" else "it crashed"
        return "the new code failed its tests" if triage == "CODE" else "it failed its tests"
    return {"SILENT": "it stopped talking", "REBOOT": "it restarted by itself",
            "LINK_DOWN": "the cable to it is not working"}.get(kind, "something went wrong")


def _team(route, triage):
    if triage == "NONE":
        return "none needed"
    return (route or "Triage").replace(" team", "").lower()


def _details(inc):
    """The short technical part, or None."""
    dev, f, ev = _device(inc)
    if _get(inc, "kind") == "HARDFAULT" or f.get("verdict") == "HARDFAULT":
        return ev.get("file_line")
    return None


def _head(inc, triage, iid):
    dev, f, _ = _device(inc)
    b = board_name(_get(inc, "board"))
    if dev == "estop":
        return "⛔ %s: someone pressed the stop button. Not a fault. (#%d)" % (b, iid)
    if dev == "power":
        if f.get("cause") == "BOR":
            return "🔴 %s restarted because its power dropped. (#%d)" % (b, iid)
        return "🔴 %s is not getting enough power. (#%d)" % (b, iid)
    if triage == "UNKNOWN":
        return "🟡 %s: %s. Checking if it is the board or the code. (#%d)" % (
            b, plain_what(inc, triage), iid)
    return "🔴 %s: %s. (#%d)" % (b, plain_what(inc, triage), iid)


def alert_text(transition, inc, cite=None):
    """The fixed wording for open, resolve and flap-<n>; inc is an incidents row or dict.

    cite (the quoted board line) is kept in the DB evidence, not in the message."""
    iid = _get(inc, "id")
    b = board_name(_get(inc, "board"))
    thing = _thing(inc)
    if transition == "resolve":
        n = _get(inc, "duration_s") or 0
        if thing == "stop button":
            return "✅ %s: the stop button was released after %.0f seconds. (#%d)" % (b, n, iid)
        if thing:
            return "✅ %s: the %s is back. Fixed after %.0f seconds. (#%d)" % (b, thing, n, iid)
        return "✅ %s is working again. Fixed after %.0f seconds. (#%d)" % (b, n, iid)
    if transition.startswith("flap-"):
        n = _get(inc, "flaps") or 0
        if thing and "wire" in thing:
            return "🟠 %s: the %s keeps coming and going. Probably loose. (#%d)" % (b, thing, iid)
        return "🟠 %s: the problem keeps coming and going (%d times). (#%d)" % (b, n, iid)
    if transition != "open":
        raise ValueError(transition)
    triage = _get(inc, "triage") or "UNKNOWN"
    if _device(inc)[0] == "estop":
        return _head(inc, triage, iid)   # an operator action: one line, nobody to call
    team = "Team: %s." % _team(_get(inc, "route"), triage)
    if triage == "HARDWARE":
        team += " Not a code problem."
    det = _details(inc)
    if det:
        team += " Details: %s." % det
    return "%s\n%s Ask: /ask %d" % (_head(inc, triage, iid), team, iid)


def diag_text(iid, cmd, cites, timeout_s):
    """The board self-check reply (the board lines stay in the incident evidence)."""
    if not cites:
        return "🔧 The board did not answer its self-check. (#%d)" % iid
    failed = any(re.search(r"\bFAIL", c.get("line") or "") for c in cites)
    return "🔧 The board ran its own self-check: it %s. (#%d)" % (
        "failed" if failed else "passed", iid)


def decode_text(iid, ev):
    """The decode follow-up row: where in the code the crash happened."""
    where = (ev or {}).get("file_line")
    if where:
        return "🧩 Found where the code crashed. (#%d)\nDetails: %s" % (iid, where)
    return "🧩 Could not find where the code crashed. (#%d) Ask: /ask %d" % (iid, iid)


# ---- the explain condenser ----

_MD = re.compile(r"\*\*|__|`+|^\s*#+\s*|^\s*[-*>]\s+|^\s*\d+[.)]\s+")
_CITE = re.compile(r"\s*\[[^\]]*\]")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\[(])")


def _plain(s):
    s = _MD.sub("", s)
    return " ".join(s.replace("**", "").replace("`", "").split())


def _heading(line):
    """(name, rest) when the line starts a section ('## Likely cause', '**Next step:** x',
    'Likely cause: x'), else None."""
    s = line.strip()
    m = re.match(r"^(#+)\s*(.+?)\s*:?\s*$", s)
    if m:
        return m.group(2).strip("* ").lower(), ""
    m = re.match(r"^\*\*([^*]{2,40}?):?\*\*:?\s*(.*)$", s)
    if m:
        return m.group(1).strip().lower(), m.group(2)
    m = re.match(r"^([A-Za-z][A-Za-z /'-]{1,30}):\s*(.*)$", s)
    if m and re.search(r"cause|next|step|action|evidence|summary|why|what|fix|recommend",
                       m.group(1), re.I):
        return m.group(1).strip().lower(), m.group(2)
    return None


def _sections(lines):
    secs, cur = [], None
    for ln in lines:
        h = _heading(ln)
        if h:
            cur = [h[0], [h[1]] if h[1] else []]
            secs.append(cur)
        elif cur is not None:
            cur[1].append(ln)
    return [(n, _plain(" ".join(b))) for n, b in secs]


def _sentences(text, n):
    parts = [p for p in _SENT.split(text.strip()) if p]
    return " ".join(parts[:n])


def _cites_fit(s):
    return s if len(s) <= EXPLAIN_LINE_MAX else _CITE.sub("", s).strip()


def explain_text(iid, text=None):
    """The model's answer condensed to at most 3 lines: DIAGNOSIS, likely cause, next step."""
    lines = (text or "").splitlines()
    diag = None
    for ln in reversed(lines):
        p = _plain(ln)
        if re.match(r"^DIAGNOSIS\b", p):
            diag = p
            break
    if diag is None:
        return "🧠 #%d: %s" % (iid, UNAVAILABLE)
    body = [ln for ln in lines if not re.match(r"^DIAGNOSIS\b", _plain(ln))]
    secs = _sections(body)
    cause = next((b for n, b in secs if "cause" in n and b), None)
    nxt = next((b for n, b in secs if re.search(r"next|step|action|recommend|fix", n) and b),
               None)
    if cause is None:
        if secs:
            pre = []
            for ln in body:
                if _heading(ln):
                    break
                pre.append(ln)
            cause = _plain(" ".join(pre)) or None
        else:
            cause = _plain(" ".join(body)) or None
    out = ["🧠 #%d: %s" % (iid, trim(_cites_fit(_sentences(cause, 2)), EXPLAIN_LINE_MAX)
                          if cause else "see the diagnosis below.")]
    if nxt:
        out.append("Next: " + trim(_cites_fit(_sentences(nxt, 1)), EXPLAIN_LINE_MAX))
    out.append(diag)
    return "\n".join(out)
