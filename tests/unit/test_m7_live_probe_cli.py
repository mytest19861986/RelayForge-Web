import json

import pytest

from relayforge.adapters import live_probe_cli


def test_probe_requires_explicit_opt_in(tmp_path, monkeypatch):
    def forbidden():
        pytest.fail("probe attempted to instantiate a connector before confirmation")

    monkeypatch.setitem(live_probe_cli.CONNECTORS, "TELEGRAM", forbidden)
    with pytest.raises(SystemExit):
        live_probe_cli.main(["telegram", "--output", str(tmp_path / "out.json")])


def test_confirmed_probe_uses_selected_transport(tmp_path, monkeypatch):
    token = "12345:synthetic-test-only"
    calls = []

    class Adapter:
        platform_tier = "GA"

        def test_connection(self, selected):
            calls.append(selected)
            return {"status": "BOT_IDENTITY_VERIFIED"}

    monkeypatch.setitem(live_probe_cli.CONNECTORS, "TELEGRAM", Adapter)
    monkeypatch.setenv("RF_AT22_TELEGRAM_TOKEN", token)
    report = tmp_path / "probe.json"
    assert live_probe_cli.main(["telegram", "--confirm-live-probe", "--output", str(report)]) == 0
    assert calls == [token]
    assert token not in report.read_text()
    assert json.loads(report.read_text())["connection"] == "BOT_IDENTITY_VERIFIED"
