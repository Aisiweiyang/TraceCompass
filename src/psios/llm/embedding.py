"""Embedding 客户端:API(urllib,OpenAI 兼容)或 none(TF-IDF 降级)。

- API key / base_url 只从环境变量读取:
  `PSIOS_EMBEDDING_API_KEY` → `OPENAI_API_KEY`,
  `PSIOS_EMBEDDING_BASE_URL` → `OPENAI_BASE_URL` → `https://api.openai.com/v1`。
- 批量请求;失败重试 1 次;响应与输入按下标严格对齐,错位即报错(宁失败不脏写)。
- transport 可注入 → 单测无需真实网络。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable

Transport = Callable[[str, dict[str, object], dict[str, str]], dict]
"""POST JSON → 返回解析后的 JSON dict;失败抛异常。由测试注入替身。"""

_DEFAULT_TIMEOUT = 30


def _http_post_json(url: str, payload: dict[str, object], headers: dict[str, str]) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_DEFAULT_TIMEOUT) as response:
        body = response.read()
    return json.loads(body.decode("utf-8"))


class EmbeddingError(RuntimeError):
    """embedding API 调用失败(已重试)。"""


class EmbeddingClient(ABC):
    model: str

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """返回与 texts 等长且按位对齐的向量;None = 本客户端不可用(降级);失败抛 EmbeddingError。"""


class NoneEmbedding(EmbeddingClient):
    """占位客户端:调用方收到 None 触发 TF-IDF 降级路径。"""

    model = "none"

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        return None


class ApiEmbedding(EmbeddingClient):
    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        batch_size: int = 64,
        transport: Transport | None = None,
    ) -> None:
        self.model = model
        self._base_url = (
            base_url
            or os.environ.get("PSIOS_EMBEDDING_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self._api_key = api_key or os.environ.get("PSIOS_EMBEDDING_API_KEY") \
            or os.environ.get("OPENAI_API_KEY") or ""
        self._batch_size = max(1, batch_size)
        self._post: Transport = transport or _http_post_json

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] | None = None
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start:start + self._batch_size]
            batch_vectors = self._embed_batch(batch)
            if vectors is None:
                vectors = []
            vectors.extend(batch_vectors)
        return vectors if vectors is not None else []

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        payload: dict[str, object] = {"model": self.model, "input": batch}
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        last_error: Exception | None = None
        for _attempt in range(2):  # 失败重试 1 次
            try:
                response = self._post(f"{self._base_url}/embeddings", payload, headers)
                data = response.get("data")
                if not isinstance(data, list) or len(data) != len(batch):
                    raise EmbeddingError(
                        f"embedding 响应错位: 期望 {len(batch)} 条, 收到 "
                        f"{len(data) if isinstance(data, list) else type(data).__name__}"
                    )
                vectors: list[list[float]] = []
                for item in data:
                    embedding = item.get("embedding") if isinstance(item, dict) else None
                    if not isinstance(embedding, list):
                        raise EmbeddingError("embedding 响应缺向量字段")
                    vectors.append([float(x) for x in embedding])
                return vectors
            except (EmbeddingError, urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
        raise EmbeddingError(f"embedding 调用失败(已重试): {last_error}") from last_error


def make_embedding_client(model_name: str) -> tuple[EmbeddingClient, list[str]]:
    """按 config [models].embedding 构建客户端;配置了 API 但缺密钥时降级并告警。"""
    warnings: list[str] = []
    if not model_name or model_name == "none":
        return NoneEmbedding(), warnings
    api_key = os.environ.get("PSIOS_EMBEDDING_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        warnings.append(
            f"[models].embedding={model_name!r} 但未设置 PSIOS_EMBEDDING_API_KEY/"
            "OPENAI_API_KEY, 降级为 TF-IDF 本地向量"
        )
        return NoneEmbedding(), warnings
    return ApiEmbedding(model=model_name), warnings
