# TraceCompass · 知迹罗盘

从历史对话中整理主题、兴趣、目标、项目和重复问题，并把每个推断连回原文证据。
在本机运行，使用 SQLite 存储；默认无需 API、账户或第三方运行时依赖。

**作者：[Aisiweiyang](https://github.com/Aisiweiyang)** · [MIT 许可证](LICENSE)

这是 V0.1 研究原型。真实聊天导出的完整适配、兴趣推断准确率和长期使用效果尚未验证。
评分与置信度是启发式指标，不代表正确概率，也不构成人格判断。

## 能做什么

- 导入 ChatGPT JSON 导出及通用 JSONL，检查重复文件并增量写入消息。
- 用本地字符 TF-IDF 或可选 embedding 发现主题，记录方法、模型与原文证据。
- 查看兴趣评分的显式分量、生命周期、周度活动和状态变化。
- 用规则识别目标、项目与近逐字重复问题。
- 在本地网页修改标签、合并主题、隐藏或恢复兴趣、确认实体，并记录人工动作。

人工标签仅在主题成员签名不变时保留；成员变化可能产生新主题。
当前没有通用撤销功能、Markdown 导入、自动 Meta Interest 生成或真实数据评测命令。

## 三步演示

需要 Python **3.11+**。运行时仅使用标准库，演示可直接运行：

```bash
git clone https://github.com/Aisiweiyang/TraceCompass.git
cd TraceCompass
python scripts/demo.py --serve
```

打开 <http://127.0.0.1:8000>。脚本每次生成一个独立的合成演示目录和数据库，
不会导入你的聊天记录或覆盖已有库。结束时按 Ctrl+C。
只运行流程、不启动网页：`python scripts/demo.py`；换端口：`--serve --port 8010`。

演示内容和日期固定，但生命周期及新近度使用运行时的当前日期，分数会随日期变化。

## 分析自己的数据

在仓库根目录安装。Windows PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m psios init
.venv\Scripts\python.exe -m psios import path/to/conversations.json
.venv\Scripts\python.exe -m psios run topics
.venv\Scripts\python.exe -m psios run entities
.venv\Scripts\python.exe -m psios run interests
.venv\Scripts\python.exe -m psios serve
```

Linux/macOS 使用 `.venv/bin/python`。安装后也可在激活的环境中使用 `tracecompass` 命令；
历史 `psios` 命令与 Python 模块保留兼容。配置默认是当前目录的 `config.toml`，
库路径相对于配置文件；从其他目录调用时显式传 `--config`。

`run interests` 会生成聚合快照，因此演示先运行 `entities`。
每次增加数据后重新运行三个阶段。输入格式、独立数据库与检查命令见[使用说明](docs/USAGE.md)。

## 数据与 API

默认 `config.toml` 的 embedding 和 llm 均为 `none`，不会调用模型 API。
手动启用 API 后，文本块或代表性原文会发送到你配置的服务商；应先用合成数据验证，
再自行决定是否发送私人内容。密钥只从环境变量读取，项目不会自动加载 `.env`。

数据库、原始导出、人工标注、日志、运行报告、环境和 Word/PDF 手册均不在公开仓库。
数据库中的消息、证据引用、人工动作和快照也属于个人数据。
网页仅监听 `127.0.0.1`，没有登录和多用户隔离，请按本机个人工具使用。

## 开发与验证

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check src tests scripts
python -m mypy
python scripts/check_public_tree.py
```

CI 检查 Python 3.11 和 3.13。实际已执行的本机检查见[验收记录](docs/VALIDATION.md)。
测试使用自建合成数据及注入式 API 替身，不代表真实数据效果或真实模型 API 已验证。

## 文档与目录

`src/psios/` 是核心实现，`tests/` 是回归用例，`scripts/` 是演示和发布检查。

[使用说明](docs/USAGE.md) · [架构](docs/architecture.md) ·
[数据模型](docs/data-model.md) · [推断与局限](docs/inference-design.md) ·
[评测计划](docs/evaluation.md) · [贡献](CONTRIBUTING.md) · [安全说明](SECURITY.md)

源码与自建用例采用 [MIT License](LICENSE)。用户导入的数据不因使用本项目而被重新授权。
