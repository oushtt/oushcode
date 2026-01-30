from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import requests


@dataclass
class OpenRouterClient:
    api_key: str
    base_url: str
    model: str
    timeout_sec: int = 60
    max_retries: int = 2
    max_tokens: int = 2048

    def chat(self, messages: list[dict[str, str]], temperature: float = 0.2) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not configured")
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": self.max_tokens,
        }
        last_error: Exception | None = None
        for _ in range(self.max_retries + 1):
            try:
                resp = requests.post(
                    url, headers=headers, data=json.dumps(payload), timeout=self.timeout_sec
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise RuntimeError(f"OpenRouter request failed: {last_error}") from last_error
