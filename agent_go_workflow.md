# agent_go — 用户工作流程说明

> 适用版本: agent_go.py  
> 运行环境: macOS (MacBook Pro/Air, Apple Silicon)  
> 核心依赖: Claude Code, Python 3.11+, 外接 API Key（可选 Greywall）  
> 更新时间: 2026-05-15

---

## 一、环境准备（首次使用，约10分钟）

### 1.1 确认基础依赖

```bash
# 检查 Python
python3 --version   # 需 >= 3.11

# 检查 Claude Code
claude --version    # 需 >= 0.2.x

# 如未安装 Claude Code
npm install -g @anthropic-ai/claude-code
```

### 1.2 安装 Greywall（可选，约3分钟）

```bash
brew tap greyhavenhq/tap
brew install greywall
greywall --version
```

> **未安装 Greywall 会怎样？**  
> 脚本会自动降级为原生 Claude Code，功能正常，但缺少 `rm -rf /`、`git push --force` 等危险命令的自动阻断。

### 1.3 下载脚本并配置

```bash
# 创建个人 bin 目录
mkdir -p ~/bin

# 下载脚本（替换为实际地址）
curl -fsSL -o ~/bin/agent_go https://your-gist-url/agent_go.py
chmod +x ~/bin/agent_go

# 加入 PATH
echo 'export PATH="$HOME/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

# 验证
agent_go --help
```

### 1.4 配置外接 API（用于 Plan Mode）

**方式一：环境变量（推荐，安全）**

```bash
export AGENT_GO_API_KEY="sk-ant-api03-..."
```

**方式二：配置文件（首次运行自动创建）**

```bash
# 运行任意命令触发配置创建
agent_go config

# 编辑配置
cat ~/.agent_go/config.json
```

```json
{
  "plan_api": {
    "provider": "anthropic",
    "base_url": "https://api.anthropic.com/v1/messages",
    "api_key": "",
    "model": "claude-sonnet-4-20250514",
    "max_tokens": 4096,
    "temperature": 0.2
  },
  "behavior": {
    "auto_confirm_plan": false,
    "auto_confirm_subtasks": false,
    "show_agent_prompt": true,
    "show_resource_map": true,
    "max_plan_iterations": 5
  },
  "fallback": {
    "local_model_url": "http://localhost:8000/v1/chat/completions",
    "local_model_name": "qwen",
    "enable_rules": true
  }
}
```

**多供应商切换：**

| 供应商 | provider | base_url | model |
|--------|----------|----------|-------|
| Anthropic | `anthropic` | `https://api.anthropic.com/v1/messages` | `claude-sonnet-4-20250514` |
| OpenAI | `openai` | `https://api.openai.com/v1/chat/completions` | `gpt-4o` |
| DeepSeek | `deepseek` | `https://api.deepseek.com/v1/chat/completions` | `deepseek-chat` |
| 自定义 | `custom` | 你的端点 | 你的模型 |

---

## 二、快速开始：第一个任务（约5分钟）

### 2.1 选择一个测试项目

```bash
cd ~/projects/demo-app   # 有 Git 仓库的项目
git status
```

### 2.2 启动 任务

```bash
agent_go run . "将 JWT 签名从 HS256 改为 RS256"
```

**预期交互流程：**

```
🔧 主任务: 将 JWT 签名从 HS256 改为 RS256
📁 项目: /Users/you/projects/demo-app
🆔 任务ID: task-20260515-093045

🤖 进入 Plan Mode，生成执行方案...
（调用外接 API 分析项目结构并生成方案）

======================================================================
📋 执行方案（Plan Mode）第 1 版
======================================================================

📝 概述: 将项目认证模块签名算法从对称密钥 HS256 迁移至非对称密钥 RS256...
⏱️  预估: 2-3 小时

📦 共享资源清单:
   🔗 Git 远程: git@github.com:you/demo-app.git
   🌿 当前分支: main
   📁 关键目录: src, tests
   ⚙️  配置文件: package.json, .env.example

📌 执行步骤:

  [1] 后端 JWT 签名迁移
      生成 RSA 密钥对，修改 src/auth/jwt.ts...
      📁 文件: src/auth/jwt.ts, src/config/auth.ts
      ✅ 验证: npm run test:auth
      ⚠️  风险: 密钥文件权限需设置为 600
      🤖 Agent Prompt: 你是一个后端安全专家。请修改 src/auth/jwt.ts，
          将 jwt.sign 的算法从 HS256 改为 RS256...

  [2] 前端登录适配
      ...

  [3] 测试补充
      ...

🔗 依赖关系:
      步骤 2 依赖: [1]
      步骤 3 依赖: [1, 2]

======================================================================

请选择操作:
  [Y] 确认方案，拆解为子任务并执行
  [S] 补充输入/修正需求（将重新生成方案）
  [D] 挂载参考文档（将重新生成方案）
  [E] 编辑某个步骤
  [R] 重新生成方案
  [N] 取消任务

> Y
```

### 2.3 确认 Plan 方案

**选项说明：**

| 按键 | 操作 | 场景 |
|------|------|------|
| `Y` | 确认当前方案 | Plan 满意，进入子任务确认 |
| `S` | 补充输入/修正 | 方案方向对，但需调整细节（如"不要改接口格式"） |
| `D` | 挂载参考文档 | 有需求文档/API 规范需要纳入上下文 |
| `E` | 编辑某个步骤 | 微调单步的标题/描述/文件/Agent Prompt |
| `R` | 重新生成 | 整体方向不对，让模型重新规划 |
| `N` | 取消 | 放弃任务 |

**补充输入示例（按 `S`）：**

```
✏️  请输入补充内容（支持多行，空行结束）：
项目要求保留现有 JWT 格式，不要引入 OAuth2
只需把 HS256 改为 RS256，不要改动接口格式

（重新调用 API，携带补充内容生成第 2 版方案）

🔄 已基于补充内容重新生成方案（第 2 版）
```

**挂载文档示例（按 `D`）：**

```
📎 请输入参考文档路径（多个逗号分隔，目录自动读 .md）：
> README.md, docs/security-requirements.md

（读取文档内容，注入 Prompt，重新生成方案）

🔄 已基于参考文档重新生成方案（第 2 版）
```

### 2.4 确认子任务并执行

Plan 确认后，系统展示子任务列表：

```
📋 子任务列表
────────────────────────────────────────────

  [sub-1] 后端 JWT 签名迁移
      生成 RSA 密钥对，修改 src/auth/jwt.ts...
      📁 涉及文件: src/auth/jwt.ts, src/config/auth.ts
      🤖 Agent Prompt: 你是一个后端安全专家。请修改 src/auth/jwt.ts...

  [sub-2] 前端登录适配
      ...

  [sub-3] 测试补充
      ...
────────────────────────────────────────────

请选择操作:
  [Y] 全部确认并执行
  [N] 取消任务
  [E] 编辑某个子任务
  [A] 添加新子任务
  [D] 删除某个子任务

> Y
```

### 2.5 在 Claude Code 中交互

每个子任务启动独立的 Claude Code 实例：

```
🚀 启动子任务 sub-1: 后端 JWT 签名迁移
   📁 Worktree: /Users/you/.agent_go/task-xxx/sub-1/work
   ⏳ 请在 Claude Code 中完成任务，然后退出

（终端进入 Claude Code 交互界面）

➜  work git:(main) ✗ claude
> 阅读 TASK.md 中的任务描述，修改 src/auth/jwt.ts 使用 RS256...

（Claude Code 自动分析、编码、测试）

> /exit
```

### 2.6 进展验证

子任务完成后，系统自动展示变更摘要：

```
============================================================
✅ 子任务 1/3 完成
============================================================

📊 变更摘要:
 src/auth/jwt.ts        | 45 ++++++++++++++--
 src/config/auth.ts     | 32 ++++++++++++
 src/routes/public-key.ts | 18 +++++++
 3 files changed, 91 insertions(+), 4 deletions(-)

请选择:
  [C] 继续下一个子任务
  [R] 重试当前子任务（丢弃当前变更，重新执行）
  [M] 修改后继续（进入 Claude Code 补充修改）
  [A] 中止整个任务

> C
```

### 2.7 任务完成

```
🎉 所有子任务完成 (3/3)

📦 任务完成报告:
────────────────────────────────────────────
✅ sub-1: 3 files changed, 91 insertions(+), 4 deletions(-)
✅ sub-2: 2 files changed, 38 insertions(+), 12 deletions(-)
✅ sub-3: 1 file changed, 28 insertions(+)
────────────────────────────────────────────

📁 所有变更位于: /Users/you/.agent_go/task-20260515-093045
📝 完整日志: /Users/you/.agent_go/task-20260515-093045/execution.log

查看总变更:
   cd /Users/you/.agent_go/task-xxx && find . -name 'work' -exec git -C {} diff --stat \;

合并回原始项目:
   对每个子任务: cd .../sub-X/work && git diff > /tmp/patch-X.diff
   原始项目: git apply /tmp/patch-X.diff

🧹 清理任务:
   rm -rf /Users/you/.agent_go/task-20260515-093045
```

---

## 三、带参考文档启动

```bash
# 启动时挂载文档（预加载）
agent_go run ~/projects/my-app "重构认证模块"     --docs "README.md,docs/auth-spec.md,docs/migration-guide.md"

# 支持格式：
#   - 相对项目路径: README.md, docs/spec.md
#   - 绝对路径: /Users/you/docs/spec.md
#   - 目录（自动读取所有 .md）: docs/
```

---

## 四、默认同意模式（非交互批量执行）

编辑配置文件启用：

```bash
cat > ~/.agent_go/config.json << 'EOF'
{
  "plan_api": {
    "provider": "anthropic",
    "api_key": "",
    "model": "claude-sonnet-4-20250514"
  },
  "behavior": {
    "auto_confirm_plan": true,
    "auto_confirm_subtasks": true,
    "show_agent_prompt": true,
    "show_resource_map": true
  }
}
EOF
```

**效果：**
- Plan 方案展示后，按 `Enter` 直接确认（无需输入 `Y`）
- 子任务列表展示后，按 `Enter` 直接执行
- 适合批量任务或信任度高的场景

**临时强制交互（覆盖默认同意）：**

```bash
AGENT_GO_INTERACTIVE=1 agent_go run ~/projects/my-app "重要任务"
```

---

## 五、日常操作流程

### 5.1 标准工作流

```
[1] 进入项目目录
      cd ~/projects/my-app

[2] 启动 任务
      agent_go run . "<具体任务描述>"

[3] Plan Mode 生成方案
      - 审查共享资源清单
      - 审查 Agent Prompt
      - 确认 / 补充 / 挂载文档 / 编辑

[4] 确认子任务列表
      - 审查子任务描述和 Agent Prompt
      - 确认 / 编辑 / 增删

[5] 逐个执行子任务
      - 在 Claude Code 中交互完成
      - 退出后审查变更摘要
      - 继续 / 修改 / 重试 / 中止

[6] 审查最终变更
      cd ~/.agent_go/task-xxx/sub-1/work && git diff

[7] 合并或丢弃
      git apply /tmp/patch.diff   # 合并
      rm -rf ~/.agent_go/task-xxx  # 丢弃
```

### 5.2 任务描述撰写建议

好的任务描述能让 Plan Mode 生成更精准的方案：

| ✅ 好的描述 | ❌ 差的描述 |
|------------|------------|
| "将 `src/auth/jwt.ts` 的签名算法从 HS256 改为 RS256，使用 `jsonwebtoken` 库的 `sign` 方法时传入 RSA 私钥" | "改一下 JWT" |
| "在 `README.md` 的安装章节添加 Node.js 版本要求（>=18）和 npm install 步骤" | "更新文档" |
| "给 `src/utils/date.ts` 添加单元测试，覆盖 `formatISO` 和 `parseISO`，使用 Jest" | "写点测试" |

---

## 六、查看历史任务

```bash
# 查看所有任务摘要
agent_go list

# 输出示例：
# 任务ID                      状态         子任务   参考文档       描述
# task-20260515-093045        🟢 completed  3       README.md      将JWT签名从HS256改为RS256...
# task-20260515-101230        🟡 aborted    2       docs/spec.md   重构数据库连接池
```

```bash
# 查看任务详情
agent_go show task-20260515-093045

# 输出包含：
# - 任务描述、项目路径、状态
# - 参考文档列表
# - 每个子任务的标题、Agent Prompt、变更摘要、耗时
```

---

## 七、日志查看

```bash
# 查看完整文本日志
cat ~/.agent_go/task-xxx/execution.log

# 提取结构化 JSON 事件
grep DEBUG ~/.agent_go/task-xxx/execution.log | grep '{' | python3 -m json.tool

# 常用查询：
# 查看所有用户决策
grep 'user_plan_choice\|user_verify' ~/.agent_go/task-xxx/execution.log

# 查看 API 调用耗时
grep 'api_call' ~/.agent_go/task-xxx/execution.log

# 查看 Plan 迭代过程
grep 'plan_generate\|plan_complete' ~/.agent_go/task-xxx/execution.log
```

---

## 八、故障排查

### 8.1 API 调用失败

```
❌ API 调用失败: HTTP 401
```

**处理**：检查 `AGENT_GO_API_KEY` 或 `config.json` 中的 `api_key` 是否正确。

### 8.2 Plan 生成失败，降级为本地拆解

```
⚠️ Plan Mode 失败: [错误信息]
降级为本地拆解...
```

**处理**：功能正常，只是缺少外接 API 的精细化方案。检查网络或 API Key。

### 8.3 Claude Code 无法启动

```bash
which claude
claude --version
npm install -g @anthropic-ai/claude-code
```

### 8.4 Greywall 未找到

```
⚠️  Greywall 未安装，降级为原生 Claude Code
```

**处理**：功能正常，缺少安全限制。可按提示安装，或继续无沙盒模式（谨慎操作）。

### 8.5 任务目录权限错误

```bash
chmod -R u+rw ~/.agent_go/
```

### 8.6 默认同意模式下想临时交互

```bash
AGENT_GO_INTERACTIVE=1 agent_go run ~/projects/my-app "任务"
```

---

## 九、命令速查表

| 命令 | 作用 |
|------|------|
| `agent_go run <path> "<task>"` | 创建任务 → Plan Mode → 确认 → 执行 |
| `agent_go run <path> "<task>" --docs "a.md,b.md"` | 带参考文档启动 |
| `agent_go list` | 查看所有任务摘要 |
| `agent_go show <task-id>` | 查看任务详情和子任务进展 |
| `agent_go config` | 查看当前配置 |
| `agent_go clean` | 清理所有任务 |

---

*文档结束 — 祝使用愉快*
