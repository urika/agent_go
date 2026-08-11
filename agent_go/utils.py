import subprocess, json, re, time, shlex, logging, importlib
from pathlib import Path
from datetime import datetime
from typing import Any, Callable, Optional

__all__ = ["read_reference_docs", "SAFE_VERIFICATION_PREFIXES", "_safe_optional_call", "slugify_branch_name"]


def _safe_optional_call(
    module_name: str,
    func_name: str,
    logger: logging.Logger,
    *args: Any,
    fallback: Optional[Any] = None,
    label: Optional[str] = None,
    **kwargs: Any,
) -> Any:
    """可选增强调用 helper（解耦原则：动态 import + try/except 容错的统一封装）。

    用法：替代散落在 executor/pipeline 中的 5+ 处重复 try/except 模式：
        try:
            from .X import Y
            Y(arg1, arg2)
        except Exception as e:
            logger.warning(f"X.Y 失败: {e}")

    改为：
        _safe_optional_call(".X", "Y", logger, arg1, arg2, label="X.Y")

    Args:
        module_name: 子模块名（相对路径，如 ".evaluator" 或 ".notify"）
        func_name: 要调用的函数名
        logger: 用于记录警告
        *args, **kwargs: 透传给目标函数
        fallback: 调用失败时返回的默认值（默认 None）
        label: 日志中的可读标签，默认 f"{module_name}.{func_name}"

    Returns:
        目标函数的返回值；调用失败时返回 fallback。
    """
    tag = label or f"{module_name}.{func_name}"
    try:
        mod = importlib.import_module(module_name, __package__)
        func = getattr(mod, func_name)
        return func(*args, **kwargs)
    except Exception as e:
        logger.warning(f"可选增强 {tag} 加载/调用失败，跳过（不中断核心）: {e}")
        return fallback

def read_reference_docs(doc_paths: list[str], repo: Path, logger: logging.Logger) -> str:
    contents = []
    repo_root = repo.resolve()
    for path_str in doc_paths:
        path = (repo / path_str).resolve()
        # 防止路径穿越：确保路径在 repo 范围内（is_relative_to 按路径段比较，不会被兄弟前缀目录绕过）
        if not path.is_relative_to(repo_root):
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
             "positionals": r'^[\w./\-_:]+$',
             # 这些 flag 的下一个 token 是其取值（如 -k 'Q5 or query_count'），
             # 取值按 pytest 语义可为任意表达式，不按 positionals 正则校验。
             "value_flags": ["-k", "-m", "-p", "-n", "--maxfail", "--tb", "-w",
                             "--rootdir", "--override-ini", "--ignore", "--durations", "--cache-show"]},
    },
    "python": {"-m pytest": "pytest",
               "-m": {"flags": r'^(-[\w@./_=]+|--[\w@./_-]+(?:=\S+)?)$',
                      # 通用 -m <模块>：模块名+参数。禁止 .. 穿越、禁止 shell 注入符号。
                      # 项目内模块（如 src.cli stats）合法；-m pytest/-m unittest 等
                      # 标准工具也放行（模块名受 positionals 约束）。
                      "positionals": r'^(?!.*\.\.)[\w./\-_]+$'},
               "-c": {"flags": r'^(--help|-h)$', "positionals": r'^[\s\S]*$'},
               "manage.py": "manage.py",
               "": {"flags": r'^(--help|-h|-V|--version|-B|-O|-OO|-u|-W=\S+)$',
                    "positionals": r'^(?!.*\.\./)[\w./\-_]+$'}},
    "python3": {"-m pytest": "pytest",
                "-m": {"flags": r'^(-[\w@./_=]+|--[\w@./_-]+(?:=\S+)?)$',
                       "positionals": r'^(?!.*\.\.)[\w./\-_]+$'},
                "-c": {"flags": r'^(--help|-h)$', "positionals": r'^[\s\S]*$'},
                "manage.py": "manage.py",
                "": {"flags": r'^(--help|-h|-V|--version|-B|-O|-OO|-u|-W=\S+)$',
                     "positionals": r'^(?!.*\.\./)[\w./\-_]+$'}},
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
    "manage.py": {"": {"flags": r'^(--settings=\S+|--pythonpath=\S+|--no-color|--verbosity=\S+|--traceback|--dry-run|--check|--list|--merge|--name=\S+|--empty|--run-syncdb|--database=\S+|--noinput|--skip-checks|--force-color|--keepdb|--parallel|--failfast|--tag=\S+|--exclude-tag=\S+)$',
                       "positionals": r'^[\w.:\-_]+$'},
                  "shell": {"flags": r'^(-c)$',
                            "positionals": r'^[\s\S]*$'},
                  "migrate": {"flags": r'^(--plan|--database=\S+|--fake|--list)$',
                              "positionals": r'^[\w\-_]+$'},
                  "makemigrations": {"flags": r'^(--check|--dry-run|--name=\S+|--empty|--merge)$',
                                     "positionals": r'^[\w\-_]+$'},
                  "test": {"flags": r'^(--verbosity=\S+|--noinput|--failfast|--keepdb|--parallel|--tag=\S+|--exclude-tag=\S+)$',
                           "positionals": r'^[\w.\-_:]+$'}},
    "django-admin": {"": {"flags": r'^(--settings=\S+|--pythonpath=\S+|--no-color|--verbosity=\S+|--traceback|--dry-run|--check|--skip-checks)$',
                           "positionals": r'^[\w.:\-_]+$'},
                     "shell": {"flags": r'^(-c)$',
                               "positionals": r'^[\s\S]*$'},
                     "migrate": {"flags": r'^(--plan|--database=\S+|--fake|--list)$',
                                 "positionals": r'^[\w\-_]+$'},
                     "makemigrations": {"flags": r'^(--check|--dry-run|--name=\S+|--empty|--merge)$',
                                        "positionals": r'^[\w\-_]+$'},
                     "test": {"flags": r'^(--verbosity=\S+|--noinput|--failfast|--keepdb|--parallel|--tag=\S+|--exclude-tag=\S+)$',
                              "positionals": r'^[\w.\-_:]+$'}},
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
    "echo":   {"": {"flags": r'^$', "positionals": r'^[\s\S]*$'}},
    # 只读 POSIX 命令（仅 stdout，无副作用）— M0 smoke 分析发现 Planner 常用 ls/find 等做文件存在性验证
    "ls":     {"": {"flags": r'^(-[alhrtS1]+)$', "positionals": r'^[\w./\-_*?\[\]]+$'}},
    "find":   {"": {"flags": r'^(-name$|-type$|-maxdepth$|-mindepth$|-path$)$', "positionals": r'^[\w./\-_*]+$',
                     "value_flags": ["-name", "-type", "-maxdepth", "-mindepth", "-path"]}},
    "cat":    {"": {"flags": r'^(-[bnEs]+)$', "positionals": r'^[\w./\-_]+$'}},
    "head":   {"": {"flags": r'^(-n$|-c$)$', "positionals": r'^[\w./\-_]+$',
                    "value_flags": ["-n", "-c"]}},
    "wc":     {"": {"flags": r'^(-[lwcmL]+)$', "positionals": r'^[\w./\-_]+$'}},
    "test":   {"": {"flags": r'^(-[efdswrxzntLh]+)$', "positionals": r'^[\w./\-_]+$'}},
    "stat":   {"": {"flags": r'^(-[fLs]+|-c$|--format$)$', "positionals": r'^[\w./\-_]+$',
                    "value_flags": ["-c", "--format"]}},
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
_SHELL_CHAIN = re.compile(r'(?<![&])&(?![&])|;|\|\|')           # 命令链: 单 & ; ||（&& 安全，允许）
_SHELL_SUBST = re.compile(r'\$\(|`[^`]+`|\$\{')            # 命令替换: $() `` ${
_SHELL_PIPE_EXEC = re.compile(r'\b(curl|wget)\b.*\|.*\b(ba)?sh\b')  # curl|sh
_SHELL_DESTROY = re.compile(r'\brm\s+-r[^ ]*\s+[/~]')      # 危险 rm
_SHELL_OUTPUT_REDIR = re.compile(r'(?<![12])>>?\s*\S')      # 输出重定向（排除 2>&1, 1>&2）
_SHELL_INPUT_REDIR = re.compile(r'(?<!<\s)<\s*\S')          # 输入重定向


def _strip_quoted(cmd: str) -> str:
    """移除命令中引号内的内容，使 shell 注入扫描只作用于引号外的真实 shell 操作符。

    例：python -c "import x; assert y" → python -c（; 在引号内，安全）
    """
    return re.sub(r'"[^"]*"|\'[^\']*\'', '', cmd)

def _is_safe_verification_command(command: str) -> tuple[bool, str]:
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

    # 预处理：agent_go 已用 cwd=worktree 执行命令，剥离冗余的 cd <dir> && / cd <dir>; 前缀
    # LLM 常生成 "cd /path && pytest ..." 这类命令，cd 多余且 && 会被注入扫描拦截
    cd_prefix = re.compile(r'^cd\s+\S+\s*(&&|;|&)\s*')
    cmd = cd_prefix.sub('', cmd).strip()
    if not cmd:
        return False, "空命令（剥离 cd 前缀后）"

    # Stage 1: shlex 解析
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        return False, f"shlex 解析失败: {e}"

    if not argv:
        return False, "空 argv"

    # Stage 2: shell 注入扫描（defense-in-depth，仅扫描引号外的操作符）
    # 引号内的 ; & | 等是程序参数（如 python -c "import x; y()"），不是 shell 操作符
    cmd_unquoted = _strip_quoted(cmd)
    _injection_checks = [
        (_SHELL_CHAIN, "命令链"),
        (_SHELL_SUBST, "命令替换"),
        (_SHELL_PIPE_EXEC, "管道执行"),
        (_SHELL_DESTROY, "危险删除"),
        (_SHELL_OUTPUT_REDIR, "输出重定向"),
        (_SHELL_INPUT_REDIR, "输入重定向"),
    ]
    for pattern, name in _injection_checks:
        if pattern.search(cmd_unquoted):
            return False, f"shell 注入特征: {name}"

    # Stage 2.5: python -c 内容结构校验——单行 -c 不能含装饰器/with 块/换行。
    # planner 常生成这类命令（如 `python -c "from x import y; @dec\ndef f(): ..."`），
    # 运行必 SyntaxError，浪费整轮执行。引号内 ; 是合法单行分隔符，不拒绝。
    if argv[0] in ("python", "python3") and len(argv) >= 3 and argv[1] == "-c":
        _content = argv[2]
        # compile 预检：单行 -c 无法表达 try/except、if/else、def 等复合语句，
        # 直接编译能精确拦截这类 SyntaxError，避免运行期浪费整轮执行。
        try:
            compile(_content, "<cmd>", "exec")
        except SyntaxError as _e:
            return False, f"python -c 语法错误: {_e}"
        _bad = []
        if "\n" in _content:
            _bad.append("含换行")
        # 装饰器不单独检测：compile() 已能精确拦截单行 -c 中非法的装饰器
        # （@dec def f() 在单行中必 SyntaxError）。@\w+ 正则会误判 email 地址
        # 中的 @（如 validate_email('test@example.com')），导致合法验证命令被拒
        # （阶段 E email-validator 0/3 全 infrastructure_failure 根因）。
        if re.search(r"(^|;|\s)with\s+\S", _content):
            _bad.append("含 with 块")
        if _bad:
            return False, f"python -c 内容{'、'.join(_bad)}，无法作为单行执行"

    # 处理 && 链：拆分为多个命令分别校验
    # LLM 常生成 "cmd1 && cmd2" 格式的验证命令，每个子命令独立安全
    if "&&" in cmd:
        parts = [p.strip() for p in cmd.split("&&")]
        for part in parts:
            if not part:
                continue
            safe, reason = _is_safe_verification_command(part)
            if not safe:
                return False, f"&& 子命令不通过: {reason}"
        return True, ""

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

    # 尝试匹配子命令（最长前缀优先），外层 while 支持多层 alias 重定向
    # 例：python manage.py shell ... → python alias 到 manage.py → 再匹配 shell 子命令
    while True:
        sub_matched = False
        for sub in sorted(rules_entry.keys(), key=len, reverse=True):
            if not sub:
                continue
            sub_tokens = sub.split()
            if len(remaining) >= len(sub_tokens) and remaining[:len(sub_tokens)] == sub_tokens:
                sub_matched = True
                rule_val = rules_entry[sub]
                # 子命令 alias（如 "-m pytest": "pytest"；或自引用如 python manage.py → manage.py）
                if isinstance(rule_val, str):
                    target_rules = rules_entry.get(rule_val)
                    if target_rules is None or isinstance(target_rules, str):
                        # alias 指向其他顶层命令或自引用 → 推进 remaining 并切换 rules_entry 重新匹配
                        target_entry = _CMD_ARG_RULES.get(rule_val)
                        if target_entry is None:
                            return False, f"子命令别名目标不存在: {rule_val}"
                        if isinstance(target_entry, str):
                            return False, f"别名目标也是别名: {rule_val} -> {target_entry}"
                        remaining = remaining[len(sub_tokens):]
                        rules_entry = target_entry
                        break  # 退出内层 for，外层 while 用新 rules_entry 重新匹配
                    matched_rules = target_rules
                else:
                    matched_rules = rule_val
                remaining = remaining[len(sub_tokens):]
                break  # 找到具体规则，退出内层 for

        if not sub_matched:
            break  # 无子命令匹配，回退到默认规则
        if matched_rules is not None:
            break  # 找到具体规则，退出外层 while

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
    value_flags = set(matched_rules.get("value_flags", []))

    positional_mode = False
    skip_next = False
    for i, token in enumerate(remaining):
        if skip_next:
            skip_next = False
            continue
        if token == "--":
            positional_mode = True
            continue
        if positional_mode or not token.startswith("-"):
            if not pos_re.match(token):
                return False, f"参数不允许: '{token}' (位置参数 #{i})"
        else:
            if not flag_re.match(token):
                return False, f"参数不允许: '{token}' (标志 #{i})"
            if token in value_flags:
                skip_next = True

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

def slugify_branch_name(title: str, max_len: int = 40) -> str:
    """将标题转为合法 git 分支名。

    规则：转小写 → 空白/下划线替换为 - → 仅保留字母数字、点(.)、横杠(-) → 去除首尾横杠；
    结果为空返回 'unnamed'；超长按 max_len 截断。
    """
    slug = title.lower()
    slug = re.sub(r'[\s_]+', '-', slug)
    slug = re.sub(r'[^a-z0-9.\-]', '', slug)
    slug = slug.strip('-')
    if not slug:
        return 'unnamed'
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
