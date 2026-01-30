from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


@dataclass
class GitHubClient:
    token: str
    api_base: str
    api_version: str

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.api_version,
        }

    def _request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{self.api_base}{path}"
        resp = requests.request(method, url, headers=self._headers(), json=json_body, timeout=30)
        resp.raise_for_status()
        if resp.text:
            return resp.json()
        return {}

    def get_issue(self, repo: str, issue_number: int) -> dict[str, Any]:
        return self._request("GET", f"/repos/{repo}/issues/{issue_number}")

    def create_pr(self, repo: str, base: str, head: str, title: str, body: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{repo}/pulls",
            json_body={"base": base, "head": head, "title": title, "body": body},
        )

    def post_comment(self, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{repo}/issues/{issue_number}/comments",
            json_body={"body": body},
        )

    def post_review(self, repo: str, pr_number: int, body: str, event: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{repo}/pulls/{pr_number}/reviews",
            json_body={"body": body, "event": event},
        )
