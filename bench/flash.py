"""Build a sha of the bench repo's main and copy it onto the NUCLEO drive.

Build (build()):
  git archive <sha> into a clean build dir (never a checkout of the repo
  itself), then make FW_VERSION=<git rev-parse --short=7 sha> [TOOLCHAIN=$TOOLCHAIN].
  build/fw.elf is kept as <state>/elf/<short>.elf for addr2line later.
  The sha must be a commit that is an ancestor of refs/heads/main.

Drive (Drive.locate()), pluggable, in this order; sudo is never called:
  1. an existing mount of the label's current device that is live and
     writable (/proc/mounts + /dev/disk/by-label + statvfs; nothing written);
  2. udisksctl mount -b /dev/disk/by-label/<label>;
  3. mount <base>/<label> (works without root when /etc/fstab has a
     "user,noauto" line for it); a stale mount there is unmounted first;
  4. otherwise a FLASH_ERROR naming the command to run.
  The label is NODE_F446RE or NOD_F446RE; bases are /run/media/$USER and
  /media/$USER. LW_DRIVE=<dir> skips all of it and uses that directory.

Copy (copy_bin()): fw.bin in chunks, flush, fsync, then sync. A failure
before every byte is written is a FLASH_ERROR. The board resets itself when
the copy finishes; the bench's LW BOOT fw=<sha> check is the verification.

FlashJob ties them to the bench: build, pause the board (Bench.begin_flash),
locate, copy, arm the 15 s BOOT window (Bench.flash_copied), or record
BUILD_ERROR / FLASH_ERROR. It runs in its own thread and reaches the bench
only through call(), which runs a function on the bench's thread.

Commit subjects and build output are data: stored and quoted, never run.
"""

import errno
import getpass
import io
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time

from bench.check_bench_repo import tree_is_clean  # noqa: F401  (the per-sha guard)

SHA_RE = re.compile(r"[0-9a-f]{7,40}")
LABELS = ("NODE_F446RE", "NOD_F446RE")
BY_LABEL = "/dev/disk/by-label"
PROC_MOUNTS = "/proc/mounts"
FSTAB = "/etc/fstab"
BUILD_TIMEOUT_S = 300
GIT_TIMEOUT_S = 30
CMD_TIMEOUT_S = 15
DEVICE_WAIT_S = 10.0
CHUNK = 16 * 1024
TEXT_MAX = 120


class FlashError(Exception):
    pass


def printable(s, cap=TEXT_MAX):
    s = "".join(c if " " <= c < "\x7f" else "?" for c in (s or ""))
    return s if len(s) <= cap else s[:cap - 3] + "..."


def _git(repo, *args, binary=False):
    p = subprocess.run(["git", "-C", repo] + list(args), stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=GIT_TIMEOUT_S)
    out = p.stdout if binary else p.stdout.decode("utf-8", "replace")
    return p.returncode, out


def resolve(repo, sha):
    """(full sha, short sha, subject) for a commit on main; FlashError otherwise."""
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise FlashError("sha must be 7-40 lowercase hex characters")
    rc, full = _git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options",
                    sha + "^{commit}")
    full = full.strip()
    if rc != 0 or not SHA_RE.fullmatch(full):
        raise FlashError("%s is not a commit in %s" % (sha, repo))
    rc, _ = _git(repo, "merge-base", "--is-ancestor", full, "refs/heads/main")
    if rc != 0:
        raise FlashError("%s is not on main of %s" % (sha, repo))
    _, short = _git(repo, "rev-parse", "--short=7", full)
    _, subject = _git(repo, "log", "-1", "--format=%s", full)
    return full, short.strip(), printable(subject.strip())


def _extract(tar_bytes, dest):
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")
        else:  # pragma: no cover - pythons without extraction filters
            tf.extractall(dest)


def _error_line(text):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):  # the compiler's or linker's line, not make's summary
        if "error" in ln.lower() and not ln.startswith(("make:", "make[")):
            return printable(ln.strip())
    for ln in reversed(lines):
        if "error" in ln.lower():
            return printable(ln.strip())
    return printable(lines[-1].strip()) if lines else ""


def toolchain_prefix(toolchain=None):
    return toolchain if toolchain is not None else os.environ.get("TOOLCHAIN") or None


def build(repo, sha, state_dir, toolchain=None, make="make", timeout=BUILD_TIMEOUT_S):
    """Build sha. Returns a dict: ok, sha (short), full, subject, bin, elf, why,
    toolchain_missing, log_tail. Never raises for a build problem."""
    res = {"ok": False, "sha": sha, "full": None, "subject": None, "bin": None, "elf": None,
           "why": "", "toolchain_missing": False, "log_tail": []}
    try:
        full, short, subject = resolve(repo, sha)
    except (FlashError, OSError, subprocess.TimeoutExpired) as e:
        res["why"] = printable(str(e)) or "git failed"
        res["bench_fault"] = True
        return res
    res.update(sha=short, full=full, subject=subject)
    try:
        clean, why = tree_is_clean(repo, full)
    except Exception as e:  # never build an unchecked tree
        clean, why = False, "tree check failed: %s" % e.__class__.__name__
    if not clean:
        res["why"] = printable(why)
        res["bench_fault"] = True
        res["refused_tree"] = True
        return res
    root = os.path.join(state_dir, "build", short)
    elf_dir = os.path.join(state_dir, "elf")
    try:
        if os.path.isdir(root):
            shutil.rmtree(root)
        os.makedirs(root)
        os.makedirs(elf_dir, exist_ok=True)
        rc, tar = _git(repo, "archive", "--format=tar", full, binary=True)
        if rc != 0 or not tar:
            raise FlashError("git archive %s failed" % short)
        _extract(tar, root)
    except (FlashError, OSError, tarfile.TarError, subprocess.TimeoutExpired) as e:
        res["why"] = printable("export failed: %s" % e)
        res["bench_fault"] = True
        return res
    tc = toolchain_prefix(toolchain)
    res["toolchain"] = tc
    cmd = [make, "-C", root, "FW_VERSION=%s" % short]
    if tc:
        cmd.append("TOOLCHAIN=%s" % tc)
    res["cmd"] = " ".join(cmd)
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout)
        out, rc = p.stdout.decode("utf-8", "replace"), p.returncode
    except subprocess.TimeoutExpired:
        out, rc = "make timed out after %d s" % timeout, -1
        res["bench_fault"] = True
    except OSError as e:
        out, rc = "make could not run: %s" % e, -1
        res["bench_fault"] = True
    res["log_tail"] = [printable(ln) for ln in out.splitlines()[-12:]]
    gcc = (tc or "arm-none-eabi-") + "gcc"
    res["toolchain_missing"] = shutil.which(gcc) is None
    binp = os.path.join(root, "build", "fw.bin")
    elfp = os.path.join(root, "build", "fw.elf")
    if rc != 0:
        res["why"] = _error_line(out) or "make exited %d" % rc
        return res
    if not os.path.isfile(binp) or os.path.getsize(binp) == 0:
        res["why"] = "make succeeded but build/fw.bin is missing or empty"
        return res
    keep = os.path.join(elf_dir, "%s.elf" % short)
    if os.path.isfile(elfp):
        shutil.copyfile(elfp, keep)
        res["elf"] = keep
    res.update(ok=True, bin=binp)
    return res


# ---- the NUCLEO drive ----

def _unescape(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def read_mounts(path=PROC_MOUNTS):
    """[(device, mountpoint, options)] from /proc/mounts (or a file like it)."""
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                parts = ln.split()
                if len(parts) >= 4:
                    out.append((_unescape(parts[0]), _unescape(parts[1]), parts[3]))
    except OSError:
        pass
    return out


def fstab_points(path=FSTAB, labels=LABELS):
    """Mount points in fstab whose last part is one of the labels."""
    pts = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                parts = ln.split()
                if len(parts) >= 2 and not parts[0].startswith("#"):
                    mp = _unescape(parts[1])
                    if os.path.basename(mp) in labels:
                        pts.append(mp)
    except OSError:
        pass
    return pts


def _run_cmd(cmd):
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=CMD_TIMEOUT_S)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


class Drive:
    """Find (or mount) the NUCLEO drive. Every path and command is injectable."""

    def __init__(self, user=None, bases=None, labels=LABELS, by_label=BY_LABEL,
                 proc_mounts=PROC_MOUNTS, fstab=FSTAB, run=_run_cmd, fixed=None,
                 device_wait_s=DEVICE_WAIT_S, log=lambda s: None):
        self.user = user or os.environ.get("USER") or getpass.getuser()
        self.bases = bases or ["/run/media/%s" % self.user, "/media/%s" % self.user]
        self.labels = labels
        self.by_label = by_label
        self.proc_mounts = proc_mounts
        self.fstab = fstab
        self.run = run
        self.fixed = fixed if fixed is not None else os.environ.get("LW_DRIVE") or None
        self.device_wait_s = device_wait_s
        self.log = log
        self.tried = []

    def devices(self):
        """{label: device path} for labels present under by-label."""
        out = {}
        for lab in self.labels:
            link = os.path.join(self.by_label, lab)
            if os.path.lexists(link):
                out[lab] = os.path.realpath(link)
        return out

    @staticmethod
    def _writable(mp):
        try:
            os.statvfs(mp)
        except OSError:
            return False
        return os.path.isdir(mp) and os.access(mp, os.W_OK)

    def live_mount(self, devs):
        """A mount point of a current label device that is live and writable."""
        want = set(devs.values())
        for dev, mp, opts in read_mounts(self.proc_mounts):
            real = os.path.realpath(dev) if dev.startswith("/") else dev
            if real in want and "ro" not in opts.split(",") and self._writable(mp):
                return mp
        return None

    def stale_mounts(self, devs):
        """Mount points named after a label that are not mounts of its current device."""
        want = set(devs.values())
        out = []
        for dev, mp, _ in read_mounts(self.proc_mounts):
            real = os.path.realpath(dev) if dev.startswith("/") else dev
            if os.path.basename(mp) in self.labels and real not in want:
                out.append(mp)
        return out

    def _cmd(self, cmd):
        rc, out = self.run(cmd)
        self.tried.append("%s -> %s" % (" ".join(cmd), "ok" if rc == 0 else "exit %s" % rc))
        self.log("flash: %s: %s" % (" ".join(cmd), "ok" if rc == 0 else printable(out, 100)))
        return rc == 0

    def release(self):
        """Unmount every mount of the NUCLEO drive (live or stale); errors are ignored."""
        if self.fixed:
            return
        want = set(self.devices().values())
        points = set()
        for dev, mp, _ in read_mounts(self.proc_mounts):
            real = os.path.realpath(dev) if dev.startswith("/") else dev
            if real in want or os.path.basename(mp) in self.labels:
                points.add(mp)
        for mp in sorted(points):
            self._cmd(["umount", mp])

    def locate(self):
        """(mount point, None) or (None, the FLASH_ERROR message)."""
        self.tried = []
        if self.fixed:
            if self._writable(self.fixed):
                return self.fixed, None
            return None, "LW_DRIVE=%s is not a writable directory" % self.fixed
        end = time.monotonic() + self.device_wait_s
        devs = self.devices()
        while not devs and time.monotonic() < end:
            time.sleep(0.25)
            devs = self.devices()
        if not devs:
            return None, ("no NUCLEO drive: %s/{%s} is missing (board unplugged, or still "
                          "re-enumerating after the last flash)" % (
                              self.by_label, ",".join(self.labels)))
        mp = self.live_mount(devs)
        if mp:
            return mp, None
        label = sorted(devs)[0]
        if self._cmd(["udisksctl", "mount", "--no-user-interaction", "-b",
                      os.path.join(self.by_label, label)]):
            mp = self.live_mount(devs)
            if mp:
                return mp, None
        points = []
        for p in fstab_points(self.fstab, self.labels) + [
                os.path.join(b, lab) for b in self.bases for lab in devs]:
            if p not in points and os.path.isdir(p):
                points.append(p)
        stale = self.stale_mounts(devs)
        for p in points:
            if p in stale:
                self._cmd(["umount", p])
            if self._cmd(["mount", p]):
                mp = self.live_mount(devs)
                if mp:
                    return mp, None
        target = points[0] if points else os.path.join(self.bases[-1], label)
        if not points:
            self.tried.append("no mount point %s exists" % target)
        return None, ("NUCLEO drive %s is not mounted writable (tried: %s). Run once: "
                      "sudo mount -o uid=%d %s %s, or add to /etc/fstab: "
                      "LABEL=%s %s vfat user,noauto,uid=%d 0 0" % (
                          label, "; ".join(self.tried) or "nothing", os.getuid(),
                          os.path.join(self.by_label, label), target, label, target,
                          os.getuid()))


def copy_bin(src, drive_dir, opener=open, chunk=CHUNK, sync=os.sync):
    """Copy src to drive_dir/fw.bin: (ok, message, warning).

    ok means every byte was written. warning is set when fsync or close then
    failed: on Linux a vanished drive often shows only there, so the bench
    treats silence after such a copy as FLASH_ERROR (a BOOT still verifies)."""
    dest = os.path.join(drive_dir, "fw.bin")
    try:
        with open(src, "rb") as f:
            data = f.read()
    except OSError as e:
        return False, "cannot read %s: %s" % (src, e.strerror or e), None
    written = 0
    note = ""
    try:
        f = opener(dest, "wb")
    except OSError as e:
        return False, "cannot open %s: %s" % (dest, e.strerror or e), None
    try:
        try:
            while written < len(data):
                n = f.write(data[written:written + chunk])
                written += n if n else len(data[written:written + chunk])
            f.flush()
        except OSError as e:
            return False, "copy failed after %d of %d bytes: %s" % (
                written, len(data), e.strerror or errno.errorcode.get(e.errno, e)), None
        try:
            os.fsync(f.fileno())
        except (OSError, AttributeError, ValueError) as e:
            # Every byte was handed over; the board may already be resetting.
            if isinstance(e, OSError):
                note = "; fsync: %s" % (e.strerror or e.__class__.__name__)
    finally:
        try:
            f.close()
        except OSError as e:
            note += "; close: %s" % (e.strerror or e)
    try:
        sync()
    except OSError:
        pass
    return True, "copied %d bytes to %s%s" % (written, dest, note), note.lstrip("; ") or None


class FlashJob:
    """Build, pause, copy, arm: one flash of sha onto one board.

    call(fn, *args) runs fn on the bench's thread and returns its result.
    """

    def __init__(self, bench, call, repo, sha, state_dir, drive=None, toolchain=None,
                 make="make", window=None, opener=open, sync=os.sync, log=print):
        self.bench = bench
        self.call = call
        self.repo = repo
        self.sha = sha
        self.state_dir = state_dir
        self.drive = drive or Drive(log=log)
        self.toolchain = toolchain
        self.make = make
        self.window = window
        self.opener = opener
        self.sync = sync
        self.log = log
        self.result = None
        self.done = threading.Event()
        self._thread = None

    def run(self):
        try:
            self.result = self._run()
        except Exception as e:  # never kill the bench; record a flash error
            self.result = ("FLASH_ERROR", "flash job error %s" % e.__class__.__name__)
            try:
                self.call(self.bench.flash_failed, self.result[1])
            except Exception:
                pass
        finally:
            self.done.set()
        return self.result

    def _run(self):
        self.log("flash: building %s from %s" % (self.sha, self.repo))
        b = build(self.repo, self.sha, self.state_dir, toolchain=self.toolchain, make=self.make)
        if not b["ok"]:
            self.log("flash: BUILD_ERROR %s: %s" % (b["sha"], b["why"]))
            self.call(self.bench.build_failed, b)
            return ("BUILD_ERROR", b["why"])
        self.log("flash: built %s (%s), elf %s" % (b["sha"], b["subject"], b["elf"]))
        self.call(self.bench.begin_flash, b["sha"], b["subject"], b["elf"])
        # The drive re-enumerates after every flash; a mount kept across that holds a
        # stale FAT view (the kernel then sets it read-only). Always mount fresh.
        release = getattr(self.drive, "release", None)
        if release:
            release()
        mp, why = self.drive.locate()
        if mp is None:
            self.log("flash: FLASH_ERROR %s" % why)
            self.call(self.bench.flash_failed, why)
            return ("FLASH_ERROR", why)
        ok, msg, warn = copy_bin(b["bin"], mp, opener=self.opener, sync=self.sync)
        t_copied = time.time()
        if release:
            release()
        self.log("flash: %s" % msg)
        if not ok:
            self.call(self.bench.flash_failed, msg)
            return ("FLASH_ERROR", msg)
        from bench.incidents import FLASH_WINDOW_S
        self.call(self.bench.flash_copied, msg, t_copied,
                  FLASH_WINDOW_S if self.window is None else self.window, warn)
        return ("COPIED", msg)

    def start(self):
        self._thread = threading.Thread(target=self.run, name="flash", daemon=True)
        self._thread.start()
        return self
