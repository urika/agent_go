import sys, os, subprocess, json, re, time, threading, shlex, signal, logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime
from typing import Any

__all__ = ["read_reference_docs", "SAFE_VERIFICATION_PREFIXES"]

def read_reference_docs(doc_paths: list[str], repo: Path, logger: logging.Logger) -> str:
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

# ── 验证命令安全规则 ──────────────────────────────────────────
# 结构化白名单：每个命令定义允许的 flags（正则）和 positionals（正则）
# 值为 str 时表示 alias（指向另一个命令的规则集）
_CMD_ARG_RULES = {
    "go": {
        "test":  {"flags": r'^(-v|-run=\S+|-count=\S+|-timeout=\S+|-tags=\S+|-cover|-race|-bench=\S+|-parallel=\S+|-json|-vet=\S+|-coverpkg=\S+|-c|-o=\S+)$',
                  "positionals": r'^[\w./@\-_]+$'},
        "vet":   {"flags": r'^(-v|-composites=false|-composites=true)$',
                  "positionals": r'^[\w./\-_]+$'},
        "build": {"flags": r'^(-o=\S+|-tags=\S+|-race|-v|-x|-mod=\S+|-trimpath|-ldflags=\S+|-gcflags=\S+)$',
                  "positionals": r'^[./\-_\w]+$'},
        "run":   {"flags": r'^(-v|-x|-mod=\S+|-tags=\S+|-race|-cover)$',
                  "positionals": r'^[\w./\-_]+$'},
    },
    "pytest": {
        "": {"flags": r'^(-v|-vv|-q|-s|-x|--tb=\S+|--tb|-k=\S+|-k|--co|--collect-only|-m=\S+|-m|-n=\S+|-N|--maxfail=\S+|-r\w?|-l|--no-header|--no-summary|-p|--rootdir=\S+|--override-ini=\S+|--failed-first|--last-failed|--new-first|--durations=\S+|--cache-show|--cache-clear|-w|--exitfirst|--ignore=\S+)$',
             "positionals": r'^[\w./\-_:]+$'},
    },
    "python": {"-m pytest": "pytest"},
    "python3": {"-m pytest": "pytest"},
    "npm":    {"test": {"flags": r'^(--silent|--verbose)$', "positionals": r'^$'},
               "run":  {"flags": r'^(--silent|--verbose)$', "positionals": r'^[\w:_\-]+$'}},
    "npx":    {"": {"flags": r'^(-y|--yes|--no)$', "positionals": r'^[\w@./\-_]+$'}},
    "yarn":   {"test": {"flags": r'^(--silent|--verbose)$', "positionals": r'^$'},
               "run":  {"flags": r'^$', "positionals": r'^[\w:_\-]+$'}},
    "pnpm":   {"test": {"flags": r'^(--silent|--verbose)$', "positionals": r'^$'},
               "run":  {"flags": r'^$', "positionals": r'^[\w:_\-]+$'}},
    "cargo":  {"test":  {"flags": r'^(-v|--lib|--bin=\S+|--test=\S+|--release|--no-run)$',
                         "positionals": r'^[\w./\-_]+$'},
               "build": {"flags": r'^(-v|--release|-j=\S+|--target=\S+)$',
                         "positionals": r'^[\w./\-_]+$'},
               "clippy": {"flags": r'^(-v|--lib|--bins|--tests)$',
                          "positionals": r'^[\w./\-_:]+$'}},
    "make":   {"test":  {"flags": r'^(-n|-j=\S+|-C\s*\S+|--dry-run)$', "positionals": r'^$'},
               "check": {"flags": r'^(-n|-j=\S+|-C\s*\S+|--dry-run)$', "positionals": r'^$'}},
    "mvn":    {"test":  {"flags": r'^(-D\S+|-pl=\S+|-am|-q|-o)$', "positionals": r'^$'}},
    "gradle": {"test":  {"flags": r'^(--tests=\S+|-x|--no-daemon|--quiet|--info)$', "positionals": r'^$'}},
    "jest":   {"": {"flags": r'^(-v|--coverage|--watchAll=false|--config=\S+|--testPathPattern=\S+|--testNamePattern=\S+|--runInBand|--bail=\S+|--detectOpenHandles|--forceExit)$',
                    "positionals": r'^[\w./\-_]+$'}},
    "vitest": {"": {"flags": r'^(-v|--run|--coverage|--config=\S+|--reporter=\S+)$',
                    "positionals": r'^[\w./\-_]+$'}},
    "mocha":  {"": {"flags": r'^(-v|--recursive|--timeout=\S+|--grep=\S+|--reporter=\S+|--require=\S+)$',
                    "positionals": r'^[\w./\-_]+$'}},
    "ruff":   {"": {"flags": r'^(-v|--check|--select=\S+|--ignore=\S+|--config=\S+|--fix|--diff|--format=\S+|--output-format=\S+)$',
                    "positionals": r'^[\w./\-_]+$'}},
    "mypy":   {"": {"flags": r'^(-v|--strict|--ignore-missing-imports|--config-file=\S+|--no-error-summary|--show-error-codes|--show-error-context|--python-version=\S+|--platform=\S+|--disable-error-code=\S+|--enable-error-code=\S+)$',
                    "positionals": r'^[\w./\-_]+$'}},
    "black":  {"--check": {"flags": r'^(-v|--diff|--config=\S+|--line-length=\S+|--exclude=\S+)$',
                           "positionals": r'^[\w./\-_]+$'}},
    "isort":  {"--check": {"flags": r'^(-v|--diff|--profile=\S+|--config-file=\S+)$',
                           "positionals": r'^[\w./\-_]+$'}},
    "shellcheck": {"": {"flags": r'^(-v|--severity=\S+|--exclude=\S+|--format=\S+|--shell=\S+)$',
                        "positionals": r'^[\w./\-_]+$'}},
    "shfmt":  {"": {"flags": r'^(-v|-w|-d|-l|--indent=\S+|--write|--diff|--language-version=\S+)$',
                    "positionals": r'^[\w./\-_]+$'}},
    "gh":     {"": {"flags": r'^(-R=\S+|--repo=\S+|-q|--jq=\S+|--json=\S+|--limit=\S+)$',
                    "positionals": r'^[\w@./\-_:]+$'}},
    "git":    {"diff":   {"flags": r'^(--stat|--name-only|--name-status|--check|-w|--color=\S+|--no-color|-U=\S+)$',
                          "positionals": r'^[\w./\-_]+$'},
               "status": {"flags": r'^(--porcelain|--short|-s|--branch|-b|--show-stash)$',
                          "positionals": r'^[\w./\-_]+$'},
               "log":    {"flags": r'^(--oneline|-n=\S+|--since=\S+|--until=\S+|--format=\S+|--decorate|--no-decorate|--graph|--stat|--name-only)$',
                          "positionals": r'^[\w./\-_:^~]+$'}},
    "deno":   {"test": {"flags": r'^(-v|--allow-all|--allow-read=\S*|--allow-write=\S*|--allow-env=\S*|--allow-net=\S*|--config=\S+|--coverage=\S+)$',
                        "positionals": r'^[\w./\-_]+$'},
               "lint": {"flags": r'^(-v|--config=\S+|--rules=\S+)$',
                        "positionals": r'^[\w./\-_]+$'}},
    "phpunit":{"": {"flags": r'^(-v|--filter=\S+|--group=\S+|--testdox|--colors=\S+|--coverage-text|--configuration=\S+)$',
                    "positionals": r'^[\w./\-_]+$'}},
    "phpstan":{"": {"flags": r'^(-v|--level=\S+|--configuration=\S+|--no-progress|--error-format=\S+)$',
                    "positionals": r'^[\w./\-_]+$'}},
    "phpcs":  {"": {"flags": r'^(-v|--standard=\S+|--sniffs=\S+|--report=\S+|--colors)$',
                    "positionals": r'^[\w./\-_]+$'}},
    "rspec":  {"": {"flags": r'^(-v|--format=\S+|--tag=\S+|--order=\S+|--backtrace|--profile)$',
                    "positionals": r'^[\w./\-_]+$'}},
    "rubocop":{"": {"flags": r'^(-v|--auto-correct|--format=\S+|--config=\S+|--except=\S+|--only=\S+)$',
                    "positionals": r'^[\w./\-_]+$'}},
}


def _build_safe_prefixes():
    """从 _CMD_ARG_RULES 动态生成白名单前缀列表，保持向后兼容。"""
    prefixes = []
    for binary, subcmds in _CMD_ARG_RULES.items():
        if isinstance(subcmds, str):
            continue  # alias，跳过
        for sub, rules in subcmds.items():
            if sub == "":
                prefixes.append(binary)
            else:
                prefixes.append(f"{binary} {sub}")
    return sorted(set(prefixes))


SAFE_VERIFICATION_PREFIXES = _build_safe_prefixes()

# shell 注入特征（精确模式，避免误伤合法的验证参数）
_SHELL_CHAIN = re.compile(r'[;&]|&&|\|\|')                    # 命令链: ; && ||
_SHELL_SUBST = re.compile(r'\$\(|`[^`]+`|\$\{')            # 命令替换: $() `` ${
_SHELL_PIPE_EXEC = re.compile(r'\b(curl|wget)\b.*\|.*\b(ba)?sh\b')  # curl|sh
_SHELL_DESTROY = re.compile(r'\brm\s+-r[^ ]*\s+[/~]')      # 危险 rm
_SHELL_OUTPUT_REDIR = re.compile(r'(?<![12])>>?\s*\S')      # 输出重定向（排除 2>&1, 1>&2）
_SHELL_INPUT_REDIR = re.compile(r'(?<!<\s)<\s*\S')          # 输入重定向

def _is_safe_verification_command(command: str) -> bool:
    """检查验证命令在 shell=True 降级前是否安全。

    四阶段验证:
      1. shlex 解析 — 无法解析则拒绝
      2. shell 注入扫描 — defense-in-depth，拦截命令链/替换/重定向等
      3. 命令 + 子命令查找 — 在 _CMD_ARG_RULES 中匹配
      4. 逐 token 校验 — 每个 flag/positional 必须匹配允许的正则

    返回 (is_safe, reason)，reason 在拒绝时为诊断信息。
    """
    cmd = command.strip()
    if not cmd:
        return False, "空命令"

    # Stage 1: shlex 解析
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        return False, f"shlex 解析失败: {e}"

    if not argv:
        return False, "空 argv"

    # Stage 2: shell 注入扫描（defense-in-depth）
    _injection_checks = [
        (_SHELL_CHAIN, "命令链"),
        (_SHELL_SUBST, "命令替换"),
        (_SHELL_PIPE_EXEC, "管道执行"),
        (_SHELL_DESTROY, "危险删除"),
        (_SHELL_OUTPUT_REDIR, "输出重定向"),
        (_SHELL_INPUT_REDIR, "输入重定向"),
    ]
    for pattern, name in _injection_checks:
        if pattern.search(cmd):
            return False, f"shell 注入特征: {name}"

    # Stage 3: 命令 + 子命令查找
    binary = argv[0]
    rules_entry = _CMD_ARG_RULES.get(binary)
    if rules_entry is None:
        return False, f"未知命令: {binary}"

    # 解析子命令，确定适用的规则集
    remaining = argv[1:]
    matched_rules = None

    # 处理顶层 alias（如 python → 其子规则为 {"-m pytest": "pytest"}）
    if isinstance(rules_entry, str):
        target = _CMD_ARG_RULES.get(rules_entry)
        if target is None:
            return False, f"别名目标不存在: {rules_entry}"
        rules_entry = target

    # 尝试匹配子命令（最长前缀优先）
    for sub in sorted(rules_entry.keys(), key=len, reverse=True):
        if not sub:
            continue
        sub_tokens = sub.split()
        if len(remaining) >= len(sub_tokens) and remaining[:len(sub_tokens)] == sub_tokens:
            rule_val = rules_entry[sub]
            # 子命令 alias（如 "-m pytest": "pytest"）
            if isinstance(rule_val, str):
                target_rules = rules_entry.get(rule_val)
                if target_rules is None:
                    # alias 指向其他顶层命令（如 "pytest"）
                    target_entry = _CMD_ARG_RULES.get(rule_val)
                    if target_entry is None or isinstance(target_entry, str):
                        return False, f"子命令别名目标不存在: {rule_val}"
                    target_rules = target_entry.get("")
                    if target_rules is None:
                        return False, f"子命令别名目标无默认规则: {rule_val}"
                matched_rules = target_rules
            else:
                matched_rules = rule_val
            remaining = remaining[len(sub_tokens):]
            break

    if matched_rules is None:
        # 回退到空子命令规则
        empty_val = rules_entry.get("")
        if empty_val is None:
            return False, f"无匹配子命令: {binary} {' '.join(remaining[:2])}"
        if isinstance(empty_val, str):
            # 空子命令也是 alias
            target_entry = _CMD_ARG_RULES.get(empty_val)
            if target_entry is not None and not isinstance(target_entry, str):
                matched_rules = target_entry.get("")
            else:
                matched_rules = rules_entry.get(empty_val, {})
        else:
            matched_rules = empty_val

    if matched_rules is None or isinstance(matched_rules, str):
        return False, f"无法解析规则: {binary}"

    # Stage 4: 逐 token 校验
    flag_re = re.compile(matched_rules.get("flags", r"^$"))
    pos_re = re.compile(matched_rules.get("positionals", r"^$"))

    positional_mode = False
    for i, token in enumerate(remaining):
        if token == "--":
            positional_mode = True
            continue
        if positional_mode or not token.startswith("-"):
            if not pos_re.match(token):
                return False, f"参数不允许: '{token}' (位置参数 #{i})"
        else:
            if not flag_re.match(token):
                return False, f"参数不允许: '{token}' (标志 #{i})"

    return True, ""


def _log_rejected_command(command, reason, logger, task_id="", sub_id=""):
    """记录被拒绝的验证命令到日志和审计文件。

    同时写入:
    - logger (WARNING 级别 + log_event 结构化事件)
    - ~/.agent_go/verification_audit.jsonl (持久化审计日志)
    """
    logger.warning(f"验证命令被拒绝: {command[:100]} — 原因: {reason}")
    try:
        from .config import log_event
        log_event(logger, "verification_rejected", {
            "command": command[:200], "reason": reason,
            "task_id": task_id, "sub_id": sub_id,
        })
    except ImportError:
        pass  # config 模块不可用时不阻塞
    # 持久化到审计文件
    try:
        audit_dir = Path.home() / ".agent_go"
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit_path = audit_dir / "verification_audit.jsonl"
        entry = {
            "timestamp": datetime.now().isoformat(),
            "command": command[:200], "reason": reason,
            "task_id": task_id, "sub_id": sub_id,
        }
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 审计写入失败不影响主流程


def _safe_append_to_file(filepath: Path, text: str, logger: logging.Logger, max_retries: int = 10) -> None:
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

def _slugify(text: str, max_len: int = 30) -> str:
    """将任务标题转为分支名适用的短标识。"""
    slug = re.sub(r'[^a-zA-Z0-9一-鿿]+', '-', text).strip('-')
    return slug[:max_len] if len(slug) > max_len else slug

def _detect_commit_prefix(title: str) -> str:
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

def _detect_commit_scope(title: str) -> str:
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

def _format_commit(title: str, issue_ref: str = "", sub_id: str = "", scope: str = "") -> str:
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

def _detect_tool_versions(logger: logging.Logger) -> dict[str, str]:
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
