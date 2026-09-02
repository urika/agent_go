| 中文术语 | English | 一句话解释 |
|------|---------|------|
| 角色-模型配置矩阵 | Role-Model Configuration Matrix | 回答"哪个角色用哪个模型、走哪个后端、优先级怎么排"的配置总表。 |
| 混合模式 | Hybrid Mode | Planner 与 Evaluator 用云端强模型、Worker 用本地模型的成本与模型组合设计。 |
| 降档链 | Degrade Fallback | 当 `budget_mode=degrade` 时按 `worker_models_degrades` 下移难度以节省成本。 |
| 本地模型判定 | Local Model Detection | 通过探测响应 `message.model` 与 `/status` 声明比对来判断后端是否为本地。 |
| 横切覆盖 | Cross-cutting Override | 由 router 提供、优先级最高的角色级 provider/model/base_url 覆盖机制。 |
| 配置冲突点 | Configuration Conflict Points | 文档中列出的当前配置存在的风险点及相应优化建议。 |