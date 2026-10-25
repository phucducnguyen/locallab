"""Telegram commands for the bench: python3 -m bench.commands [--dry-run] [--once]

A separate small process that waits for our instructions. It long-polls
getUpdates through $LW_TG_API (default https://api.telegram.org; on the GB10
the laptop relay https://api.telegram.org:8443) and answers only:

    /status          device states, open incidents and the last run (bench DB, read-only)
    /test <name>     POST /board/test on the bench endpoint (crc32 ringbuf systick
                     cmd_dispatch loopback); replies "queued" or the reason
    /rerun           POST /run with the bench's verified sha; replies "queued" or the reason
    /ask <question>  one sandbox-agent turn (agent.explain's config), 60 s, one at a time
    /help            this list

Only messages from the configured chat (telegram_chat) whose text starts
with "/" are read; with $LW_TG_USERS (comma-separated user ids) only those
users. Everything else is ignored with one log line that never holds the
message text. Deterministic code picks every action: the model only answers
/ask. It never flashes, edits files, calls git or talks to the board; its
only side effects are the endpoint's two rate-limited routes. At most one
command per 3 s per user; extra ones get one "slow down" per 30 s.

The getUpdates offset is kept in <state dir>/telegram_offset (the directory
of the bench DB unless --state-dir / $LW_STATE_DIR), written before each
update is handled, so a restart never replays a command. Commands older than
120 s when first seen are skipped too.

Each /ask is kept in a small SQLite file, <directory of the bench DB>/asks.db
($LW_ASKS_DB overrides), WAL mode, busy_timeout 5000, for bench/dashboard.py
to show the latest question and answer (it opens the file read-only):

    CREATE TABLE asks (
        id          INTEGER PRIMARY KEY,
        t_asked     REAL NOT NULL,   -- unix time the question was queued
        question    TEXT NOT NULL,   -- as it went to the agent; "/ask 5" stays "/ask 5"
        incident_id INTEGER,         -- 5 for "/ask 5", else NULL
        answer      TEXT,            -- the text replied: condensed answer, timeout or failure
        t_answered  REAL,            -- unix time of the answer, NULL while asked
        status      TEXT NOT NULL    -- asked | answered | timeout | failed
    )

A row is inserted when the question is queued and updated when the answer
comes back. It never holds user names, user ids, chat ids or tokens. A DB
error is logged in one line and never stops the command.

Secrets ($LW_SECRETS_DIR, default ~/.config/labwatch/): telegram_token,
telegram_chat, bench_token. They are read from files when needed, never
printed, never logged, never put in argv.
"""

import argparse
import json
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from agent import explain
from bench import requests as bench_requests
from bench import status_data
from bench.sender import SendError, Telegram

DEFAULT_API = "https://api.telegram.org"
DEFAULT_SECRETS = "~/.config/labwatch/"
DEFAULT_ENDPOINT = "http://172.18.0.1:8765"
DEFAULT_DB = "~/.local/state/labwatch/bench.db"
ASKS_DB_NAME = "asks.db"
ASKS_SCHEMA = ("CREATE TABLE IF NOT EXISTS asks (id INTEGER PRIMARY KEY, t_asked REAL NOT NULL, "
               "question TEXT NOT NULL, incident_id INTEGER, answer TEXT, t_answered REAL, "
               "status TEXT NOT NULL)")
POLL_TIMEOUT_S = 25
ASK_TIMEOUT_S = 60.0
REPLY_TRIES = 5
REPLY_RETRY_S = 2.0
ASK_QUEUE = 3
QUESTION_MAX = 300
ANSWER_LINES = 8
LINE_MAX = 200
USER_INTERVAL_S = 3.0
SLOW_DOWN_EVERY_S = 30.0
STALE_S = 120.0
ERROR_BACKOFF_S = 5.0
SELF_TESTS = bench_requests.SELF_TESTS
ASK_PROMPT = ("Answer this question from the bench's records using the labwatch skill, cite "
              "every claim, and say 'I can't tell' if the records don't decide: ")

HELP = "\n".join([
    "Local Lab commands:",
    "/status - devices, open incidents, last run",
    "/test <name> - queue a self-test: %s" % " ".join(SELF_TESTS),
    "/rerun - rerun the verified sha (one per 60 s)",
    "/ask <question> - ask the bench agent (60 s, one at a time)",
    "/help - this list",
])

_MD_RE = re.compile(r"\*+|`+|(?<!\w)_+|_+(?!\w)|^#+\s*|^>\s*")
_CMD_RE = re.compile(r"^/([A-Za-z_]+)(?:@\S+)?(?:\s+(.*))?$", re.S)


def _read_secret(path):
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def printable(text, limit):
    text = " ".join(str(text or "").split())
    return "".join(c for c in text if c.isprintable())[:limit]


def age(seconds):
    s = max(0, int(seconds))
    if s < 120:
        return "%ds" % s
    if s < 7200:
        return "%dm" % (s // 60)
    if s < 172800:
        return "%dh" % (s // 3600)
    return "%dd" % (s // 86400)


def condense(text, lines=ANSWER_LINES):
    """The agent's answer as at most `lines` plain lines (markdown stripped)."""
    out = []
    for ln in (text or "").splitlines():
        ln = _MD_RE.sub("", ln.strip()).strip()
        if ln.startswith(("- ", "+ ")):
            ln = "- " + ln[2:].strip()
        ln = printable(ln, LINE_MAX)
        if ln and set(ln) - set("-=| "):
            out.append(ln)
    if len(out) > lines:
        out = out[:lines - 1] + ["..."]
    return "\n".join(out)


# ---------- /status, from the bench DB read-only ----------

def _health_from_event(fields):
    """The parsed event's own keys first; the raw line fills what they lack."""
    h = {k: fields.get(k) for k in ("loopback", "sensor", "radio", "estop", "vdd", "temp")}
    parsed = status_data.parse_health(fields.get("line")) if fields.get("line") else None
    for k, v in (parsed or {}).items():
        if h.get(k) is None and v is not None:
            h[k] = v
    return h


def status_text(db_path, now):
    try:
        conn = status_data.open_ro(db_path)
    except Exception:
        return "status: the bench DB cannot be read"
    try:
        return _status(conn, now)
    except Exception as e:
        return "status: the bench DB query failed (%s)" % e.__class__.__name__
    finally:
        conn.close()


def _has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def verified_sha(conn):
    if not _has_table(conn, "bench_state"):
        return None
    r = conn.execute("SELECT verified_sha FROM bench_state WHERE verified_sha IS NOT NULL "
                     "ORDER BY t DESC LIMIT 1").fetchone()
    return r[0] if r else None


def _status(conn, now):
    lines = []
    ev = conn.execute("SELECT board, t_wall, fields FROM events WHERE kind='HEALTH' "
                      "ORDER BY id DESC LIMIT 1").fetchone()
    if ev is None:
        lines.append("devices: no HEALTH line yet")
    else:
        h = _health_from_event(status_data.loads(ev["fields"]))
        parts = ["%s %s" % (k, h[k]) for k in ("loopback", "sensor", "radio", "estop")
                 if h.get(k) is not None]
        if h.get("vdd") is not None:
            parts.append("vdd %s mV" % h["vdd"])
        if h.get("temp") is not None:
            parts.append("temp %s C" % h["temp"])
        lines.append(printable("%s: %s (HEALTH %s ago)" % (
            ev["board"], ", ".join(parts) or "no fields", age(now - ev["t_wall"])), 300))
    sha = verified_sha(conn)
    if sha:
        lines.append("verified sha: %s" % printable(sha, 12))
    incs = conn.execute("SELECT id, kind, board, triage, opened, state FROM incidents "
                        "WHERE state != 'RESOLVED' ORDER BY id DESC LIMIT 6").fetchall()
    if not incs:
        lines.append("open incidents: none")
    else:
        lines.append("open incidents: %d" % len(incs))
        for r in incs[:5]:
            lines.append(printable("#%d %s %s %s %s, %s" % (
                r["id"], r["kind"], r["board"], r["triage"], r["state"],
                age(now - r["opened"])), 200))
    run = conn.execute('SELECT run_id, "commit", verdict, t_start FROM runs '
                       "ORDER BY run_id DESC LIMIT 1").fetchone()
    if run is None:
        lines.append("last run: none")
    else:
        lines.append(printable("last run %d: %s %s, %s ago" % (
            run["run_id"], (run["commit"] or "unverified")[:12], run["verdict"],
            age(now - run["t_start"])), 200))
    return "\n".join(lines[:10])


# ---------- the bot ----------

class Bot:
    def __init__(self, api, secrets_dir, db_path, endpoint, state_dir, agent_config=None,
                 users=None, dry_run=False, out=None, clock=time.time,
                 poll_timeout=POLL_TIMEOUT_S, ask_timeout=ASK_TIMEOUT_S, asks_db=None):
        self.api = api.rstrip("/")
        self.secrets = secrets_dir
        self.db_path = db_path
        self.asks_db = (asks_db or os.environ.get("LW_ASKS_DB")
                        or os.path.join(os.path.dirname(os.path.abspath(db_path)), ASKS_DB_NAME))
        self.endpoint = endpoint.rstrip("/")
        self.offset_path = os.path.join(state_dir, "telegram_offset")
        self.agent_config = agent_config
        self.users = users
        self.dry_run = dry_run
        self.out = out or sys.stdout
        self.clock = clock
        self.poll_timeout = poll_timeout
        self.ask_timeout = ask_timeout
        self.tg = Telegram(self.api, os.path.join(secrets_dir, "telegram_token"),
                           os.path.join(secrets_dir, "telegram_chat"))
        self._lock = threading.Lock()
        self._last_cmd = {}      # user id -> time of the last accepted command
        self._last_slow = {}     # user id -> time of the last "slow down"
        self.asks = queue.Queue(maxsize=ASK_QUEUE)
        self._worker = None
        os.makedirs(state_dir, exist_ok=True)

    def log(self, msg):
        with self._lock:
            print(msg, file=self.out, flush=True)

    # --- offset ---
    def load_offset(self):
        try:
            with open(self.offset_path, encoding="utf-8") as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def save_offset(self, offset):
        tmp = self.offset_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("%d\n" % offset)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.offset_path)

    # --- Telegram ---
    def get_updates(self):
        try:
            token = _read_secret(os.path.join(self.secrets, "telegram_token"))
        except OSError:
            raise SendError("the telegram_token file cannot be read")
        if not token:
            raise SendError("the telegram_token file is empty")
        body = {"timeout": self.poll_timeout, "allowed_updates": ["message"]}
        off = self.load_offset()
        if off is not None:
            body["offset"] = off
        req = urllib.request.Request("%s/bot%s/getUpdates" % (self.api, token),
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.poll_timeout + 10) as r:
                reply = json.loads(r.read(4 * 1024 * 1024).decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            code = e.code
            e.close()
            raise SendError("HTTP %d" % code)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise SendError("unreachable (%s)" % e.__class__.__name__)
        if not isinstance(reply, dict) or not reply.get("ok") or \
                not isinstance(reply.get("result"), list):
            raise SendError("Telegram answered ok=false")
        return reply["result"]

    def reply(self, message_id, text):
        if self.dry_run:
            self.log("would reply to %s:\n%s" % (message_id, text))
            return
        # The relay to Telegram rides a phone tether and drops for seconds at a time.
        for attempt in range(REPLY_TRIES):
            try:
                self.tg.send_message(text, message_id)
                return
            except SendError as e:
                self.log("commands: reply to %s not sent (try %d): %s"
                         % (message_id, attempt + 1, e.what))
                if attempt + 1 < REPLY_TRIES:
                    time.sleep(REPLY_RETRY_S * (attempt + 1))

    # --- one update ---
    def chat_id(self):
        try:
            return _read_secret(os.path.join(self.secrets, "telegram_chat"))
        except OSError:
            return None

    def handle_update(self, u):
        uid = u.get("update_id")
        msg = u.get("message") if isinstance(u.get("message"), dict) else {}
        chat = msg.get("chat") if isinstance(msg.get("chat"), dict) else {}
        cid = chat.get("id")
        sender = msg.get("from") if isinstance(msg.get("from"), dict) else {}
        user = sender.get("id")
        text = msg.get("text") if isinstance(msg.get("text"), str) else ""
        want = self.chat_id()
        ok = (want is not None and cid is not None and str(cid) == want
              and text.startswith("/") and not sender.get("is_bot")
              and (self.users is None or str(user) in self.users))
        if not ok:
            self.log("ignored update %s from chat %s" % (printable(uid, 20), printable(cid, 30)))
            return
        mid = msg.get("message_id")
        date = msg.get("date")
        now = self.clock()
        if isinstance(date, (int, float)) and now - date > STALE_S:
            self.log("ignored update %s from chat %s: older than %d s" % (uid, cid, STALE_S))
            return
        last = self._last_cmd.get(user)
        if last is not None and now - last < USER_INTERVAL_S:
            slow = self._last_slow.get(user)
            if slow is None or now - slow >= SLOW_DOWN_EVERY_S:
                self._last_slow[user] = now
                self.reply(mid, "slow down: one command per %d s" % USER_INTERVAL_S)
            self.log("commands: update %s rate-limited (user %s)" % (uid, user))
            return
        self._last_cmd[user] = now
        m = _CMD_RE.match(text.strip())
        name = m.group(1).lower() if m else ""
        arg = (m.group(2) or "").strip() if m else ""
        self.log("commands: update %s user %s /%s" % (uid, user, printable(name, 20)))
        if name == "status":
            self.reply(mid, status_text(self.db_path, now))
        elif name == "test":
            self.reply(mid, self.cmd_test(arg))
        elif name == "rerun":
            self.reply(mid, self.cmd_rerun())
        elif name == "ask":
            self.cmd_ask(mid, arg)
        elif name in ("help", "start"):
            self.reply(mid, HELP)
        else:
            self.reply(mid, "unknown command; /help")

    # --- endpoint ---
    def post(self, path, body):
        """(status or None, response dict)."""
        try:
            token = _read_secret(os.path.join(self.secrets, "bench_token"))
        except OSError:
            return None, {"error": "the bench_token file cannot be read"}
        req = urllib.request.Request(self.endpoint + path,
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + token},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, _dict(r.read(65536))
        except urllib.error.HTTPError as e:
            try:
                return e.code, _dict(e.read(65536))
            finally:
                e.close()
        except (urllib.error.URLError, OSError, ValueError) as e:
            return None, {"error": "endpoint unreachable (%s)" % e.__class__.__name__}

    @staticmethod
    def _outcome(what, status, resp):
        if status in (200, 202):
            rid = resp.get("request_id")
            return "queued: %s%s" % (what, " (request %s)" % printable(rid, 12)
                                     if rid is not None else "")
        reason = printable(resp.get("error") or "no reason given", 120)
        extra = resp.get("retry_after_s")
        if extra is not None:
            reason += ", retry in %s s" % printable(extra, 10)
        if status is None:
            return "not queued: %s" % reason
        return "not queued (%d): %s" % (status, reason)

    def cmd_test(self, arg):
        name = arg.split()[0].lower() if arg.split() else ""
        if name not in SELF_TESTS:
            return "usage: /test <name>, one of: %s" % " ".join(SELF_TESTS)
        status, resp = self.post("/board/test", {"name": name})
        return self._outcome("test %s" % name, status, resp)

    def cmd_rerun(self):
        try:
            conn = status_data.open_ro(self.db_path)
            try:
                sha = verified_sha(conn)
            finally:
                conn.close()
        except Exception:
            return "not queued: the bench DB cannot be read"
        if not sha or not re.fullmatch(r"[0-9a-f]{7,40}", sha):
            return "not queued: the bench has no verified sha"
        status, resp = self.post("/run", {"sha": sha})
        return self._outcome("run %s" % sha[:12], status, resp)

    # --- the asks DB (for the dashboard; never in the way of a reply) ---
    def _asks_conn(self):
        conn = sqlite3.connect(self.asks_db, timeout=5.0)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(ASKS_SCHEMA)
        except Exception:
            conn.close()
            raise
        return conn

    def record_asked(self, question, incident_id):
        """The new row's id, or None when the asks DB cannot be written."""
        try:
            conn = self._asks_conn()
            try:
                with conn:
                    cur = conn.execute("INSERT INTO asks (t_asked, question, incident_id, status) "
                                       "VALUES (?, ?, ?, 'asked')",
                                       (self.clock(), question, incident_id))
                return cur.lastrowid
            finally:
                conn.close()
        except Exception as e:
            self.log("commands: asks DB not written (%s)" % e.__class__.__name__)
            return None

    def record_answer(self, ask_id, status, answer):
        if ask_id is None:
            return
        try:
            conn = self._asks_conn()
            try:
                with conn:
                    conn.execute("UPDATE asks SET answer=?, t_answered=?, status=? WHERE id=?",
                                 (answer, self.clock(), status, ask_id))
            finally:
                conn.close()
        except Exception as e:
            self.log("commands: asks DB not written (%s)" % e.__class__.__name__)

    def _forget_ask(self, ask_id):
        if ask_id is None:
            return
        try:
            conn = self._asks_conn()
            try:
                with conn:
                    conn.execute("DELETE FROM asks WHERE id=?", (ask_id,))
            finally:
                conn.close()
        except Exception as e:
            self.log("commands: asks DB not written (%s)" % e.__class__.__name__)

    # --- /ask ---
    def cmd_ask(self, mid, arg):
        m = re.fullmatch(r"\s*#?(\d{1,6})\s*", arg or "")
        incident_id = None
        if m:  # "/ask 5" means incident 5, as the alerts say
            incident_id = int(m.group(1))
            arg = ("What happened in incident %s, is it the hardware or the code, and what "
                   "should we do next? Answer in at most 4 short plain sentences." % m.group(1))
        question = printable(arg, QUESTION_MAX)
        if not question:
            self.reply(mid, "usage: /ask <question>")
            return
        if self.asks.full():
            self.reply(mid, "busy: %d questions already waiting" % ASK_QUEUE)
            return
        shown = "/ask %d" % incident_id if incident_id is not None else question
        ask_id = self.record_asked(shown, incident_id)
        try:
            self.asks.put_nowait((mid, question, ask_id))
        except queue.Full:
            self._forget_ask(ask_id)
            self.reply(mid, "busy: %d questions already waiting" % ASK_QUEUE)
            return
        self._start_worker()

    def _start_worker(self):
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._ask_loop, daemon=True)
            self._worker.start()

    def _ask_loop(self):
        while True:
            mid, question, ask_id = self.asks.get()
            try:
                try:
                    status, text = self._ask(question)
                except Exception as e:
                    self.log("commands: /ask failed (%s)" % e.__class__.__name__)
                    self.record_answer(ask_id, "failed",
                                       "ask: failed (%s)" % e.__class__.__name__)
                    continue
                self.record_answer(ask_id, status, text)
                self.reply(mid, text)
            except Exception as e:
                self.log("commands: /ask failed (%s)" % e.__class__.__name__)
            finally:
                self.asks.task_done()

    def ask(self, question):
        return self._ask(question)[1]

    def _ask(self, question):
        """(status, reply text); status is answered, timeout or failed."""
        try:
            cfg = explain.load_config(self.agent_config)
        except explain.ConfigError as e:
            return "failed", "ask: the agent is not configured (%s)" % printable(str(e), 120)
        argv, message, stdin_message = build_ask_command(cfg, question)
        self.log("commands: /ask in a fresh agent session")
        try:
            rc, raw = explain.run_agent(argv, message, self.ask_timeout, stdin_message)
        except OSError as e:
            return "failed", "ask: cannot start the agent (%s)" % e.__class__.__name__
        if rc is None:
            return "timeout", "ask: no answer within %.0f s" % self.ask_timeout
        answer = condense(explain.clean_reply(raw or b""))
        if rc != 0:
            return "failed", "ask: the agent failed (exit %d)%s" % (
                rc, "\n" + answer if answer else "")
        if not answer:
            return "failed", "ask: the agent gave an empty answer"
        return "answered", answer

    # --- loop ---
    def step(self):
        n = 0
        for u in self.get_updates():
            uid = u.get("update_id") if isinstance(u, dict) else None
            if not isinstance(uid, int):
                continue
            off = self.load_offset()
            if off is not None and uid < off:
                continue
            self.save_offset(uid + 1)   # before handling: a restart never replays it
            self.handle_update(u)
            n += 1
        return n

    def loop(self, stop=None):
        said = None
        while stop is None or not stop.is_set():
            try:
                self.step()
                said = None
            except SendError as e:
                if e.what != said:
                    self.log("commands: getUpdates failed: %s; retrying" % e.what)
                    said = e.what
                time.sleep(ERROR_BACKOFF_S)
            except Exception as e:
                self.log("commands: pass failed (%s)" % e.__class__.__name__)
                time.sleep(ERROR_BACKOFF_S)


def _dict(raw):
    try:
        v = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def build_ask_command(cfg, question, session=None):
    """(argv, message, stdin_message) for one /ask turn in a fresh session.

    The question is data inside the prompt; it never becomes an argv token of its own.
    """
    session = session or "%sask-%s" % (cfg["session_prefix"], uuid.uuid4().hex)
    message = ASK_PROMPT + '"' + printable(question, QUESTION_MAX).replace('"', "'") + '"'
    values = {"kind": "ask", "id": "question", "session": session}
    argv = []
    for t in cfg["agent_cmd"]:
        t = explain.fill(t, values)
        argv.append(t.replace("{message}", message))
    stdin_message = not any("{message}" in t for t in cfg["agent_cmd"])
    return argv, message, stdin_message


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m bench.commands",
                                 description="Answer Telegram commands for the bench")
    ap.add_argument("--db", help="bench DB (default $LW_DB or %s)" % DEFAULT_DB)
    ap.add_argument("--secrets-dir", help="default $LW_SECRETS_DIR or %s" % DEFAULT_SECRETS)
    ap.add_argument("--state-dir", help="where telegram_offset lives (default $LW_STATE_DIR "
                    "or the DB's directory)")
    ap.add_argument("--agent-config", help="default $LW_AGENT_CONFIG")
    ap.add_argument("--dry-run", action="store_true", help="print replies instead of sending")
    ap.add_argument("--once", action="store_true", help="one getUpdates pass, then exit")
    a = ap.parse_args(argv)
    secrets = os.path.expanduser(a.secrets_dir or os.environ.get("LW_SECRETS_DIR")
                                 or DEFAULT_SECRETS)
    db = os.path.expanduser(a.db or os.environ.get("LW_DB") or DEFAULT_DB)
    state = os.path.expanduser(a.state_dir or os.environ.get("LW_STATE_DIR")
                               or os.path.dirname(os.path.abspath(db)))
    api = os.environ.get("LW_TG_API") or DEFAULT_API
    endpoint = os.environ.get("LW_ENDPOINT") or DEFAULT_ENDPOINT
    users = os.environ.get("LW_TG_USERS")
    users = {u.strip() for u in users.split(",") if u.strip()} if users else None
    bot = Bot(api, secrets, db, endpoint, state, agent_config=a.agent_config, users=users,
              dry_run=a.dry_run)
    print("commands: polling %s%s; endpoint %s; users %s" % (
        api, " (dry run)" if a.dry_run else "", endpoint,
        "any in the chat" if users is None else len(users)), flush=True)
    if a.once:
        try:
            bot.step()
        except SendError as e:
            print("commands: getUpdates failed: %s" % e.what, flush=True)
            return 2
        bot.asks.join()
        return 0
    try:
        bot.loop()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
