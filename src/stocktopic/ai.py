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
        raw, parsed, sources = self._call_prompt(prompt, reasoning_effort="low")
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

    def assess_for_admission(
        self,
        theme: dict[str, Any],
        historical_matches: list[dict[str, Any]],
        eligible_stock_pool: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("OpenAI API is not configured")
        trigger_members = [
            {
                "code": member["code"],
                "name": member["name"],
                "evidence": member.get("evidence", {}),
            }
            for member in theme.get("members", [])
            if member.get("active", 1)
        ]
        prompt = f"""
你是A股“新重点题材准入审查器”。目标是排除小异动、旧题材复炒和缺乏持续性的噪声，
不是为了尽可能多地产生题材。必须使用web_search核查最近催化及过去60个交易日的历史发酵。

确定性触发证据（已经满足当日至少4只股票曾触及涨停，炸板计入）：
{json.dumps(trigger_members, ensure_ascii=False)}

候选最小共同逻辑：{json.dumps(theme.get("shared_tag"), ensure_ascii=False)}
系统保存的60交易日历史相似题材：
{json.dumps(historical_matches, ensure_ascii=False)}

允许提议加入的股票白名单（只能从中选择，不能自行创造代码）：
{json.dumps(eligible_stock_pool, ensure_ascii=False)}

“新题材”指新的最小共同炒作逻辑或首次形成广泛资金共识；旧题材出现一条新新闻、换名、
反复轮动，不算新题材。持续性与30%空间必须是有催化路径的情景判断，不得伪装成确定预测。
优先政府、监管、交易所、上市公司公告、产业链原始信息和权威媒体；必须列出反证和风险。
如果市场已形成至少4只触板的广泛共识，但只有机构研报或供应链消息、尚无官方确认，仍可判定
为新题材和具有持续性；证据等级必须如实写为“供应链未确认”，由系统放入早期观察而非正式题材。

只返回一个JSON对象，不要Markdown：
{{
  "suggested_name": "最小共同逻辑名称",
  "is_new_theme": true,
  "novelty_confidence": 0,
  "novelty_reason": "与60交易日历史的区别，或为何属于复炒",
  "catalyst_summary": "当前催化链条",
  "catalyst_confidence": 0,
  "expected_duration_days": 0,
  "duration_reason": "为何可能持续或为何不可持续",
  "leader_candidate_code": "必须来自触发股票或白名单",
  "leader_upside_scenario_pct": 0,
  "upside_scenario_reason": "达到该空间需要满足的条件，不是目标价",
  "counter_evidence": ["反证或证伪条件"],
  "proposed_members": [
    {{"code":"只能来自白名单", "role":"龙头/核心/弹性/跟随", "reason":"同逻辑依据"}}
  ],
  "catalysts": [
    {{
      "title":"标题", "summary":"与题材关系", "source":"来源", "url":"原始URL",
      "published_at":"带时区ISO时间或空", "catalyst_type":"首次催化/强化催化/反证",
      "evidence_level":"官方确认/多源交叉验证/供应链未确认/合理推断",
      "source_kind":"official/company_disclosure/industry_primary/authoritative_media/brokerage_research/supply_chain_report/social"
    }}
  ]
}}
        """.strip()
        raw, parsed, sources = self._call_prompt(prompt, reasoning_effort="medium")
        required = {
            "suggested_name",
            "is_new_theme",
            "novelty_confidence",
            "catalyst_confidence",
            "expected_duration_days",
            "leader_candidate_code",
            "leader_upside_scenario_pct",
        }
        missing = sorted(required - set(parsed))
        if missing:
            raise RuntimeError(
                "AI admission response missing required fields: " + ", ".join(missing)
            )
        return {
            "model": self.model,
            "suggested_name": str(parsed.get("suggested_name") or theme["provisional_name"]),
            "is_new_theme": _boolean(parsed.get("is_new_theme")),
            "novelty_confidence": _bounded_number(parsed.get("novelty_confidence")),
            "novelty_reason": str(parsed.get("novelty_reason") or "未提供新颖性依据"),
            "catalyst_summary": str(parsed.get("catalyst_summary") or "未找到明确催化"),
            "catalyst_confidence": _bounded_number(parsed.get("catalyst_confidence")),
            "expected_duration_days": max(0, _integer(parsed.get("expected_duration_days"))),
            "duration_reason": str(parsed.get("duration_reason") or "未提供持续性依据"),
            "leader_candidate_code": str(parsed.get("leader_candidate_code") or "").strip(),
            "leader_upside_scenario_pct": max(
                0.0, _number(parsed.get("leader_upside_scenario_pct"))
            ),
            "upside_scenario_reason": str(
                parsed.get("upside_scenario_reason") or "未提供空间推演依据"
            ),
            "counter_evidence": [
                str(value) for value in (parsed.get("counter_evidence") or [])[:8]
            ],
            "proposed_members": _normalize_proposed_members(
                parsed.get("proposed_members"), eligible_stock_pool
            ),
            "sources": sources,
            "catalysts": _normalize_catalysts(parsed.get("catalysts"), sources),
            "raw": raw,
        }

    def cluster_limit_events(
        self, trade_date: str, events: list[dict[str, Any]], minimum_members: int = 4
    ) -> list[dict[str, Any]]:
        """Cluster touched-limit stocks by one concrete shared event, not exact tag text."""
        if not self.enabled:
            raise RuntimeError("OpenAI API is not configured")
        compact_events = []
        allowed_codes: set[str] = set()
        for event in events:
            code = str(event.get("code") or "")
            if not code:
                continue
            allowed_codes.add(code)
            compact_events.append(
                {
                    "code": code,
                    "name": event.get("name"),
                    "board_tag": event.get("board_tag"),
                    "status": event.get("status"),
                    "limit_reason": event.get("limit_reason"),
                    "source_themes": event.get("themes", []),
                    "concept_tags": [
                        item.get("tag")
                        for item in event.get("concept_tags", [])[:10]
                        if item.get("tag")
                    ],
                }
            )
        prompt = f"""
你是A股涨停共同事件聚类器。交易日：{trade_date}。
目标是发现“同一个具体催化事件/新增产业逻辑”驱动的股票组合，而不是按标签文字完全相同分组。
必须使用web_search核查当日及隔夜新闻。综合涨停原因、开盘啦标签、题材成分和新闻催化。

硬约束：
1. 只返回至少{minimum_members}只输入股票共同指向同一具体事件的组合，涨停和炸板都计入。
2. 股票代码只能来自输入；不得为了凑数加入仅有宽泛行业关系的股票。
3. “AI、英伟达、化工、国企、并购”等宽泛标签本身不是共同事件，必须下钻到最小炒作逻辑。
4. 不同标签可以合并，例如PTFE、氟化工、PCB材料可因同一Rubin背板选材事件合并；
   但必须逐只说明为什么与该事件有关。
5. 同一组股票不要重复输出近义题材。没有合格组合就返回空数组。

输入股票：
{json.dumps(compact_events, ensure_ascii=False)}

只返回JSON对象：
{{
  "clusters": [
    {{
      "canonical_name": "具体共同事件题材名",
      "common_logic": "事件→产业变化→股票受益的共同链条",
      "member_codes": ["只能来自输入"],
      "aliases": ["输入标签或常用近义名"],
      "cluster_confidence": 0,
      "catalysts": [
        {{
          "title":"标题", "summary":"与共同事件的关系", "source":"来源",
          "url":"web_search实际返回URL", "published_at":"ISO时间或空",
          "catalyst_type":"首次催化/强化催化/反证",
          "evidence_level":"官方确认/多源交叉验证/供应链未确认/合理推断",
          "source_kind":"official/company_disclosure/industry_primary/authoritative_media/brokerage_research/supply_chain_report/social"
        }}
      ]
    }}
  ]
}}
        """.strip()
        raw, parsed, sources = self._call_prompt(prompt, reasoning_effort="medium")
        clusters = parsed.get("clusters")
        if not isinstance(clusters, list):
            raise RuntimeError("AI semantic cluster response missing clusters array")
        result = []
        seen: set[tuple[str, ...]] = set()
        for cluster in clusters[:20]:
            if not isinstance(cluster, dict):
                continue
            codes = list(
                dict.fromkeys(
                    str(code).strip()
                    for code in cluster.get("member_codes", [])
                    if str(code).strip() in allowed_codes
                )
            )
            if len(codes) < minimum_members:
                continue
            signature = tuple(sorted(codes))
            if signature in seen:
                continue
            seen.add(signature)
            name = str(cluster.get("canonical_name") or "").strip()
            logic = str(cluster.get("common_logic") or "").strip()
            if not name or not logic:
                continue
            aliases = [
                str(value).strip()
                for value in cluster.get("aliases", [])[:12]
                if str(value).strip()
            ]
            result.append(
                {
                    "tag": name[:60],
                    "common_logic": logic[:1000],
                    "member_codes": codes,
                    "aliases": list(dict.fromkeys([name, *aliases])),
                    "cluster_confidence": _bounded_number(cluster.get("cluster_confidence")),
                    "cluster_method": "semantic_event",
                    "catalysts": _normalize_catalysts(cluster.get("catalysts"), sources),
                    "sources": sources,
                    "model": self.model,
                    "raw": raw,
                }
            )
        return result

    def _call_prompt(
        self, prompt: str, *, reasoning_effort: str
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
        payload = {
            "model": self.model,
            "reasoning": {"effort": reasoning_effort},
            "tools": [{"type": "web_search", "search_context_size": "medium"}],
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
        parsed = _parse_json_object(_output_text(raw))
        sources = _sources(raw)
        return raw, parsed, sources


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


def _normalize_catalysts(value: Any, sources: list[dict[str, str]]) -> list[dict[str, Any]]:
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
                "evidence_level": str(raw.get("evidence_level") or "合理推断").strip(),
                "source_kind": str(raw.get("source_kind") or "unknown").strip(),
            }
        )
    return result


def _normalize_proposed_members(
    value: Any, eligible_stock_pool: list[dict[str, Any]]
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    allowed = {
        str(item.get("code")): str(item.get("name") or "")
        for item in eligible_stock_pool
        if item.get("code")
    }
    result = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "").strip()
        if code not in allowed or code in seen:
            continue
        seen.add(code)
        result.append(
            {
                "code": code,
                "name": allowed[code],
                "role": str(raw.get("role") or "同逻辑").strip(),
                "reason": str(raw.get("reason") or "高置信题材标签匹配").strip(),
            }
        )
    return result


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _integer(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _bounded_number(value: Any) -> float:
    return max(0.0, min(100.0, _number(value)))


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes"}
