from __future__ import annotations

import fnmatch
import glob
import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

from agent.artifacts.job_log import JobLogger
from agent.config import Config


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
    read_paths: set[str] = field(default_factory=set)


ToolHandler = Callable[[dict[str, Any], ToolContext], str]


def _safe_path(repo_path: str, path: str) -> str:
    full = os.path.abspath(os.path.join(repo_path, path))
    base = os.path.abspath(repo_path)
    if not full.startswith(base + os.sep) and full != base:
        raise ValueError("Path escapes repository")
    return full


def _rel_path(repo_path: str, path: str) -> str:
    full = _safe_path(repo_path, path)
    return os.path.relpath(full, repo_path)


def _run(cmd: list[str], cwd: str, timeout: int) -> str:
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return "command timed out"
    output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    return output.strip()


def _run_with_exit(cmd: list[str], cwd: str, timeout: int) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return 124, "", "command timed out"
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    return result.returncode, stdout, stderr


def tool_list_files(args: dict[str, Any], ctx: ToolContext) -> str:
    pattern = args.get("pattern")
    max_results = int(args.get("max_results", 200))
    entries: list[str] = []
    if pattern and "**" in pattern:
        for path in glob.glob(os.path.join(ctx.repo_path, pattern), recursive=True):
            if os.path.isdir(path):
                continue
            rel = os.path.relpath(path, ctx.repo_path)
            if rel.startswith(".git/") or rel.startswith("agent_notes/") or rel.startswith("artifacts/"):
                continue
            if rel.startswith(".venv/") or "__pycache__" in rel or rel.startswith("data/"):
                continue
            entries.append(rel)
            if len(entries) >= max_results:
                break
        return "\n".join(entries) if entries else "NO_MATCHES"
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
    return "\n".join(entries) if entries else "NO_MATCHES"


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
    ctx.read_paths.add(_rel_path(ctx.repo_path, path))
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
        ctx.read_paths.add(_rel_path(ctx.repo_path, path))
        return "".join(lines)


def tool_rg_search(args: dict[str, Any], ctx: ToolContext) -> str:
    query = args.get("query")
    globs = args.get("globs")
    context_lines = int(args.get("context_lines", 2))
    if not query:
        raise ValueError("query is required")
    cmd = [
        "rg",
        "--line-number",
        f"-C{context_lines}",
        "-g",
        "!.git/**",
        "-g",
        "!agent_notes/**",
        "-g",
        "!artifacts/**",
        "-g",
        "!.venv/**",
        "-g",
        "!__pycache__/**",
        query,
    ]
    if globs:
        for g in globs:
            cmd.extend(["-g", g])
    out = _run(cmd, ctx.repo_path, ctx.cfg.agent_tool_timeout_sec)
    return out if out else "NO_MATCHES"


def tool_grep_search(args: dict[str, Any], ctx: ToolContext) -> str:
    return tool_rg_search(args, ctx)


def tool_glob_files(args: dict[str, Any], ctx: ToolContext) -> str:
    pattern = args.get("pattern")
    if not pattern:
        raise ValueError("pattern is required")
    matches = []
    for path in glob.glob(os.path.join(ctx.repo_path, pattern), recursive=True):
        if os.path.isdir(path):
            continue
        rel = os.path.relpath(path, ctx.repo_path)
        if rel.startswith(".git/") or rel.startswith("agent_notes/") or rel.startswith("artifacts/"):
            continue
        if rel.startswith(".venv/") or "__pycache__" in rel:
            continue
        matches.append(rel)
    return "\n".join(sorted(matches)) if matches else "NO_MATCHES"


def tool_ast_grep(args: dict[str, Any], ctx: ToolContext) -> str:
    pattern = args.get("pattern")
    language = args.get("language", "python")
    if not pattern:
        raise ValueError("pattern is required")
    cmd = ["ast-grep", "-l", str(language), "-p", str(pattern), "."]
    out = _run(cmd, ctx.repo_path, ctx.cfg.agent_tool_timeout_sec)
    if not out:
        return "NO_MATCHES"
    lines = []
    for line in out.splitlines():
        if (
            line.startswith(".git/")
            or line.startswith("agent_notes/")
            or line.startswith("artifacts/")
            or line.startswith(".venv/")
            or "__pycache__" in line
        ):
            continue
        lines.append(line)
    return "\n".join(lines) if lines else "NO_MATCHES"


def tool_git_diff(_: dict[str, Any], ctx: ToolContext) -> str:
    return _run(["git", "diff"], ctx.repo_path, ctx.cfg.agent_tool_timeout_sec)


def tool_git_status(_: dict[str, Any], ctx: ToolContext) -> str:
    return _run(["git", "status", "--porcelain"], ctx.repo_path, ctx.cfg.agent_tool_timeout_sec)


def tool_git_log(args: dict[str, Any], ctx: ToolContext) -> str:
    max_count = int(args.get("max_count", 5))
    return _run(["git", "log", f"-n{max_count}", "--oneline"], ctx.repo_path, ctx.cfg.agent_tool_timeout_sec)


def tool_git_show(args: dict[str, Any], ctx: ToolContext) -> str:
    ref = args.get("ref")
    path = args.get("path")
    if not ref:
        raise ValueError("ref is required")
    if path:
        return _run(["git", "show", f"{ref}:{path}"], ctx.repo_path, ctx.cfg.agent_tool_timeout_sec)
    return _run(["git", "show", str(ref)], ctx.repo_path, ctx.cfg.agent_tool_timeout_sec)


def tool_apply_patch(args: dict[str, Any], ctx: ToolContext) -> str:
    patch = args.get("patch")
    if not patch:
        raise ValueError("patch is required")
    if "diff --git" not in patch:
        return "patch must include diff --git header"
    if "+++" not in patch or "---" not in patch:
        return "patch must be unified diff with --- and +++ headers"
    missing = _missing_reads_for_patch(patch, ctx)
    if missing:
        return "must read file before editing: " + ", ".join(sorted(missing))
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
        return "EXIT=124\nSTDOUT=\n\nSTDERR=command timed out"
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    return f"EXIT={result.returncode}\nSTDOUT=\n{stdout}\n\nSTDERR=\n{stderr}"


def tool_run_ruff(_: dict[str, Any], ctx: ToolContext) -> str:
    code, stdout, stderr = _run_with_exit(
        ["ruff", "check", "."], ctx.repo_path, ctx.cfg.agent_tool_timeout_sec
    )
    return f"EXIT={code}\nSTDOUT=\n{stdout}\n\nSTDERR=\n{stderr}"


def tool_run_mypy(args: dict[str, Any], ctx: ToolContext) -> str:
    target = args.get("target", "src")
    code, stdout, stderr = _run_with_exit(
        ["mypy", target], ctx.repo_path, ctx.cfg.agent_tool_timeout_sec
    )
    return f"EXIT={code}\nSTDOUT=\n{stdout}\n\nSTDERR=\n{stderr}"


def tool_run_shell(args: dict[str, Any], ctx: ToolContext) -> str:
    if not ctx.cfg.agent_allow_shell:
        return "shell tool disabled"
    command = args.get("command")
    if not command:
        raise ValueError("command is required")
    blocked = [
        "git merge",
        "git checkout",
        "git show",
        "git log",
        "git diff",
        "git status",
        "cat ",
        "grep ",
        "ls ",
        "find ",
    ]
    lower = str(command).lower()
    if any(b in lower for b in blocked):
        return "command blocked: use tools (read_file_*, rg_search, git_log, git_show, git_diff, git_status)"
    return _run(["bash", "-lc", str(command)], ctx.repo_path, ctx.cfg.agent_tool_timeout_sec)


def tool_write_file(args: dict[str, Any], ctx: ToolContext) -> str:
    path = args.get("path")
    content = args.get("content")
    if not path or content is None:
        raise ValueError("path and content are required")
    full = _safe_path(ctx.repo_path, path)
    if os.path.exists(full):
        rel = _rel_path(ctx.repo_path, path)
        if rel not in ctx.read_paths:
            return "must read file before editing"
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(str(content))
    return "file written"


def tool_str_replace_in_file(args: dict[str, Any], ctx: ToolContext) -> str:
    path = args.get("path")
    old = args.get("old")
    new = args.get("new")
    if not path or old is None or new is None:
        raise ValueError("path, old, and new are required")
    full = _safe_path(ctx.repo_path, path)
    rel = _rel_path(ctx.repo_path, path)
    if rel not in ctx.read_paths:
        return "must read file before editing"
    with open(full, "r", encoding="utf-8") as f:
        content = f.read()
    count = content.count(str(old))
    if count == 0:
        return "no matches"
    if count > 1:
        return f"ambiguous: {count} matches"
    content = content.replace(str(old), str(new))
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return "replaced"


def tool_insert_in_file(args: dict[str, Any], ctx: ToolContext) -> str:
    path = args.get("path")
    insert_after = args.get("insert_after")
    new = args.get("new")
    if not path or insert_after is None or new is None:
        raise ValueError("path, insert_after, and new are required")
    full = _safe_path(ctx.repo_path, path)
    rel = _rel_path(ctx.repo_path, path)
    if rel not in ctx.read_paths:
        return "must read file before editing"
    with open(full, "r", encoding="utf-8") as f:
        lines = f.readlines()
    match_indexes = [i for i, line in enumerate(lines) if str(insert_after) in line]
    if not match_indexes:
        return "anchor not found"
    if len(match_indexes) > 1:
        return f"ambiguous: {len(match_indexes)} anchors"
    idx = match_indexes[0]
    insert_lines = str(new).splitlines(keepends=True)
    if insert_lines and not insert_lines[-1].endswith("\n"):
        insert_lines[-1] += "\n"
    lines[idx + 1:idx + 1] = insert_lines
    with open(full, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return "inserted"


def _extract_patch_paths(patch: str) -> set[str]:
    paths: set[str] = set()
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                paths.add(parts[2])
                paths.add(parts[3])
        elif line.startswith("+++ "):
            parts = line.split()
            if len(parts) >= 2:
                paths.add(parts[1])
        elif line.startswith("--- "):
            parts = line.split()
            if len(parts) >= 2:
                paths.add(parts[1])
    cleaned: set[str] = set()
    for path in paths:
        if path == "/dev/null":
            continue
        if path.startswith("a/") or path.startswith("b/"):
            path = path[2:]
        cleaned.add(path)
    return cleaned


def _missing_reads_for_patch(patch: str, ctx: ToolContext) -> set[str]:
    missing: set[str] = set()
    for path in _extract_patch_paths(patch):
        try:
            full = _safe_path(ctx.repo_path, path)
        except ValueError:
            continue
        if not os.path.exists(full):
            continue
        rel = _rel_path(ctx.repo_path, path)
        if rel not in ctx.read_paths:
            missing.add(rel)
    return missing


def tool_todo_init(args: dict[str, Any], ctx: ToolContext) -> str:
    items = args.get("items")
    if not isinstance(items, list):
        raise ValueError("items must be a list of strings")
    if not all(isinstance(i, str) for i in items):
        return "invalid items: list must contain only strings"
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
    if status not in {"pending", "running", "done", "blocked"}:
        return "invalid status"
    ok = ctx.todo.set_status(item_id, status)
    return "ok" if ok else "not found"


def build_tools(cfg: Config) -> dict[str, ToolHandler]:
    tools: dict[str, ToolHandler] = {
        "list_files": tool_list_files,
        "repo_tree": tool_repo_tree,
        "read_file_range": tool_read_file_range,
        "read_file_head": tool_read_file_head,
        "rg_search": tool_rg_search,
        "grep_search": tool_grep_search,
        "glob_files": tool_glob_files,
        "ast_grep": tool_ast_grep,
        "git_diff": tool_git_diff,
        "git_status": tool_git_status,
        "git_log": tool_git_log,
        "git_show": tool_git_show,
        "apply_patch": tool_apply_patch,
        "format_code": tool_format_code,
        "run_pytest": tool_run_pytest,
        "run_ruff": tool_run_ruff,
        "run_mypy": tool_run_mypy,
        "write_file": tool_write_file,
        "str_replace_in_file": tool_str_replace_in_file,
        "insert_in_file": tool_insert_in_file,
        "todo_init": tool_todo_init,
        "todo_list": tool_todo_list,
        "todo_set": tool_todo_set,
    }
    if cfg.agent_allow_shell:
        tools["run_shell"] = tool_run_shell
    return tools


def build_readonly_tools(cfg: Config) -> dict[str, ToolHandler]:
    allowed = {
        "list_files",
        "repo_tree",
        "read_file_range",
        "read_file_head",
        "rg_search",
        "grep_search",
        "glob_files",
        "ast_grep",
        "git_diff",
        "git_status",
        "git_log",
        "git_show",
        "todo_init",
        "todo_list",
        "todo_set",
    }
    tools = build_tools(cfg)
    return {name: tool for name, tool in tools.items() if name in allowed}


def tool_list_lines(cfg: Config) -> list[str]:
    return [line for _, line in _tool_description_pairs(cfg)]


def tool_list_lines_for(names: set[str], cfg: Config) -> list[str]:
    return [line for name, line in _tool_description_pairs(cfg) if name in names]


def _tool_description_pairs(cfg: Config) -> list[tuple[str, str]]:
    pairs = [
        ("list_files", "- list_files {pattern?(glob), max_results?} — list matching files (fast inventory)"),
        ("repo_tree", "- repo_tree {max_depth?} — shallow tree view (structure)"),
        ("read_file_range", "- read_file_range {path, start, end} — read exact lines (use before edit)"),
        ("read_file_head", "- read_file_head {path, n} — read file head (use before edit)"),
        ("rg_search", "- rg_search {query, globs?, context_lines?} — ripgrep search in repo"),
        ("grep_search", "- grep_search {query, globs?, context_lines?} — alias to rg_search"),
        ("glob_files", "- glob_files {pattern} — glob search for files"),
        ("ast_grep", "- ast_grep {pattern, language?} — AST search (requires ast-grep)"),
        ("git_diff", "- git_diff {} — current diff"),
        ("git_status", "- git_status {} — git status porcelain"),
        ("git_log", "- git_log {max_count?} — recent commits (oneline)"),
        ("git_show", "- git_show {ref, path?} — show commit or file at ref"),
        ("apply_patch", "- apply_patch {patch} — apply unified diff (preferred edit)"),
        ("write_file", "- write_file {path, content} — overwrite file (use if patch fails)"),
        ("str_replace_in_file", "- str_replace_in_file {path, old, new} — replace single exact match"),
        ("insert_in_file", "- insert_in_file {path, insert_after, new} — insert after unique anchor"),
        ("format_code", "- format_code {} — ruff format"),
        ("run_pytest", "- run_pytest {target?} — run tests (uses PYTHONPATH=src)"),
        ("run_ruff", "- run_ruff {} — ruff check"),
        ("run_mypy", "- run_mypy {target?} — mypy"),
        ("todo_init", "- todo_init {items} — create TODO list"),
        ("todo_list", "- todo_list {} — show TODO list"),
        ("todo_set", "- todo_set {id, status} — update TODO item (pending|running|done|blocked)"),
    ]
    if cfg.agent_allow_shell:
        pairs.append(("run_shell", "- run_shell {command}"))
    return pairs
