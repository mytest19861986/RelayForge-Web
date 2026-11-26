import base64
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psycopg
import pytest
from cryptography.exceptions import InvalidTag
from django.test import Client
from test_m1_auth_rls_vault_and_shredding import expect, login, provision
from test_m4_ai_gateway_channel_and_learning import key, send


def test_rtl_shell_and_reports(db):
    provision("m6-ui")
    account = login("m6-ui")
    for path in ("/", "/app/operations", "/app/media"):
        response = Client().get(path)
        assert response.status_code == 200
        assert b'<html lang="fa" dir="rtl">' in b"".join(response.streaming_content)
        assert "frame-ancestors 'none'" in response["Content-Security-Policy"]
    response = Client().get("/static/relayforge_ui/studio.js")
    script = b"".join(response.streaming_content)
    assert b"localStorage" not in script and b"sessionStorage" not in script
    assert expect(send(account, "GET", "/dashboard"), 200)["sources"] == 0
    expect(send(account, "GET", "/events/poll"), 200)
    assert send(account, "GET", "/reports/export?format=csv").status_code == 200
    expect(send(account, "GET", "/audit-events"), 200)


def test_backup_preview_apply_reset(db):
    provision("m6-backup")
    account = login("m6-backup")
    password = {"backup_password": "synthetic-m6-strong-pass"}
    backup = expect(send(account, "POST", "/backups", password), 200)
    request = password | {"bundle_base64": backup["bundle_base64"], "mode": "PREVIEW"}
    expect(send(account, "POST", "/backups/restore", request), 200)
    assert not expect(send(account, "GET", "/dashboard"), 200)["emergency_stop"]
    expect(
        send(
            account,
            "POST",
            "/backups/restore",
            request | {"backup_password": "synthetic-incorrect-password"},
        ),
        422,
    )
    expect(send(account, "POST", "/backups/restore", request | {"mode": "APPLY"}), 200)
    assert expect(send(account, "GET", "/dashboard"), 200)["emergency_stop"]
    idempotency = key()
    receipt = expect(send(account, "POST", "/ops/reset", password, **idempotency), 200)
    replay = expect(send(account, "POST", "/ops/reset", password, **idempotency), 200)
    assert not receipt["remote_deletion_performed"]
    assert replay["manifest_id"] == receipt["manifest_id"]


def test_authentic_postgres16_pitr(tmp_path, monkeypatch):
    import importlib.util
    import json
    import socket
    import tarfile

    repo = Path(__file__).resolve().parents[2]
    archive_tool = repo / "deploy/pitr/pitr_aead_archive.py"
    spec = importlib.util.spec_from_file_location("rf_pitr", archive_tool)
    archive = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(archive)
    pg_bin = Path(os.environ.get("RF_PG16_BIN", "/usr/lib/postgresql/16/bin"))
    tools = {
        name: shutil.which(str(pg_bin / name)) for name in ("initdb", "pg_ctl", "pg_basebackup")
    }
    if not all(tools.values()):
        if os.environ.get("CI"):
            pytest.fail("PostgreSQL 16 physical backup binaries required")
        pytest.skip("PostgreSQL 16 physical backup binaries unavailable")
    for executable in tools.values():
        assert b" 16." in subprocess.check_output([executable, "--version"])

    def port():
        with socket.socket(socket.AF_INET) as address:
            address.bind(("127.0.0.1", 0))
            return address.getsockname()[1]

    secret = os.urandom(32)
    archive_key = tmp_path / "archive-key"
    archive_key.write_bytes(base64.b64encode(secret))
    archive_key.chmod(0o600)
    monkeypatch.setenv("RF_PITR_KEY_FILE", str(archive_key))
    wal = tmp_path / "wal"
    wal.mkdir()
    source, target = tmp_path / "source", tmp_path / "restored"
    source_port, target_port = port(), port()
    cmd = shlex.join([sys.executable, str(archive_tool)])
    run = subprocess.run
    run(
        [tools["initdb"], "-D", str(source), "-U", "postgres", "-A", "trust", "--no-locale"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    with (source / "postgresql.auto.conf").open("a") as config:
        config.write(
            f"port={source_port}\nlisten_addresses='127.0.0.1'\nunix_socket_directories=''\n"
            "wal_level=replica\narchive_mode=on\n"
            f"archive_command='{cmd} seal %p {wal}/%f.aead %f'\n"
        )

    def control(directory, action):
        run(
            [
                tools["pg_ctl"],
                "-D",
                str(directory),
                "-l",
                str(directory.with_suffix(".log")),
                "-w",
                action,
            ],
            check=True,
            timeout=60,
            capture_output=True,
        )

    control(source, "start")
    try:
        dsn = f"postgresql://postgres@127.0.0.1:{source_port}/postgres"
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE TABLE drill (tenant_id uuid PRIMARY KEY)")
        physical = tmp_path / "physical"
        run(
            [
                tools["pg_basebackup"],
                "-d",
                dsn,
                "-D",
                str(physical),
                "-Ft",
                "-X",
                "none",
                "--checkpoint=fast",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        sealed_base = tmp_path / "base.aead"
        archive.transfer(physical / "base.tar", sealed_base, secret, "base")
        marker = "0ad2347a-12db-4f73-a614-9614c2cbe250"
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("INSERT INTO drill VALUES (%s)", (marker,))
            segment = conn.execute(
                "SELECT pg_walfile_name(pg_current_wal_insert_lsn())"
            ).fetchone()[0]
            conn.execute("SELECT pg_switch_wal()")
        sealed_wal = wal / f"{segment}.aead"
        deadline = time.monotonic() + 45
        while not sealed_wal.exists() and time.monotonic() < deadline:
            time.sleep(0.15)
        assert sealed_wal.exists()
        for encrypted, label in ((sealed_base, "base"), (sealed_wal, segment)):
            content = bytearray(encrypted.read_bytes())
            content[len(content) // 2] ^= 1
            corrupted = tmp_path / "corrupted"
            corrupted.write_bytes(content)
            with pytest.raises(InvalidTag):
                archive.transfer(corrupted, tmp_path / "rejected", secret, label, decrypt=True)
            assert not (tmp_path / "rejected").exists()
        start = time.monotonic()
        restored_tar = tmp_path / "restored.tar"
        archive.transfer(sealed_base, restored_tar, secret, "base", decrypt=True)
        archive.transfer(sealed_wal, tmp_path / "verified.wal", secret, segment, decrypt=True)
        target.mkdir(mode=0o700)
        with tarfile.open(restored_tar) as tar:
            tar.extractall(target, filter="data")
        with (target / "postgresql.auto.conf").open("a") as config:
            config.write(
                f"\nport={target_port}\narchive_mode=off\n"
                f"restore_command='{cmd} open {wal}/%f.aead %p %f'\n"
            )
        (target / "recovery.signal").touch()
        control(target, "start")
        try:
            dsn = f"postgresql://postgres@127.0.0.1:{target_port}/postgres"
            with psycopg.connect(dsn) as conn:
                recovered = conn.execute("SELECT tenant_id::text FROM drill").fetchone()[0]
            assert recovered == marker
            elapsed = time.monotonic() - start
            print(f"OC-03 physical PITR rto_seconds={elapsed:.3f}")
            assert 0 < elapsed <= 1800
            evidence = repo / "tmp/m6-oc03-pitr-evidence.json"
            evidence.parent.mkdir(exist_ok=True)
            evidence.write_text(
                json.dumps(
                    {
                        "rto_seconds": elapsed,
                        "recovered_post_basebackup_row": True,
                        "base_tamper_rejected": True,
                        "wal_tamper_rejected": True,
                    }
                )
            )
        finally:
            control(target, "stop")
    finally:
        control(source, "stop")


def test_notifications_and_formula_safety(db):
    from django.db import transaction

    from relayforge.web.db import first, set_context
    from relayforge.web.ops_m6 import alert, csv_cell

    for value in ("=CMD|test", "+1", "-2", "@SUM", "  =1+1", "\t=1", "\r=1"):
        assert csv_cell(value) == "'" + value
    provision("m6-notifications")
    account = login("m6-notifications")
    ws = account["default_workspace_id"]
    with transaction.atomic():
        set_context(account["tenant_id"])
        for _ in range(3):
            alert(account["tenant_id"], ws, "fixture", "WARNING", "هشدار", "آزمایشی")
        assert first("SELECT count(*) AS total FROM app.system_notifications")["total"] == 1
    listing = expect(send(account, "GET", "/notifications"), 200)
    assert listing["items"][0]["occurrence_count"] == 3
    expect(send(account, "POST", f"/notifications/{listing['items'][0]['id']}/read", {}), 200)
    assert expect(send(account, "GET", "/dashboard"), 200)["unread"] == 0


def test_automated_route_attaches_workspace_media_and_falls_back(db, monkeypatch):
    from uuid import uuid4

    from test_m3_publishing_and_real_sigkill import CHANNEL, destination
    from test_m4_ai_gateway_channel_and_learning import configure, grant, provider

    from relayforge.adapters.ai_gateway import Generation
    from relayforge.web import ai_m4

    provision("m6-media-route")
    account = login("m6-media-route")
    ws = account["default_workspace_id"]
    secret = expect(
        send(
            account,
            "POST",
            "/security/secrets",
            {
                "secret_kind": "TELEGRAM_BOT_TOKEN",
                "label": "M6 bot",
                "raw_secret": "12345:fixture",
            },
        ),
        201,
    )["id"]
    dest = destination(db, account | {"secret_id": secret}, CHANNEL + "1")
    source = expect(
        send(
            account,
            "POST",
            "/sources",
            {
                "source_kind": "WEBHOOK_INBOUND",
                "canonical_channel_id": CHANNEL,
                "display_title": "M6 source",
                "webhook_platform": "telegram",
            },
        ),
        201,
    )
    selected = provider(account)
    configure(account)
    grant(account, "OPENAI_TEXT")
    output = iter(("قیمت ۱۰۰ تومان", "هواشناسی ساحل ۲۰۰ تومان"))

    class Stub:
        def generate(self, *_args, **_kwargs):
            return Generation(next(output), None, 1, 1, 1)

    monkeypatch.setattr(ai_m4, "adapter", lambda *_: Stub())
    asset = uuid4()
    with psycopg.connect(db) as conn:
        conn.execute(
            """
            INSERT INTO app.media_assets
              (id,tenant_id,workspace_id,media_kind,origin_kind,s3_object_key,
               sha256_hex,mime_type,byte_size)
            VALUES (%s,%s,%s,'IMAGE','USER_UPLOAD',%s,%s,'image/png',1024)
        """,
            (
                asset,
                account["tenant_id"],
                ws,
                f"tenants/{account['tenant_id']}/media/" + "a" * 64,
                "a" * 64,
            ),
        )
    policy = {
        "source_ids": [source["id"]],
        "destination_ids": [dest],
        "fallback_policy": "SEND_WITHOUT_MEDIA",
        "transform_rules": {
            "routing_mode": "MANUAL",
            "manual_provider_id": selected,
            "manual_model_id": "gpt-local",
            "media_policy": "ATTACH_WORKSPACE_ASSET",
            "media_asset_id": str(asset),
        },
    }
    route = expect(
        send(
            account,
            "POST",
            "/routes",
            policy
            | {
                "slug": "m6-media",
                "display_name": "M6 media",
                "route_mode": "HYBRID",
                "automation_level": "AUTO_PUBLISH_BY_POLICY",
            },
        ),
        201,
    )
    endpoint = f"/routes/{route['id']}"
    expect(send(account, "PATCH", endpoint, {"is_enabled": True}), 200)

    def run(message, text):
        from test_m1_auth_rls_vault_and_shredding import request

        token = source["webhook_token"]
        expect(
            request(
                Client(),
                "POST",
                f"/webhooks/inbound/telegram/{token}",
                {
                    "channel_post": {
                        "message_id": message,
                        "text": text,
                        "chat": {"id": int(CHANNEL), "type": "channel"},
                    },
                },
                HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=token,
            ),
            200,
        )
        with psycopg.connect(db) as conn:
            item = conn.execute(
                """
                SELECT ci.id FROM app.content_items ci JOIN app.content_item_revisions r
                 ON r.content_item_id=ci.id WHERE ci.source_id=%s AND r.plain_text=%s
            """,
                (source["id"], text),
            ).fetchone()[0]
        return expect(
            send(account, "POST", endpoint + "/run-item", {"content_item_id": str(item)}), 200
        )

    attached = run(1, "قیمت ۱۰۰ تومان")
    assert attached["decision"] == "ENQUEUED"
    with psycopg.connect(db) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM app.media_asset_refs WHERE media_asset_id=%s",
                (asset,),
            ).fetchone()[0]
            == 1
        )
    policy["transform_rules"]["media_asset_id"] = str(uuid4())
    expect(send(account, "PATCH", endpoint, {"policy": policy}), 200)
    fallback = run(2, "هواشناسی ساحل ۲۰۰ تومان")
    assert fallback["decision"] == "ENQUEUED"
    assert fallback["media_fallback_applied"] == "SEND_WITHOUT_MEDIA"
