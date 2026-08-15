# Task Spec: agent_go problems CLI 命令（M5 收尾）

task_type: feat

## 1. 目标（做什么）

REQ-001 新增 `agent_go problems` CLI 命令，消费已实现的 problems.py 数据层：
- 无参数：列出全局 Problem（~/.agent_go/problems.jsonl），按 occurrence_count 降序展示
- `--aggregate`：输出聚合分析（total / 状态分布 / 复发数 / top 模式）
- `--json`：机器可读输出

## 2. 动机（为什么）

problems.py 数据层（B4/H3）已实现且 executor 已录制失败（#50），但用户无法查看——「越用越聪明」的界面入口缺失。这是 M5 问题跟踪的 CLI 收尾。

## 3. 范围（动哪里，不动哪里）

### 需要改动的文件/模块
- `agent_go/cli.py` — 新增 cmd_problems + parser
- `agent_go/__init__.py` — 如需导出（视需要）
- `tests/test_cli_commands.py` 或新测试文件 — 新增 CLI 测试
- `AGENTS.md` — 命令文档一行

### 明确不动的区域
- `agent_go/problems.py` — 数据层已冻结，不改
- `agent_go/executor.py` — 录制逻辑已实现，不改

## 4. 约束

- 纯 stdlib，复用 problems.load/aggregate
- 命令风格对齐现有 CLI（cmd_list 等）：console 输出 + --json 模式

## 5. 验收标准（怎么算做完）

- [ ] AC-001 problems 命令可列出 Problem：`python3 -m pytest tests/test_cli_commands.py -q` 新增测试全绿
- [ ] AC-002 聚合分析可输出：`python3 -c "from agent_go.cli import cmd_problems; print('import-ok')"` 正常退出

## 6. 参考资料

- `agent_go/problems.py`（数据层：load / aggregate / PROBLEM_STATES）
- docs/design/humility-layer-design.md §八（存储与召回机制）

## 7. 已知风险

- cli.py 文件较大（3800+ 行），新增命令需注意与既有 parser 结构对齐
- problems.jsonl 可能不存在（无失败记录）——命令需容错空文件
