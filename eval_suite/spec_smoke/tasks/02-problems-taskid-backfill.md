# Task Spec: problems.record 复发时更新空 task_id

task_type: bugfix

## 1. 目标（做什么）

REQ-001 修复 problems.record 的一个边界缺陷：当已有 Problem 的 task_id 为空（历史脏数据）而新失败携带了 task_id 时，复发路径不更新 task_id，导致该 Problem 永远无法追溯到首个来源任务。

## 2. 动机（为什么）

record 的复发分支只做 occurrence_count++ 和 last_seen_at 更新；task_id 只在「不同 task 复发」时追加进 summary。task_id 为空的历史记录（理论上来自旧数据或手动创建）会永久缺失来源，损害追溯性（谦逊层 H3 的价值之一）。

## 3. 范围（动哪里，不动哪里）

### 需要改动的文件/模块
- `agent_go/problems.py` — record() 复发分支补 task_id 回填
- `tests/test_problems.py` — 新增回归测试

### 明确不动的区域
- 三态状态机与复发重开逻辑 — 不改
- `agent_go/executor.py` — 录制接线不改

## 4. 约束

- 保持函数签名与返回结构不变（向后兼容）
- 纯 stdlib，无新依赖

## 5. 验收标准（怎么算做完）

- [ ] AC-001 空 task_id 复发时被回填：`python3 -m pytest tests/test_problems.py -q` 新增测试全绿
- [ ] AC-002 既有行为不回归：`python3 -m pytest tests/test_problems.py -q` 全文件通过

## 6. 参考资料

- `agent_go/problems.py` record() 复发分支
- docs/design/humility-layer-design.md §8.3（写入时召回 upsert）

## 7. 已知风险

- 极低：纯数据层边界修复，无状态机语义变化
