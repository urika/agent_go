# 需求：github工作流程对齐

User: claude本地开发如何与github伤的PR管理流程对齐？
Kimi: 将 Claude Code 本地开发与 GitHub PR 管理流程对齐，核心在于建立 **"本地编码 → AI 辅助 → 分支规范 → PR 自动化"** 的闭环。以下是针对你技术栈的完整方案：

---

## 一、分支策略与 Claude Code 工作流对齐

### 1. 分支命名规范（Claude 可自动执行）
让 Claude 在创建功能分支时遵循命名约定，便于 GitHub 自动化识别：

```bash
# 分支命名格式
feature/ISSUE-ID-short-desc   # 新功能
fix/ISSUE-ID-short-desc       # Bug 修复
refactor/ISSUE-ID-area        # 重构
docs/ISSUE-ID-topic           # 文档

# Claude 指令示例
"Create a feature branch for issue #42 about user auth, named feature/42-oauth-login"
```

### 2. Claude Code 的 GitHub 集成配置
在 Claude Code 项目根目录创建 `.claude/settings.json`（或项目级配置）：

```json
{
  "git": {
    "defaultBranch": "main",
    "requireIssueReference": true,
    "commitTemplate": "{{type}}({{scope}}): {{message}}\n\nFixes #{{issueId}}\n\n{{description}}"
  },
  "github": {
    "autoLinkIssues": true,
    "prTemplate": ".github/pull_request_template.md"
  }
}
```

---

## 二、Commit 规范与 Issue 自动关联

### 1. Conventional Commits + 自动关 Issue
要求 Claude 生成的 commit message 包含 Issue 引用：

```bash
# 格式
<type>(<scope>): <subject>

<body>

Fixes #123
Closes #456
Relates to #789

# Claude 提示词模板
"Commit this change with message: feat(auth): add OAuth2 login flow. 
 Reference issue #42. Include BREAKING CHANGE note if API changed."
```

### 2. GitHub Actions 自动验证
配置提交信息检查，拒绝不合规的 commit：

```yaml
# .github/workflows/commit-lint.yml
name: Commit Lint
on: [pull_request]

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: wagoid/commitlint-github-action@v6
        with:
          configFile: .commitlintrc.json
```

---

## 三、PR 模板与 Claude 自动化生成

### 1. PR 模板设计（放在 `.github/pull_request_template.md`）

```markdown
## 关联 Issue
Fixes #(issue number)

## 变更类型
- [ ] Bug fix
- [ ] New feature
- [ ] Breaking change
- [ ] Refactor

## 变更描述
<!-- Claude 自动生成：基于 commit history 摘要 -->

## 检查清单
- [ ] 代码通过本地测试
- [ ] 文档已更新
- [ ] CHANGELOG 已更新
- [ ] 关联 Issue 已验证

## 测试验证
<!-- Claude 自动填充：运行了哪些测试命令及结果 -->
```

### 2. 让 Claude 自动填充 PR 描述
在 Claude Code 中自定义命令：

```bash
# 创建 .claude/commands/create-pr.md
"Based on the current branch commits and diff, generate a PR description 
 following the template in .github/pull_request_template.md. 
 Include: summary of changes, linked issues, test results, and breaking changes."
```

---

## 四、GitHub Actions 与 Claude 本地验证对齐

### 1. 本地预提交钩子（与 CI 一致）
配置 `pre-commit` 让 Claude 在推送前执行与 GitHub Actions 相同的检查：

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.5.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
  - repo: local
    hooks:
      - id: pytest
        name: Run tests
        entry: pytest
        language: system
        types: [python]
      - id: mypy
        name: Type check
        entry: mypy
        language: system
        types: [python]
```

### 2. Claude 本地运行检查
指示 Claude 在提交前执行：

```bash
# Claude 自动执行序列
pre-commit run --all-files    # 代码格式/基础检查
pytest                        # 单元测试
mypy src/                     # 类型检查
```

---

## 五、Projects 看板自动化

### 1. GitHub Projects 自动流转规则
配置 Workflow 让 PR 状态自动同步到看板：

| 触发条件            | 自动操作                                 |
| ------------------- | ---------------------------------------- |
| PR 创建且关联 Issue | Issue 状态 → "In Review"                 |
| PR 被请求审查       | 添加到 Reviewer 的 "Pending Review" 视图 |
| PR 合并             | Issue 自动关闭，移动到 "Done"            |
| PR 有变更请求       | Issue 移回 "In Progress"                 |

### 2. Claude 与 Projects 字段同步
让 Claude 在创建 PR 时自动设置 Projects 字段：

```bash
# 通过 GitHub CLI 在 Claude 中执行
gh pr create \
  --title "feat(auth): OAuth2 login (#42)" \
  --body-file .github/pr-body.md \
  --project "Sprint-2026-05" \
  --label "enhancement,area/auth" \
  --milestone "v2.1.0"
```

---

## 六、完整工作流示例

```
1. 需求阶段
   GitHub Issue #42 创建（带模板）→ 自动进入 Projects Backlog
   
2. 开发阶段（Claude Code）
   git checkout -b feature/42-oauth-login
   # Claude 编码实现...
   git commit -m "feat(auth): add OAuth2 flow
   
   Implements PKCE for secure token exchange.
   Fixes #42"
   
3. 本地验证（Claude 自动执行）
   pre-commit run --all-files
   pytest tests/auth/
   
4. PR 创建（Claude 自动生成）
   gh pr create --fill  # 自动使用 commit message 填充
   
5. GitHub 自动化
   → Actions 运行测试/检查
   → Projects 状态自动更新为 "In Review"
   → 关联 Issue 显示 PR 链接
   
6. 审查合并
   审查通过 → 合并 → Issue #42 自动关闭
   → Projects 移动到 "Done"
   → Milestone 进度更新
```

---

## 七、针对你环境的特别建议

| 你的场景             | 建议方案                                                     |
| -------------------- | ------------------------------------------------------------ |
| **MacBook 主力开发** | 在 Claude Code 配置中启用 `git.signing` GPG 签名，确保提交可信 |
| **Ubuntu mini 主机** | 配置 `self-hosted runner` 运行重负载 CI（如模型测试），与 GitHub Actions 混合使用 |
| **多 Agent 并发**    | 用分支隔离不同 Agent 的工作，通过 PR 进行代码审查和合并，避免直接冲突 |
| **cc-switch 项目**   | 在 PR 模板中增加 "模型兼容性检查" 清单项，Claude 自动验证 Qwen/Claude 双端行为 |

---

**核心原则**：让 Claude 成为 **"遵守团队规范的开发者"** 而非自由编码——通过模板、钩子和自动化规则约束其行为，使其输出天然符合 GitHub 管理流程。