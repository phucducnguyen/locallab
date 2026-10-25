"""Host load while the fleet runs: CPU from /proc/stat, RAM from /proc/meminfo,
per-process CPU and RSS from /proc/<pid>/stat. Linux only; never raises."""

import os
import time

CLK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def cpu_times():
    """(busy, total) jiffies over all cores, and the number of cores."""
    try:
        with open("/proc/stat") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    ncpu = sum(1 for l in lines if l.startswith("cpu") and l[3:4].isdigit())
    v = [int(x) for x in lines[0].split()[1:]]
    idle = v[3] + (v[4] if len(v) > 4 else 0)  # idle + iowait
    total = sum(v[:8])  # leave out guest columns (already in user)
    return {"busy": total - idle, "total": total, "ncpu": ncpu}


def meminfo():
    out = {}
    try:
        with open("/proc/meminfo") as f:
            for l in f:
                k, _, rest = l.partition(":")
                if k in ("MemTotal", "MemAvailable"):
                    out[k] = int(rest.split()[0]) // 1024  # MiB
    except OSError:
        pass
    return out


def proc_stat(pid):
    """(cpu seconds, rss MiB) of one process, or None."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            s = f.read()
        fields = s[s.rindex(")") + 2:].split()
        cpu = (int(fields[11]) + int(fields[12])) / CLK
        rss = int(fields[21]) * os.sysconf("SC_PAGE_SIZE") / 2**20
        return cpu, rss
    except (OSError, ValueError, IndexError):
        return None


class Sampler:
    """Start, then finish(): busy cores and % over the window, RAM, per-pid CPU."""

    def __init__(self, pids=()):
        self.pids = [p for p in pids if p]
        self.t0 = time.monotonic()
        self.c0 = cpu_times()
        self.p0 = {p: proc_stat(p) for p in self.pids}

    def finish(self):
        dt = time.monotonic() - self.t0
        c1 = cpu_times()
        out = {"window_s": round(dt, 1)}
        if self.c0 and c1 and c1["total"] > self.c0["total"]:
            frac = (c1["busy"] - self.c0["busy"]) / (c1["total"] - self.c0["total"])
            out["cpu_busy_pct"] = round(100 * frac, 1)
            out["cores_busy"] = round(frac * c1["ncpu"], 2)
            out["ncpu"] = c1["ncpu"]
        mem = meminfo()
        if mem:
            out["mem_total_mib"] = mem.get("MemTotal")
            out["mem_avail_mib"] = mem.get("MemAvailable")
        per, rss = [], 0.0
        for p in self.pids:
            a, b = self.p0.get(p), proc_stat(p)
            if a and b and dt > 0:
                per.append((b[0] - a[0]) / dt)
                rss += b[1]
        if per:
            out["boards_cores"] = round(sum(per), 2)
            out["per_board_core_max"] = round(max(per), 2)
            out["boards_rss_mib"] = round(rss, 1)
        return out
