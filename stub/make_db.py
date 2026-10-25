"""Build a stub bench DB with canned records.

    python3 -m stub.make_db --out /tmp/lw-stub.db

Records, in time order on 2026-10-25 (UTC): a passing run of 1111111, (a) a
pulled jumper and (c) its recovery, (d) a flap, (b) a HardFault run of 2222222.
"""

import argparse
import calendar
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stub import scenarios  # noqa: E402
from stub.db import Writer, connect, init  # noqa: E402

T_BASE = calendar.timegm((2026, 10, 25, 8, 30, 0, 0, 0, 0))


def build(path):
    if os.path.exists(path):
        raise FileExistsError(path)
    conn = connect(path)
    init(conn)
    w = Writer(conn)
    scenarios.passing_run(w, scenarios.FakeClock(T_BASE))
    clock = scenarios.FakeClock(T_BASE + 30 * 60)
    jumper = scenarios.jumper(w, clock)
    clock.wait(6.0)
    scenarios.recover(w, clock, jumper)
    scenarios.flap(w, scenarios.FakeClock(T_BASE + 50 * 60))
    scenarios.hardfault(w, scenarios.FakeClock(T_BASE + 70 * 60))
    conn.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, help="path of the new DB (must not exist)")
    args = ap.parse_args(argv)
    try:
        build(args.out)
    except FileExistsError:
        print("refusing to overwrite %s" % args.out, file=sys.stderr)
        return 1
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
