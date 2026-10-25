"""Append a live scenario to a bench DB over a few seconds, as the bench would.

    python3 -m stub.emit --db /tmp/lw-stub.db --scenario jumper|hardfault|flap|recover

Prints the new incident id. Lanes B and C watch GET /events?since=<cursor>.
"""

import argparse
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stub import scenarios  # noqa: E402
from stub.db import Writer, connect, init  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--scenario", required=True, choices=sorted(scenarios.SCENARIOS))
    ap.add_argument("--speed", type=float, default=0.5,
                    help="sleep factor per simulated second (0 = no sleeping)")
    args = ap.parse_args(argv)
    conn = connect(args.db)
    init(conn)
    try:
        iid = scenarios.SCENARIOS[args.scenario](Writer(conn), scenarios.RealClock(args.speed))
    except LookupError as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(iid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
