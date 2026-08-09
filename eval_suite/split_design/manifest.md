# Split Design Benchmark — Task Manifest

针对「子任务编排 / 拆分设计」场景的跨 Agent 对比测试集。
同一任务以统一 prompt 分别交给 Claude Code 与 OpenCode，要求返回结构化拆分设计，
用于对照 agent_go 的拆分算法合理性（G5/G6/G7 判定）。

## 选择逻辑

覆盖 3 个拆分关键维度：
- 难度档位（easy / medium / hard）
- 文件作用域（单文件 / 多文件）
- 拆分期望（不拆 / 适度拆 / 必须拆）

| # | task_id | 难度 | 文件面 | 拆分期望 | 选择理由 |
|---|---------|------|--------|----------|----------|
| 1 | add-format-helper | easy | 单文件 (src/utils.py) | **不拆** | 5 行单函数改动，G6 应拦截过度分解 |
| 2 | fix-missing-default | easy | 单文件 (src/cli.py) | **不拆** | 防御性缺陷单点修复 |
| 3 | add-simple-caching | medium | 双文件 (utils.py + storage.py) | 1-2 步 | 装饰器+接入点，强耦合不拆 |
| 4 | implement-done-command | medium | 双文件 (cli.py + storage.py) | 适度拆 | 新命令+存储方法，可拆 2 步 |
| 5 | security-hardening-taskmgr | hard | 多文件 (cli/storage/models/tests) | **必须拆** | 5 个独立安全改动，跨 4 文件 |
| 6 | conditional-branching-datapipeline | hard | 多文件跨模块 | **必须拆** | pipeline + transform + tests 多模块 |

## 任务来源

复用 eval_suite/golden_tasks/ 与 eval_suite/phaseD_tasks/ 的标准任务定义
（已冻结 task_version，见 task_catalog.json）。拆分对比只评估「拆分设计」，
不执行实际代码变更（低成本，仅调用一次 plan 类 prompt）。

## 判定维度（对比报告）

1. 子任务数量 vs 期望
2. 文件作用域互斥性（对应 agent_go G7）
3. 小改动是否被过度拆分（对应 agent_go G6）
4. 依赖关系设计合理性（依赖是否反映文件/数据耦合）
5. rationale 质量（是否说明拆分理由）
6. 与 agent_go Planner 历史拆分结果对比
