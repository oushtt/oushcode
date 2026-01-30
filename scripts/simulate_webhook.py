from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import uuid

import requests


def sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulate GitHub webhook")
    parser.add_argument("--event", required=True, help="GitHub event name")
    parser.add_argument("--file", required=True, help="Path to JSON payload")
    parser.add_argument("--url", default="http://localhost:8000/webhook")
    parser.add_argument("--secret", default="")
    args = parser.parse_args()

    with open(args.file, "r", encoding="utf-8") as f:
        payload = json.load(f)

    body = json.dumps(payload).encode("utf-8")
    headers = {
        "X-GitHub-Event": args.event,
        "X-GitHub-Delivery": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }
    if args.secret:
        headers["X-Hub-Signature-256"] = sign(args.secret, body)

    resp = requests.post(args.url, headers=headers, data=body, timeout=30)
    print(resp.status_code, resp.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
