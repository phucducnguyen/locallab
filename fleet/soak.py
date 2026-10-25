"""Soak-test the bench's serial code against every board in boards.json.

    python3 -m fleet.soak --state-dir ~/lw-fleet/run --seconds 120 \
        [--reset] [--json out.json] [--host-stats]

Each board gets its own bench.serial_owner.SerialOwner (the bench's code, used
as is) on the board's stable link. Per board it counts LW records by kind,
non-LW lines, unreadable bytes, whether BOOT and RESULT were seen, HEALTH
lines per wall-clock second (mean, min, max over whole seconds), and every
gap of more than 3 s between LW lines (the bench would call that SILENT).

--reset presses each board's RESET through its QEMU monitor (fleet.run
--monitor) after the ports are open, so the BOOT, TEST and RESULT lines are
seen; it also makes a missing BOOT or RESULT an anomaly.

Anomalies (exit 1 when any): a gap > 3 s, unreadable bytes, a malformed LW
record, a non-LW line after the first HEALTH, HEALTH not OK, a HARDFAULT,
RESULT FAIL or TEST FAIL, an ALIVE/HEALTH counter that goes back without a
BOOT, a link drop, no HEALTH at all, and (with --reset) no BOOT or RESULT.
"""

import argparse
import json
import os
import sys
import time

from bench.serial_owner import SerialOwner

GAP_S = 3.0


class BoardSoak:
    """Counts one board's events. Feed on_event(), then summary(t_end)."""

    def __init__(self, name, t_start_mono, t_start_wall, expect_boot=False):
        self.name = name
        self.t0 = t_start_mono
        self.w0 = t_start_wall
        self.expect_boot = expect_boot
        self.by_kind = {}
        self.lw_lines = 0
        self.nonlw = 0
        self.nonlw_steady = 0
        self.nonlw_pending = 0  # non-LW lines after HEALTH, judged by the next record
        self.bad_bytes = 0
        self.malformed = 0
        self.boot = 0
        self.result = None
        self.fails = []
        self.health_bad = 0
        self.health_per_sec = {}
        self.health = 0
        self.last_lw = None
        self.gaps = []
        self.link_down = 0
        self.link_up_t = None
        self.last_n = None
        self.counter_back = 0
        self.seen_health = False
        self.fw = None
        self.first_line_t = None

    def on_event(self, ev):
        if ev.kind == "LINK_DOWN":
            if self.link_up_t is not None:
                self.link_down += 1
            return
        if ev.kind == "LINK_UP":
            self.link_up_t = ev.t_mono
            return
        if ev.kind != "LINE":
            return
        if ev.t_mono < self.t0:
            return
        self.bad_bytes += ev.bad_bytes
        if not ev.records:
            if ev.nonlw:
                self.nonlw += 1
                if self.seen_health:
                    self.nonlw_pending += 1
            return
        self.lw_lines += 1
        if self.first_line_t is None:
            self.first_line_t = ev.t_mono
        self._gap(ev.t_mono)
        if self.nonlw_pending:
            # free text right before a BOOT is the boot banner, not noise
            if ev.records[0]["kind"] != "BOOT":
                self.nonlw_steady += self.nonlw_pending
            self.nonlw_pending = 0
        for r in ev.records:
            k = r["kind"]
            self.by_kind[k] = self.by_kind.get(k, 0) + 1
            if r.get("malformed") or k == "OTHER":
                self.malformed += 1
            if k == "BOOT":
                self.boot += 1
                self.fw = r.get("fw")
                self.last_n = None
                self.seen_health = False
            elif k == "RESULT":
                self.result = r.get("status")
                if r.get("status") != "PASS":
                    self.fails.append(r["line"][:80])
            elif k == "TEST" and r.get("status") == "FAIL":
                self.fails.append(r["line"][:80])
            elif k == "HARDFAULT":
                self.fails.append(r["line"][:80])
            elif k == "HEALTH":
                self.health += 1
                self.seen_health = True
                sec = int(ev.t_wall - self.w0)
                self.health_per_sec[sec] = self.health_per_sec.get(sec, 0) + 1
                if r.get("loopback") != "OK":
                    self.health_bad += 1
                n = r.get("n")
                if n is not None:
                    if self.last_n is not None and n < self.last_n:
                        self.counter_back += 1
                    self.last_n = n

    def _gap(self, t):
        prev = self.last_lw if self.last_lw is not None else self.t0
        if t - prev > GAP_S:
            self.gaps.append({"after_s": round(prev - self.t0, 2), "gap_s": round(t - prev, 2)})
        self.last_lw = t

    def summary(self, t_end_mono):
        dur = max(1e-6, t_end_mono - self.t0)
        # a silence at the end counts too
        prev = self.last_lw if self.last_lw is not None else self.t0
        gaps = list(self.gaps)
        if t_end_mono - prev > GAP_S:
            gaps.append({"after_s": round(prev - self.t0, 2), "gap_s": round(t_end_mono - prev, 2),
                         "open": True})
        whole = int(dur)  # whole seconds only, for min/max
        per = [self.health_per_sec.get(s, 0) for s in range(whole)]
        anomalies = []
        if gaps:
            anomalies.append("gap>3s x%d (max %.1fs)" % (len(gaps), max(g["gap_s"] for g in gaps)))
        if self.bad_bytes:
            anomalies.append("unreadable bytes %d" % self.bad_bytes)
        if self.malformed:
            anomalies.append("malformed LW %d" % self.malformed)
        steady = self.nonlw_steady + self.nonlw_pending
        if steady:
            anomalies.append("non-LW lines after HEALTH %d" % steady)
        if self.health_bad:
            anomalies.append("HEALTH not OK %d" % self.health_bad)
        if self.fails:
            anomalies.append("fail: %s" % self.fails[0])
        if self.counter_back:
            anomalies.append("counter went back %d" % self.counter_back)
        if self.link_down:
            anomalies.append("link down %d" % self.link_down)
        if not self.health:
            anomalies.append("no HEALTH")
        if self.expect_boot and not self.boot:
            anomalies.append("no BOOT")
        if self.expect_boot and self.result is None:
            anomalies.append("no RESULT")
        return {
            "board": self.name, "seconds": round(dur, 1), "fw": self.fw,
            "by_kind": dict(sorted(self.by_kind.items())), "lw_lines": self.lw_lines,
            "nonlw_lines": self.nonlw, "bad_bytes": self.bad_bytes,
            "boot_seen": self.boot, "result": self.result,
            "health": self.health,
            "health_per_s": round(self.health / dur, 2),
            "health_per_s_min": min(per) if per else None,
            "health_per_s_max": max(per) if per else None,
            "gaps": gaps, "link_down": self.link_down,
            "anomalies": anomalies,
        }


def load_boards(state_dir):
    with open(os.path.join(state_dir, "boards.json")) as f:
        data = json.load(f)
    return data


def table(rows):
    head = ("board", "fw", "HEALTH", "/s", "min", "max", "ALIVE", "BOOT", "RESULT",
            "nonLW", "bad", "gaps", "anomalies")
    out = ["%-7s %-8s %7s %6s %4s %4s %7s %4s %6s %5s %4s %4s  %s" % head]
    for r in rows:
        out.append("%-7s %-8s %7d %6.2f %4s %4s %7d %4d %6s %5d %4d %4d  %s" % (
            r["board"], (r["fw"] or "-")[:8], r["health"], r["health_per_s"],
            r["health_per_s_min"], r["health_per_s_max"], r["by_kind"].get("ALIVE", 0),
            r["boot_seen"], r["result"] or "-", r["nonlw_lines"], r["bad_bytes"],
            len(r["gaps"]), "; ".join(r["anomalies"]) or "none"))
    return "\n".join(out)


def soak(state_dir, seconds, reset=False, host_stats=False, owner_cls=SerialOwner,
         out=sys.stdout):
    data = load_boards(state_dir)
    boards = [b for b in data["boards"] if b.get("emulated") is True]
    if not boards:
        raise SystemExit("no emulated boards in boards.json")
    t0, w0 = time.monotonic(), time.time()
    soaks, owners = [], []
    for b in boards:
        s = BoardSoak(b["board"], t0, w0, expect_boot=reset)
        o = owner_cls(port=b.get("link") or b["pty"], on_event=s.on_event,
                      reopen_interval=0.2)
        soaks.append(s)
        owners.append(o.start())
    sampler = None
    try:
        if reset:
            from fleet.run import reset_board
            end = time.monotonic() + 5
            while time.monotonic() < end and not all(o.link_up for o in owners):
                time.sleep(0.05)
            for b in boards:
                if not b.get("monitor"):
                    raise SystemExit("--reset needs a fleet started with --monitor")
            t0, w0 = time.monotonic(), time.time()
            for s in soaks:  # count from the reset on
                s.__init__(s.name, t0, w0, expect_boot=True)
                s.link_up_t = t0
            for b in boards:
                reset_board(b["monitor"])
        else:
            for s in soaks:
                s.link_up_t = t0
        if host_stats:
            from fleet.hoststat import Sampler
            sampler = Sampler([b.get("pid") for b in boards])
        time.sleep(seconds)
        t_end = time.monotonic()
    finally:
        for o in owners:
            o.stop()
    rows = [s.summary(t_end) for s in soaks]
    result = {"state_dir": state_dir, "seconds": seconds, "boards": len(rows),
              "icount": data.get("fleet", {}).get("icount"),
              "elf": data.get("fleet", {}).get("elf"),
              "started": w0, "per_board": rows,
              "anomalies": sum(len(r["anomalies"]) for r in rows)}
    if sampler:
        result["host"] = sampler.finish()
    rates = [r["health_per_s"] for r in rows]
    result["health_per_s_mean"] = round(sum(rates) / len(rates), 2)
    print(table(rows), file=out)
    h = result.get("host")
    if h:
        print("host: %s%% busy (%s of %s cores), boards %s cores, RSS %s MiB, avail %s MiB" % (
            h.get("cpu_busy_pct"), h.get("cores_busy"), h.get("ncpu"), h.get("boards_cores"),
            h.get("boards_rss_mib"), h.get("mem_avail_mib")), file=out)
    print("boards %d, HEALTH/s mean %.2f, anomalies %d" % (
        len(rows), result["health_per_s_mean"], result["anomalies"]), file=out)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--host-stats", action="store_true")
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    try:
        import serial  # noqa: F401
    except ImportError:
        print("pyserial is needed", file=sys.stderr)
        return 2
    res = soak(a.state_dir, a.seconds, a.reset, a.host_stats)
    if a.json:
        tmp = a.json + ".tmp"
        with open(tmp, "w") as f:
            json.dump(res, f, indent=1)
        os.replace(tmp, a.json)
    return 1 if res["anomalies"] else 0


if __name__ == "__main__":
    sys.exit(main())
