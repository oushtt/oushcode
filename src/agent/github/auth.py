from __future__ import annotations

"""GitHub App authentication helpers.

We authenticate as a GitHub App (JWT) and then exchange it for an installation token,
which is used for all repository-level API calls.
"""

import time
from dataclasses import dataclass
from typing import Any

import jwt
import requests


@dataclass
class GitHubAppAuth:
    app_id: str
    private_key_path: str
    api_base: str
    api_version: str

    def _read_private_key(self) -> str:
        # Private key is mounted into the container (e.g. /app/secrets/*.pem).
        if not self.private_key_path:
            raise RuntimeError("GitHub App private key path is not configured")
        with open(self.private_key_path, "r", encoding="utf-8") as f:
            return f.read()

    def app_jwt(self) -> str:
        # GitHub requires:
        # - iat: issued-at (allow small clock skew)
        # - exp: short TTL (<= 10 minutes recommended)
        # - iss: GitHub App ID
        key = self._read_private_key()
        now = int(time.time())
        payload = {
            "iat": now - 30,
            "exp": now + 9 * 60,
            "iss": self.app_id,
        }
        token = jwt.encode(payload, key, algorithm="RS256")
        return token

    def _request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.api_base}{path}"
        headers = {
            "Authorization": f"Bearer {self.app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.api_version,
        }
        resp = requests.request(method, url, headers=headers, json=json_body, timeout=30)
        resp.raise_for_status()
        if resp.text:
            return resp.json()
        return {}

    def get_installation_id(self, repo_full_name: str) -> int:
        # Repository must have the App installed; GitHub returns installation metadata for that repo.
        data = self._request("GET", f"/repos/{repo_full_name}/installation")
        return int(data["id"])

    def get_installation_token(self, repo_full_name: str) -> str:
        # Installation tokens are short-lived and scoped to the installation permissions.
        installation_id = self.get_installation_id(repo_full_name)
        data = self._request(
            "POST", f"/app/installations/{installation_id}/access_tokens"
        )
        return data["token"]
