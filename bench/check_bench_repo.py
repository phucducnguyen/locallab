"""Refuse to run on a bench firmware repo that could hold the answer key.

    python3 -m bench.check_bench_repo [--repo PATH]     (default $LW_FW_REPO or ~/bench-fw)

The bench builds only from its own clone (interface "The bench repo and the
answer key"). This check reads git metadata only, never file contents:

- `git for-each-ref`: refs/heads/main must be the only ref (no tags, no
  remotes, no other branches, no stash);
- `git symbolic-ref HEAD`: HEAD is refs/heads/main;
- `git ls-tree -r --name-only HEAD`: no path component is CASES.md, logs or
  bugs (any case). main keeps its history, so older commits still hold them:
  tree_is_clean(repo, sha) is the per-sha guard that build() (and later the
  endpoint's POST /run) applies before anything is exported. Names are only
  compared, never printed.

It never looks at ~/fw. Exit 0 when the repo passes, 1 otherwise.
"""

import argparse
import os
import subprocess
import sys

DEFAULT_REPO = "~/bench-fw"
MAIN = "refs/heads/main"
FORBIDDEN = ("CASES.md", "logs", "bugs")
GIT_TIMEOUT_S = 20


def repo_path(path=None):
    return os.path.expanduser(path or os.environ.get("LW_FW_REPO") or DEFAULT_REPO)


def _git(repo, *args):
    p = subprocess.run(["git", "-C", repo] + list(args), stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=GIT_TIMEOUT_S)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def _printable(s, cap=80):
    s = "".join(c if " " <= c < "\x7f" else "?" for c in s)
    return s if len(s) <= cap else s[:cap - 3] + "..."


def tree_is_clean(repo, sha):
    """(ok, why): sha's tree has no CASES.md, logs or bugs at any depth, any case."""
    try:
        rc, names = _git(repo, "ls-tree", "-r", "--name-only", "--end-of-options", sha)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, "git ls-tree could not run: %s" % e.__class__.__name__
    if rc != 0:
        return False, "git ls-tree %s failed" % _printable(sha, 40)
    bad = set(n.lower() for n in FORBIDDEN)
    for path in names.splitlines():
        if any(part.lower() in bad for part in path.split("/")):
            return False, ("the tree of %s holds answer-key paths (CASES.md, logs or bugs); "
                           "the bench never builds it" % _printable(sha, 40))
    return True, ""


def check(repo=None):
    """A list of problems (empty when the repo is fit for the bench)."""
    repo = repo_path(repo)
    if not os.path.isdir(repo):
        return ["bench repo %s does not exist" % repo]
    try:
        rc, top = _git(repo, "rev-parse", "--is-inside-work-tree")
        if rc != 0 or top.strip() != "true":
            return ["%s is not a git work tree" % repo]
        problems = []
        rc, refs = _git(repo, "for-each-ref", "--format=%(refname)")
        if rc != 0:
            return ["git for-each-ref failed in %s" % repo]
        refs = [r for r in refs.splitlines() if r]
        if MAIN not in refs:
            problems.append("no %s" % MAIN)
        extra = [r for r in refs if r != MAIN]
        if extra:
            problems.append("%d ref(s) besides main, e.g. %s (the bench repo holds main only)" % (
                len(extra), _printable(extra[0])))
        rc, head = _git(repo, "symbolic-ref", "-q", "HEAD")
        if rc != 0 or head.strip() != MAIN:
            problems.append("HEAD is not %s" % MAIN)
        rc, names = _git(repo, "ls-tree", "--name-only", "HEAD")
        if rc != 0:
            problems.append("git ls-tree HEAD failed")
        else:
            present = [n for n in names.splitlines() if n in FORBIDDEN]
            if present:
                problems.append("HEAD still has %s (remove them in a commit)" % ", ".join(present))
            else:
                ok, why = tree_is_clean(repo, "HEAD")
                if not ok:
                    problems.append("HEAD: %s" % why)
        return problems
    except (OSError, subprocess.TimeoutExpired) as e:
        return ["git could not run in %s: %s" % (repo, e.__class__.__name__)]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m bench.check_bench_repo")
    ap.add_argument("--repo", help="default $LW_FW_REPO or %s" % DEFAULT_REPO)
    a = ap.parse_args(argv)
    repo = repo_path(a.repo)
    problems = check(repo)
    if problems:
        for p in problems:
            print("bench repo REFUSED: %s" % p, file=sys.stderr)
        return 1
    _, head = _git(repo, "rev-parse", "--short=7", "HEAD")
    print("bench repo OK: %s (main only, main=%s)" % (repo, head.strip()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
