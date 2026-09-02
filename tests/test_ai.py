import urllib.error
from unittest import TestCase
from unittest.mock import patch

from stocktopic.ai import OpenAIThemeExplainer, _concrete_suggested_name
from stocktopic.theme_graph import install_graph_first_ai_clustering


class JsonResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return b'{"output": []}'


class OpenAIEndpointTests(TestCase):
    def test_official_base_url_builds_responses_endpoint(self):
        client = OpenAIThemeExplainer("key", "model")
        self.assertEqual(client.endpoint, "https://api.openai.com/v1/responses")

    def test_custom_base_url_builds_responses_endpoint(self):
        client = OpenAIThemeExplainer("key", "model", "https://provider.example/openai/v1/")
        self.assertEqual(client.endpoint, "https://provider.example/openai/v1/responses")

    def test_task_model_can_be_cheaper_without_lowering_admission_model(self):
        client = OpenAIThemeExplainer(
            "key",
            "strong-model",
            task_models={"catalyst_refresh": "economy-model"},
        )
        self.assertEqual(client.model_for_task("catalyst_refresh"), "economy-model")
        self.assertEqual(client.model_for_task("admission_analysis"), "strong-model")

    def test_complete_responses_endpoint_is_not_duplicated(self):
        client = OpenAIThemeExplainer("key", "model", "https://provider.example/v1/responses/")
        self.assertEqual(client.endpoint, "https://provider.example/v1/responses")

    def test_base_url_rejects_credentials(self):
        with self.assertRaisesRegex(ValueError, "must not contain credentials"):
            OpenAIThemeExplainer("key", "model", "https://user:pass@provider.example/v1")

    def test_openai_request_sends_json_accept_and_explicit_user_agent(self):
        client = OpenAIThemeExplainer("key", "model", "https://relay.example/v1")
        with patch.object(
            client, "_request_json_with_retry", return_value={"output": []}
        ) as sender:
            client._request_payload({"model": "model", "input": "test"})

        request = sender.call_args.args[0]
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(
            request.get_header("User-agent"),
            "StockTopic/0.12 (+https://github.com/Chris958/stocktopic)",
        )

    def test_broad_admission_name_falls_back_to_concrete_shared_tag(self):
        self.assertEqual(
            _concrete_suggested_name(
                {"suggested_name": "医药"},
                {"shared_tag": "创新药出海授权", "provisional_name": "创新药待审"},
            ),
            "创新药出海授权",
        )

    def test_transient_dns_failure_is_retried_before_ai_request_fails(self):
        client = OpenAIThemeExplainer("key", "model", "https://relay.example/v1")
        error = urllib.error.URLError(OSError(8, "nodename nor servname provided"))
        with (
            patch("stocktopic.ai.open_url", side_effect=[error, JsonResponse()]) as opener,
            patch("stocktopic.ai.time.sleep") as sleeper,
        ):
            result = client._request_json_with_retry(object())
        self.assertEqual(result, {"output": []})
        self.assertEqual(opener.call_count, 2)
        sleeper.assert_called_once_with(1.0)

    def test_persistent_dns_failure_reports_host_after_three_attempts(self):
        client = OpenAIThemeExplainer("key", "model", "https://relay.example/v1")
        error = urllib.error.URLError(OSError(8, "nodename nor servname provided"))
        with (
            patch("stocktopic.ai.open_url", side_effect=error) as opener,
            patch("stocktopic.ai.time.sleep") as sleeper,
            self.assertRaisesRegex(RuntimeError, "host=relay.example"),
        ):
            client._request_json_with_retry(object())
        self.assertEqual(opener.call_count, 3)
        self.assertEqual(sleeper.call_count, 2)

    def test_semantic_cluster_only_accepts_input_codes_and_actual_search_urls(self):
        install_graph_first_ai_clustering()
        client = OpenAIThemeExplainer("key", "model")
        client._call_prompt = lambda prompt, reasoning_effort, **kwargs: (
            {"output": []},
            {
                "clusters": [
                    {
                        "anchor_tag": "PTFE",
                        "canonical_name": "英伟达PTFE正交背板",
                        "common_logic": "Rubin Ultra背板材料升级",
                        "member_codes": [
                            "600000.SH",
                            "600001.SH",
                            "600002.SH",
                            "600003.SH",
                            "FAKE",
                        ],
                        "member_reasons": [
                            {"code": "600000.SH", "reason": "与背板材料升级直接相关"},
                            {"code": "FAKE", "reason": "不应保留"},
                        ],
                        "aliases": ["PTFE", "PCB材料"],
                        "cluster_confidence": 90,
                        "catalysts": [
                            {
                                "title": "供应链材料升级",
                                "summary": "PTFE用于高速背板",
                                "url": "https://example.com/real",
                                "source": "产业媒体",
                                "evidence_level": "供应链未确认",
                                "source_kind": "supply_chain_report",
                            },
                            {
                                "title": "虚构来源",
                                "summary": "不应保存URL",
                                "url": "https://fake.example/not-searched",
                            },
                        ],
                    }
                ]
            },
            [{"url": "https://example.com/real", "title": "真实来源"}],
        )
        events = [
            {
                "code": f"60000{i}.SH",
                "name": f"股票{i}",
                "themes": ["PTFE"],
                "concept_tags": [],
            }
            for i in range(4)
        ]
        clusters = client.cluster_limit_events("20260827", events, 4)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["tag"], "PTFE")
        self.assertEqual(len(clusters[0]["member_codes"]), 4)
        self.assertEqual(
            clusters[0]["member_reasons"],
            {"600000.SH": "与背板材料升级直接相关"},
        )
        self.assertEqual(clusters[0]["catalysts"][0]["source_kind"], "supply_chain_report")
        self.assertEqual(clusters[0]["catalysts"][1]["url"], "")

    def test_admission_prompt_limits_old_theme_rejection_to_system_history_window(self):
        client = OpenAIThemeExplainer("key", "model")
        captured = {}

        def answer(prompt, reasoning_effort, **kwargs):
            captured["prompt"] = prompt
            return (
                {"output": []},
                {
                    "suggested_name": "AI漫剧上星",
                    "is_new_theme": False,
                    "novelty_confidence": 20,
                    "novelty_reason": "2月份曾出现AI漫剧热点",
                    "within_window_match_ids": [],
                    "catalyst_summary": "新剧上星",
                    "catalyst_confidence": 80,
                    "expected_duration_days": 3,
                    "duration_reason": "播出与平台排期",
                    "leader_candidate_code": "300001.SZ",
                    "leader_upside_scenario_pct": 30,
                    "upside_scenario_reason": "收视与商业化超预期",
                    "counter_evidence": [],
                    "proposed_members": [],
                    "catalysts": [],
                },
                [],
            )

        client._call_prompt = answer
        result = client.assess_for_admission(
            {
                "provisional_name": "AI漫剧待审",
                "shared_tag": "AI漫剧上星",
                "members": [
                    {"code": "300001.SZ", "name": "测试股份", "evidence": {}}
                ],
            },
            [],
            [],
        )
        self.assertFalse(result["is_new_theme"])
        self.assertEqual(result["within_window_match_ids"], [])
        self.assertIn("更早历史只能作为产业背景", captured["prompt"])

    def test_responses_request_has_task_specific_token_controls_and_usage_recording(self):
        recorded = []
        client = OpenAIThemeExplainer("key", "model", usage_callback=recorded.append)
        payloads = []

        def respond(payload):
            payloads.append(payload)
            return {
                "output": [
                    {"type": "web_search_call", "action": {"sources": []}},
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "{}"}],
                    },
                ],
                "usage": {
                    "input_tokens": 1200,
                    "input_tokens_details": {"cached_tokens": 800},
                    "output_tokens": 300,
                    "output_tokens_details": {"reasoning_tokens": 120},
                    "total_tokens": 1500,
                },
            }

        client._request_payload = respond
        client._call_prompt(
            "stable prompt",
            reasoning_effort="low",
            task_type="catalyst_refresh",
            subject_id="7",
            search_context_size="low",
            max_output_tokens=3500,
            max_tool_calls=3,
        )
        self.assertEqual(payloads[0]["max_output_tokens"], 3500)
        self.assertEqual(payloads[0]["max_tool_calls"], 3)
        self.assertEqual(payloads[0]["prompt_cache_key"], "stocktopic:catalyst_refresh:v1")
        self.assertEqual(payloads[0]["tools"][0]["search_context_size"], "low")
        self.assertEqual(recorded[0]["cached_input_tokens"], 800)
        self.assertEqual(recorded[0]["reasoning_tokens"], 120)
        self.assertEqual(recorded[0]["web_search_calls"], 1)

    def test_unsupported_optional_controls_are_negotiated_once(self):
        client = OpenAIThemeExplainer("key", "model")
        payloads = []

        def respond(payload):
            payloads.append(payload)
            if len(payloads) == 1:
                raise RuntimeError("OpenAI HTTP 400: unsupported prompt_cache_key")
            return {"output": []}

        client._request_payload = respond
        client._call_prompt(
            "prompt",
            reasoning_effort="low",
            task_type="catalyst_refresh",
        )
        self.assertEqual(client._request_controls_mode, "bounded")
        self.assertNotIn("prompt_cache_key", payloads[1])
        self.assertIn("max_output_tokens", payloads[1])

        client._call_prompt(
            "next prompt",
            reasoning_effort="low",
            task_type="catalyst_refresh",
        )
        self.assertEqual(len(payloads), 3)
        self.assertNotIn("prompt_cache_key", payloads[2])
