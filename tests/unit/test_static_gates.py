import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest

from relayforge.web.db_tx import on_tenant_commit

GATE = Path(__file__).resolve().parents[2] / "scripts/gates/check_ast_gates.py"


@pytest.mark.parametrize("tenant_id", [None, "", "not-a-uuid"])
def test_reject_invalid_tenant(tenant_id):
    with pytest.raises(ValueError, match="tenant_id"):
        on_tenant_commit(tenant_id, Mock())


def test_bind_tenant_at_registration(monkeypatch):
    tenant_id = UUID(int=1)
    callback = Mock()
    register = Mock()
    monkeypatch.setattr("relayforge.web.db_tx.transaction.on_commit", register)

    on_tenant_commit(tenant_id, callback, using="tenant-db")

    callback.assert_not_called()
    assert register.call_args.kwargs == {"using": "tenant-db"}
    register.call_args.args[0]()
    callback.assert_called_once_with(tenant_id)


def test_repository_ast_gate():
    subprocess.run([sys.executable, str(GATE)], check=True)


@pytest.mark.parametrize(
    ("path", "source", "rule"),
    [
        ("relayforge/web/alias.py", "from django.db import transaction as tx; tx.on_commit(fn)",
         "OC-06"),
        ("relayforge/web/attr.py", 'getattr(transaction, "on_commit")(fn)', "OC-06"),
        ("relayforge/core/dynamic.py", 'import importlib; importlib.import_module("django.conf")',
         "CORE"),
        ("relayforge/core/relative.py", "from .. import adapters", "CORE"),
    ],
)
def test_gate_rejects_forbidden_code(tmp_path, path, source, rule):
    file = tmp_path / path
    file.parent.mkdir(parents=True)
    file.write_text(source, encoding="utf-8")

    assert f"{file}:1: {rule}:" in next(runpy.run_path(str(GATE))["violations"](tmp_path))
