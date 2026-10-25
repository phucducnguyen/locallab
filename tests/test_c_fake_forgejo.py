"""A fake Forgejo on 127.0.0.1 for lane C tests, and tests of the fake itself.

Implements only what tracker/ uses, with the behaviour the spec found on the
real 16.0.6: search lags behind a create and matches loosely (title or body),
and the write:issue bench token is refused repo creation.

Code repos (stage C3): each created repo is also a bare git repo served over
smart HTTP by the real `git http-backend`, with Basic auth (user:token) as
Forgejo takes tokens on its git side. Push and pull requests need the
write:repository scope, as on Forgejo (pulls are in the repository scope
category, not issue), so the bench token is refused both.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SETUP_TOKEN = "fake-setup-token"
BENCH_TOKEN = "fake-bench-token"
SCOPES = {SETUP_TOKEN: {"write:user", "write:repository", "write:issue"},
          BENCH_TOKEN: {"write:issue"}}
OWNER = "labwatch"


class FakeForgejo:
    def __init__(self, search_lag=1.0, with_repo=False):
        self.search_lag = search_lag
        self.git_root = tempfile.mkdtemp(prefix="lw-fake-forgejo-")
        self.auth_seen = []    # Authorization header values seen on git routes
        self.lock = threading.Lock()
        self.repos = {}        # name -> {"labels": [...], "issues": [...]}
        self.calls = []        # (method, path, auth scheme ok)
        self.fail_next = []    # HTTP codes to answer the next calls with
        self.drop_reply_once = False  # create the issue, then close without a reply
        self.drop_comment_reply_once = False  # post the comment, then close without a reply
        self.on_create = None  # called after an issue is stored, before the reply
        if with_repo:
            self._new_repo("bench-1")
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.srv.daemon_threads = True
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.git_root, ignore_errors=True)

    def bare(self, name):
        """Path of the bare git repo behind repo <name> (created with the repo)."""
        return os.path.join(self.git_root, name + ".git")

    def _new_repo(self, name):
        self.repos[name] = {"labels": [], "issues": [], "pulls": []}
        subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", self.bare(name)],
                       check=True)

    def pulls(self, repo="bench-fw"):
        with self.lock:
            return [dict(p) for p in self.repos.get(repo, {}).get("pulls", [])]

    def issues(self, repo="bench-1"):
        with self.lock:
            return [dict(i) for i in self.repos[repo]["issues"]]

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj=None):
                data = b"" if obj is None else json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _scopes(self):
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("token "):
                    return None
                return SCOPES.get(auth[len("token "):])

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def _git(self, method, name, sub, query):
                """Smart HTTP through the real git http-backend, Basic auth only."""
                auth = self.headers.get("Authorization", "")
                fake.auth_seen.append(auth)
                scopes = None
                if auth.startswith("Basic "):
                    try:
                        user, _, tok = base64.b64decode(auth[6:]).decode().partition(":")
                        scopes = SCOPES.get(tok) if user == OWNER else None
                    except (ValueError, UnicodeDecodeError):
                        scopes = None
                if scopes is None:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="fake"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                if name not in fake.repos:
                    return self._send(404, {"message": "no repo"})
                push = "receive-pack" in sub or "service=git-receive-pack" in query
                if push and "write:repository" not in scopes:
                    return self._send(403, {"message": "token does not have scope"})
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    body = b""
                    while True:
                        n = int(self.rfile.readline().strip() or b"0", 16)
                        if n == 0:
                            self.rfile.readline()
                            break
                        body += self.rfile.read(n)
                        self.rfile.readline()
                else:
                    body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                       "GIT_PROJECT_ROOT": fake.git_root, "GIT_HTTP_EXPORT_ALL": "1",
                       "PATH_INFO": "/%s.git%s" % (name, sub), "REQUEST_METHOD": method,
                       "QUERY_STRING": query, "REMOTE_USER": OWNER, "REMOTE_ADDR": "127.0.0.1",
                       "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                       "CONTENT_LENGTH": str(len(body))}
                if self.headers.get("Content-Encoding"):
                    env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
                if self.headers.get("Git-Protocol"):
                    env["GIT_PROTOCOL"] = self.headers["Git-Protocol"]
                out = subprocess.run(["git", "http-backend"], input=body, env=env,
                                     capture_output=True).stdout
                head, _, data = out.partition(b"\r\n\r\n")
                if not _:
                    head, _, data = out.partition(b"\n\n")
                status, headers = 200, []
                for ln in head.decode("latin-1").splitlines():
                    k, _, v = ln.partition(":")
                    if k.lower() == "status":
                        status = int(v.split()[0])
                    elif k:
                        headers.append((k, v.strip()))
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return None

            def _route(self, method):
                u = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                path = u.path
                fake.calls.append((method, path, q))
                g = re.fullmatch(r"/%s/([^/]+)\.git(/.*)" % OWNER, path)
                if g:
                    return self._git(method, g.group(1), g.group(2), u.query)
                if path == "/api/v1/version" and method == "GET":
                    return self._send(200, {"version": "16.0.6+fake"})
                scopes = self._scopes()
                if scopes is None:
                    return self._send(401, {"message": "token is required"})
                if fake.fail_next:
                    return self._send(fake.fail_next.pop(0), {"message": "injected"})
                if path == "/api/v1/user/repos" and method == "POST":
                    if "write:repository" not in scopes:
                        return self._send(403, {"message": "token does not have scope"})
                    name = self._body()["name"]
                    with fake.lock:
                        if name in fake.repos:
                            return self._send(409, {"message": "repo exists"})
                        fake._new_repo(name)
                    return self._send(201, {"name": name, "full_name": "%s/%s" % (OWNER, name)})
                m = re.fullmatch(r"/api/v1/repos/%s/([^/]+)(/.*)?" % OWNER, path)
                if not m or m.group(1) not in fake.repos:
                    return self._send(404, {"message": "not found"})
                repo = fake.repos[m.group(1)]
                rest = m.group(2) or ""
                if rest == "" and method == "GET":
                    return self._send(200, {"name": m.group(1)})
                if rest == "/labels":
                    if method == "GET":
                        with fake.lock:
                            return self._send(200, list(repo["labels"]))
                    if method == "POST":
                        b = self._body()
                        with fake.lock:
                            lb = {"id": max([x["id"] for x in repo["labels"]] + [0]) + 1, "name": b["name"],
                                  "color": b.get("color", "")}
                            repo["labels"].append(lb)
                        return self._send(201, lb)
                if rest == "/pulls":
                    if "write:repository" not in scopes:
                        return self._send(403, {"message": "token does not have scope"})
                    if method == "GET":
                        with fake.lock:
                            return self._send(200, [p for p in repo["pulls"]
                                                    if q.get("state", "open") in
                                                    ("all", p["state"])])
                    if method == "POST":
                        b = self._body()
                        bare = fake.bare(m.group(1))
                        for ref in (b.get("head"), b.get("base")):
                            ok = subprocess.run(["git", "--git-dir", bare, "rev-parse",
                                                 "--verify", "--quiet", "refs/heads/%s" % ref],
                                                capture_output=True).returncode == 0
                            if not ref or not ok:
                                return self._send(404, {"message": "no branch"})
                        with fake.lock:
                            n = len(repo["issues"]) + len(repo["pulls"]) + 1
                            pr = {"number": n, "title": b["title"], "body": b.get("body", ""),
                                  "head": {"ref": b["head"]}, "base": {"ref": b["base"]},
                                  "state": "open", "html_url": "%s/%s/%s/pulls/%d"
                                  % (fake.base, OWNER, m.group(1), n)}
                            repo["pulls"].append(pr)
                        return self._send(201, pr)
                if "write:issue" not in scopes:
                    return self._send(403, {"message": "token does not have scope"})
                if rest == "/issues" and method == "POST":
                    b = self._body()
                    with fake.lock:
                        labels = [lb for lb in repo["labels"] if lb["id"] in b.get("labels", [])]
                        if len(labels) != len(b.get("labels", [])):
                            return self._send(422, {"message": "bad label"})
                        issue = {"number": len(repo["issues"]) + 1, "title": b["title"],
                                 "body": b.get("body", ""), "state": "open", "labels": labels,
                                 "comments": [], "created": time.monotonic()}
                        repo["issues"].append(issue)
                        drop, fake.drop_reply_once = fake.drop_reply_once, False
                    if fake.on_create is not None:
                        fake.on_create(issue)
                    if drop:
                        self.close_connection = True
                        return None
                    return self._send(201, _public(issue))
                if rest == "/issues" and method == "GET":
                    return self._send(200, fake._search(repo, q))
                m2 = re.fullmatch(r"/issues/(\d+)(/comments)?", rest)
                if m2:
                    n = int(m2.group(1))
                    with fake.lock:
                        if not 1 <= n <= len(repo["issues"]):
                            return self._send(404, {"message": "no issue"})
                        issue = repo["issues"][n - 1]
                        if m2.group(2) and method == "POST":
                            issue["comments"].append(self._body()["body"])
                            drop, fake.drop_comment_reply_once = \
                                fake.drop_comment_reply_once, False
                            if drop:
                                self.close_connection = True
                                return None
                            return self._send(201, {"body": issue["comments"][-1]})
                        if m2.group(2) and method == "GET":
                            return self._send(200, [{"body": c} for c in issue["comments"]])
                        if not m2.group(2) and method == "PATCH":
                            b = self._body()
                            if "state" in b:
                                issue["state"] = b["state"]
                            return self._send(201, _public(issue))
                return self._send(404, {"message": "no route"})

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def do_PATCH(self):
                self._route("PATCH")

        return H

    def _search(self, repo, q):
        now = time.monotonic()
        state = q.get("state", "open")
        terms = [t for t in re.split(r"[^a-z0-9]+", q.get("q", "").lower()) if t]
        want_labels = [x for x in q.get("labels", "").split(",") if x]
        out = []
        with self.lock:
            for i in repo["issues"]:
                if q.get("q") and now - i["created"] < self.search_lag:
                    continue  # the indexer has not seen it yet
                if state != "all" and i["state"] != state:
                    continue
                hay = (i["title"] + " " + i["body"]).lower()
                if not all(t in hay for t in terms):  # loose: substrings, title or body
                    continue
                names = {lb["name"] for lb in i["labels"]}
                if not all(x in names for x in want_labels):
                    continue
                out.append(_public(i))
        return out


def _public(issue):
    return {k: issue[k] for k in ("number", "title", "body", "state", "labels")}


def _req(base, method, path, token=None, body=None):
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(base + path, data=data, method=method)
    if token:
        r.add_header("Authorization", "token " + token)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        e.close()
        return e.code, None


class FakeForgejoTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeForgejo(search_lag=0.6).start()
        self.b = self.fake.base

    def tearDown(self):
        self.fake.stop()

    def test_bench_token_cannot_create_repo(self):
        self.assertEqual(_req(self.b, "POST", "/api/v1/user/repos", BENCH_TOKEN,
                              {"name": "bench-1"})[0], 403)
        self.assertEqual(_req(self.b, "POST", "/api/v1/user/repos", SETUP_TOKEN,
                              {"name": "bench-1"})[0], 201)

    def test_no_token_401(self):
        self.assertEqual(_req(self.b, "GET", "/api/v1/repos/labwatch/bench-1/issues")[0], 401)

    def test_search_lags_and_is_fuzzy(self):
        _req(self.b, "POST", "/api/v1/user/repos", SETUP_TOKEN, {"name": "bench-1"})
        _req(self.b, "POST", "/api/v1/repos/labwatch/bench-1/issues", BENCH_TOKEN,
             {"title": "[lw-12] x", "body": "on bench-1", "labels": []})
        path = "/api/v1/repos/labwatch/bench-1/issues?state=all&type=issues&q=lw-1"
        self.assertEqual(_req(self.b, "GET", path, BENCH_TOKEN)[1], [])
        time.sleep(0.7)
        found = _req(self.b, "GET", path, BENCH_TOKEN)[1]
        self.assertEqual([i["title"] for i in found], ["[lw-12] x"])  # lw-1 matches lw-12
        body_hit = _req(self.b, "GET", path.replace("lw-1", "bench-1"), BENCH_TOKEN)[1]
        self.assertEqual(len(body_hit), 1)


if __name__ == "__main__":
    unittest.main()
