"""Full-screen visual dashboard for the GB10 monitor (Firefox kiosk mode).

    python3 -m bench.dashboard [--db PATH] [--host 127.0.0.1] [--port 8770] [--board NAME]

GET /           one self-contained page (inline CSS, JS and SVG; no external resources)
GET /state.json the current board state from the bench DB; the page polls it every 500 ms

The DB is opened read-only (bench.status_data.open_ro: mode=ro URI, busy timeout, reconnect
on error); this server never writes it. It binds 127.0.0.1 by default and has no auth.
"""

import argparse
import http.server
import json
import os
import re
import socketserver
import sqlite3
import sys
import threading
import time

from bench.status_data import (VDD_MIN_MV, Db, clean, loads, parse_health)

PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_page.html")
FOOTER = "Everything runs on this NVIDIA GB10 \u2014 no internet. Model: Qwen3.6-35B-A3B."
STALE_S = 5.0

# name -> (header pins, MCU pins, input pin on CN8 or None)
DEVICES = {
    "loopback": ("A0-A1", "PA0 to PA1"),
    "sensor": ("A2-A3", "PA4 to PB0"),
    "radio": ("A4-A5", "PC1 to PC0"),
    "estop": ("B1", "PC13"),
    "power": ("VDD", "VDD"),
}
ORDER = ("loopback", "sensor", "radio", "estop", "power")
LINKS = ("loopback", "sensor", "radio")
KIND_DEVICE = {"HEALTH_FAIL": "loopback", "ESTOP": "estop"}
KIND_WORDS = {"HEALTH_FAIL": "health check failing", "SILENT": "board silent",
              "LINK_DOWN": "serial link down", "REBOOT": "unexpected reboot",
              "HARDFAULT": "HardFault", "RUN_FAIL": "run failed", "ESTOP": "estop pressed"}


def health_from_fields(fields):
    """{loopback, sensor, radio, estop, vdd, temp, n} from a HEALTH event's fields:
    the parsed keys first, else the key=value text of fields.line; None when unknown."""
    parsed = parse_health(fields.get("line") if isinstance(fields.get("line"), str) else "")
    parsed = parsed or {}
    out = {}
    for k in LINKS:
        v = fields.get(k)
        out[k] = v if v in ("OK", "OPEN", "STUCK") else parsed.get(k)
    v = fields.get("estop")
    out["estop"] = v if v in ("ON", "OFF") else parsed.get("estop")
    for k, kind in (("vdd", int), ("temp", float), ("n", int)):
        v = fields.get(k)
        try:
            out[k] = kind(v) if v is not None and not isinstance(v, bool) else parsed.get(k)
        except (TypeError, ValueError):
            out[k] = parsed.get(k)
    return out


def incident_device(inc):
    """The device an incident is about: evidence.device, else by kind; None for board-level."""
    ev = loads(inc.get("evidence"))
    dev = ev.get("device")
    if isinstance(dev, str) and dev in DEVICES:
        return dev
    if dev == "vdd":
        return "power"
    if inc.get("kind") == "HEALTH_FAIL":
        f = ev.get("fields")
        if isinstance(f, dict):
            for k in LINKS:
                if f.get(k) in ("OPEN", "STUCK"):
                    return k
            if f.get("estop") == "ON":
                return "estop"
    return KIND_DEVICE.get(inc.get("kind"))


def _t0(inc):
    return inc.get("t_first_bad_line") or inc.get("t_open") or inc.get("opened")


def _sent(alerts, transition):
    for a in alerts:
        if a.get("transition") == transition:
            return a.get("sent") or a.get("created")
    return None


WIRE_WORDS = {"loopback": "test wire", "sensor": "sensor wire", "radio": "radio wire"}
TEAM = {"HARDWARE": "hardware", "CODE": "software", "ESTOP": "none needed",
        "UNKNOWN": "still checking"}
OK_HEAD = "\u2705 Everything is working"
WAIT_HEAD = "Waiting for the board\u2026"


def board_name(board):
    """'nucleo-1' -> 'Board 1'; anything without a trailing number -> 'The board'."""
    m = re.search(r"(\d+)$", board or "")
    return "Board %s" % int(m.group(1)) if m else "The board"


def incident_headline(inc, dev, state, board):
    """(level, text) in the plain words of the Telegram messages, for an open incident."""
    name, kind, tri = board_name(board), inc.get("kind"), inc.get("triage")
    if dev == "estop" or kind == "ESTOP" or tri == "ESTOP":
        return "stop", "\u26d4 %s: someone pressed the stop button. Not a fault." % name
    if dev in LINKS:
        wire = WIRE_WORDS[dev]
        if state == "STUCK":
            return "bad", "\U0001f534 %s: the %s is stuck." % (name, wire)
        if state == "OPEN":
            return "bad", "\U0001f534 %s: the %s came loose." % (name, wire)
        return "bad", "\U0001f534 %s: the %s has a problem." % (name, wire)
    if dev == "power":
        return "bad", "\U0001f534 %s: the power is too low." % name
    if kind == "HARDFAULT" or tri == "CODE":
        return "bad", "\U0001f534 %s: the new code made it crash." % name
    if kind in ("SILENT", "LINK_DOWN"):
        if tri in (None, "UNKNOWN"):
            return "warn", ("\U0001f7e1 %s stopped talking after the update. Checking if it "
                            "is the board or the code." % name)
        return "bad", "\U0001f534 %s stopped talking." % name
    if kind == "REBOOT":
        return "bad", "\U0001f534 %s restarted by itself." % name
    if kind == "RUN_FAIL":
        return "bad", "\U0001f534 %s: the test run failed." % name
    return "bad", "\U0001f534 %s: something went wrong." % name


def headline_sub(inc, alert_s, dev=None):
    team = "ESTOP" if dev == "estop" or inc.get("kind") == "ESTOP" else inc.get("triage")
    bits = ["Team: %s" % TEAM.get(team, "still checking")]
    if alert_s is not None:
        bits.append("alert sent in %.1f s" % alert_s)
    bits.append("incident #%s" % inc["id"])
    return " \u00b7 ".join(bits)


def first_sentence(text):
    """The first plain sentence of an explain alert's text (skips the LOCAL LAB head)."""
    if not isinstance(text, str):
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("LOCAL LAB") or line.startswith("Incident:") \
                or line == "...":
            continue
        line = re.sub(r"^(EXPLANATION|SUMMARY|DIAGNOSIS)\s*:\s*", "", line, flags=re.I)
        m = re.match(r"(.+?[.!?])(\s|$)", line)
        return clean(m.group(1) if m else line, 200)
    return None


def _since(t, t0):
    return round(t - t0, 1) if t is not None and t0 is not None else None


class Dashboard:
    def __init__(self, path, board=None):
        self.db = Db(path)
        self.board = board
        self.lock = threading.Lock()

    def state(self):
        with self.lock:
            try:
                return self._state()
            except (sqlite3.Error, OSError) as e:
                self.db.close()
                return {"error": "DB: %s" % clean(str(e), 200), "now": time.time(),
                        "devices": {}, "banner": {"level": "unknown", "text": "NO DATA"},
                        "headline": {"level": "unknown", "text": WAIT_HEAD, "sub": ""},
                        "stale": True, "footer": FOOTER}

    def _health(self):
        if self.board:
            rows = self.db.query("SELECT * FROM events WHERE kind='HEALTH' AND board=? "
                                 "ORDER BY id DESC LIMIT 1", (self.board,))
        else:
            rows = self.db.query("SELECT * FROM events WHERE kind='HEALTH' "
                                 "ORDER BY id DESC LIMIT 1")
        return dict(rows[0]) if rows else None

    def _state(self):
        snap = self.db.read()
        st = {"error": snap["error"], "now": time.time(), "footer": FOOTER}
        if snap["error"]:
            st.update(devices={}, banner={"level": "unknown", "text": "NO DATA"},
                      headline={"level": "unknown", "text": WAIT_HEAD, "sub": ""}, stale=True)
            return st
        ev = self._health()
        board = self.board or (ev and ev["board"]) or (snap["boards"][0] if snap["boards"]
                                                       else None)
        h = health_from_fields(loads(ev["fields"])) if ev else {}
        st["board"] = clean(board, 40) if board else None
        st["health"] = ({"t": ev["t_wall"], "raw_ts": clean(ev["raw_ts"], 12),
                         "line": clean(loads(ev["fields"]).get("line"), 200), "n": h.get("n")}
                        if ev else None)
        vdd = h.get("vdd")
        st["vdd"] = round(vdd / 1000.0, 3) if vdd is not None else None
        st["vdd_low"] = vdd is not None and vdd < VDD_MIN_MV
        st["temp"] = h.get("temp")
        st["stale"] = ev is None or ev["t_wall"] is None or st["now"] - ev["t_wall"] > STALE_S

        devices = {}
        for name in ORDER:
            hdr, mcu = DEVICES[name]
            if name in LINKS:
                s = h.get(name) or "UNKNOWN"
            elif name == "estop":
                s = h.get("estop") or "UNKNOWN"
            else:
                s = "UNKNOWN" if vdd is None else ("LOW" if vdd < VDD_MIN_MV else "OK")
            devices[name] = {"state": s, "pins": hdr, "mcu": mcu, "incident": None}

        open_incs = [i for i in snap["open"] if board is None or i["board"] == board]
        tickets = self._tickets([i["id"] for i in open_incs])
        heads, lines = [], []
        for inc in open_incs:  # newest first
            alerts = snap["alerts"].get(inc["id"], [])
            t0 = _t0(inc)
            t_alert = inc.get("t_alert_sent") or _sent(alerts, "open")
            dev = incident_device(inc)
            ticket = inc.get("ticket") or tickets.get(inc["id"])
            info = {"id": inc["id"], "kind": inc["kind"], "state": inc["state"],
                    "triage": inc["triage"], "alert_s": _since(t_alert, t0),
                    "ticket": clean(ticket, 40) if ticket else None, "device": dev}
            if dev and devices[dev]["incident"] is None:
                devices[dev]["incident"] = info
                if devices[dev]["state"] in ("OK", "OFF", "UNKNOWN"):
                    devices[dev]["state_note"] = "incident open"
            heads.append(self._head(inc, dev, devices, snap["causes"].get(inc["id"])))
            dstate = devices[dev]["state"] if dev else None
            f = loads(inc.get("evidence")).get("fields")
            if dev in LINKS and isinstance(f, dict) and f.get(dev) in ("OPEN", "STUCK"):
                dstate = f[dev]
            lines.append((incident_headline(inc, dev, dstate, board),
                          headline_sub(inc, info["alert_s"], dev)))
        st["devices"] = devices
        if heads:
            st["banner"] = {"level": "bad", "text": heads[0]["text"],
                            "causes": heads[0]["causes"], "incident": open_incs[0]["id"],
                            "more": len(heads) - 1}
            (lvl, txt), sub = lines[0]
            if len(lines) > 1:
                sub += " \u00b7 %d more open" % (len(lines) - 1)
            st["headline"] = {"level": lvl, "text": txt, "sub": sub}
        else:
            bad = [n for n in ORDER if devices[n]["state"] in ("OPEN", "STUCK", "ON", "LOW")]
            if bad:
                st["banner"] = {"level": "warn", "text": "CHECKING: " + ", ".join(
                    "%s %s" % (n, devices[n]["state"]) for n in bad), "causes": []}
            else:
                st["banner"] = {"level": "ok", "text": "ALL CLEAR", "causes": []}
            st["headline"] = self._quiet_headline(bad, devices, board, st["stale"])

        ls = snap["last_sent"]
        st["telegram"] = ({"text": clean(ls["text"].replace("\n", " | "), 600),
                           "sent": ls["sent"], "incident": ls["incident_id"]} if ls else None)
        newest = (open_incs or [i for i in snap["recent"]
                                if board is None or i["board"] == board] or [None])[0]
        if newest:
            alerts = snap["alerts"].get(newest["id"], [])
            t0 = _t0(newest)
            st["stopwatch"] = {
                "incident": newest["id"], "state": newest["state"], "t0": t0,
                "raw_t0": time.strftime("%H:%M:%S", time.gmtime(t0)) if t0 else None,
                "alert_s": _since(newest.get("t_alert_sent") or _sent(alerts, "open"), t0),
                "explain_s": _since(_sent(alerts, "explain"), t0),
                "fix_s": _since(_sent(alerts, "fix"), t0),
                "ticket": None, "explain": None}
            tk = newest.get("ticket") or self._tickets([newest["id"]]).get(newest["id"])
            st["stopwatch"]["ticket"] = clean(str(tk), 40).lstrip("#") if tk else None
            ex = None
            for a in alerts:
                if a.get("transition") == "explain":
                    ex = first_sentence(a.get("text"))
            if not ex:
                e = loads(newest.get("evidence")).get("explanation")
                ex = first_sentence(e) if isinstance(e, str) else None
            st["stopwatch"]["explain"] = ex
        else:
            st["stopwatch"] = None
        return st

    def _quiet_headline(self, bad, devices, board, stale):
        if stale:
            return {"level": "unknown", "text": WAIT_HEAD,
                    "sub": "No news from the board for more than %d s" % STALE_S}
        name = board_name(board)
        if bad:
            n = bad[0]
            s = devices[n]["state"]
            if n in LINKS:
                what = "the %s looks %s" % (WIRE_WORDS[n], "stuck" if s == "STUCK" else "loose")
            elif n == "estop":
                what = "the stop button is pressed"
            else:
                what = "the power looks low"
            return {"level": "warn", "text": "\U0001f7e1 %s: %s. Checking\u2026" % (name, what),
                    "sub": "No alert yet \u00b7 watching it"}
        return {"level": "ok", "text": OK_HEAD, "sub": "%s \u00b7 all parts checked" % name}

    def _tickets(self, ids):
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        out = {}
        for r in self.db.query("SELECT incident_id, ticket FROM routing WHERE incident_id IN "
                               "(%s) AND ticket IS NOT NULL ORDER BY id" % marks, ids):
            out[r["incident_id"]] = r["ticket"]
        return out

    def _head(self, inc, dev, devices, causes):
        tri = inc["triage"]
        ev = loads(inc.get("evidence"))
        if dev in LINKS:
            s = devices[dev]["state"]
            f = ev.get("fields") if isinstance(ev.get("fields"), dict) else {}
            if f.get(dev) in ("OPEN", "STUCK"):
                s = f[dev]
            word = s if s in ("OPEN", "STUCK") else "FAULT"
            text = "%s: %s link %s (%s)" % (tri, dev, word, DEVICES[dev][0])
        elif dev == "estop":
            text = "%s: estop pressed (B1)" % tri
        elif dev == "power":
            text = "%s: VDD low" % tri
        elif inc["kind"] == "HARDFAULT":
            fl = ev.get("file_line")
            text = "%s: HardFault at %s" % (tri, clean(fl, 80)) if fl else "%s: HardFault" % tri
        else:
            text = "%s: %s" % (tri, KIND_WORDS.get(inc["kind"], clean(inc["kind"], 20)))
        if not causes and inc["kind"] == "HARDFAULT":
            bits = ev.get("cfsr_bits")
            causes = [clean(b, 30) for b in bits if isinstance(b, str)] if isinstance(
                bits, list) else []
        return {"text": clean(text, 160), "causes": causes or []}


def load_page():
    with open(PAGE_FILE, encoding="utf-8") as f:
        return f.read()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "labwatch-dashboard"

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", self.server.page.encode("utf-8"))
        elif path == "/state.json":
            body = json.dumps(self.server.dash.state(), sort_keys=True).encode("ascii")
            self._send(200, "application/json", body)
        else:
            self._send(404, "text/plain", b"not found\n")

    do_HEAD = do_GET

    def log_message(self, fmt, *args):
        pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, dash):
        self.dash = dash
        self.page = load_page()
        super().__init__(addr, Handler)


def make_server(db, host="127.0.0.1", port=8770, board=None):
    return Server((host, port), Dashboard(db, board))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m bench.dashboard",
                                 description="Visual board dashboard for the GB10 monitor.")
    ap.add_argument("--db", help="bench DB, opened read-only (default $LW_DB or "
                                 "~/.local/state/labwatch/bench.db)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--board", help="board name (default: the newest HEALTH line's board)")
    args = ap.parse_args(argv)
    srv = make_server(args.db, args.host, args.port, args.board)
    print("dashboard on http://%s:%d/ (db %s)" % (args.host, srv.server_address[1],
                                                   srv.dash.db.path), flush=True)
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
