"""The bench's alert wording (docs/01-day-contract.md, "Alerts"), fixed text, no model.

Lane T (2026-10-25, Patrick): every Telegram message starts with one plain-English
line a non-technical reader understands, then a short technical part. At most 6
lines; the incident number is always there. The first alert:

    🔴 Problem on test board 1: the sensor wire (A2–A3) is disconnected.
    Who: Hardware team · Not a software bug.
    Details: sensor link OPEN (A2–A3 wire) · nucleo-1 · fw a0f0c9f · #5
    Line: LW HEALTH 812 ... sensor=OPEN  [20:41:07.123]
    Alert 1.9 s after the fault.
    Why: <the triage reason>                (CODE and UNKNOWN only)

Resolve, flap, diag, explain, decode and confirm rows are one to three lines.
Board text inside an alert is quoted as data; alerts are sent as plain text
(no parse mode), so nothing in a board line can format or link anything.
"""

import json
import re

TEXT_MAX = 3800   # Telegram allows 4096 characters per message
LINE_MAX = 90     # the quoted board line
REASON_MAX = 140
EXPLAIN_LINE_MAX = 200
UNAVAILABLE = "explanation unavailable; triage stands"
LOOPBACK_PINS = "A0-A1"
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
    """'nucleo-1' -> 'test board 1' for the plain line (the id stays in Details)."""
    m = re.search(r"(\d+)$", board or "")
    return "test board %s" % m.group(1) if m else (board or "the test board")


def _cap(s):
    return s[:1].upper() + s[1:]


def _link(name, pins, st):
    """'sensor link OPEN (A2–A3 wire)' / 'radio link STUCK (A5 to ground)'."""
    if st == "STUCK":
        where = "%s to ground" % (pins or "?").split("-")[-1]
    elif st == "OPEN":
        where = "%s wire" % _pins(pins)
    else:
        where = _pins(pins)
    return "%s %s (%s)" % (name, st or "?", where)


def _fault_causes(ev):
    """Cause names or CFSR bits from the decode summary, without the registers."""
    s = ((ev.get("fault") or {}) if isinstance(ev.get("fault"), dict) else {}).get("summary")
    if not s:
        return None
    parts = [p for p in s.split("; ") if not re.match(r"(pc|lr)=", p)]
    fn = (ev.get("addr2line") or {}).get("function") if isinstance(
        ev.get("addr2line"), dict) else None
    return "; ".join(parts) + (" in %s()" % fn if fn else "")


def hardfault_what(ev):
    fl = ev.get("file_line")
    head = "HardFault at %s" % fl if fl else "HardFault (no file:line)"
    causes = _fault_causes(ev)
    return head + (" · " + causes if causes else "")


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


def device_what(inc):
    """The technical what: device and state with pins, ESTOP, HardFault, run verdict."""
    dev, f, ev = _device(inc)
    kind = _get(inc, "kind")
    if dev in WIRE_NAMES:
        name = "loopback" if dev == "loopback" else "%s link" % dev
        return _link(name, f.get("pins") or DEFAULT_PINS[dev], _state(dev, f))
    if dev == "estop":
        return "emergency stop pressed (operator action)"
    if dev == "power":
        if f.get("cause") == "BOR":
            return "power brown-out reset (BOR)"
        return "power low (vdd %s mV)" % (_last(f.get("vdd")) or "?")
    if kind == "HARDFAULT":
        return hardfault_what(ev)
    if kind == "RUN_FAIL":
        return "%s on %s" % (f.get("verdict") or "FAIL", _get(inc, "firmware") or "?")
    return {"SILENT": "board silent", "REBOOT": "unexpected reboot",
            "LINK_DOWN": "serial link down"}.get(kind, kind or "?")


def device_tag(inc):
    """A short technical name of what the incident is about, for resolve and flap."""
    dev, f, ev = _device(inc)
    kind = _get(inc, "kind")
    if dev in WIRE_NAMES:
        name = "loopback" if dev == "loopback" else "%s link" % dev
        return "%s (%s)" % (name, _pins(f.get("pins") or DEFAULT_PINS[dev]))
    if dev == "estop":
        return "emergency stop"
    if dev == "power":
        return "power"
    if kind == "HARDFAULT":
        return "HardFault at %s" % ev["file_line"] if ev.get("file_line") else "HardFault"
    if kind == "RUN_FAIL":
        return "run %s" % (f.get("verdict") or "FAIL")
    return kind or "?"


def _thing(inc):
    """The plain noun: 'sensor wire (A2–A3)', 'power supply', 'emergency stop button'."""
    dev, f, _ = _device(inc)
    if dev in WIRE_NAMES:
        return "%s (%s)" % (WIRE_NAMES[dev], _pins(f.get("pins") or DEFAULT_PINS[dev]))
    if dev == "power":
        return "power supply"
    if dev == "estop":
        return "emergency stop button"
    return None


def plain_what(inc):
    """What happened, in words for someone who has never seen the bench."""
    dev, f, ev = _device(inc)
    kind = _get(inc, "kind")
    if dev in WIRE_NAMES:
        st = _state(dev, f)
        how = {"OPEN": "is disconnected", "STUCK": "is shorted to ground"}.get(st, "is faulty")
        return "the %s %s" % (_thing(inc), how)
    if kind == "HARDFAULT":
        return "the firmware crashed"
    if kind == "RUN_FAIL":
        v = f.get("verdict")
        return {"NO_OUTPUT": "the board printed nothing after the update",
                "RESET_LOOP": "the board keeps restarting",
                "HARDFAULT": "the firmware crashed during its tests"}.get(
                    v, "the firmware failed its tests")
    return {"SILENT": "the board stopped responding",
            "REBOOT": "the board restarted unexpectedly",
            "LINK_DOWN": "the cable link to the board is down"}.get(kind, "something went wrong")


def quote(cite):
    if not cite or not cite.get("line"):
        return None
    return "Line: %s  [%s]" % (trim(cite["line"], LINE_MAX), cite.get("raw_ts") or "?")


def _head(inc, triage):
    dev, f, _ = _device(inc)
    b = board_name(_get(inc, "board"))
    if dev == "estop":
        return "⛔ Someone pressed the emergency stop on %s. Not a fault." % b
    if dev == "power":
        if f.get("cause") == "BOR":
            return "🔴 %s reset because its power supply dipped too low." % _cap(b)
        v = _last(f.get("vdd"))
        volts = " (%.2f V)" % (v / 1000.0) if isinstance(v, (int, float)) else ""
        return "🔴 %s is getting too little power%s." % (_cap(b), volts)
    if triage == "UNKNOWN":
        return ("🟡 Problem on %s: %s. Checking whether it's the board or the code…"
                % (b, plain_what(inc)))
    return "🔴 Problem on %s: %s." % (b, plain_what(inc))


def _who(inc, triage, route):
    if _device(inc)[0] == "estop":
        return None   # an operator action: nobody to call
    if triage == "HARDWARE":
        return "Who: %s · Not a software bug." % (route or "Hardware team")
    if triage == "CODE":
        return "Who: %s · Caused by the latest code change (%s)." % (
            route or "Firmware team", _get(inc, "firmware") or "?")
    if triage == "UNKNOWN":
        return "Who: %s" % (route or "Triage")
    return None   # NONE (an operator action): nobody to call


def alert_text(transition, inc, cite=None):
    """The fixed wording for open, resolve and flap-<n>; inc is an incidents row or dict."""
    iid = _get(inc, "id")
    b = board_name(_get(inc, "board"))
    thing = _thing(inc)
    if transition == "resolve":
        n = _get(inc, "duration_s") or 0
        if thing == "emergency stop button":
            plain = "✅ Cleared: the emergency stop on %s was released (after %.0f s)." % (b, n)
        elif thing:
            plain = "✅ Fixed: %s's %s is back to normal (after %.0f s)." % (b, thing, n)
        else:
            plain = "✅ Fixed: %s is working normally again (after %.0f s)." % (b, n)
        return "%s\n#%d · %s resolved" % (plain, iid, device_tag(inc))
    if transition.startswith("flap-"):
        n = _get(inc, "flaps") or 0
        if thing and "wire" in thing:
            plain = ("🟠 %s's %s keeps connecting and disconnecting (%d times). Probably loose."
                     % (_cap(b), thing, n))
        else:
            plain = "🟠 %s keeps failing and recovering (%d times)." % (
                _cap(thing and "%s's %s" % (b, thing) or b), n)
        return "%s\n#%d · %s flapping" % (plain, iid, device_tag(inc))
    if transition != "open":
        raise ValueError(transition)
    triage = _get(inc, "triage") or "UNKNOWN"
    route = _get(inc, "route")
    out = [_head(inc, triage)]
    who = _who(inc, triage, route)
    if who:
        out.append(who)
    out.append("Details: %s · %s · fw %s · %s · #%d" % (
        device_what(inc), _get(inc, "board") or "?", _get(inc, "firmware") or "?",
        triage, iid))
    q = quote(cite)
    if q:
        out.append(q)
    t_open, t_bad = _get(inc, "t_open"), _get(inc, "t_first_bad_line")
    if t_open is not None and t_bad is not None and t_open >= t_bad:
        out.append("Alert %.1f s after the fault." % (t_open - t_bad))
    if triage in ("CODE", "UNKNOWN") and _get(inc, "reason"):
        out.append("Why: " + trim(_get(inc, "reason"), REASON_MAX))
    return "\n".join(out)


def diag_text(iid, cmd, cites, timeout_s):
    if not cites:
        return ("🔧 The board did not answer its self-test (#%d).\nDetails: no reply to '%s' "
                "within %.0f s" % (iid, cmd, timeout_s))
    shown = [c for c in cites if not (c.get("line") or "").startswith("LW REPLY")] or cites
    body = " | ".join("%s  [%s]" % (trim(c["line"], LINE_MAX), c.get("raw_ts") or "?")
                      for c in shown[:3])
    failed = any(re.search(r"\bFAIL", c.get("line") or "") for c in cites)
    return "🔧 The board's own self-test %s (#%d).\nDetails: %s" % (
        "failed" if failed else "passed", iid, body)


def decode_text(iid, ev):
    """The decode follow-up row: where in the code the crash happened."""
    ev = ev or {}
    where = ev.get("file_line")
    plain = ("🧩 Found where the firmware crashed (#%d): %s" % (iid, where) if where
             else "🧩 Decoded the crash registers (#%d)." % iid)
    return "%s\nDetails: %s" % (plain, trim(hardfault_what(ev), 300))


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
        return "🧠 What the AI found (#%d): %s" % (iid, UNAVAILABLE)
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
    out = ["🧠 What the AI found (#%d): %s" % (
        iid, trim(_cites_fit(_sentences(cause, 2)), 2 * EXPLAIN_LINE_MAX)
        if cause else "see the diagnosis below.")]
    if nxt:
        out.append("Next step: " + trim(_cites_fit(_sentences(nxt, 1)), EXPLAIN_LINE_MAX))
    out.append(trim(_cites_fit(diag), EXPLAIN_LINE_MAX + 100))
    return "\n".join(out)
