import base64
import json
import urllib.error
from dataclasses import replace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from stocktopic.api import create_app
from stocktopic.config import Settings
from stocktopic.feishu import FeishuDeliveryError, FeishuNotifier
from stocktopic.service import StockTopicService, _safe_error

WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/test-placeholder-hook"


class JsonResponse:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.value).encode()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        tushare_token="test",
        db_path=tmp_path / "test.sqlite3",
        archive_dir=tmp_path / "archive",
        admin_username="admin",
        admin_password="password",
        app_api_token="app-token",
        feishu_bot_webhook=WEBHOOK,
    )


def test_feishu_payload_and_success_are_not_wecom_protocol():
    with patch("stocktopic.feishu.open_url", return_value=JsonResponse({"code": 0})) as opener:
        FeishuNotifier(WEBHOOK).send_text("正式题材", "测试消息")
    request = opener.call_args.args[0]
    assert request.full_url == WEBHOOK
    assert json.loads(request.data) == {
        "msg_type": "text",
        "content": {"text": "正式题材\n\n测试消息"},
    }


@pytest.mark.parametrize("code", [11232, 99991400])
def test_feishu_rate_limit_response_retries(code):
    with (
        patch(
            "stocktopic.feishu.open_url",
            side_effect=[
                JsonResponse({"code": code}),
                JsonResponse({"code": 0}),
            ],
        ) as opener,
        patch("stocktopic.feishu.time.sleep") as sleeper,
    ):
        FeishuNotifier(WEBHOOK).send_text("测试", "重试")
    assert opener.call_count == 2
    sleeper.assert_called_once_with(0.5)


def test_network_http_and_business_errors_share_three_attempt_budget():
    with (
        patch(
            "stocktopic.feishu.open_url",
            side_effect=[
                urllib.error.URLError(WEBHOOK),
                urllib.error.HTTPError(WEBHOOK, 503, "unavailable", {}, None),
                JsonResponse({"code": 11232, "msg": "rate limited"}),
            ],
        ) as opener,
        patch("stocktopic.feishu.time.sleep") as sleeper,
        pytest.raises(FeishuDeliveryError, match="11232"),
    ):
        FeishuNotifier(WEBHOOK).send_text("测试", "三次上限")
    assert opener.call_count == 3
    assert sleeper.call_count == 2


@pytest.mark.parametrize(
    "result",
    [
        {},
        [],
        {"code": False},
        {"code": "0"},
        {"code": 19024, "StatusCode": 0, "msg": "keyword missing"},
    ],
)
def test_missing_or_failed_business_status_cannot_mark_delivery_success(result):
    with (
        patch("stocktopic.feishu.open_url", return_value=JsonResponse(result)),
        patch("stocktopic.feishu.time.sleep"),
        pytest.raises(FeishuDeliveryError),
    ):
        FeishuNotifier(WEBHOOK).send_text("测试", "状态校验")


def test_legacy_success_response_remains_supported():
    with patch("stocktopic.feishu.open_url", return_value=JsonResponse({"StatusCode": 0})):
        FeishuNotifier(WEBHOOK).send_text("测试", "兼容响应")


def test_permanent_failure_has_guidance_and_no_retry_or_secret_leak():
    secret = "test-signing-secret"
    with (
        patch(
            "stocktopic.feishu.open_url",
            return_value=JsonResponse(
                {
                    "code": 19021,
                    "msg": f"sign mismatch {WEBHOOK} {secret}",
                }
            ),
        ) as opener,
        patch("stocktopic.feishu.time.sleep") as sleeper,
        pytest.raises(FeishuDeliveryError, match="FEISHU_BOT_SECRET") as error,
    ):
        FeishuNotifier(WEBHOOK, secret=secret).send_text("测试", "签名失败")
    assert "test-placeholder-hook" not in str(error.value)
    assert secret not in str(error.value)
    assert opener.call_count == 1
    sleeper.assert_not_called()


def test_non_retryable_http_error_stops_immediately():
    with (
        patch(
            "stocktopic.feishu.open_url",
            side_effect=urllib.error.HTTPError(
                WEBHOOK,
                403,
                "forbidden",
                {},
                None,
            ),
        ) as opener,
        pytest.raises(FeishuDeliveryError, match="HTTP 403"),
    ):
        FeishuNotifier(WEBHOOK).send_text("测试", "权限拒绝")
    assert opener.call_count == 1


def test_exhausted_network_failure_never_exposes_webhook_or_signing_secret():
    with (
        patch(
            "stocktopic.feishu.open_url",
            side_effect=urllib.error.URLError(
                WEBHOOK + " test-signing-secret",
            ),
        ),
        patch("stocktopic.feishu.time.sleep"),
        pytest.raises(FeishuDeliveryError) as error,
    ):
        FeishuNotifier(WEBHOOK, secret="test-signing-secret").send_text("测试", "失败")
    assert "test-placeholder-hook" not in str(error.value)
    assert "test-signing-secret" not in str(error.value)


@pytest.mark.parametrize("body", ["中😀" * 10000, '\x00"\\' * 10000])
def test_request_limit_includes_json_escaping_and_optional_signature(body):
    with patch("stocktopic.feishu.open_url", return_value=JsonResponse({"code": 0})) as opener:
        FeishuNotifier(WEBHOOK, secret="test-secret").send_text("题材", body)
    data = opener.call_args.args[0].data
    assert len(data) <= 20_000
    assert json.loads(data)["content"]["text"].endswith("…")


def test_signature_uses_feishu_timestamp_and_empty_message_hmac():
    with (
        patch("stocktopic.feishu.time.time", return_value=1600000000),
        patch("stocktopic.feishu.open_url", return_value=JsonResponse({"code": 0})) as opener,
    ):
        FeishuNotifier(WEBHOOK, secret="test-secret").send_text("测试", "签名")
    payload = json.loads(opener.call_args.args[0].data)
    assert payload["timestamp"] == "1600000000"
    assert payload["sign"] == "BwqgZGo8lGLJAUAc/kiJR0TDtnLVnsITtszoHeRuXV0="


def test_only_official_feishu_custom_bot_webhook_is_accepted():
    for value in (
        "http://open.feishu.cn/open-apis/bot/v2/hook/test",
        "https://evil.example/open-apis/bot/v2/hook/test",
        "https://open.feishu.cn.evil.example/open-apis/bot/v2/hook/test",
        "https://open.feishu.cn:invalid/open-apis/bot/v2/hook/test",
        "https://open.feishu.cn/open-apis/bot/v2/hook/",
        WEBHOOK + "?secret=test",
        WEBHOOK + "#test",
        WEBHOOK + "/extra",
        "https://user:password@open.feishu.cn/open-apis/bot/v2/hook/test",
    ):
        with pytest.raises(FeishuDeliveryError):
            FeishuNotifier(value)


def test_disabled_feishu_never_falls_back_to_legacy_wecom(settings):
    service = StockTopicService(
        replace(
            settings,
            feishu_bot_webhook="",
            wecom_bot_webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=legacy-test",
        )
    )
    service.initialize_storage()
    assert isinstance(service.notifier, FeishuNotifier)
    assert not service.notifier.enabled
    assert service.health()["integrations"]["wecom_group_robot"] is False
    with patch("stocktopic.feishu.open_url") as opener:
        service._send_alert(
            dedupe_key="data",
            category="data_pull_failed",
            severity="high",
            title="测试",
            body="未配置",
        )
    opener.assert_not_called()
    assert service.database.recent_alerts()[0]["pushed"] == 0


def test_local_observation_is_suppressed_at_notification_boundary(settings):
    service = StockTopicService(settings)
    service.initialize_storage()
    with patch("stocktopic.feishu.open_url") as opener:
        service._send_alert(
            dedupe_key="local",
            category="local_observation",
            severity="high",
            title="本地早期观察",
            body="仅网页展示",
        )
    opener.assert_not_called()
    assert service.database.recent_alerts() == []


def test_channel_aware_delivery_keeps_old_alerts_and_records_feishu_failures(settings):
    service = StockTopicService(settings)
    service.initialize_storage()
    db = service.database
    old_id = db.create_alert("old", "formal_theme", "high", "历史企业微信", "历史")
    db.mark_alert_pushed(old_id)
    with patch("stocktopic.feishu.open_url", return_value=JsonResponse({"code": 0})):
        for category in (
            "formal_theme",
            "early_watch_theme",
            "high_value_opportunity",
            "high_risk",
            "data_pull_failed",
            "data_stale",
        ):
            service._send_alert(
                dedupe_key=category,
                category=category,
                severity="high",
                title=category,
                body="飞书推送",
            )
        service._send_alert(
            dedupe_key="formal_theme",
            category="formal_theme",
            severity="high",
            title="不重复",
            body="不重复",
        )
    rows = db.recent_alerts()
    assert len(rows) == 7
    for row in rows:
        assert row["pushed"] == 1
        assert row["push_channel"] == ("wecom" if row["id"] == old_id else "feishu")
    with patch(
        "stocktopic.feishu.open_url",
        return_value=JsonResponse(
            {
                "code": 19024,
                "msg": "keyword not found " + WEBHOOK,
            }
        ),
    ):
        service._send_alert(
            dedupe_key="failure",
            category="data_pull_failed",
            severity="high",
            title="失败",
            body="失败",
        )
    failed = db.recent_alerts()[0]
    assert failed["pushed"] == 0 and failed["push_channel"] == "feishu"
    assert "19024" in failed["push_error"]
    health = service.health()
    assert health["integrations"]["feishu_group_robot"] is True
    assert health["last_feishu_success_at"]
    assert "test-placeholder-hook" not in health["latest_feishu_error"]
    assert "test-placeholder-hook" not in _safe_error(RuntimeError("failed " + WEBHOOK))


def test_feishu_test_endpoint_requires_auth_and_returns_channel(settings):
    app = create_app(settings)
    app.state.service.initialize_storage()
    client = TestClient(app, base_url="https://testserver")
    assert client.post("/api/v1/admin/feishu-test").status_code == 401
    basic = {
        "Authorization": "Basic " + base64.b64encode(b"admin:password").decode(),
        "X-StockTopic-Request": "1",
    }
    assert (
        client.post(
            "/api/v1/admin/feishu-test",
            headers={
                "Authorization": basic["Authorization"],
            },
        ).status_code
        == 403
    )
    with patch("stocktopic.feishu.open_url", return_value=JsonResponse({"code": 0})) as opener:
        for path in ("/api/v1/admin/feishu-test", "/api/v1/admin/wecom-test"):
            response = client.post(path, headers=basic)
            assert response.status_code == 200
            assert response.json() == {"ok": True, "channel": "feishu"}
    assert opener.call_count == 2
    assert app.state.service.database.get_metadata("last_feishu_success_at")


def test_unconfigured_test_endpoint_returns_503(settings):
    app = create_app(replace(settings, feishu_bot_webhook=""))
    app.state.service.initialize_storage()
    client = TestClient(app, base_url="https://testserver")
    basic = {
        "Authorization": "Basic " + base64.b64encode(b"admin:password").decode(),
        "X-StockTopic-Request": "1",
    }
    response = client.post("/api/v1/admin/feishu-test", headers=basic)
    assert response.status_code == 503
    assert "飞书机器人未配置" in response.json()["detail"]
