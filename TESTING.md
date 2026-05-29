# agent_go 测试指南

## 快速开始

```bash
# 安装测试依赖
pip3 install pytest pytest-mock

# 运行全部测试
pytest tests/

# 带详细输出
pytest tests/ -v

# 仅运行单元测试（纯函数，< 1s）
pytest tests/ -k "not integration"

# 仅运行集成测试（全流程 mock，< 2s）
pytest tests/test_integration.py -v

# 运行特定测试类
pytest tests/ -k "TestFormatCommit" -v

# 运行特定测试
pytest tests/test_format_commit.py::TestFormatCommitChinese::test_feat_add
```

## 测试架构

```
tests/
├── __init__.py
├── conftest.py                  # 共享 fixtures（logger、temp_dir、sample_plan）
│
├── test_format_commit.py        # _format_commit — Conventional Commits 生成
├── test_slugify.py              # _slugify — 分支名短标识生成
├── test_plan_to_subtasks.py     # plan_to_subtasks — Plan → 子任务转换
├── test_safe_append_to_file.py  # _safe_append_to_file — 线程安全文件追加
├── test_api.py                  # call_api — LLM API 调用（4 provider）
├── test_config.py               # load_config / get_api_key — 配置加载
├── test_project.py              # analyze_project / get_git_info / get_resource_map
├── test_read_reference_docs.py  # read_reference_docs — 参考文档读取
│
└── test_integration.py          # 全流程集成测试（所有外部调用 mock）
```

## 测试统计

| 文件 | 测试数 | 测试对象 | 类型 |
|------|:------:|----------|:----:|
| `test_format_commit.py` | 25 | `_format_commit()` | 纯函数 |
| `test_slugify.py` | 9 | `_slugify()` | 纯函数 |
| `test_plan_to_subtasks.py` | 11 | `plan_to_subtasks()` | 纯函数 |
| `test_safe_append_to_file.py` | 6 | `_safe_append_to_file()` | 纯函数 + 并发 |
| `test_api.py` | 5 | `call_api()` | mock HTTP |
| `test_config.py` | 7 | `load_config()`, `get_api_key()` | mock 文件系统 |
| `test_project.py` | 6 | `analyze_project()`, `get_git_info()`, `get_resource_map()` | mock subprocess |
| `test_read_reference_docs.py` | 6 | `read_reference_docs()` | 文件系统 |
| `test_integration.py` | 16 | 全流程管线 | mock 全部外部依赖 |
| **合计** | **91** | | |

## Mock 依赖总览

### 各测试文件的 Mock 依赖

```
test_api.py
  └─ mock: urllib.request.urlopen           → 伪造 HTTP 响应，验证请求头/body
     └─ 测试: Anthropic / OpenAI / DeepSeek / Custom 四种 provider

test_config.py
  ├─ mock: ~/.agent_go/config.json 文件     → 验证默认配置创建与合并
  └─ mock: os.environ (AGENT_GO_API_KEY)    → 验证环境变量优先级

test_project.py
  ├─ mock: subprocess.run                    → 伪造 git ls-files / remote / branch 输出
  └─ mock: FileNotFoundError                 → 验证无 git 时的降级

test_read_reference_docs.py
  └─ mock: 文件系统 (tmp_path)               → 验证文件读取/截断/路径穿越防护

test_safe_append_to_file.py
  └─ mock: 文件系统 (tmp_path)               → 验证追加/并发写入/锁清理

test_integration.py
  ├─ @patch("agent_go.generate_plan")       → 返回固定 Plan dict，跳过 API
  ├─ @patch("agent_go.run_subtask")         → 快速返回结果，跳过 Claude 执行
  ├─ @patch("agent_go._run_headless")       → 返回 mock CP，跳过子进程创建
  ├─ @patch("agent_go.subprocess.run")      → 返回 mock CP，跳过 git 操作
  └─ @patch("shutil.copytree")              → 跳过真实文件复制
```

### 各测试文件的真实依赖

以下函数在测试中**不 mock**，使用真实实现：

| 文件 | 真实执行的内容 |
|------|----------------|
| `test_format_commit.py` | `_format_commit()` — 纯字符串处理，无 IO |
| `test_slugify.py` | `_slugify()` — 纯字符串处理，无 IO |
| `test_plan_to_subtasks.py` | `plan_to_subtasks()` — 纯数据结构转换 |
| `test_safe_append_to_file.py` | `_safe_append_to_file()` 的文件读写操作 |
| `test_read_reference_docs.py` | `path.read_text()` 等真实文件系统操作 |
| `test_config.py` 部分测试 | `load_config()` 真实读写 `~/.agent_go/config.json` |
| `test_project.py` 部分测试 | 真实文件扫描（`repo.exists()`, `path.is_dir()`） |
| `test_integration.py` | `plan_to_subtasks()`, `plan_to_md()`, `decompose_fallback()` 规则匹配 |

### Mock 模式详解

```python
# ── 模式 A: @patch 装饰器（推荐） ──
# 作用域：整个测试函数
# 用法：将所有对该名称的引用替换为 MagicMock

@patch("agent_go.subprocess.run")
@patch("agent_go._run_headless")
def test_worktree_creation(self, mock_headless, mock_subprocess):
    mock_subprocess.return_value = MagicMock(returncode=0, stdout="ok")
    mock_headless.return_value = MagicMock(returncode=0)
    # 在 agent_go.py 内部所有 subprocess.run / _run_headless 调用均被拦截

# ── 模式 B: context manager ──
# 作用域：with 块内
# 用法：局部隔离，不影响其他测试

with patch("shutil.copytree") as mock_copy:
    mock_copy.return_value = None
    run_subtask(...)

# ── 模式 C: side_effect（动态返回值） ──
# 作用域：依据调用参数返回不同结果
# 用法：同一函数被多次调用时返回不同值

def side_effect(args, **kwargs):
    if "merge" in str(args):
        return MagicMock(returncode=1, stderr="CONFLICT")
    return MagicMock(returncode=0)

mock_subprocess.side_effect = side_effect
```

### 集成测试 Mock 数据流

```
测试函数
  │
  ├─ @patch("agent_go.generate_plan")
  │   └─ return_value = sample_plan    ──→ 跳过 call_api() HTTP 请求
  │
  ├─ plan_to_subtasks(plan)            ──→ 真实执行（纯函数）
  │
  ├─ @patch("agent_go.run_subtask")
  │   └─ side_effect = fast_result     ──→ 跳过 _run_headless / claude CLI
  │       ├─ 内部调用 @patch("agent_go.subprocess.run")
  │       │   └─ return_value = mock   ──→ 跳过 git clone/checkout/commit/tag
  │       └─ 内部调用 @patch("agent_go._run_headless")
  │           └─ return_value = mock   ──→ 跳过 claude -p 子进程
  │
  └─ _run_pipeline(subtasks)           ──→ 验证调度逻辑
      └─ 断言 meta["status"] == "completed"
```

## 设计原则

### 1. 纯函数优先

`_format_commit()`、`_slugify()`、`plan_to_subtasks()` 等纯函数测试不依赖任何 mock，直接调用并断言返回值：

```python
def test_feat_add(self):
    msg = _format_commit("新增用户登录功能")
    assert msg.startswith("feat:")
```

### 2. 外部调用全部 mock

涉及 API、subprocess、文件系统的测试全部通过 `unittest.mock` 隔离外部依赖：

```python
@patch("urllib.request.urlopen")
def test_anthropic_provider(self, mock_urlopen, logger):
    mock_resp = MockResponse({"content": [{"text": "响应"}]})
    mock_urlopen.return_value = mock_resp
    result = call_api(config, messages, logger)
    assert result == "响应"
```

### 3. 并发安全验证

`test_safe_append_to_file.py` 使用 10 个并发线程验证 `_safe_append_to_file()` 的线程安全性：

```python
def test_concurrent_safety(self, temp_dir, logger):
    n_threads = 10
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads: t.start()
    for t in threads: t.join()
    lines = fp.read_text().strip().split("\n")
    assert len(lines) == n_threads  # 数据不丢失
```

### 4. 集成测试通过 mock 实现全流程覆盖

`test_integration.py` 通过 mock `generate_plan`、`run_subtask`、`_run_headless`、`subprocess.run`，在无需真实 LLM/Claude/Git 的情况下验证完整 Plan → Decompose → Execute 管线：

| 测试 | 验证点 |
|------|--------|
| `test_happy_path` | 完整管线完成，meta.json 状态正确 |
| `test_parallel_vs_serial_timing` | 并行比串行快 3x |
| `test_parallel_with_dependencies` | 依赖约束下的执行顺序 |
| `test_plan_failure_triggers_fallback` | API 失败 → 规则拆解降级 |
| `test_resume_detects_completed` | 恢复时跳过已完成子任务 |
| `test_merge_conflict_detection` | merge 冲突时调用 --abort |
| `test_no_changes_detected` | 无变更子任务标记为 `no_changes` |

## 编写新测试

### 测试纯函数

将测试文件放入 `tests/` 目录，直接导入函数并断言：

```python
from agent_go.module_name import my_function

class TestMyFunction:
    def test_basic(self):
        assert my_function("input") == "expected"
```

### 导入约定

- **顶层 API**: `from agent_go import main, cmd_run, load_config` — 仅有 CLI 入口和核心配置
- **模块级导入**: `from agent_go.api import call_api, generate_plan` — 推荐，IDE 可直接跳转
- **私有函数（仅测试）**: `from agent_go.utils import _slugify` — 可访问但标记为内部实现

### 测试涉及 mock 的函数

使用 `@patch` 装饰器隔离外部依赖：

```python
from unittest.mock import patch

@patch("agent_go.subprocess.run")
def test_with_subprocess(self, mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout="ok")
    result = function_that_uses_subprocess()
    assert result == "ok"
```

### 添加集成测试

集成测试在 `test_integration.py` 中添加。关键 mock 目标：

| 目标函数 | mock 方式 | 说明 |
|----------|----------|------|
| `agent_go.generate_plan` | `return_value` = 固定 Plan dict | 跳过 API 调用 |
| `agent_go.run_subtask` | `side_effect` = 快速返回 | 跳过 Claude 执行 |
| `agent_go._run_headless` | `return_value` = mock CP | 跳过子进程创建 |
| `agent_go.subprocess.run` | `return_value` = mock CP | 跳过所有 git 操作 |

## 性能

全部 91 个测试在 3-4 秒内完成：

```
============================== 91 passed in 3.54s ==============================
```

集成测试中的并行验证使用 `time.sleep(0.3)` 模拟耗时，通过比较串行/并行执行时间验证并发效果。
