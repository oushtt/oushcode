from __future__ import annotations

import hmac
import hashlib
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from agent.config import Config
from agent.jobs.enqueue import enqueue_from_event
from agent.server.ui import render_ui
from agent.storage import db


def _verify_signature(secret: str, body: bytes, signature: str) -> bool:
    if not secret:
        return True
    if not signature or not signature.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    return hmac.compare_digest(expected, signature)


def create_app() -> FastAPI:
    cfg = Config.load()
    app = FastAPI(title="Coding Agents SDLC")
    conn = db.connect(cfg.database_path)
    db.init_db(conn)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ui", response_class=HTMLResponse)
    def ui(job_id: int | None = None) -> HTMLResponse:
        jobs = list(db.list_jobs(conn))
        selected = db.get_job(conn, job_id) if job_id else None
        html = render_ui(jobs=jobs, selected=selected, artifacts_dir=cfg.artifacts_dir)
        return HTMLResponse(content=html)

    @app.post("/webhook")
    async def webhook(request: Request) -> dict[str, Any]:
        event = request.headers.get("X-GitHub-Event", "")
        delivery_id = request.headers.get("X-GitHub-Delivery", "")
        signature = request.headers.get("X-Hub-Signature-256", "")
        body = await request.body()

        if db.delivery_seen(conn, delivery_id):
            return {"status": "skipped", "reason": "duplicate delivery"}

        if not (
            _verify_signature(cfg.code_webhook_secret, body, signature)
            or _verify_signature(cfg.reviewer_webhook_secret, body, signature)
        ):
            raise HTTPException(status_code=401, detail="Invalid signature")

        payload = await request.json()
        job_id = enqueue_from_event(
            conn,
            event=event,
            payload=payload,
            delivery_id=delivery_id,
        )
        db.mark_delivery(conn, delivery_id)
        return {"status": "accepted", "job_id": job_id}

    return app
