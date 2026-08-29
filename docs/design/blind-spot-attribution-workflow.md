# 盲区归因标注工作流设计（Blind-Spot Attribution Workflow）

- **日期**：2026-08-29
- **状态**：P0 / P1 / P1.5 已交付；P2 / P3 待讨论后实施
- **前置**：`c4ec257`（追溯闭环：漏报率 + 注记通道 + 排除收紧）、`ad892a3`（死挂起终态化 N/A）、`94d8893`（ISSUE-54 两步修复记录）

---

## 1. 背景

盲区命中率是 #49 信任指标（阶段 D 放行门）之一。`c4ec257` 建立了自动判定（两级证据：即时终局 + 交付后 14d 返工）与人工注记覆盖机制，但标注入口只有 CLI 单发命令——**标注完成率是指标效度的命门**：没人标，系统只能靠 git 触碰信号猜；标了，读数就是人工判断 + 自动信号的加权事实。

本设计回答两个问题：这一流程在软件工程中如何归类；以及如何用工作流/界面设计保证人工完成标注。

## 2. 软件工程归类

三重身份，最贴切的定位是第三种：

| 视角 | 归类 |
|------|------|
| 质量管理 | PDCA 的 Check/Act 环（度量 → 归因 → 纠偏） |
| SRE 传统 | 事后复盘（Postmortem）+ 缺陷逃逸分析（盲区 = 逃逸点预测，归因 = 逃逸路径确认） |
| **ML/数据工程（最贴切）** | **Human-in-the-loop 的 ground truth 供给**：自动信号（git 触碰）= 弱监督代理信号，人工注记 = 真值标注 |

本质：**放行门指标的真值数据集构建活动**——既非「测试」（事前）也非「运维」（响应），而是「度量体系的数据运营」。

## 3. 标注完成率的三大杀手与对策

| 杀手 | 对策 | 落地 |
|------|------|------|
| 时机错位（事后回忆） | 绑定既有动作（修 bug 的那一刻） | P0（返工现场提示）/ P2（hook 自动触发，待讨论） |
| 认知负担（拼命令/找 ID） | 现场预填 + 四选一选择题 | P1 向导 / P1.5 四按钮 |
| 无反馈（标了没感觉） | 即时价值回传 | 惰性重算即时生效 + Web 按钮即时变 `[已注:x]` |

## 4. 已交付（P0 / P1 / P1.5）

### 4.1 共用写入核心

`metrics.write_attribution(td, item, attribution, note) -> (ok, msg)`：CLI 单发、CLI 向导、Web 端点三入口共用。文件协议 `<task_dir>/blind_spot_attribution.json`，项级 `sig:key`（confirmed / false-hit / false-clear），任务级（missed）。重算时注记**优先于自动判定**——即时判定、不等观察期、可复活 N/A 项。

### 4.2 P0 — 零交互成本（纯输出改进）

`agent_go trust` 尾部自动列「返工未归因」任务 + **可直接复制的预填命令**（task_id/item 已填，人只补归因类型）；交付时无任何标注的返工任务给出任务级 `missed` 命令。数据源：`compute_post_delivery_rework().reworked[].annotated=False`。

### 4.3 P1 — 5 秒交互（向导模式）

`agent_go trust --annotate`（裸调用）→ 列近期有可归因标注的任务（每任务最多预览 3 项）→ 选任务编号（`m<N>` 直达任务级漏报）→ 选项编号 → 归因四选一（或任务级 missed）→ 可选备注 → 写入。非 TTY / EOF 环境优雅退出（headless 安全），空输入静默退出。

### 4.4 P1.5 — Web 四按钮（界面最优解）

任务详情「已知盲区」卡片（#51 谦逊层）增强：

- 三类可归因信号（uac / wa / inc）每行尾加 `✓确认 / 假阳 / 假阴` 三按钮（hover 有语义提示）
- 卡片标题行加 `漏报复注` 按钮（任务级 missed）
- 已标注项回显 `[已注:xxx]`（GET 详情透传 `blind_spot_attributions`）
- 点击 → `prompt` 备注（可空）→ `POST /api/tasks/{id}/blind-spot-attribution` → 按钮组局部替换为 `[已注:x]`（零刷新）
- 写端点复用 `metrics.write_attribution`，`web_audit.jsonl` 留痕（`tasks.blind_spot_attribution`）

**为什么 Web 是完成率最高形态**：看板/详情页是人的自然驻留地，标注动作嵌入视线路径，认知成本为「点一个按钮」。

### 4.5 测试

- `tests/test_web_ops.py::TestBlindSpotAttribEndpoint`（6 用例：项级/回显/任务级/非法信号 422/缺归因 422/404）
- `tests/test_metrics.py` 注记五路径 + 漏报两态（c4ec257 已含 10 用例）

## 5. P2 — 工作流自动触发（时机绑定）：**已实施 opt-in MVP（2026-08-29 拍板 ①）**

### 5.0 已交付（opt-in + Stop Hook 会话聚合）

- **开启/关闭**：`agent_go trust --watch-repo <repo>`（opt-in，观察信任后可转自动）/ `--watch-off <repo>`（卸载，其余配置保留）
- **watch index**：`~/.agent_go/attribution_watch.json`——开启时扫描该 repo 交付任务（交付三态 + 文件集非空 + 有三类盲区标注）登记 `{files, blind_items}`
- **Stop Hook**：会话结束聚合「未提交改动 ∩ 监视交付文件集」输出一条提醒（含预填 annotate 命令）；无命中静默 exit 0（每会话最多一次，零噪声）
- **注入安全**：合并式（保留用户已有 hooks/其他键）、幂等（HOOK_MARK 检测）、可卸载（精确移除本工具 entry）、首次注入备份 `settings.json.agent_go_bak`；hook 脚本放 `~/.agent_go/hooks/`（repo 内零新增文件）
- **实现**：`agent_go/attribution_watch.py`（install/uninstall/scan_repo_tasks/stop_hook_report）+ cli `attribution stop-hook` 子命令 + trust `--watch-repo/--watch-off`
- **测试**：`tests/test_attribution_watch.py` 10 用例（合并保留/幂等/只移除自己的/命中提醒/未命中静默/未监视静默/hook 脚本真实执行）

### 5.1 机制调研结论（历史存档）

### 5.1 机制调研结论

可复用基础：`goal_injector.py` 的 hook 注入先例（settings.json + 白名单脚本 + 备份恢复）、`notify.py` 三通道、Web SSE、`_post_delivery_touches` 触碰检测。

| 方案 | 机制 | 优劣 |
|------|------|------|
| **B（推荐主）** | Claude Code PostToolUse hook（matcher Edit\|Write）：交付时注册 delivery_index（task→交付文件集），hook 收 file_path 对照命中即提醒 | 修的那一刻触发；**agent 可代办标注**（人只答一个词）；仅覆盖 Claude Code |
| A（兜底） | git post-commit hook（`core.hooksPath` 隔离）：commit 触碰交付文件交集 → 预填命令 + desktop 通知 | 工具无关（vim/IDE 全覆盖）；「修完」而非「修时」；侵入用户 repo |
| C | agent_go watch / Web 后台轮询 + notify 推送 | 零侵入；非即时；需常驻进程 |
| D | `blind-spot-attribution` SKILL.md | 被动指引，与 B 协同（hook 触发时 agent 依 skill 获得操作规范） |

### 5.2 关键工程风险（与 goal_injector 的差异）

goal_injector 在**隔离 worktree** 覆盖式写 settings.json 是安全的；P2 在**用户主 repo**，必须：① 合并式注入（保留用户已有 hooks）+ 幂等；② 可卸载（`attribution unwatch`）；③ 性能（每次 Edit 跑脚本，需毫秒级路径匹配，不跑 git）。

### 5.3 后续决策（MVP2 范围）

1. ~~注入生命周期~~ → **已拍板 opt-in 起步**（观察信任后转自动）
2. agent 代办标注的确认边界：Claude 直接写注记 vs 输出建议等人确认？（MVP2，倾向折中：confirmed/false-hit 需确认、missed 直接写）
3. PostToolUse 即时提醒（带去重限流）是否追加？（MVP2，Stop 聚合已覆盖主要场景）
4. 方案 A git post-commit 兜底（vim/IDE 场景）与方案 D skill 指引何时补？（待 Stop Hook 实际使用数据）

## 6. 待讨论：P3 — 制度化（DoD 扩展）

- `agent_go review` 的 decision 环节顺带收集盲区归因
- 修复类 issue 关闭前置条件 = 有归因注记（把标注从「志愿」变「流程定义的完成」）
- 依赖：标注习惯已形成（P0-P2 数据起量后评估）

## 7. 操作速查（四场景）

```bash
# 确认命中：交付后人工修复验证了该盲区
agent_go trust --annotate task-xxx --item weakly_anchored_subtasks:sub-1 --attribution confirmed --note '…'
# 假阳性：自动计命中但实为巧合触碰
agent_go trust --annotate task-xxx --item inconclusive_evaluations:sub-2 --attribution false-hit
# 假阴性：观察期内已确认真出问题（提前判定命中）
agent_go trust --annotate task-xxx --item uncovered_acceptance_ids:AC-3 --attribution false-clear
# 任务级漏报：交付后出问题但当时无任何盲区标注
agent_go trust --annotate task-xxx --attribution missed --note '…'
# 交互向导（裸 --annotate）/ Web：任务详情盲区卡片按钮
agent_go trust --annotate
```

## 8. 边界与诚实声明

- 注记依赖人工参与，激励缺位风险仍在（P2/P3 即为此设计，待讨论）
- `blind_spot_miss_rate` 分母是返工任务（已知出问题），测「返工时有没有预警」，不是全任务漏报率
- judged 样本积累仍需时间——机制已就绪，读数等数据成熟
