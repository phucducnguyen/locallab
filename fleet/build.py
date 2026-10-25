"""Build the SIM firmware of one commit of the bench repo, for QEMU only.

    python3 -m fleet.build --repo ~/bench-fw --sha HEAD --out ~/lw-fleet

The sources are never read here: the commit is exported with
`git archive <sha> | tar -x` into a fresh temp dir, built there with
`make SIM=1 FW_VERSION=<short sha>`, and only build/fw.elf is copied out to
<out>/elf/<short sha>.sim.elf. The repo's own build/ dir is never touched and
no .bin is ever copied: a SIM build must never reach the real board.
"""

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
SIM_MARK = b"SIM build"  # the firmware banner of a SIM build (README)
TOOLCHAIN_GLOBS = ("~/opt/arm-gnu-toolchain-*/bin/arm-none-eabi-gcc",
                   "/opt/arm*/bin/arm-none-eabi-gcc")


class BuildError(Exception):
    pass


def find_toolchain():
    """A TOOLCHAIN= prefix when arm-none-eabi-gcc is not on PATH, else None."""
    if shutil.which("arm-none-eabi-gcc"):
        return None
    for pat in TOOLCHAIN_GLOBS:
        hits = sorted(glob.glob(os.path.expanduser(pat)))
        if hits:
            return hits[-1][:-len("gcc")]
    return None


def git_cmd(repo, *args):
    return ["git", "-C", repo] + list(args)


def resolve_cmds(repo, sha):
    """Commands that turn a ref into (full sha, short sha)."""
    return (git_cmd(repo, "rev-parse", "--verify", "--quiet", sha + "^{commit}"),
            git_cmd(repo, "rev-parse", "--short=7", sha + "^{commit}"))


def export_cmds(repo, full, tmpdir):
    """The archive | tar pipeline, as two argv lists."""
    return (git_cmd(repo, "archive", "--format=tar", full),
            ["tar", "-x", "-C", tmpdir])


def make_cmd(tmpdir, short, toolchain=None):
    cmd = ["make", "-C", tmpdir, "SIM=1", "FW_VERSION=" + short]
    if toolchain:
        cmd.append("TOOLCHAIN=" + toolchain)
    cmd.append("build/fw.elf")  # the ELF only; no fw.bin for anybody
    return cmd


def elf_path(out, short):
    return os.path.join(out, "elf", short + ".sim.elf")


def _run(cmd, sp, **kw):
    r = sp.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    if r.returncode != 0:
        tail = (r.stderr or b"").decode("utf-8", "replace")[-800:]
        raise BuildError("%s failed (%d): %s" % (cmd[0:4], r.returncode, tail))
    return (r.stdout or b"").decode("utf-8", "replace").strip()


def build(repo, sha, out, toolchain=None, sp=subprocess, keep_tmp=False,
          check_mark=True):
    """Build and return the path of <out>/elf/<short>.sim.elf."""
    repo = os.path.abspath(os.path.expanduser(repo))
    out = os.path.abspath(os.path.expanduser(out))
    if not sha or sha.startswith("-") or not re.match(r"^[\w./^~-]+$", sha):
        raise BuildError("bad ref %r" % sha)
    rev, short_cmd = resolve_cmds(repo, sha)
    full = _run(rev, sp)
    short = _run(short_cmd, sp)
    if not SHA_RE.match(full) or not SHA_RE.match(short):
        raise BuildError("bad sha from git: %r %r" % (full, short))
    if toolchain is None:
        toolchain = find_toolchain()
    tmpdir = tempfile.mkdtemp(prefix="lw-fleet-build-")
    try:
        archive, untar = export_cmds(repo, full, tmpdir)
        a = sp.Popen(archive, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            t = sp.run(untar, stdin=a.stdout, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
        finally:
            if a.stdout:
                a.stdout.close()
            a_rc = a.wait()
        if a_rc != 0 or t.returncode != 0:
            raise BuildError("git archive | tar failed (%s, %s)" % (a_rc, t.returncode))
        _run(make_cmd(tmpdir, short, toolchain), sp)
        src = os.path.join(tmpdir, "build", "fw.elf")
        if check_mark:
            with open(src, "rb") as f:
                if SIM_MARK not in f.read():
                    raise BuildError("build/fw.elf has no %r banner; refusing it" % SIM_MARK)
        dst = elf_path(out, short)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        part = dst + ".part"
        shutil.copyfile(src, part)
        os.replace(part, dst)
        return dst
    finally:
        if not keep_tmp:
            shutil.rmtree(tmpdir, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=os.environ.get("LW_FW_REPO", "~/bench-fw"))
    ap.add_argument("--sha", default="HEAD")
    ap.add_argument("--out", default="~/lw-fleet")
    ap.add_argument("--toolchain", default=None,
                    help="TOOLCHAIN= prefix (default: PATH, else ~/opt/arm-gnu-toolchain-*)")
    a = ap.parse_args(argv)
    try:
        path = build(a.repo, a.sha, a.out, a.toolchain)
    except BuildError as e:
        print("build failed: %s" % e, file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
