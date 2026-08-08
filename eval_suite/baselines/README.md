# Immutable Baselines

每个固定批次使用独立目录：

```text
<source_batch>/
  manifest.json
  results.jsonl
  summary.json
```

`manifest.json` 中的 `results_sha256`、schema version、task catalog hash、suite 和
source batch 用于防止跨批次或跨 schema 直接合并。
