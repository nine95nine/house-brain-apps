"""GitHub REST client (fine-grained token: Contents read, Issues read/write)."""
from __future__ import annotations

import re
import urllib.parse

from . import net

RE_REPO = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
REQUESTS_DIR = "maint/requests"


class GitHub:
    def __init__(self, repo: str, token: str, api: str = "https://api.github.com") -> None:
        if not RE_REPO.fullmatch(repo):
            raise ValueError("invalid repository name")
        self.repo = repo
        self.api = api.rstrip("/")
        self._token = token
        net.register_secret(token)
        self.calls = 0

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "house-brain-maintenance",
        }

    def _url(self, path: str, **query: str) -> str:
        url = f"{self.api}/repos/{self.repo}/{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    def _get(self, path: str, raw: bool = False, **query: str):
        self.calls += 1
        accept = "application/vnd.github.raw+json" if raw else "application/vnd.github+json"
        return net.request("GET", self._url(path, **query), self._headers(accept), raw=raw)[1]

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
        net.request("POST", self._url(f"issues/{int(issue)}/comments"), self._headers(),
                    body={"body": body[:60000]})
