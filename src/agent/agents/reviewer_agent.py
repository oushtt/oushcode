from __future__ import annotations

import json
import re
from typing import Any

from agent.artifacts.job_log import JobLogger
from agent.config import Config
from agent.github.client import GitHubClient
from agent.llm.openrouter import OpenRouterClient
from agent.tools.local_tools import TodoState, ToolContext, build_readonly_tools, tool_list_lines_for
from agent.tools.reviewer_tools import ReviewContext, build_tools, tool_list_lines


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncated {len(text) - limit} chars)"


def _extract_json(text: str) -> dict[str, Any] | None:
    content = text.strip()
    if content.startswith("```"):
        content = content.strip("`\n")
    try:
        decoder = json.JSONDecoder()
        data, idx = decoder.raw_decode(content)
    except json.JSONDecodeError:
        return None
    if content[idx:].strip():
        return None
    return data


def _looks_like_multiple_objects(text: str) -> bool:
    content = text.strip()
    if content.startswith("```"):
        content = content.strip("`\n")
    return content.count("{") > 1


def run_reviewer_agent(
    *,
    cfg: Config,
    gh: GitHubClient,
    repo: str,
    pr_number: int,
    head_sha: str,
    issue_title: str,
    issue_body: str,
    job_log: JobLogger,
    repo_path: str | None = None,
) -> dict[str, Any]:
    llm = OpenRouterClient(
        api_key=cfg.openrouter_api_key,
        base_url=cfg.openrouter_base_url,
        model=cfg.openrouter_model,
        timeout_sec=cfg.openrouter_timeout_sec,
        max_retries=cfg.openrouter_max_retries,
        max_tokens=cfg.openrouter_max_tokens,
    )
    tools = build_tools()
    ctx = ReviewContext(repo=repo, pr_number=pr_number, head_sha=head_sha, cfg=cfg, gh=gh)
    local_tools: dict[str, Any] = {}
    local_ctx: ToolContext | None = None
    local_tool_list: list[str] = []
    if repo_path:
        local_tools = build_readonly_tools(cfg)
        local_ctx = ToolContext(
            repo_path=repo_path,
            cfg=cfg,
            job_log=job_log,
            todo=TodoState(),
        )
        local_tool_list = tool_list_lines_for(set(local_tools.keys()), cfg)

    tool_list = "\n".join(tool_list_lines() + local_tool_list)

    system = (
        "You are a meticulous senior code reviewer.\n"
        "Be skeptical, evidence-driven, and prioritize correctness, CI health, and maintainability.\n"
        "\n"
        "HARD OUTPUT RULES (must follow exactly):\n"
        "1) Return EXACTLY ONE JSON object per response.\n"
        "2) Do NOT include any extra text or markdown.\n"
        "3) For tool calls: {\"type\":\"tool\",\"tool\":\"<name>\",\"args\":{...}}\n"
        "4) For final: {\"type\":\"final\",\"decision\":\"ok|fix\",\"summary\":\"...\","
        "\"findings\":[{\"severity\":\"low|med|high\",\"file\":\"path-or-'-'\",\"note\":\"...\"}],"
        "\"ci\":\"...\"}\n"
        "\n"
        "SCOPE / SAFETY:\n"
        "5) Use tools to inspect PR, diff, CI, and local repo (read-only). Do NOT modify code.\n"
        "\n"
        "REVIEW WORKFLOW (thorough, not verbose):\n"
        "6) Always check CI status for the head SHA (combined status + check runs). Summarize in ci.\n"
        "7) Inspect PR metadata and the diff. Confirm the change matches the Issue requirements.\n"
        "8) Look for: missing/weak tests, edge cases, correctness bugs, security issues, "
        "performance pitfalls, and unrelated changes.\n"
        "9) If CI failed or is inconclusive for the relevant SHA, decision should be fix.\n"
        "10) Findings must be actionable: point to file (or '-') and describe the concrete issue.\n"
        "\n"
        "FINAL RESPONSE QUALITY:\n"
        "11) summary should be short but substantive: what was checked, whether requirements are met, "
        "and the primary reason for ok/fix.\n"
        "12) Keep findings concise; include the highest-impact issues first.\n"
    )

    user = (
        f"PR: #{pr_number} in {repo}\n"
        f"Head SHA: {head_sha}\n\n"
        f"Issue title: {issue_title}\n\nIssue body:\n{issue_body}\n\n"
        f"Available tools:\n{tool_list}"
    )

    job_log.section("Reviewer Input", f"PR #{pr_number}\nIssue: {issue_title}\n\n{issue_body}")

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    for step in range(cfg.agent_max_steps):
        response = llm.chat(messages, temperature=cfg.agent_temperature)
        content = response["choices"][0]["message"]["content"]
        job_log.section(f"Reviewer LLM Step {step + 1}", content)

        data = _extract_json(content)
        if not data or "type" not in data:
            if _looks_like_multiple_objects(content):
                err = "Multiple JSON objects detected. Return exactly ONE JSON object per response."
            else:
                err = "Invalid JSON. Respond with a single JSON object per instructions."
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": err})
            job_log.event("error", "parse_failed", {"message": err})
            continue

        if data["type"] == "final":
            decision = str(data.get("decision", "fix")).lower()
            return {
                "decision": "ok" if decision == "ok" else "fix",
                "summary": str(data.get("summary", "")),
                "findings": data.get("findings", []),
                "ci": str(data.get("ci", "")),
            }

        if data["type"] != "tool":
            messages.append({"role": "user", "content": "Unknown type. Use tool or final."})
            continue

        tool_name = data.get("tool")
        args = data.get("args", {})
        if tool_name not in tools and tool_name not in local_tools:
            observation = f"Unknown tool: {tool_name}"
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": observation})
            continue

        job_log.event("tool", tool_name, {"args": args})
        try:
            if tool_name in tools:
                result = tools[tool_name](args, ctx)
            else:
                if local_ctx is None:
                    raise RuntimeError("local tools unavailable")
                result = local_tools[tool_name](args, local_ctx)
        except Exception as exc:  # noqa: BLE001
            result = f"tool error: {exc}"
        result = _truncate(result, cfg.agent_max_tool_output_chars)
        job_log.section(f"Tool: {tool_name}", result or "(no output)")
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": f"OBSERVATION:\n{result}"})

    return {"decision": "fix", "summary": "Max steps reached", "findings": [], "ci": ""}
