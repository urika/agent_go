# 本地模型困难任务 prompts

> 仅收录 2026-08-12 测试中使用的 5 个任务题目，便于复用或扩展基准测试。

---

## 任务 1：AVL 树实现与验证

Implement a complete AVL tree in Python with the following methods:
- insert(key): insert a key and rebalance the tree
- delete(key): delete a key and rebalance the tree
- search(key): return True/False
- inorder(): return list of keys in sorted order
- height(): return tree height
- is_balanced(): return True if every node has |balance_factor| <= 1

Your code must be self-contained (no external libraries) and handle duplicates by ignoring them. Provide ONLY the Python code inside a single markdown code block.

---

## 任务 2：多约束调度逻辑推理

Solve this logic puzzle and give the final daily schedule only.

Five people (Alice, Bob, Carol, Dave, Eve) each choose one day from Mon-Fri to present, with no two people on the same day. Clues:
1. Alice presents the day before Bob.
2. Carol presents on Wednesday.
3. Dave presents the day after Eve.
4. Bob does not present on Friday.
5. Eve does not present on Monday.

Provide the final answer as a JSON object mapping day -> person, e.g. {"Mon":"..."}. No explanation needed.

---

## 任务 3：并发代码缺陷诊断与修复

This Python program has a concurrency bug. Identify the bug, explain why it causes incorrect results, and provide the corrected code.

```python
import threading

class Counter:
    def __init__(self):
        self.value = 0
        self.lock = threading.Lock()

    def increment(self):
        # BUG: lock is not actually held during the read-modify-write
        with self.lock:
            pass
        current = self.value
        self.value = current + 1

    def get(self):
        return self.value

def race_test():
    c = Counter()
    threads = [threading.Thread(target=lambda: [c.increment() for _ in range(1000)]) for _ in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()
    return c.get()
```

---

## 任务 4：复杂结构化 JSON 生成

Generate a JSON object representing a software project plan with exactly these constraints:
- top-level keys: project (string), version (semantic version string), tasks (list of exactly 3 tasks)
- each task has: id (integer, unique and sequential 1-3), title (string), priority (one of low/medium/high), subtasks (list of 2 strings), done (boolean)
- exactly one task has priority 'high' and that task's done must be false
- the first task's title must contain the word 'design'

Output ONLY valid JSON, no markdown, no explanation.

---

## 任务 5：长上下文信息提取与聚合

> 提示：实际使用时需向模型提供 30 个项目段落（每个段落包含项目名称、状态、预算、负责人、模块等信息，并打乱顺序）。

Based ONLY on the document above, answer with a JSON object:
{"in_progress_count": <int>, "total_budget_completed": <int>, "leads_with_budget_over_200k": [strings]}

No explanation. Output valid JSON only.
