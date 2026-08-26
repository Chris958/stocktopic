from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from .themes import candidate_for_ai


class OpenAIThemeExplainer:
    endpoint = "https://api.openai.com/v1/responses"

    def __init__(self, api_key: str, model: str, timeout: float = 90.0):
        self.api_key = api_key.strip()
        self.model = model
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def explain(self, theme: dict[str, Any], other_theme_names: list[str]) -> dict[str, Any]:
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

请搜索最近72小时内能够解释这些股票共同异动的公开信息，优先监管机构、政府、交易所、
上市公司公告和权威媒体。区分明确证据、合理推断和未找到证据。新闻只是解释和验证市场行为。

只返回一个JSON对象，不要Markdown：
{{
  "suggested_name": "最小共同炒作逻辑名称",
  "explanation": "为何共同异动，明确标注证据或推断",
  "catalyst_summary": "催化摘要或未找到明确催化",
  "catalyst_duration": "一次性/数日/数周/数月/未知",
  "merge_suggestions": ["仅给题材名称建议，不改变成员"]
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
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI HTTP {error.code}: {body[:500]}") from error
        text = _output_text(raw)
        parsed = _parse_json_object(text)
        return {
            "model": self.model,
            "suggested_name": str(parsed.get("suggested_name") or theme["provisional_name"]),
            "explanation": str(parsed.get("explanation") or "未生成解释"),
            "catalyst_summary": str(parsed.get("catalyst_summary") or "未找到明确催化"),
            "catalyst_duration": str(parsed.get("catalyst_duration") or "未知"),
            "merge_suggestions": list(parsed.get("merge_suggestions") or []),
            "sources": _sources(raw),
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

