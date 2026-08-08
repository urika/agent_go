# ADR-002: 分离 Commit、Verification 和 Delivery 完成边界

## 状态

Accepted

## 决策

系统使用三个独立边界：

```text
commit       = 代码已保存
verification = 代码满足验证要求
delivery     = 用户可以取得并合并代码
```

## 原因

commit 成功不代表代码正确，验证通过也不代表 PR 交付成功。三者混为一谈会导致未经验证或无法取得的代码被报告为成功。

## 约束

- `COMMITTED_UNVERIFIED` 不得进入下游。
- `ACCEPTED_DELIVERY` 必须同时满足验证和交付条件。
- commit hash、delivery branch、target branch 和 pr_url 必须可追溯。

## 实现

`executor.py`、`pipeline.py`、`recover.py`、`delivery.py`、`bench.py`。
