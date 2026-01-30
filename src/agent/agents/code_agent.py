from __future__ import annotations

import json
from typing import Any

from agent.artifacts.job_log import JobLogger
from agent.config import Config
from agent.llm.openrouter import OpenRouterClient
from agent.tools.local_tools import TodoState, ToolContext, build_tools, tool_list_lines


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


def run_code_agent(
    *,
    cfg: Config,
    repo_path: str,
    issue_title: str,
    issue_body: str,
    job_log: JobLogger,
) -> dict[str, str]:
    llm = OpenRouterClient(
        api_key=cfg.openrouter_api_key,
        base_url=cfg.openrouter_base_url,
        model=cfg.openrouter_model,
        timeout_sec=cfg.openrouter_timeout_sec,
        max_retries=cfg.openrouter_max_retries,
        max_tokens=cfg.openrouter_max_tokens,
    )
    tools = build_tools(cfg)
    tool_list = "\n".join(tool_list_lines(cfg))

    system = (
        "You are a coding agent. Follow these rules strictly:\n"
        "1) Return EXACTLY ONE JSON object per response.\n"
        "2) Do NOT include any extra text or markdown.\n"
        "3) For tool calls: {\"type\":\"tool\",\"tool\":\"<name>\",\"args\":{...}}\n"
        "4) For final: {\"type\":\"final\",\"summary\":\"...\",\"tests\":\"...\"}\n"
        "5) If you need multiple tool calls, do them across multiple steps.\n"
        "6) Use unified diff format for apply_patch (like git diff).\n"
        "   Example:\n"
        "   diff --git a/path b/path\n"
        "   --- a/path\n"
        "   +++ b/path\n"
        "   @@ -1,2 +1,3 @@\n"
        "   -old line\n"
        "   +new line\n"
        "7) Do NOT commit or push.\n"
        "8) Prefer apply_patch or write_file over run_shell.\n"
        "9) For searching/reading use rg_search/grep_search/glob_files/read_file_* "
        "(avoid run_shell for cat/grep/ls).\n"
        "10) If you run tests, use run_pytest/run_ruff/run_mypy tools. "
        "Only claim tests were run if you actually ran them.\n"
        "11) If helpful, create a TODO list using todo_init (items must be a list of strings) "
        "and keep it updated. Allowed statuses: pending|running|done|blocked.\n"
        "12) Before final, verify your changes match the issue and mention this in summary.\n"
        "13) After editing tests, re-read the edited block to confirm syntax.\n"
        "14) You MUST read a file before editing it (read_file_*). Edits without prior read will fail.\n"
        "15) If the Issue cannot be completed as written (missing file/function, conflicting "
        "requirements, or it would require changing unrelated code), do NOT invent changes. "
        "Complete only the clearly possible parts and explain what's missing or ambiguous in final.\n"
    )
    user = (
        f"Issue title: {issue_title}\n\nIssue body:\n{issue_body}\n\n"
        f"Available tools:\n{tool_list}"
    )

    todo_state = TodoState()
    tool_ctx = ToolContext(repo_path=repo_path, cfg=cfg, job_log=job_log, todo=todo_state)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    job_log.section("Agent Input", f"Title: {issue_title}\n\n{issue_body}")

    patch_failures = 0
    for step in range(cfg.agent_max_steps):
        response = llm.chat(messages, temperature=cfg.agent_temperature)
        content = response["choices"][0]["message"]["content"]
        job_log.section(f"LLM Step {step + 1}", content)

        data = _extract_json(content)
        if not data or "type" not in data:
            if _looks_like_multiple_objects(content):
                err = (
                    "Multiple JSON objects detected. "
                    "Return exactly ONE JSON object per response."
                )
            else:
                err = "Invalid JSON. Respond with a single JSON object per instructions."
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": err})
            job_log.event("error", "parse_failed", {"message": err})
            continue

        if data["type"] == "final":
            summary = str(data.get("summary", ""))
            tests = str(data.get("tests", ""))
            return {"summary": summary, "tests": tests}

        if data["type"] != "tool":
            messages.append({"role": "user", "content": "Unknown type. Use tool or final."})
            continue

        tool_name = data.get("tool")
        args = data.get("args", {})
        if tool_name not in tools:
            observation = f"Unknown tool: {tool_name}"
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": observation})
            continue

        job_log.event("tool", tool_name, {"args": args})
        try:
            result = tools[tool_name](args, tool_ctx)
        except Exception as exc:  # noqa: BLE001
            result = f"tool error: {exc}"
        if tool_name == "apply_patch" and "patch" in args:
            patch_error_markers = (
                "patch must include diff --git",
                "patch must be unified diff",
                "apply failed",
                "git apply",
                "patch failed",
                "corrupt patch",
                "unified diff",
            )
            if any(marker in result for marker in patch_error_markers):
                patch_failures += 1
                if patch_failures >= 2:
                    messages.append(
                        {
                            "role": "user",
                            "content": "apply_patch failed twice. Use write_file instead.",
                        }
                    )
        result = _truncate(result, cfg.agent_max_tool_output_chars)
        job_log.section(f"Tool: {tool_name}", result or "(no output)")
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": f"OBSERVATION:\n{result}"})

    return {"summary": "Max steps reached", "tests": "not run"}
