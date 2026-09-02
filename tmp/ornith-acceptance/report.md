# Ornith-1.5-9B 文档处理验收报告

- 任务数: 14，一次性通过: 5（36%）
- 模型: ornith-ai/Ornith-1.5-9B-MLX-4bit（MLX 4bit, thinking off, temp 0.6）

## 翻译
- ❌ **A1-translate-verification**（9.6s, 470 tok） — 代码块1被改动

- ✅ **A2-translate-failure-class**（10.9s, 537 tok）

- ❌ **A3-translate-func-arch**（16.9s, 828 tok） — 代码块1被改动；代码块2被改动

- ❌ **A4-translate-greywall**（33.2s, 1635 tok） — 中文残留 34.8% > 5%

## 摘要
- ❌ **B1-summary-trust**（7.4s, 306 tok） — 字数 432 > 上限 200

- ❌ **B2-summary-kanban**（5.0s, 177 tok） — 字数 296 > 上限 200

- ❌ **B3-summary-blindspot**（10.4s, 425 tok） — 字数 699 > 上限 300

## 术语表
- ✅ **C1-glossary-state**（5.2s, 204 tok）

- ❌ **C2-glossary-role-matrix**（6.6s, 220 tok） — 术语不在原文: 降档链；术语不在原文: 本地模型判定

## 索引维护
- ✅ **D1-index-entry**（2.8s, 41 tok）

- ✅ **D2-cross-ref**（4.7s, 123 tok）

## 改写
- ❌ **E1-checklist-gates**（5.5s, 269 tok） — 字数 473 > 上限 400

- ✅ **E2-rewrite-issues**（5.6s, 274 tok）

- ❌ **E3-rewrite-metric-freeze**（8.4s, 407 tok） — 数字丢失: ['08', '2026']；字数 948 > 上限 250
