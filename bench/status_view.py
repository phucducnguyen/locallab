"""Full-screen, read-only status view for the GB10 monitor.

    python3 -m bench.status_view [--db PATH] [--log PATH] [--once] [--no-color]

Shows the live LW lines (green for a good HEALTH, red for faults), a banner per
open incident (HARDWARE or CODE), a stopwatch row per recent incident (fault,
alert sent, explanation, fix PR), the last Telegram text sent, and the model
footer. Serial lines come from --log (a text file the bench writes), else from
the events table. The DB is opened read-only; the serial port is never opened.

--once prints one plain-text frame and exits (for tests and a quick look over
ssh). q or Ctrl-C quits the full-screen view.
"""

import argparse
import re
import shutil
import sys
import time

from bench.status_data import (DEVICES, VDD_MIN_MV, Db, EventTail, LogTail, clean,
                                health_bad, latest_health, loads, parse_health)

FOOTER = "model: Qwen3.6-35B-A3B on this GB10 - no internet"
FRAME_S = 0.1          # redraw period (10 per second)
SERIAL_MIN = 3
WAIT_MAX_S = 600.0    # show a running "waiting" timer this long, then "--"

_BAD_RE = re.compile(r"HARDFAULT|RESULT FAIL")

PINS = {"loopback": "A0-A1/PA1", "sensor": "A2-A3/PB0", "radio": "A4-A5/PC0"}
BOARD_ROWS = (("loopback", "loopback (A0-A1)"), ("sensor", "sensor (A2-A3)"),
              ("radio", "radio (A4-A5)"), ("estop", "estop (B1)"), ("vdd", "vdd"),
              ("temp", "temp"))

KIND_WORDS = {"HEALTH_FAIL": "loopback check failing", "SILENT": "board silent",
              "LINK_DOWN": "serial link down", "REBOOT": "unexpected reboot",
              "HARDFAULT": "HardFault", "RUN_FAIL": "run failed"}

ANSI = {"ok": "\x1b[32m", "bad": "\x1b[31m", "banner": "\x1b[1;97;41m",
        "head_ok": "\x1b[1;30;42m", "head_bad": "\x1b[1;97;41m", "title": "\x1b[1m",
        "dim": "\x1b[2m", "plain": ""}


# ---------- classification ----------

def classify(line):
    """'ok', 'bad' or 'plain' for one (already cleaned) serial line."""
    i = line.find("LW ")
    lw = line[i:] if i >= 0 else line
    if lw.startswith("LW DUMP") or _BAD_RE.search(lw):
        return "bad"
    h = parse_health(lw)
    if h is not None:
        # One predicate for the line and the BOARD panel: any device OPEN/STUCK,
        # estop ON or vdd under 3000 mV is red; otherwise green once loopback is OK.
        if health_bad(h):
            return "bad"
        if h.get("loopback") == "OK":
            return "ok"
    return "plain"


# ---------- banners ----------

def _names(v):
    """Fault bit names from a list of names, a list of dicts, a dict or a string."""
    if v is None:
        return []
    if isinstance(v, str):
        return [w for w in re.split(r"[\s,|]+", v) if w]
    if isinstance(v, dict):
        return [k for k, on in v.items() if on]
    out = []
    if isinstance(v, (list, tuple)):
        for x in v:
            if isinstance(x, dict):
                x = x.get("name") or x.get("bit") or x.get("short")
            if x:
                out.append(str(x))
    return out


def _decoded(ev):
    """{register: [bit names]} from the evidence, read defensively."""
    out = {}
    places = [ev] + [ev.get(k) for k in ("decode", "decoded", "fault_decode", "facts")
                     if isinstance(ev.get(k), dict)]
    for p in places:
        for key, val in p.items():
            m = re.fullmatch(r"([a-z]+)_bits", key)
            if m and _names(val):
                out.setdefault(m.group(1).upper(), _names(val))
            elif key in ("cfsr", "hfsr") and isinstance(val, (list, dict)) and p is not ev:
                if _names(val):
                    out.setdefault(key.upper(), _names(val))
    return out


def _file_line(ev):
    for p in (ev, ev.get("decode"), ev.get("decoded"), ev.get("facts")):
        if isinstance(p, dict) and isinstance(p.get("file_line"), str) and p["file_line"]:
            return p["file_line"]
    return None


def _registers(ev):
    for v in (ev.get("registers"), (ev.get("facts") or {}).get("fault")
              if isinstance(ev.get("facts"), dict) else None):
        if isinstance(v, dict) and v:
            return v
    return {}


def _bad_devices(ev):
    """{device: OPEN|STUCK} for a HEALTH_FAIL incident, read defensively from the evidence."""
    out = {}
    for src in (ev.get("fields"), ev.get("facts")):
        if not isinstance(src, dict):
            continue
        for d in DEVICES:
            v = src.get(d)
            if isinstance(v, list) and v:
                v = v[-1]
            if v in ("OPEN", "STUCK"):
                out.setdefault(d, v)
    if out:
        return out
    lines = ev.get("lines")
    for c in lines if isinstance(lines, list) else []:
        line = c.get("line") if isinstance(c, dict) else c
        h = parse_health(line) if isinstance(line, str) else None
        for d, v in health_bad(h) if h else []:
            if d in DEVICES:
                out.setdefault(d, v)
    return out


def banner(inc, causes=None):
    """(title, head, detail) for one open incident; causes are DUMP cause names."""
    ev = loads(inc.get("evidence"))
    kind, triage = inc.get("kind") or "?", inc.get("triage") or "UNKNOWN"
    fl = _file_line(ev)
    if kind == "HEALTH_FAIL":
        bad = _bad_devices(ev)
        title = (", ".join("%s %s on %s" % (d, v, PINS[d]) for d, v in sorted(bad.items()))
                 if bad else "board health check failing")
    elif kind == "HARDFAULT":
        if fl:
            title = "HardFault at %s" % fl
        else:
            pc = _registers(ev).get("pc")
            title = "HardFault (pc=%s, no file:line yet)" % pc if pc else "HardFault"
    elif kind == "RUN_FAIL":
        title = "RESULT FAIL" + (" at %s" % fl if fl else "")
    else:
        title = KIND_WORDS.get(kind, kind)
    title = "%s: %s" % (triage if triage in ("HARDWARE", "CODE") else "UNKNOWN", title)
    bits = _decoded(ev)
    if bits:
        detail = "  ".join("%s %s" % (reg, " ".join(names)) for reg, names in sorted(bits.items()))
    if causes:
        detail = ("cause %s" % " ".join(causes)) + ("  " + detail if bits else "")
    elif bits:
        pass
    else:
        detail = inc.get("reason") or ""
    state = inc.get("state") or "OPEN"
    if state == "FLAPPING":
        state = "FLAPPING (%s flaps)" % inc.get("flaps")
    head = "incident %s %s on %s, route %s" % (inc.get("id"), state, inc.get("board"),
                                                inc.get("route") or "none")
    return clean(title, 200), clean(head, 200), clean(detail, 300)


# ---------- stopwatch ----------

def _sent(alerts, pred):
    ts = [a["sent"] for a in alerts if a.get("sent") is not None and pred(a["transition"] or "")]
    return min(ts) if ts else None


def stopwatch(inc, alerts, now):
    """One row: fault at t0, alert +x, explanation +y, fix PR +z (seconds after t0)."""
    t0 = inc.get("t_first_bad_line") or inc.get("t_open") or inc.get("opened")
    alert = inc.get("t_alert_sent") or _sent(alerts, lambda t: t == "open")
    explain = _sent(alerts, lambda t: t == "explain")
    fix = _sent(alerts, lambda t: t == "fix" or t.startswith("fix"))
    is_open = inc.get("state") != "RESOLVED"

    def rel(t, waiting):
        if t is None or t0 is None:
            if waiting and t0 is not None and is_open and now - t0 < WAIT_MAX_S:
                return "waiting %.1f s" % max(0.0, now - t0)
            return "--"
        return "+%.1f s" % (t - t0)

    t0s = (time.strftime("%H:%M:%S", time.gmtime(t0)) + ".%03d" % int((t0 % 1) * 1000)
           if t0 is not None else "--")
    marks = "".join(" [%s]" % m for m in ("diag", "confirmed")
                    if _sent(alerts, lambda t, m=m: t == m) is not None)
    row = "#%-3s %-11s %-8s fault %s | alert %s | explain %s | fix PR %s%s" % (
        inc.get("id"), inc.get("kind"), inc.get("triage"), t0s,
        rel(alert, True), rel(explain, alert is not None),
        rel(fix, False) if inc.get("triage") == "CODE" or fix else "n/a", marks)
    return clean(row, 300)


# ---------- the board panel ----------

def board_panel(h):
    """Title plus one row per device from the newest HEALTH line (absent = unknown)."""
    h = h or {}
    bad = dict(health_bad(h)) if h else {}
    rows = [("BOARD (latest LW HEALTH%s)" % (" %s" % h["n"] if h.get("n") is not None else
                                             "" if h else ": none yet"), "title")]
    for key, label in BOARD_ROWS:
        v = h.get(key)
        if v is None:
            text, style = "unknown", "dim"
        elif key == "vdd":
            text = "%d mV%s" % (v, " (under %d mV)" % VDD_MIN_MV if key in bad else "")
            style = "bad" if key in bad else "ok"
        elif key == "temp":
            text, style = "%g C" % v, "plain"   # no threshold agreed
        else:
            text = "%s" % v + (" on %s" % PINS[key] if key in bad and key in PINS else "")
            style = "bad" if key in bad else "ok"
        mark = "! " if style == "bad" else "+ " if style == "ok" else "  "
        rows.append(("%s%-18s %s" % (mark, label, clean(text, 60)), style))
    return rows


# ---------- the frame ----------

def frame(st, serial_lines, serial_note, width, height, now):
    """A list of (text, style) rows, exactly height rows of at most width chars."""
    width, height = max(width, 20), max(height, 6)
    top, mid = [], []
    n_open = len(st["open"])
    clock = time.strftime("%H:%M:%S UTC", time.gmtime(now))
    hb = st.get("heartbeat")
    hb_s = ("bench %.0f s ago" % (now - hb)) if hb else "bench heartbeat n/a"
    boards = ",".join(clean(b, 20) for b in st.get("boards") or []) or "-"
    status = ("FAULT: %d open incident%s" % (n_open, "" if n_open == 1 else "s")
              if n_open else "ALL CLEAR")
    top.append((" LAB WATCH  %s  |  %s  |  board %s  |  %s" % (status, clock, boards, hb_s),
                "head_bad" if n_open else "head_ok"))
    if st.get("error"):
        top.append(("! " + clean(st["error"], 200), "bad"))

    for inc in st["open"][:3]:
        title, head, detail = banner(inc, st.get("causes", {}).get(inc.get("id")))
        top.append(("", "banner"))
        top.append(("  >>> %s <<<" % title, "banner"))
        top.append(("      " + head, "banner"))
        if detail:
            top.append(("      " + detail, "banner"))
        top.append(("", "banner"))
    if len(st["open"]) > 3:
        top.append(("  ... and %d more open" % (len(st["open"]) - 3), "bad"))

    mid += board_panel(latest_health(list(serial_lines)))
    mid.append(("STOPWATCH (seconds after the first bad line)", "title"))
    if st["recent"]:
        for inc in st["recent"][:4]:
            mid.append(("  " + stopwatch(inc, st["alerts"].get(inc["id"], []), now),
                        "bad" if inc.get("state") != "RESOLVED" else "plain"))
    else:
        mid.append(("  no incidents yet", "dim"))
    mid.append(("LAST TELEGRAM SENT", "title"))
    ls = st.get("last_sent")
    if ls:
        sent = time.strftime("%H:%M:%S", time.gmtime(ls["sent"]))
        mid.append(("  [%s UTC, incident %s, %s]" % (sent, ls.get("incident_id"),
                                                     clean(ls.get("transition"), 20)), "dim"))
        text_lines = [clean(x) for x in (ls.get("text") or "").splitlines() if x.strip()]
        mid += [("  " + x, "plain") for x in text_lines[:8]]
        if len(text_lines) > 8:
            mid.append(("  ...", "dim"))
    else:
        mid.append(("  nothing sent yet", "dim"))

    footer = [(" %s   (q quits)" % FOOTER, "dim")]
    serial_head = [("SERIAL (newest last)", "title")]
    room = height - len(top) - len(footer) - len(serial_head)
    if room - len(mid) < SERIAL_MIN:  # shrink the middle first, then the banners
        mid = mid[:max(0, room - SERIAL_MIN)]
    room -= len(mid)
    if room < 1:
        top = top[:max(1, height - len(footer) - len(serial_head) - 1)]
        room = height - len(top) - len(footer) - len(serial_head)
    body = []
    if serial_note and not serial_lines:
        body.append(("  " + clean(serial_note, 200), "dim"))
    for line in list(serial_lines)[-max(room, 0):] if room > 0 else []:
        cls = classify(line)
        body.append((("+ " if cls == "ok" else "! " if cls == "bad" else "  ") + line, cls))
    body = body[-room:] if room > 0 else []
    body += [("", "plain")] * (max(room, 0) - len(body))
    rows = (top + mid + serial_head + body + footer)[:height]
    rows += [("", "plain")] * (height - len(rows))
    return [(t[:width], s) for t, s in rows]


def plain(rows, width, color):
    out = []
    for text, style in rows:
        if color and ANSI.get(style):
            fill = text.ljust(width) if style in ("banner", "head_ok", "head_bad") else text
            out.append(ANSI[style] + fill + "\x1b[0m")
        else:
            out.append(text.rstrip())
    return "\n".join(out) + "\n"


# ---------- sources and loop ----------

class View:
    def __init__(self, db_path, log_path=None):
        self.db = Db(db_path)
        self.tail = LogTail(log_path) if log_path else EventTail(Db(db_path))

    def poll(self):
        st = self.db.read()
        self.tail.poll()
        return st

    def rows(self, width, height, now=None):
        st = self.poll()
        return frame(st, self.tail.lines, self.tail.note, width, height,
                     time.time() if now is None else now)

    def close(self):
        self.db.close()
        if isinstance(self.tail, LogTail):
            self.tail.close()
        else:
            self.tail.db.close()


def run_curses(view, color):
    import curses

    def loop(scr):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        scr.timeout(int(FRAME_S * 1000))
        attrs = {k: curses.A_NORMAL for k in ANSI}
        attrs.update(title=curses.A_BOLD, dim=curses.A_DIM, head_ok=curses.A_REVERSE,
                     head_bad=curses.A_REVERSE | curses.A_BOLD,
                     banner=curses.A_REVERSE | curses.A_BOLD, bad=curses.A_BOLD)
        if color and curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            curses.init_pair(1, curses.COLOR_GREEN, bg)
            curses.init_pair(2, curses.COLOR_RED, bg)
            curses.init_pair(3, curses.COLOR_WHITE, curses.COLOR_RED)
            curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_GREEN)
            attrs.update(ok=curses.color_pair(1), bad=curses.color_pair(2) | curses.A_BOLD,
                         banner=curses.color_pair(3) | curses.A_BOLD,
                         head_bad=curses.color_pair(3) | curses.A_BOLD,
                         head_ok=curses.color_pair(4) | curses.A_BOLD)
        while True:
            h, w = scr.getmaxyx()
            rows = view.rows(w, h)
            scr.erase()
            for y, (text, style) in enumerate(rows[:h]):
                fill = style in ("banner", "head_ok", "head_bad")
                s = text.ljust(w) if fill else text
                n = w - 1 if y == h - 1 else w
                try:
                    scr.addnstr(y, 0, s, max(n, 0), attrs.get(style, curses.A_NORMAL))
                except curses.error:
                    pass
            scr.noutrefresh()
            curses.doupdate()
            k = scr.getch()
            if k in (ord("q"), ord("Q")):
                return
            if k == curses.KEY_RESIZE:
                curses.update_lines_cols()

    curses.wrapper(loop)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m bench.status_view", description=__doc__.split("\n\n")[1])
    ap.add_argument("--db", help="bench DB, opened read-only (default $LW_DB or "
                                 "~/.local/state/labwatch/bench.db)")
    ap.add_argument("--log", help="serial log file to tail (default: the events table)")
    ap.add_argument("--once", action="store_true", help="print one plain-text frame and exit")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--width", type=int, help="--once frame width (default: terminal)")
    ap.add_argument("--height", type=int, help="--once frame height (default: terminal)")
    a = ap.parse_args(argv)
    view = View(a.db, a.log)
    try:
        if a.once:
            size = shutil.get_terminal_size((100, 40))
            w, h = a.width or size.columns, a.height or size.lines
            sys.stdout.write(plain(view.rows(w, h), w, color=not a.no_color))
            sys.stdout.flush()
            return 0
        run_curses(view, color=not a.no_color)
    except KeyboardInterrupt:
        pass
    finally:
        view.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
