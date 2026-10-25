"""Check an explanation's citations against the record the endpoint returned.

    python3 -m agent.check_citations --explanation reply.txt --record record.json \
        [--show show.json]

Lists every sentence with no bracketed citation (noting whether it is marked
"likely") and every citation that does not resolve to a field of the record:

    [serial HH:MM:SS.mmm]   a raw_ts anywhere in the record
    [<register> <BIT>]      BIT in the record's <register>_bits list
    [<file>:<line>]         the record's file_line
    [diff <sha> +N]         sha names the record's commit (firmware/commit, either
                            one a prefix of the other); with --show, line N
                            (1-based) of that response's data.diff exists
    [incident N]            the record's incident id (id, incident_id)

Several citations may share one bracket, separated by ";". The DIAGNOSIS line
is not a sentence. Prints a JSON report; exit 0 when nothing is listed, else 1.
Lane D imports check(text, record, show=None).
"""

import argparse
import json
import re
import sys

SERIAL_RE = re.compile(r"serial (\d{2}:\d{2}:\d{2}\.\d{3})")
BIT_RE = re.compile(r"([a-z][a-z0-9]*) ([A-Z][A-Z0-9_]*)")
FILE_LINE_RE = re.compile(r"([A-Za-z0-9_./-]+\.[A-Za-z0-9]+):(\d+)")
DIFF_RE = re.compile(r"diff ([0-9a-f]{7,40}) \+(\d+)")
INCIDENT_RE = re.compile(r"incident (\d+)")
BRACKET_RE = re.compile(r"\[([^\[\]]*)\]")
SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?!\[)")
LIKELY_RE = re.compile(r"\blikely\b", re.IGNORECASE)


def walk(obj, path=()):
    """Yield (key path, value) for every leaf and container in a JSON value."""
    yield path, obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, path + (i,))


def facts(record):
    """What a citation may resolve to, collected from anywhere in the record."""
    f = {"raw_ts": set(), "bits": set(), "file_line": set(), "commit": set(), "incident": set()}
    if isinstance(record, dict) and "kind" in record and isinstance(record.get("id"), int):
        f["incident"].add(record["id"])
    for path, v in walk(record):
        key = path[-1] if path else None
        if key == "raw_ts" and isinstance(v, str):
            f["raw_ts"].add(v)
        elif isinstance(key, str) and key.endswith("_bits") and isinstance(v, list):
            reg = key[:-len("_bits")].lower()
            f["bits"].update((reg, b) for b in v if isinstance(b, str))
        elif key == "file_line" and isinstance(v, str):
            f["file_line"].add(v)
        elif key in ("firmware", "commit", "sha") and isinstance(v, str) \
                and re.fullmatch(r"[0-9a-f]{7,40}", v) and "last_pass" not in path:
            f["commit"].add(v)
        elif key == "incident_id" and isinstance(v, int):
            f["incident"].add(v)
    return f


def diff_lines(show):
    if not isinstance(show, dict):
        return None
    lines = (show.get("data") or {}).get("diff")
    return lines if isinstance(lines, list) else None


def resolve(cite, f, show=None):
    """None when the citation resolves, else the reason it does not."""
    m = SERIAL_RE.fullmatch(cite)
    if m:
        return None if m.group(1) in f["raw_ts"] else "no serial line with that raw_ts"
    m = DIFF_RE.fullmatch(cite)
    if m:
        sha, n = m.group(1), int(m.group(2))
        if not any(c.startswith(sha) or sha.startswith(c) for c in f["commit"]):
            return "not the record's commit"
        lines = diff_lines(show)
        if show is not None:
            ref = show.get("ref", "") if isinstance(show, dict) else ""
            if not (ref.startswith(sha) or sha.startswith(ref)):
                return "the diff given is for another sha"
            if lines is None or not 1 <= n <= len(lines):
                return "no line %d in the diff" % n
        return None
    m = INCIDENT_RE.fullmatch(cite)
    if m:
        return None if int(m.group(1)) in f["incident"] else "not this record's incident"
    m = FILE_LINE_RE.fullmatch(cite)
    if m:
        return None if cite in f["file_line"] else "not the record's file_line"
    m = BIT_RE.fullmatch(cite)
    if m:
        return None if (m.group(1), m.group(2)) in f["bits"] else "no such decoded bit"
    return "not a citation form"


def sentences(text):
    """(line number, sentence) for every sentence outside the DIAGNOSIS line and headings."""
    out = []
    for no, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("DIAGNOSIS:") or s.startswith("#") or s.startswith("```"):
            continue
        s = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", s).strip()
        s = re.sub(r"^\*\*[^*]+\*\*:?\s*", "", s).strip()  # a bold label such as "**Cause:**"
        if not s or (s.endswith(":") and not BRACKET_RE.search(s)):
            continue  # a section label such as "Next step:"
        out.extend((no, part.strip()) for part in SPLIT_RE.split(s) if part.strip())
    return out


def check(text, record, show=None):
    """{"ok", "sentences", "uncited": [...], "unresolved": [...]}."""
    f = facts(record)
    uncited, unresolved = [], []
    sents = sentences(text)
    for no, s in sents:
        cites = [c.strip() for b in BRACKET_RE.findall(s) for c in b.split(";") if c.strip()]
        if not cites:
            uncited.append({"line": no, "sentence": s, "likely": bool(LIKELY_RE.search(s))})
        for c in cites:
            why = resolve(c, f, show)
            if why:
                unresolved.append({"line": no, "citation": c, "why": why})
    return {"ok": not uncited and not unresolved, "sentences": len(sents),
            "uncited": uncited, "unresolved": unresolved}


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    # Accept the client's {"status", "body"} wrapper as well as the bare record.
    if isinstance(data, dict) and set(data) == {"status", "body"}:
        data = data["body"]
    return data


def main(argv=None, out=None):
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="python3 -m agent.check_citations")
    ap.add_argument("--explanation", required=True, help="text file, or - for stdin")
    ap.add_argument("--record", required=True, help="the record JSON from the endpoint")
    ap.add_argument("--show", help="the /show JSON for the cited commit")
    args = ap.parse_args(argv)
    if args.explanation == "-":
        text = sys.stdin.read()
    else:
        with open(args.explanation, encoding="utf-8") as fh:
            text = fh.read()
    report = check(text, load_json(args.record), load_json(args.show) if args.show else None)
    print(json.dumps(report, indent=1, sort_keys=True), file=out)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
