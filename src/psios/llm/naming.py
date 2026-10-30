"""LLM 主题命名:OpenAI 兼容 chat completions + 严格 JSON schema 校验。

任何失败(网络/超时/非 JSON/缺字段/类型错)一律返回 None,由调用方降级为
关键词命名 —— 命名失败不允许拖垮 pipeline(inference-design §1.3)。
密钥环境变量:`PSIOS_LLM_API_KEY` → `OPENAI_API_KEY`;
base_url:`PSIOS_LLM_BASE_URL` → `OPENAI_BASE_URL`。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

Transport = Callable[[str, dict[str, object], dict[str, str]], dict]

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_DEFAULT_TIMEOUT = 30

_PROMPT_TEMPLATE = """你是个人知识系统的主题命名助手。以下是一个主题簇的代表文本片段与高频词项。
只输出一个 JSON 对象,不要输出任何其他文字,格式:
{{"label": "≤20字的主题名", "summary": "一句话摘要",
  "keywords": ["关键词", ...], "confidence": 0.0到1.0}}
禁止推测用户人格;label 必须概括主题本身。

代表片段:
{representatives}

高频词项:{keywords}
"""


@dataclass
class TopicNaming:
    label: str
    summary: str | None
    keywords: list[str]
    confidence: float


def _extract_json(text: str) -> dict | None:
    match = _FENCE.search(text)
    candidate = match.group(1) if match else text
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def parse_naming(payload: dict | None) -> TopicNaming | None:
    """schema 校验:字段缺失/类型错/越界一律 None。"""
    if payload is None:
        return None
    label = payload.get("label")
    if not isinstance(label, str) or not label.strip():
        return None
    label = " ".join(label.split())[:80]
    summary = payload.get("summary")
    if summary is not None and not isinstance(summary, str):
        summary = None
    keywords_raw = payload.get("keywords")
    if not isinstance(keywords_raw, list):
        return None
    keywords = [str(k) for k in keywords_raw if isinstance(k, (str, int, float)) and str(k).strip()]
    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return None
    confidence = max(0.0, min(1.0, float(confidence)))
    return TopicNaming(label=label, summary=summary, keywords=keywords, confidence=confidence)


class KeywordNaming:
    """降级命名:top 关键词拼接;诚实标注低质量(中文无分词,见 design 局限)。"""

    model = "keyword-rule"

    def propose(self, representatives: list[str], keywords: list[str]) -> TopicNaming | None:
        if not keywords:
            return None
        label = "/".join(keywords[:3])[:60]
        return TopicNaming(
            label=label,
            summary=None,
            keywords=keywords[:6],
            confidence=min(0.6, 0.3 + 0.05 * len(representatives)),
        )


class LlmNaming:
    model: str

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.model = model
        self._base_url = (
            base_url
            or os.environ.get("PSIOS_LLM_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self._api_key = api_key or os.environ.get("PSIOS_LLM_API_KEY") \
            or os.environ.get("OPENAI_API_KEY") or ""
        self._post: Transport = transport or _post_json

    def propose(self, representatives: list[str], keywords: list[str]) -> TopicNaming | None:
        prompt = _PROMPT_TEMPLATE.format(
            representatives="\n".join(f"- {r[:200]}" for r in representatives[:5]),
            keywords="、".join(keywords[:8]),
        )
        try:
            response = self._post(
                f"{self._base_url}/chat/completions",
                {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                },
                {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {},
            )
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return None
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            return None
        return parse_naming(_extract_json(content))


def _post_json(url: str, payload: dict[str, object], headers: dict[str, str]) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_DEFAULT_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def make_naming_client(model_name: str) -> tuple[LlmNaming | None, list[str]]:
    """按 config [models].llm 构建;为 none 或缺密钥时返回 None(走关键词降级)。"""
    warnings: list[str] = []
    if not model_name or model_name == "none":
        return None, warnings
    api_key = os.environ.get("PSIOS_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        warnings.append(
            f"[models].llm={model_name!r} 但未设置 PSIOS_LLM_API_KEY/OPENAI_API_KEY,"
            " 主题命名降级为关键词拼接"
        )
        return None, warnings
    return LlmNaming(model=model_name), warnings
