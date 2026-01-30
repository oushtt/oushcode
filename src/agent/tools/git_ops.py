from __future__ import annotations

import os
import subprocess
from typing import Iterable


def run_git(args: Iterable[str], cwd: str, env: dict[str, str] | None = None) -> None:
    cmd = ["git", *args]
    subprocess.run(cmd, cwd=cwd, env=env, check=True, capture_output=True, text=True)


def run_git_output(args: Iterable[str], cwd: str) -> str:
    cmd = ["git", *args]
    result = subprocess.run(cmd, cwd=cwd, check=False, capture_output=True, text=True)
    output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    return output.strip()

def clone_repo(repo_url: str, dest: str) -> None:
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    run_git(["clone", repo_url, dest], cwd=parent)


def ensure_mirror(repo_url: str, mirror_path: str) -> None:
    parent = os.path.dirname(mirror_path)
    os.makedirs(parent, exist_ok=True)
    if not os.path.exists(mirror_path):
        run_git(["clone", "--mirror", repo_url, mirror_path], cwd=parent)
        return
    run_git(["remote", "set-url", "origin", repo_url], cwd=mirror_path)
    run_git(["fetch", "--prune"], cwd=mirror_path)


def clone_from_mirror(mirror_path: str, dest: str) -> None:
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    run_git(["clone", "--shared", mirror_path, dest], cwd=parent)


def set_origin(repo_url: str, cwd: str) -> None:
    run_git(["remote", "set-url", "origin", repo_url], cwd=cwd)


def create_branch(branch: str, cwd: str) -> None:
    run_git(["checkout", "-b", branch], cwd=cwd)


def add_all_and_commit(message: str, cwd: str, env: dict[str, str]) -> None:
    run_git(["add", "-A"], cwd=cwd, env=env)
    run_git(["commit", "-m", message], cwd=cwd, env=env)


def push_branch(branch: str, cwd: str, env: dict[str, str]) -> None:
    run_git(["push", "-u", "origin", branch], cwd=cwd, env=env)


def git_status_porcelain(cwd: str) -> str:
    return run_git_output(["status", "--porcelain"], cwd=cwd)
