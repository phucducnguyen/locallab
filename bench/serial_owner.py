"""The serial owner: the only code that opens the board's port.

- Opens the port exclusively (pyserial, exclusive=True) at 115200 8N1, from
  --port or the first match of /dev/serial/by-id/*STM*.
- Splits the byte stream into lines, timestamps every line on receipt (UTC
  wall clock with ms, plus time.monotonic()) and parses it.
- Is the only path for commands: send() queues one line of at most 32
  printable ASCII characters; the reader thread writes it.
- When the port disappears it records LINK_DOWN, reopens in a loop and
  records LINK_UP.

- request_probe() (after a GARBLED run): closes the port, listens for a
  moment at each other standard rate, reports which rate (if any) gives
  well-formed LW lines, and reopens at 115200. Probe bytes are only scored,
  never delivered as board lines.

Every line and link change becomes an Event, delivered to the on_event
callback (if any) and put on the events queue.

pyserial is imported lazily so that other code can import this module
without it.
"""

import datetime
import glob
import os
import queue
import threading
import time

from bench import parser

DEFAULT_GLOB = "/dev/serial/by-id/*STM*"
BAUD = 115200
MAX_CMD = 32
MAX_LINE = 4096
PROBE_RATES = (9600, 19200, 38400, 57600, 230400, 460800, 921600)
PROBE_DWELL_S = 1.5


def wall_ts(t):
    """Unix seconds to UTC HH:MM:SS.mmm (events.raw_ts)."""
    d = datetime.datetime.fromtimestamp(t, tz=datetime.timezone.utc)
    return d.strftime("%H:%M:%S.") + "%03d" % (d.microsecond // 1000)


class Event:
    """One line or link change. kind is LINE, LINK_DOWN or LINK_UP."""

    __slots__ = ("kind", "t_wall", "t_mono", "raw_ts", "raw", "records",
                 "nonlw", "bad_bytes", "port", "detail")

    def __init__(self, kind, t_wall, t_mono, raw=b"", parsed=None, port=None,
                 detail=""):
        self.kind = kind
        self.t_wall = t_wall
        self.t_mono = t_mono
        self.raw_ts = wall_ts(t_wall)
        self.raw = raw
        parsed = parsed or {"records": [], "nonlw": False, "bad_bytes": 0}
        self.records = parsed["records"]
        self.nonlw = parsed["nonlw"]
        self.bad_bytes = parsed["bad_bytes"]
        self.port = port
        self.detail = detail

    def as_tuple(self):
        """(timestamp, raw line, parsed records), the shape later stages use."""
        return (self.t_wall, self.raw, self.records)

    def __repr__(self):
        return "Event(%s %s %r %r)" % (self.kind, self.raw_ts, self.raw,
                                       [r["kind"] for r in self.records])


def find_port(pattern=DEFAULT_GLOB):
    matches = sorted(glob.glob(pattern))
    return matches[0] if matches else None


def valid_command(cmd):
    if not isinstance(cmd, str) or not cmd or len(cmd) > MAX_CMD:
        return False
    return all(0x20 <= ord(c) <= 0x7E for c in cmd)


class LineSplitter:
    """Bytes in, complete raw lines out (without the LF)."""

    def __init__(self, max_line=MAX_LINE):
        self.buf = b""
        self.max_line = max_line

    def feed(self, data):
        self.buf += data
        lines = []
        while True:
            i = self.buf.find(b"\n")
            if i < 0:
                break
            lines.append(self.buf[:i])
            self.buf = self.buf[i + 1:]
        # An LW line with records glued together and no line break yet: hand
        # over the complete records now, keep the one still arriving.
        head = self.buf.lstrip(b"\r")
        if head.startswith(b"LW "):
            last = None
            for m in parser.SPLIT_RE_BYTES.finditer(self.buf):
                if m.start() > len(self.buf) - len(head):
                    last = m.start()
            if last is not None:
                lines.append(self.buf[:last])
                self.buf = self.buf[last:]
        if len(self.buf) > self.max_line:
            lines.append(self.buf)
            self.buf = b""
        return lines

    def flush(self):
        rest, self.buf = self.buf, b""
        return [rest] if rest else []


class SerialOwner:
    def __init__(self, port=None, port_glob=DEFAULT_GLOB, baud=BAUD,
                 on_event=None, reopen_interval=0.5, idle_flush=0.5,
                 read_timeout=0.05, serial_module=None):
        self.port = port
        self.port_glob = port_glob
        self.baud = baud
        self.on_event = on_event
        self.reopen_interval = reopen_interval
        self.idle_flush = idle_flush
        self.read_timeout = read_timeout
        self._serial_module = serial_module
        self.events = queue.Queue()
        self.counters = parser.Counters()
        self.link_up = None  # None until the first open attempt
        self.current_port = None
        self._cmds = queue.Queue()
        self._stop = threading.Event()
        self._thread = None
        self._ser = None
        self._split = LineSplitter()
        self._last_rx = None
        self._probes = queue.Queue()

    # ---- public ----
    def start(self):
        self._thread = threading.Thread(target=self._run, name="serial-owner",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=2.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        self._close()

    def send(self, cmd):
        """Queue one command line. False if it is invalid or the link is down."""
        if not valid_command(cmd) or not self.link_up:
            return False
        self._cmds.put(cmd)
        return True

    def request_probe(self, rates=PROBE_RATES, dwell=PROBE_DWELL_S, callback=None):
        """Ask the reader thread to try other rates; callback(result) when done."""
        self._probes.put((tuple(rates), dwell, callback))
        return True

    @staticmethod
    def score(data):
        """(well-formed LW records, unreadable bytes) in a probe's bytes."""
        good = 0
        for raw in data.split(b"\n"):
            for r in parser.parse_line(raw.rstrip(b"\r"))["records"]:
                if r["kind"] in parser.KINDS and not r.get("malformed"):
                    good += 1
        return good, parser.count_bad_bytes(data)

    def probe_rates(self, path, rates, dwell):
        """Listen at each rate on path; the result dict (port must be closed)."""
        serial = self._serial()
        tried = []
        best = None
        for rate in rates:
            if self._stop.is_set():
                break
            data = b""
            try:
                ser = serial.Serial(port=path, baudrate=rate, bytesize=8, parity="N",
                                    stopbits=1, timeout=0.05, exclusive=True)
            except Exception as e:
                tried.append({"rate": rate, "error": e.__class__.__name__})
                continue
            try:
                end = time.monotonic() + dwell
                while time.monotonic() < end and len(data) < 8192:
                    data += ser.read(ser.in_waiting or 1)
            except Exception as e:
                tried.append({"rate": rate, "error": e.__class__.__name__})
                continue
            finally:
                try:
                    ser.close()
                except Exception:
                    pass
            good, bad = self.score(data)
            tried.append({"rate": rate, "bytes": len(data), "lw_records": good,
                          "bad_bytes": bad})
            if good and bad * 10 <= len(data) and (best is None or good > best[1]):
                best = (rate, good)
        return {"tried": tried, "decodes_at": best[0] if best else None,
                "standard_rates": list(rates)}

    def _run_probe(self):
        rates, dwell, callback = self._probes.get_nowait()
        path = self.current_port
        self._emit_lines(self._split.flush(), time.time(), time.monotonic())
        self._close()
        try:
            result = self.probe_rates(path, rates, dwell)
        except Exception as e:
            result = {"tried": [], "decodes_at": None, "error": e.__class__.__name__}
        if callback:
            try:
                callback(result)
            except Exception:
                pass
        # The port reopens at self.baud on the next pass of _run.

    # ---- internals ----
    def _serial(self):
        if self._serial_module is None:
            import serial  # pyserial, lazily
            self._serial_module = serial
        return self._serial_module

    def _emit(self, ev):
        if ev.kind == "LINE":
            self.counters.add({"records": ev.records, "nonlw": ev.nonlw,
                               "bad_bytes": ev.bad_bytes})
        self.events.put(ev)
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:
                pass

    def _emit_lines(self, lines, t_wall, t_mono):
        for raw in lines:
            raw = raw.rstrip(b"\r")
            p = parser.parse_line(raw)
            if not p["records"] and not p["nonlw"]:
                continue  # empty line
            self._emit(Event("LINE", t_wall, t_mono, raw, p,
                             port=self.current_port))

    def _set_link(self, up, detail=""):
        if self.link_up is up:
            return
        self.link_up = up
        self._emit(Event("LINK_UP" if up else "LINK_DOWN", time.time(),
                         time.monotonic(), port=self.current_port,
                         detail=detail))

    def _open(self):
        path = self.port or find_port(self.port_glob)
        if not path:
            raise OSError("no port matches %s" % self.port_glob)
        serial = self._serial()
        ser = serial.Serial(port=path, baudrate=self.baud, bytesize=8,
                            parity="N", stopbits=1, timeout=self.read_timeout,
                            write_timeout=1.0, exclusive=True)
        self._ser = ser
        self.current_port = path
        self._split = LineSplitter()
        self._last_rx = None

    def _close(self):
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

    def _lost(self, why):
        now_w, now_m = time.time(), time.monotonic()
        self._emit_lines(self._split.flush(), now_w, now_m)
        self._close()
        # Drop commands queued for the old link; nothing is sent blind.
        while True:
            try:
                self._cmds.get_nowait()
            except queue.Empty:
                break
        self._set_link(False, why)

    def _run(self):
        while not self._stop.is_set():
            if self._ser is not None and not self._probes.empty():
                self._run_probe()
            if self._ser is None:
                try:
                    self._open()
                except Exception as e:  # missing, busy, permission
                    self._set_link(False, str(e)[:120])
                    self._stop.wait(self.reopen_interval)
                    continue
                self._set_link(True, self.current_port)
            try:
                self._pump()
            except Exception as e:
                self._lost(str(e)[:120])
                self._stop.wait(self.reopen_interval)

    def _pump(self):
        ser = self._ser
        while True:
            try:
                cmd = self._cmds.get_nowait()
            except queue.Empty:
                break
            ser.write(cmd.encode("ascii") + b"\n")
            ser.flush()
        data = ser.read(ser.in_waiting or 1)
        t_wall, t_mono = time.time(), time.monotonic()
        if data:
            self._last_rx = t_mono
            self._emit_lines(self._split.feed(data), t_wall, t_mono)
        elif (self._split.buf and self._last_rx is not None
              and t_mono - self._last_rx >= self.idle_flush):
            self._emit_lines(self._split.flush(), t_wall, t_mono)
        if not os.path.exists(self.current_port):
            raise OSError("port %s disappeared" % self.current_port)
