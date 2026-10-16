import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def config_script(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/configure_integrations.py"
    spec = importlib.util.spec_from_file_location("integration_config", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "APP_DIR", tmp_path)
    monkeypatch.setattr(module, "ENV_PATH", tmp_path / ".env")
    return module


def test_feishu_only_preserves_unrelated_settings_and_hides_secrets(
    config_script,
    monkeypatch,
    capsys,
):
    original = "TUSHARE_TOKEN=retain-test\nADMIN_PASSWORD=retain-password\n# keep comment\n"
    config_script.ENV_PATH.write_text(original)
    webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/test-new-private"
    answers = iter([webhook, "test-signing-secret"])
    monkeypatch.setattr("sys.argv", ["configure_integrations.py", "--feishu-only"])
    monkeypatch.setattr(config_script.getpass, "getpass", lambda _: next(answers))
    config_script.main()
    saved = config_script.ENV_PATH.read_text()
    assert saved.startswith(original)
    assert f"FEISHU_BOT_WEBHOOK={webhook}" in saved
    assert "FEISHU_BOT_SECRET=test-signing-secret" in saved
    assert config_script.ENV_PATH.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr().out
    assert "test-new-private" not in output
    assert "test-signing-secret" not in output
    prompts = []

    def keep_existing(prompt):
        prompts.append(prompt)
        return ""

    monkeypatch.setattr(config_script.getpass, "getpass", keep_existing)
    config_script.main()
    assert config_script.ENV_PATH.read_text() == saved
    assert all(webhook not in prompt and "test-signing-secret" not in prompt for prompt in prompts)


def test_invalid_hook_does_not_change_existing_env_or_echo_secret(config_script, monkeypatch):
    original = "ADMIN_PASSWORD=retain-password\n"
    config_script.ENV_PATH.write_text(original)
    answers = iter(["https://evil.example/test-secret", ""])
    monkeypatch.setattr("sys.argv", ["configure_integrations.py", "--feishu-only"])
    monkeypatch.setattr(config_script.getpass, "getpass", lambda _: next(answers))
    with pytest.raises(SystemExit) as error:
        config_script.main()
    assert config_script.ENV_PATH.read_text() == original
    assert "test-secret" not in str(error.value)
