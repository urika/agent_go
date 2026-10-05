# ADR-015 Problem ↔ GitHub Issue 联动（默认关的对外写入面）

> 状态：**Accepted**（2026-10-05）
> 依据：`docs/roadmap.md` §7.6 M5 后续「Issue 联动」（A6 决策：`--track-issues` 显式开启，默认关，避免 issue 洪水）
> 相关：[ADR-003 DAG/Artifact](ADR-003-dag-artifact-transfer.md)、`docs/design/humility-layer-design.md`（H3 Problem 实体）

## 背景

M5 已把跨任务失败沉淀为一等公民 `Problem`（`~/.agent_go/problems.jsonl`，三态 + 复发重开 + 半衰期 + 葬礼），
`Problem` 数据类自始保留 `github_issue` 字段但**没有任何写入路径**——即"链路预留、联动未实现"。
roadmap 长期把它列为 `deferred`，理由是 A6 决策：默认开启会造成 issue 洪水。

需要明确的是：**创建/评论/关闭 GitHub issue 是对外写入（外发）**，与 ADR-013 的 jev 外发边界同类——
必须有显式人闸门、可审计、可回滚，且不得把本地原始失败输出（可能含路径）直接外发。

## 决策

1. **默认关**：`issues.enabled=false`。开闸只有两条路径——`agent_go run --track-issues`（单次运行的
   人闸门）或 `agent_go issues sync --yes`（显式回填命令）。未开闸时任何 `gh` 调用都不发生。
2. **单向数据流**：Problem 状态 → issue（create / comment / close）；**不从 issue 反向改 Problem 状态**
   （避免远端成为状态机真源）。状态机仍在本地 `problems.jsonl`。
3. **幂等**：`Problem.issue_synced` 记录上次同步的 `{occurrence_count, status, number, at}`；只有三种漂移
   触发动作——无 `github_issue`（create）、`occurrence_count` 增加（comment，"复发"）、`status=resolved`
   且未同步过（close）。重复运行不产生重复 issue/评论。
4. **不外发本地证据**：issue 正文默认不含 `Problem.evidence`（`issues.include_evidence=true` 或
   `--include-evidence` 才带）；正文/标题对家目录与 `.agent_go` 路径脱敏（`<home>` / `<agent_go-dir>`）。
5. **fail-open**：`gh` 缺失/未登录/网络失败/超时/label 不存在 → 记警告，**绝不阻断主链路**；失败项不写
   `issue_synced`，下次同步自然重试。label 不存在时去掉 `--label` 重试一次（标签缺失不漏报）。
6. **数据契约扩展**：`Problem` 增 `issue_synced` 字段（JSONL 只增字段，旧行缺键按 `{}` 读取 → 兼容）。
7. **外发时机**：`run --track-issues` 在流水线结束后只同步**本次任务触达**的 Problem
   （`task_id` 过滤，避免一次运行扫描/外发全量历史）；全量回填由人工 `issues sync --yes` 控制
   （默认 dry-run 预览，`--limit` 限流）。

## 后果

- **正面**：跨任务失败第一次有了"仓库外可见的载体"（团队/看板可直接跟进）；本地状态机与远端 issue 一一对应、
  幂等、可审计（每次动作留 `issue_synced` 时间戳）。
- **代价**：新增一处对外写入面与一个 `gh` 外部依赖（缺失时仅降级）；`Problem` schema 增一字段。
- **非目标**：不做双向同步；不做 issue 模板/项目字段管理；不自动归档/关闭他人 issue；不在 bench/eval 路径触发
  （避免评测流量制造 issue）。

## 引用

- 实现：`agent_go/issue_link.py`（`sync_problems` / `sync_problem` / `needs_sync`）；CLI `agent_go issues sync`
  / `agent_go run --track-issues`；配置 `issues.{enabled,include_evidence}`
- 数据层：`agent_go/problems.py`（`github_issue` / `issue_synced`）
- 测试：`tests/test_issue_link.py`（10 例，全部 mock `gh`，零外发）
