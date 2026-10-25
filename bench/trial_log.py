"""One line per scored trial, read from the bench DB only.

    python3 -m bench.trial_log --incident <id> [--db PATH] [--header]
    python3 -m bench.trial_log --since <unix time>      (every incident opened since then)

Prints: incident id, kind, bench triage, t_first_bad_line, first alert seconds, explanation
seconds, the agent's DIAGNOSIS line (or "no explanation yet").
"""

import argparse
import os
import re
import sqlite3
import sys
import time

DEFAULT_DB = os.environ.get("LW_DB", os.path.expanduser("~/.local/state/labwatch/bench.db"))
DIAG_RE = re.compile(r"^DIAGNOSIS:.*$", re.M)
HEADER = "incident\tkind\ttriage\tt_first_bad_line\talert_s\texplain_s\tdiagnosis"


def connect(path):
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=5000")
    return c


def utc(t):
    if t is None:
        return "-"
    return time.strftime("%H:%M:%S", time.gmtime(t)) + ".%03d" % int((t % 1) * 1000)


def secs(a, b):
    return "-" if a is None or b is None else "%.2f" % (a - b)


def line(c, iid):
    inc = c.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()
    if inc is None:
        return "%s\tno such incident" % iid
    exp = c.execute("SELECT text, sent FROM alerts WHERE incident_id=? AND transition='explain'",
                    (iid,)).fetchone()
    diag, t_exp = "no explanation yet", None
    if exp is not None:
        t_exp = exp["sent"]
        m = DIAG_RE.findall(exp["text"] or "")
        diag = m[-1].strip() if m else (exp["text"] or "").strip().splitlines()[-1][:100]
    return "\t".join([str(iid), inc["kind"], inc["triage"], utc(inc["t_first_bad_line"]),
                      secs(inc["t_alert_sent"], inc["t_first_bad_line"]),
                      secs(t_exp, inc["t_first_bad_line"]), diag])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--incident", type=int)
    g.add_argument("--since", type=float, help="unix time; every incident opened since then")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--header", action="store_true")
    a = ap.parse_args(argv)
    c = connect(a.db)
    if a.header:
        print(HEADER)
    if a.incident is not None:
        print(line(c, a.incident))
    else:
        for r in c.execute("SELECT id FROM incidents WHERE opened >= ? ORDER BY id", (a.since,)):
            print(line(c, r["id"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
