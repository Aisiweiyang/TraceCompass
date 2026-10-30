# 架构

TraceCompass 使用 Python 3.11+ 标准库：`sqlite3` 存储、`tomllib` 配置、
`argparse` 命令行、`urllib` 可选模型接口、`http.server` 本地网页。
运行时没有 FastAPI、Jinja2、NumPy 或向量数据库依赖。

```text
JSON / JSONL → ingestion → SQLite conversation / message
                               ↓
run topics: chunk → TF-IDF 或 embedding → leader clustering → 命名 → 证据
run entities: user messages → 目标 / 项目 / 问题规则 → 证据
run interests: topics × 时间活动 → 分量 / 生命周期 → revision / snapshot
                               ↓
                   CLI 检查 + 本地网页与人工动作
```

`domain/` 定义会话消息；`store/` 管理连接、迁移和不变量；`pipeline/` 管理运行记录与切块；
`inference/` 实现主题、兴趣和实体；`scoring/` 是评分纯函数；`llm/` 提供向量与命名接口；
`web/` 渲染页面并处理人工动作。

每个 CLI 阶段记录 `model_run`、配置快照、时间、状态和统计。聚合快照在 `run interests`
时写入，因此完整流程先运行 `entities`。没有自动调度、断点续跑或后台自学习机制。

数据库位置相对配置文件解析，原始导入文件不修改。模型 API 是可选依赖路径，默认关闭。
所有个人输入、标签和派生输出留在 Git 忽略的本地目录，演示每次新建隔离目录。
