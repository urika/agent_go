# M0-10 结果和批次治理

固定批次目录：

```text
eval_suite/
  baselines/<source_batch>/
    manifest.json
    results.jsonl
    summary.json
  exploratory/
```

使用 `agent_go.batch_governance.archive_baseline()` 归档结果，源文件不会被删除或改写。`manifest.json` 固定记录 `source_batch`、schema version、结果 SHA-256、task catalog hash、config hash、suite、models 和 repeat。

`validate_mergeable_batches()` 拒绝不同 `bench_schema_version` 或不同 `source_batch` 的直接合并。历史 `results_v1/v2/v3/v4` 保留原位置，并由 `eval_suite/exploratory/manifest.json` 标记为 exploratory，不作为当前产品 KPI 基线。

单独生成 manifest：

```bash
agent_go eval batch-manifest \
  --results eval_suite/baselines/<source_batch>/results.jsonl \
  --source-batch <source_batch> \
  --manifest-output eval_suite/baselines/<source_batch>/manifest.json
```
