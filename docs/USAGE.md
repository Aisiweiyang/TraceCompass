# 使用说明

## 导入

ChatGPT adapter 读取 `conversations.json` 列表及消息 mapping，按父链和时间顺序解析文本。
实际导出格式可能变化；目前只做了合成结构测试，先导入少量记录并抽查原文与顺序。
附件和多模态内容不是完整支持范围。

通用 JSONL：每行一个消息对象，一个文件对应一个会话。

```json
{"role":"user","content":"我计划整理读书笔记。","ts":"2026-06-01T08:00:00+00:00"}
```

`role` 可为 `user`、`assistant`、`system`、`other`；时间可为 ISO 8601 或 epoch 秒。
缺失时间会降低时间型推断的信息量。目录导入仅扫描该层的 `.json`、`.jsonl`、`.ndjson`。
原始文件不会被修改。JSONL 的文件名参与会话身份；独立改名或修改文件后应检查去重结果。

```bash
python -m psios import data/raw
python -m psios dump conversations
python -m psios dump messages --conversation CONVERSATION_ID
```

## 独立配置和数据库

每个命令都可指定 `--config config.toml --db path/to/isolated.db`（放在子命令之后）。
`init` 建库和应用迁移；后续命令需要库已存在。
不要让演示与私人研究共用数据库。移动项目后重新安装，不要复用指向旧路径的 editable 环境。

```bash
python -m psios status
python -m psios invariants
python -m psios dump topics
python -m psios dump interests
python -m psios dump goals
python -m psios dump projects
python -m psios dump questions
```

## 网页与人工修正

`serve --port 8000` 显示概览、主题证据、周度演化、实体列表和运行历史。
可改标签、合并主题、隐藏/恢复兴趣、确认实体。
隐藏只影响展示，不删除原文和计算记录；直接打开主题详情仍可查看。
合并没有通用撤销入口；当前主题身份取决于成员签名，新增数据后请复核人工修改。

## 可选模型 API

在配置 `[models]` 中把 `embedding` 或 `llm` 从 `none` 改为服务商支持的模型名。
设置 `PSIOS_EMBEDDING_API_KEY` / `PSIOS_LLM_API_KEY`；
对应 `PSIOS_EMBEDDING_BASE_URL` / `PSIOS_LLM_BASE_URL` 可覆盖服务地址。
也兼容 `OPENAI_API_KEY`、`OPENAI_BASE_URL`。
环境变量名称沿用原项目以保持兼容。模型名和接口需由使用者按服务商实际支持情况确认。

缺少密钥会提示并使用本地降级；主题命名失败会降级为关键词命名。
此接口未做本轮真实服务商调用验收，配置 API 不会启用尚未实现的 Meta Interest 模块。
