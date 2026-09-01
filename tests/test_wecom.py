import json
from unittest import TestCase
from unittest.mock import patch

from stocktopic.wecom import WeComDeliveryError, WeComNotifier

WEBHOOK = (
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?"
    "key=11111111-2222-3333-4444-555555555555"
)


class JsonResponse:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.value).encode()


class WeComRobotTests(TestCase):
    def test_robot_webhook_sends_text_without_requesting_access_token(self):
        notifier = WeComNotifier(WEBHOOK)
        with patch(
            "stocktopic.wecom.open_url", return_value=JsonResponse({"errcode": 0, "errmsg": "ok"})
        ) as opener:
            notifier.send_text("正式题材", "测试消息")
        request = opener.call_args.args[0]
        payload = json.loads(request.data.decode())
        self.assertEqual(request.full_url, WEBHOOK)
        self.assertEqual(payload["msgtype"], "text")
        self.assertEqual(payload["text"]["content"], "正式题材\n\n测试消息")
        self.assertNotIn("agentid", payload)
        self.assertNotIn("touser", payload)

    def test_rate_limit_response_is_retried(self):
        notifier = WeComNotifier(WEBHOOK)
        with (
            patch(
                "stocktopic.wecom.open_url",
                side_effect=[
                    JsonResponse({"errcode": 45009, "errmsg": "rate limit"}),
                    JsonResponse({"errcode": 0, "errmsg": "ok"}),
                ],
            ) as opener,
            patch("stocktopic.wecom.time.sleep") as sleeper,
        ):
            notifier.send_text("测试", "重试")
        self.assertEqual(opener.call_count, 2)
        sleeper.assert_called_once_with(0.5)

    def test_long_multibyte_content_is_truncated_to_robot_limit(self):
        notifier = WeComNotifier(WEBHOOK)
        with patch(
            "stocktopic.wecom.open_url", return_value=JsonResponse({"errcode": 0, "errmsg": "ok"})
        ) as opener:
            notifier.send_text("题材", "中" * 1000)
        payload = json.loads(opener.call_args.args[0].data.decode())
        self.assertLessEqual(len(payload["text"]["content"].encode()), 2000)
        self.assertTrue(payload["text"]["content"].endswith("…"))

    def test_only_official_wecom_robot_webhook_is_accepted(self):
        for value in (
            "http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
            "https://evil.example/cgi-bin/webhook/send?key=test",
            "https://qyapi.weixin.qq.com/cgi-bin/message/send?key=test",
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send",
            "https://qyapi.weixin.qq.com:invalid/cgi-bin/webhook/send?key=test",
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test&debug=true",
        ):
            with self.subTest(value=value), self.assertRaises(WeComDeliveryError):
                WeComNotifier(value)

    def test_invalid_robot_key_has_actionable_guidance(self):
        error = WeComDeliveryError("发送消息", 93000, "invalid webhook url")
        self.assertIn("errcode=93000", str(error))
        self.assertIn("重新复制", str(error))
