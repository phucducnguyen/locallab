"""Pair a fault's one-time "LW DUMP" burst with the next LW HARDFAULT line.

Pure: no I/O, no clock. Feed every parsed record (bench.parser.parse_record)
in arrival order with its wall time. feed() returns a dump dict only for a
HARDFAULT record that has a fresh burst in front of it, else None.

Rules (firmware grammar v2):
- A burst is reg, up to 8 stack lines (or one "stack bad sp="), cause, end,
  printed once before the first HARDFAULT line of a fault.
- It belongs to the next HARDFAULT. A burst with no HARDFAULT within
  timeout_s (3 s) of its last DUMP line is discarded.
- The HARDFAULT line repeats every second; repeats get None (burst used up).
- A reconnect may lose the burst, so a HARDFAULT with no burst gets None and
  the HARDFAULT line stays the authority.
- A BOOT record (board reset) discards any pending burst; the next fault
  prints a new one.
- "complete" is True only for reg (all 8 registers), at least one stack line
  or a bad sp, cause and end, with no malformed or extra lines.
"""

TIMEOUT_S = 3.0
MAX_STACK_LINES = 8


class DumpCollector:
    def __init__(self, timeout_s=TIMEOUT_S):
        self.timeout_s = timeout_s
        self.burst = None
        self.discarded = 0  # bursts dropped (timeout, reset, or a new burst)

    def _new(self, t):
        if self.burst is not None:
            self.discarded += 1
        self.burst = {"t_first": t, "t_last": t, "regs": None, "regs_partial": False,
                      "stack": [], "bad_sp": None, "cause": None, "ended": False,
                      "flaws": 0, "cites": []}
        return self.burst

    def expire(self, t):
        """Drop a burst whose last line is older than timeout_s."""
        b = self.burst
        if b is not None and t - b["t_last"] > self.timeout_s:
            self.burst = None
            self.discarded += 1

    def feed(self, t, rec, cite=None):
        self.expire(t)
        kind = rec.get("kind")
        if kind == "DUMP":
            self._dump_line(t, rec, cite)
            return None
        if kind == "BOOT":
            if self.burst is not None:
                self.burst = None
                self.discarded += 1
            return None
        if kind == "HARDFAULT":
            b, self.burst = self.burst, None
            return self._result(b) if b is not None else None
        return None

    def _dump_line(self, t, rec, cite):
        sub = rec.get("sub")
        b = self.burst
        # A reg line after any other line opens a new burst. A burst seen from
        # the middle (reconnect) is kept but incomplete. A non-reg line after
        # "end" only marks the finished burst as flawed.
        if b is not None and b["ended"] and sub != "reg":
            b["flaws"] += 1
            return
        if b is None or b["ended"] or (sub == "reg" and (b["regs"] is not None
                                                         or b["stack"] or b["bad_sp"]
                                                         or b["cause"] is not None)):
            b = self._new(t)
        b["t_last"] = t
        if cite is not None:
            b["cites"].append(cite)
        if sub == "reg":
            b["regs"] = dict(rec.get("regs") or {})
            b["regs_partial"] = bool(rec.get("partial"))
        elif sub == "stack":
            if rec.get("bad_sp") is not None:
                if b["bad_sp"] is not None or b["stack"]:
                    b["flaws"] += 1
                b["bad_sp"] = rec["bad_sp"]
            elif len(b["stack"]) >= MAX_STACK_LINES:
                b["flaws"] += 1
            else:
                b["stack"].append((rec.get("addr"), list(rec.get("words") or [])))
        elif sub == "cause":
            if b["cause"] is not None:
                b["flaws"] += 1
            b["cause"] = list(rec.get("names") or [])
            if rec.get("unknown"):
                b["flaws"] += 1
        elif sub == "end":
            b["ended"] = True
        else:  # malformed
            b["flaws"] += 1

    @staticmethod
    def _result(b):
        complete = (b["regs"] is not None and not b["regs_partial"]
                    and (bool(b["stack"]) or b["bad_sp"] is not None)
                    and b["cause"] is not None and b["ended"] and b["flaws"] == 0)
        return {
            "registers": b["regs"] or {},
            "stack": list(b["stack"]),
            "stack_bad_sp": b["bad_sp"],
            "cause_names": b["cause"],  # None: no cause line; []: the board said NONE
            "complete": complete,
            "cites": list(b["cites"]),
        }
