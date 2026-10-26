"""Decode an LW HARDFAULT line: fault status bits by name, and PC/LR to file:line.

The bit table is the ARMv7-M Architecture Reference Manual's (B3.2.15 CFSR,
B3.2.16 HFSR), written from the architecture, not from any board output:

    CFSR = UFSR[31:16] | BFSR[15:8] | MMFSR[7:0]

BFAR is a fault address only while CFSR.BFARVALID (bit 15) is set, MMFAR only
while CFSR.MMARVALID (bit 7) is set; otherwise those registers hold whatever
they held before, so they are reported as "not valid" and never quoted.

addr2line runs "arm-none-eabi-addr2line -f -e elf/<sha>.elf" on the stacked
PC and on the stacked LR minus 1 (LR carries the Thumb bit; minus 1 gives the
return address, which addr2line attributes to the calling line). An LR that
is an EXC_RETURN value (0xFFFFFFxx) is no code address and is not looked up.

Pure functions: board text is data; nothing here raises on bad input.

Bench hook:
    ev = fault_evidence(hardfault_record, elf_path(elf_dir, sha))
    evidence.update(ev)     # keys "fault", "addr2line", "file_line"
"""

import glob
import os
import re
import shutil
import subprocess

ADDR2LINE = "arm-none-eabi-addr2line"
ADDR2LINE_TIMEOUT_S = 5.0

# (bit, name, meaning) per sub-register; bit numbers are CFSR / HFSR bit positions.
MMFSR_BITS = (
    (0, "IACCVIOL", "instruction fetch from a location the MPU or XN forbids"),
    (1, "DACCVIOL", "data access to a location the MPU forbids"),
    (3, "MUNSTKERR", "MemManage fault on exception-return unstacking"),
    (4, "MSTKERR", "MemManage fault on exception-entry stacking"),
    (5, "MLSPERR", "MemManage fault during lazy FP state preservation"),
    (7, "MMARVALID", "MMFAR holds the faulting address"),
)
BFSR_BITS = (
    (8, "IBUSERR", "bus fault on an instruction fetch"),
    (9, "PRECISERR", "precise data bus error; the stacked PC is the faulting instruction"),
    (10, "IMPRECISERR", "imprecise data bus error; the stacked PC is after the access"),
    (11, "UNSTKERR", "bus fault on exception-return unstacking"),
    (12, "STKERR", "bus fault on exception-entry stacking"),
    (13, "LSPERR", "bus fault during lazy FP state preservation"),
    (15, "BFARVALID", "BFAR holds the faulting address"),
)
UFSR_BITS = (
    (16, "UNDEFINSTR", "undefined instruction"),
    (17, "INVSTATE", "invalid EPSR state (e.g. a branch to an address with bit 0 clear)"),
    (18, "INVPC", "invalid PC load on exception return (bad EXC_RETURN)"),
    (19, "NOCP", "coprocessor access while the coprocessor is absent or disabled"),
    (24, "UNALIGNED", "unaligned access with CCR.UNALIGN_TRP set"),
    (25, "DIVBYZERO", "divide by zero with CCR.DIV_0_TRP set"),
)
HFSR_BITS = (
    (1, "VECTTBL", "bus fault reading the vector table"),
    (30, "FORCED", "a configurable fault escalated to HardFault (see CFSR)"),
    (31, "DEBUGEVT", "debug event while halting debug is off"),
)
CFSR_GROUPS = (("MMFSR", MMFSR_BITS, 0x000000FF), ("BFSR", BFSR_BITS, 0x0000FF00),
               ("UFSR", UFSR_BITS, 0xFFFF0000))
BFARVALID = 1 << 15
MMARVALID = 1 << 7
# Status-only bits: they qualify a fault, they are not one.
_FLAG_BITS = ("MMARVALID", "BFARVALID")
EPSR_T = 1 << 24

REG_FIELDS = ("pc", "lr", "psr", "cfsr", "hfsr", "bfar", "mmfar", "sp")
_HEX_RE = re.compile(r"(?:0[xX])?([0-9a-fA-F]{1,8})")


def parse_reg(s):
    """'0x1234abcd' (or bare hex) to int; None when missing or not a 32-bit hex value."""
    if isinstance(s, int):
        return s if 0 <= s <= 0xFFFFFFFF else None
    if not isinstance(s, str):
        return None
    m = _HEX_RE.fullmatch(s.strip())
    return int(m.group(1), 16) if m else None


def hex32(v):
    return None if v is None else "0x%08x" % v


def _bits(value, table):
    out = []
    for bit, name, meaning in table:
        if value & (1 << bit):
            out.append({"bit": bit, "name": name, "meaning": meaning})
    return out


def _reserved(value, table, mask):
    known = 0
    for bit, _, _ in table:
        known |= 1 << bit
    return [b for b in range(32) if (mask & ~known) & value & (1 << b)]


def decode_fault(fields):
    """Decode a HARDFAULT record (parser dict or any mapping of field -> hex string).

    Returns a JSON-able dict: regs (normalized hex or None), missing, invalid,
    cfsr {MMFSR, BFSR, UFSR: [bit dicts], reserved: [bit numbers]}, hfsr
    {bits, reserved}, names (every set bit by name), bfar / mmfar (the fault
    address, or None) with bfar_note / mmfar_note, thumb (EPSR.T, or None),
    and summary (one line).
    """
    fields = fields or {}
    regs, missing, invalid = {}, [], []
    for k in REG_FIELDS:
        raw = fields.get(k) if hasattr(fields, "get") else None
        v = parse_reg(raw)
        if raw is None:
            missing.append(k)
        elif v is None:
            invalid.append(k)
        regs[k] = v
    out = {"regs": {k: hex32(v) for k, v in regs.items()}, "missing": missing,
           "invalid": invalid, "cfsr": None, "hfsr": None, "names": []}
    cfsr, hfsr = regs["cfsr"], regs["hfsr"]
    names = []
    if hfsr is not None:
        hb = _bits(hfsr, HFSR_BITS)
        out["hfsr"] = {"bits": hb, "reserved": _reserved(hfsr, HFSR_BITS, 0xFFFFFFFF)}
        names += [b["name"] for b in hb]
    if cfsr is not None:
        c = {}
        for group, table, mask in CFSR_GROUPS:
            c[group] = _bits(cfsr, table)
            names += [b["name"] for b in c[group]]
        c["reserved"] = sorted(sum((_reserved(cfsr, t, m) for _, t, m in CFSR_GROUPS), []))
        out["cfsr"] = c
    out["names"] = names
    for reg, flag, flag_name in (("bfar", BFARVALID, "BFARVALID"),
                                 ("mmfar", MMARVALID, "MMARVALID")):
        if cfsr is None:
            out[reg], note = None, "not valid (no CFSR to check %s)" % flag_name
        elif not cfsr & flag:
            out[reg], note = None, "not valid (%s clear)" % flag_name
        elif regs[reg] is None:
            out[reg], note = None, "%s set but %s missing" % (flag_name, reg.upper())
        else:
            out[reg], note = hex32(regs[reg]), "fault address (%s set)" % flag_name
        out[reg + "_note"] = note
    psr = regs["psr"]
    out["thumb"] = None if psr is None else bool(psr & EPSR_T)
    out["summary"] = _summary(out)
    return out


def _summary(d):
    parts = []
    hf = [b["name"] for b in (d["hfsr"] or {}).get("bits", [])]
    if hf:
        parts.append("HFSR " + "+".join(hf))
    if d["cfsr"] is not None:
        for group, _, _ in CFSR_GROUPS:
            ns = [b["name"] for b in d["cfsr"][group] if b["name"] not in _FLAG_BITS]
            if ns:
                parts.append("%s %s" % (group, "+".join(ns)))
    if not parts:
        if d["cfsr"] is None and d["hfsr"] is None:
            parts.append("no CFSR/HFSR in the line")
        else:
            parts.append("no fault status bits set")
    if d["bfar"]:
        parts.append("BFAR=%s" % d["bfar"])
    if d["mmfar"]:
        parts.append("MMFAR=%s" % d["mmfar"])
    if d["thumb"] is False:
        parts.append("EPSR.T=0")
    for k in ("pc", "lr"):
        if d["regs"][k] is not None:
            parts.append("%s=%s" % (k, d["regs"][k]))
    return "; ".join(parts)


# ---- addr2line ----

def is_exc_return(v):
    return v is not None and (v & 0xFFFFFF00) == 0xFFFFFF00


def elf_path(elf_dir, sha):
    """elf/<sha>.elf for this sha (exact name, else one whose name prefixes or extends sha)."""
    if not sha or not elf_dir:
        return None
    p = os.path.join(elf_dir, "%s.elf" % sha)
    if os.path.isfile(p):
        return p
    hits = []
    for q in glob.glob(os.path.join(glob.escape(elf_dir), "*.elf")):
        name = os.path.basename(q)[:-4]
        if len(name) >= 7 and (name.startswith(sha) or sha.startswith(name)):
            hits.append(q)
    return hits[0] if len(hits) == 1 else None


def _loc(addr_s, func, fl, root):
    func = (func or "").strip()
    fl = (fl or "").strip()
    fl = re.sub(r"\s*\(discriminator \d+\)$", "", fl)
    path, _, line = fl.rpartition(":")
    if not path:
        path, line = fl, ""
    if root and os.path.isabs(path):
        try:
            rel = os.path.relpath(path, root)
            if not rel.startswith(".."):
                path = rel
        except ValueError:
            pass
    known_file = bool(path) and path != "??"
    known_line = line.isdigit() and line != "0"
    return {"addr": addr_s,
            "function": func if func and func != "??" else None,
            "file": path if known_file else None,
            "line": int(line) if known_file and known_line else None,
            "file_line": "%s:%s" % (path, line) if known_file and known_line else None}


def _empty(addr_s, note=None):
    d = {"addr": addr_s, "function": None, "file": None, "line": None, "file_line": None}
    if note:
        d["note"] = note
    return d


def addr2line(elf, pc, lr, tool=None, runner=subprocess.run, root=None):
    """Look up the stacked PC and LR-1 in elf. pc/lr are ints or hex strings.

    Returns {"pc": loc, "lr": loc, "file_line": best, "function": best,
    "error": None or text}; loc = {addr, function, file, line, file_line}.
    Missing fields, a missing ELF or a missing tool give None values, never
    an exception. "best" is the PC's location when known, else the LR's.
    """
    pcv, lrv = parse_reg(pc), parse_reg(lr)
    out = {"elf": os.path.basename(elf) if elf else None, "error": None,
           "pc": _empty(hex32(pcv), None if pcv is not None else "no pc"),
           "lr": _empty(hex32(lrv), None if lrv is not None else "no lr"),
           "file_line": None, "function": None}
    queries = []
    if pcv is not None:
        queries.append(("pc", pcv))
    if lrv is not None:
        if is_exc_return(lrv):
            out["lr"]["note"] = "EXC_RETURN value, not a code address"
        elif lrv == 0:
            out["lr"]["note"] = "lr is 0"
        else:
            queries.append(("lr", lrv - 1))
    if not elf or not os.path.isfile(elf):
        out["error"] = "no ELF for this sha"
        return out
    exe = tool or shutil.which(ADDR2LINE)
    if not exe:
        out["error"] = "%s not found" % ADDR2LINE
        return out
    if not queries:
        return out
    cmd = [exe, "-f", "-e", elf] + ["0x%x" % a for _, a in queries]
    try:
        p = runner(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, timeout=ADDR2LINE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        out["error"] = "%s failed: %s" % (ADDR2LINE, e.__class__.__name__)
        return out
    text = p.stdout.decode("utf-8", "replace") if isinstance(p.stdout, bytes) else p.stdout
    lines = (text or "").splitlines()
    if p.returncode != 0 or len(lines) < 2 * len(queries):
        out["error"] = "%s exit %s, %d lines" % (ADDR2LINE, p.returncode, len(lines))
    for i, (key, a) in enumerate(queries):
        if 2 * i + 1 < len(lines):
            note = out[key].get("note")
            out[key] = _loc(hex32(a), lines[2 * i], lines[2 * i + 1], root)
            if note:
                out[key]["note"] = note
    for key in ("pc", "lr"):
        if out["file_line"] is None and out[key]["file_line"]:
            out["file_line"] = out[key]["file_line"]
            out["function"] = out[key]["function"]
    if out["function"] is None:
        out["function"] = out["pc"]["function"] or out["lr"]["function"]
    return out


def fault_evidence(fields, elf, tool=None, runner=subprocess.run, root=None):
    """The evidence keys for a HardFault: fault (decode_fault), addr2line, file_line."""
    d = decode_fault(fields)
    fields = fields or {}
    a = addr2line(elf, fields.get("pc"), fields.get("lr"), tool=tool, runner=runner, root=root)
    return {"fault": d, "addr2line": a, "file_line": a["file_line"]}


