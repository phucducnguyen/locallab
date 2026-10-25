"""Drain the alerts outbox to Telegram: python3 -m bench.sender [--db PATH] [--once]

A separate small process. Every 200 ms it takes the rows with sent IS NULL
(oldest first), prints each one to stdout once, and posts it with
sendMessage to $LW_TG_API (default https://api.telegram.org). A reply row
waits until its parent has a telegram_message_id and then replies to it.
On success it stores telegram_message_id and sent (only where sent IS NULL,
so a sent row is never sent again). If Telegram is unreachable the row stays
unsent and is retried with a short backoff. It never calls getUpdates.

Watcher health (lane A item 10): if the bench's heartbeat is older than 10 s
it posts "⚠️ Local Lab is blind: bench down since T" once per outage. When the
bench has logged LINK_DOWN (the USB port gone) and no LINK_UP for 10 s it
posts "Local Lab is not watching bench-1: link down since T" once, and on
the LINK_UP after it "Local Lab: bench-1 link back after N s" once. Each is
keyed in watcher_posts, so a restarted sender never posts it again.

The token and chat id are read from files in the secrets dir
($LW_SECRETS_DIR, default ~/.config/labwatch/): telegram_token and
telegram_chat. Their contents are never printed or logged; error messages
name the exception class and HTTP status only, never the URL.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

from bench import store
from bench.serial_owner import wall_ts

DEFAULT_API = "https://api.telegram.org"
DEFAULT_SECRETS = "~/.config/labwatch/"
INTERVAL_S = 0.2
HTTP_TIMEOUT_S = 5.0
BLIND_AFTER_S = 10.0
LINK_DOWN_AFTER_S = 10.0
LINK_HISTORY_S = 60.0
BENCH_NAME = "bench-1"
BACKOFF = (0.5, 1.0, 2.0, 5.0)
TEXT_MAX = 4096


class SendError(Exception):
    def __init__(self, what, permanent=False):
        Exception.__init__(self, what)
        self.what = what
        self.permanent = permanent


def _read_secret(path):
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


class Telegram:
    def __init__(self, api_base, token_path, chat_path, timeout=HTTP_TIMEOUT_S):
        self.api_base = api_base.rstrip("/")
        self.token_path = token_path
        self.chat_path = chat_path
        self.timeout = timeout

    def send_message(self, text, reply_to=None):
        """telegram message_id, or SendError (never carrying the URL or token)."""
        try:
            token = _read_secret(self.token_path)
            chat = _read_secret(self.chat_path)
        except OSError:
            raise SendError("the telegram_token or telegram_chat file cannot be read")
        if not token or not chat:
            raise SendError("the telegram_token or telegram_chat file is empty")
        body = {"chat_id": chat, "text": text[:TEXT_MAX], "disable_web_page_preview": True}
        if reply_to:
            body["reply_parameters"] = {"message_id": reply_to,
                                        "allow_sending_without_reply": True}
        req = urllib.request.Request(
            "%s/bot%s/sendMessage" % (self.api_base, token),
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
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


class Sender:
    def __init__(self, conn, telegram, out=None, clock=time.time, blind_after=BLIND_AFTER_S):
        self.conn = conn
        self.tg = telegram
        self.out = out or sys.stdout
        self.clock = clock
        self.blind_after = blind_after
        self._retry = {}   # key -> (next try time, failures)
        self._said = set()

    def _say(self, msg):
        print(msg, file=self.out, flush=True)

    def _due(self, key, now):
        nxt = self._retry.get(key)
        return nxt is None or now >= nxt[0]

    def _failed(self, key, now, err):
        n = self._retry.get(key, (0, 0))[1] + 1
        self._retry[key] = (now + BACKOFF[min(n, len(BACKOFF)) - 1], n)
        msg = "sender: %s not sent yet: %s; will retry" % (key, err.what)
        if (key, err.what) not in self._said:
            self._said.add((key, err.what))
            self._say(msg)

    def _print_alert(self, row, now):
        cur = self.conn.execute("INSERT OR IGNORE INTO alerts_printed (alert_id, t) VALUES (?, ?)",
                                (row["id"], now))
        self.conn.commit()
        if cur.rowcount == 1:
            self._say("ALERT %d (incident %d, %s)\n%s" % (row["id"], row["incident_id"],
                                                         row["transition"], row["text"]))

    def check_heartbeat(self, now):
        hb = self.conn.execute("SELECT t FROM heartbeat WHERE id=1").fetchone()
        if hb is None or now - hb["t"] <= self.blind_after:
            return
        text = "⚠️ Local Lab is blind: bench down since %s UTC" % wall_ts(hb["t"])
        self.conn.execute("INSERT OR IGNORE INTO watcher_posts (key, text, created) "
                          "VALUES (?, ?, ?)", ("blind-%.3f" % hb["t"], text, now))
        self.conn.commit()

    def _watcher_post(self, key, text, now):
        self.conn.execute("INSERT OR IGNORE INTO watcher_posts (key, text, created) "
                          "VALUES (?, ?, ?)", (key, text, now))
        self.conn.commit()

    def check_link(self, now):
        down = self.conn.execute("SELECT id, t_wall FROM events WHERE kind='LINK_DOWN' "
                                 "ORDER BY id DESC LIMIT 1").fetchone()
        if down is None:
            return
        # LINK_UP, or any board line: a bench restarted after the drop logs no LINK_UP
        up = self.conn.execute("SELECT id, t_wall FROM events WHERE id>? AND kind IN "
                               "('LINK_UP','BOOT','ALIVE','HEALTH','RESULT','TEST','REPLY',"
                               "'HARDFAULT') ORDER BY id LIMIT 1", (down["id"],)).fetchone()
        until = now if up is None else up["t_wall"]
        if until - down["t_wall"] < LINK_DOWN_AFTER_S:
            return  # a short drop (or a replug): no watcher post
        key = "linkdown-%d" % down["id"]
        if up is not None and now - up["t_wall"] > LINK_HISTORY_S and not self.conn.execute(
                "SELECT 1 FROM watcher_posts WHERE key=?", (key,)).fetchone():
            return  # an old outage from before this sender ran: history, not news
        self._watcher_post(key,
                           "⚠️ Local Lab is not watching %s: link down since %s UTC"
                           % (BENCH_NAME, wall_ts(down["t_wall"])), now)
        if up is not None:
            self._watcher_post("linkup-%d" % down["id"], "⚠️ Local Lab: %s link back after %d s"
                               % (BENCH_NAME, round(up["t_wall"] - down["t_wall"])), now)

    def step(self):
        """One drain pass; returns the number of messages Telegram accepted."""
        now = self.clock()
        self.check_heartbeat(now)
        self.check_link(now)
        sent = 0
        rows = self.conn.execute("SELECT * FROM alerts WHERE sent IS NULL ORDER BY id").fetchall()
        for row in rows:
            self._print_alert(row, now)
        for row in rows:
            key = "alert %d" % row["id"]
            reply_to = None
            if row["reply_to_alert_id"] is not None:
                parent = self.conn.execute("SELECT sent, telegram_message_id FROM alerts "
                                           "WHERE id=?", (row["reply_to_alert_id"],)).fetchone()
                if parent is not None and parent["sent"] is None:
                    continue  # the first alert goes out first
                reply_to = parent["telegram_message_id"] if parent else None
            if not self._due(key, now):
                continue
            try:
                mid = self.tg.send_message(row["text"], reply_to)
            except SendError as e:
                self._failed(key, now, e)
                if e.what.startswith("unreachable"):
                    break  # everything after it would fail the same way
                continue
            t = self.clock()
            self._retry.pop(key, None)
            with self.conn:
                cur = self.conn.execute("UPDATE alerts SET sent=?, telegram_message_id=? "
                                        "WHERE id=? AND sent IS NULL", (t, mid, row["id"]))
                if cur.rowcount == 1:
                    if row["transition"] == "open":
                        self.conn.execute("UPDATE incidents SET t_alert_sent=? WHERE id=? AND "
                                          "t_alert_sent IS NULL", (t, row["incident_id"]))
                    board = self.conn.execute("SELECT board FROM incidents WHERE id=?",
                                              (row["incident_id"],)).fetchone()
                    store.event(self.conn, t, board["board"] if board else "?", "ALERT_SENT",
                                {"incident_id": row["incident_id"], "alert_id": row["id"],
                                 "transition": row["transition"], "telegram_message_id": mid})
            sent += 1
            self._say("sender: alert %d sent (incident %d, %s)" % (
                row["id"], row["incident_id"], row["transition"]))
        sent += self._watcher_posts(now)
        return sent

    def _watcher_posts(self, now):
        sent = 0
        for row in self.conn.execute("SELECT * FROM watcher_posts WHERE sent IS NULL "
                                     "ORDER BY id").fetchall():
            if row["printed"] is None:
                self._say("WATCHER %s" % row["text"])
                self.conn.execute("UPDATE watcher_posts SET printed=? WHERE id=?",
                                  (now, row["id"]))
                self.conn.commit()
            key = "watcher post %d" % row["id"]
            if not self._due(key, now):
                continue
            try:
                mid = self.tg.send_message(row["text"])
            except SendError as e:
                self._failed(key, now, e)
                continue
            self._retry.pop(key, None)
            self.conn.execute("UPDATE watcher_posts SET sent=?, telegram_message_id=? "
                              "WHERE id=? AND sent IS NULL", (self.clock(), mid, row["id"]))
            self.conn.commit()
            sent += 1
        return sent

    def loop(self, interval=INTERVAL_S, stop=None):
        while stop is None or not stop.is_set():
            t0 = time.monotonic()
            try:
                self.step()
            except Exception as e:  # a locked DB or similar: keep draining
                self._say("sender: pass failed (%s)" % e.__class__.__name__)
            time.sleep(max(0.0, interval - (time.monotonic() - t0)))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m bench.sender")
    ap.add_argument("--db", help="bench DB (default $LW_DB or %s)" % store.DEFAULT_DB)
    ap.add_argument("--secrets-dir", help="default $LW_SECRETS_DIR or %s" % DEFAULT_SECRETS)
    ap.add_argument("--interval", type=float, default=INTERVAL_S)
    ap.add_argument("--once", action="store_true", help="one drain pass, then exit")
    a = ap.parse_args(argv)
    secrets = os.path.expanduser(a.secrets_dir or os.environ.get("LW_SECRETS_DIR")
                                 or DEFAULT_SECRETS)
    api = os.environ.get("LW_TG_API") or DEFAULT_API
    conn = store.open_db(a.db)
    tg = Telegram(api, os.path.join(secrets, "telegram_token"),
                  os.path.join(secrets, "telegram_chat"))
    s = Sender(conn, tg)
    print("sender: draining %s every %.1f s to %s" % (store.db_path(a.db), a.interval, api),
          flush=True)
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
