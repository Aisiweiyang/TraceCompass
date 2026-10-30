# 存储与数据模型

DDL 以 [`src/psios/store/migrations/`](../src/psios/store/migrations/) 为准。
迁移由 `PRAGMA user_version` 管理；当前版本为 2，每次迁移在事务中执行。

| 分组 | 表及用途 |
|---|---|
| 来源 | `source`、`conversation`、`message`：文件来源、会话身份、原文、角色、时间及内容哈希 |
| 主题 | `chunk`、`topic`、`topic_chunk`：文本块、主题、层级及成员关联 |
| 兴趣 | `interest`：分量、原始统计、总分、类型、生命周期、置信度 |
| 实体 | `goal`、`project`、`question`、`question_occurrence`：候选和问题出现记录 |
| 可追溯 | `evidence`、`entity_revision`、`human_action`：引用、状态变化和人工动作 |
| 运行 | `model_run`、`personal_model_snapshot`：运行身份、配置、统计和聚合快照 |
| 预留 | `meta_interest`：保留表结构，尚无自动生成实现 |

`v_interest_current` 汇总当前可见兴趣，`v_topic_weekly` 汇总周度活动。
历史 `psios` 包名、ID 命名空间和默认 `data/psios.db` 保留，产品改名不改变数据库身份。

不变量检查覆盖证据外键、置信度、时间戳与主题合并引用等有限条件。
通过检查不表示推断正确。revision 由应用按追加方式记录，并非防篡改审计库。

数据库内包含完整原文及证据片段，甚至在隐藏兴趣后也会保留；备份按私人数据管理。
