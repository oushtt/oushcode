from __future__ import annotations

import json
import os
import fnmatch
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

from agent.artifacts.job_log import JobLogger
from agent.config import Config
from agent.llm.openrouter import OpenRouterClient


@dataclass
class TodoItem:
    id: int
    text: str
    status: str = "pending"


@dataclass
class TodoState:
    items: list[TodoItem] = field(default_factory=list)
    next_id: int = 1

    def reset(self, items: list[str]) -> list[TodoItem]:
        self.items = []
        self.next_id = 1
        for text in items:
            self.items.append(TodoItem(id=self.next_id, text=text))
            self.next_id += 1
        return self.items

    def as_dicts(self) -> list[dict[str, str | int]]:
        return [{"id": i.id, "text": i.text, "status": i.status} for i in self.items]

    def set_status(self, item_id: int, status: str) -> bool:
        for item in self.items:
            if item.id == item_id:
                item.status = status
                return True
        return False


@dataclass
class ToolContext:
    repo_path: str
    cfg: Config
    job_log: JobLogger
    todo: TodoState


ToolHandler = Callable[[dict[str, Any], ToolContext], str]


def _safe_path(repo_path: str, path: str) -> str:
    full = os.path.abspath(os.path.join(repo_path, path))
    base = os.path.abspath(repo_path)
    if not full.startswith(base + os.sep) and full != base:
        raise ValueError("Path escapes repository")
    return full


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncated {len(text) - limit} chars)"


def _run(cmd: list[str], cwd: str, timeout: int) -> str:
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return "command timed out"
    output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    return output.strip()


def tool_list_files(args: dict[str, Any], ctx: ToolContext) -> str:
    pattern = args.get("pattern")
    max_results = int(args.get("max_results", 200))
    entries: list[str] = []
    for root, dirs, files in os.walk(ctx.repo_path):
        dirs[:] = [
            d
            for d in dirs
            if d
            not in {
                ".git",
                ".venv",
                "__pycache__",
                "agent_notes",
                "artifacts",
                "data",
                "workdir",
            }
        ]
        for name in files:
            rel = os.path.relpath(os.path.join(root, name), ctx.repo_path)
            if pattern and not fnmatch.fnmatch(rel, pattern):
                continue
            entries.append(rel)
            if len(entries) >= max_results:
                break
        if len(entries) >= max_results:
            break
    return "\n".join(entries)


def tool_repo_tree(args: dict[str, Any], ctx: ToolContext) -> str:
    max_depth = int(args.get("max_depth", 3))
    lines: list[str] = []
    base_depth = ctx.repo_path.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(ctx.repo_path):
        dirs[:] = [
            d
            for d in dirs
            if d
            not in {
                ".git",
                ".venv",
                "__pycache__",
                "agent_notes",
                "artifacts",
                "data",
                "workdir",
            }
        ]
        depth = root.count(os.sep) - base_depth
        if depth > max_depth:
            dirs[:] = []
            continue
        indent = "  " * depth
        rel_root = os.path.relpath(root, ctx.repo_path)
        if rel_root == ".":
            rel_root = "./"
        lines.append(f"{indent}{rel_root}")
        for name in files:
            lines.append(f"{indent}  {name}")
    return "\n".join(lines)


def tool_read_file_range(args: dict[str, Any], ctx: ToolContext) -> str:
    path = args.get("path")
    start = int(args.get("start", 1))
    end = int(args.get("end", start + 200))
    if not path:
        raise ValueError("path is required")
    full = _safe_path(ctx.repo_path, path)
    with open(full, "r", encoding="utf-8") as f:
        lines = f.readlines()
    start = max(1, start)
    end = min(len(lines), end)
    selected = lines[start - 1 : end]
    return "".join(selected)


def tool_read_file_head(args: dict[str, Any], ctx: ToolContext) -> str:
    path = args.get("path")
    n = int(args.get("n", 200))
    if not path:
        raise ValueError("path is required")
    full = _safe_path(ctx.repo_path, path)
    with open(full, "r", encoding="utf-8") as f:
        lines: list[str] = []
        for _ in range(n):
            line = f.readline()
            if not line:
                break
            lines.append(line)
        return "".join(lines)


def tool_rg_search(args: dict[str, Any], ctx: ToolContext) -> str:
    query = args.get("query")
    globs = args.get("globs")
    context_lines = int(args.get("context_lines", 2))
    if not query:
        raise ValueError("query is required")
    cmd = ["rg", "--line-number", f"-C{context_lines}", query]
    if globs:
        for g in globs:
            cmd.extend(["-g", g])
    return _run(cmd, ctx.repo_path, ctx.cfg.agent_tool_timeout_sec)


def tool_git_diff(_: dict[str, Any], ctx: ToolContext) -> str:
    return _run(["git", "diff"], ctx.repo_path, ctx.cfg.agent_tool_timeout_sec)


def tool_git_status(_: dict[str, Any], ctx: ToolContext) -> str:
    return _run(["git", "status", "--porcelain"], ctx.repo_path, ctx.cfg.agent_tool_timeout_sec)


def tool_apply_patch(args: dict[str, Any], ctx: ToolContext) -> str:
    patch = args.get("patch")
    if not patch:
        raise ValueError("patch is required")
    if "+++" not in patch or "---" not in patch:
        return "patch must be unified diff with --- and +++ headers"
    patch_path = os.path.join(ctx.repo_path, ".agent_patch.diff")
    with open(patch_path, "w", encoding="utf-8") as f:
        f.write(patch.rstrip() + "\n")
    check = subprocess.run(
        ["git", "apply", "--check", patch_path], cwd=ctx.repo_path, capture_output=True, text=True
    )
    if check.returncode != 0:
        os.remove(patch_path)
        return (check.stderr or check.stdout or "git apply --check failed").strip()
    apply = subprocess.run(
        ["git", "apply", patch_path], cwd=ctx.repo_path, capture_output=True, text=True
    )
    os.remove(patch_path)
    if apply.returncode != 0:
        return (apply.stderr or apply.stdout or "git apply failed").strip()
    return "patch applied"


def tool_format_code(_: dict[str, Any], ctx: ToolContext) -> str:
    return _run(["ruff", "format", "."], ctx.repo_path, ctx.cfg.agent_tool_timeout_sec)


def tool_run_pytest(args: dict[str, Any], ctx: ToolContext) -> str:
    target = args.get("target", "")
    cmd = ["pytest"]
    if target:
        cmd.append(target)
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{env.get('PYTHONPATH','')}:{os.path.join(ctx.repo_path, 'src')}"
    try:
        result = subprocess.run(
            cmd,
            cwd=ctx.repo_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=ctx.cfg.agent_tool_timeout_sec,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return "command timed out"
    output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    return output.strip()


def tool_run_ruff(_: dict[str, Any], ctx: ToolContext) -> str:
    return _run(["ruff", "check", "."], ctx.repo_path, ctx.cfg.agent_tool_timeout_sec)


def tool_run_mypy(args: dict[str, Any], ctx: ToolContext) -> str:
    target = args.get("target", "src")
    return _run(["mypy", target], ctx.repo_path, ctx.cfg.agent_tool_timeout_sec)

def tool_run_shell(args: dict[str, Any], ctx: ToolContext) -> str:
    if not ctx.cfg.agent_allow_shell:
        return "shell tool disabled"
    command = args.get("command")
    if not command:
        raise ValueError("command is required")
    return _run(["bash", "-lc", str(command)], ctx.repo_path, ctx.cfg.agent_tool_timeout_sec)


def tool_write_file(args: dict[str, Any], ctx: ToolContext) -> str:
    path = args.get("path")
    content = args.get("content")
    if not path or content is None:
        raise ValueError("path and content are required")
    full = _safe_path(ctx.repo_path, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(str(content))
    return "file written"


def tool_todo_init(args: dict[str, Any], ctx: ToolContext) -> str:
    items = args.get("items")
    if not isinstance(items, list):
        raise ValueError("items must be a list of strings")
    texts = [str(i) for i in items]
    ctx.todo.reset(texts)
    return json.dumps(ctx.todo.as_dicts(), ensure_ascii=True)


def tool_todo_list(_: dict[str, Any], ctx: ToolContext) -> str:
    return json.dumps(ctx.todo.as_dicts(), ensure_ascii=True)


def tool_todo_set(args: dict[str, Any], ctx: ToolContext) -> str:
    item_id = int(args.get("id", 0))
    status = str(args.get("status", "pending"))
    if not item_id:
        raise ValueError("id is required")
    ok = ctx.todo.set_status(item_id, status)
    return "ok" if ok else "not found"


def build_tools(cfg: Config) -> dict[str, ToolHandler]:
    tools: dict[str, ToolHandler] = {
        "list_files": tool_list_files,
        "repo_tree": tool_repo_tree,
        "read_file_range": tool_read_file_range,
        "read_file_head": tool_read_file_head,
        "rg_search": tool_rg_search,
        "git_diff": tool_git_diff,
        "git_status": tool_git_status,
        "apply_patch": tool_apply_patch,
        "format_code": tool_format_code,
        "run_pytest": tool_run_pytest,
        "run_ruff": tool_run_ruff,
        "run_mypy": tool_run_mypy,
        "write_file": tool_write_file,
        "todo_init": tool_todo_init,
        "todo_list": tool_todo_list,
        "todo_set": tool_todo_set,
    }
    if cfg.agent_allow_shell:
        tools["run_shell"] = tool_run_shell
    return tools


def _extract_json(text: str) -> dict[str, Any] | None:
    content = text.strip()
    if content.startswith("```"):
        content = content.strip("`\n")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


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

    tool_lines = [
            "- list_files {pattern?(glob), max_results?}",
            "- repo_tree {max_depth?}",
            "- read_file_range {path, start, end}",
            "- read_file_head {path, n}",
            "- rg_search {query, globs?, context_lines?}",
            "- git_diff {}",
            "- git_status {}",
            "- apply_patch {patch}",
            "- write_file {path, content}",
            "- format_code {}",
            "- run_pytest {target?}",
            "- run_ruff {}",
            "- run_mypy {target?}",
            "- todo_init {items}",
            "- todo_list {}",
            "- todo_set {id, status}",
    ]
    if cfg.agent_allow_shell:
        tool_lines.append("- run_shell {command}")
    tool_list = "\n".join(tool_lines)

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
        "9) If helpful, create a TODO list using todo_init and keep it updated.\n"
        "10) Before final, verify your changes match the issue and mention this in summary.\n"
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
            if "corrupt patch" in result or "apply failed" in result or "unified diff" in result:
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
