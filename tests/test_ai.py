from unittest import TestCase

from stocktopic.ai import OpenAIThemeExplainer


class OpenAIEndpointTests(TestCase):
    def test_official_base_url_builds_responses_endpoint(self):
        client = OpenAIThemeExplainer("key", "model")
        self.assertEqual(client.endpoint, "https://api.openai.com/v1/responses")

    def test_custom_base_url_builds_responses_endpoint(self):
        client = OpenAIThemeExplainer("key", "model", "https://provider.example/openai/v1/")
        self.assertEqual(client.endpoint, "https://provider.example/openai/v1/responses")

    def test_complete_responses_endpoint_is_not_duplicated(self):
        client = OpenAIThemeExplainer("key", "model", "https://provider.example/v1/responses/")
        self.assertEqual(client.endpoint, "https://provider.example/v1/responses")

    def test_base_url_rejects_credentials(self):
        with self.assertRaisesRegex(ValueError, "must not contain credentials"):
            OpenAIThemeExplainer("key", "model", "https://user:pass@provider.example/v1")

    def test_semantic_cluster_only_accepts_input_codes_and_actual_search_urls(self):
        client = OpenAIThemeExplainer("key", "model")
        client._call_prompt = lambda prompt, reasoning_effort: (
            {"output": []},
            {
                "clusters": [
                    {
                        "canonical_name": "英伟达PTFE正交背板",
                        "common_logic": "Rubin Ultra背板材料升级",
                        "member_codes": [
                            "600000.SH",
                            "600001.SH",
                            "600002.SH",
                            "600003.SH",
                            "FAKE",
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
            {"code": f"60000{i}.SH", "name": f"股票{i}", "themes": [], "concept_tags": []}
            for i in range(4)
        ]
        clusters = client.cluster_limit_events("20260827", events, 4)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]["member_codes"]), 4)
        self.assertEqual(clusters[0]["catalysts"][0]["source_kind"], "supply_chain_report")
        self.assertEqual(clusters[0]["catalysts"][1]["url"], "")
