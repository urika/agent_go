import subprocess
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = ["analyze_project", "get_git_info", "get_resource_map", "repo_health_signal", "get_special_file_count", "init_git_repo", "resolve_project_id",
           "get_dirty_files", "commit_baseline"]

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


def get_dirty_files(repo: Path) -> list[str]:
    """返回主工作区未提交的改动文件列表（git status --porcelain）。

    仅当 repo 是 git 仓库时有效；非 git 仓库或命令失败返回空列表。
    路径为相对 repo 的路径，去掉 porcelain 状态前缀（如 ' M src/a.py' → 'src/a.py'）。
    """
    if not (repo / ".git").exists():
        return []
    try:
        r = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo),
                           capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    files: list[str] = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        # porcelain 格式：前两字符为状态码，其后为路径（重命名为 'old -> new'）
        path = line[3:] if len(line) > 3 else line.strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path.strip())
    return files


def commit_baseline(repo: Path, message: str = "") -> tuple[bool, str, str]:
    """把主工作区未提交改动提交为基线 commit。返回 (success, commit_hash, error_msg)。

    用于 A3 未提交基线处理：worktree 从 HEAD 建，看不到主工作区未提交改动，
    显式 commit 让子任务基于正确基线。identity 通过 -c 内联传入，零副作用。
    若无改动可提交，返回 (True, 当前HEAD, "")（幂等）。
    """
    if not (repo / ".git").exists():
        return False, "", "not a git repo"
    dirty = get_dirty_files(repo)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                          capture_output=True, text=True)
    head_hash = head.stdout.strip() if head.returncode == 0 else ""
    if not dirty:
        return True, head_hash, ""
    inline_identity = ["-c", "user.email=agent_go@local", "-c", "user.name=agent_go"]
    try:
        add = subprocess.run(["git", "add", "-A"], cwd=str(repo),
                             capture_output=True, text=True)
        if add.returncode != 0:
            return False, "", add.stderr.strip()[:200]
        msg = message or "chore(baseline): commit uncommitted changes as agent_go baseline"
        com = subprocess.run(["git"] + inline_identity + ["commit", "-m", msg],
                             cwd=str(repo), capture_output=True, text=True)
        if com.returncode != 0:
            return False, "", com.stderr.strip()[:200]
        new_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                                  capture_output=True, text=True)
        return True, (new_head.stdout.strip() if new_head.returncode == 0 else ""), ""
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        return False, "", str(e)[:200]


def get_resource_map(repo: Path, git_info: dict[str, str]) -> dict[str, Any]:
    """生成共享资源清单。"""
    resources: dict[str, Any] = {
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

    # repo 健康信号（planner/worker 识别异常根目录：非 git/临时目录/特殊文件）
    resources["repo_health"] = repo_health_signal(repo)

    return resources


def get_special_file_count(repo: Path) -> int:
    """统计 repo 下的特殊文件（socket/FIFO/设备）数量（用于 prompt 健康信号）。

    用 os.scandir 递归扫描（上层目录若含海量文件会较慢，故只下探一层 + 顶层，
    足够暴露 /private/tmp 这类系统临时目录里的 socket）。任何错误 fail-open 返回 0。
    """
    import os
    import stat as _stat

    count = 0
    try:
        for root, dirs, files in os.walk(str(repo)):
            parts = str(Path(root).relative_to(repo)).split(os.sep) if root != str(repo) else []
            # 只扫顶层 + 一层子目录，避免拖慢 /private/tmp 等大目录
            if len(parts) > 1:
                dirs[:] = []
                files = []
                continue
            for name in list(files):
                p = Path(root) / name
                try:
                    st = p.stat()
                except OSError:
                    continue
                if not (_stat.S_ISREG(st.st_mode) or _stat.S_ISDIR(st.st_mode)):
                    count += 1
    except (OSError, ValueError):
        return 0
    return count


def repo_health_signal(repo: Path) -> str:
    """生成一行 repo 健康信号，注入 Plan prompt，让 planner 识别异常根目录。

    覆盖信号：
    - 是否 git 项目；
    - 顶层疑似系统/临时目录（/tmp、/private/tmp、/var/tmp、名称含 temp/tmp）；
    - 特殊文件（socket/FIFO/设备）数量（>0 时提示：无法拷入 worktree，可能影响执行）；
    - 顶层条目过少（空/近似空项目）。

    返回单行字符串；获取失败返回空串（调用方拼接时自然忽略）。
    """
    if not repo.exists():
        return f"repo 路径不存在: {repo}"
    try:
        top = [p for p in repo.iterdir()] if repo.is_dir() else []
    except OSError:
        return f"repo 目录不可读: {repo}"
    n_top = len(top)
    is_git = (repo / ".git").exists()
    special = get_special_file_count(repo)

    path_str = str(repo)
    sys_temp = (path_str in ("/tmp", "/private/tmp", "/var/tmp")
                or any("/tmp/" in path_str or f"/{tag}/" in path_str
                       for tag in ("tmp", "temp"))
                or path_str.endswith(("/tmp", "/temp")))
    signals = []
    if is_git:
        signals.append("git 项目")
    else:
        signals.append("非 git 项目（执行时将整目录拷入 worktree）")
    if sys_temp:
        signals.append("⚠️ 疑似系统临时目录（可能含 socket/特殊文件）")
    if special > 0:
        signals.append(f"⚠️ 含 {special} 个特殊文件（socket/FIFO/设备，无法拷入 worktree）")
    if n_top <= 1:
        signals.append("⚠️ 顶层几乎为空（可能不是项目仓库）")
    return " · ".join(signals)


def resolve_project_id(repo: Path) -> str:
    """解析项目身份标识，用于会话归属、记忆分片等场景。

    优先级（从高到低）：
    1. 向上遍历目录树查找 .agent-go-project marker 文件
       → 以其所在目录的 sha256 前 16 位为项目 ID
       → 支持 monorepo 子项目显式分片
    2. git remote get-url origin
       → 每个物理 git 仓库独立身份
    3. 当前目录路径的 sha256
       → 非 git 项目也有稳定 ID
    """
    import hashlib
    # 1. Marker 文件
    marker = _find_marker_upwards(repo, ".agent-go-project")
    if marker is not None:
        return hashlib.sha256(marker.encode()).hexdigest()[:16]
    # 2. Git remote
    git_info = get_git_info(repo)
    if git_info.get("remote"):
        return hashlib.sha256(git_info["remote"].encode()).hexdigest()[:16]
    # 3. 目录路径兜底
    return hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]


def _find_marker_upwards(start: Path, marker_name: str) -> Optional[str]:
    """从 start 目录向上遍历，返回第一个 marker 文件的绝对路径，找不到返回 None。

    遇到 / 或文件系统边界时停止（避免无限循环）。
    """
    current = start.resolve()
    for _ in range(64):
        marker = current / marker_name
        if marker.exists() and marker.is_file():
            return str(marker)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None
