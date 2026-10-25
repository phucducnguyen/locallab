#!/usr/bin/env python3
"""Lab Watch client for the skill: one command per bench endpoint route.

    python3 labwatch.py [--config PATH] <command> [args]

Commands (one per route of the interface):
    health                  GET  /health
    board                   GET  /board
    incidents [--open]      GET  /incidents[?open=1]
    incident <id>           GET  /incidents/<id>
    runs                    GET  /runs
    run <id>                GET  /runs/<id>
    events [--since <id>]   GET  /events?since=<id>
    show <sha>              GET  /show?ref=<sha>     (a sha only, refused otherwise)
    post-run <sha>          POST /run {"sha"}        (side effect: audited, rate-limited)
    board-test <name>       POST /board/test {"name"} (one of the five self-tests)

The base URL and the bench token's file path come from a JSON config file
({"base_url": ..., "token_file": ...}), found at --config, else $LW_CLIENT_CONFIG,
else ~/.config/labwatch/client.json. The token value is never printed.

Prints the response JSON on stdout. Exit 0 on a 2xx answer, 1 on any other
HTTP status or a connection error, 2 when the request is refused before it is
sent (bad sha, bad id, unknown test name, bad config).

Standard library only, so it runs wherever python3 does.
"""

import argparse
import contextlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit

SHA_RE = re.compile(r"[0-9a-f]{7,40}")
ID_RE = re.compile(r"[0-9]{1,9}")
CURSOR_RE = re.compile(r"[0-9]{1,12}")
SELF_TESTS = ("crc32", "ringbuf", "systick", "cmd_dispatch", "loopback")
DEFAULT_CONFIG = "~/.config/labwatch/client.json"
DEFAULT_TIMEOUT_S = 10.0
RESPONSE_MAX = 1 << 20


class Refused(Exception):
    """A request the client will not send."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the bearer token goes to the bench and nowhere else."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def is_sha(ref):
    return isinstance(ref, str) and SHA_RE.fullmatch(ref) is not None


def load_config(path=None):
    path = path or os.environ.get("LW_CLIENT_CONFIG") or os.path.expanduser(DEFAULT_CONFIG)
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        raise Refused("cannot read the client config %s (%s)" % (path, e.__class__.__name__))
    if not isinstance(cfg, dict):
        raise Refused("the client config must be a JSON object")
    base = cfg.get("base_url")
    tok = cfg.get("token_file")
    if not isinstance(base, str) or urlsplit(base).scheme not in ("http", "https") \
            or not urlsplit(base).hostname:
        raise Refused("base_url in the client config must be an http(s) URL")
    if not isinstance(tok, str) or not tok:
        raise Refused("token_file in the client config must be a path")
    return {"base_url": base.rstrip("/"), "token_file": os.path.expanduser(tok),
            "timeout_s": float(cfg.get("timeout_s", DEFAULT_TIMEOUT_S)),
            "proxy": cfg.get("proxy", "env")}


def read_token(path):
    try:
        with open(path, encoding="utf-8") as f:
            tok = f.read().strip()
    except OSError as e:
        raise Refused("cannot read the token file (%s)" % e.__class__.__name__)
    if not tok or any(c < " " or c > "~" for c in tok):
        raise Refused("the token file is empty or not one printable line")
    return tok


def build(command, args):
    """Map one command to (method, path, body), refusing anything off-interface."""
    if command == "health":
        return "GET", "/health", None
    if command == "board":
        return "GET", "/board", None
    if command == "incidents":
        return "GET", "/incidents?open=1" if args.open else "/incidents", None
    if command in ("incident", "run"):
        if not ID_RE.fullmatch(args.id):
            raise Refused("the id must be a number")
        return "GET", ("/incidents/%s" if command == "incident" else "/runs/%s") % args.id, None
    if command == "runs":
        return "GET", "/runs", None
    if command == "events":
        if not CURSOR_RE.fullmatch(args.since):
            raise Refused("--since must be an event id")
        return "GET", "/events?since=%s" % args.since, None
    if command == "show":
        if not is_sha(args.sha):
            raise Refused("show takes a commit sha only (7 to 40 lowercase hex digits)")
        return "GET", "/show?ref=%s" % quote(args.sha, safe=""), None
    if command == "post-run":
        if not is_sha(args.sha):
            raise Refused("post-run takes a commit sha only (7 to 40 lowercase hex digits)")
        return "POST", "/run", {"sha": args.sha}
    if command == "board-test":
        if args.name not in SELF_TESTS:
            raise Refused("board-test takes one of: %s" % ", ".join(SELF_TESTS))
        return "POST", "/board/test", {"name": args.name}
    raise Refused("unknown command")


def request(cfg, method, path, body=None):
    """Send one request; return (status, parsed JSON or None)."""
    data = None
    req = urllib.request.Request(cfg["base_url"] + path, method=method)
    if path != "/health":
        req.add_header("Authorization", "Bearer " + read_token(cfg["token_file"]))
    if body is not None:
        data = json.dumps(body).encode("ascii")
        req.add_header("Content-Type", "application/json")
    handlers = [NoRedirect()]
    if cfg.get("proxy") == "none":
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, data=data, timeout=cfg["timeout_s"]) as resp:
            status, raw = resp.status, resp.read(RESPONSE_MAX)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read(RESPONSE_MAX)
        e.close()
    try:
        return status, json.loads(raw.decode("ascii", "replace")) if raw else None
    except ValueError:
        return status, {"error": "the response was not JSON"}


def parser():
    ap = argparse.ArgumentParser(prog="labwatch.py", description="Lab Watch bench client")
    ap.add_argument("--config", help="client config JSON (base_url, token_file)")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("health")
    sub.add_parser("board")
    p = sub.add_parser("incidents")
    p.add_argument("--open", action="store_true", help="only incidents not RESOLVED")
    sub.add_parser("incident").add_argument("id")
    sub.add_parser("runs")
    sub.add_parser("run").add_argument("id")
    sub.add_parser("events").add_argument("--since", default="0")
    sub.add_parser("show").add_argument("sha")
    sub.add_parser("post-run").add_argument("sha")
    sub.add_parser("board-test").add_argument("name")
    return ap


def main(argv=None, out=None, err=None):
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        with contextlib.redirect_stderr(err):
            args = parser().parse_args(argv)
    except SystemExit as e:  # usage errors (an option-like ref such as --x) send nothing
        return e.code if isinstance(e.code, int) else 2
    try:
        method, path, body = build(args.command, args)
        cfg = load_config(args.config)
        status, resp = request(cfg, method, path, body)
    except Refused as e:
        print("refused: %s" % e, file=err)
        return 2
    except (urllib.error.URLError, OSError) as e:
        print("cannot reach the bench (%s)" % e.__class__.__name__, file=err)
        return 1
    print(json.dumps({"status": status, "body": resp}, indent=1, sort_keys=True), file=out)
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
