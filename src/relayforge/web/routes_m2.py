import hmac
import json
import re
import secrets
from datetime import UTC, datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from django.conf import settings
from django.db import connections, transaction
from django.http import JsonResponse
from ninja import Router

from relayforge.adapters.content_store import advance_cursor, bot_post, insert_messages
from relayforge.adapters.disk_pressure import disk_state
from relayforge.adapters.messengers import CONNECTORS, MessengerError
from relayforge.adapters.s3_storage import (
    EXPORT_TTL,
    MAX_EXPORT_BYTES,
    S3Storage,
    StorageError,
    export_key,
)
from relayforge.core.security import (
    SecretRevokedError,
    TenantKeyShreddedError,
    scrub_sensitive_text,
)
from relayforge.web import schemas_m2 as dto
from relayforge.web.db import audit, first, lock_tenant, rows, set_context
from relayforge.web.endpoints import ack, page, require_permission, require_workspace
from relayforge.web.middleware import sha256
from relayforge.web.problems import ProblemError, problem

router = Router()
SECRET_KINDS = dict(zip(CONNECTORS, (
    "TELEGRAM_BOT_TOKEN", "BALE_BOT_TOKEN", "EITAA_API_TOKEN", "RUBIKA_BOT_TOKEN",
    "SOROUSH_BOT_TOKEN",
), strict=True))
SOURCE_PLATFORMS = {"TELEGRAM_BOT_CHANNEL": "TELEGRAM", "BALE_BOT_CHANNEL": "BALE"}
DEST_COLUMNS = """
    id, platform, maturity_tier, canonical_chat_id, display_name, secret_id, backlog_mode,
    is_enabled, is_archived, publish_verified_at, capabilities_json, brand_identity_id
"""


def storage():
    try:
        return S3Storage.from_environment()
    except (KeyError, ValueError):
        raise ProblemError(503, "EXPORT_STORAGE_UNAVAILABLE") from None


def scoped_source(workspace, identifier, *, for_update=False):
    query = ("SELECT id, tenant_id, workspace_id, source_kind, canonical_channel_id, secret_id "
             "FROM app.sources WHERE workspace_id = %s AND id = %s AND is_active")
    row = first(query + (" FOR UPDATE" if for_update else ""), (workspace, identifier))
    if row is None:
        raise ProblemError(404)
    return row


def scoped_destination(workspace, identifier, *, for_update=False):
    query = (
        f"SELECT {DEST_COLUMNS} FROM app.destinations "
        "WHERE workspace_id = %s AND id = %s AND NOT is_archived"
    )
    row = first(query + (" FOR UPDATE" if for_update else ""), (workspace, identifier))
    if row is None:
        raise ProblemError(404)
    return row


def read_secret(workspace, identifier, kind):
    row = first("""
        SELECT s.tenant_id, s.workspace_id, s.id AS secret_id, s.secret_kind AS kind,
            s.nonce_bytes AS nonce, s.ciphertext_aead AS ciphertext, s.dek_version AS key_version,
            s.is_revoked, k.wrapped_dek_ciphertext AS wrapped_dek, k.key_state
        FROM app.secrets_vault s
        JOIN app.tenant_encryption_keys k
          ON k.tenant_id = s.tenant_id AND k.key_version = s.dek_version
        WHERE s.workspace_id = %s AND s.id = %s AND s.secret_kind = %s AND NOT s.is_revoked
    """, (workspace, identifier, kind))
    if row is None:
        raise ProblemError(404)
    return row


def credentials(workspace, identifier, kind):
    row = read_secret(workspace, identifier, kind)
    try:
        return settings.VAULT.decrypt_secret(**row)
    except (SecretRevokedError, TenantKeyShreddedError):
        raise ProblemError(404) from None


def get_idempotency_token(request):
    try:
        return UUID(request.headers.get("Idempotency-Key", ""))
    except ValueError:
        raise ProblemError(422, "IDEMPOTENCY_KEY_REQUIRED") from None


def ensure_limit(limit):
    if not 1 <= limit <= 100:
        raise ProblemError(422)


def archive_job(request, workspace, source, job_id):
    row = first("""
        SELECT id, tenant_id, workspace_id, source_id, s3_object_key, file_size_bytes, job_mode,
               status, created_at, raw_deleted_at
        FROM app.export_import_jobs WHERE workspace_id = %s AND source_id = %s AND id = %s
        FOR UPDATE
    """, (workspace, source, job_id))
    if not row or row["tenant_id"] != request.tenant["tenant_id"]:
        raise ProblemError(404)
    return row


@router.post("/webhooks/inbound/{platform}/{webhookToken}", auth=None)
def receive_webhook(request, platform: str, webhookToken: str):
    if platform not in {"telegram", "bale"} or not re.fullmatch(
        r"[A-Za-z0-9_-]{32,128}", webhookToken
    ):
        raise ProblemError(404)
    with transaction.atomic():
        resolve = "SELECT * FROM app.fn_resolve_inbound_webhook_source(%s, %s)"
        params = (platform, sha256(webhookToken))
        source = first(resolve, params)
        if not source:
            raise ProblemError(404)
        lock_tenant(source["tenant_id"])
        if first(resolve, params) != source:
            raise ProblemError(404)
        set_context(source["tenant_id"])
        expected = credentials(source["workspace_id"], source["secret_id"],
                               "WEBHOOK_SIGNING_SECRET") if source["secret_id"] else webhookToken
        presented = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if platform == "bale":
            presented = request.headers.get("X-Bale-Bot-Api-Secret-Token", presented)
        if not presented or not hmac.compare_digest(presented.encode(), expected.encode()):
            raise ProblemError(403)
        try:
            payload = json.loads(request.body)
            if not isinstance(payload, dict):
                raise ValueError
            post = payload.get("channel_post")
            if post is None:
                return ack(request)
            if not isinstance(post, dict) or not isinstance(chat := post.get("chat"), dict):
                raise ValueError
            if (
                str(chat.get("id")) != source["canonical_channel_id"]
                or chat.get("type") != "channel"
            ):
                raise ProblemError(403)
            normalized = bot_post(post)
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise ProblemError(422) from None
        if normalized:
            with connections["default"].cursor() as cursor:
                insert_messages(cursor, source, "LIVE_BOT_UPDATE", "INGEST_PIPELINE", [normalized])
                advance_cursor(cursor, source, normalized.external_id, normalized.published_at)
    return ack(request)


@router.get("/workspaces/{wsId}/sources")
def sources(request, wsId: UUID, limit: int = 50, cursor: UUID | None = None):
    require_workspace(wsId)
    ensure_limit(limit)
    return page(rows("""
        SELECT id, source_kind, canonical_channel_id, display_title, is_active,
               republish_rights_attested, last_health_status, last_checked_at, webhook_platform
        FROM app.sources WHERE workspace_id = %s AND (%s::uuid IS NULL OR id > %s)
        ORDER BY id LIMIT %s
    """, (wsId, cursor, cursor, limit + 1)), limit)


@router.post("/workspaces/{wsId}/sources")
def add_source(request, wsId: UUID, data: dto.SourceInput):
    require_permission(request, "manage_workspaces")
    require_workspace(wsId)
    source_id = uuid4()
    webhook = data.source_kind == "WEBHOOK_INBOUND"
    if webhook != bool(data.webhook_platform):
        raise ProblemError(422)
    platform = SOURCE_PLATFORMS.get(data.source_kind)
    if webhook or data.source_kind == "TELEGRAM_DESKTOP_EXPORT":
        number = r"-100[0-9]+" if data.webhook_platform != "bale" else r"-?[0-9]+"
        if not re.fullmatch(number, data.canonical_channel_id):
            raise ProblemError(422)
    if platform and not data.secret_id:
        raise ProblemError(422)
    if data.secret_id:
        if not webhook and not platform:
            raise ProblemError(422)
        read_secret(wsId, data.secret_id, "WEBHOOK_SIGNING_SECRET" if webhook else
                    SECRET_KINDS[platform])
    token = secrets.token_urlsafe(32) if webhook else None
    rows("""
        INSERT INTO app.sources (id, tenant_id, workspace_id, source_kind, canonical_channel_id,
            display_title, secret_id, republish_rights_attested, webhook_platform,
            webhook_token_sha256)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (source_id, request.tenant["tenant_id"], wsId, data.source_kind, data.canonical_channel_id,
          data.display_title, data.secret_id, data.republish_rights_attested, data.webhook_platform,
          sha256(token) if token else None))
    audit(request, "SOURCE_CREATED", "SOURCE", source_id, workspace=wsId)
    output = ack(request, identifier=source_id)
    if token:
        output.update(webhook_token=token)
    return JsonResponse(output, status=201)


def _messenger_problem(request, exc):
    statuses = {"RATE_LIMIT_429": 429, "QUOTA_EXHAUSTED": 409, "AUTH_REVOKED": 403,
                "PERMISSION_DENIED": 403, "INCOMPATIBLE_PAYLOAD": 422}
    return problem(request, statuses.get(exc.category, 503), code=exc.category,
                   retry_after=exc.retry_after_sec)


@router.post("/workspaces/{wsId}/sources/{sourceId}/test")
def test_source(request, wsId: UUID, sourceId: UUID):
    require_permission(request, "manage_workspaces")
    source = scoped_source(wsId, sourceId, for_update=True)
    platform = SOURCE_PLATFORMS.get(source["source_kind"])
    if not platform:
        raise ProblemError(422)
    token = credentials(wsId, source["secret_id"], SECRET_KINDS[platform])
    try:
        info = CONNECTORS[platform]().resolve_and_verify_source_channel(
            token, source["canonical_channel_id"],
        )
    except MessengerError as exc:
        health = {"AUTH_REVOKED": "AUTH_ERROR", "PERMISSION_DENIED": "AUTH_ERROR",
                  "RATE_LIMIT_429": "RATE_LIMITED"}.get(exc.category, "UNREACHABLE")
        # HTTP 200 represents the completed diagnostic, whose result can be unhealthy.
        info = {"status": health, "error_code": exc.category}
    else:
        info["status"] = "HEALTHY"
        info["display_title"] = scrub_sensitive_text(info["display_title"], (token,))
    rows("""
        UPDATE app.sources SET canonical_channel_id = COALESCE(%s, canonical_channel_id),
            display_title = COALESCE(%s, display_title), last_health_status = %s,
            last_checked_at = clock_timestamp() WHERE id = %s
    """, (info.get("canonical_channel_id"), info.get("display_title"), info["status"], sourceId))
    audit(request, "SOURCE_CONNECTION_TESTED", "SOURCE", sourceId, workspace=wsId)
    return ack(request, identifier=sourceId, **info)


@router.post("/workspaces/{wsId}/sources/{sourceId}/exports/presign")
def export_presign(request, wsId: UUID, sourceId: UUID, data: dto.ExportInput):
    if disk_state() == "CRITICAL":
        raise ProblemError(503, "DISK_PRESSURE_CRITICAL")
    require_permission(request, "manage_workspaces")
    source = scoped_source(wsId, sourceId)
    if source["source_kind"] != "TELEGRAM_DESKTOP_EXPORT":
        raise ProblemError(422)
    idempotency = get_idempotency_token(request)
    tenant = request.tenant["tenant_id"]
    job_id = uuid5(NAMESPACE_URL, f"relayforge:export:{tenant}:{idempotency}")
    backend = storage()
    key = export_key(tenant, job_id)
    created = first("""
        INSERT INTO app.export_import_jobs (id, tenant_id, workspace_id, source_id, s3_object_key,
            file_size_bytes, job_mode)
        VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING RETURNING id
    """, (job_id, tenant, wsId, sourceId, key, data.file_size_bytes, data.job_mode))
    job = first("""
        SELECT workspace_id, source_id, file_size_bytes, job_mode, status, created_at
        FROM app.export_import_jobs WHERE id = %s FOR UPDATE
    """, (job_id,))
    expected = (wsId, sourceId, data.file_size_bytes, data.job_mode)
    if not job or tuple(job[key] for key in (
        "workspace_id", "source_id", "file_size_bytes", "job_mode",
    )) != expected:
        raise ProblemError(409, "IDEMPOTENCY_KEY_REUSED")
    if job["status"] != "UPLOADING" or (datetime.now(UTC) - job["created_at"]).total_seconds() >= (
        EXPORT_TTL
    ):
        raise ProblemError(409, "EXPORT_UPLOAD_CLOSED")
    fields = backend.generate_export_presigned_post(tenant, job_id, issued_at=job["created_at"])
    if created:
        audit(request, "EXPORT_UPLOAD_PRESIGNED", "EXPORT", job_id, workspace=wsId)
    return JsonResponse({"export_job_id": job_id, **fields}, status=201)


@router.post("/workspaces/{wsId}/sources/{sourceId}/exports/{exportJobId}/complete")
def export_complete(request, wsId: UUID, sourceId: UUID, exportJobId: UUID):
    require_permission(request, "manage_workspaces")
    source = scoped_source(wsId, sourceId)
    if source["source_kind"] != "TELEGRAM_DESKTOP_EXPORT":
        raise ProblemError(422)
    job = archive_job(request, wsId, sourceId, exportJobId)
    if job["status"] in {"PARSING", "COMPLETED"}:
        return JsonResponse(ack(request, identifier=exportJobId, status=job["status"]), status=202)
    if job["status"] != "UPLOADING":
        raise ProblemError(409)
    backend = storage()
    key = export_key(job["tenant_id"], exportJobId)
    if key != job["s3_object_key"]:
        raise ProblemError(409)
    try:
        head = backend.head_object(key)
    except StorageError:
        raise ProblemError(503, "EXPORT_STORAGE_UNAVAILABLE") from None
    if head is None:
        raise ProblemError(409, "EXPORT_OBJECT_MISSING")
    size, etag = head
    if not 1 <= size <= MAX_EXPORT_BYTES or size != job["file_size_bytes"]:
        raise ProblemError(422, "EXPORT_OBJECT_SIZE_MISMATCH")
    if not etag or not re.fullmatch(r'"[A-Za-z0-9-]{1,128}"', etag):
        raise ProblemError(503, "EXPORT_OBJECT_ETAG_MISSING")
    rows("""
        UPDATE app.export_import_jobs SET status = 'PARSING', s3_object_etag = %s WHERE id = %s
    """, (etag, exportJobId))
    audit(request, "EXPORT_UPLOAD_COMPLETED", "EXPORT", exportJobId, workspace=wsId)
    return JsonResponse(ack(request, identifier=exportJobId, status="PARSING"), status=202)


@router.get("/workspaces/{wsId}/content-items")
def content_items(request, wsId: UUID, limit: int = 50, cursor: UUID | None = None,
                  q: str | None = None, access_class: Literal[
                      "PROCESSABLE", "REFERENCE_ONLY", "PROTECTED_NO_FORWARD"
                  ] | None = None, operation_scope: Literal[
                      "INGEST_PIPELINE", "ANALYSIS_ISOLATED", "MANUAL_INPUT"
                  ] = "INGEST_PIPELINE"):
    require_workspace(wsId)
    ensure_limit(limit)
    if q is not None and len(q) > 200:
        raise ProblemError(422)
    return page(rows("""
        SELECT ci.id, ci.source_id, ci.external_message_id, ci.canonical_channel_id,
            ci.origin_type, ci.operation_scope, ci.access_class, ci.source_published_at,
            cir.plain_text, cir.entities_ast_json AS spans_utf16
        FROM app.content_items ci
        JOIN app.content_item_revisions cir ON cir.tenant_id = ci.tenant_id
            AND cir.content_item_id = ci.id
        WHERE ci.workspace_id = %s AND ci.operation_scope = %s
          AND (%s::text IS NULL OR ci.access_class = %s)
          AND (%s::uuid IS NULL OR ci.id > %s)
          AND (%s::text IS NULL OR cir.fts_vector @@ plainto_tsquery('simple', %s))
          AND NOT EXISTS (
              SELECT 1 FROM app.content_item_revisions newer WHERE newer.tenant_id = ci.tenant_id
                AND newer.content_item_id = ci.id AND newer.revision_no > cir.revision_no
          )
        ORDER BY ci.id LIMIT %s
    """, (wsId, operation_scope, access_class, access_class, cursor, cursor, q, q, limit + 1)),
        limit)
