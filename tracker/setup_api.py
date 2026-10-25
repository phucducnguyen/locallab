"""The API half of tracker/setup_forgejo.sh: repo bench-1, its labels, the label ids.

    python3 -m tracker.setup_api [--secrets-dir DIR]

Uses the setup token (DIR/tracker_setup_token). Creates repo bench-1 and the
labels hardware, firmware, needs-triage only when missing, reads the label ids
back and writes DIR/tracker_labels.json only when its content changes. A
second run changes nothing. Never prints a token.
"""

import argparse
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker.forgejo import DEFAULT_REPO, Forgejo, TrackerError, secrets_dir  # noqa: E402

LABELS = {"hardware": "#d73a4a", "firmware": "#0075ca", "needs-triage": "#fbca04"}


def write_private(path, text):
    """Write text to path with mode 600 unless it already holds exactly that. True if written."""
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return False
    except FileNotFoundError:
        pass
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return True


def setup(fj, labels_path, log=print):
    owner, name = fj.repo.split("/", 1)
    if fj.repo_exists():
        log("repo %s: exists" % fj.repo)
    else:
        fj.create_repo(name)
        log("repo %s: created" % fj.repo)
    have = {lb.get("name"): lb.get("id") for lb in fj.labels()}
    for lname, color in LABELS.items():
        if lname in have:
            log("label %s: exists" % lname)
        else:
            fj.create_label(lname, color)
            log("label %s: created" % lname)
    have = {lb.get("name"): lb.get("id") for lb in fj.labels()}
    ids = {}
    for lname in LABELS:
        if not isinstance(have.get(lname), int):
            raise TrackerError("label %s missing after setup" % lname)
        ids[lname] = have[lname]
    changed = write_private(labels_path, json.dumps(ids, sort_keys=True) + "\n")
    log("%s: %s %s" % (labels_path, "written" if changed else "unchanged",
                       json.dumps(ids, sort_keys=True)))
    return ids


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--secrets-dir", default=secrets_dir())
    ap.add_argument("--repo", default=DEFAULT_REPO)
    args = ap.parse_args(argv)
    fj = Forgejo(os.path.join(args.secrets_dir, "tracker_setup_token"), repo=args.repo)
    try:
        setup(fj, os.path.join(args.secrets_dir, "tracker_labels.json"))
    except (TrackerError, OSError) as e:
        print("setup_api: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
