from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .http import open_url
from .themes import candidate_for_ai


class OpenAIThemeExplainer:
    default_base_url = "https://api.openai.com/v1"

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = default_base_url,
        timeout: float = 90.0,
    ):
        self.api_key = api_key.strip()
        self.model = model
        self.base_url = (base_url or self.default_base_url).strip().rstrip("/")
        self.endpoint = _responses_endpoint(self.base_url)
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def explain(
        self,
        theme: dict[str, Any],
        other_theme_names: list[str],
        existing_catalysts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("OpenAI API is not configured")
        immutable_candidate = candidate_for_ai(theme)
        prompt = f"""
你是A股题材解释引擎，不是选股模型。市场已经通过确定性规则选出了股票成员。
禁止增加、删除、替换或决定任何股票成员，也不得输出交易建议。

候选题材：
{json.dumps(immutable_candidate, ensure_ascii=False)}

系统中其他题材名称：
{json.dumps(other_theme_names, ensure_ascii=False)}

已经收录的催化（不得重复）：
{json.dumps(existing_catalysts or [], ensure_ascii=False)}

请持续搜索最近72小时、特别是最近24小时内能够解释这些股票共同异动的公开信息，
同时覆盖海外隔夜事件。优先监管机构、政府、交易所、上市公司公告和权威媒体。
区分明确证据、合理推断和未找到证据。新闻只是解释和验证市场行为。

只返回一个JSON对象，不要Markdown：
{{
  "suggested_name": "最小共同炒作逻辑名称",
  "explanation": "为何共同异动，明确标注证据或推断",
  "catalyst_summary": "催化摘要或未找到明确催化",
  "catalyst_duration": "一次性/数日/数周/数月/未知",
  "merge_suggestions": ["仅给题材名称建议，不改变成员"],
  "catalysts": [
    {{
      "title": "新闻或公告标题",
      "summary": "它如何催化该题材，明确事实或推断",
      "source": "来源名称",
      "url": "原始来源URL",
      "published_at": "带时区的ISO时间；无法确认则为空",
      "catalyst_type": "首次催化/强化催化/降温证伪/背景",
      "evidence_level": "明确证据/合理推断"
    }}
  ]
}}
""".strip()
        payload = {
            "model": self.model,
            "reasoning": {"effort": "low"},
            "tools": [{"type": "web_search", "search_context_size": "low"}],
            "tool_choice": "required",
            "include": ["web_search_call.action.sources"],
            "input": prompt,
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with open_url(request, timeout=self.timeout) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI HTTP {error.code}: {body[:500]}") from error
        text = _output_text(raw)
        parsed = _parse_json_object(text)
        sources = _sources(raw)
        catalysts = _normalize_catalysts(parsed.get("catalysts"), sources)
        if not catalysts and sources:
            summary = str(parsed.get("catalyst_summary") or "公开信息可能与题材异动相关")
            catalysts = [
                {
                    "title": source["title"],
                    "summary": summary,
                    "source": source["title"],
                    "url": source["url"],
                    "published_at": None,
                    "catalyst_type": "背景",
                    "evidence_level": "合理推断",
                }
                for source in sources[:5]
            ]
        return {
            "model": self.model,
            "suggested_name": str(parsed.get("suggested_name") or theme["provisional_name"]),
            "explanation": str(parsed.get("explanation") or "未生成解释"),
            "catalyst_summary": str(parsed.get("catalyst_summary") or "未找到明确催化"),
            "catalyst_duration": str(parsed.get("catalyst_duration") or "未知"),
            "merge_suggestions": list(parsed.get("merge_suggestions") or []),
            "sources": sources,
            "catalysts": catalysts,
            "raw": raw,
        }


def _output_text(response: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                parts.append(str(content["text"]))
    return "\n".join(parts)


def _responses_endpoint(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("OPENAI_BASE_URL must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError("OPENAI_BASE_URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("OPENAI_BASE_URL must not contain a query string or fragment")
    if parsed.path.rstrip("/").endswith("/responses"):
        return base_url.rstrip("/")
    return f"{base_url.rstrip('/')}/responses"


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return {"explanation": cleaned or "模型未返回内容"}
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return {"explanation": cleaned}


def _sources(response: dict[str, Any]) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for item in response.get("output", []):
        action = item.get("action") or {}
        for source in action.get("sources") or []:
            url = str(source.get("url") or "")
            if url:
                found[url] = {"url": url, "title": str(source.get("title") or url)}
        for content in item.get("content", []):
            for annotation in content.get("annotations") or []:
                url = str(annotation.get("url") or "")
                if url:
                    found[url] = {"url": url, "title": str(annotation.get("title") or url)}
    return list(found.values())


def _normalize_catalysts(
    value: Any, sources: list[dict[str, str]]
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    known_urls = {source["url"] for source in sources}
    result = []
    for raw in value[:8]:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        summary = str(raw.get("summary") or "").strip()
        url = str(raw.get("url") or "").strip()
        if not title or not summary:
            continue
        if url and known_urls and url not in known_urls:
            # Do not persist URLs that were not actually returned by web search.
            url = ""
        result.append(
            {
                "title": title,
                "summary": summary,
                "source": str(raw.get("source") or "").strip(),
                "url": url,
                "published_at": str(raw.get("published_at") or "").strip() or None,
                "catalyst_type": str(raw.get("catalyst_type") or "背景").strip(),
                "evidence_level": str(
                    raw.get("evidence_level") or "合理推断"
                ).strip(),
            }
        )
    return result
