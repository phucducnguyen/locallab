"""Lane A: serial owner over a pty, replaying ../fw-public/logs/*.raw.

Skipped cleanly when pyserial is not importable (other lanes' runs).
"""

import glob
import json
import os
import queue
import tempfile
import time
import tty
import unittest

try:
    import serial  # noqa: F401
    HAVE_SERIAL = True
except ImportError:
    HAVE_SERIAL = False

from bench.serial_owner import SerialOwner

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
LOGS = os.path.join(os.path.dirname(REPO), "fw-public", "logs")
EXPECTED = os.path.join(HERE, "data", "a_replay_expected.json")
BYTE_TIME = 10.0 / 115200  # 8N1


def compact(r):
    k = r["kind"]
    if k == "BOOT":
        return "BOOT %s %s" % (r["fw"], ",".join(r["reset"]))
    if k == "TEST":
        return "TEST %s %s" % (r["name"], r["status"])
    if k == "RESULT":
        return "RESULT %s" % r["status"]
    if k == "HARDFAULT":
        return "HARDFAULT %d" % sum(1 for f in ("pc", "lr", "psr", "cfsr", "hfsr", "bfar", "mmfar", "sp") if f in r)
    if k == "ALIVE":
        return "ALIVE %s" % r["n"]
    if k == "HEALTH":
        return "HEALTH %s %s" % (r["n"], r["loopback"])
    return "%s %s" % (k, r.get("line"))


class FakePort:
    """A pty whose slave is reached through a symlink, like /dev/serial/by-id."""

    def __init__(self, link):
        self.link = link
        self.master, self.slave = os.openpty()
        tty.setraw(self.master)
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(os.ttyname(self.slave), link)

    def write_paced(self, data, chunk=32):
        for i in range(0, len(data), chunk):
            part = data[i:i + chunk]
            os.write(self.master, part)
            time.sleep(len(part) * BYTE_TIME)

    def unplug(self):
        if os.path.lexists(self.link):
            os.unlink(self.link)
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


def wait_for(owner, kind, timeout=5.0):
    """Return the events up to and including the first of this kind."""
    got = []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            ev = owner.events.get(timeout=0.05)
        except queue.Empty:
            continue
        got.append(ev)
        if ev.kind == kind:
            return got
    raise AssertionError("no %s within %.1fs; got %r" % (kind, timeout, got))


def drain(owner, quiet=0.4, timeout=10.0):
    got = []
    end = time.monotonic() + timeout
    last = time.monotonic()
    while time.monotonic() < end and time.monotonic() - last < quiet:
        try:
            got.append(owner.events.get(timeout=0.05))
            last = time.monotonic()
        except queue.Empty:
            pass
    return got


@unittest.skipUnless(HAVE_SERIAL, "pyserial not importable")
class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.link = os.path.join(self.tmp.name, "usb-STMicroelectronics_STLINK-V2-1_TEST-if02")

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, **kw):
        kw.setdefault("idle_flush", 0.2)
        kw.setdefault("reopen_interval", 0.1)
        owner = SerialOwner(port_glob=os.path.join(self.tmp.name, "*STM*"), **kw).start()
        self.addCleanup(owner.stop)
        return owner

    def test_all_recordings(self):
        with open(EXPECTED) as f:
            expected = json.load(f)
        files = sorted(glob.glob(os.path.join(LOGS, "*.raw")))
        self.assertEqual(len(files), 13, files)
        for path in files:
            name = os.path.basename(path)
            with self.subTest(recording=name):
                exp = expected[name]
                port = FakePort(self.link)
                owner = self.start()
                try:
                    wait_for(owner, "LINK_UP")
                    with open(path, "rb") as f:
                        port.write_paced(f.read())
                    events = drain(owner)
                finally:
                    owner.stop()
                    port.unplug()
                lines = [e for e in events if e.kind == "LINE"]
                records = [r for e in lines for r in e.records]
                n = len(exp["first"])
                self.assertEqual([compact(r) for r in records[:n]], exp["first"])
                c = owner.counters.as_dict()
                self.assertEqual(c["by_kind"], exp["counts"])
                self.assertEqual(c["lw_records"], sum(exp["counts"].values()))
                self.assertEqual(c["nonlw_lines"], exp["nonlw_lines"])
                self.assertEqual(c["bad_bytes"], exp["bad_bytes"])
                for e in lines:  # timestamps on receipt, in order
                    self.assertRegex(e.raw_ts, r"^\d\d:\d\d:\d\d\.\d{3}$")
                monos = [e.t_mono for e in lines]
                self.assertEqual(monos, sorted(monos))

    def test_port_loss_and_reopen(self):
        port = FakePort(self.link)
        owner = self.start()
        wait_for(owner, "LINK_UP")
        port.write_paced(b"LW BOOT fw=abc1234 reset=PIN csr=0x04000000\r\nLW TEST crc32 PASS\r\nLW ALI")
        time.sleep(0.1)
        port.unplug()  # mid-line
        before = wait_for(owner, "LINK_DOWN")
        recs = [r for e in before if e.kind == "LINE" for r in e.records]
        self.assertEqual([r["kind"] for r in recs], ["BOOT", "TEST", "OTHER"])
        self.assertFalse(owner.link_up)
        self.assertFalse(owner.send("ping"))
        time.sleep(0.3)  # the owner keeps retrying with nothing there
        port2 = FakePort(self.link)
        self.addCleanup(port2.unplug)
        wait_for(owner, "LINK_UP")
        port2.write_paced(b"LW ALIVE 7\r\nLW HEALTH 7 loopback=OK\r\n")
        after = [r for e in drain(owner) if e.kind == "LINE" for r in e.records]
        self.assertEqual([compact(r) for r in after], ["ALIVE 7", "HEALTH 7 OK"])
        # one transition each way, never repeated while the port was missing
        self.assertTrue(owner.link_up)

    def test_link_transitions_not_repeated(self):
        owner = self.start()  # nothing plugged in yet
        evs = wait_for(owner, "LINK_DOWN")
        time.sleep(0.4)
        evs += drain(owner, quiet=0.2)
        self.assertEqual(sum(e.kind == "LINK_DOWN" for e in evs), 1)
        port = FakePort(self.link)
        self.addCleanup(port.unplug)
        wait_for(owner, "LINK_UP")

    def test_commands_and_exclusive(self):
        port = FakePort(self.link)
        self.addCleanup(port.unplug)
        owner = self.start()
        wait_for(owner, "LINK_UP")
        self.assertFalse(owner.send("x" * 33))
        self.assertTrue(owner.send("ping"))
        self.assertTrue(owner.send("test loopback"))
        got = b""
        end = time.monotonic() + 3
        os.set_blocking(port.master, False)
        while time.monotonic() < end and got.count(b"\n") < 2:
            try:
                got += os.read(port.master, 100)
            except BlockingIOError:
                time.sleep(0.02)
        self.assertEqual(got, b"ping\ntest loopback\n")
        with self.assertRaises(serial.SerialException):
            serial.Serial(self.link, 115200, exclusive=True)


if __name__ == "__main__":
    unittest.main()
