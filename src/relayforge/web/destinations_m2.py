import hashlib
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from django.db import connections
from django.http import JsonResponse
from ninja import Router
from psycopg.types.json import Jsonb

from relayforge.adapters.messengers import CONNECTORS, MessengerError
from relayforge.core.rich_text import PROFILES, CanonicalRichText, compile_rich_text
from relayforge.web import schemas_m2 as dto
from relayforge.web.db import audit, first, rows
from relayforge.web.endpoints import ack, page, require_permission, require_workspace
from relayforge.web.problems import ProblemError
from relayforge.web.publications_common import get_brand
from relayforge.web.routes_m2 import (
    DEST_COLUMNS,
    SECRET_KINDS,
    _messenger_problem,
    credentials,
    ensure_limit,
    get_idempotency_token,
    read_secret,
    scoped_destination,
)

router = Router()


@router.get("/workspaces/{wsId}/destinations")
def destinations(request, wsId: UUID, limit: int = 50, cursor: UUID | None = None):
    require_workspace(wsId)
    ensure_limit(limit)
    return page(rows(f"""
        SELECT {DEST_COLUMNS}
        FROM app.destinations WHERE workspace_id = %s AND NOT is_archived
          AND (%s::uuid IS NULL OR id > %s) ORDER BY id LIMIT %s
    """, (wsId, cursor, cursor, limit + 1)), limit)


@router.post("/workspaces/{wsId}/destinations")
def add_destination(request, wsId: UUID, data: dto.DestinationInput):
    require_permission(request, "manage_workspaces")
    require_workspace(wsId)
    read_secret(wsId, data.secret_id, SECRET_KINDS[data.platform])
    get_brand(wsId, data.brand_identity_id)
    connector, profile = CONNECTORS[data.platform], PROFILES[data.platform]
    capabilities = {"native_albums": profile.native_albums,
                    "supported_spans": sorted(profile.supported_spans),
                    "max_text_length": profile.max_text_length,
                    "max_caption_length": profile.max_caption_length,
                    "max_metadata_parts": profile.max_metadata_parts}
    identifier = uuid4()
    rows("""
        INSERT INTO app.destinations (id, tenant_id, workspace_id, platform, maturity_tier,
            canonical_chat_id, display_name, secret_id, backlog_mode, capabilities_json,
             brand_identity_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (identifier, request.tenant["tenant_id"], wsId, data.platform, connector.platform_tier,
          data.canonical_chat_id, data.display_name, data.secret_id, data.backlog_mode,
          Jsonb(capabilities), data.brand_identity_id))
    audit(request, "DESTINATION_CREATED", "DESTINATION", identifier, workspace=wsId)
    return JsonResponse(ack(request, identifier=identifier), status=201)


@router.patch("/workspaces/{wsId}/destinations/{destId}")
def patch_destination(request, wsId: UUID, destId: UUID, data: dto.DestinationPatch):
    require_permission(request, "manage_workspaces")
    destination = scoped_destination(wsId, destId, for_update=True)
    get_brand(wsId, data.brand_identity_id)
    rekey = data.secret_id is not None and data.secret_id != destination["secret_id"]
    if rekey:
        read_secret(wsId, data.secret_id, SECRET_KINDS[destination["platform"]])
    if data.is_enabled and (
        rekey or data.is_archived or destination["publish_verified_at"] is None
    ):
        raise ProblemError(409, "DESTINATION_PUBLISH_NOT_VERIFIED")
    rows("""
        UPDATE app.destinations SET display_name = COALESCE(%s, display_name),
            backlog_mode = COALESCE(%s, backlog_mode), is_archived = COALESCE(%s, is_archived),
            is_enabled = CASE WHEN %s THEN false ELSE COALESCE(%s, is_enabled) END,
            secret_id = COALESCE(%s, secret_id),
            publish_verified_at = CASE WHEN %s THEN NULL ELSE publish_verified_at END,
            brand_identity_id = CASE WHEN %s THEN %s ELSE brand_identity_id END,
            updated_at = clock_timestamp() WHERE id = %s
    """, (data.display_name, data.backlog_mode, data.is_archived, bool(data.is_archived) or rekey,
          data.is_enabled, data.secret_id, rekey, "brand_identity_id" in data.model_fields_set,
          data.brand_identity_id, destId))
    audit(request, "DESTINATION_UPDATED", "DESTINATION", destId, workspace=wsId)
    return ack(request, identifier=destId)


@router.post("/workspaces/{wsId}/destinations/{destId}/purge-queue")
def purge_queue(request, wsId: UUID, destId: UUID):
    require_permission(request, "manage_workspaces")
    scoped_destination(wsId, destId, for_update=True)
    count = first("""
        WITH cancelled AS (
            UPDATE app.delivery_tasks SET status = 'CANCELLED', lease_epoch = lease_epoch + 1,
                updated_at = clock_timestamp()
            WHERE workspace_id = %s AND destination_id = %s
              AND status IN ('PENDING', 'SCHEDULED', 'READY', 'RETRY_WAIT') RETURNING id
        ) SELECT count(*) AS total FROM cancelled
    """, (wsId, destId))["total"]
    audit(request, "DESTINATION_QUEUE_PURGED", "DESTINATION", destId, workspace=wsId)
    return ack(request, identifier=destId, cancelled_count=count)


@router.post("/workspaces/{wsId}/destinations/{destId}/test-connection")
def test_connection(request, wsId: UUID, destId: UUID):
    require_permission(request, "manage_workspaces")
    destination = scoped_destination(wsId, destId)
    platform = destination["platform"]
    token = credentials(wsId, destination["secret_id"], SECRET_KINDS[platform])
    try:
        info = CONNECTORS[platform]().test_connection(token)
    except MessengerError as exc:
        return _messenger_problem(request, exc)
    audit(request, "DESTINATION_CONNECTION_TESTED", "DESTINATION", destId, workspace=wsId)
    return ack(request, identifier=destId, **info)


def publish_record(request, ws_id, dest_id, marker, action, details):
    """Commit send claims and receipts independently of the HTTP transaction."""
    connection = connections["default"].copy(alias="test_publish_ledger")
    try:
        connection.set_autocommit(False)
        with connection.cursor() as cursor:
            context = request.tenant
            cursor.execute(
                "SELECT set_config('app.tenant_id', %s, true), "
                "set_config('app.user_id', %s, true)",
                (str(context["tenant_id"]), str(context["user_id"])),
            )
            cursor.execute("""
                INSERT INTO app.audit_events (id, tenant_id, workspace_id, actor_user_id,
                    actor_role, action_code, target_type, target_id, correlation_id,
                    details_safe_json)
                VALUES (%s, %s, %s, %s, %s, %s, 'DESTINATION', %s, %s, %s)
                ON CONFLICT (id) DO NOTHING RETURNING id
            """, (marker, context["tenant_id"], ws_id, context["user_id"], context["role"],
                  action, str(dest_id), request.correlation_id, Jsonb(details)))
            inserted = cursor.fetchone() is not None
        connection.commit()
        return inserted
    finally:
        connection.close()


@router.post("/workspaces/{wsId}/destinations/{destId}/test-publish")
def test_publish(request, wsId: UUID, destId: UUID, data: dto.TestPublishInput):
    require_permission(request, "publish")
    destination = scoped_destination(wsId, destId, for_update=True)
    tenant = request.tenant["tenant_id"]
    key = get_idempotency_token(request)
    seed = f"relayforge:test-publish:{tenant}:{key}"
    claim, done = (uuid5(NAMESPACE_URL, f"{seed}:{phase}") for phase in ("claim", "receipt"))
    text_hash = hashlib.sha256(data.text.encode()).hexdigest()
    platform = destination["platform"]
    token = credentials(wsId, destination["secret_id"], SECRET_KINDS[platform])
    compilation = compile_rich_text(CanonicalRichText(data.text), platform)
    if compilation.length_overflow or not data.text.strip():
        raise ProblemError(422)
    if publish_record(request, wsId, destId, claim, "DESTINATION_TEST_PUBLISH_REQUESTED",
                      {"text_sha256": text_hash}):
        try:
            receipt = CONNECTORS[platform]().test_publish(
                token, destination["canonical_chat_id"], compilation,
                explicit_user_confirmation=data.explicit_user_confirmation,
            )
        except MessengerError as exc:
            if exc.category != "TRANSIENT_NETWORK":
                publish_record(request, wsId, destId, done, "DESTINATION_TEST_PUBLISH_REJECTED", {
                    "error_code": exc.category, "retry_after_sec": exc.retry_after_sec,
                })
            return _messenger_problem(request, exc)
        remote = receipt.remote_message_id
        publish_record(request, wsId, destId, done, "DESTINATION_TEST_PUBLISHED", {
            "remote_message_id": remote,
        })
    else:
        prior = first("SELECT target_id, details_safe_json FROM app.audit_events WHERE id = %s",
                      (claim,))
        if not prior or prior["target_id"] != str(destId) or (
            prior["details_safe_json"].get("text_sha256") != text_hash
        ):
            raise ProblemError(409, "IDEMPOTENCY_KEY_REUSED")
        receipt = first("SELECT details_safe_json FROM app.audit_events WHERE id = %s", (done,))
        if receipt is None:
            raise ProblemError(409, "TEST_PUBLISH_OUTCOME_UNCERTAIN")
        result = receipt["details_safe_json"]
        if "error_code" in result:
            return _messenger_problem(request, MessengerError(
                result["error_code"], retry_after_sec=result.get("retry_after_sec"),
            ))
        remote = result["remote_message_id"]
    rows("""
        UPDATE app.destinations SET publish_verified_at = COALESCE(
            publish_verified_at, clock_timestamp()
        ), updated_at = clock_timestamp() WHERE id = %s
    """, (destId,))
    return ack(request, identifier=destId, remote_message_id=remote, publish_verified=True)
