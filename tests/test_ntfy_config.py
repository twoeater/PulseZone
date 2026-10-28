import os

import pytest

from pulsezone.config import Config


@pytest.mark.parametrize("token", ["", "사용자: 비밀:번호 ", "tk_test"])
def test_ntfy_single_auth_value_and_environment_priority(tmp_path, monkeypatch, token):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    keys = ("ALERT_NTFY", "ALERT_NTFY_TOKEN")
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        'ALERT_NTFY=https://ntfy.example.test/private\n'
        'ALERT_NTFY_TOKEN="{}"\n'.format(token), encoding="utf-8")
    cfg = Config.from_env(str(env))
    assert cfg.alerts["ntfy"] == "https://ntfy.example.test/private"
    assert cfg.ntfy_token == token

    monkeypatch.setenv("ALERT_NTFY_TOKEN", "tk_environment")
    cfg = Config.from_env(str(env))
    assert cfg.ntfy_token == "tk_environment"


def test_ntfy_empty_topic_disables_channel_even_with_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ALERT_NTFY", "")
    monkeypatch.setenv("ALERT_NTFY_TOKEN", "monitor:password")
    assert "ntfy" not in Config.from_env(str(tmp_path / "missing.env")).alerts
