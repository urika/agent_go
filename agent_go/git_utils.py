import sys, os, subprocess, json, re, time, threading, shlex, signal, logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

__all__ = ["analyze_project", "get_git_info", "get_resource_map"]

logger = logging.getLogger(__name__)

def analyze_project(repo):
    """分析项目结构，返回文件列表和关键目录。"""
    try:
        if (repo / ".git").exists():
            result = subprocess.run(["git", "ls-files"], cwd=str(repo), capture_output=True, text=True, timeout=5)
            files = result.stdout.strip().split("\n")[:50]
            return "\n".join(files)
        else:
            result = subprocess.run(["find", ".", "-maxdepth", "2", "-type", "f"], cwd=str(repo), capture_output=True, text=True, timeout=5)
            files = result.stdout.strip().split("\n")[:30]
            return "\n".join(f.lstrip("./") for f in files)
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.debug("Failed to analyze project: %s", e)
        return ""

def get_git_info(repo):
    """获取 git 远程地址和当前分支。"""
    info = {"remote": "", "branch": "", "commit": ""}
    try:
        r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(repo), capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            info["remote"] = r.stdout.strip()
        b = subprocess.run(["git", "branch", "--show-current"], cwd=str(repo), capture_output=True, text=True, timeout=3)
        if b.returncode == 0:
            info["branch"] = b.stdout.strip()
        c = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(repo), capture_output=True, text=True, timeout=3)
        if c.returncode == 0:
            info["commit"] = c.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.debug("Failed to get git info: %s", e)
    return info

def _worktree_create(repo, branch, worktree_path):
    """创建 git worktree。返回 (success: bool, error_message: str)。"""
    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_path), "HEAD"],
        cwd=str(repo), capture_output=True
    )
    if result.returncode != 0:
        return False, result.stderr.decode("utf-8", errors="replace").strip()[:200]
    return True, ""


def _worktree_remove(repo, worktree_path):
    """移除 git worktree。返回 (success: bool, error_message: str)。"""
    if not worktree_path.exists():
        return True, ""
    result = subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=str(repo), capture_output=True
    )
    if result.returncode != 0:
        return False, result.stderr.decode("utf-8", errors="replace").strip()[:200]
    return True, ""


def _worktree_prune(repo):
    """清理失效 worktree 记录。返回 (success: bool, error_message: str)。"""
    result = subprocess.run(
        ["git", "worktree", "prune"],
        cwd=str(repo), capture_output=True
    )
    if result.returncode != 0:
        return False, result.stderr.decode("utf-8", errors="replace").strip()[:200]
    return True, ""


def _set_gc_auto(repo, value="0"):
    """设置 git gc.auto 值。返回 (original_value: str, success: bool, error_message: str)。"""
    orig = subprocess.run(
        ["git", "config", "gc.auto"],
        cwd=str(repo), capture_output=True, text=True
    )
    original = orig.stdout.strip() or "1"
    set_result = subprocess.run(
        ["git", "config", "gc.auto", value],
        cwd=str(repo), capture_output=True
    )
    err_msg = set_result.stderr.decode("utf-8", errors="replace").strip()[:200] if set_result.returncode != 0 else ""
    return original, set_result.returncode == 0, err_msg


def get_resource_map(repo, git_info):
    """生成共享资源清单。"""
    resources = {
        "project_root": str(repo),
        "git_remote": git_info.get("remote", ""),
        "git_branch": git_info.get("branch", ""),
        "git_commit": git_info.get("commit", ""),
        "directories": [],
        "key_files": []
    }

    # 扫描关键目录
    for subdir in ["src", "lib", "app", "components", "pages", "tests", "docs"]:
        p = repo / subdir
        if p.exists() and p.is_dir():
            resources["directories"].append(subdir)

    # 扫描关键文件
    for pattern in ["package.json", "requirements.txt", "Cargo.toml", "go.mod", "README.md", ".env.example", "docker-compose.yml"]:
        p = repo / pattern
        if p.exists():
            resources["key_files"].append(pattern)

    return resources
