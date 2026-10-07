import os
from unittest.mock import patch

import pytest

from stocktopic.config import Settings


def test_mcp_url_satisfies_tushare_secret_requirement():
    values = {
        "TUSHARE_MCP_URL": "https://relay.example/mcp?token=test-placeholder",
        "ADMIN_PASSWORD": "test",
        "APP_API_TOKEN": "test",
    }
    with patch.dict(os.environ, values, clear=True):
        settings = Settings.from_env()

    assert settings.tushare_mcp_url == values["TUSHARE_MCP_URL"]
    assert settings.tushare_token == ""


def test_tushare_configuration_requires_mcp_url_or_direct_token():
    values = {"ADMIN_PASSWORD": "test", "APP_API_TOKEN": "test"}
    with patch.dict(os.environ, values, clear=True):
        with pytest.raises(RuntimeError, match="TUSHARE_MCP_URL or TUSHARE_TOKEN"):
            Settings.from_env()


def test_rt_k_can_be_disabled_without_disabling_direct_tushare():
    values = {
        "TUSHARE_TOKEN": "test",
        "TUSHARE_RT_K_ENABLED": "false",
        "ADMIN_PASSWORD": "test",
        "APP_API_TOKEN": "test",
    }
    with patch.dict(os.environ, values, clear=True):
        settings = Settings.from_env()

    assert settings.tushare_token == "test"
    assert settings.tushare_mcp_url == ""
    assert settings.tushare_rt_k_enabled is False


def test_invalid_rt_k_boolean_is_rejected():
    values = {
        "TUSHARE_TOKEN": "test",
        "TUSHARE_RT_K_ENABLED": "sometimes",
        "ADMIN_PASSWORD": "test",
        "APP_API_TOKEN": "test",
    }
    with patch.dict(os.environ, values, clear=True):
        with pytest.raises(RuntimeError, match="TUSHARE_RT_K_ENABLED"):
            Settings.from_env()
