"""Agent 工具执行器 — Read/Write/Edit/Bash/Grep/Glob/View 工具。

供 agent_loop.py 使用，在直接 API 模式下执行 LLM 产生的 tool_call。
每个工具是一个独立函数 + JSON schema，通过 ToolRegistry 统一分发。
B2（阶段十三）补齐 ACI 风格导航工具（Grep/Glob/View）与 explore 只读模式。
"""

import fnmatch
import os
import re
import subprocess
import shlex
import logging
from pathlib import Path

_logger = logging.getLogger(__name__)


def _read_file(file_path: str, worktree: Path) -> dict:
    """读取 worktree 中的文件。"""
    path = worktree / file_path
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}
    try:
        content = path.read_text(encoding="utf-8")
        return {"success": True, "output": content}
    except Exception as e:
        return {"success": False, "error": f"读取失败: {e}"}


def _write_file(file_path: str, content: str, worktree: Path) -> dict:
    """写入文件（创建或覆盖）。"""
    path = worktree / file_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"success": True, "output": f"已写入 {len(content)} 字符到 {file_path}"}
    except Exception as e:
        return {"success": False, "error": f"写入失败: {e}"}


def _edit_file(file_path: str, old_string: str, new_string: str, worktree: Path) -> dict:
    """精确替换文件中的字符串（类似 Claude Code Edit 语义）。

    替换逻辑：
    1. 读取文件全部内容
    2. 查找 old_string（精确匹配，唯一）
    3. 替换为 new_string
    4. 写回文件
    """
    path = worktree / file_path
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}
    try:
        full_text = path.read_text(encoding="utf-8")
    except Exception as e:
        return {"success": False, "error": f"读取失败: {e}"}

    count = full_text.count(old_string)
    if count == 0:
        return {"success": False, "error": f"未在 {file_path} 中找到匹配的文本"}
    if count > 1:
        return {"success": False, "error": f"在 {file_path} 中找到 {count} 处匹配，请使用更精确的 old_string"}

    new_text = full_text.replace(old_string, new_string, 1)
    try:
        path.write_text(new_text, encoding="utf-8")
        return {"success": True, "output": f"已替换 {file_path} 中的 1 处文本"}
    except Exception as e:
        return {"success": False, "error": f"写入失败: {e}"}


def _bash(command: str, worktree: Path) -> dict:
    """在 worktree 中执行 shell 命令。"""
    # 优先使用 shlex 分词精确匹配，避免误伤引号内内容（如 python -c "import os; print()"）
    try:
        tokens = [t.lower() for t in shlex.split(command)]
        shlex_ok = True
    except ValueError:
        tokens = None
        shlex_ok = False

    # 禁止的命令名（精确 token 匹配）
    blocked_commands = {
        "rm", "mv", "cp", "chmod", "chown",
        "sudo", "su", "mkfs", "dd", "wget", "curl",
        "kill", "pkill", "renice", "nohup",
    }

    if shlex_ok and tokens:
        for token in tokens:
            if token in blocked_commands:
                return {"success": False, "error": f"禁止的命令: {token}"}
        # 引号外的 shell 元字符检测（剥离引号内容后检查）
        _unquoted = re.sub(r'"[^"]*"', '', command)
        _unquoted = re.sub(r"'[^']*'", '', _unquoted)
        for pat in ("|", ";", "&&", "||", ">", ">>"):
            if pat in _unquoted:
                return {"success": False, "error": f"禁止的命令: shell 操作符 {pat}"}
        # 多词规则检测（git push / git remote / git config 等）
        _cmd_lower = command.strip().lower()
        _multi_word_blocked = ["git push", "git remote", "git config"]
        for pat in _multi_word_blocked:
            if pat in _cmd_lower:
                return {"success": False, "error": f"禁止的命令: {pat}"}
    else:
        # shlex 解析失败，回退到保守的子串匹配
        cmd_lower = command.strip().lower()
        for cmd in blocked_commands:
            if cmd in cmd_lower:
                return {"success": False, "error": f"禁止的命令: {cmd}"}
        blocked_substrings = ["|", ";", ">", "&&", "||", "git push", "git remote"]
        for pat in blocked_substrings:
            if pat in cmd_lower:
                return {"success": False, "error": f"禁止的命令: {pat}"}

    try:
        result = subprocess.run(
            shlex.split(command),
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = result.stdout
        if result.stderr:
            output += "\n" + result.stderr
        return {"success": result.returncode == 0, "output": output[:8000]}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "命令执行超时 (60s)"}
    except FileNotFoundError:
        return {"success": False, "error": f"命令未找到: {command.split()[0]}"}
    except Exception as e:
        return {"success": False, "error": f"执行失败: {e}"}


# 遍历时的默认跳过目录（依赖/构建产物/VCS 元数据）
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
_MAX_RESULTS = 200


def _iter_files(root: Path):
    """遍历 root 下的文件（跳过依赖/缓存目录），产出相对路径。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            yield Path(dirpath) / fn


def _grep_files(pattern: str, worktree: Path, path: str = "", glob: str = "") -> dict:
    """按正则在 worktree 中搜索文件内容，返回 `相对路径:行号: 内容` 列表。"""
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return {"success": False, "error": f"无效正则: {e}"}
    root = (worktree / path) if path else worktree
    if not root.exists():
        return {"success": False, "error": f"路径不存在: {path or '.'}"}
    try:
        matches = []
        files = [root] if root.is_file() else _iter_files(root)
        for f in files:
            rel = str(f.relative_to(worktree))
            if glob and not fnmatch.fnmatch(f.name, glob) and not fnmatch.fnmatch(rel, glob):
                continue
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        if rx.search(line):
                            matches.append(f"{rel}:{i}: {line.rstrip()}")
                            if len(matches) >= _MAX_RESULTS:
                                break
            except (OSError, UnicodeError):
                continue
            if len(matches) >= _MAX_RESULTS:
                break
        if not matches:
            return {"success": True, "output": "（无匹配）"}
        out = "\n".join(matches)
        if len(matches) >= _MAX_RESULTS:
            out += f"\n... （结果截断，仅显示前 {_MAX_RESULTS} 条）"
        return {"success": True, "output": out}
    except Exception as e:
        return {"success": False, "error": f"搜索失败: {e}"}


def _glob_files(pattern: str, worktree: Path) -> dict:
    """按 glob 模式列出 worktree 中的文件（如 `**/*.py`），返回相对路径列表。"""
    try:
        hits = sorted(
            str(f.relative_to(worktree))
            for f in worktree.glob(pattern)
            if f.is_file() and not any(part in _SKIP_DIRS for part in f.relative_to(worktree).parts)
        )
        if not hits:
            return {"success": True, "output": "（无匹配文件）"}
        out = "\n".join(hits[:_MAX_RESULTS])
        if len(hits) > _MAX_RESULTS:
            out += f"\n... （共 {len(hits)} 个文件，仅显示前 {_MAX_RESULTS} 个）"
        return {"success": True, "output": out}
    except Exception as e:
        return {"success": False, "error": f"glob 失败: {e}"}


def _view_file(file_path: str, worktree: Path, offset: int = 1, limit: int = 0) -> dict:
    """长文件导航：按行号范围读取（offset 为 1-based 起始行，limit 为行数，0=读到文件尾）。

    返回带行号的内容（`  12\\t...`），便于 LLM 引用具体行。
    """
    path = worktree / file_path
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        return {"success": False, "error": f"读取失败: {e}"}
    total = len(lines)
    offset = max(1, int(offset or 1))
    if offset > total and total > 0:
        return {"success": False, "error": f"offset={offset} 超出文件行数（共 {total} 行）"}
    end = total if not limit else min(total, offset + int(limit) - 1)
    numbered = [f"{i:>6}\t{lines[i - 1]}" for i in range(offset, end + 1)]
    header = f"{file_path}（第 {offset}-{end} 行 / 共 {total} 行）"
    return {"success": True, "output": header + "\n" + "\n".join(numbered)}


# ═══════════════════════════════════════════════════════════════
# Tool Registry
# ═══════════════════════════════════════════════════════════════

TOOL_DEFINITIONS = [
    {
        "name": "Read",
        "description": "读取 worktree 中的文件内容。返回文件的完整文本。",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "文件路径（相对于 worktree 根目录）",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "Write",
        "description": "写入文件内容。如果文件不存在则创建，如果已存在则覆盖。",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "文件路径（相对于 worktree 根目录）",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的完整文件内容",
                },
            },
            "required": ["file_path", "content"],
        },
    },
    {
        "name": "Edit",
        "description": "精确替换文件中的一段文本。old_string 必须在文件中唯一出现。适用于修改而非完全重写文件的场景。",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "文件路径（相对于 worktree 根目录）",
                },
                "old_string": {
                    "type": "string",
                    "description": "要替换的原始文本（必须在文件中唯一出现）",
                },
                "new_string": {
                    "type": "string",
                    "description": "替换后的新文本",
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    {
        "name": "Bash",
        "description": "在 worktree 中执行 shell 命令。仅允许读取操作（ls/cat/grep/find/git status 等），禁止写入/删除/网络命令。",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 shell 命令",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "Grep",
        "description": "按正则在 worktree 中搜索文件内容，返回 相对路径:行号: 内容 列表。比 Bash grep 更适合代码定位。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "path": {"type": "string", "description": "搜索起点（相对 worktree，默认根目录）"},
                "glob": {"type": "string", "description": "文件名过滤（如 *.py）"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "Glob",
        "description": "按 glob 模式列出 worktree 中的文件（如 **/*.py），返回相对路径列表。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "glob 模式"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "View",
        "description": "长文件导航：按行号范围读取文件（返回带行号内容）。offset 为 1-based 起始行，limit 为行数（0=读到文件尾）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径（相对于 worktree 根目录）"},
                "offset": {"type": "integer", "description": "起始行（1-based，默认 1）"},
                "limit": {"type": "integer", "description": "读取行数（0=到文件尾）"},
            },
            "required": ["file_path"],
        },
    },
]


# explore 只读模式允许的工具（Bash 自身已禁写入/删除/网络命令）
READONLY_TOOLS = frozenset({"Read", "Grep", "Glob", "View", "Bash"})


class ToolRegistry:
    """工具注册表 — 按名称分发工具调用。"""

    @staticmethod
    def definitions(readonly: bool = False) -> list[dict]:
        """返回工具的 JSON schema 列表（用于 LLM API 的 tools 参数）。

        readonly=True（explore 只读模式）时只暴露 READONLY_TOOLS。
        """
        if not readonly:
            return TOOL_DEFINITIONS
        return [d for d in TOOL_DEFINITIONS if d["name"] in READONLY_TOOLS]

    @staticmethod
    def execute(tool_name: str, arguments: dict, worktree: Path, readonly: bool = False) -> dict:
        """执行一个工具调用。

        Args:
            tool_name: 工具名称（Read / Write / Edit / Bash / Grep / Glob / View）
            arguments: 工具参数 dict
            worktree: worktree 路径
            readonly: True 时屏蔽写工具（explore 只读模式）

        Returns:
            dict: {"success": bool, "output": str} 或 {"success": False, "error": str}
        """
        _logger.debug(f"[ToolRegistry] {tool_name}({arguments})")
        if readonly and tool_name not in READONLY_TOOLS:
            return {"success": False,
                    "error": f"只读模式：工具 {tool_name} 不可用（允许: {sorted(READONLY_TOOLS)}）"}
        try:
            if tool_name == "Read":
                return _read_file(arguments["file_path"], worktree)
            elif tool_name == "Write":
                return _write_file(arguments["file_path"], arguments["content"], worktree)
            elif tool_name == "Edit":
                return _edit_file(arguments["file_path"], arguments["old_string"], arguments["new_string"], worktree)
            elif tool_name == "Bash":
                return _bash(arguments["command"], worktree)
            elif tool_name == "Grep":
                return _grep_files(arguments["pattern"], worktree,
                                   arguments.get("path", ""), arguments.get("glob", ""))
            elif tool_name == "Glob":
                return _glob_files(arguments["pattern"], worktree)
            elif tool_name == "View":
                return _view_file(arguments["file_path"], worktree,
                                  arguments.get("offset", 1), arguments.get("limit", 0))
            else:
                return {"success": False, "error": f"未知工具: {tool_name}"}
        except KeyError as e:
            # LLM 产生缺参 tool_call 时返回错误而非抛 KeyError 使 AgentLoop 崩溃
            return {"success": False, "error": f"缺少参数: {e}"}
