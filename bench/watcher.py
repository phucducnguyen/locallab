"""The repo watcher: python3 -m bench.watcher [--db PATH] [--repo PATH] [--interval 5]

Polls main of the bench repo ($LW_FW_REPO, ~/bench-fw) every 5 s. Any change
of the head, a new commit or a move back, queues one bench_requests row
"run <new head>" (source 'watcher') for the bench to flash and run. The last
head seen is kept in watcher_state, so a restart triggers only on a real
change. On its very first start it records the head and triggers nothing.

It ignores the hold file on purpose: the accuracy loop holds the endpoint's
side-effect routes, but its case commits reach the board through this
watcher. Git text is data; the watcher reads only the head sha.
"""

import argparse
import sys
import time

from bench import fwrepo, requests, store

INTERVAL_S = 5.0

WATCHER_SCHEMA = """
CREATE TABLE IF NOT EXISTS watcher_state (
    repo  TEXT PRIMARY KEY,
    head  TEXT NOT NULL,
    t     REAL NOT NULL
);
"""


class Watcher:
    def __init__(self, conn, repo, clock=time.time, log=print):
        self.conn = conn
        self.repo = repo
        self.clock = clock
        self.log = log
        self._git_ok = True
        conn.executescript(WATCHER_SCHEMA)
        requests.init(conn)

    def last_head(self):
        r = self.conn.execute("SELECT head FROM watcher_state WHERE repo=?",
                              (self.repo,)).fetchone()
        return r["head"] if r else None

    def poll(self):
        """One poll; the bench_requests id it queued, or None."""
        head = fwrepo.main_head(self.repo)
        now = self.clock()
        if head is None:
            if self._git_ok:
                self.log("watcher: cannot read main of %s; will keep trying" % self.repo)
            self._git_ok = False
            return None
        self._git_ok = True
        prev = self.last_head()
        if head == prev:
            return None
        with self.conn:
            self.conn.execute("INSERT INTO watcher_state (repo, head, t) VALUES (?, ?, ?) "
                              "ON CONFLICT(repo) DO UPDATE SET head=excluded.head, t=excluded.t",
                              (self.repo, head, now))
            if prev is None:
                rid = None
            else:
                rid = requests.add(self.conn, now, "watcher", "run", head)
        if rid is None:
            self.log("watcher: main is %s (first start, nothing queued)" % head[:12])
        else:
            self.log("watcher: main moved %s -> %s, run queued (request %d)" % (
                prev[:12], head[:12], rid))
        return rid

    def loop(self, interval=INTERVAL_S, stop=None):
        while stop is None or not stop.is_set():
            t0 = time.monotonic()
            try:
                self.poll()
            except Exception as e:  # a locked DB or similar: keep watching
                self.log("watcher: poll failed (%s)" % e.__class__.__name__)
            delay = max(0.0, interval - (time.monotonic() - t0))
            if stop is not None:
                stop.wait(delay)
            else:
                time.sleep(delay)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m bench.watcher")
    ap.add_argument("--db", help="bench DB (default $LW_DB or %s)" % store.DEFAULT_DB)
    ap.add_argument("--repo", default=fwrepo.repo_path())
    ap.add_argument("--interval", type=float, default=INTERVAL_S)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    conn = store.open_db(a.db)
    w = Watcher(conn, a.repo, log=lambda s: print(s, flush=True))
    print("watcher: polling main of %s every %.0f s" % (a.repo, a.interval), flush=True)
    if a.once:
        w.poll()
        return 0
    try:
        w.loop(a.interval)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
