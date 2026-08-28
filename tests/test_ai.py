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
