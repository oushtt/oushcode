from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from agent.config import Config
from agent.github.client import GitHubClient


@dataclass
class ReviewContext:
    repo: str
    pr_number: int
    head_sha: str
    cfg: Config
    gh: GitHubClient


ToolHandler = Callable[[dict[str, Any], ReviewContext], str]


def tool_pr_info(_: dict[str, Any], ctx: ReviewContext) -> str:
    pr = ctx.gh.get_pr(ctx.repo, ctx.pr_number)
    info = {
        "title": pr.get("title"),
        "body": pr.get("body"),
        "author": (pr.get("user") or {}).get("login"),
        "state": pr.get("state"),
        "base": (pr.get("base") or {}).get("ref"),
        "head": (pr.get("head") or {}).get("ref"),
        "head_sha": (pr.get("head") or {}).get("sha"),
        "changed_files": pr.get("changed_files"),
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
    }
    return json.dumps(info, ensure_ascii=True)


def tool_pr_diff(_: dict[str, Any], ctx: ReviewContext) -> str:
    return ctx.gh.get_pr_diff(ctx.repo, ctx.pr_number)


def tool_pr_files(_: dict[str, Any], ctx: ReviewContext) -> str:
    files = ctx.gh.get_pr_files(ctx.repo, ctx.pr_number)
    simplified = []
    for f in files:
        simplified.append(
            {
                "filename": f.get("filename"),
                "status": f.get("status"),
                "additions": f.get("additions"),
                "deletions": f.get("deletions"),
                "patch": f.get("patch"),
            }
        )
    return json.dumps(simplified, ensure_ascii=True)


def tool_ci_status(_: dict[str, Any], ctx: ReviewContext) -> str:
    status = ctx.gh.get_commit_status(ctx.repo, ctx.head_sha)
    checks = ctx.gh.get_check_runs(ctx.repo, ctx.head_sha)
    summary = {
        "combined_status": status.get("state"),
        "statuses": status.get("statuses", []),
        "check_runs": (checks.get("check_runs") or []),
    }
    return json.dumps(summary, ensure_ascii=True)


def build_tools() -> dict[str, ToolHandler]:
    return {
        "pr_info": tool_pr_info,
        "pr_diff": tool_pr_diff,
        "pr_files": tool_pr_files,
        "ci_status": tool_ci_status,
    }


def tool_list_lines() -> list[str]:
    return [
        "- pr_info {} — PR metadata",
        "- pr_diff {} — unified diff of PR",
        "- pr_files {} — list of files with patches",
        "- ci_status {} — combined status + check runs",
    ]
