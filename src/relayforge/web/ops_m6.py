import csv
import io
import json
from typing import Literal
from uuid import UUID, uuid4, uuid5

from django.conf import settings
from django.http import HttpResponse
from ninja import Router, Schema
from psycopg.types.json import Jsonb
from pydantic import Field, SecretStr

from relayforge.adapters.disk_pressure import disk_state
from relayforge.adapters.s3_storage import StorageError, export_key
from relayforge.adapters.tenant_backup import (
    MAX_BYTES,
    decode,
    encode,
    open_bundle,
    packed,
    seal,
    sha,
)
from relayforge.core.security import SecretKind, mask_secret_preview, scrub_sensitive_text
from relayforge.web.ai_m4 import budgets
from relayforge.web.db import audit, first, rows
from relayforge.web.delivery_m3 import emergency_stop
from relayforge.web.endpoints import require_permission, require_workspace
from relayforge.web.logical_m7 import apply_logical, inspect_entries, logical_export
from relayforge.web.problems import ProblemError
from relayforge.web.routes_m2 import credentials, get_idempotency_token, storage
from relayforge.web.schemas_m3 import StopInput

router = Router()
ROOT = "/workspaces/{wsId}"
CONFIG = "config/workspace.json"
SECRETS = "secrets/tenant_vault_rekeyed.aead"


class BackupInput(Schema):
    backup_password: SecretStr = Field(min_length=12, max_length=256)
    includes_reencrypted_secrets: bool = False
    backup_scope: Literal["TENANT_SETTINGS_EXPORT", "TENANT_FULL_LOGICAL"] = (
        "TENANT_SETTINGS_EXPORT"
    )
    include_media_assets: bool = False


class RestoreInput(BackupInput):
    mode: Literal["PREVIEW", "APPLY"]
    bundle_base64: str | None = Field(default=None, max_length=700000)
    manifest_id: UUID | None = None


def alert(tenant, workspace, key, severity, title, body):
    rows(
        """
        INSERT INTO app.system_notifications
            (id,tenant_id,workspace_id,dedup_key,severity,title_fa,body_fa)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (tenant_id,workspace_id,dedup_key) WHERE NOT is_read DO UPDATE SET
          occurrence_count=app.system_notifications.occurrence_count+1,
          last_seen_at=clock_timestamp(), severity=excluded.severity
    """,
        (uuid4(), tenant, workspace, key, severity, title, body),
    )


@router.get(ROOT + "/dashboard")
def dashboard(request, wsId: UUID):
    require_workspace(wsId)
    counts = first(
        """
        SELECT (SELECT count(*) FROM app.sources WHERE workspace_id=%(ws)s AND is_active)
          AS sources, (SELECT count(*) FROM app.destinations
              WHERE workspace_id=%(ws)s AND NOT is_archived) AS destinations,
          (SELECT count(*) FROM app.routes WHERE workspace_id=%(ws)s AND is_enabled) AS routes,
          (SELECT count(*) FROM app.system_notifications WHERE workspace_id=%(ws)s AND NOT is_read)
              AS unread,
          (SELECT count(*) FROM app.media_jobs WHERE workspace_id=%(ws)s AND
              status IN ('PENDING','RUNNING')) AS active_media_jobs,
          (SELECT emergency_stop FROM app.workspaces WHERE id=%(ws)s) AS emergency_stop
    """,
        {"ws": wsId},
    )
    return {
        **counts,
        "disk_pressure": disk_state(),
        "engine_status": "STOPPED" if counts["emergency_stop"] else "ENABLED",
        "drafts": rows(
            """
            SELECT approval_state,count(*) AS total FROM app.drafts
            WHERE workspace_id=%s GROUP BY approval_state
        """,
            (wsId,),
        ),
        "deliveries": rows(
            """
            SELECT status,count(*) AS total FROM app.delivery_tasks
            WHERE workspace_id=%s GROUP BY status
        """,
            (wsId,),
        ),
        "budgets": budgets(request, wsId) if request.tenant["role"] in {"OWNER", "ADMIN"} else None,
    }


def paginated(workspace, limit, cursor, table, fields):
    if not 1 <= limit <= 100:
        raise ProblemError(422)
    found = rows(
        f"""
        SELECT {fields} FROM app.{table} WHERE workspace_id=%s
          AND (%s::uuid IS NULL OR id<%s) ORDER BY id DESC LIMIT %s
    """,
        (workspace, cursor, cursor, limit + 1),
    )
    return {
        "items": found[:limit],
        "next_cursor": found[limit - 1]["id"] if len(found) > limit else None,
    }


@router.get(ROOT + "/notifications")
def notifications(request, wsId: UUID, cursor: UUID | None = None, limit: int = 50):
    require_workspace(wsId)
    return paginated(
        wsId,
        limit,
        cursor,
        "system_notifications",
        "id,severity,title_fa,body_fa,is_read,occurrence_count,last_seen_at",
    )


@router.post(ROOT + "/notifications/{notificationId}/read")
def read_alert(request, wsId: UUID, notificationId: UUID):
    require_workspace(wsId)
    row = first(
        """
        UPDATE app.system_notifications SET is_read=true WHERE id=%s AND workspace_id=%s
        RETURNING id,is_read
    """,
        (notificationId, wsId),
    )
    if not row:
        raise ProblemError(404)
    return row


@router.get(ROOT + "/audit-events")
def audit_events(request, wsId: UUID, cursor: UUID | None = None, limit: int = 50):
    require_permission(request, "manage_workspaces")
    require_workspace(wsId)
    return paginated(
        wsId,
        limit,
        cursor,
        "audit_events",
        "id,action_code,target_type,target_id,actor_role,created_at,details_safe_json",
    )


@router.get(ROOT + "/reports/ai-usage")
def ai_usage(request, wsId: UUID):
    require_permission(request, "manage_workspaces")
    require_workspace(wsId)
    return {
        "items": rows(
            """
        SELECT e.provider_id,e.model_id,e.task_modality,b.route_id,b.cost_accuracy,
          count(*) AS executions,coalesce(sum(e.input_tokens),0) AS input_tokens,
          coalesce(sum(e.output_tokens),0) AS output_tokens,
          coalesce(sum(b.settled_micros),0) AS cost_micros
        FROM app.ai_executions e LEFT JOIN app.budget_reservations b
            ON b.id=e.reservation_id AND b.tenant_id=e.tenant_id
        WHERE e.workspace_id=%s GROUP BY 1,2,3,4,5 ORDER BY 1,2,3,4,5 LIMIT 1000
    """,
            (wsId,),
        )
    }


def csv_cell(value):
    raw = scrub_sensitive_text(str(value) if value is not None else "")
    return (
        "'" + raw
        if raw.startswith(("=", "+", "-", "@", "\t", "\r"))
        or (raw.lstrip().startswith(("=", "+", "-", "@")))
        else raw
    )


@router.get(ROOT + "/reports/export")
def report_export(request, wsId: UUID, format: Literal["json", "csv"] = "json"):
    ai = ai_usage(request, wsId)["items"]
    deliveries = rows(
        """
        SELECT status,count(*) AS total FROM app.delivery_tasks WHERE workspace_id=%s
        GROUP BY status ORDER BY status
    """,
        (wsId,),
    )
    output = io.StringIO(newline="")
    if format == "csv":
        writer = csv.writer(output)
        writer.writerow(("section", "metric", "value"))
        for section, records in (("AI", ai), ("DELIVERY", deliveries)):
            for row in records:
                for name, value in row.items():
                    writer.writerow(map(csv_cell, (section, name, value)))
        return HttpResponse(output.getvalue(), content_type="text/csv; charset=utf-8")
    report = json.dumps({"ai": ai, "deliveries": deliveries}, default=str, ensure_ascii=False)
    return HttpResponse(scrub_sensitive_text(report), content_type="application/json")


def create_backup(request, workspace, data, identifier):
    tenant = request.tenant["tenant_id"]
    config = first(
        """
        SELECT w.slug,w.timezone,s.settings_json FROM app.workspaces w
        JOIN app.workspace_settings s ON s.workspace_id=w.id WHERE w.id=%s
    """,
        (workspace,),
    )
    if not config:
        raise ProblemError(404)
    config["operation_snapshot"] = {
        "routes": rows(
            "SELECT id::text,is_enabled FROM app.routes WHERE workspace_id=%s ORDER BY id",
            (workspace,),
        ),
        "destinations": rows(
            "SELECT id::text,is_enabled FROM app.destinations WHERE workspace_id=%s ORDER BY id",
            (workspace,),
        ),
    }
    entries = {CONFIG: packed(config)}
    if data.includes_reencrypted_secrets:
        secrets = rows(
            """
            SELECT id,secret_kind,label FROM app.secrets_vault
            WHERE workspace_id=%s AND NOT is_revoked ORDER BY id LIMIT 129
        """,
            (workspace,),
        )
        if len(secrets) > 128:
            raise ProblemError(422, "BACKUP_TOO_LARGE")
        entries[SECRETS] = packed(
            [
                {
                    "id": str(item["id"]),
                    "secret_kind": item["secret_kind"],
                    "label": item["label"],
                    "value": credentials(workspace, item["id"], item["secret_kind"]),
                }
                for item in secrets
            ]
        )
    try:
        if data.backup_scope == "TENANT_FULL_LOGICAL":
            entries.update(logical_export(workspace, tenant, data.include_media_assets))
        manifest, bundle = seal(
            data.backup_password.get_secret_value(),
            tenant,
            config["slug"],
            entries,
            scope=data.backup_scope,
        )
    except ValueError:
        raise ProblemError(422, "BACKUP_TOO_LARGE") from None
    inline = len(bundle) <= 512 * 1024
    if not inline:
        try:
            storage().put_object(f"tenants/{tenant}/backups/{identifier}.rfbak", bundle)
        except StorageError:
            raise ProblemError(503, "BACKUP_STORAGE_UNAVAILABLE") from None
    rows(
        """
        INSERT INTO app.backup_manifests (id,tenant_id,backup_scope,cipher_alg,
          archive_checksum_sha256,byte_size,status,inline_archive)
        VALUES (%s,%s,%s,'AES_256_GCM',%s,%s,'COMPLETED',%s)
    """,
        (
            identifier,
            tenant,
            manifest["backup_scope"],
            sha(bundle),
            len(bundle),
            bundle if inline else None,
        ),
    )
    return {
        "manifest_id": identifier,
        "manifest": manifest,
        "bundle_base64": encode(bundle) if inline else None,
    }


@router.post(ROOT + "/backups")
def backup(request, wsId: UUID, data: BackupInput):
    require_permission(request, "manage_security")
    require_workspace(wsId)
    return create_backup(request, wsId, data, uuid4())


def stored_bundle(tenant, identifier):
    manifest = first(
        """
        SELECT inline_archive,archive_checksum_sha256,byte_size FROM app.backup_manifests
        WHERE id=%s AND tenant_id=%s AND status IN ('COMPLETED','VERIFIED')
    """,
        (identifier, tenant),
    )
    if not manifest or manifest["byte_size"] > MAX_BYTES:
        raise ValueError("INVALID_BACKUP")
    if manifest["inline_archive"] is not None:
        bundle = bytes(manifest["inline_archive"])
    else:
        key = f"tenants/{tenant}/backups/{identifier}.rfbak"
        content = bytearray()
        for chunk in storage().stream_object_chunks(key):
            content.extend(chunk)
            if len(content) > MAX_BYTES:
                raise ValueError("INVALID_BACKUP")
        bundle = bytes(content)
    if sha(bundle) != manifest["archive_checksum_sha256"]:
        raise ValueError("INVALID_BACKUP")
    return bundle


@router.post(ROOT + "/backups/restore")
def restore(request, wsId: UUID, data: RestoreInput):
    require_permission(request, "manage_security")
    require_workspace(wsId)
    tenant = request.tenant["tenant_id"]
    try:
        if (data.bundle_base64 is None) == (data.manifest_id is None):
            raise ValueError
        bundle = (
            decode(data.bundle_base64)
            if data.bundle_base64 is not None
            else stored_bundle(tenant, data.manifest_id)
        )
        manifest, entries = open_bundle(data.backup_password.get_secret_value(), bundle)
        records, media = {}, {}
        if manifest["backup_scope"] == "TENANT_FULL_LOGICAL":
            records, media = inspect_entries(
                {key: value for key, value in entries.items() if key not in {CONFIG, SECRETS}}
            )
        elif set(entries) - {CONFIG, SECRETS}:
            raise ValueError
        config = json.loads(entries[CONFIG])
        secrets = json.loads(entries.get(SECRETS, b"[]"))
        if not isinstance(config["settings_json"], dict) or not isinstance(secrets, list):
            raise ValueError
        if len(secrets) > 128 or any(
            not isinstance(item, dict)
            or set(item)
            not in ({"label", "secret_kind", "value"}, {"id", "label", "secret_kind", "value"})
            or item["secret_kind"] not in SecretKind
            or not isinstance(item["label"], str)
            or not 1 <= len(item["label"]) <= 120
            or not isinstance(item["value"], str)
            or not 1 <= len(item["value"]) <= 8192
            for item in secrets
        ):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ProblemError(422, "INVALID_BACKUP") from None
    impact = {
        "workspace_settings": 1,
        "table_counts": {name: len(values) for name, values in records.items()},
        "media_count": len(media),
        "secret_rekey_count": len(secrets),
        "safe_mode_enforced": True,
        "pitr_retention_disclosure_days": 14,
    }
    if data.mode == "PREVIEW":
        return impact
    tenant = request.tenant["tenant_id"]
    rows("SELECT pg_advisory_xact_lock(hashtextextended(%s,6))", (str(wsId),))
    key = first("""
        SELECT key_version,wrapped_dek_ciphertext FROM app.tenant_encryption_keys
        WHERE key_state='ACTIVE' ORDER BY key_version DESC LIMIT 1
    """)
    secret_map = {}
    for index, item in enumerate(secrets):
        if not key:
            raise ProblemError(409, "BACKUP_DESTINATION_KEY_REQUIRED")
        sid = item.get("id", str(index))
        identifier = uuid5(wsId, f"{sha(bundle)}:{sid}")
        if "id" in item:
            secret_map[str(UUID(sid))] = str(identifier)
        value = item["value"]
        nonce, encrypted = settings.VAULT.encrypt_secret(
            tenant,
            wsId,
            identifier,
            item["secret_kind"],
            value,
            key_version=key["key_version"],
            wrapped_dek=key["wrapped_dek_ciphertext"],
        )
        rows(
            """
            INSERT INTO app.secrets_vault
              (id,tenant_id,workspace_id,secret_kind,label,masked_hint,dek_version,
               ciphertext_aead,nonce_bytes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING
        """,
            (
                identifier,
                tenant,
                wsId,
                item["secret_kind"],
                item["label"],
                mask_secret_preview(value),
                key["key_version"],
                encrypted,
                nonce,
            ),
        )
    try:
        if records:
            apply_logical(wsId, tenant, records, media, secret_map)
    except (ValueError, StorageError):
        raise ProblemError(422, "LOGICAL_RESTORE_INCOMPATIBLE") from None
    disable_workspace(request, wsId)
    rows(
        """
        UPDATE app.workspace_settings SET settings_json=%s,allow_external_ai=false,
          allow_external_media_search=false,allow_feedback_learning=false WHERE workspace_id=%s
    """,
        (Jsonb(config["settings_json"]), wsId),
    )
    rows("UPDATE app.external_consents SET is_granted=false WHERE workspace_id=%s", (wsId,))
    identifier = uuid4()
    rows(
        """
        INSERT INTO app.backup_manifests (id,tenant_id,backup_scope,cipher_alg,
          archive_checksum_sha256,byte_size,status)
        VALUES (%s,%s,%s,'AES_256_GCM',%s,%s,'RESTORED')
    """,
        (identifier, tenant, manifest["backup_scope"], sha(bundle), len(bundle)),
    )
    audit(request, "BACKUP_RESTORED_SAFE_MODE", "WORKSPACE", wsId, workspace=wsId)
    return impact | {"manifest_id": identifier}


def disable_workspace(request, workspace):
    emergency_stop(request, workspace, StopInput(enabled=True, cancel_waiting=True))
    rows("UPDATE app.routes SET is_enabled=false WHERE workspace_id=%s", (workspace,))
    rows("UPDATE app.destinations SET is_enabled=false WHERE workspace_id=%s", (workspace,))


@router.post(ROOT + "/ops/reset")
def reset(request, wsId: UUID, data: BackupInput):
    require_permission(request, "manage_workspaces")
    if data.includes_reencrypted_secrets:
        require_permission(request, "manage_security")
    require_workspace(wsId)
    token = get_idempotency_token(request)
    identifier = uuid5(wsId, str(token))
    rows("SELECT pg_advisory_xact_lock(hashtextextended(%s,6))", (str(wsId),))
    prior = first("SELECT inline_archive FROM app.backup_manifests WHERE id=%s", (identifier,))
    if prior:
        try:
            bundle = stored_bundle(request.tenant["tenant_id"], identifier)
            manifest, _ = open_bundle(data.backup_password.get_secret_value(), bundle)
        except (TypeError, ValueError):
            raise ProblemError(409, "IDEMPOTENCY_KEY_REUSED") from None
        if (
            manifest["includes_reencrypted_secrets"] != data.includes_reencrypted_secrets
            or manifest["backup_scope"] != data.backup_scope
        ):
            raise ProblemError(409, "IDEMPOTENCY_KEY_REUSED")
        return {
            "manifest_id": identifier,
            "manifest": manifest,
            "bundle_base64": encode(bundle),
            "remote_deletion_performed": False,
            "replayed": True,
        }
    result = create_backup(request, wsId, data, identifier)
    disable_workspace(request, wsId)
    audit(request, "OPS_SAFE_RESET", "WORKSPACE", wsId, workspace=wsId)
    return result | {"remote_deletion_performed": False}


@router.post(ROOT + "/maintenance/cleanup")
def cleanup(request, wsId: UUID):
    require_permission(request, "manage_workspaces")
    require_workspace(wsId)
    tenant = request.tenant["tenant_id"]
    staging = rows(
        """
        SELECT id,s3_object_key FROM app.export_import_jobs WHERE workspace_id=%s
          AND raw_deleted_at IS NULL AND (
              status IN ('COMPLETED','FAILED','EXPIRED_CLEANED') OR
              (status='UPLOADING' AND created_at <= clock_timestamp()-interval '24 hours'))
        ORDER BY id LIMIT 50 FOR UPDATE SKIP LOCKED
    """,
        (wsId,),
    )
    backend = None
    removed = 0
    for item in staging:
        if item["s3_object_key"] != export_key(tenant, item["id"]):
            continue
        try:
            backend = backend or storage()
            backend.delete_object(item["s3_object_key"])
        except StorageError:
            raise ProblemError(503, "MEDIA_STORAGE_UNAVAILABLE") from None
        rows(
            """
            UPDATE app.export_import_jobs SET raw_deleted_at=clock_timestamp(),
                status=CASE WHEN status='UPLOADING' THEN 'EXPIRED_CLEANED' ELSE status END
            WHERE id=%s
        """,
            (item["id"],),
        )
        removed += 1
    candidates = rows(
        """
        SELECT id,s3_object_key FROM app.media_assets a
        WHERE workspace_id=%s AND NOT is_deleted
          AND created_at <= clock_timestamp()-interval '24 hours'
          AND NOT EXISTS (SELECT 1 FROM app.media_asset_refs r WHERE r.media_asset_id=a.id)
        ORDER BY s3_object_key,id LIMIT 50
    """,
        (wsId,),
    )
    count = 0
    for item in candidates:
        object_key, asset_id = item["s3_object_key"], item["id"]
        rows("SELECT pg_advisory_xact_lock(hashtextextended(%s,6))", (object_key,))
        if not first("SELECT id FROM app.media_assets WHERE id=%s FOR UPDATE", (asset_id,)):
            continue
        changed = first(
            """
            UPDATE app.media_assets a SET is_deleted=true
            WHERE a.id=%s AND NOT a.is_deleted
              AND NOT EXISTS (SELECT 1 FROM app.media_asset_refs r WHERE r.media_asset_id=a.id)
              AND NOT EXISTS (
                  SELECT 1 FROM app.draft_revisions r JOIN app.drafts d ON d.id=r.draft_id
                  WHERE d.approval_state<>'ARCHIVED' AND r.media_manifest_json @> %s::jsonb
              )
              AND NOT EXISTS (
                  SELECT 1 FROM app.podcast_scripts p WHERE p.synthesized_media_asset_id=a.id
              )
            RETURNING id
        """,
            (asset_id, json.dumps([str(asset_id)])),
        )
        if not changed:
            continue
        # Keep backing objects when another workspace still uses the same digest.
        if object_key.startswith(f"tenants/{tenant}/media/") and not first(
            """
            SELECT id FROM app.media_assets WHERE s3_object_key=%s AND NOT is_deleted LIMIT 1
        """,
            (object_key,),
        ):
            try:
                backend = backend or storage()
                backend.delete_object(object_key)
            except StorageError:
                raise ProblemError(503, "MEDIA_STORAGE_UNAVAILABLE") from None
        count += 1
    severity = disk_state()
    if severity != "NORMAL":
        alert(
            tenant,
            wsId,
            "DISK_PRESSURE",
            severity,
            "فضای ذخیره‌سازی محدود است",
            "فضای عملیاتی را بررسی کنید.",
        )
    audit(request, "OPS_CLEANUP", "WORKSPACE", wsId, workspace=wsId)
    return {"expired_exports_removed": removed, "unreferenced_assets_removed": count}
