import sys, os, subprocess, json, re, time, threading, shlex, signal, logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

def read_reference_docs(doc_paths, repo, logger):
    contents = []
    for path_str in doc_paths:
        path = (repo / path_str).resolve()
        # 防止路径穿越：确保路径在 repo 范围内
        if not str(path).startswith(str(repo.resolve())):
            logger.warning(f"路径越界，已拒绝: {path_str}")
            continue
        if not path.exists():
            logger.warning(f"文档不存在: {path}")
            continue
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                max_len = 15000
                if len(text) > max_len:
                    text = text[:max_len] + f"\n... [截断，原 {len(text)} 字符]"
                contents.append(f"===== {path.name} =====\n{text}\n===== 结束 =====")
                logger.info(f"已读文档: {path} ({len(text)} 字符)")
            except Exception as e:
                logger.warning(f"读取失败 {path}: {e}")
        elif path.is_dir():
            for md_file in sorted(path.rglob("*.md")):
                try:
                    text = md_file.read_text(encoding="utf-8", errors="replace")
                    max_len = 8000
                    if len(text) > max_len:
                        text = text[:max_len] + "\n... [截断]"
                    contents.append(f"===== {md_file.name} =====\n{text}\n===== 结束 =====")
                    logger.info(f"已读文档: {md_file} ({len(text)} 字符)")
                except Exception as e:
                    logger.warning(f"读取失败 {md_file}: {e}")
    return "\n".join(contents) if contents else ""

SAFE_VERIFICATION_PREFIXES = [
    "go test", "go vet", "go build", "go run",
    "pytest", "python -m pytest", "python3 -m pytest",
    "npm test", "npm run", "npx", "yarn test", "pnpm test",
    "cargo test", "cargo build", "cargo clippy",
    "make test", "make check",
    "mvn test", "gradle test",
    "jest", "vitest", "mocha",
    "phpunit", "phpstan", "phpcs",
    "rspec", "rubocop",
    "deno test", "deno lint",
    "ruff", "mypy", "black --check", "isort --check",
    "shellcheck", "shfmt",
    "gh", "git diff", "git status", "git log",
]

# shell 注入特征（精确模式，避免误伤合法的验证参数）
_SHELL_CHAIN = re.compile(r'[;&]|\b(&&|\|\|)\b')            # 命令链: ; && ||
_SHELL_SUBST = re.compile(r'\$\(|`[^`]+`|\$\{')            # 命令替换: $() `` ${
_SHELL_PIPE_EXEC = re.compile(r'\b(curl|wget)\b.*\|.*\b(ba)?sh\b')  # curl|sh
_SHELL_DESTROY = re.compile(r'\brm\s+-r[^ ]*\s+[/~]')      # 危险 rm
_SHELL_OUTPUT_REDIR = re.compile(r'(?<![12])>>?\s*\S')      # 输出重定向（排除 2>&1, 1>&2）
_SHELL_INPUT_REDIR = re.compile(r'(?<!<\s)<\s*\S')          # 输入重定向

def _is_safe_verification_command(command):
    """检查验证命令在 shell=True 降级前是否安全。

    策略：先匹配白名单前缀，再检查 shell 注入特征。
    只拦截明确危险的模式，不因单个特殊字符（如 $ 在正则中）误判。
    """
    cmd = command.strip()
    cmd_lower = cmd.lower()

    # 第一关：命令前缀必须在白名单中
    if not any(cmd_lower.startswith(p.lower()) for p in SAFE_VERIFICATION_PREFIXES):
        return False

    # 第二关：检查明确的 shell 注入特征
    if _SHELL_CHAIN.search(cmd):
        return False
    if _SHELL_SUBST.search(cmd):
        return False
    if _SHELL_PIPE_EXEC.search(cmd_lower):
        return False
    if _SHELL_DESTROY.search(cmd):
        return False
    if _SHELL_OUTPUT_REDIR.search(cmd):
        return False
    if _SHELL_INPUT_REDIR.search(cmd):
        return False

    return True

def _safe_append_to_file(filepath, text, logger, max_retries=10):
    """线程安全的文件追加写入，使用锁文件机制防止并发冲突。"""
    lock_path = filepath.with_suffix(filepath.suffix + ".lock")
    for attempt in range(max_retries):
        try:
            # 尝试创建锁文件（原子操作）
            with open(lock_path, "x") as _:
                pass
            break
        except FileExistsError:
            time.sleep(0.1 * (attempt + 1))
    else:
        logger.warning(f"无法获取文件锁: {lock_path}，直接写入")
    try:
        # 使用原子追加方式，避免读取-修改-写入的竞态条件
        with open(filepath, 'a', encoding='utf-8') as f:
            f.write(text)
    finally:
        lock_path.unlink(missing_ok=True)

def _slugify(text, max_len=30):
    """将任务标题转为分支名适用的短标识。"""
    slug = re.sub(r'[^a-zA-Z0-9一-鿿]+', '-', text).strip('-')
    return slug[:max_len] if len(slug) > max_len else slug

def _detect_commit_prefix(title):
    """根据标题关键词检测 Conventional Commits 类型前缀。"""
    title_lower = title.lower()
    if any(kw in title for kw in ["实现", "新增", "添加", "增加"]) or \
       any(kw in title_lower for kw in ["add", "implement", "feature", "new", "create", "introduce"]):
        return "feat"
    elif any(kw in title for kw in ["修复", "修正", "解决"]) or \
         any(kw in title_lower for kw in ["fix", "bug", "hotfix", "patch", "resolve", "correct"]):
        return "fix"
    elif any(kw in title for kw in ["重构", "优化", "改进"]) or \
         any(kw in title_lower for kw in ["refactor", "optimize", "improve", "restructure"]):
        return "refactor"
    elif any(kw in title for kw in ["文档", "注释"]) or \
         any(kw in title_lower for kw in ["docs", "document", "readme", "comment"]):
        return "docs"
    elif any(kw in title for kw in ["测试"]) or \
         any(kw in title_lower for kw in ["test", "spec", "coverage"]):
        return "test"
    elif any(kw in title for kw in ["配置", "依赖", "升级"]) or \
         any(kw in title_lower for kw in ["chore", "bump", "upgrade", "update", "config", "dep", "dependency"]):
        return "chore"
    else:
        return "chore"

def _detect_commit_scope(title):
    """从标题中提取 scope（圆括号显式声明 或 常见模块名关键词）。"""
    scope_match = re.search(r'\((\w+)\)', title)
    if scope_match:
        return scope_match.group(1)
    common_modules = ["auth", "api", "ui", "db", "config", "test", "doc",
                      "cli", "server", "client", "middleware", "schema"]
    title_lower = title.lower()
    for mod in common_modules:
        # 前后不能是 ASCII 字母（允许中文、数字、空格等紧邻）
        if re.search(r'(?<![a-zA-Z])' + re.escape(mod) + r'(?![a-zA-Z])', title_lower):
            return mod
    return ""

def _format_commit(title, issue_ref="", sub_id="", scope=""):
    """生成 Conventional Commits 格式的提交消息（支持中英文标题 + scope）。"""
    prefix = _detect_commit_prefix(title)
    if scope:
        msg = f"{prefix}({scope}): {title}"
    else:
        msg = f"{prefix}: {title}"
    if issue_ref:
        msg += f"\n\nRefs: #{issue_ref}"
    msg += f"\n\nagent_go: {sub_id}"
    return msg

def _detect_tool_versions(logger):
    """检测 claude / greywall 版本并记录，返回版本信息 dict。"""
    versions = {}
    for tool in ["claude", "greywall"]:
        try:
            result = subprocess.run([tool, "--version"], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                ver = result.stdout.strip().split("\n")[0][:100]
                versions[tool] = ver
                logger.debug(f"{tool} 版本: {ver}")
            else:
                logger.debug(f"{tool} --version 失败: rc={result.returncode}")
        except FileNotFoundError:
            logger.debug(f"{tool} 未安装")
        except Exception as e:
            logger.debug(f"{tool} 版本检测异常: {e}")
    return versions
