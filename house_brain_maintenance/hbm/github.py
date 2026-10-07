"""GitHub REST client: Contents read, Issues read/write.

Signed by a GitHub App installation token (Credential Autopilot, 0.6.3/0.3.6) or the hand-made
fine-grained token (PAT), chosen per call by ``ghauth.Auth``.
"""
from __future__ import annotations

import re
import urllib.parse

from . import ghauth, net

RE_REPO = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
REQUESTS_DIR = "maint/requests"


class GitHub:
    def __init__(self, repo: str, token: str, api: str = "https://api.github.com",
                 auth: ghauth.Auth | None = None) -> None:
        if not RE_REPO.fullmatch(repo):
            raise ValueError("invalid repository name")
        self.repo = repo
        self.api = api.rstrip("/")
        net.register_secret(token)
        self.auth = auth or ghauth.Auth(ghauth.PAT_ONLY, token, None)
        self.calls = 0

    def _headers(self, bearer: str, accept: str = "application/vnd.github+json") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {bearer}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "house-brain-maintenance",
        }

    def _send(self, method: str, url: str, accept: str = "application/vnd.github+json", raw: bool = False,
              body=None):
        """One call; a 401 on a cached GitHub App token is retried once with a fresh token."""
        for attempt in (0, 1):
            bearer, kind = self.auth.bearer()
            seen: dict[str, str] = {}
            try:
                status, data = net.request(method, url, self._headers(bearer, accept), body=body, raw=raw,
                                           headers_out=seen)
            except net.NetError as err:
                self.auth.observe(kind, err.status, seen)
                if err.status == 401 and attempt == 0 and self.auth.unauthorized(kind):
                    continue
                raise
            self.auth.observe(kind, status, seen)
            return data
        raise net.NetError(401, "unauthorized")  # pragma: no cover - the loop always returns or raises

    def _url(self, path: str, **query: str) -> str:
        url = f"{self.api}/repos/{self.repo}/{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    def _get(self, path: str, raw: bool = False, **query: str):
        self.calls += 1
        accept = "application/vnd.github.raw+json" if raw else "application/vnd.github+json"
        return self._send("GET", self._url(path, **query), accept, raw=raw)

    def branch_head(self, branch: str) -> str:
        data = self._get(f"branches/{urllib.parse.quote(branch, safe='')}")
        sha = (data or {}).get("commit", {}).get("sha", "")
        if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
            raise net.NetError(0, "branch head missing")
        return sha

    def list_request_ids(self, ref: str) -> list[str]:
        try:
            data = self._get(f"contents/{REQUESTS_DIR}", ref=ref)
        except net.NetError as err:
            if err.status == 404:
                return []
            raise
        if not isinstance(data, list):
            return []
        return sorted(
            item["name"] for item in data
            if isinstance(item, dict) and item.get("type") == "dir" and isinstance(item.get("name"), str)
        )

    def read_file(self, path: str, ref: str) -> bytes:
        quoted = urllib.parse.quote(path, safe="/")
        return self._get(f"contents/{quoted}", raw=True, ref=ref)

    def is_ancestor(self, commit: str, ref: str) -> bool:
        """True when ``commit`` is reachable from branch ``ref`` (same repository)."""
        quoted = urllib.parse.quote(ref, safe="")
        data = self._get(f"compare/{commit}...{quoted}")
        status = (data or {}).get("status")
        return status in ("ahead", "identical")

    def comment(self, issue: int, body: str) -> None:
        self.calls += 1
        self._send("POST", self._url(f"issues/{int(issue)}/comments"), body={"body": body[:60000]})
