# PRD: Plan 结果缓存机制

> 版本: v1.0  
> 日期: 2026-05-26  
> 作者: Product  
> 状态: ✅ 已实现 (v0.6 — SHA256 缓存键 + 24h TTL + cache 管理命令)  
> 关联: [../design/roadmap.md](../design/roadmap.md) M2 | [../design/requirements.md](../design/requirements.md) #23

---

## 一、背景与问题

### 1.1 现状

每次 `agent_go run` 都调用 LLM API 生成 Plan，即使相同任务描述和相同的项目。API 调用是最大的时间成本（3-10s）和资金成本。

### 1.2 核心问题

| 问题 | 影响 |
|------|------|
| 重复调用浪费 | 相同任务多次运行每次都调用 API |
| CI/CD 场景成本高 | 自动化触发频繁，API 费用线性增长 |
| Plan 迭代时丢失 | 修改 Plan 后重新生成，原始版本不可恢复 |

---

## 二、目标

| 目标 | 衡量指标 | 基线 | 目标值 |
|------|----------|------|--------|
| API 调用减少 | 重复任务场景缓存命中率 | 0% | ≥80% |
| 响应时间 | Plan 阶段耗时 | 3-10s | <0.1s（缓存命中） |
| 透明性 | 用户感知缓存状态 | 无 | 明确展示缓存标识 |

---

## 三、功能需求

### 3.1 F-C1: 缓存键设计

**优先级**: P0

```
cache_key = SHA256(
    任务描述（原始文本）
    + 项目文件列表（git ls-files 前 100 行）
    + 关键文件 mtimes（最多 10 个）
    + 项目 git remote URL
    + 项目 git branch
)
```

**命中条件**: 相同任务描述 + 相同项目结构 → 命中。项目文件变化则自动不命中。

### 3.2 F-C2: 缓存存储

**优先级**: P0

**路径**: `~/.agent_go/cache/plans/{cache_key[:2]}/{cache_key}.json`

```json
{
  "cache_key": "abc123...",
  "plan": { "overview": "...", "steps": [...], ... },
  "meta": {
    "created_at": "2026-05-26T15:00:00",
    "last_hit_at": "2026-05-26T16:00:00",
    "hit_count": 3,
    "task": "重构 JWT 认证",
    "ttl": 86400
  }
}
```

**TTL**: 默认 86400s（24h），`config.cache.plan_ttl` 可配置。

### 3.3 F-C3: 缓存命中流程

**优先级**: P0

```
agent_go run
  ├── --no-cache? → 跳过缓存，直接 API
  ├── iteration > 1? → 跳过缓存（补充/文档变了）
  ├── 计算 cache_key → 查找缓存
  │     ├── 命中 → "📦 使用缓存 Plan（Xh 前生成，命中 N 次）" → 跳过 API
  │     └── 未命中/过期 → 调用 API → 写入缓存
  └── 进入 Plan 确认流程
```

### 3.4 F-C4: 缓存管理命令

**优先级**: P1

```bash
agent_go cache list          # 列出所有缓存条目
agent_go cache show <key>    # 显示指定缓存的 Plan 详情
agent_go cache clean         # 清理过期缓存
agent_go cache clear         # 清除所有缓存（需确认）
agent_go cache stats         # 统计：条目数、命中率、磁盘占用
```

### 3.5 F-C5: 不缓存场景

| 场景 | 行为 |
|------|------|
| `--no-cache` 标志 | 跳过缓存 |
| Plan 迭代（iteration > 1） | 跳过缓存 |
| 缓存 TTL 过期 | 惰性删除，重新调用 API |
| 降级拆解（decompose_fallback） | 不缓存（本地规则生成） |
| 空 Plan（无 steps） | 不缓存 |

---

## 四、技术方案

### 4.1 改动文件

| 文件 | 改动 |
|------|------|
| `agent_go/api.py` | 新增 `get_cache_key()`、`load_cached_plan()`、`save_cached_plan()` |
| `agent_go/cli.py` | `cmd_run()` 调用缓存 + 新增 `cmd_cache()` |
| `agent_go/config.py` | `DEFAULT_CONFIG` 新增 `cache` 字段 |

### 4.2 配置

```json
{
  "cache": {
    "enabled": true,
    "plan_ttl": 86400,
    "max_entries": 100
  }
}
```

---

## 五、验收标准

1. 相同任务+相同项目 24h 内命中缓存，跳过 API
2. 终端显示 "📦 使用缓存 Plan（Xh 前，命中 N 次）"
3. `--no-cache` 强制跳过
4. 项目文件变更后自动不命中
5. `agent_go cache` 子命令可管理缓存

---

*文档结束*
