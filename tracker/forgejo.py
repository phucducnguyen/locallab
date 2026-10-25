"""A small Forgejo API client: the four issue calls, the setup calls, and the
code repo calls of the fix beat (create repo, git push, pull request).

Base URL from env LW_TRACKER (default http://127.0.0.1:3000). The token is
read from a file on every call and only ever placed in the Authorization
header; it never appears in an exception, a log line or a return value.
Standard library only (urllib); no proxies, so 127.0.0.1 stays local.
"""

import base64
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:3000"
DEFAULT_REPO = "labwatch/bench-1"
DEFAULT_CODE_REPO = "labwatch/bench-fw"
PUSH_TIMEOUT = 120.0
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

    def ensure_repo(self):
        """Create self.repo (setup token) unless it exists. True if created."""
        if self.repo_exists():
            return False
        self.create_repo(self.repo.split("/", 1)[1])
        return True

    # -- code repo: pull requests and git push (setup token) -------------
    def create_pull(self, title, body, head, base="main"):
        """Open a pull request; returns (number, html_url)."""
        r = self._call("POST", "/repos/%s/pulls" % self.repo,
                       {"title": title, "body": body, "head": head, "base": base})
        try:
            return int(r["number"]), str(r.get("html_url") or "")
        except (TypeError, KeyError, ValueError):
            raise TrackerError("create pull: no number in reply") from None

    def pulls(self, state="open"):
        r = self._call("GET", "/repos/%s/pulls" % self.repo, query={"state": state})
        return r if isinstance(r, list) else []

    def git_url(self):
        return "%s/%s.git" % (self.base, self.repo)

    def push(self, local_repo, refspecs, timeout=PUSH_TIMEOUT):
        """git push local_repo's refspecs to self.repo over HTTP, never forced.

        The token goes to git only as an http.extraHeader set through the
        GIT_CONFIG_* environment of the git process: never in the URL, never
        in an argv. Basic auth (owner:token), which Forgejo's git HTTP side
        accepts for tokens. Raises TrackerError without git's output text.
        """
        owner = self.repo.split("/", 1)[0]
        cred = base64.b64encode(("%s:%s" % (owner, _read_token(self.token_file)))
                                .encode("ascii")).decode("ascii")
        env = {k: v for k, v in os.environ.items()
               if not k.lower().endswith("_proxy") and not k.startswith("GIT_CONFIG")}
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "",
                    "NO_PROXY": "*", "no_proxy": "*",
                    "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                    "GIT_CONFIG_VALUE_0": "Authorization: Basic " + cred})
        argv = ["git", "-C", local_repo, "-c", "credential.helper=", "-c",
                "http.followRedirects=false", "push", "--quiet", "--porcelain",
                self.git_url()] + list(refspecs)
        if any(r.startswith("+") for r in refspecs):
            raise TrackerError("push: forced refspecs are not allowed")
        try:
            p = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TrackerError("push to %s -> timed out" % self.repo) from None
        except OSError as e:
            raise TrackerError("push: cannot run git (%s)" % e.__class__.__name__) from None
        if p.returncode != 0:
            err = (p.stderr or b"").decode("utf-8", "replace")
            status = 403 if "403" in err else 401 if "401" in err else None
            hint = ("rejected (not a fast-forward)" if "rejected" in err or "[rejected]" in
                    (p.stdout or b"").decode("utf-8", "replace") else "git exit %d" % p.returncode)
            raise TrackerError("push to %s -> %s" % (self.repo, hint if status is None
                                                     else "HTTP %d" % status), status)

    def labels(self):
        r = self._call("GET", "/repos/%s/labels" % self.repo)
        return r if isinstance(r, list) else []

    def create_label(self, name, color):
        return self._call("POST", "/repos/%s/labels" % self.repo, {"name": name, "color": color})
