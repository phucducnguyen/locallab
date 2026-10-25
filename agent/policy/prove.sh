#!/usr/bin/env bash
# Show, from inside the sandbox, what the Lab Watch policy allows and refuses.
# Run on the GB10:  agent/policy/prove.sh [sandbox]
# Every refusal must be the proxy's own JSON ("error":"policy_denied"), not a
# DNS or no-route error: the GB10 has no internet, so only the policy's text
# proves the policy. HTTPS requests are sent as a raw CONNECT to the proxy so
# its JSON body is visible (curl only prints "CONNECT tunnel failed, 403").
set -uo pipefail
SANDBOX="${1:-labwatch}"
export PATH="$HOME/.local/bin:$PATH" NEMOCLAW_GATEWAY_PORT="${NEMOCLAW_GATEWAY_PORT:-8090}"

nemoclaw "$SANDBOX" exec --timeout 90 -- python3 -c '
import json, os, socket, urllib.parse, urllib.request
proxy = urllib.parse.urlsplit(os.environ.get("HTTPS_PROXY") or os.environ["HTTP_PROXY"])

def connect(host):
    s = socket.create_connection((proxy.hostname, proxy.port), timeout=10)
    s.sendall(("CONNECT %s:443 HTTP/1.1\r\nHost: %s:443\r\n\r\n" % (host, host)).encode())
    reply = s.recv(4096).decode("ascii", "replace")
    s.close()
    return reply.splitlines()[0] + " " + reply.split("\r\n\r\n", 1)[-1].strip()

def http(method, url):
    req = urllib.request.Request(url, method=method, data=b"{}" if method == "POST" else None)
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return "%d %s" % (r.status, r.read(200).decode("ascii", "replace"))
    except urllib.error.HTTPError as e:
        return "%d %s" % (e.code, e.read(300).decode("ascii", "replace"))
    except Exception as e:
        return "ERROR %s %s" % (type(e).__name__, e)

bench = "http://host.openshell.internal:8765"
print("allow  GET  /health        ->", http("GET", bench + "/health"))
print("deny   POST /patch         ->", http("POST", bench + "/patch"))
print("deny   GET  /shell         ->", http("GET", bench + "/shell"))
print("deny   https github.com    ->", connect("github.com"))
print("deny   https example.com   ->", connect("example.com"))
print("deny   https api.telegram.org ->", connect("api.telegram.org"))
print("deny   http  github.com    ->", http("GET", "http://github.com/"))
print("deny   http  example.com   ->", http("GET", "http://example.com/"))
'
echo "--- curl (not an allowed binary for the bench) is refused too:"
nemoclaw "$SANDBOX" exec --timeout 30 -- curl -sS --max-time 10 http://host.openshell.internal:8765/health
echo
