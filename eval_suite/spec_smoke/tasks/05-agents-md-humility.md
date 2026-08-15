# Task Spec: AGENTS.md 谦逊层说明补全

task_type: docs

## 1. 目标（做什么）

REQ-001 在 AGENTS.md 补充谦逊层已落地的能力说明：
- `agent_go problems` 相关（M5 数据层已建，CLI 见任务 01）
- 交付报告/审查输出中的「已知盲区」「层间归因」「失败历史关联」段落
- meta.json 新增字段：blind_spots / uncovered_perspectives / layer_attribution / traceability / problem_id
- 设计文档索引：humility-layer-design.md

## 2. 动机（为什么）

AGENTS.md 是 AI coding agent 的操作手册，谦逊层字段/展示已落地但未记录——未来 agent 处理 meta.json / review 输出时不知道这些字段的含义和用途。

## 3. 范围（动哪里，不动哪里）

### 需要改动的文件/模块
- `AGENTS.md` — 命令区 + 关键模块表 + 关键设计决策区补谦逊层条目

### 明确不动的区域
- docs/prd.md / docs/roadmap.md — 已在其他任务更新
- 任何 agent_go/*.py — 纯文档任务

## 4. 约束

- 只写已验证的事实（字段名/命令名以代码为准）
- 风格对齐 AGENTS.md 现有条目（简短、可操作）

## 5. 验收标准（怎么算做完）

- [ ] AC-001 AGENTS.md 含谦逊层字段说明：`python3 -c "assert 'blind_spots' in open('AGENTS.md', encoding='utf-8').read() and 'humility-layer-design' in open('AGENTS.md', encoding='utf-8').read(); print('OK')"` 正常输出 OK
- [ ] AC-002 不破坏现有测试：`python3 -m pytest tests/test_spec.py -q` 通过（文档任务无代码变更）

## 6. 参考资料

- docs/design/humility-layer-design.md（全文）
- agent_go/pipeline.py _build_humility_signals（字段定义）

## 7. 已知风险

- 无：纯文档
