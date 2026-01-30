from __future__ import annotations

import logging
import os
import shutil
import textwrap
from urllib.parse import quote

from agent.config import Config
from agent.github.auth import GitHubAppAuth
from agent.github.client import GitHubClient
from agent.artifacts.job_log import JobLogger
from agent.storage.db import Job
from agent.agents.code_agent import run_code_agent
from agent.tools.git_ops import (
    add_all_and_commit,
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

    body = textwrap.dedent(
        """
        DECISION: fix
        REASON: Reviewer logic is not implemented yet.

        FINDINGS:
        - severity: low
          file: -
          note: Reviewer placeholder comment.
        """
    ).strip()

    gh.post_comment(repo, int(pr_number), body)
    job_log.section("Reviewer Output", body)
    logger.info("Posted review comment for pr=%s", pr_number)
