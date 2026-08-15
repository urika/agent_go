# Task Spec: extract_do_not_touch 标记变体测试

task_type: test

## 1. 目标（做什么）

REQ-001 为 `extract_do_not_touch` 的标记匹配（_DO_NOT_TOUCH_MARK）补充测试覆盖：当前测试只覆盖「明确不动的区域」一种写法，而正则支持「禁止修改」「不可修改」「不改动」等变体——补 3 个变体测试，若发现正则未命中变体则同步修复 spec.py。

## 2. 动机（为什么）

`_DO_NOT_TOUCH_MARK` 正则支持 5+ 种变体但测试只覆盖 1 种——谦逊层 do-not-touch 硬约束（fail-close）依赖该提取，标记变体未覆盖 = 硬约束可能静默失效。这是 spec 闭环 P2 的测试债。

## 3. 范围（动哪里，不动哪里）

### 需要改动的文件/模块
- `tests/test_spec.py` — TestExtractDoNotTouch 补 3 个变体用例
- `agent_go/spec.py` — 仅当变体测试暴露正则缺陷时修复 _DO_NOT_TOUCH_MARK

### 明确不动的区域
- extract_file_paths / parse_spec — 不改
- validate_spec_l1 六项检查 — 不改

## 4. 约束

- 测试风格对齐 TestExtractDoNotTouch 现有用例
- 不改变正则的匹配语义（除非变体确实该命中而未命中）

## 5. 验收标准（怎么算做完）

- [ ] AC-001 三个变体测试全绿：`python3 -m pytest tests/test_spec.py::TestExtractDoNotTouch -q` 通过
- [ ] AC-002 spec 全文件不回归：`python3 -m pytest tests/test_spec.py -q` 通过

## 6. 参考资料

- `agent_go/spec.py` extract_do_not_touch + _DO_NOT_TOUCH_MARK
- docs/design/humility-layer-design.md §二 H2 前置（do-not-touch 硬约束）

## 7. 已知风险

- 极低：纯测试补充
