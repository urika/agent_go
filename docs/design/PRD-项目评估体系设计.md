# PRD: agent_go 项目评估体系设计

> 版本: v2.0
> 日期: 2026-05-26
> 作者: Product
> 状态: Draft

---

## 一、架构分层

评估体系分为三层，严格分离数据采集与统计分析：

```
┌─────────────────────────────────────────────────────────┐
│                    统计分析层 (eval.py)                   │
│  agent_go eval quality/cost/perf/reliability/ux/compare  │
│  读取历史数据 → 计算指标 → 聚合展示                        │
└──────────────────────────┬──────────────────────────────┘
                           │ 读取
┌──────────────────────────┴──────────────────────────────┐
│                    数据存储层                             │
│  result.json (per subtask)  ·  execution.log             │
│  meta.json (per task)       ·  session_stats.json        │
└──────────────────────────┬──────────────────────────────┘
                           │ 写入
┌──────────────────────────┴──────────────────────────────┐
│                    数据采集层 (metrics.py)                │
│  执行过程中采集: timing · change_stats · merge_results    │
│  API 响应中提取: usage · status_code                     │
└─────────────────────────────────────────────────────────┘
```

**核心原则**:
- 采集层只在执行过程中**记录事实**，不做任何计算
- 存储层**结构化持久化**事实数据，不做聚合
- 分析层**读取+计算**指标，不依赖采集层的实时状态

---

## 二、数据采集层（metrics.py）

### 2.1 设计原则

- 所有采集函数无副作用：只接收数据、返回字典，不写文件
- 采集逻辑嵌入 executor.py / api.py 的现有流程中
- 采集结果最终写入 result.json（per subtask）或 log_event（per session）

### 2.2 采集项清单

#### A 类：立即采集（7 项）

| # | 采集项 | 采集函数 | 插入点 | 存储位置 |
|---|--------|---------|--------|---------|
| A1 | `timing` 各阶段 ms | `collect_timing()` | executor.py 各 subprocess.run 前后 | result.json |
| A2 | `retry_count` | 内联计数 | executor.py 验证重试循环后 | result.json |
| A3 | `change_stats` 结构化 | `collect_change_stats()` | executor.py git diff/status 后 | result.json |
| A4 | API `usage` token | `extract_usage()` | api.py call_api response 解析 | plan 阶段 log_event |
| A5 | `merge_results` | `collect_merge_result()` | executor.py merge 循环内 | result.json |
| A6 | `plan_duration_ms` | 内联计时 | api.py generate_plan 前后 | plan_complete event |
| A7 | HTTP `status_code` | 内联捕获 | api.py call_api 异常处理 | api_error event |

#### B 类：择机采集（2 项）

| # | 采集项 | 触条件 |
|---|--------|--------|
| B1 | `session_stats` 累计 | Plan 缓存实现后 |
| B2 | `wave_timings` | TUI 实现后需要 |

#### C 类：暂缓（4 项）

cache_hit · claude_thinking_ms · tool_calls 持久化 · plan_accuracy 对比

### 2.3 采集函数接口

```python
# agent_go/metrics.py

def collect_timing(worktree_create_ms, merge_upstream_ms, claude_execute_ms,
                   verification_ms, git_commit_ms) -> dict:
    """采集 subtask 各阶段耗时。返回 timing 字典。"""
    return {
        "worktree_create_ms": round(worktree_create_ms),
        "merge_upstream_ms": round(merge_upstream_ms),
        "claude_execute_ms": round(claude_execute_ms),
        "verification_ms": round(verification_ms),
        "git_commit_ms": round(git_commit_ms),
    }


def collect_change_stats(worktree_path) -> dict:
    """采集变更统计。执行 git diff --numstat + git status --porcelain。
    返回 change_stats 字典。"""
    # 内部执行 2 次 git 命令，返回结构化结果
    pass


def collect_merge_result(upstream_id, success, conflict_files=None) -> dict:
    """采集单次 merge 结果。返回 merge_result 字典。"""
    result = {"upstream": upstream_id, "status": "success" if success else "conflict"}
    if conflict_files:
        result["conflict_files"] = conflict_files
    return result


def extract_usage(api_response: dict, provider: str, model: str) -> dict:
    """从 API response 提取 token 用量。返回 usage 字典。"""
    usage = api_response.get("usage", {})
    return {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "model": model,
        "provider": provider,
    }
```

---

## 三、数据存储层

### 3.1 result.json 扩展（per subtask）

```json
{
  "subtask_id": "sub-1",
  "status": "completed",
  "duration_sec": 95.3,
  "retry_count": 0,

  "timing": {
    "worktree_create_ms": 320,
    "merge_upstream_ms": 150,
    "claude_execute_ms": 93000,
    "verification_ms": 1500,
    "git_commit_ms": 200
  },

  "change_stats": {
    "files_changed": 3,
    "insertions": 45,
    "deletions": 2,
    "new_files": 1,
    "modified_files": 2,
    "actual_files": ["agent_go/cli.py", "agent_go/__init__.py", "agent_go/version.py"]
  },

  "merge_results": [
    {"upstream": "sub-0", "status": "success"},
    {"upstream": "sub-0", "status": "conflict", "conflict_files": ["main.py"]}
  ]
}
```

**写入位置**: `executor.py run_subtask()` return 前。

### 3.2 api_call event 扩展

```json
{
  "event": "api_call",
  "provider": "anthropic",
  "model": "claude-sonnet-4-20250514",
  "latency_ms": 3150,
  "prompt_tokens": 12000,
  "completion_tokens": 2400,
  "status_code": 200
}
```

**写入位置**: `api.py call_api()` 中 `log_event` 调用。

### 3.3 api_error event（新增）

```json
{
  "event": "api_error",
  "provider": "anthropic",
  "status_code": 429,
  "error_message": "rate_limit_exceeded"
}
```

### 3.4 plan_complete event 扩展

```json
{
  "event": "plan_complete",
  "iteration": 1,
  "step_count": 3,
  "plan_duration_ms": 3200,
  "cache_hit": false
}
```

### 3.5 session_stats.json（择机，B1）

```json
{
  "session_id": "20260526",
  "started_at": "2026-05-26T00:00:00",
  "api_usage": {
    "total_calls": 12,
    "total_prompt_tokens": 45000,
    "total_completion_tokens": 8200,
    "errors": {"4xx": 1, "5xx": 0}
  },
  "reliability": {
    "tasks_total": 5, "completed": 4, "failed": 1,
    "interrupted": 1, "resumed": 1,
    "retries": {"total": 3, "successful": 2}
  }
}
```

---

## 四、统计分析层（eval.py）

### 4.1 设计原则

- 所有分析函数**纯计算**：输入历史数据，输出指标值
- 不依赖采集层的实时状态，不写文件
- 支持单任务分析和全量聚合两种模式

### 4.2 分析函数接口

```python
# agent_go/eval.py

def analyze_quality(task_dir: Path) -> dict:
    """分析单个任务的执行质量。
    读取 meta.json + 所有 result.json，计算 Q1-Q8。"""
    pass

def analyze_performance(task_dir: Path) -> dict:
    """分析单个任务的性能。
    读取 meta.json + 所有 result.json，计算 P1-P8。"""
    pass

def analyze_cost(log_dir: Path) -> dict:
    """分析成本效率。
    扫描所有 execution.log 中的 api_call/api_error events，计算 C1-C7。"""
    pass

def analyze_reliability(log_dir: Path) -> dict:
    """分析可靠性。
    扫描 meta.json + session_stats（如有），计算 R1-R8。"""
    pass

def analyze_ux(tasks_dir: Path) -> dict:
    """分析用户体验。
    扫描所有 meta.json，聚合参数使用统计，计算 U1-U7。"""
    pass

def aggregate_quality(all_tasks_dir: Path) -> dict:
    """全量聚合：所有历史任务的质量指标。"""
    pass

def compare_versions(v1_tasks, v2_tasks) -> dict:
    """版本对比：两个数据集各维度指标对比。"""
    pass
```

### 4.3 指标计算映射

#### 执行质量（analyze_quality）

```
Q1 任务成功率    = count(status=="completed") / count(all)
Q2 Subtask成功率  = count(result.status=="completed") / count(all_results)
Q3 首次通过率     = count(result.retry_count==0) / count(all_results)
Q4 验证通过率     = count(result.verify_ok) / count(result.status!="no_changes")
Q5 新文件遗漏率   = count(no_changes & change_stats.new_files>0) / count(all_results)
Q6 产物传递成功率  = count(merge.status=="success") / count(all_merges)
Q7 计划准确性     = precision/recall (需 plan.steps[].files vs change_stats.actual_files)
Q8 变更规模       = avg(change_stats.files_changed), avg(change_stats.insertions+deletions)
```

**数据依赖**: Q1-Q2/Q4/Q8 已有数据 | Q3 需 retry_count | Q5-Q7 需 change_stats/merge_results

#### 性能（analyze_performance）

```
P1 端到端耗时     = pipeline最后事件时间 - meta.created
P2 Plan耗时       = plan_duration_ms (from plan_complete event)
P3 平均耗时       = avg(result.duration_sec)
P4 耗时分布       = percentile(result.duration_sec, [50,95,99])
P5 阶段占比       = avg(timing各项) / avg(timing总和)
P6 并发效率       = sum(duration_sec) / wall_time
P7 Claude思考     = timing.claude_thinking_ms (暂缓)
P8 等待时间       = wave(N).start - wave(N-1).end (择机)
```

**数据依赖**: P3/P4/P6 已有 | P1/P2 需 plan_duration_ms | P5 需 timing

#### 成本（analyze_cost）

```
C1 API调用次数    = count(api_call events)
C2 Token消耗      = sum(prompt_tokens) + sum(completion_tokens)
C3 预估费用       = prompt_tokens/1M * prompt_price + completion_tokens/1M * completion_price
C4 缓存命中率     = count(cache_hit==true) / count(plan_complete events)
C5 缓存节省       = cache_hits * avg(cost_per_generation)
C6 每任务成本     = total_cost / task_count
C7 每Subtask成本  = total_cost / subtask_count
```

**数据依赖**: 全部需 A4(usage) + B1(cache_hit)

#### 可靠性（analyze_reliability）

```
R1 中断恢复率     = count(resumed) / count(interrupted)
R2 降级率         = count(fallback) / count(all_plans)
R3 API错误率      = count(api_error) / count(api_call)
R4 僵尸率         = count(zombie) / count(running)
R5 Worktree泄漏   = count(残留worktree) (需git worktree list)
R6 Tag泄漏        = count(残留tag) (需git tag -l)
R7 Greywall使用率 = count(sandbox=="greywall") / count(all)
R8 重试率         = count(retry_count>0) / count(all_results)
```

**数据依赖**: R2/R4/R7/R8 已有 | R1 需 interrupted/resumed 标记 | R3 需 status_code

#### 用户体验（analyze_ux）

```
U1 Plan迭代       = avg(plan_generate.iteration)
U2 默认同意使用    = count(--yes) / count(all)
U3 人工编辑率      = count(user_plan_choice in (E,S,D)) / count(all_plans)
U4 文档挂载率      = count(reference_docs非空) / count(all)
U5 Skill使用率     = count(skills非空) / count(all_subtasks)
U6 Agent多样性    = count(agent_type!="developer") / count(all_subtasks)
U7 参数分布        = count各参数 / count(all)
```

**数据依赖**: U1/U4/U5/U6 已有 | U2/U3/U7 可从 meta + log event 提取

### 4.4 CLI 命令

```bash
agent_go eval quality [task-id]        # 单任务质量报告
agent_go eval quality --all            # 全量聚合

agent_go eval perf [task-id]           # 单任务性能分析
agent_go eval perf --all               # 全量耗时分布

agent_go eval cost                     # 成本统计（需 token 数据积累）
agent_go eval reliability              # 健康检查
agent_go eval ux                       # 使用习惯
agent_go eval compare <v1> <v2>        # 版本对比

agent_go eval all                      # 综合报告（所有维度）
```

### 4.5 评分算法

每个维度 0-100 分，综合评分加权平均：

| 维度 | 权重 | 关键指标 |
|------|------|---------|
| 质量 | 30% | Q1(40%) + Q3(30%) + Q4(30%) |
| 性能 | 25% | P1(30%) + P4(30%) + P6(40%) |
| 成本 | 15% | C3(50%) + C6(50%) |
| 可靠性 | 20% | R1(30%) + R3(30%) + R8(40%) |
| UX | 10% | U1(30%) + U5(40%) + U6(30%) |

---

## 五、指标与维度设计（存储层明细）

### 5.1 事实-维度-度量模型

系统产生 5 类事实记录。每类记录包含维度（分组/过滤依据）、度量（可聚合数值）、元数据（drill-down 上下文）。

### 5.2 事实记录 1：plan_generation（计划生成）

**粒度**: 每次 `generate_plan()` 调用产生 1 条。同一 task 可能多条（迭代）。

**存储**: plan_complete log event（结构化 JSON）

| 字段 | 类型 | 分类 | 说明 |
|------|------|------|------|
| `task_id` | string | 维度 | 关联任务 |
| `iteration` | int | 维度 | 第几次生成（1=首次, >1=迭代） |
| `provider` | string | 维度 | anthropic / openai / deepseek |
| `model` | string | 维度 | claude-sonnet-4-20250514 |
| `cache_hit` | bool | 维度 | 是否命中缓存 |
| `plan_duration_ms` | int | 度量 | Plan 阶段耗时 |
| `prompt_tokens` | int | 度量 | 输入 token |
| `completion_tokens` | int | 度量 | 输出 token |
| `step_count` | int | 度量 | 生成的步骤数 |
| `has_supplement` | bool | 维度 | 是否有用户补充 |
| `has_docs` | bool | 维度 | 是否有参考文档 |
| `has_skills` | bool | 维度 | 是否有 Skill 上下文 |

**支持指标**: P2, C1-C3, U1, R2

**Drill-down 示例**: "cost by provider → 谁最贵?" | "duration by iteration → 补充输入会让plan变慢吗?"

---

### 5.3 事实记录 2：subtask_execution（子任务执行）

**粒度**: 每个 subtask 产生 1 条。含 retry 计数但不含单次 retry 细节。

**存储**: result.json（per subtask）

| 字段 | 类型 | 分类 | 说明 |
|------|------|------|------|
| `subtask_id` | string | 维度 | sub-1, sub-2... |
| `task_id` | string | 维度 | 关联任务 |
| `status` | string | 维度 | completed / no_changes / failed / degraded |
| `agent_type` | string | 维度 | developer / architect / reviewer / tester |
| `agent_type_source` | string | 维度 | llm / rule / default |
| `sandbox_type` | string | 维度 | headless / greywall / native |
| `skills` | string[] | 维度 | 已加载的 Skill 名称列表 |
| `skills_unresolved` | string[] | 元数据 | 未找到的 Skill |
| `retry_count` | int | 度量 | 验证失败重试次数 |
| `duration_sec` | float | 度量 | 总耗时 |
| `verify_ok` | bool | 维度 | 验证是否通过 |

**timing 子对象**（度量组）:

| 字段 | 类型 | 度量粒度 |
|------|------|---------|
| `worktree_create_ms` | int | git worktree add 耗时 |
| `merge_upstream_ms` | int | 所有上游 merge 总耗时 |
| `claude_execute_ms` | int | Claude 进程存活时间 |
| `verification_ms` | int | 所有验证命令总耗时 |
| `git_commit_ms` | int | git add + commit + tag 耗时 |

**change_stats 子对象**（度量组）:

| 字段 | 类型 | 度量粒度 |
|------|------|---------|
| `files_changed` | int | 变更文件总数 |
| `insertions` | int | 新增行数 |
| `deletions` | int | 删除行数 |
| `new_files` | int | 新建文件数（untracked → staged） |
| `modified_files` | int | 修改文件数（tracked + modified） |
| `actual_files` | string[] | 实际被修改的文件路径列表（元数据） |

**merge_results 子对象**（数组，每项为）:

| 字段 | 类型 | 分类 |
|------|------|------|
| `upstream` | string | 上游 subtask_id（维度） |
| `status` | string | success / conflict（维度） |
| `conflict_files` | string[] | 冲突文件列表（元数据） |

**支持指标**: Q1-Q8, P3-P7, R8, U5, U6

**Drill-down 示例**:
- "P5 阶段占比 by agent_type → tester 的 verify 占比是否更高?" → `agent_type` × `timing.verification_ms`
- "Q5 新文件遗漏 by status → no_changes 的 new_files 分布?" → `status` × `change_stats.new_files`
- "Q3 重试率 by skills → 有 tdd-workflow 的 subtask 重试更少?" → `skills` × `retry_count`

---

### 5.4 事实记录 3：api_call（API 调用）

**粒度**: 每次 LLM API 调用产生 1 条。包括 Plan 生成和降级拆解。

**存储**: api_call / api_error log event（结构化 JSON）

| 字段 | 类型 | 分类 | 说明 |
|------|------|------|------|
| `task_id` | string | 维度 | 关联任务 |
| `call_type` | string | 维度 | plan_generate / decompose_fallback |
| `provider` | string | 维度 | anthropic / openai / deepseek |
| `model` | string | 维度 | 模型名 |
| `status_code` | int | 维度 | 200 / 429 / 500... |
| `latency_ms` | int | 度量 | 响应耗时 |
| `prompt_tokens` | int | 度量 | 输入 token |
| `completion_tokens` | int | 度量 | 输出 token |
| `response_len` | int | 度量 | 响应体长度(bytes) |
| `error_message` | string | 元数据 | 仅 status_code != 200 |

**支持指标**: C1-C3, R3

**Drill-down 示例**:
- "R3 API 错误 by hour → 什么时候限流?" → `status_code` × 时间
- "C1 日均调用 by provider → 增长趋势?" → `provider` × 时间

---

### 5.5 事实记录 4：verification_run（验证执行）

**粒度**: 每个验证命令产生 1 条。一个 subtask 可能多条（数组验证）。

**存储**: result.json 的 verification_results 子数组

| 字段 | 类型 | 分类 | 说明 |
|------|------|------|------|
| `command` | string | 元数据 | 完整命令文本（截断至 200 字符） |
| `exit_code` | int | 度量 | 0=成功 |
| `duration_ms` | int | 度量 | 命令执行耗时 |
| `stderr_tail` | string | 元数据 | 失败时 stderr 最后 200 字符 |
| `attempt` | int | 维度 | 1=首次, 2=重试 |

**支持指标**: Q4（验证通过率）, P5（verification_ms 占比）

**Drill-down 示例**:
- "哪些验证命令最慢?" → `command` × `duration_ms`
- "首次失败 vs 重试成功的分布?" → `attempt` × `exit_code`

---

### 5.6 事实记录 5：task_session（任务会话，择机 B1）

**粒度**: 每个 task 产生 1 条摘要记录。

**存储**: meta.json 扩展

| 字段 | 类型 | 分类 | 说明 |
|------|------|------|------|
| `task_id` | string | 维度 | — |
| `repo` | string | 维度 | 项目路径 |
| `task` | string | 元数据 | 任务描述 |
| `status` | string | 维度 | completed / failed / paused |
| `created` | string | 维度 | 创建时间 (YYYYMMDD-HHMMSS) |
| `parallel` | int | 维度 | 并发 worker 数 |
| `headless` | bool | 维度 | 是否 headless 模式 |
| `remote_url` | string | 维度 | 远程推送地址 |
| `issue` | string | 维度 | GitHub issue |
| `reference_docs` | string[] | 维度 | 参考文档列表 |
| `total_subtasks` | int | 度量 | — |
| `completed_subtasks` | int | 度量 | — |
| `failed_subtasks` | int | 度量 | — |
| `total_duration_sec` | float | 度量 | 端到端耗时 |
| `interrupted` | bool | 维度 | 是否被中断过 |
| `resumed` | bool | 维度 | 是否恢复执行 |

**支持指标**: P1, U2-U4, U7, R1

---

### 5.7 维度和度量的交叉分析矩阵

纵向=维度（分组/过滤），横向=度量（聚合值），交叉点=可回答的问题：

```
                 token    duration  files    retry   verify  cost
                 ─────    ────────  ─────    ─────   ──────  ────
agent_type        ✅        ✅        ✅       ✅      ✅      ✅
skills            -         ✅        ✅       ✅      ✅      -
provider          ✅        ✅        -        -       -       ✅
sandbox_type      -         ✅        -        ✅      -       -
status            -         -         ✅       -       -       -
agent_type_source -         -         -        ✅      ✅      -
task_id           ✅        ✅        ✅       ✅      ✅      ✅
hour/day          ✅        ✅        -        -       ✅      ✅
```

### 5.8 存储位置总览

| 记录 | 文件 | 粒度 | 条数 |
|------|------|------|------|
| task_session | `meta.json` | 1 per task | N tasks |
| subtask_execution | `<sub_id>/result.json` | 1 per subtask | N×M subtasks |
| plan_generation | `execution.log` (plan_complete event) | 1 per plan | N tasks × iterations |
| api_call | `execution.log` (api_call event) | 1 per API 调用 | ~N tasks × (1 plan + optional) |
| verification_run | `result.json` 子数组 | 1 per 验证命令 | N×M×K commands |

**无需新增文件**：所有新增字段嵌入现有 result.json、meta.json、execution.log 中。

---

## 六、实施计划

### Phase 1：采集层 + 存储层（立即）

| 任务 | 文件 | 内容 |
|------|------|------|
| 创建 `metrics.py` | 新文件 | 4 个采集函数 (timing/change_stats/merge_result/extract_usage) |
| 扩展 `result.json` | executor.py | 嵌入采集调用：timing + change_stats + merge_results + retry_count |
| 扩展 `api.py` | api.py | call_api 返回 usage + plan_duration_ms + status_code 捕获 |
| 测试 | tests/ | 验证 result.json 包含新字段 |

**改动量**: ~80 行代码，4 个文件

### Phase 2：分析层（后续）

| 任务 | 文件 | 内容 |
|------|------|------|
| 创建 `eval.py` | 新文件 | 6 个分析函数 + CLI 命令 |
| `agent_go eval quality/perf` | eval.py | 单任务 + --all 聚合 |

**改动量**: ~200 行代码，1 个新文件

### Phase 3：全维度（后续）

| 任务 | 内容 |
|------|------|
| `eval cost/reliability/ux` | 等 token/缓存/session_stats 数据积累 |
| `eval compare` | 版本对比 |

---

## 七、指标-采集-分析 完整映射

| 指标 | 事实记录 (5.2-5.6) | 主维度 | 主度量 | 采集 | 分析函数 |
|------|-------------------|--------|--------|------|---------|
| Q1 任务成功率 | task_session | status | count | 已有 | analyze_quality |
| Q2 Subtask成功率 | subtask_execution | status | count | 已有 | analyze_quality |
| Q3 首次通过率 | subtask_execution | agent_type, skills | retry_count=0 count | A2 | analyze_quality |
| Q4 验证通过率 | subtask_execution + verification_run | agent_type | exit_code | 已有 | analyze_quality |
| Q5 新文件遗漏率 | subtask_execution | status, agent_type | change_stats.new_files | A3 | analyze_quality |
| Q6 产物传递 | subtask_execution.merge_results | upstream | status=success count | A5 | analyze_quality |
| Q7 计划准确性 | subtask_execution.change_stats | — | actual_files vs plan.files | A3 | analyze_quality |
| Q8 变更规模 | subtask_execution.change_stats | agent_type, skills | files/insertions/deletions | A3 | analyze_quality |
| P1 端到端耗时 | task_session | — | total_duration_sec | 已有 | analyze_performance |
| P2 Plan耗时 | plan_generation | provider, cache_hit | plan_duration_ms | A6 | analyze_performance |
| P3 平均耗时 | subtask_execution | agent_type | duration_sec avg | 已有 | analyze_performance |
| P4 耗时分布 | subtask_execution | agent_type | duration_sec P50/95/99 | 已有 | analyze_performance |
| P5 阶段占比 | subtask_execution.timing | agent_type, sandbox_type | 各阶段 ms | A1 | analyze_performance |
| P6 并发效率 | subtask_execution + task_session | parallel | wall_time / sum(duration) | 已有 | analyze_performance |
| C1 API调用 | api_call | provider, call_type | count | A4 | analyze_cost |
| C2 Token | api_call + plan_generation | provider, model | prompt+completion sum | A4 | analyze_cost |
| C3 费用 | api_call + plan_generation | provider | tokens × price | A4 | analyze_cost |
| C4 缓存命中 | plan_generation | — | cache_hit count | B1 | analyze_cost |
| R1 中断恢复 | task_session | — | interrupted/resumed | B1 | analyze_reliability |
| R3 API错误 | api_call | provider, status_code | status_code≠200 count | A7 | analyze_reliability |
| R8 重试率 | subtask_execution | agent_type, skills | retry_count>0 count | A2 | analyze_reliability |

---

*文档结束*
