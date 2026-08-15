# Task Spec: attribute_layer 补 delivery_failure 归因

task_type: feat

## 1. 目标（做什么）

REQ-001 为 H2 层间归因补齐 delivery_failure 的归因：`attribute_layer()` 对 failure_class == "delivery_failure" 返回 "contract_broken"（协议层——交付协议断裂：PR 创建失败/merge 冲突/推送失败）。当前该 class 返回 None（未归因），层间归因存在盲区。

## 2. 动机（为什么）

delivery_failure 是 8 类稳定失败之一，但没有层间归因——「交付失败」定位不到「协议层」会让复盘时错过「修交付协议」这个动作。补齐后 8 类失败中 7 类可归因。

## 3. 范围（动哪里，不动哪里）

### 需要改动的文件/模块
- `agent_go/failure.py` — attribute_layer() 补 delivery_failure 分支
- `tests/test_failure.py` — 新增/更新归因测试

### 明确不动的区域
- classify_failure / aggregate_failure_class — 不改
- LAYER_PRIORITY 排序 — 不改（contract_broken 已有）

## 4. 约束

- 保持纯函数、确定性、零 LLM
- 不改变现有 5 归因的行为

## 5. 验收标准（怎么算做完）

- [ ] AC-001 delivery_failure 归因为 contract_broken：`python3 -m pytest tests/test_failure.py::test_no_attribution_for_unclassified -q` 更新后通过
- [ ] AC-002 既有归因不回归：`python3 -m pytest tests/test_failure.py -q` 全文件通过

## 6. 参考资料

- `agent_go/failure.py` attribute_layer（H2 实现）
- docs/design/humility-layer-design.md §二 H2（层间归因五归因 → 六归因）

## 7. 已知风险

- 低：纯函数扩展 + 测试更新
