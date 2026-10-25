"""Run N emulated boards (QEMU netduinoplus2) and keep them running.

    python3 -m fleet.run --elf ~/lw-fleet/elf/<sha>.sim.elf --boards 4 \
        --state-dir ~/lw-fleet/run [--icount SHIFT] [--monitor]

Each board is one qemu-system-arm with USART2 (the second serial) on a pty:

    -M netduinoplus2 -display none -monitor none -serial null -serial pty
    -kernel <elf>

The boards are emu-1..emu-N. <state-dir>/boards.json lists every board (name,
pty, a stable symlink <state-dir>/<name>.tty to that pty, pid, elf,
"emulated": true). A board that dies is started again (its pty path changes;
the symlink follows, like a replug). SIGTERM, SIGINT or SIGHUP (tmux kill-session) stops every board.

--monitor gives each board a QEMU monitor on <state-dir>/<name>.mon (a unix
socket) instead of none, so that reset_board() can press its RESET button.
Only SIM builds (*.sim.elf) are accepted: this never runs a real-board build
and never touches a real port.
"""

import argparse
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time

MACHINE = "netduinoplus2"
PTY_RE = re.compile(r"char device redirected to (\S+) \(label ([^)]+)\)")
PTY_TIMEOUT = 10.0
POLL = 0.5


def qemu_argv(qemu, elf, icount=None, monitor=None):
    argv = list(qemu) + ["-M", MACHINE, "-display", "none",
                         "-monitor", "unix:%s,server=on,wait=off" % monitor if monitor else "none",
                         "-serial", "null", "-serial", "pty", "-kernel", elf]
    if icount is not None and str(icount) != "":
        argv += ["-icount", "shift=%s" % icount]
    return argv


def find_pty(text):
    """The pty path of the second serial (USART2) from QEMU's stderr text."""
    hits = PTY_RE.findall(text)
    for path, label in hits:
        if label == "serial1":
            return path
    return hits[-1][0] if len(hits) == 1 else None


def atomic_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def atomic_symlink(target, link):
    tmp = link + ".tmp"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, link)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reset_board(monitor_path, timeout=3.0):
    """Press RESET on one emulated board through its QEMU monitor socket."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(monitor_path)
        buf = b""
        end = time.monotonic() + timeout
        while b"(qemu)" not in buf and time.monotonic() < end:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.sendall(b"system_reset\n")
        buf = b""
        while b"(qemu)" not in buf and time.monotonic() < end:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return True


class Board:
    def __init__(self, name, state_dir):
        self.name = name
        self.link = os.path.join(state_dir, name + ".tty")
        self.log = os.path.join(state_dir, name + ".log")
        self.monitor = None
        self.proc = None
        self.pty = None
        self.started = None
        self.restarts = 0
        self.fails = 0  # deaths in a row soon after a start
        self.next_try = 0.0

    def as_dict(self, elf):
        d = {"board": self.name, "emulated": True, "elf": elf,
             "pid": self.proc.pid if self.proc and self.proc.poll() is None else None,
             "pty": self.pty, "link": self.link, "log": self.log,
             "restarts": self.restarts, "started": self.started}
        if self.monitor:
            d["monitor"] = self.monitor
        return d


class Fleet:
    def __init__(self, elf, n, state_dir, qemu=("qemu-system-arm",), icount=None,
                 monitor=False, log=sys.stderr):
        self.elf = os.path.abspath(elf)
        self.state_dir = os.path.abspath(state_dir)
        self.qemu = list(qemu)
        self.icount = icount
        self.use_monitor = monitor
        self.logf = log
        self.boards = [Board("emu-%d" % i, self.state_dir) for i in range(1, n + 1)]
        self.stopping = False
        self.started = time.time()

    @property
    def boards_json(self):
        return os.path.join(self.state_dir, "boards.json")

    def say(self, msg):
        if self.logf:
            try:
                print("fleet %s %s" % (time.strftime("%H:%M:%S"), msg), file=self.logf, flush=True)
            except (OSError, ValueError):  # a closed pipe (tee gone) must not stop the stop
                self.logf = None

    def write_state(self, state="running"):
        atomic_json(self.boards_json, {
            "fleet": {"pid": os.getpid(), "state": state, "elf": self.elf,
                      "machine": MACHINE, "icount": self.icount,
                      "started": self.started, "updated": time.time(),
                      "boards": len(self.boards)},
            "boards": [b.as_dict(self.elf) for b in self.boards],
        })

    def start_board(self, b):
        if b.monitor and os.path.exists(b.monitor):
            os.unlink(b.monitor)
        b.monitor = (os.path.join(self.state_dir, b.name + ".mon")
                     if self.use_monitor else None)
        argv = qemu_argv(self.qemu, self.elf, self.icount, b.monitor)
        with open(b.log, "ab") as lf:
            offset = lf.tell()
            lf.write(("--- %s start %s\n" % (time.strftime("%H:%M:%S"),
                                             shlex.join(argv))).encode())
            lf.flush()
            offset = lf.tell()
            b.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=lf,
                                      stderr=lf, start_new_session=True)
        b.started = time.time()
        b.pty = None
        end = time.monotonic() + PTY_TIMEOUT
        while time.monotonic() < end:
            with open(b.log, "rb") as f:
                f.seek(offset)
                pty = find_pty(f.read().decode("utf-8", "replace"))
            if pty:
                b.pty = pty
                break
            if b.proc.poll() is not None:
                break
            time.sleep(0.05)
        if b.pty:
            atomic_symlink(b.pty, b.link)
            self.say("%s up pid=%d pty=%s" % (b.name, b.proc.pid, b.pty))
            return True
        self.say("%s did not come up (rc=%s), see %s" % (b.name, b.proc.poll(), b.log))
        self._kill(b)
        return False

    def _kill(self, b, grace=3.0):
        p = b.proc
        if p is None or p.poll() is not None:
            return
        p.terminate()
        try:
            p.wait(grace)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(grace)

    def check(self, now=None):
        """Restart dead boards. Returns the names restarted."""
        now = time.monotonic() if now is None else now
        restarted = []
        for b in self.boards:
            if self.stopping:
                break
            if b.proc is not None and b.proc.poll() is None:
                if b.started and time.time() - b.started > 10:
                    b.fails = 0
                continue
            if now < b.next_try:
                continue
            if b.proc is not None:
                b.restarts += 1
                b.fails += 1
                self.say("%s died rc=%s, restart %d" % (b.name, b.proc.returncode, b.restarts))
            ok = self.start_board(b)
            if not ok:
                b.fails += 1
            b.next_try = now + (min(30.0, 2.0 ** b.fails) if b.fails > 1 else 0.0)
            restarted.append(b.name)
        if restarted:
            self.write_state()
        return restarted

    def start(self):
        os.makedirs(self.state_dir, exist_ok=True)
        for b in self.boards:
            if not self.start_board(b):
                b.fails += 1
        self.write_state()

    def stop(self):
        self.stopping = True
        for b in self.boards:
            if b.proc is not None and b.proc.poll() is None:
                b.proc.terminate()
        for b in self.boards:
            self._kill(b)
            for p in (b.link, b.monitor):
                if p and os.path.lexists(p):
                    os.unlink(p)
        self.write_state("stopped")
        self.say("stopped %d boards" % len(self.boards))

    def serve(self):
        def on_sig(signum, frame):
            self.stopping = True
        signal.signal(signal.SIGTERM, on_sig)
        signal.signal(signal.SIGINT, on_sig)
        signal.signal(signal.SIGHUP, on_sig)  # tmux kill-session sends HUP
        self.start()
        try:
            while not self.stopping:
                time.sleep(POLL)
                self.check()
        finally:
            self.stop()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--elf", required=True)
    ap.add_argument("--boards", type=int, default=1)
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--icount", default=None, help="QEMU -icount shift=SHIFT (int or auto)")
    ap.add_argument("--monitor", action="store_true",
                    help="a monitor socket per board (for reset) instead of -monitor none")
    ap.add_argument("--qemu", default="qemu-system-arm", help="the QEMU command (tests use a fake)")
    a = ap.parse_args(argv)
    if not a.elf.endswith(".sim.elf"):
        print("refusing %s: only SIM builds (*.sim.elf) run in the fleet" % a.elf, file=sys.stderr)
        return 2
    if not os.path.isfile(a.elf):
        print("no such ELF: %s" % a.elf, file=sys.stderr)
        return 2
    if not 1 <= a.boards <= 32:
        print("--boards must be 1..32", file=sys.stderr)
        return 2
    if a.icount is not None and not re.match(r"^(\d{1,2}|auto)$", a.icount):
        print("--icount takes an integer shift or auto", file=sys.stderr)
        return 2
    os.makedirs(a.state_dir, exist_ok=True)
    pidfile = os.path.join(a.state_dir, "fleet.pid")
    try:
        with open(pidfile) as f:
            old = int(f.read().strip() or 0)
        if old and old != os.getpid() and pid_alive(old):
            print("a fleet (pid %d) already runs in %s" % (old, a.state_dir), file=sys.stderr)
            return 2
    except (OSError, ValueError):
        pass
    with open(pidfile, "w") as f:
        f.write("%d\n" % os.getpid())
    try:
        Fleet(a.elf, a.boards, a.state_dir, shlex.split(a.qemu), a.icount, a.monitor).serve()
    finally:
        try:
            os.unlink(pidfile)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
