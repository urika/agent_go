# UI 验证方案调研与三层定位（2026-08-12）

> 关联：M3 P0/P1 前端工具链落地、evaluator 视觉评估策略、mcp_client 消费层
> 背景：用户要求验证 agent_go 是否能无感支持网站开发，P0（前端构建验证）+ P1（E2E/视觉回归）已落地，本文调研更优的 UI 验证方案

## 1. 调研的三个方案

### 方案 A：自建视觉策略（截图 + vision LLM）

- **实现**：`agent_go/evaluator.py` `_VisualEvalStrategy`（P1 Part2 已建）
- **机制**：playwright 截图 → 多模态 LLM（Anthropic image source / OpenAI image_url）→ JSON pass/fail
- **优点**：无基线依赖，可做"设计意图"语义判断（如"这个登录页是否合理"）
- **缺点**：精度低（LLM 看图判断主观）、成本高（vision token 贵）、依赖 vision 模型

### 方案 B：Playwright CLI 快照测试（`toHaveScreenshot` 像素 diff）

- **实现**：`npx playwright test`（P1 Part1 白名单已解锁）
- **机制**：`expect(page).toHaveScreenshot('login.png')` 像素级 diff，首次 `--update-snapshots` 生成基线
- **优点**：**确定性**（像素 diff 零歧义）、**零 LLM 成本**、业界标准、CI 友好
- **缺点**：需维护基线截图、抗噪声需调 threshold、无法判断"语义合理性"

### 方案 C：Playwright MCP（@playwright/mcp 外部 server）

- **实现**：微软官方 npm 包 `@playwright/mcp`，通过 agent_go `mcp_client.py` 消费层接入
- **机制**：**无障碍树（accessibility snapshot）而非 vision 模型**——结构化 DOM 树精确描述页面状态
- **官方原话**：
  - "No vision models needed, operates purely on structured data"
  - "Deterministic tool application. Avoids ambiguity common with screenshot-based approaches"
  - "Fast and lightweight. Uses Playwright's accessibility tree, not pixel-based input"
- **但官方也明确说**：coding agent 应该用 CLI+SKILLS 而非 MCP——
  > "CLI invocations are more token-efficient... better suited for high-throughput coding agents that must balance browser automation with large codebases within limited context windows"
- **agent_go 定位**：正是 coding agent（代码库 + 浏览器自动化双重需求），CLI 是主路径

## 2. 三方案对比

| 维度 | CLI 快照 (B) | Playwright MCP (C) | 视觉策略 (A) |
|---|---|---|---|
| **确定性** | 高（像素 diff） | 高（无障碍树） | 低（LLM 主观） |
| **token 成本** | 零 | 中（无障碍树文本） | 高（vision 图片） |
| **语义判断** | 无（纯回归） | 有（结构化语义） | 有（设计意图） |
| **依赖** | playwright CLI | @playwright/mcp + npx | playwright + vision LLM |
| **集成成本** | 已解锁（P1） | 仅 config 一行 | 已建（P1 Part2） |
| **适用场景** | 回归验证（捕获意外改动） | 交互验证（页面走通） | 无基线的设计判断 |

## 3. 视觉回归 CLI 工具生态

| 工具 | 类型 | 机制 | agent_go 适配 |
|---|---|---|---|
| **Playwright snapshot** | 内置 | `toHaveScreenshot` 像素 diff | ✅ 主推（白名单已解锁） |
| reg-suit | npm | 像素 diff + 报告 | 可用（npm run） |
| BackstopJS | npm | 像素 diff + 多视口 | 可用（npx backstop） |
| Lost Pixel | npm | 像素 diff + OSS | 可用 |
| Percy / Chromatic / Applitools | SaaS | 托管视觉回归 | 需 API key，不适合沙箱 |

## 4. 三层定位结论

### Tier 1（主推）：Playwright CLI 快照测试

- **定位**：回归验证——捕获意外视觉改动
- **机制**：`toHaveScreenshot` 确定性像素 diff，零 LLM 成本
- **业界地位**：标准做法，CI 友好
- **agent_go 状态**：P1 Part1 已解锁白名单，可直接用作 verification 命令
- **推荐验证命令**：`npx playwright test`（含视觉回归断言）

### Tier 2（推荐补充）：Playwright MCP 外部 server

- **定位**：语义验证——验证页面交互流程走通（如"点击登录→跳转正确"）
- **机制**：无障碍树结构化精确，比 vision LLM 便宜且无歧义
- **agent_go 集成**：`mcp_client.py` 消费层已就绪，仅需 config 配置（见 §5）
- **与 CLI 的关系**：互补而非替代——MCP 适合交互探索，CLI 适合批量回归

### Tier 3（小众保留）：自建视觉策略（_VisualEvalStrategy）

- **定位**：无基线/无 MCP 环境下的"设计意图"语义判断
- **保留理由**：边角价值（如"这个组件看起来合理吗"），但不应主推
- **使用**：`evaluator.strategy = "visual"` + `evaluator.visual_url`（fail-open 降级 default）

## 5. Playwright MCP 配置

agent_go `mcp_client.py` 已支持外部 MCP server 消费。Playwright MCP 接入仅需 config：

```json
{
  "mcp_servers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest"],
      "enabled": true,
      "scope": "worker"
    }
  }
}
```

工具暴露为 `mcp__playwright__<tool>`（如 `mcp__playwright__browser_navigate`），在 worker 子任务中可用。

**启用前提**：子任务环境需有 `npx` + 网络（首次 `npx @playwright/mcp@latest` 下载包）。worktree 沙箱默认有 `npx`（继承宿主 PATH）。

**注意**：Playwright MCP 官方建议 coding agent 用 CLI。agent_go 默认 **不启用** Playwright MCP（`enabled: false`），用户按需开启——CLI 快照（Tier 1）覆盖大多数回归场景，MCP 作为交互探索的补充。

## 6. plan prompt 验证范式补充

plan prompt（`api.py` 验证命令生成规范）已补充：
- **主推视觉回归**：`npx playwright test`（含 `toHaveScreenshot` 确定性像素 diff）
- **首次基线**：`npx playwright test --update-snapshots`
- **E2E 交互**：`npx playwright test` / `cypress run --headless`

planner 会为前端项目生成合规的视觉回归验证命令。

## 7. 决策记录

- **不主推 vision LLM**：精度低、成本高、有更确定性的替代（CLI 像素 diff + MCP 无障碍树）
- **CLI 为主，MCP 为辅**：符合 Playwright 官方对 coding agent 的建议
- **保留 _VisualEvalStrategy**：fail-open 设计，无配置时降级 default，有边角价值
- **Playwright MCP 默认关闭**：避免给所有用户引入 npx 下载开销，按需启用
