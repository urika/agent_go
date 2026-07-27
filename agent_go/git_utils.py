import subprocess, logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["analyze_project", "get_git_info", "get_resource_map", "init_git_repo"]

def analyze_project(repo: Path) -> str:
    """分析项目结构，返回文件列表和关键目录。"""
    try:
        if (repo / ".git").exists():
            result = subprocess.run(["git", "ls-files"], cwd=str(repo), capture_output=True, text=True, timeout=5)
            files = result.stdout.strip().split("\n")[:50]
            return "\n".join(files)
        else:
            result = subprocess.run(["find", ".", "-maxdepth", "2", "-type", "f"], cwd=str(repo), capture_output=True, text=True, timeout=5)
            files = result.stdout.strip().split("\n")[:30]
            return "\n".join(f[2:] if f.startswith("./") else f for f in files)
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.debug("Failed to analyze project: %s", e)
        return ""

def get_git_info(repo: Path) -> dict[str, str]:
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

def _worktree_create(repo: Path, branch: str, worktree_path: Path) -> tuple[bool, str]:
    """创建 git worktree。返回 (success: bool, error_message: str)。"""
    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_path), "HEAD"],
        cwd=str(repo), capture_output=True
    )
    if result.returncode != 0:
        return False, result.stderr.decode("utf-8", errors="replace").strip()[:200]
    return True, ""


def _worktree_remove(repo: Path, worktree_path: Path) -> tuple[bool, str]:
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


def _worktree_prune(repo: Path) -> tuple[bool, str]:
    """清理失效 worktree 记录。返回 (success: bool, error_message: str)。"""
    result = subprocess.run(
        ["git", "worktree", "prune"],
        cwd=str(repo), capture_output=True
    )
    if result.returncode != 0:
        return False, result.stderr.decode("utf-8", errors="replace").strip()[:200]
    return True, ""


def _set_gc_auto(repo: Path, value: str = "0") -> tuple[str, bool, str]:
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


def init_git_repo(repo: Path) -> tuple[bool, str]:
    """初始化一个本地 git 仓库（含首次 commit）。返回 (success, error_msg)。

    用于 --auto-init：目标目录非 git 仓库时，自动 init + 首次提交，
    保证 worktree / commit / tag / merge 机制可用。
    identity 通过 -c 内联传入，不写 repo-local config、不依赖全局 config
    （CI / 容器 / 陌生机器都能跑，零副作用）。
    空目录会自动创建 .gitkeep 以确保 commit 成功。
    """
    inline_identity = [
        "-c", "user.email=agent_go@local",
        "-c", "user.name=agent_go",
    ]
    try:
        # 1. git init -b main（git < 2.28 不支持 -b，回退到 init + branch -m）
        r = subprocess.run(["git", "init", "-b", "main"], cwd=str(repo),
                           capture_output=True, text=True)
        if r.returncode != 0:
            r = subprocess.run(["git", "init"], cwd=str(repo),
                               capture_output=True, text=True)
            if r.returncode != 0:
                return False, r.stderr.strip()[:200]
            subprocess.run(["git", "branch", "-m", "main"],
                           cwd=str(repo), capture_output=True)

        # 2. 空目录保护：git commit 需要至少一个文件
        files = [p for p in repo.iterdir() if p.name != ".git"]
        if not files:
            (repo / ".gitkeep").write_text("", encoding="utf-8")

        # 3. git add -A
        r = subprocess.run(["git", "add", "-A"], cwd=str(repo),
                           capture_output=True, text=True)
        if r.returncode != 0:
            return False, r.stderr.strip()[:200]

        # 4. git commit（用 -c 内联 identity）
        r = subprocess.run(["git"] + inline_identity +
                           ["commit", "-m", "init (auto-created by agent_go)"],
                           cwd=str(repo), capture_output=True, text=True)
        if r.returncode != 0:
            return False, r.stderr.strip()[:200]
        return True, ""
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        return False, str(e)[:200]


def get_resource_map(repo: Path, git_info: dict[str, str]) -> dict[str, Any]:
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
