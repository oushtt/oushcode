from __future__ import annotations

from typing import Any

from agent.storage import db


def _repo_full_name(payload: dict[str, Any]) -> str | None:
    repo = payload.get("repository") or {}
    if isinstance(repo, str):
        return repo
    return repo.get("full_name")


def _extract_pr_number(payload: dict[str, Any]) -> int | None:
    pr = payload.get("pull_request") or {}
    number = pr.get("number")
    if number:
        try:
            return int(number)
        except (TypeError, ValueError):
            return None
    number = payload.get("pr_number") or payload.get("pr")
    if isinstance(number, dict):
        number = number.get("number")
    if number is None:
        return None
    try:
        return int(number)
    except (TypeError, ValueError):
        return None


def _extract_head_sha(payload: dict[str, Any]) -> str | None:
    head_sha = payload.get("head_sha") or payload.get("sha")
    if head_sha:
        return str(head_sha)
    head = (payload.get("head") or {})
    if isinstance(head, dict):
        head_sha = head.get("sha")
        if head_sha:
            return str(head_sha)
    pr = payload.get("pull_request") or {}
    head = (pr.get("head") or {})
    if isinstance(head, dict):
        head_sha = head.get("sha")
        if head_sha:
            return str(head_sha)
    return None


def enqueue_from_event(
    conn,
    *,
    event: str,
    payload: dict[str, Any],
    delivery_id: str,
    retry_labels: list[str] | None = None,
) -> int | None:
    if event == "issues":
        action = payload.get("action")
        if action not in {"opened", "labeled"}:
            return None
        issue = payload.get("issue") or {}
        return db.enqueue_job(
            conn,
            kind="issue",
            payload=payload,
            repo=_repo_full_name(payload),
            issue_number=issue.get("number"),
            delivery_id=delivery_id,
        )

    if event == "pull_request":
        action = payload.get("action")
        if action == "labeled":
            label = (payload.get("label") or {}).get("name", "")
            if retry_labels and label in retry_labels:
                pr = payload.get("pull_request") or {}
                pr_number = pr.get("number")
                head_sha = (pr.get("head") or {}).get("sha")
                repo = _repo_full_name(payload)
                if not (repo and pr_number and head_sha):
                    return None
                if db.has_active_job(conn, kind="fix", repo=repo, pr_number=int(pr_number)):
                    return None
                iter_num = db.get_iteration_count(
                    conn, repo=repo, issue_number=None, pr_number=int(pr_number)
                ) + 1
                db.set_iteration_status(
                    conn,
                    repo=repo,
                    issue_number=None,
                    pr_number=int(pr_number),
                    iter_num=iter_num,
                    status="queued",
                )
                payload["agent_force_retry"] = True
                return db.enqueue_job(
                    conn,
                    kind="fix",
                    payload=payload,
                    repo=repo,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    iter_num=iter_num,
                    delivery_id=delivery_id,
                )
            return None
        return None

    if event == "check_suite":
        action = payload.get("action")
        if action != "completed":
            return None
        pull_requests = payload.get("pull_requests") or []
        if not pull_requests:
            return None
        pr = pull_requests[0]
        pr_number = pr.get("number")
        head_sha = (
            (payload.get("workflow_run") or {}).get("head_sha")
            or (payload.get("check_suite") or {}).get("head_sha")
            or pr.get("head", {}).get("sha")
        )
        repo = _repo_full_name(payload)
        if not (repo and pr_number and head_sha):
            return None
        if db.review_seen(conn, repo, int(pr_number), head_sha):
            return None
        job_id = db.enqueue_job(
            conn,
            kind="review",
            payload=payload,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            delivery_id=delivery_id,
        )
        db.mark_review(conn, repo, int(pr_number), head_sha)
        return job_id

    if event == "ci_completed":
        repo = _repo_full_name(payload) or payload.get("repo")
        pr_number = _extract_pr_number(payload)
        head_sha = _extract_head_sha(payload)
        if isinstance(repo, dict):
            repo = repo.get("full_name")
        if not (repo and pr_number and head_sha):
            return None
        if db.review_seen(conn, repo, int(pr_number), head_sha):
            return None
        job_id = db.enqueue_job(
            conn,
            kind="review",
            payload=payload,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            delivery_id=delivery_id,
        )
        db.mark_review(conn, repo, int(pr_number), head_sha)
        return job_id

    if event == "workflow_run":
        action = payload.get("action")
        if action != "completed":
            return None
        pull_requests = (payload.get("workflow_run") or {}).get("pull_requests") or []
        if not pull_requests:
            return None
        pr = pull_requests[0]
        pr_number = pr.get("number")
        head_sha = (
            (payload.get("workflow_run") or {}).get("head_sha")
            or pr.get("head", {}).get("sha")
        )
        repo = _repo_full_name(payload)
        if not (repo and pr_number and head_sha):
            return None
        if db.review_seen(conn, repo, int(pr_number), head_sha):
            return None
        job_id = db.enqueue_job(
            conn,
            kind="review",
            payload=payload,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            delivery_id=delivery_id,
        )
        db.mark_review(conn, repo, int(pr_number), head_sha)
        return job_id

    return None
