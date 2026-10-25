"""Send a photo of the board drawing as a reply to each incident's first alert.

    python3 -m bench.snapshot [--db PATH] [--url URL] [--once]
    LW_TG_API=https://api.telegram.org:8443 python3 -m bench.snapshot   (on the GB10)

Polls bench.db read-only every second for sent 'open' alerts with no photo yet,
waits 1.5 s so the dashboard shows the red state, screenshots the dashboard
(LW_SNAPSHOT_CMD, a JSON argv with {url}, {out} and optional {profile}; default
headless Firefox with its own temp profile) and posts it with sendPhoto as a
reply to the alert, with a one-line plain caption. Photos sent (or skipped) are
kept in $LW_STATE_DIR/snapshots.db so a restart never resends. LINK_DOWN is
skipped (the board is gone). The token is read from the secrets dir, used only
in the request URL, and never printed or put in argv.
"""

import argparse
import http.client
import http.server
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bench import store  # noqa: E402
from bench.dashboard import incident_device  # noqa: E402

DEFAULT_API = "https://api.telegram.org"
DEFAULT_SECRETS = "~/.config/labwatch"
DEFAULT_STATE = "~/.local/state/labwatch"
DEFAULT_URL = "http://127.0.0.1:8770/"
DEFAULT_CMD = ["firefox", "--headless", "--no-remote", "--profile", "{profile}",
               "--window-size=1280,800", "--screenshot", "{out}", "{url}"]
INTERVAL_S = 1.0
DELAY_S = 1.5
SHOT_TIMEOUT_S = 30.0
STARTUP_SKIP_S = 600.0
GIVE_UP_S = 300.0
HTTP_TIMEOUT_S = 30.0
BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)
SETTLE_S = 1.5
SETTLE_PATH = "/__lw_settle.png"
PROXY_PATHS = ("/", "/index.html", "/state.json")   # all the dashboard page loads
SKIP_KINDS = ("LINK_DOWN",)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
TINY_PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0bIDATx\x9cc\xf8\x0f"
            b"\x00\x01\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82")

WIRES = {"loopback": "the test wire (A0–A1)", "sensor": "the sensor wire (A2–A3)",
         "radio": "the radio wire (A4–A5)"}
WIRE_STATES = {"OPEN": "is disconnected", "STUCK": "is shorted to ground"}
KIND_PLAIN = {"ESTOP": "the emergency stop button is pressed",
              "HARDFAULT": "the firmware crashed",
              "SILENT": "the board has gone silent",
              "REBOOT": "the board rebooted unexpectedly",
              "RUN_FAIL": "a test run failed",
              "HEALTH_FAIL": "a health check is failing"}

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    alert_id    INTEGER PRIMARY KEY,
    incident_id INTEGER,
    status      TEXT NOT NULL,   -- sent | skipped | failed
    reason      TEXT,
    message_id  INTEGER,
    t           REAL NOT NULL
);
"""


def _loads(s):
    try:
        v = json.loads(s or "{}")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def board_label(board):
    """'nucleo-1' -> 'Board 1'; anything without a trailing number stays as is."""
    board = board or "the board"
    tail = board.rsplit("-", 1)[-1]
    return "Board %s" % tail if tail.isdigit() else board


def caption(inc):
    """One plain-English line for a non-technical reader."""
    inc = dict(inc)
    kind = (inc.get("kind") or "").upper()
    ev = _loads(inc.get("evidence"))
    dev = incident_device(inc)
    fields = ev.get("fields") if isinstance(ev.get("fields"), dict) else {}
    what = None
    if dev in WIRES:
        state = fields.get(dev) if fields.get(dev) in WIRE_STATES else None
        if state is None and kind in WIRE_STATES:
            state = kind
        what = "%s %s" % (WIRES[dev], WIRE_STATES.get(state, "is not working"))
    elif dev == "estop" or kind == "ESTOP":
        what = KIND_PLAIN["ESTOP"]
    elif dev == "power" or ev.get("device") in ("vdd", "power"):
        what = "the power supply is low"
    elif kind in KIND_PLAIN:
        what = KIND_PLAIN[kind]
    else:
        what = "there is a problem: %s" % (kind.replace("_", " ").lower() or "unknown")
    return "%s: %s (incident #%d)" % (board_label(inc.get("board")), what, inc["id"])


def _read_secret(path):
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


class SendError(Exception):
    pass


class Telegram:
    def __init__(self, api_base, token_path, chat_path, timeout=HTTP_TIMEOUT_S):
        self.api_base = api_base.rstrip("/")
        self.token_path = token_path
        self.chat_path = chat_path
        self.timeout = timeout

    def send_photo(self, png, caption_text, reply_to):
        """telegram message_id, or SendError (never carrying the URL or token)."""
        try:
            token = _read_secret(self.token_path)
            chat = _read_secret(self.chat_path)
        except OSError:
            raise SendError("the telegram_token or telegram_chat file cannot be read")
        if not token or not chat:
            raise SendError("the telegram_token or telegram_chat file is empty")
        body, ctype = multipart({"chat_id": chat, "reply_to_message_id": str(reply_to),
                                 "caption": caption_text},
                                ("photo", "board.png", "image/png", png))
        req = urllib.request.Request("%s/bot%s/sendPhoto" % (self.api_base, token), data=body,
                                     headers={"Content-Type": ctype}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                reply = json.loads(r.read(65536).decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            code = e.code
            e.close()
            raise SendError("HTTP %d" % code)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise SendError("unreachable (%s)" % e.__class__.__name__)
        if not isinstance(reply, dict) or not reply.get("ok"):
            raise SendError("Telegram answered ok=false")
        try:
            return int(reply["result"]["message_id"])
        except (KeyError, TypeError, ValueError):
            raise SendError("Telegram answer has no message_id")


def multipart(fields, file_part):
    """(body bytes, content type) for multipart/form-data with one file."""
    boundary = "lw-" + uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n"
                    % (boundary, k)).encode("utf-8"))
        out.append(v.encode("utf-8") + b"\r\n")
    name, filename, ctype, data = file_part
    out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                "Content-Type: %s\r\n\r\n" % (boundary, name, filename, ctype)).encode("utf-8"))
    out.append(data + b"\r\n")
    out.append(("--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(out), "multipart/form-data; boundary=%s" % boundary


class SettleProxy:
    """Serves the dashboard through 127.0.0.1 with one hidden image that answers
    after SETTLE_S, so Firefox's --screenshot (taken at the load event) waits until
    the page's own state.json poll has drawn the board. The dashboard is untouched."""

    def __init__(self, target, settle=SETTLE_S):
        t = urllib.parse.urlsplit(target)
        if t.scheme not in ("http", "https") or not t.hostname:
            raise ValueError("dashboard URL must be http(s)://host[:port]/")
        self.scheme, self.host, self.port = t.scheme, t.hostname, t.port
        self.prefix = t.path.rstrip("/")
        proxy = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, ctype, body):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                req = urllib.parse.urlsplit(self.path)
                path = req.path
                if req.scheme or req.netloc or not path.startswith("/") or ".." in path:
                    return self._send(400, "text/plain", b"bad path\n")
                if path == SETTLE_PATH:
                    time.sleep(settle)
                    return self._send(200, "image/png", TINY_PNG)
                if path not in PROXY_PATHS:
                    return self._send(404, "text/plain", b"not found\n")
                try:
                    code, ctype, body = proxy.fetch(path)
                except (OSError, http.client.HTTPException):
                    return self._send(502, "text/plain", b"dashboard not answering\n")
                if code != 200:
                    return self._send(code, "text/plain", b"upstream error\n")
                if path in ("/", "/index.html") and b"</body>" in body:
                    tag = ('<img src="%s" alt="" style="position:absolute;width:1px;'
                           'height:1px;opacity:0">' % SETTLE_PATH.lstrip("/")).encode()
                    body = body.replace(b"</body>", tag + b"</body>", 1)
                self._send(200, ctype, body)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.url = "http://127.0.0.1:%d/" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def fetch(self, path):
        """(status, content type, body) of one allowed path on the fixed dashboard host."""
        cls = (http.client.HTTPSConnection if self.scheme == "https"
               else http.client.HTTPConnection)
        conn = cls(self.host, self.port, timeout=10)
        try:
            conn.request("GET", self.prefix + path)
            r = conn.getresponse()
            return r.status, r.getheader("Content-Type", "application/octet-stream"), r.read()
        finally:
            conn.close()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def screenshot(cmd, url, timeout=SHOT_TIMEOUT_S, settle=SETTLE_S):
    """PNG bytes of the page (after it has drawn its state), or RuntimeError."""
    proxy = SettleProxy(url, settle)
    try:
        return _shoot(cmd, proxy.url, timeout)
    finally:
        proxy.close()


def _shoot(cmd, url, timeout):
    work = tempfile.mkdtemp(prefix="lw-snap-")
    try:
        out = os.path.join(work, "board.png")
        profile = os.path.join(work, "profile")
        os.mkdir(profile)
        argv = [a.replace("{url}", url).replace("{out}", out).replace("{profile}", profile)
                for a in cmd]
        try:
            p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, timeout=timeout)
        except FileNotFoundError:
            raise RuntimeError("screenshot command not found: %s" % argv[0])
        except subprocess.TimeoutExpired:
            raise RuntimeError("screenshot timed out after %.0f s" % timeout)
        try:
            with open(out, "rb") as f:
                data = f.read()
        except OSError:
            err = p.stderr.decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError("screenshot wrote no file (exit %d%s)"
                               % (p.returncode, ": " + err[-1][:200] if err else ""))
        if not data.startswith(PNG_MAGIC):
            raise RuntimeError("screenshot is not a PNG (%d bytes)" % len(data))
        return data
    finally:
        shutil.rmtree(work, ignore_errors=True)


def open_bench_ro(path):
    conn = sqlite3.connect("file:%s?mode=ro" % store.db_path(path), uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def open_state(state_dir):
    os.makedirs(state_dir, exist_ok=True)
    conn = sqlite3.connect(os.path.join(state_dir, "snapshots.db"), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(STATE_SCHEMA)
    conn.commit()
    return conn


class Snapshotter:
    def __init__(self, bench, state, telegram, cmd=None, url=DEFAULT_URL, out=None,
                 clock=time.time, delay=DELAY_S, give_up=GIVE_UP_S,
                 startup_skip=STARTUP_SKIP_S, shot_timeout=SHOT_TIMEOUT_S,
                 settle=SETTLE_S):
        self.bench = bench
        self.state = state
        self.tg = telegram
        self.cmd = cmd or DEFAULT_CMD
        self.url = url
        self.out = out or sys.stdout
        self.clock = clock
        self.delay = delay
        self.give_up = give_up
        self.shot_timeout = shot_timeout
        self.settle = settle
        self.started = clock()
        self.startup_skip = startup_skip
        self.pending = {}   # alert_id -> {"png", "first", "next", "tries"}
        self.shot_s = []

    def _say(self, msg):
        print("snapshot: %s" % msg, file=self.out, flush=True)

    def _done(self, alert_id):
        return self.state.execute("SELECT 1 FROM photos WHERE alert_id=?",
                                  (alert_id,)).fetchone() is not None

    def _record(self, row, status, reason=None, message_id=None):
        self.state.execute("INSERT OR IGNORE INTO photos (alert_id, incident_id, status, reason, "
                           "message_id, t) VALUES (?, ?, ?, ?, ?, ?)",
                           (row["alert_id"], row["incident_id"], status, reason, message_id,
                            self.clock()))
        self.state.commit()
        self.pending.pop(row["alert_id"], None)
        self._say("incident #%d alert %d: %s%s" % (row["incident_id"], row["alert_id"], status,
                                                   " (%s)" % reason if reason else ""))

    def candidates(self):
        return self.bench.execute(
            "SELECT a.id AS alert_id, a.incident_id, a.sent, a.telegram_message_id, "
            "i.id, i.kind, i.board, i.evidence FROM alerts a JOIN incidents i "
            "ON i.id = a.incident_id WHERE a.transition='open' AND a.sent IS NOT NULL "
            "AND a.telegram_message_id IS NOT NULL ORDER BY a.id").fetchall()

    def step(self):
        now = self.clock()
        for row in self.candidates():
            if self._done(row["alert_id"]):
                continue
            if (row["kind"] or "").upper() in SKIP_KINDS:
                self._record(row, "skipped", "%s: the board is gone" % row["kind"])
                continue
            if row["sent"] < self.started - self.startup_skip:
                self._record(row, "skipped", "older than %.0f min at startup"
                             % (self.startup_skip / 60))
                continue
            if now < row["sent"] + self.delay:
                continue
            p = self.pending.get(row["alert_id"])
            if p is None:
                t0 = time.monotonic()
                try:
                    png = screenshot(self.cmd, self.url, self.shot_timeout, self.settle)
                except RuntimeError as e:
                    self._record(row, "failed", str(e))
                    continue
                self.shot_s.append(time.monotonic() - t0)
                self._say("screenshot for incident #%d took %.1f s"
                          % (row["incident_id"], self.shot_s[-1]))
                p = self.pending[row["alert_id"]] = {"png": png, "first": now, "next": now,
                                                     "tries": 0}
            if now < p["next"]:
                continue
            try:
                mid = self.tg.send_photo(p["png"], caption(row), row["telegram_message_id"])
            except SendError as e:
                p["tries"] += 1
                if self.clock() - p["first"] >= self.give_up:
                    self._record(row, "failed", "gave up after %.0f s: %s" % (self.give_up, e))
                else:
                    p["next"] = self.clock() + BACKOFF[min(p["tries"] - 1, len(BACKOFF) - 1)]
                    self._say("incident #%d: send failed (%s), retry #%d"
                              % (row["incident_id"], e, p["tries"]))
                continue
            self._record(row, "sent", message_id=mid)

    def loop(self, interval=INTERVAL_S, stop=None):
        while stop is None or not stop():
            try:
                self.step()
            except sqlite3.Error as e:
                self._say("bench DB not readable (%s); retrying" % e.__class__.__name__)
            time.sleep(interval)


def snapshot_cmd():
    raw = os.environ.get("LW_SNAPSHOT_CMD")
    if not raw:
        return list(DEFAULT_CMD)
    cmd = json.loads(raw)
    if not isinstance(cmd, list) or not cmd or not all(isinstance(a, str) for a in cmd):
        raise ValueError("LW_SNAPSHOT_CMD must be a JSON list of strings")
    return cmd


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m bench.snapshot")
    ap.add_argument("--db", help="bench DB (default $LW_DB or %s)" % store.DEFAULT_DB)
    ap.add_argument("--url", default=DEFAULT_URL, help="dashboard URL (default %(default)s)")
    ap.add_argument("--secrets-dir", help="default $LW_SECRETS_DIR or %s" % DEFAULT_SECRETS)
    ap.add_argument("--state-dir", help="default $LW_STATE_DIR or %s" % DEFAULT_STATE)
    ap.add_argument("--interval", type=float, default=INTERVAL_S)
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    a = ap.parse_args(argv)
    secrets = os.path.expanduser(a.secrets_dir or os.environ.get("LW_SECRETS_DIR")
                                 or DEFAULT_SECRETS)
    state_dir = os.path.expanduser(a.state_dir or os.environ.get("LW_STATE_DIR")
                                   or DEFAULT_STATE)
    api = os.environ.get("LW_TG_API") or DEFAULT_API
    try:
        cmd = snapshot_cmd()
    except ValueError as e:
        print("snapshot: %s" % e, file=sys.stderr)
        return 2
    tg = Telegram(api, os.path.join(secrets, "telegram_token"),
                  os.path.join(secrets, "telegram_chat"))
    s = Snapshotter(open_bench_ro(a.db), open_state(state_dir), tg, cmd=cmd, url=a.url)
    print("snapshot: watching %s every %.1f s, photos to %s" % (store.db_path(a.db), a.interval,
                                                                api), flush=True)
    if a.once:
        s.step()
        return 0
    try:
        s.loop(a.interval)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
