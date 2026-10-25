"""Print timestamped, parsed board lines: python3 -m bench.tail [--port P]

For the board check. Opens the port through the serial owner (so it must be
the only process on the port), prints one line per record or link change,
and the counters on Ctrl-C. Optional --send queues commands after RESULT and
one ALIVE have been seen.
"""

import argparse
import queue
import sys

from bench import serial_owner


def fmt_record(r):
    k = r["kind"]
    if k == "BOOT":
        s = "BOOT fw=%s reset=%s csr=%s" % (r.get("fw"), ",".join(r.get("reset", [])), r.get("csr"))
    elif k == "TEST":
        s = "TEST %s %s %s" % (r.get("name"), r.get("status"), r.get("detail", ""))
    elif k == "RESULT":
        s = "RESULT %s" % r.get("status")
    elif k == "HARDFAULT":
        regs = " ".join("%s=%s" % (f, r[f]) for f in ("pc", "lr", "psr", "cfsr", "hfsr", "bfar", "mmfar", "sp") if f in r)
        s = "HARDFAULT %s%s" % (regs, " (partial)" if r.get("partial") else "")
    elif k == "ALIVE":
        s = "ALIVE %s" % r.get("n")
    elif k == "HEALTH":
        s = "HEALTH %s loopback=%s" % (r.get("n"), r.get("loopback"))
    elif k == "REPLY":
        s = "REPLY %s %s" % (r.get("cmd"), r.get("text", ""))
    else:
        s = "OTHER %r" % r.get("line", "")[:60]
    if r.get("malformed"):
        s += " (malformed)"
    return s.rstrip()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m bench.tail")
    ap.add_argument("--port", help="serial port (default: first %s)" % serial_owner.DEFAULT_GLOB)
    ap.add_argument("--send", action="append", default=[],
                    help="command to send once RESULT and one ALIVE are seen (repeatable)")
    ap.add_argument("--raw", action="store_true", help="also print each raw line")
    a = ap.parse_args(argv)

    owner = serial_owner.SerialOwner(port=a.port).start()
    pending = list(a.send)
    seen_result = seen_alive = False
    try:
        while True:
            try:
                ev = owner.events.get(timeout=0.5)
            except queue.Empty:
                continue
            ts = "%s mono=%.3f" % (ev.raw_ts, ev.t_mono)
            if ev.kind != "LINE":
                print("%s %s %s" % (ts, ev.kind, ev.detail), flush=True)
                continue
            if a.raw:
                print("%s RAW %r" % (ts, ev.raw), flush=True)
            for r in ev.records:
                print("%s %s" % (ts, fmt_record(r)), flush=True)
                seen_result = seen_result or r["kind"] == "RESULT"
                seen_alive = seen_alive or (seen_result and r["kind"] == "ALIVE")
            if ev.nonlw:
                print("%s TEXT %d chars, %d unreadable bytes" % (ts, len(ev.raw), ev.bad_bytes), flush=True)
            if pending and seen_result and seen_alive:
                for c in pending:
                    ok = owner.send(c)
                    print("%s SEND %r %s" % (ts, c, "queued" if ok else "refused"), flush=True)
                pending = []
    except KeyboardInterrupt:
        pass
    finally:
        owner.stop()
        print("counters: %s" % owner.counters.as_dict(), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
