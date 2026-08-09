# 任务拆分设计基准 Prompt（Split Design Benchmark）

用于让不同 Agent（Claude Code / OpenCode）对同一任务产出**结构化拆分设计**，
供 agent_go 拆分算法参考。要求只输出 JSON，不做实际代码修改。

## Prompt 模板

```
你是资深软件架构师。请针对下面的开发任务，设计一个子任务拆分方案。

背景：这些子任务将分别派发给独立的 Agent 实例执行，每个 Agent 有独立的
工作目录（git worktree），子任务之间通过 git merge 传递产物。
因此拆分设计必须遵守以下原则：

【拆分原则】
1. 子任务数量尽量少：改动面 ≤2 个文件时优先 1 个任务；只有改动真正跨模块
   （不同文件集、可独立验证）时才拆分。
2. 文件互斥：不同子任务不要修改同一文件（避免合并冲突和交叉污染）。
   如果确实需要修改同一文件，必须通过依赖关系（depends_on）串行执行。
3. 每个子任务给出：files（它负责的文件，路径相对于仓库根）、
   depends_on（依赖的子任务 id）、difficulty（easy/medium/hard）、
   verification（验证命令）、rationale（为什么这样拆/为什么这里合并）。
4. 拆分数量范围：小型改动 1 个；中型改动 1-2 个；大型跨模块改动 2-4 个。
   禁止为小改动制造不必要的拆分。

【任务信息】
- 仓库: {repo}
- 难度: {difficulty}
- 任务描述:
{task}

请只输出一个 JSON 对象（不要输出任何其他文字或代码块标记），格式如下：
{{
  "subtasks": [
    {{
      "id": "sub-1",
      "title": "...",
      "files": ["file1.py", "file2.py"],
      "depends_on": [],
      "difficulty": "easy",
      "verification": "验证命令（如 pytest ...）",
      "rationale": "为什么拆/合并这一步"
    }}
  ],
  "overall_rationale": "整体拆分策略说明，说明为什么拆 N 个而不是更多/更少"
}}
```

## 使用说明

- `{repo}` 替换为 fixture 仓库路径（绝对路径，agent 可读取文件了解结构）
- `{difficulty}` 替换为任务难度
- `{task}` 替换为任务描述正文
- 评估时解析 JSON 的 `subtasks[]`，提取数量/文件/依赖/rationale
