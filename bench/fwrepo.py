"""Read-only git questions about the bench firmware repo (~/bench-fw, env LW_FW_REPO).

Every call is a list argv (no shell), with -C <repo>, a timeout, no pager,
no prompts. Refs from outside are checked by the caller against
^[0-9a-f]{7,40}$ before they get here, and are passed after --end-of-options.
Git output (commit messages, diffs) is data: returned, never acted on.
"""

import os
import subprocess

DEFAULT_REPO = "~/bench-fw"
MAIN = "refs/heads/main"
GIT_TIMEOUT_S = 10.0


def repo_path(path=None):
    return os.path.expanduser(path or os.environ.get("LW_FW_REPO") or DEFAULT_REPO)


def _env():
    env = dict(os.environ)
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_EXTERNAL_DIFF",
              "GIT_CONFIG_PARAMETERS"):
        env.pop(k, None)
    env.update({"GIT_PAGER": "cat", "PAGER": "cat", "GIT_TERMINAL_PROMPT": "0",
                "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"})
    return env


def git(repo, *args, timeout=GIT_TIMEOUT_S):
    """(returncode, stdout bytes); returncode -1 when git could not run."""
    try:
        p = subprocess.run(["git", "-C", repo] + list(args), stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout, env=_env())
    except (OSError, subprocess.TimeoutExpired):
        return -1, b""
    return p.returncode, p.stdout


def main_head(repo):
    """The full sha of main, or None."""
    rc, out = git(repo, "rev-parse", "--verify", "--quiet", MAIN)
    sha = out.decode("ascii", "replace").strip()
    return sha if rc == 0 and len(sha) == 40 else None


def resolve_commit(repo, ref):
    """The full sha of the commit that hex prefix `ref` names, or None.

    The result must start with ref (so a branch or tag that happens to be
    named like hex is never followed) and must be a commit object.
    """
    rc, out = git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", ref)
    sha = out.decode("ascii", "replace").strip()
    if rc != 0 or len(sha) != 40 or not sha.startswith(ref):
        return None
    rc, out = git(repo, "cat-file", "-t", sha)
    if rc != 0 or out.strip() != b"commit":
        return None
    return sha


def on_main(repo, sha):
    """True if full sha is main's head or an ancestor of it."""
    rc, _ = git(repo, "merge-base", "--is-ancestor", sha, MAIN)
    return rc == 0


def subject(repo, sha):
    rc, out = git(repo, "log", "-1", "--format=%s", sha)
    return out.decode("utf-8", "replace").strip() if rc == 0 else None


def show_src(repo, sha, max_lines):
    """(lines, truncated) of git show for src/ only, or None if git failed."""
    rc, out = git(repo, "show", "--no-decorate", "--no-ext-diff", "--no-color",
                  "--end-of-options", sha, "--", "src/")
    if rc != 0:
        return None
    lines = out.decode("utf-8", "replace").splitlines()
    return lines[:max_lines], len(lines) > max_lines
