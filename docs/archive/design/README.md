# agent_go 设计文档索引

> 版本: 2026-07-24
> 整理: 统一命名、分组归类、交叉链接

---

## 文档导航

### 架构设计

| 文档 | 内容 | 行数 |
|------|------|------|
| [architecture.md](architecture.md) | 核心架构：设计目标、6 步管线、关键决策、数据结构、安全边界 | 418 |
| [workflow.md](workflow.md) | 用户工作流：完整操作流程、命令示例、配置说明、故障排除 | 544 |
| [data-architecture.md](data-architecture.md) | 数据架构：三层评估体系、采集存储分析分离、持久化布局 | 339 |

### 产品管理

| 文档 | 内容 | 行数 |
|------|------|------|
| [roadmap.md](roadmap.md) | 产品路线图：版本交付历程、NFR KPI 状况、Q3 优先投入 | 159 |
| [requirements.md](requirements.md) | 需求清单：P0/P1/P2/P3 功能需求 + NFR 24 项 + 测试覆盖 | 200 |

> **产品 PRD 已独立为 `docs/prd/`**，主入口: [../prd/PRD.md](../prd/PRD.md)

### 设计审查

| 文档 | 内容 | 行数 |
|------|------|------|
| [review/design-review.md](review/design-review.md) | 概念与机制问题评审：全 13 模块概念模型审查 | 345 |
| [review/data-architecture-review.md](review/data-architecture-review.md) | 数据架构审查：三层审查 (模型/完整性/性能) | 908 |

### 质量与测试

| 文档 | 内容 | 行数 |
|------|------|------|
| [quality/nfr-testing-strategy.md](quality/nfr-testing-strategy.md) | NFR 测试策略：7 维度分析、优先级排序、实施路线图 | 390 |

### 历史归档

| 文档 | 内容 |
|------|------|
| [archive/github-workflow-alignment.md](archive/github-workflow-alignment.md) | 早期需求调研：Claude Code 本地开发与 GitHub PR 流程对齐 |

---

## 文档关系图

```
                    ┌─────────────────────────────┐
                    │  ../prd/PRD.md (产品PRD)     │
                    │  产品定位 · 功能优先级 · KPI  │
                    └─────────────┬───────────────┘
                                  │ 引用
            ┌─────────────────────┼─────────────────────┐
            ▼                     ▼                     ▼
    ┌───────────────┐   ┌───────────────┐   ┌───────────────────┐
    │ architecture  │   │  requirements │   │    roadmap        │
    │ 架构设计       │   │  需求清单      │   │    路线图          │
    └───────┬───────┘   └───────────────┘   └───────────────────┘
            │
    ┌───────┼───────────┐
    ▼       ▼           ▼
┌──────┐ ┌──────────┐ ┌────────────────┐
│work- │ │  data-   │ │   review/      │
│flow  │ │architecture│ │  design-review │
│工作流│ │ 数据架构  │ │  data-arch-    │
│      │ │          │ │  review        │
└──────┘ └──────────┘ └────────────────┘
```

---

## 整理说明 (2026-07-24)

**重命名**:
- `agent_go-design.md` → `architecture.md`
- `agent_go_workflow.md` → `workflow.md`
- `DATA-ARCHITECTURE-数据架构设计.md` → `data-architecture.md`
- `PRODUCT_ROADMAP.md` → `roadmap.md`
- `REQUIREMENTS.md` → `requirements.md`

**分组**:
- `DESIGN-REVIEW-*.md` + `REVIEW-*.md` → `review/`
- `nfr-testing-strategy.md` → `quality/`
- `需求-github工作流程对齐.md` → `archive/`

**独立**: 6 份子 PRD + 产品定位 + 功能优先级 → `docs/prd/`（主入口 [PRD.md](../prd/PRD.md)）

所有交叉引用已更新为新的相对路径。
