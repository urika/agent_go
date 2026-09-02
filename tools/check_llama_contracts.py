#!/usr/bin/env python3
"""check_llama_contracts.py — 双端契约漂移检测（AG-1）。

比对 agent_go/llama_contracts.py（vendored）与 llama-defender 仓库的
signal_types.py / protocol_types.py，检测：
1. CONTRACT_VERSION 是否一致（删/改字段上游应 +1，本端需同步跟进）；
2. 共享工厂函数（build_signal_snapshot / build_escalation）签名是否漂移。

用法：
    python3 tools/check_llama_contracts.py [--repo /path/to/llama-defender]

仓库路径解析顺序：--repo 参数 > $LLAMA_DEFENDER_REPO > 常见相邻路径猜测。
找不到对方仓库时打印 SKIP 并退出 0（CI 无对端仓库不阻断；本地开发应配置路径）。
契约漂移时退出 1。
"""
import argparse
import ast
import os
import sys
from pathlib import Path
from typing import List, Optional

AGENT_GO_ROOT = Path(__file__).resolve().parent.parent
VENDORED = AGENT_GO_ROOT / "agent_go" / "llama_contracts.py"

# (上游文件, 期望存在的工厂函数)
UPSTREAM_FILES = {
    "signal_types.py": ["build_signal_snapshot"],
    "protocol_types.py": ["build_escalation"],
}
VENDORED_FACTORIES = ["build_signal_snapshot", "build_escalation"]


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _contract_version(tree: ast.Module) -> Optional[int]:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "CONTRACT_VERSION":
                    if isinstance(node.value, ast.Constant):
                        return int(node.value.value)
    return None


def _factory_params(tree: ast.Module, name: str) -> Optional[List[str]]:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return [a.arg for a in node.args.args]
    return None


def _guess_repo() -> Optional[Path]:
    candidates = [
        AGENT_GO_ROOT.parent / "llama.cpp",
        AGENT_GO_ROOT.parent.parent / "APP" / "llama.cpp",
        Path.home() / "APP" / "llama.cpp",
    ]
    for c in candidates:
        if (c / "signal_types.py").is_file():
            return c
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="llama-defender 共享契约漂移检测")
    ap.add_argument("--repo", default=os.environ.get("LLAMA_DEFENDER_REPO", ""),
                    help="llama-defender 仓库路径")
    args = ap.parse_args()

    repo = Path(args.repo) if args.repo else _guess_repo()
    if repo is None or not (repo / "signal_types.py").is_file():
        print("SKIP: 未找到 llama-defender 仓库（--repo 或 LLAMA_DEFENDER_REPO 指定）")
        return 0

    errors: list[str] = []

    vendored_tree = _parse(VENDORED)
    vendored_version = _contract_version(vendored_tree)

    # 1. CONTRACT_VERSION 一致性
    for fname in UPSTREAM_FILES:
        upstream_version = _contract_version(_parse(repo / fname))
        if upstream_version != vendored_version:
            errors.append(
                f"CONTRACT_VERSION 漂移: {fname}={upstream_version} "
                f"vs llama_contracts.py={vendored_version}")

    # 2. 工厂函数签名一致性（上游权威 → vendored 应相同）
    for fname, factories in UPSTREAM_FILES.items():
        upstream_tree = _parse(repo / fname)
        for fn in factories:
            up = _factory_params(upstream_tree, fn)
            vend = _factory_params(vendored_tree, fn)
            if up is None:
                errors.append(f"{fname}: 上游缺少 {fn}()")
            elif vend != up:
                errors.append(
                    f"{fn} 签名漂移: 上游{up} vs vendored{vend}")

    # 3. vendored 侧完整性
    for fn in VENDORED_FACTORIES:
        if _factory_params(vendored_tree, fn) is None:
            errors.append(f"llama_contracts.py: 缺少 {fn}()")

    if errors:
        print("契约漂移检测到不一致：")
        for e in errors:
            print(f"  ✗ {e}")
        print("请同步 agent_go/llama_contracts.py 并更新 vendored_at 日期。")
        return 1
    print(f"OK: 契约一致（repo={repo}, CONTRACT_VERSION={vendored_version}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
