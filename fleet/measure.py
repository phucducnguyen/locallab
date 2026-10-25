"""Step the fleet up and measure what the host carries.

    python3 -m fleet.measure --elf <x.sim.elf> --steps 1,4,8,12 --seconds 120 \
        --state-dir ~/lw-fleet/measure --out ~/lw-fleet/results [--icount SHIFT]

For each step: a baseline of the host with no boards, then fleet.run with N
boards (--monitor), a soak with --reset and host stats, then the fleet is
stopped. It stops early when idle CPU during a step falls below --min-idle
percent, and never goes past --max-boards. One JSON per step plus
summary.json in --out.
"""

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import time

from fleet import hoststat, soak

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def wait_fleet(state_dir, n, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with open(os.path.join(state_dir, "boards.json")) as f:
                d = json.load(f)
            if (d["fleet"]["state"] == "running" and len(d["boards"]) == n
                    and all(b["pid"] for b in d["boards"])):
                return d
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.2)
    raise RuntimeError("fleet of %d did not come up" % n)


def step(elf, n, seconds, state_dir, icount, baseline_s, qemu=None):
    base = hoststat.Sampler()
    time.sleep(baseline_s)
    base = base.finish()
    cmd = [sys.executable, "-m", "fleet.run", "--elf", elf, "--boards", str(n),
           "--state-dir", state_dir, "--monitor"]
    if icount is not None:
        cmd += ["--icount", icount]
    if qemu:
        cmd += ["--qemu", qemu]
    p = subprocess.Popen(cmd, cwd=HERE)
    try:
        wait_fleet(state_dir, n)
        time.sleep(2)
        with contextlib.redirect_stdout(sys.stderr):
            res = soak.soak(state_dir, seconds, reset=True, host_stats=True)
    finally:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(20)
        except subprocess.TimeoutExpired:
            p.kill()
    res["baseline"] = base
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--elf", required=True)
    ap.add_argument("--steps", default="1,4,8,12")
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--baseline", type=float, default=15)
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--icount", default=None)
    ap.add_argument("--min-idle", type=float, default=25.0)
    ap.add_argument("--max-boards", type=int, default=14)
    ap.add_argument("--qemu", default=None, help="passed to fleet.run (a fake on the laptop)")
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    summary = []
    for n in [int(x) for x in a.steps.split(",")]:
        if n > a.max_boards:
            print("skip %d: over --max-boards %d" % (n, a.max_boards), file=sys.stderr)
            break
        res = step(a.elf, n, a.seconds, a.state_dir, a.icount, a.baseline, a.qemu)
        with open(os.path.join(a.out, "step-%02d.json" % n), "w") as f:
            json.dump(res, f, indent=1)
        h, b = res.get("host", {}), res["baseline"]
        row = {"boards": n, "cpu_busy_pct": h.get("cpu_busy_pct"),
               "baseline_busy_pct": b.get("cpu_busy_pct"),
               "cores_busy": h.get("cores_busy"), "boards_cores": h.get("boards_cores"),
               "per_board_core_max": h.get("per_board_core_max"),
               "boards_rss_mib": h.get("boards_rss_mib"), "mem_avail_mib": h.get("mem_avail_mib"),
               "health_per_s_mean": res["health_per_s_mean"],
               "health_per_s_min": min(r["health_per_s"] for r in res["per_board"]),
               "health_per_s_max": max(r["health_per_s"] for r in res["per_board"]),
               "anomalies": res["anomalies"],
               "anomaly_list": [r["board"] + ": " + "; ".join(r["anomalies"])
                                for r in res["per_board"] if r["anomalies"]]}
        summary.append(row)
        print(json.dumps(row), flush=True)
        with open(os.path.join(a.out, "summary.json"), "w") as f:
            json.dump(summary, f, indent=1)
        idle = 100.0 - (h.get("cpu_busy_pct") or 0)
        if idle < a.min_idle:
            print("stop: idle %.1f%% < %.1f%% at %d boards" % (idle, a.min_idle, n),
                  file=sys.stderr)
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
