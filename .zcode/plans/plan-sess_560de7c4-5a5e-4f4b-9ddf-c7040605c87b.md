## 修复 P0「声明与实现不符」

调研发现三组相互交织的问题，一并修复：

### 问题 A：任务集半提交 + repo 路径不可移植（最严重）
- 22 个任务中 12 个是 untracked（git status `??`）
- **22 个任务的 `repo:` 全部用绝对路径** `/Users/jinsongwang/test-target/...` 或 `/tmp/fp-target`（后者在你本机都不存在）→ 任何人 clone 仓库后 `eval bench` 全部失败
- `eval_suite/fixtures/task-mgr` 仓库内副本已存在但无任务引用它（孤岛）

### 问题 B：文档数字过时（17 处，跨 7 个文件）
精确清单（grep 实测）：
- **模型数**：文档写 22（5 处）/ 38（1 处 spec.md）/ 20（2 处 design md），代码实际 **48**
- **任务数**：文档写 8（6 处）/ 12（1 处 AGENTS.md），代码实际 **22**
- **行数**：spec.md 写 pricing.py "130 行"（实际 **152**）、cross_judge.py "350 行"（实际 **413**）
- 涉及文件：`docs/roadmap.md`、`docs/prd.md`、`docs/spec.md`、`docs/design/model-evaluation-and-tiering.md`、`AGENTS.md`、`README.md`、`CLAUDE.md`

### 问题 C：cross_judge 零测试 + 四维评分退化
- `tests/` 里 `cross_judge` 零覆盖（grep 确认）
- 四维（correctness/completeness/code_quality）退化为同一个 `semantic_score`（`cross_judge.py:201-203`）
- 用正则从 reason 文本提取分数（`_heuristic_score`），非结构化 rubric
- 底层 `evaluate_semantic` 只返回 `{passed, reason}`，根本不产出四维分——**用户已确认本次不改 evaluator.py**，按"补测试 + 修文档措辞"方案

---

## 实施方案

### 第 1 步：补 fixture（问题 A 的前置）

把 `/Users/jinsongwang/test-target/` 下三个独立项目拷成仓库内 fixture，**排除 `.git`、worktree 残留、`__pycache__`**：

```bash
# task-mgr 已存在于 eval_suite/fixtures/，但当前是 agent_go 主仓库的嵌套子目录（无独立 .git）
# 三个目标：让 fixtures/ 下有 task-mgr / data-pipeline / django-blog 三个独立 git 仓库
```

**做法**：
1. 删除现有 `eval_suite/fixtures/task-mgr`（孤岛副本）
2. 用 `cp -R` 把三个项目拷进来，排除 `.git`、`__pycache__`、`.pytest_cache`、worktrees
3. 三个 fixture 各自 `git init -b main && git add -A && git commit -m "fixture: initial state"`（独立 git，让 agent_go worktree 机制能工作）
4. 验证：每个 fixture 跑一次 `git status` 应干净、`git log` 有 1 条 commit

**fixture 数据已确认无敏感信息**：django-blog 的 `SECRET_KEY="insecure-dev-key-not-for-production"` 是开发占位符，DB 密码走环境变量有默认值。

### 第 2 步：修任务 repo 路径 + 提交 untracked（问题 A）

把 22 个任务文件的 `repo:` 字段全部改成相对路径：

| 旧路径 | 新路径 |
|--------|--------|
| `/Users/jinsongwang/test-target/task-mgr` | `eval_suite/fixtures/task-mgr` |
| `/Users/jinsongwang/test-target/data-pipeline` | `eval_suite/fixtures/data-pipeline` |
| `/Users/jinsongwang/test-target/django-blog` | `eval_suite/fixtures/django-blog` |
| `/tmp/fp-target`（不存在） | **新建 `eval_suite/fixtures/fp-sandbox`**（最小空项目，给 13-email-validator / 14-safe-file-reader 用） |

`bench.py:76-77` 已支持相对路径（`Path.cwd()/repo`），跑 bench 时 cwd 是 workspace 根，相对路径能正确解析。

然后 `git add` 全部 12 个 untracked 任务文件 + 修改的 22 个 repo 字段。

### 第 3 步：修文档过时数字（问题 B，17 处）

精确替换（grep 已定位）：

**模型数 22/38/20 → 48**：
- `docs/roadmap.md:20` "22 模型定价表" → "48 模型定价表"
- `docs/roadmap.md:21` "22 模型定价表" → "48 模型定价表"
- `docs/roadmap.md:47` "（22 模型）" → "（48 模型）"
- `docs/spec.md:320` "38 个模型" → "48 个模型"
- `docs/design/model-evaluation-and-tiering.md:392,403` "20 个模型" → **改为"已扩充至 48 个模型（2026-07 最新）"**（设计稿是历史规划，注明实际落地数）
- `AGENTS.md:132` "(22 models)" → "(48 models)"
- `CLAUDE.md:112` "(22 models)" → "(48 models)"

**任务数 8/12 → 22**：
- `docs/roadmap.md:21,47,61,168` 全部 "8 任务" → "22 任务"
- `docs/design/model-evaluation-and-tiering.md:393,405` "8 任务" / "8 个标准任务" → "22 任务" / "22 个标准任务"
- `AGENTS.md:225` "12 tasks" → "22 tasks"
- `README.md:132` "(8 tasks + fixtures)" → "(22 tasks + fixtures)"
- `CLAUDE.md:149` "(8 tasks + fixtures)" → "(22 tasks + fixtures)"

**行数**：
- `docs/spec.md:297` "cross_judge.py — 交叉评判矩阵 (350 行)" → "(413 行)"
- `docs/spec.md:318` "pricing.py — 大模型定价表 (130 行)" → "(152 行)"

### 第 4 步：cross_judge 补测试（问题 C）

新建 `tests/test_cross_judge.py`，按 `test_evaluator.py` 范式（mock `evaluate_semantic` + tmp_path 造 worktree）。

**测试用例清单（≥8 个）**：

1. **`_infer_provider`**（5 case）：claude→anthropic、gpt→openai、gemini→google、deepseek→deepseek、glm→zhipu、未知→custom
2. **`_same_provider`**（2 case）：同 provider 返回 True；跨 provider 返回 False
3. **`_heuristic_score`**（5 case）：
   - "完全正确" → 5.0；"基本正确" → 4.0；"缺少" → 2.0；"错误" → 1.0；空 → 2.5；其他 → 3.0
   - **显式断言"四维退化"行为**（用注释说明这是 P1 简化，P2 升级时此测试需更新）
4. **`cross_judge_results` 禁绝自评**（2 case）：
   - candidate=claude-sonnet-4, judge=claude-haiku-4 → 标 `error="自评禁止"`，`semantic_score=-1`（同 provider 不同模型也禁）
   - candidate=claude-sonnet-4, judge=gpt-5 → 正常评判（mock evaluate_semantic 返回固定 dict）
5. **`cross_judge_results` 无 worktree 降级**（1 case）：meta.json 的 results 里所有 subtask 无 `worktree` 字段 → 所有 judge 填 `error="无可用 worktree"`
6. **`_judge_one` 正常路径**（1 case）：mock `evaluate_semantic` 返回 `{passed:True, reason:"完全正确", cost_usd:0.01, latency_ms:200}` → 断言返回的 correctness/completeness/code_quality 都是 5.0（验证退化行为）、false_positive=False、semantic_score=5.0
7. **`_judge_one` 异常路径**（1 case）：mock `evaluate_semantic` 抛 RuntimeError → 填 `error` + `semantic_score=-1`
8. **`calibrate_judge`**（2 case）：
   - LLM 与人工分歧 ≤1.0 → reliable
   - 分歧 >1.5 → unreliable
   - 用 tmp_path 造 LLM JSONL + human CSV

**关键设计**：所有测试都 **mock `agent_go.evaluator.evaluate_semantic`**（参考 `test_eval.py:367` 的 `@patch` 范式），不调真实 LLM。

### 第 5 步：修 cross_judge 文档措辞（问题 C）

诚实标注当前是 P1 简化版，避免"声明与实现不符"：

**`docs/spec.md:308-310`**（cross_judge 评分尺度章节）：
- 原文声称"评分尺度：correctness/completeness/code_quality（1-5）+ false_positive(bool)"
- 改为注明"**当前实现（P1 简化）**：四维退化为单一 `semantic_score`（由 reason 文本启发式提取），`false_positive = not passed`。P2 计划：升级 `evaluator.py` prompt 为结构化 rubric，产出独立四维分。"

**`docs/roadmap.md:21`** cross_judge 描述：
- "（交叉评判矩阵 P1 + 禁绝自评 + 人工校准）" → "（交叉评判矩阵 **P1 简化版**：禁绝自评 + 启发式评分 + 人工校准；P2 升级结构化 rubric）"

**`cross_judge.py` 代码内**：
- `JUDGE_RUBRIC` 常量上方加注释明确"目标 rubric，P1 阶段未实现独立四维评分"
- `_judge_one` 第 201-203 行的 `# 简化：用总分代理` 注释扩充为说明 P1→P2 的演进路径

---

## 验证

```bash
# 1. fixture 独立 git 验证
for d in task-mgr data-pipeline django-blog fp-sandbox; do
  test -d eval_suite/fixtures/$d/.git && echo "$d: OK" || echo "$d: MISSING .git"
done

# 2. 任务 repo 路径全部相对化
grep -l "^repo: /" eval_suite/tasks/*.yaml  # 期望无输出

# 3. bench 冒烟（确认相对路径能解析）
python3 -c "
from pathlib import Path
import yaml
for tf in sorted(Path('eval_suite/tasks').glob('*.yaml')):
    t = yaml.safe_load(tf.read_text())
    repo = Path(t['repo'])
    if not repo.is_absolute():
        repo = Path.cwd() / repo
    assert repo.exists(), f'{tf.name}: repo not found {repo}'
print('all 22 task repos resolve OK')
"

# 4. cross_judge 测试
pytest tests/test_cross_judge.py -v

# 5. 全量回归
pytest tests/ -q
```

## 不做的事

- ❌ 不改 `evaluator.py` 的 prompt 模板和返回结构（用户明确选了"补测试 + 修文档措辞"，不改 evaluator）
- ❌ 不改 `cross_judge.py` 的退化行为本身（保留 P1 简化，只在文档/注释诚实标注）
- ❌ 不改 `bench.py` / `cmd_judge` 的业务逻辑（只补测试覆盖）
- ❌ 不实现"人工抽检 10%"机制（用户未要求，文档措辞调整即可）
- ❌ 不动 `docs/archive/` 历史文档（保留历史快照）

## 交付物清单

| 文件 | 改动类型 |
|------|---------|
| `eval_suite/fixtures/task-mgr/` | 替换为独立 git 的 fixture |
| `eval_suite/fixtures/data-pipeline/` | 新增（从 test-target 拷贝） |
| `eval_suite/fixtures/django-blog/` | 新增（从 test-target 拷贝） |
| `eval_suite/fixtures/fp-sandbox/` | 新增（最小空项目） |
| `eval_suite/tasks/*.yaml`（22 个） | repo 字段改相对路径；12 个 untracked 加入 git |
| `docs/roadmap.md` | 4 处 22→48 模型、4 处 8→22 任务、cross_judge 措辞 |
| `docs/prd.md` | 检查并同步（如有相关数字） |
| `docs/spec.md` | 38→48 模型、130→152 行、350→413 行、cross_judge 评分尺度 P1 标注 |
| `docs/design/model-evaluation-and-tiering.md` | 20→48、8→22 |
| `AGENTS.md` | 22→48 模型、12→22 tasks |
| `README.md` | 8→22 tasks |
| `CLAUDE.md` | 22→48 模型、8→22 tasks |
| `agent_go/cross_judge.py` | JUDGE_RUBRIC 和退化点注释扩充 |
| `tests/test_cross_judge.py` | 新增（≥8 个测试用例） |