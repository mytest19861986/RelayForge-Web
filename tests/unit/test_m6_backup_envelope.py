from uuid import uuid4

import pytest

from relayforge.adapters.tenant_backup import open_bundle, seal


def test_tenant_backup_seal_tamper_and_traversal():
    password = "synthetic-backup-password"
    entries = {"config/workspace.json": b'{"settings_json":{}}'}
    manifest, data = seal(password, uuid4(), "workspace", entries)
    assert manifest["limits"]["max_compression_ratio"] == 1
    assert manifest["kdf_params"]["memory_kib"] == 65536
    assert open_bundle(password, data) == (manifest, entries)
    corrupted = data[:-1] + bytes([data[-1] ^ 1])
    for key, value in ((password, corrupted), ("wrong", data)):
        with pytest.raises(ValueError, match="INVALID_BACKUP"):
            open_bundle(key, value)
    with pytest.raises(ValueError, match="INVALID_BACKUP_PATH"):
        seal(password, uuid4(), "workspace", {"config/../escape.json": b"bad"})
