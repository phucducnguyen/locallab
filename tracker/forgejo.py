"""A small Forgejo API client: the four issue calls plus the setup calls.

Base URL from env LW_TRACKER (default http://127.0.0.1:3000). The token is
read from a file on every call and only ever placed in the Authorization
header; it never appears in an exception, a log line or a return value.
Standard library only (urllib); no proxies, so 127.0.0.1 stays local.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:3000"
DEFAULT_REPO = "labwatch/bench-1"
DEFAULT_TIMEOUT = 5.0

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class TrackerError(Exception):
    """The tracker refused a call or could not be reached. status is None when unreachable."""

    def __init__(self, msg, status=None, refused=False):
        super().__init__(msg)
        self.status = status
        self.refused = refused  # True: the request never reached the tracker


def base_url():
    return os.environ.get("LW_TRACKER", DEFAULT_BASE).rstrip("/")


def secrets_dir():
    return os.environ.get("LW_SECRETS_DIR", os.path.expanduser("~/.config/labwatch"))


def _read_token(path):
    with open(path, encoding="ascii") as f:
        tok = f.read().strip()
    if not tok or any(c in tok for c in "\r\n"):
        raise TrackerError("token file %s is empty or malformed" % path)
    return tok


class Forgejo:
    def __init__(self, token_file, base=None, repo=DEFAULT_REPO, timeout=DEFAULT_TIMEOUT):
        self.token_file = token_file
        self.base = (base or base_url()).rstrip("/")
        self.repo = repo
        self.timeout = timeout

    # -- transport -------------------------------------------------------
    def _call(self, method, path, body=None, query=None):
        url = self.base + "/api/v1" + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "token " + _read_token(self.token_file))
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with _OPENER.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            e.close()
            raise TrackerError("%s %s -> HTTP %d" % (method, path, e.code), e.code) from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise TrackerError("%s %s -> unreachable (%s)" % (method, path, reason),
                               refused=isinstance(reason, ConnectionRefusedError)) from None
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise TrackerError("%s %s -> not JSON" % (method, path)) from None

    def _issues(self):
        return "/repos/%s/issues" % self.repo

    # -- the four bench calls (write:issue token) -------------------------
    def create_issue(self, title, body, label_ids):
        """Returns the new issue's number."""
        r = self._call("POST", self._issues(),
                       {"title": title, "body": body, "labels": list(label_ids)})
        try:
            return int(r["number"])
        except (TypeError, KeyError, ValueError):
            raise TrackerError("create issue: no number in reply") from None

    def comment(self, number, body):
        self._call("POST", "%s/%d/comments" % (self._issues(), int(number)), {"body": body})

    def close(self, number):
        self._call("PATCH", "%s/%d" % (self._issues(), int(number)), {"state": "closed"})

    def reopen(self, number):
        self._call("PATCH", "%s/%d" % (self._issues(), int(number)), {"state": "open"})

    def comments(self, number):
        """The issue's comments (bodies only), to check an ambiguous earlier post."""
        r = self._call("GET", "%s/%d/comments" % (self._issues(), int(number)))
        return [c.get("body", "") for c in r if isinstance(c, dict)] if isinstance(r, list) else []

    def search(self, q=None, labels=None):
        """Fuzzy and lagging (see the spec): never a dedupe key on its own."""
        query = {"state": "all", "type": "issues"}
        if q is not None:
            query["q"] = q
        if labels is not None:
            query["labels"] = labels
        r = self._call("GET", self._issues(), query=query)
        return r if isinstance(r, list) else []

    # -- setup calls (setup token) ---------------------------------------
    def version(self):
        return self._call("GET", "/version")

    def repo_exists(self):
        try:
            self._call("GET", "/repos/%s" % self.repo)
            return True
        except TrackerError as e:
            if e.status == 404:
                return False
            raise

    def create_repo(self, name):
        return self._call("POST", "/user/repos", {"name": name})

    def labels(self):
        r = self._call("GET", "/repos/%s/labels" % self.repo)
        return r if isinstance(r, list) else []

    def create_label(self, name, color):
        return self._call("POST", "/repos/%s/labels" % self.repo, {"name": name, "color": color})
