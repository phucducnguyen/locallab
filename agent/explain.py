"""Ask the sandbox agent to explain one bench record, in a fresh session.

    python3 -m agent.explain --incident <id>
    python3 -m agent.explain --run <id>

Runs one agent turn with a new session id per call, prints the reply on
stdout, and exits 0 only when the agent finished in time and the reply's last
line is a DIAGNOSIS line in the interface's format. Exit 2 on a timeout
(default 55 s), a bad or missing final line, an agent failure or a bad config.
It posts nothing and talks to no network itself.

The agent command is config, not code: a JSON file at --config, else
$LW_AGENT_CONFIG, else ~/.config/labwatch/agent.json, with

    "agent_cmd": [argv tokens; {session}, {message}, {kind}, {id} are filled in]
    "timeout_s": 55
    "session_prefix": "lw-"
    "prompt": "... {kind} {id} ..."

If no token holds {message}, the message goes to the agent on stdin.
See agent/agent.example.json.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import uuid

DEFAULT_CONFIG = "~/.config/labwatch/agent.json"
DEFAULT_TIMEOUT_S = 55.0
DEFAULT_PROMPT = ("Use the labwatch skill. Explain Lab Watch {kind} {id}: fetch that record with "
                  "the skill's client and follow the skill's rules. End with the DIAGNOSIS line.")
REPLY_MAX = 64 * 1024

TRIAGES = ("HARDWARE", "CODE", "UNKNOWN", "NONE")
VERDICTS = ("PASS", "FAIL", "HARDFAULT", "RESET_LOOP", "NO_OUTPUT", "GARBLED", "FLASH_ERROR",
            "BUILD_ERROR")
KINDS = ("HEALTH_FAIL", "SILENT", "LINK_DOWN", "REBOOT", "HARDFAULT", "RUN_FAIL")
DIAGNOSIS_RE = re.compile(
    r"DIAGNOSIS: (?P<diagnosis>%s) \| VERDICT: (?P<verdict>%s) \| "
    r"COMMIT: (?P<commit>[0-9a-f]{7,40}|none) \| CAUSE: (?P<cause>[A-Za-z0-9_-]+)"
    % ("|".join(TRIAGES), "|".join(sorted(set(VERDICTS + KINDS), key=len, reverse=True))))

EXIT_OK = 0
EXIT_FAIL = 2


class ConfigError(Exception):
    pass


def parse_diagnosis(line):
    """The DIAGNOSIS line's fields as a dict, or None if it is not one."""
    m = DIAGNOSIS_RE.fullmatch(line.strip())
    return m.groupdict() if m else None


def last_line(text):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""


def clean_reply(raw):
    """Decode the agent's stdout; keep printable text, newlines and tabs only."""
    text = raw[:REPLY_MAX].decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    return "".join(c for c in text if c in "\n\t" or (c >= " " and c != "\x7f"))


def load_config(path=None):
    path = path or os.environ.get("LW_AGENT_CONFIG") or os.path.expanduser(DEFAULT_CONFIG)
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError("cannot read the agent config %s (%s)" % (path, e.__class__.__name__))
    cmd = cfg.get("agent_cmd") if isinstance(cfg, dict) else None
    if not isinstance(cmd, list) or not cmd or not all(isinstance(t, str) for t in cmd):
        raise ConfigError("agent_cmd must be a non-empty list of strings")
    if any(t.startswith("<") and t.endswith(">") for t in cmd):
        raise ConfigError("agent_cmd still holds placeholders from agent.example.json")
    try:
        timeout = float(cfg.get("timeout_s", DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        raise ConfigError("timeout_s must be a number")
    return {"agent_cmd": cmd, "timeout_s": timeout,
            "session_prefix": str(cfg.get("session_prefix", "lw-")),
            "prompt": str(cfg.get("prompt", DEFAULT_PROMPT))}


def fill(template, values):
    for k, v in values.items():
        template = template.replace("{%s}" % k, v)
    return template


def build_command(cfg, kind, rid, session=None):
    """(argv, message, session): a new session id on every call."""
    session = session or "%s%s-%s-%s" % (cfg["session_prefix"], kind, rid, uuid.uuid4().hex)
    values = {"kind": kind, "id": rid, "session": session}
    message = fill(cfg["prompt"], values)
    values["message"] = message
    argv = [fill(t, values) for t in cfg["agent_cmd"]]
    return argv, message, session


def run_agent(argv, message, timeout, stdin_message):
    """(returncode or None on timeout, stdout bytes)."""
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin_message else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, start_new_session=True)
    try:
        out, _ = proc.communicate(message.encode("utf-8") if stdin_message else None,
                                  timeout=timeout)
        return proc.returncode, out
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        out, _ = proc.communicate()
        return None, out


def explain(kind, rid, cfg, out=None, err=None):
    out = out or sys.stdout
    err = err or sys.stderr
    argv, message, session = build_command(cfg, kind, rid)
    stdin_message = not any("{message}" in t for t in cfg["agent_cmd"])
    print("explain: %s %s in session %s" % (kind, rid, session), file=err)
    try:
        rc, raw = run_agent(argv, message, cfg["timeout_s"], stdin_message)
    except OSError as e:
        print("explain: cannot start the agent command (%s)" % e.__class__.__name__, file=err)
        return EXIT_FAIL
    reply = clean_reply(raw or b"").strip("\n")
    if reply:
        print(reply, file=out)
    if rc is None:
        print("explain: no answer within %.0f s" % cfg["timeout_s"], file=err)
        return EXIT_FAIL
    if rc != 0:
        print("explain: the agent command exited %d" % rc, file=err)
        return EXIT_FAIL
    if parse_diagnosis(last_line(reply)) is None:
        print("explain: the reply's last line is not a DIAGNOSIS line", file=err)
        return EXIT_FAIL
    return EXIT_OK


def main(argv=None, out=None, err=None):
    err = err or sys.stderr
    ap = argparse.ArgumentParser(prog="python3 -m agent.explain",
                                 description="Explain one Lab Watch record with the sandbox agent")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--incident", metavar="ID")
    g.add_argument("--run", metavar="ID")
    ap.add_argument("--config", help="agent config JSON (default $LW_AGENT_CONFIG or %s)"
                    % DEFAULT_CONFIG)
    ap.add_argument("--timeout", type=float, help="seconds (overrides timeout_s)")
    args = ap.parse_args(argv)
    kind, rid = ("incident", args.incident) if args.incident is not None else ("run", args.run)
    if not re.fullmatch(r"[0-9]{1,9}", rid):
        print("explain: the %s id must be a number" % kind, file=err)
        return EXIT_FAIL
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print("explain: %s" % e, file=err)
        return EXIT_FAIL
    if args.timeout is not None:
        cfg["timeout_s"] = args.timeout
    return explain(kind, rid, cfg, out, err)


if __name__ == "__main__":
    sys.exit(main())
