"""PSI OS 的模型通道(embedding / LLM 命名):可插拔、可降级、默认全离线。

原则(inference-design §6):LLM 是推断组件之一,不是依赖;`config.toml [models]`
里为 "none"(默认)时全链路走纯本地路径。API 密钥只从环境变量读取,不入库不入配置文件。
"""

from psios.llm.embedding import ApiEmbedding, EmbeddingClient, NoneEmbedding, make_embedding_client
from psios.llm.naming import KeywordNaming, LlmNaming, TopicNaming, make_naming_client
from psios.llm.tfidf import build_tfidf, top_terms

__all__ = [
    "ApiEmbedding",
    "EmbeddingClient",
    "KeywordNaming",
    "LlmNaming",
    "NoneEmbedding",
    "TopicNaming",
    "build_tfidf",
    "make_embedding_client",
    "make_naming_client",
    "top_terms",
]
