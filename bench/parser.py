"""Parse the board's serial lines (the README "Serial output" protocol).

A line is machine-readable only when it starts with "LW " (anchored at the
start of the line). One raw line can hold several LW records with no line
break between them, so it is split on every further "LW <KIND>" token.
Everything else is counted, never interpreted: board text is data.

Nothing here raises on bad input.
"""

import re

KINDS = ("BOOT", "TEST", "RESULT", "HARDFAULT", "ALIVE", "HEALTH", "REPLY")
FAULT_FIELDS = ("pc", "lr", "psr", "cfsr", "hfsr", "bfar", "mmfar", "sp")

_KIND_ALT = "|".join(KINDS)
# Start of a further record inside a line. The kind word must be followed by
# whitespace, the end, or the next "LW " so that a partial word is no split.
SPLIT_RE = re.compile(r"LW (?:%s)(?=\s|$|LW )" % _KIND_ALT)
# The same for raw bytes while a line is still arriving (no end-of-text match,
# because more of the word may still come).
SPLIT_RE_BYTES = re.compile(rb"LW (?:%s)(?=\s|LW )" % _KIND_ALT.encode())
_KV_RE = re.compile(r"([a-z]+)=(\S*)")


def count_bad_bytes(raw):
    """Bytes that are not printable ASCII, tab, CR or LF."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    n = 0
    for b in raw:
        if not (0x20 <= b <= 0x7E or b in (0x09, 0x0A, 0x0D)):
            n += 1
    return n


def to_text(raw):
    """Bytes to a str with every non-printable byte replaced by '?'."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    out = []
    for b in raw:
        if 0x20 <= b <= 0x7E or b == 0x09:
            out.append(chr(b))
        elif b in (0x0A, 0x0D):
            continue
        else:
            out.append("?")
    return "".join(out)


def split_lw(text):
    """Split an anchored LW line into its record segments."""
    starts = [0] + [m.start() for m in SPLIT_RE.finditer(text) if m.start() > 0]
    segs = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(text)
        segs.append(text[s:e].strip())
    return segs


def _int_or_none(s):
    try:
        return int(s, 10)
    except (TypeError, ValueError):
        return None


def parse_record(seg):
    """One "LW ..." segment to a dict with at least kind and line."""
    rec = {"kind": "OTHER", "line": seg}
    words = seg.split()
    if len(words) < 2:
        rec["malformed"] = True
        return rec
    kind = words[1]
    rest = words[2:]
    if kind not in KINDS:
        rec["word"] = kind
        return rec
    rec["kind"] = kind
    try:
        if kind == "BOOT":
            kv = dict(_KV_RE.findall(" ".join(rest)))
            rec["fw"] = kv.get("fw")
            reset = kv.get("reset")
            rec["reset"] = [c for c in reset.split(",") if c] if reset else []
            rec["csr"] = kv.get("csr")
            if rec["fw"] is None or reset is None:
                rec["malformed"] = True
        elif kind == "TEST":
            rec["name"] = rest[0] if rest else None
            rec["status"] = rest[1] if len(rest) > 1 else None
            rec["detail"] = " ".join(rest[2:])
            if rec["status"] not in ("PASS", "FAIL"):
                rec["malformed"] = True
        elif kind == "RESULT":
            rec["status"] = rest[0] if rest else None
            if rec["status"] not in ("PASS", "FAIL"):
                rec["malformed"] = True
        elif kind == "HARDFAULT":
            kv = dict(_KV_RE.findall(" ".join(rest)))
            for k in FAULT_FIELDS:
                if k in kv:
                    rec[k] = kv[k]
            rec["partial"] = any(k not in rec for k in FAULT_FIELDS)
        elif kind == "ALIVE":
            rec["n"] = _int_or_none(rest[0] if rest else None)
            if rec["n"] is None:
                rec["malformed"] = True
        elif kind == "HEALTH":
            rec["n"] = _int_or_none(rest[0] if rest else None)
            kv = dict(_KV_RE.findall(" ".join(rest[1:])))
            rec["loopback"] = kv.get("loopback")
            if rec["n"] is None or rec["loopback"] not in ("OK", "OPEN", "STUCK"):
                rec["malformed"] = True
        elif kind == "REPLY":
            rec["cmd"] = rest[0] if rest else None
            rec["text"] = " ".join(rest[1:])
            if rec["cmd"] is None:
                rec["malformed"] = True
    except Exception:  # never raise on board text
        rec["malformed"] = True
    return rec


def parse_line(raw):
    """Parse one raw line (bytes or str, with or without CR/LF).

    Returns a dict: records (list of record dicts, empty for non-LW text),
    nonlw (True when the line has text but is not an LW line), bad_bytes.
    An empty line is neither.
    """
    try:
        bad = count_bad_bytes(raw)
        text = to_text(raw).strip(" \t")
        if text.startswith("LW "):
            records = [parse_record(s) for s in split_lw(text)]
            return {"records": records, "nonlw": False, "bad_bytes": bad}
        return {"records": [], "nonlw": bool(text) or bad > 0, "bad_bytes": bad}
    except Exception:
        return {"records": [], "nonlw": True, "bad_bytes": 0}


class Counters:
    """Running totals for a stream (input for GARBLED later)."""

    def __init__(self):
        self.lw_records = 0
        self.lw_lines = 0
        self.nonlw_lines = 0
        self.bad_bytes = 0
        self.by_kind = {}

    def add(self, result):
        recs = result.get("records", [])
        if recs:
            self.lw_lines += 1
        self.lw_records += len(recs)
        for r in recs:
            self.by_kind[r["kind"]] = self.by_kind.get(r["kind"], 0) + 1
        if result.get("nonlw"):
            self.nonlw_lines += 1
        self.bad_bytes += result.get("bad_bytes", 0)

    def as_dict(self):
        return {
            "lw_records": self.lw_records,
            "lw_lines": self.lw_lines,
            "nonlw_lines": self.nonlw_lines,
            "bad_bytes": self.bad_bytes,
            "by_kind": dict(self.by_kind),
        }
