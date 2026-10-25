"""python3 -m agent.fix --incident <id>: the fix beat. The logic lives in tracker/fix.py."""

import sys

from tracker.fix import main

if __name__ == "__main__":
    sys.exit(main(prog="python3 -m agent.fix"))
