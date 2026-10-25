"""A stand-in for qemu-system-arm, for tests and laptop demos (no QEMU there).

It takes the same arguments fleet.run passes, opens a pty, announces it on
stderr the way QEMU does, and prints the README's serial protocol on it:
a BOOT, the five tests, RESULT PASS, then ALIVE/HEALTH pairs at FAKEQEMU_HZ
(default 10, like the SIM build on QEMU). A "-monitor unix:<path>,..." gets a
tiny monitor that answers system_reset by booting again.

Env: FAKEQEMU_HZ, FAKEQEMU_FW (the sha in BOOT), FAKEQEMU_DIE_AFTER (exit 3
after that many seconds, to test restarts).
"""

import os
import select
import signal
import socket
import sys
import time
import tty

TESTS = ("crc32", "ringbuf", "systick", "cmd_dispatch", "loopback")


def boot_lines(fw, reset):
    out = ["", "fake firmware (SIM build)", "LW BOOT fw=%s reset=%s csr=0x0c000000" % (fw, reset)]
    out += ["LW TEST %s PASS" % t for t in TESTS]
    out.append("LW RESULT PASS")
    return out


def main(argv):
    hz = float(os.environ.get("FAKEQEMU_HZ", "10"))
    fw = os.environ.get("FAKEQEMU_FW", "fake000")
    die_after = float(os.environ.get("FAKEQEMU_DIE_AFTER", "0") or 0)
    mon_path = None
    if "-monitor" in argv:
        spec = argv[argv.index("-monitor") + 1]
        if spec.startswith("unix:"):
            mon_path = spec[5:].split(",")[0]
    master, slave = os.openpty()
    tty.setraw(master)
    os.set_blocking(master, False)
    sys.stderr.write("char device redirected to %s (label serial1)\n" % os.ttyname(slave))
    sys.stderr.flush()
    os.close(slave)
    mon = None
    if mon_path:
        mon = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        mon.bind(mon_path)
        mon.listen(4)
    stop = []
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))

    def emit(line):
        try:
            os.write(master, line.encode() + b"\r\n")
        except OSError:
            pass  # nobody reading, buffer full: drop, as QEMU does

    pending = boot_lines(fw, "POR")
    n = 0
    t0 = time.monotonic()
    next_tick = t0
    while not stop:
        if die_after and time.monotonic() - t0 > die_after:
            return 3
        while pending:
            emit(pending.pop(0))
        now = time.monotonic()
        if now >= next_tick:
            n += 1
            emit("LW ALIVE %d" % n)
            emit("LW HEALTH %d loopback=OK" % n)
            next_tick += 1.0 / hz
        rl = [mon] if mon else []
        timeout = max(0.0, next_tick - time.monotonic())
        try:
            r, _, _ = select.select(rl, [], [], timeout)
        except InterruptedError:
            continue
        if mon and mon in r:
            c, _ = mon.accept()
            c.settimeout(2)
            try:
                c.sendall(b"QEMU fake monitor\r\n(qemu) ")
                data = b""
                while b"\n" not in data:
                    chunk = c.recv(256)
                    if not chunk:
                        break
                    data += chunk
                if b"system_reset" in data:
                    pending = boot_lines(fw, "NONE")  # QEMU has no RCC_CSR model
                    n = 0
                c.sendall(b"\r\n(qemu) ")
            except OSError:
                pass
            finally:
                c.close()
    if mon_path and os.path.exists(mon_path):
        os.unlink(mon_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
