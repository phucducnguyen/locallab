"""The bench's alert wording (docs/01-day-contract.md, "Alerts"), fixed text, no model.

Board text inside an alert is quoted as data; alerts are sent as plain text
(no parse mode), so nothing in a board line can format or link anything.
"""

import json

from bench.decode import fault_alert_line

TEXT_MAX = 3800   # Telegram allows 4096 characters per message


def _get(inc, k):
    try:
        return inc[k]
    except (KeyError, IndexError):
        return None


def _facts(inc):
    try:
        ev = json.loads(_get(inc, "evidence") or "{}")
    except (ValueError, TypeError):
        return {}
    if not isinstance(ev, dict):
        return {}
    f = ev.get("facts") if isinstance(ev.get("facts"), dict) else {}
    out = dict(f)
    for k in ("device", "pins", "pin_in"):
        if ev.get(k) is not None and out.get(k) is None:
            out[k] = ev[k]
    return out


def device_what(inc):
    """The device part of the first alert's head, or None (the loopback keeps the bare head).

    "sensor link OPEN (A2-A3)", "radio link STUCK (A4-A5)", "emergency stop pressed
    (operator action)", "power low (vdd 2950 mV)", "power brown-out reset (BOR)".
    """
    f = _facts(inc)
    dev = f.get("device")
    if dev in ("sensor", "radio"):
        st = f.get("state") or ((f.get(dev) or ["?"])[-1])
        return "%s link %s (%s)" % (dev, st, f.get("pins") or "?")
    if dev == "estop" or _get(inc, "kind") == "ESTOP":
        return "emergency stop pressed (operator action)"
    if dev == "power":
        if f.get("cause") == "BOR":
            return "power brown-out reset (BOR)"
        vdd = f.get("vdd") or []
        return "power low (vdd %s mV)" % (vdd[-1] if vdd else "?")
    return None


def alert_text(transition, inc, cite=None):
    """The fixed wording for open, resolve and flap-<n>; inc is an incidents row or dict."""
    iid = _get(inc, "id")
    if transition == "open":
        head = "LOCAL LAB incident %d OPEN" % iid
        what = device_what(inc)
        if what:
            head += ": " + what
    elif transition == "resolve":
        head = "LOCAL LAB incident %d RESOLVED after %.0f s" % (iid, _get(inc, "duration_s") or 0)
    elif transition.startswith("flap-"):
        head = "LOCAL LAB incident %d FLAPPING (%d flaps)" % (iid, _get(inc, "flaps") or 0)
    else:
        raise ValueError(transition)
    out = [head,
           "Board: %s" % _get(inc, "board"),
           "Kind: %s" % _get(inc, "kind"),
           "Triage: %s" % _get(inc, "triage"),
           "Routed to: %s" % (_get(inc, "route") or "none"),
           "Reason: %s" % _get(inc, "reason")]
    if cite and cite.get("line"):
        out.append("Line: [%s] %s" % (cite.get("raw_ts"), cite["line"]))
    try:
        ev = json.loads(_get(inc, "evidence") or "{}")
        fl = ev.get("file_line")
    except (ValueError, AttributeError):
        ev, fl = {}, None
    fault = fault_alert_line(ev) if transition == "open" else None
    if fault:
        out.append(fault)
    if fl:
        out.append("File: %s" % fl)
    out.append("Incident: %d" % iid)
    return "\n".join(out)


def diag_text(iid, cmd, cites, timeout_s):
    out = ["LOCAL LAB incident %d diagnostic" % iid, "Sent: %s" % cmd]
    if cites:
        out += ["Reply: [%s] %s" % (c["raw_ts"], c["line"]) for c in cites]
    else:
        out.append("Reply: none within %.0f s" % timeout_s)
    out.append("Incident: %d" % iid)
    return "\n".join(out)


UNAVAILABLE = "explanation unavailable; triage stands"


def explain_text(iid, text=None):
    body = (text or "").strip() or UNAVAILABLE
    head = "LOCAL LAB incident %d explanation" % iid
    tail = "Incident: %d" % iid
    room = TEXT_MAX - len(head) - len(tail) - 2
    if len(body) > room:
        # Keep the final DIAGNOSIS line; cut the middle.
        last = body.rstrip().splitlines()[-1][:300]
        body = body[:room - len(last) - 6] + "\n...\n" + last
    return "\n".join([head, body, tail])
