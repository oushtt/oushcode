from __future__ import annotations

import logging
import time

from agent.config import Config
from agent.artifacts.job_log import JobLogger
from agent.jobs.handlers import handle_issue_job, handle_review_job, handle_fix_job
from agent.storage import db

logger = logging.getLogger("agent.worker")


def run_worker(cfg: Config) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("Worker starting (db=%s)", cfg.database_path)
    conn = db.connect(cfg.database_path)
    db.init_db(conn)

    while True:
        job = db.fetch_next_job(conn)
        if job is None:
            time.sleep(1.0)
            continue

        job_log = JobLogger(job.id, cfg.artifacts_dir)
        db.update_job_status(conn, job.id, "running")
        try:
            logger.info(
                "Job start id=%s kind=%s repo=%s issue=%s pr=%s sha=%s",
                job.id,
                job.kind,
                job.repo,
                job.issue_number,
                job.pr_number,
                job.head_sha,
            )
            job_log.event(
                "job_start",
                "Job started",
                {
                    "kind": job.kind,
                    "repo": job.repo,
                    "issue_number": job.issue_number,
                    "pr_number": job.pr_number,
                    "head_sha": job.head_sha,
                },
            )
            if job.kind == "issue":
                handle_issue_job(cfg, job, job_log)
            elif job.kind == "fix":
                handle_fix_job(cfg, job, job_log)
            elif job.kind == "review":
                handle_review_job(cfg, job, job_log)
            else:
                raise ValueError(f"Unknown job kind: {job.kind}")
            db.update_job_status(conn, job.id, "done")
            logger.info("Job done id=%s kind=%s", job.id, job.kind)
            job_log.event("job_done", "Job completed", {"kind": job.kind})
        except Exception as exc:  # noqa: BLE001
            db.update_job_status(conn, job.id, "failed", error=str(exc))
            logger.exception("Job failed id=%s kind=%s error=%s", job.id, job.kind, exc)
            job_log.event("job_failed", "Job failed", {"error": str(exc)})
