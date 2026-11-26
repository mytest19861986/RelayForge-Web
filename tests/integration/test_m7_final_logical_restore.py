import hashlib
import io
import random

import psycopg
import pytest
from django.conf import settings
from PIL import Image
from test_m1_auth_rls_vault_and_shredding import expect, login, provision
from test_m2_s3_export_and_ingestion import s3 as s3
from test_m3_publishing_and_real_sigkill import CHANNEL, destination
from test_m4_ai_gateway_channel_and_learning import configure, grant, send


@pytest.mark.parametrize("include_secrets", [True, False])
def test_full_restore_media_destination_and_credentials(db, s3, monkeypatch, include_secrets):
    from relayforge.web import logical_m7, ops_m6

    suffix = "keyed" if include_secrets else "revoked"
    handles = [f"restore-{suffix}-{label}" for label in ("from", "to")]
    for handle in handles:
        provision(handle)
    a, b = [login(handle) for handle in handles]
    backend, _ = s3
    monkeypatch.setattr(logical_m7, "storage", lambda: backend)
    monkeypatch.setattr(ops_m6, "storage", lambda: backend)
    tenant_a, tenant_b = a["tenant_id"], b["tenant_id"]
    source_ws, target_ws = a["default_workspace_id"], b["default_workspace_id"]
    token = "123456:restore-synthetic-token"
    secret = expect(
        send(
            a,
            "POST",
            "/security/secrets",
            {
                "secret_kind": "TELEGRAM_BOT_TOKEN",
                "label": "Restore bot",
                "raw_secret": token,
            },
        ),
        201,
    )["id"]
    dest = destination(db, a | {"secret_id": secret}, CHANNEL + "1")
    source = expect(
        send(
            a,
            "POST",
            "/sources",
            {
                "source_kind": "WEBHOOK_INBOUND",
                "canonical_channel_id": CHANNEL,
                "display_title": "Restore source",
                "webhook_platform": "telegram",
            },
        ),
        201,
    )["id"]
    route = expect(
        send(
            a,
            "POST",
            "/routes",
            {
                "slug": "restore-route",
                "display_name": "Restore route",
                "source_ids": [source],
                "destination_ids": [dest],
            },
        ),
        201,
    )["id"]
    expect(send(a, "PATCH", f"/routes/{route}", {"is_enabled": True}), 200)
    content = io.BytesIO()
    image = Image.frombytes("RGB", (480, 480), random.Random(42).randbytes(480 * 480 * 3))
    image.save(content, format="PNG")
    media_bytes = content.getvalue()
    digest = hashlib.sha256(media_bytes).hexdigest()
    source_key = f"tenants/{tenant_a}/media/{digest}"
    backend.put_object(source_key, media_bytes)
    with psycopg.connect(db) as conn:
        asset = conn.execute(
            """
            INSERT INTO app.media_assets (
                id,tenant_id,workspace_id,media_kind,origin_kind,s3_object_key,
                sha256_hex,mime_type,byte_size
            )
            VALUES (gen_random_uuid(),%s,%s,'IMAGE','USER_UPLOAD',%s,%s,'image/png',%s)
            RETURNING id
        """,
            (tenant_a, source_ws, source_key, digest, len(media_bytes)),
        ).fetchone()[0]
    expect(
        send(
            a,
            "POST",
            "/drafts",
            {
                "body_text": "نسخه دارای رسانه",
                "media_asset_ids": [str(asset)],
            },
        ),
        201,
    )
    configure(b)
    grant(b, "OPENAI_TEXT")
    data = {
        "backup_password": "synthetic-restore-password",
        "backup_scope": "TENANT_FULL_LOGICAL",
        "include_media_assets": True,
        "includes_reencrypted_secrets": include_secrets,
    }
    backup = expect(send(a, "POST", "/backups", data), 200)
    assert backup["bundle_base64"] is None
    manifest_id = backup["manifest_id"]
    preview = {
        "backup_password": data["backup_password"],
        "mode": "PREVIEW",
        "manifest_id": manifest_id,
    }
    assert expect(send(a, "POST", "/backups/restore", preview), 200)["media_count"] == 1
    expect(send(b, "POST", "/backups/restore", preview), 422)
    archived = b"".join(
        backend.stream_object_chunks(f"tenants/{tenant_a}/backups/{manifest_id}.rfbak")
    )
    # Transport an authenticated archive using a temporary destination-owned manifest, as a
    # direct S3 transfer fixture. An inline upload remains subject to its smaller API limit.
    with psycopg.connect(db) as conn:
        imported = conn.execute(
            """
            INSERT INTO app.backup_manifests
              (id,tenant_id,backup_scope,cipher_alg,archive_checksum_sha256,byte_size,status)
            VALUES (gen_random_uuid(),%s,'TENANT_FULL_LOGICAL','AES_256_GCM',%s,%s,'COMPLETED')
            RETURNING id
        """,
            (tenant_b, hashlib.sha256(archived).hexdigest(), len(archived)),
        ).fetchone()[0]
    backend.put_object(f"tenants/{tenant_b}/backups/{imported}.rfbak", archived)
    restore = preview | {"manifest_id": str(imported)}
    assert expect(send(b, "POST", "/backups/restore", restore), 200)["table_counts"]["routes"] == 1
    for _ in range(2):
        expect(send(b, "POST", "/backups/restore", restore | {"mode": "APPLY"}), 200)
    restored_key = f"tenants/{tenant_b}/media/{digest}"
    assert b"".join(backend.stream_object_chunks(restored_key)) == media_bytes
    with psycopg.connect(db, row_factory=psycopg.rows.dict_row) as conn:
        result = conn.execute(
            """
            SELECT d.id,d.is_enabled,d.publish_verified_at,s.is_revoked,s.masked_hint,
                s.id AS secret_id,s.secret_kind AS kind,s.nonce_bytes AS nonce,
                s.ciphertext_aead AS ciphertext,s.dek_version AS key_version,
                k.wrapped_dek_ciphertext AS wrapped_dek,k.key_state,s.tenant_id,s.workspace_id
            FROM app.destinations d JOIN app.secrets_vault s ON s.id=d.secret_id
            JOIN app.tenant_encryption_keys k ON k.tenant_id=s.tenant_id
                AND k.key_version=s.dek_version
            WHERE d.workspace_id=%s
        """,
            (target_ws,),
        ).fetchall()
        assert len(result) == 1
        restored = result[0]
        assert restored["is_enabled"] is False and restored["publish_verified_at"] is None
        assert restored["is_revoked"] is (not include_secrets)
        if include_secrets:
            vault_args = {
                name: restored[name]
                for name in (
                    "tenant_id",
                    "workspace_id",
                    "secret_id",
                    "kind",
                    "nonce",
                    "ciphertext",
                    "key_version",
                    "wrapped_dek",
                    "key_state",
                    "is_revoked",
                )
            }
            assert settings.VAULT.decrypt_secret(**vault_args) == token
        else:
            assert restored["masked_hint"] == "[REKEY_REQUIRED]"
        assert (
            conn.execute(
                """
            SELECT count(*) AS total FROM app.drafts d JOIN app.draft_revisions r ON r.draft_id=d.id
            JOIN app.media_asset_refs ref ON ref.owner_entity_id=r.id
            JOIN app.media_assets m ON m.id=ref.media_asset_id
            WHERE d.workspace_id=%s AND m.workspace_id=d.workspace_id AND m.s3_object_key=%s
        """,
                (target_ws, restored_key),
            ).fetchone()["total"]
            == 1
        )
        assert conn.execute(
            """
            SELECT w.emergency_stop,s.allow_external_ai,
                   (SELECT bool_or(is_enabled) FROM app.routes WHERE workspace_id=w.id) AS route,
                   (SELECT bool_or(is_granted) FROM app.external_consents
                    WHERE workspace_id=w.id) AS consent
            FROM app.workspaces w JOIN app.workspace_settings s ON s.workspace_id=w.id
            WHERE w.id=%s
        """,
            (target_ws,),
        ).fetchone() == {
            "emergency_stop": True,
            "allow_external_ai": False,
            "route": False,
            "consent": False,
        }
    if not include_secrets:
        replacement = expect(
            send(
                b,
                "POST",
                "/security/secrets",
                {
                    "secret_kind": "TELEGRAM_BOT_TOKEN",
                    "label": "Replacement",
                    "raw_secret": token,
                },
            ),
            201,
        )["id"]
        expect(send(b, "PATCH", f"/destinations/{restored['id']}", {"secret_id": replacement}), 200)
        expect(send(b, "PATCH", f"/destinations/{restored['id']}", {"is_enabled": True}), 409)
