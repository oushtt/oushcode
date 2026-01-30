from __future__ import annotations

import logging
import os
import re
import shutil
import textwrap
from urllib.parse import quote

from agent.config import Config
from agent.github.auth import GitHubAppAuth
from agent.github.client import GitHubClient
from agent.artifacts.job_log import JobLogger
from agent.storage import db
from agent.storage.db import Job
from agent.agents.code_agent import run_code_agent
from agent.agents.reviewer_agent import run_reviewer_agent
from agent.tools.git_ops import (
    add_all_and_commit,
    checkout_ref,
    checkout_remote_branch,
    clone_from_mirror,
    create_branch,
    ensure_mirror,
    git_status_porcelain,
    push_branch,
    set_origin,
)

logger = logging.getLogger("agent.jobs")


def _workdir(cfg: Config, repo: str, job_id: int) -> str:
    safe = repo.replace("/", "__")
    return os.path.join(cfg.workdir_root, safe, f"job-{job_id}")

def _mirror_path(cfg: Config, repo: str) -> str:
    safe = repo.replace("/", "__")
    return os.path.join(cfg.workdir_root, "cache", f"{safe}.git")


def _git_env(cfg: Config) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = cfg.git_user_name
    env["GIT_AUTHOR_EMAIL"] = cfg.git_user_email
    env["GIT_COMMITTER_NAME"] = cfg.git_user_name
    env["GIT_COMMITTER_EMAIL"] = cfg.git_user_email
    return env


def handle_issue_job(cfg: Config, job: Job, job_log: JobLogger) -> None:
    payload = job.payload
    repo = (payload.get("repository") or {}).get("full_name")
    issue = payload.get("issue") or {}
    issue_number = issue.get("number")
    if not repo or not issue_number:
        raise RuntimeError("Missing repo or issue_number in payload")

    logger.info("Issue job: repo=%s issue=%s", repo, issue_number)
    job_log.event("issue", "Issue job received", {"repo": repo, "issue": issue_number})
    app_auth = GitHubAppAuth(
        app_id=cfg.code_app_id,
        private_key_path=cfg.code_app_private_key_path,
        api_base=cfg.github_api_base,
        api_version=cfg.github_api_version,
    )
    token = app_auth.get_installation_token(repo)
    gh = GitHubClient(token=token, api_base=cfg.github_api_base, api_version=cfg.github_api_version)

    issue_data = gh.get_issue(repo, int(issue_number))
    default_branch = (payload.get("repository") or {}).get("default_branch", "main")
    logger.info("Issue title: %s", issue_data.get("title", ""))
    job_log.section(
        "Input (Issue)",
        f"Title: {issue_data.get('title','')}\n\n{issue_data.get('body','')}",
    )

    branch = f"agent/issue-{issue_number}-{job.id}"
    workdir = _workdir(cfg, repo, job.id)
    mirror_path = _mirror_path(cfg, repo)
    if os.path.exists(workdir):
        shutil.rmtree(workdir)

    token_safe = quote(token, safe="")
    clone_url = f"https://x-access-token:{token_safe}@github.com/{repo}.git"
    logger.info("Updating mirror cache %s", mirror_path)
    job_log.event("tool", "git.ensure_mirror", {"repo": repo})
    ensure_mirror(clone_url, mirror_path)
    logger.info("Cloning from mirror to %s", workdir)
    job_log.event("tool", "git.clone_from_mirror", {"dest": workdir})
    clone_from_mirror(mirror_path, workdir)
    set_origin(clone_url, workdir)
    logger.info("Creating branch %s", branch)
    job_log.event("tool", "git.create_branch", {"branch": branch})
    create_branch(branch, workdir)

    notes_dir = os.path.join(workdir, "agent_notes")
    os.makedirs(notes_dir, exist_ok=True)
    notes_path = os.path.join(notes_dir, f"issue-{issue_number}.md")
    with open(notes_path, "w", encoding="utf-8") as f:
        f.write(
            textwrap.dedent(
                f"""
                # Issue #{issue_number}

                Title: {issue_data.get('title','')}

                {issue_data.get('body','')}
                """
            ).strip()
            + "\n"
        )

    agent_result = run_code_agent(
        cfg=cfg,
        repo_path=workdir,
        issue_title=str(issue_data.get("title", "")),
        issue_body=str(issue_data.get("body", "")),
        job_log=job_log,
    )

    status = git_status_porcelain(workdir)
    status_lines = [line.strip() for line in status.splitlines() if line.strip()]
    non_note_changes = [line for line in status_lines if "agent_notes/" not in line]
    if not non_note_changes:
        job_log.event("info", "no_changes", {"message": "No changes detected"})
        gh.post_comment(
            repo,
            int(issue_number),
            "Code Agent did not produce any changes. Please уточни задачу.",
        )
        return

    logger.info("Committing changes")
    commit_msg = f"Agent: {issue_data.get('title', f'Issue #{issue_number}')}"
    job_log.event("tool", "git.commit", {"message": commit_msg})
    add_all_and_commit(commit_msg, workdir, _git_env(cfg))
    logger.info("Pushing branch %s", branch)
    job_log.event("tool", "git.push", {"branch": branch})
    push_branch(branch, workdir, _git_env(cfg))

    pr_title = f"Agent: {issue_data.get('title', f'Issue #{issue_number}')}"
    pr_body = textwrap.dedent(
        f"""
        Closes #{issue_number}

        ## Summary
        - {agent_result.get('summary','Automated change generated by Code Agent')}

        ## Testing
        - {agent_result.get('tests','Not run locally (CI in GitHub Actions)')}
        """
    ).strip() + "\n"

    logger.info("Creating PR base=%s head=%s", default_branch, branch)
    job_log.section("Agent Output (PR)", pr_body)
    pr = gh.create_pr(repo, default_branch, branch, pr_title, pr_body)
    pr_url = pr.get("html_url", "")
    logger.info("PR created: %s", pr_url)
    job_log.event("github", "pr.created", {"url": pr_url, "branch": branch})
    gh.post_comment(
        repo,
        int(issue_number),
        f"Created PR: {pr_url}" if pr_url else "Created PR.",
    )


def handle_fix_job(cfg: Config, job: Job, job_log: JobLogger) -> None:
    payload = job.payload
    repo = job.repo or (payload.get("repository") or {}).get("full_name")
    pr = payload.get("pull_request") or {}
    pr_number = job.pr_number or pr.get("number")
    if not repo or not pr_number:
        raise RuntimeError("Missing repo or pr_number in payload")

    force_retry = bool(payload.get("agent_force_retry"))

    logger.info("Fix job: repo=%s pr=%s", repo, pr_number)
    job_log.event("fix", "Fix job received", {"repo": repo, "pr": pr_number})
    app_auth = GitHubAppAuth(
        app_id=cfg.code_app_id,
        private_key_path=cfg.code_app_private_key_path,
        api_base=cfg.github_api_base,
        api_version=cfg.github_api_version,
    )
    token = app_auth.get_installation_token(repo)
    gh = GitHubClient(token=token, api_base=cfg.github_api_base, api_version=cfg.github_api_version)

    pr_data = gh.get_pr(repo, int(pr_number))
    pr_body = pr_data.get("body") or ""
    head_ref = (pr_data.get("head") or {}).get("ref") or ""
    head_sha = job.head_sha or (pr_data.get("head") or {}).get("sha") or ""

    issue_title = pr_data.get("title") or ""
    issue_body = pr_body
    issue_number = None
    match = re.search(r"(?i)closes\s+#(\d+)", pr_body)
    if match:
        issue_number = int(match.group(1))
        issue = gh.get_issue(repo, issue_number)
        issue_title = str(issue.get("title", ""))
        issue_body = str(issue.get("body", ""))

    conn = db.connect(cfg.database_path)
    iter_num = job.iter if job.iter else db.get_iteration_count(
        conn, repo=repo, issue_number=issue_number, pr_number=int(pr_number)
    ) + 1
    if (not force_retry) and iter_num > cfg.agent_max_iters:
        db.set_iteration_status(
            conn,
            repo=repo,
            issue_number=issue_number,
            pr_number=int(pr_number),
            iter_num=iter_num,
            status="blocked",
        )
        labels_hint = ", ".join(cfg.agent_retry_labels) if cfg.agent_retry_labels else "retry"
        gh.post_comment(
            repo,
            int(pr_number),
            f"Max iterations reached ({cfg.agent_max_iters}). "
            f"Add label [{labels_hint}] to retry.",
        )
        raise RuntimeError("max iterations reached")

    db.set_iteration_status(
        conn,
        repo=repo,
        issue_number=issue_number,
        pr_number=int(pr_number),
        iter_num=iter_num,
        status="running",
    )

    workdir = _workdir(cfg, repo, job.id)
    mirror_path = _mirror_path(cfg, repo)
    if os.path.exists(workdir):
        shutil.rmtree(workdir)

    token_safe = quote(token, safe="")
    clone_url = f"https://x-access-token:{token_safe}@github.com/{repo}.git"
    logger.info("Updating mirror cache %s", mirror_path)
    job_log.event("tool", "git.ensure_mirror", {"repo": repo})
    ensure_mirror(clone_url, mirror_path)
    logger.info("Cloning from mirror to %s", workdir)
    job_log.event("tool", "git.clone_from_mirror", {"dest": workdir})
    clone_from_mirror(mirror_path, workdir)
    set_origin(clone_url, workdir)
    if head_ref:
        logger.info("Checking out branch %s", head_ref)
        job_log.event("tool", "git.checkout_branch", {"ref": head_ref})
        checkout_remote_branch(head_ref, workdir)
    elif head_sha:
        logger.info("Checking out SHA %s", head_sha)
        job_log.event("tool", "git.checkout", {"ref": head_sha})
        checkout_ref(head_sha, workdir)

    agent_result = run_code_agent(
        cfg=cfg,
        repo_path=workdir,
        issue_title=str(issue_title),
        issue_body=str(issue_body),
        job_log=job_log,
    )

    status = git_status_porcelain(workdir)
    status_lines = [line.strip() for line in status.splitlines() if line.strip()]
    non_note_changes = [line for line in status_lines if "agent_notes/" not in line]
    if not non_note_changes:
        db.set_iteration_status(
            conn,
            repo=repo,
            issue_number=issue_number,
            pr_number=int(pr_number),
            iter_num=iter_num,
            status="done",
        )
        gh.post_comment(
            repo,
            int(pr_number),
            "Code Agent did not produce any changes for this fix cycle. Please уточни задачу.",
        )
        return

    logger.info("Committing changes")
    commit_msg = f"Agent: Fix PR #{pr_number}"
    job_log.event("tool", "git.commit", {"message": commit_msg})
    add_all_and_commit(commit_msg, workdir, _git_env(cfg))
    if head_ref:
        logger.info("Pushing branch %s", head_ref)
        job_log.event("tool", "git.push", {"branch": head_ref})
        push_branch(head_ref, workdir, _git_env(cfg))

    pr_body = textwrap.dedent(
        f"""
        ## Fix iteration {iter_num}
        - {agent_result.get('summary','Automated fix generated by Code Agent')}

        ## Testing
        - {agent_result.get('tests','Not run locally (CI in GitHub Actions)')}
        """
    ).strip() + "\n"
    job_log.section("Agent Output (PR Fix)", pr_body)
    gh.post_comment(repo, int(pr_number), pr_body)

    db.set_iteration_status(
        conn,
        repo=repo,
        issue_number=issue_number,
        pr_number=int(pr_number),
        iter_num=iter_num,
        status="done",
    )


def handle_review_job(cfg: Config, job: Job, job_log: JobLogger) -> None:
    payload = job.payload
    repo = (payload.get("repository") or {}).get("full_name")
    pr = payload.get("pull_request") or {}
    if not pr:
        pull_requests = payload.get("pull_requests") or []
        if not pull_requests and payload.get("workflow_run"):
            pull_requests = (payload.get("workflow_run") or {}).get("pull_requests") or []
        pr = pull_requests[0] if pull_requests else {}
    pr_number = pr.get("number")
    if not repo or not pr_number:
        raise RuntimeError("Missing repo or pr_number in payload")

    logger.info("Review job: repo=%s pr=%s", repo, pr_number)
    job_log.event("review", "Review job received", {"repo": repo, "pr": pr_number})
    app_auth = GitHubAppAuth(
        app_id=cfg.reviewer_app_id,
        private_key_path=cfg.reviewer_app_private_key_path,
        api_base=cfg.github_api_base,
        api_version=cfg.github_api_version,
    )
    token = app_auth.get_installation_token(repo)
    gh = GitHubClient(token=token, api_base=cfg.github_api_base, api_version=cfg.github_api_version)

    pr_data = gh.get_pr(repo, int(pr_number))
    pr_body = pr_data.get("body") or ""
    head_sha = job.head_sha or (pr_data.get("head") or {}).get("sha") or ""

    issue_title = ""
    issue_body = ""
    match = re.search(r"(?i)closes\s+#(\d+)", pr_body)
    if match:
        issue_number = int(match.group(1))
        issue = gh.get_issue(repo, issue_number)
        issue_title = str(issue.get("title", ""))
        issue_body = str(issue.get("body", ""))

    workdir = _workdir(cfg, repo, job.id)
    mirror_path = _mirror_path(cfg, repo)
    if os.path.exists(workdir):
        shutil.rmtree(workdir)
    token_safe = quote(token, safe="")
    clone_url = f"https://x-access-token:{token_safe}@github.com/{repo}.git"
    logger.info("Updating mirror cache %s", mirror_path)
    job_log.event("tool", "git.ensure_mirror", {"repo": repo})
    ensure_mirror(clone_url, mirror_path)
    logger.info("Cloning from mirror to %s", workdir)
    job_log.event("tool", "git.clone_from_mirror", {"dest": workdir})
    clone_from_mirror(mirror_path, workdir)
    set_origin(clone_url, workdir)
    if head_sha:
        logger.info("Checking out SHA %s", head_sha)
        job_log.event("tool", "git.checkout", {"ref": head_sha})
        checkout_ref(head_sha, workdir)

    result = run_reviewer_agent(
        cfg=cfg,
        gh=gh,
        repo=repo,
        pr_number=int(pr_number),
        head_sha=head_sha,
        issue_title=issue_title,
        issue_body=issue_body,
        job_log=job_log,
        repo_path=workdir,
    )

    decision = result.get("decision", "fix")
    summary = result.get("summary", "")
    findings = result.get("findings", [])
    ci = result.get("ci", "")
    decision = str(decision).lower()
    ci_lower = str(ci).lower()
    if decision == "ok" and ci_lower in {"failed", "error"}:
        decision = "fix"

    findings_lines = []
    if isinstance(findings, list):
        for item in findings:
            if isinstance(item, str):
                findings_lines.append(f"- severity: low\n  file: -\n  note: {item}")
                continue
            if not isinstance(item, dict):
                continue
            severity = item.get("severity", "low")
            file = item.get("file", "-")
            note = item.get("note", "")
            findings_lines.append(f"- severity: {severity}\n  file: {file}\n  note: {note}")
    findings_block = "\n".join(findings_lines) if findings_lines else "- severity: low\n  file: -\n  note: No findings."

    body = textwrap.dedent(
        f"""
        DECISION: {decision}
        SUMMARY: {summary}
        CI: {ci}

        FINDINGS:
        {findings_block}
        """
    ).strip()

    gh.post_comment(repo, int(pr_number), body)
    try:
        if decision == "ok" and ci_lower in {"success", "passed", "ok"}:
            gh.post_review(repo, int(pr_number), body, "APPROVE")
        elif decision != "ok":
            gh.post_review(repo, int(pr_number), body, "REQUEST_CHANGES")
    except Exception as exc:  # noqa: BLE001
        logger.info("Review submission skipped: %s", exc)
    job_log.section("Reviewer Output", body)
    logger.info("Posted review comment for pr=%s", pr_number)

    if decision != "ok":
        conn = db.connect(cfg.database_path)
        if not db.has_active_job(conn, kind="fix", repo=repo, pr_number=int(pr_number)):
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
            db.enqueue_job(
                conn,
                kind="fix",
                payload=payload,
                repo=repo,
                pr_number=pr_number,
                head_sha=head_sha,
                iter_num=iter_num,
            )
