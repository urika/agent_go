# agent_go 代码审查清单（Code Review Checklist）

> 来源：AGENTS.md 拆分（2026-09-02）。审查 Python 代码时检查以下高风险模式。

## Loop body truncation (critical)

**Anti-pattern**: After a `for x in collection:` loop, code references `x` (the loop variable) to do state mutations — this means the mutations are outside the loop, acting only on the last element.

```python
# ❌ BUG: mutations after the loop — only processes last item
for st in wave:
    fut = executor.submit(run_subtask, ...)
    futures[fut] = st
for fut in as_completed(futures):    # ← same level as for st above
    st = futures[fut]
    try:
        result = fut.result()
    except Exception:
        ...
with meta_lock:                      # ← OUTSIDE the as_completed loop!
    results_map[st["id"]] = result   # ← only saves last st
completed_ids.add(st["id"])          # ← only adds last st

# ✅ CORRECT: all per-item processing inside the loop
for st in wave:
    fut = executor.submit(run_subtask, ...)
    futures[fut] = st
for fut in as_completed(futures):
    st = futures[fut]
    try:
        result = fut.result()
    except Exception:
        ...
    with meta_lock:                  # ← INSIDE the loop
        results_map[st["id"]] = result
    completed_ids.add(st["id"])
```

**Detection rule**: When a `for` loop processes a collection and the lines immediately after the loop reference the loop variable (`st`, `item`, `fut`, etc.), verify the indentation: these should likely be inside the loop.

## Other review patterns

- **Thread safety**: Shared mutable state (`results_map`, `completed_ids`, counters) modified across threads must be protected by a lock.
- **Mock side_effect exhaustion**: When mocking a function with `side_effect=[...]`, ensure the number of expected calls doesn't exceed the list length — otherwise `StopIteration` crashes the test.
- **Subprocess pipe deadlock**: Always use `capture_output=True` or `stdin=PIPE` with `communicate()`, never `wait()` on a pipe that fills the buffer.
- **Temp directory leak**: Tests that create directories/files should use `tmp_path` fixture (auto-cleanup) or clean up in a `finally` block.
- **Codegen/config drift**: `config.example.json` must stay in sync with `DEFAULT_CONFIG` in `config.py`; pricing table additions must update `pricing.py`. `spec validate` gates non-trivial tasks — `--force` bypasses it and should not be the default path.
