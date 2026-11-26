import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from django.db import connections
from django.http import JsonResponse
from ninja import Router
from psycopg.types.json import Jsonb

from relayforge.adapters.publications import freeze_publication
from relayforge.core.rich_text import compile_rich_text, safe_link
from relayforge.core.rules import inject_brand_identity
from relayforge.core.scheduling import scheduled_for
from relayforge.web import schemas_m3 as dto
from relayforge.web.db import audit, first, rows
from relayforge.web.endpoints import ack, page, require_permission, require_workspace
from relayforge.web.problems import ProblemError
from relayforge.web.publications_common import (
    draft_record,
    event,
    get_brand,
    get_destinations,
    list_page,
    rich_text,
    save_revision,
)
from relayforge.web.routes_m2 import get_idempotency_token

router = Router()


def _validate_assets(workspace, assets):
    ids = [UUID(str(value)) for value in assets]
    found = rows("""
        SELECT id FROM app.media_assets WHERE workspace_id = %s AND id = ANY(%s)
          AND NOT is_deleted ORDER BY id FOR UPDATE
    """, (workspace, ids))
    if len(found) != len(ids):
        raise ProblemError(404)
    return [str(identifier) for identifier in ids]


@router.get("/workspaces/{wsId}/brands")
def brands(request, wsId: UUID, limit: int = 50, cursor: UUID | None = None):
    require_workspace(wsId)
    return page(list_page("""
        SELECT id, display_name, channel_handle, official_url, signature_template, separator_line,
            placement_policy, visual_tokens_json AS visual_tokens, ownership_attested
        FROM app.brand_identities WHERE workspace_id = %s AND (%s::uuid IS NULL OR id > %s)
        ORDER BY id LIMIT %s
    """, wsId, limit, cursor), limit)


@router.post("/workspaces/{wsId}/brands")
def save_brand(request, wsId: UUID, data: dto.BrandInput):
    require_permission(request, "manage_workspaces")
    require_workspace(wsId)
    if (data.visual_tokens or data.logo_asset_id) and not data.ownership_attested:
        raise ProblemError(422, "BRAND_OWNERSHIP_ATTESTATION_REQUIRED")
    if data.official_url is not None and not safe_link(data.official_url):
        raise ProblemError(422, "INVALID_BRAND_URL")
    if data.logo_asset_id:
        _validate_assets(wsId, [data.logo_asset_id])
    identifier = data.id or uuid4()
    if data.id:
        get_brand(wsId, identifier)
    visuals = data.visual_tokens | (
        {"logo_asset_id": str(data.logo_asset_id)} if data.logo_asset_id else {}
    )
    rows("""
        INSERT INTO app.brand_identities (id, tenant_id, workspace_id, display_name, channel_handle,
            official_url, signature_template, separator_line, placement_policy, visual_tokens_json,
            ownership_attested)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (id) DO UPDATE SET display_name = EXCLUDED.display_name,
            channel_handle = EXCLUDED.channel_handle, official_url = EXCLUDED.official_url,
            signature_template = EXCLUDED.signature_template,
            separator_line = EXCLUDED.separator_line, placement_policy = EXCLUDED.placement_policy,
            visual_tokens_json = EXCLUDED.visual_tokens_json,
            ownership_attested = EXCLUDED.ownership_attested, updated_at = clock_timestamp()
    """, (identifier, request.tenant["tenant_id"], wsId, data.display_name, data.channel_handle,
          data.official_url, data.signature_template, data.separator_line, data.placement_policy,
          Jsonb(visuals), data.ownership_attested))
    audit(request, "BRAND_SAVED", "BRAND", identifier, workspace=wsId)
    event(request, wsId, "brand.identity.updated", {"brand_identity_id": str(identifier)})
    return JsonResponse(ack(request, identifier=identifier), status=201)


@router.get("/workspaces/{wsId}/drafts")
def drafts(request, wsId: UUID, limit: int = 50, cursor: UUID | None = None):
    require_workspace(wsId)
    return page(list_page("""
        SELECT d.id, d.route_id, d.approval_state, d.current_revision_no, d.updated_at, r.title_text
        FROM app.drafts d JOIN app.draft_revisions r ON r.tenant_id = d.tenant_id
          AND r.draft_id = d.id AND r.revision_no = d.current_revision_no
        WHERE d.workspace_id = %s AND (%s::uuid IS NULL OR d.id > %s) ORDER BY d.id LIMIT %s
    """, wsId, limit, cursor), limit)


@router.post("/workspaces/{wsId}/drafts")
def create_draft(request, wsId: UUID, data: dto.DraftInput):
    require_permission(request, "edit_content")
    require_workspace(wsId)
    get_brand(wsId, data.brand_identity_id)
    attachments = _validate_assets(wsId, data.media_asset_ids)
    content = rich_text(data.body_text, [span.model_dump() for span in data.spans_utf16])
    identifier, tenant = uuid4(), request.tenant["tenant_id"]
    rows("""
        INSERT INTO app.drafts (id, tenant_id, workspace_id, created_by_user_id)
        VALUES (%s,%s,%s,%s)
    """, (identifier, tenant, wsId, request.tenant["user_id"]))
    save_revision(tenant, identifier, 1, content, data.title_text, data.brand_identity_id,
                  attachments)
    event(request, wsId, "draft.saved", {"draft_id": str(identifier), "revision_no": 1})
    return JsonResponse(ack(request, identifier=identifier, revision_no=1), status=201)


@router.get("/workspaces/{wsId}/drafts/{draftId}")
def show_draft(request, wsId: UUID, draftId: UUID):
    return draft_record(wsId, draftId)


@router.patch("/workspaces/{wsId}/drafts/{draftId}")
def edit_draft(request, wsId: UUID, draftId: UUID, data: dto.DraftPatch):
    require_permission(request, "edit_content")
    previous = draft_record(wsId, draftId, lock=True)
    if previous["approval_state"] == "ARCHIVED" or data.expected_draft_revision_no not in (
        None, previous["current_revision_no"],
    ):
        raise ProblemError(409, "DRAFT_REVISION_CONFLICT")
    changed = data.model_dump(exclude_unset=True)
    if any(changed.get(key, True) is None for key in (
        "body_text", "spans_utf16", "media_asset_ids",
    )):
        raise ProblemError(422)
    content = rich_text(changed.get("body_text", previous["body_text"]),
                        changed.get("spans_utf16", previous["spans_utf16"]))
    brand = changed.get("brand_identity_id", previous["brand_identity_id"])
    get_brand(wsId, brand)
    attachments = (_validate_assets(wsId, data.media_asset_ids) if "media_asset_ids" in changed
                   else previous["media_manifest_json"])
    revision = previous["current_revision_no"] + 1
    save_revision(request.tenant["tenant_id"], draftId, revision, content,
                  changed.get("title_text", previous["title_text"]), brand, attachments)
    rows("""
        UPDATE app.drafts SET current_revision_no = %s, approval_state = 'EDITING',
            updated_at = clock_timestamp() WHERE id = %s
    """, (revision, draftId))
    event(request, wsId, "draft.saved", {"draft_id": str(draftId), "revision_no": revision})
    return ack(request, identifier=draftId, revision_no=revision)


def _preview(workspace, draft, destinations, album_policy):
    content = rich_text(draft["body_text"], draft["spans_utf16"])
    attachments = draft["media_manifest_json"]
    assets = rows("""
        SELECT id,media_kind,mime_type,byte_size,s3_object_key
        FROM app.media_assets WHERE workspace_id=%s AND id=ANY(%s) AND NOT is_deleted
    """, (workspace, [UUID(item) for item in attachments]))
    by_id = {str(asset["id"]): asset for asset in assets}
    if len(by_id) != len(attachments):
        raise ProblemError(409, "MEDIA_ASSET_UNAVAILABLE")
    results = []
    for dest in destinations:
        brand = get_brand(workspace, dest["brand_identity_id"] or draft["brand_identity_id"])
        try:
            compiled = compile_rich_text(
                inject_brand_identity(content, brand), dest["platform"], caption=bool(attachments),
                album_count=max(1, len(attachments)), album_policy=album_policy,
            )
        except ValueError:
            raise ProblemError(422, "INVALID_RENDERED_CONTENT") from None
        result = asdict(compiled)
        if attachments:
            ordered = [
                {key: value for key, value in by_id[item].items() if key != "id"}
                for item in attachments
            ]
            native = len(ordered) > 1 and compiled.album_action is None
            kinds = {asset["media_kind"] for asset in ordered}
            allowed = {
                "IMAGE", "INFOGRAPHIC", "STICKER_IMAGE", "VIDEO", "AUDIO",
                "VOICE_NOTE", "NATIVE_STICKER", "GIF",
            }
            invalid = (
                dest["platform"] not in {"TELEGRAM", "BALE", "EITAA"}
                or len(ordered) > 10 or sum(asset["byte_size"] for asset in ordered) > 50_000_000
                or not kinds <= allowed
                or (native and not kinds <= {"IMAGE", "INFOGRAPHIC", "STICKER_IMAGE", "VIDEO"})
                or ("NATIVE_STICKER" in kinds and bool(content.plain_text.strip()))
                or (dest["platform"] != "TELEGRAM"
                    and bool(kinds & {"VOICE_NOTE", "NATIVE_STICKER"}))
            )
            if invalid:
                result["degradations"].append("DESTINATION_MEDIA_INCOMPATIBLE")
            result["media"] = ordered

        results.append({"destination_id": str(dest["id"]), "media_asset_ids": attachments,
                        **result})
    return results


@router.post("/workspaces/{wsId}/drafts/{draftId}/preview")
def preview(request, wsId: UUID, draftId: UUID, data: dto.PreviewInput):
    draft = draft_record(wsId, draftId)
    targets = get_destinations(wsId, data.destination_ids)
    return {"simulated": True, "platform_rendering_verified": False,
            "revision_no": draft["current_revision_no"], "destinations": _preview(
                wsId, draft, targets, data.album_fallback_policy,
            )}


def _publish_time(schedule):
    now = datetime.now(UTC)
    policy = {"mode": "IMMEDIATE"}
    if schedule and schedule.kind == "AT_TIMESTAMP":
        policy = {"mode": "AT_TIMESTAMP", "at_timestamp": schedule.scheduled_for}
    try:
        when = scheduled_for(policy, "UTC")
    except ValueError:
        raise ProblemError(422, "INVALID_SCHEDULE_TIMESTAMP") from None
    if policy["mode"] == "IMMEDIATE":
        return now, now
    if when < now:
        raise ProblemError(422, "SCHEDULE_IN_PAST")
    return now, when


@router.post("/workspaces/{wsId}/drafts/{draftId}/publish")
def publish(request, wsId: UUID, draftId: UUID, data: dto.PublishInput):
    require_permission(request, "publish")
    tenant = request.tenant["tenant_id"]
    key = get_idempotency_token(request)
    identifier = uuid5(NAMESPACE_URL, f"relayforge:publish:{tenant}:{wsId}:{key}")
    request_hash = hashlib.sha256(json.dumps(
        data.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    rows("SELECT pg_advisory_xact_lock(hashtextextended(%s, 5))", (str(identifier),))
    prior = first("""
        SELECT draft_id, frozen_payload_json FROM app.publication_versions
        WHERE workspace_id = %s AND id = %s
    """, (wsId, identifier))
    if prior:
        if prior["draft_id"] != draftId or prior["frozen_payload_json"].get(
            "request_sha256"
        ) != request_hash:
            raise ProblemError(409, "IDEMPOTENCY_KEY_REUSED")
        tasks = rows("""
            SELECT id FROM app.delivery_tasks WHERE publication_version_id = %s ORDER BY id
        """, (identifier,))
        return JsonResponse(ack(request, identifier=identifier,
                                delivery_task_ids=[task["id"] for task in tasks]), status=202)
    draft = draft_record(wsId, draftId, lock=True)
    if draft["current_revision_no"] != data.expected_draft_revision_no or (
        draft["approval_state"] == "ARCHIVED"
    ):
        raise ProblemError(409, "DRAFT_REVISION_CONFLICT")
    if draft["source_content_item_id"]:
        source = first("""
            SELECT access_class, is_deleted_upstream FROM app.content_items
            WHERE id = %s AND workspace_id = %s AND operation_scope = 'INGEST_PIPELINE'
        """, (draft["source_content_item_id"], wsId))
        if not source or source != {"access_class": "PROCESSABLE", "is_deleted_upstream": False}:
            raise ProblemError(422, "SOURCE_CONTENT_NOT_PUBLISHABLE")
    rows("SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 6))", (str(wsId),))
    workspace = first("SELECT emergency_stop FROM app.workspaces WHERE id = %s", (wsId,))
    if workspace["emergency_stop"]:
        raise ProblemError(409, "WORKSPACE_EMERGENCY_STOPPED")
    destinations = get_destinations(wsId, data.destination_ids)
    if any(not dest["is_enabled"] for dest in destinations):
        raise ProblemError(409, "DESTINATION_PAUSED")
    rendered = _preview(wsId, draft, destinations, data.album_fallback_policy)
    if any(
        item["length_overflow"]
        or (not item["payload"]["plain_text"].strip() and not item.get("media"))
        or "DESTINATION_MEDIA_INCOMPATIBLE" in item["degradations"]
        for item in rendered
    ):
        raise ProblemError(422, "DESTINATION_CONTENT_INCOMPATIBLE")
    now, when = _publish_time(data.schedule)
    targets = [{
        "id": dest["id"], "platform": dest["platform"], "chat_id": dest["canonical_chat_id"],
        "payload": item["payload"], "scheduled_for": when, "degradations": item["degradations"],
        "media": item.get("media"), "album_action": item["album_action"],
    } for dest, item in zip(destinations, rendered, strict=True)
        if item["album_action"] != "CANCEL_FOR_INCOMPATIBLE_DEST"]
    if not targets:
        raise ProblemError(422, "DESTINATION_CONTENT_INCOMPATIBLE")
    with connections["default"].cursor() as cursor:
        tasks = freeze_publication(
            cursor, tenant, wsId, identifier, targets, intent=data.mode,
            correlation=request.correlation_id, now=now, draft_id=draftId,
            draft_revision=draft["current_revision_no"], route_id=draft["route_id"],
            route_revision=draft["route_policy_revision_no"], actor=request.tenant["user_id"],
            request_hash=request_hash, source_item=draft["source_content_item_id"],
            media_asset_ids=draft["media_manifest_json"],
        )
    rows("UPDATE app.drafts SET approval_state = 'APPROVED' WHERE id = %s", (draftId,))
    audit(request, f"PUBLICATION_{data.mode}", "PUBLICATION", identifier, workspace=wsId)
    return JsonResponse(ack(request, identifier=identifier, delivery_task_ids=tasks), status=202)
