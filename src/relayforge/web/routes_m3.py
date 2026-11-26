from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import connections
from django.http import JsonResponse
from ninja import Router
from psycopg.types.json import Jsonb

from relayforge.adapters.publications import freeze_publication
from relayforge.core.rich_text import CanonicalRichText, compile_rich_text
from relayforge.core.rules import (
    CONTENT_KINDS,
    FilterContext,
    RuleError,
    check_route_dag,
    compile_filter,
    duplicate_decision,
    evaluate_filter,
    fingerprint,
    inject_brand_identity,
    resolve_policy,
    transform,
)
from relayforge.core.scheduling import catch_up, scheduled_for
from relayforge.web import schemas_m3 as dto
from relayforge.web.auto_orchestrator import AutoPublishRiskGate, rewrite
from relayforge.web.db import audit, first, rows
from relayforge.web.endpoints import ack, page, require_permission, require_workspace
from relayforge.web.problems import ProblemError
from relayforge.web.publications_common import (
    event,
    get_brand,
    get_destinations,
    list_page,
    rich_text,
    save_revision,
)

router = Router()
POLICY_COLUMNS = """
    p.source_ids_json, p.destination_overrides_json, p.filter_ast_json,
    p.transform_rules_json, p.schedule_policy_json, p.daily_post_cap, p.fallback_policy
"""


@dataclass(frozen=True)
class FixedClock:
    value: datetime

    def now(self):
        return self.value


def _source_rows(workspace, identifiers):
    ids = [UUID(str(identifier)) for identifier in identifiers]
    if len(set(ids)) != len(ids):
        raise ProblemError(422, "DUPLICATE_SOURCE")
    found = rows("""
        SELECT id, canonical_channel_id, source_kind, webhook_platform, is_active
        FROM app.sources WHERE workspace_id = %s AND id = ANY(%s)
    """, (workspace, ids))
    if len(found) != len(ids):
        raise ProblemError(404)
    return found


def _source_node(source):
    platform = source["webhook_platform"] or (
        "bale" if source["source_kind"] == "BALE_BOT_CHANNEL" else "telegram"
    )
    return platform.upper(), source["canonical_channel_id"].casefold()


def _edges(sources, destinations):
    return [
        (_source_node(source), (dest["platform"], dest["canonical_chat_id"].casefold()))
        for source in sources for dest in destinations
    ]


def _workspace(workspace):
    ws = first("""
        SELECT w.timezone, w.emergency_stop, coalesce(s.settings_json, '{}'::jsonb) AS settings
        FROM app.workspaces w LEFT JOIN app.workspace_settings s ON s.workspace_id = w.id
        WHERE w.id = %s AND NOT w.is_archived
    """, (workspace,))
    if not ws:
        raise ProblemError(404)
    return ws


def _route(workspace, identifier, *, lock=False):
    result = first(f"""
        SELECT r.id, r.display_name, r.slug, r.route_mode, r.automation_level, r.is_enabled,
            r.active_policy_revision, {POLICY_COLUMNS}
        FROM app.routes r JOIN app.route_policy_revisions p ON p.tenant_id = r.tenant_id
            AND p.route_id = r.id AND p.revision_no = r.active_policy_revision
        WHERE r.workspace_id = %s AND r.id = %s
    """ + (" FOR NO KEY UPDATE OF r" if lock else ""), (workspace, identifier))
    if result is None:
        raise ProblemError(404)
    return result


def _policy(workspace, data):
    src = _source_rows(workspace, data.source_ids)
    dest = get_destinations(workspace, data.destination_ids)
    if set(data.destination_overrides) - set(data.destination_ids):
        raise ProblemError(422, "UNKNOWN_DESTINATION_OVERRIDE")
    overrides = [{"destination_id": str(dest_id), "override": data.destination_overrides.get(
        dest_id, {},
    )} for dest_id in data.destination_ids]
    zone = _workspace(workspace)["timezone"]
    try:
        compile_filter(data.filter_ast)
        for settings in [data.transform_rules, *(item["override"] for item in overrides)]:
            transform(CanonicalRichText("نمونه"), settings)
            get_brand(workspace, settings.get("brand_identity_id"))
        _schedule(data.schedule_policy, zone, datetime.now(UTC))
    except (ValueError, KeyError, TypeError, AttributeError, ZoneInfoNotFoundError):
        raise ProblemError(422, "INVALID_ROUTE_POLICY") from None
    return src, dest, overrides


def _graph_lock(workspace):
    rows("SELECT pg_advisory_xact_lock(hashtextextended(%s, 3))", (str(workspace),))


def _check_graph(workspace, proposed, skip=None):
    existing = rows(f"""
        SELECT r.id, {POLICY_COLUMNS} FROM app.routes r
        JOIN app.route_policy_revisions p ON p.tenant_id = r.tenant_id AND p.route_id = r.id
         AND p.revision_no = r.active_policy_revision
        WHERE r.workspace_id = %s AND (%s::uuid IS NULL OR r.id <> %s)
    """, (workspace, skip, skip))
    for route in existing:
        sources = rows("""
            SELECT canonical_channel_id, source_kind, webhook_platform FROM app.sources
            WHERE workspace_id = %s AND id = ANY(%s)
        """, (workspace, [UUID(value) for value in route["source_ids_json"]]))
        destinations = rows("""
            SELECT platform, canonical_chat_id FROM app.destinations
            WHERE workspace_id = %s AND id = ANY(%s) AND NOT is_archived
        """, (workspace, [UUID(item["destination_id"])
                          for item in route["destination_overrides_json"]]))
        proposed.extend(_edges(sources, destinations))
    try:
        check_route_dag(proposed)
    except RuleError:
        raise ProblemError(409, "ROUTE_CYCLE_DETECTED") from None


def _insert_policy(request, route, number, data, overrides):
    rows("""
        INSERT INTO app.route_policy_revisions (id, tenant_id, route_id, revision_no,
            source_ids_json, destination_overrides_json, filter_ast_json, transform_rules_json,
            schedule_policy_json, daily_post_cap, fallback_policy)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (uuid4(), request.tenant["tenant_id"], route, number,
          Jsonb([str(identifier) for identifier in data.source_ids]), Jsonb(overrides),
          Jsonb(data.filter_ast), Jsonb(data.transform_rules), Jsonb(data.schedule_policy),
          data.daily_post_cap, data.fallback_policy))


@router.get("/workspaces/{wsId}/routes")
def routes(request, wsId: UUID, limit: int = 50, cursor: UUID | None = None):
    require_workspace(wsId)
    return page(list_page("""
        SELECT r.id, r.slug, r.display_name, r.route_mode, r.automation_level, r.is_enabled,
               r.active_policy_revision, p.daily_post_cap
        FROM app.routes r JOIN app.route_policy_revisions p ON p.tenant_id = r.tenant_id
         AND p.route_id = r.id AND p.revision_no = r.active_policy_revision
        WHERE workspace_id = %s AND (%s::uuid IS NULL OR r.id > %s) ORDER BY r.id LIMIT %s
    """, wsId, limit, cursor), limit)


@router.post("/workspaces/{wsId}/routes")
def create_route(request, wsId: UUID, data: dto.RouteInput):
    require_permission(request, "manage_workspaces")
    if data.automation_level == "AUTO_PUBLISH_BY_POLICY":
        require_permission(request, "publish")
    _graph_lock(wsId)
    src, dest, overrides = _policy(wsId, data)
    _check_graph(wsId, _edges(src, dest))
    identifier = uuid4()
    rows("""
        INSERT INTO app.routes (id, tenant_id, workspace_id, slug, display_name, route_mode,
                               automation_level, is_enabled) VALUES (%s,%s,%s,%s,%s,%s,%s,false)
    """, (identifier, request.tenant["tenant_id"], wsId, data.slug, data.display_name,
          data.route_mode, data.automation_level))
    _insert_policy(request, identifier, 1, data, overrides)
    audit(request, "ROUTE_CREATED", "ROUTE", identifier, workspace=wsId)
    return JsonResponse(ack(request, identifier=identifier, revision_no=1, is_enabled=False),
                        status=201)


@router.patch("/workspaces/{wsId}/routes/{routeId}")
def patch_route(request, wsId: UUID, routeId: UUID, data: dto.RoutePatch):
    require_permission(request, "manage_workspaces")
    _graph_lock(wsId)
    previous = _route(wsId, routeId, lock=True)
    revision = previous["active_policy_revision"]
    if data.policy:
        src, dest, overrides = _policy(wsId, data.policy)
        _check_graph(wsId, _edges(src, dest), routeId)
        revision += 1
        _insert_policy(request, routeId, revision, data.policy, overrides)
    elif data.is_enabled:
        src = _source_rows(wsId, previous["source_ids_json"])
        dest = get_destinations(wsId, [item["destination_id"]
                                      for item in previous["destination_overrides_json"]])
        _check_graph(wsId, _edges(src, dest), routeId)
    rows("""
        UPDATE app.routes SET display_name = coalesce(%s, display_name),
            route_mode = coalesce(%s, route_mode),
            automation_level = coalesce(%s, automation_level),
            is_enabled = coalesce(%s, is_enabled), active_policy_revision = %s,
            updated_at = clock_timestamp() WHERE id = %s
    """, (data.display_name, data.route_mode, data.automation_level, data.is_enabled,
          revision, routeId))
    audit(request, "ROUTE_UPDATED", "ROUTE", routeId, workspace=wsId)
    return ack(request, identifier=routeId, revision_no=revision)


def _schedule(policy, zone, now, *, position=0):
    mode = policy.get("catch_up", "HOLD_FOR_REVIEW")
    spacing = policy.get("spacing_sec", 60)
    if mode not in {"HOLD_FOR_REVIEW", "GRADUAL_THROTTLED", "SKIP_EXPIRED"} or (
        type(spacing) is not int or not 1 <= spacing <= 86400
    ):
        raise ValueError("INVALID_CATCH_UP_POLICY")
    target = scheduled_for(policy, zone, clock=FixedClock(now))
    return catch_up(target, now, mode, spacing_sec=spacing, position=position)


def _source_content(workspace, identifier):
    row = first("""
        SELECT ci.id, ci.source_id, ci.access_class, ci.media_group_id,
            rev.plain_text, rev.entities_ast_json, rev.media_manifest_json
        FROM app.content_items ci JOIN LATERAL (
            SELECT plain_text, entities_ast_json, media_manifest_json
            FROM app.content_item_revisions WHERE tenant_id = ci.tenant_id
              AND content_item_id = ci.id ORDER BY revision_no DESC LIMIT 1
        ) rev ON true
        WHERE ci.workspace_id = %s AND ci.id = %s AND ci.operation_scope = 'INGEST_PIPELINE'
          AND NOT ci.is_deleted_upstream
    """, (workspace, identifier))
    if not row:
        raise ProblemError(404)
    media = row["media_manifest_json"]
    if len(media) > 1 or row["media_group_id"]:
        row.update(content_kind="ALBUM", album_count=max(2, len(media)))
    elif media and isinstance(media[0], dict):
        kind = media[0].get("kind")
        row["content_kind"] = kind if isinstance(kind, str) and kind in CONTENT_KINDS else (
            "DOCUMENT"
        )
    return row


def _daily_limit(route_id, cap, zone, at):
    local = at.astimezone(ZoneInfo(zone))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return first("""
        SELECT count(DISTINCT publication_version_id) AS total FROM app.delivery_tasks
        WHERE route_id = %s AND scheduled_for >= %s AND scheduled_for < %s
    """, (route_id, start, start + timedelta(days=1)))["total"] >= cap


def _compile_target(workspace, ws, route, info, content, destination, override, compiled, now):
    defaults = ws["settings"].get("route_defaults", {})
    route_policy = (route["transform_rules_json"]
                    if route["route_mode"] in {"RULES", "AI", "HYBRID"} else {})
    settings = resolve_policy(defaults, route_policy, override)
    brand = get_brand(workspace, override.get("brand_identity_id")
                      or destination["brand_identity_id"] or settings.get("brand_identity_id"))
    schedule_policy = route["schedule_policy_json"] | override.get("schedule_policy", {})
    ctx = FilterContext(
        content, source_id=str(info["source_id"]), access_class=info["access_class"],
        platform=destination["platform"], transformed=bool(settings or brand),
        content_kind=info.get("content_kind", "TEXT"),
    )
    matched, reason = evaluate_filter(compiled, ctx)
    trace = {"destination_id": str(destination["id"]), "matched": matched, "filter_reason": reason}
    if not matched:
        return trace, None, False
    try:
        rendered = inject_brand_identity(transform(content, settings), brand)
        formatting = compile_rich_text(
            rendered, destination["platform"], album_count=info.get("album_count", 1),
            album_policy=settings.get("album_fallback_policy", "CANCEL_FOR_INCOMPATIBLE_DEST"),
        )
        # A reserved slot is deterministic under the route row lock used for actual runs.
        spacing_position = 0
        if schedule_policy.get("catch_up") == "GRADUAL_THROTTLED":
            spacing_position = first("""
                SELECT count(*) AS queued FROM app.delivery_tasks
                WHERE route_id = %s AND destination_id = %s AND scheduled_for >= %s
                  AND status IN ('READY','SCHEDULED','RETRY_WAIT')
            """, (route["id"], destination["id"], now))["queued"]
        schedule, target = _schedule(schedule_policy, ws["timezone"], now,
                                     position=spacing_position)
    except (ValueError, TypeError, AttributeError, KeyError, ZoneInfoNotFoundError):
        raise ProblemError(422, "INVALID_ROUTE_POLICY") from None
    hold = (
        formatting.length_overflow or schedule == "NEEDS_REVIEW"
        or formatting.album_action == "CANCEL_FOR_INCOMPATIBLE_DEST"
        or info["access_class"] == "REFERENCE_ONLY" or info.get("content_kind", "TEXT") != "TEXT"
    )
    if target and _daily_limit(route["id"], route["daily_post_cap"], ws["timezone"], target):
        hold = True
        schedule = "DAILY_CAP_REACHED"
    trace.update(formatting=asdict(formatting), schedule_decision=schedule, scheduled_for=target)
    if target is None or hold or not destination["is_enabled"]:
        return trace, None, hold
    return trace, {
        "id": destination["id"], "platform": destination["platform"],
        "chat_id": destination["canonical_chat_id"], "scheduled_for": target,
        "payload": formatting.payload, "degradations": formatting.degradations,
        "draft_text": rendered.plain_text,
        "draft_spans": [asdict(span) for span in rendered.spans_utf16],
        "catch_up": {"mode": schedule_policy.get("catch_up", "HOLD_FOR_REVIEW"),
                     "spacing_sec": schedule_policy.get("spacing_sec", 60)},
    }, hold


class NullDryRunSink:
    def apply(self, request, workspace, route, item, content, candidate, targets, decision):
        return {}


class RouteWriteSink:
    def apply(self, request, workspace, route, item, content, candidate, targets, decision):
        if decision not in {"EDITING", "NEEDS_REVIEW", "ENQUEUED"}:
            return {}
        tenant = request.tenant["tenant_id"]
        identifier = uuid4()
        rows("""
            INSERT INTO app.content_fingerprints (id, tenant_id, workspace_id, route_id,
                content_item_id, exact_hash_sha256, simhash_64) VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (uuid4(), tenant, workspace, route["id"], item, *candidate))
        revision = route["active_policy_revision"]
        if decision == "ENQUEUED":
            with connections["default"].cursor() as cursor:
                tasks = freeze_publication(
                    cursor, tenant, workspace, identifier, targets, intent="LIVE_PUBLISH",
                    correlation=request.correlation_id, now=datetime.now(UTC),
                    route_id=route["id"], route_revision=revision, source_item=item,
                )
            return {"publication_version_id": identifier, "delivery_task_ids": tasks}
        rows("""
            INSERT INTO app.drafts (id, tenant_id, workspace_id, route_id, route_policy_revision_no,
                source_content_item_id, approval_state, created_by_user_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, (identifier, tenant, workspace, route["id"], revision, item, decision,
              request.tenant["user_id"]))
        base = _workspace(workspace)["settings"].get("route_defaults", {})
        common = base | (route["transform_rules_json"]
                         if route["route_mode"] in {"RULES", "AI", "HYBRID"} else {})
        brand = common.get("brand_identity_id")
        if brand:
            get_brand(workspace, brand)
            brand = UUID(str(brand))
        try:
            edited = transform(content, common)
        except (ValueError, TypeError):
            raise ProblemError(422, "INVALID_ROUTE_POLICY") from None
        save_revision(tenant, identifier, 1, edited, None, brand, [])
        event(request, workspace, "draft.saved", {"draft_id": str(identifier), "revision_no": 1})
        return {"draft_id": identifier}


def run_pipeline(request, workspace, route, info, sink):
    source_id = str(info["source_id"])
    content = rich_text(info["plain_text"], info["entities_ast_json"])
    if info["id"] and source_id not in route["source_ids_json"]:
        return {"decision": "SOURCE_NOT_IN_ROUTE", "destinations": []}
    ws = _workspace(workspace)
    candidate = fingerprint(content.plain_text)
    history = rows("""
        SELECT exact_hash_sha256, simhash_64 FROM app.content_fingerprints WHERE route_id = %s
    """, (route["id"],))
    duplicate = duplicate_decision(candidate, [(row["exact_hash_sha256"], row["simhash_64"])
                                              for row in history])
    overrides = {item["destination_id"]: item["override"]
                 for item in route["destination_overrides_json"]}
    compiled = compile_filter(route["filter_ast_json"])
    trace, targets, now = [], [], datetime.now(UTC)
    hold = duplicate == "AMBIGUOUS_NEAR_DUPLICATE_HOLD_REVIEW"
    destinations = get_destinations(workspace, list(overrides))
    for destination in destinations:
        step, target, needs_review = _compile_target(
            workspace, ws, route, info, content, destination,
            overrides[str(destination["id"])], compiled, now,
        )
        trace.append(step)
        if target:
            targets.append(target)
        hold |= needs_review
    if duplicate == "DUPLICATE_EXACT_SKIP":
        decision = duplicate
    elif not any(step["matched"] for step in trace):
        decision = "FILTER_SKIP"
    elif all(step.get("schedule_decision") == "SKIP_EXPIRED" for step in trace
             if step["matched"]):
        decision = "SKIP_EXPIRED"
    elif all(step.get("schedule_decision") == "DAILY_CAP_REACHED" for step in trace
             if step["matched"]):
        decision = "DAILY_CAP_REACHED"
    elif hold or route["automation_level"] == "APPROVAL_REQUIRED":
        decision = "NEEDS_REVIEW"
    elif route["automation_level"] == "DRAFT_ONLY" or not route["is_enabled"]:
        decision = "EDITING"
    elif ws["emergency_stop"]:
        decision = "NEEDS_REVIEW"
    elif targets:
        decision = "ENQUEUED"
    else:
        decision = "SKIP_UNAVAILABLE_OR_EXPIRED"
    ai_trace = {}
    automated = route["route_mode"] in {"AI", "HYBRID"} and isinstance(sink, RouteWriteSink)
    if (
        automated and decision in {"ENQUEUED", "EDITING", "NEEDS_REVIEW"}
        and route["is_enabled"] and not ws["emergency_stop"]
        and info["access_class"] == "PROCESSABLE" and not info.get("media_manifest_json")
        and duplicate != "AMBIGUOUS_NEAR_DUPLICATE_HOLD_REVIEW"
    ):
        text, guard, ai_trace = rewrite(request, workspace, route, content.plain_text)
        policy = route["transform_rules_json"]
        risk = AutoPublishRiskGate.evaluate(
            guard, policy.get("freedom_mode", "BALANCED"), policy,
            has_media=bool(info.get("media_manifest_json")),
        )
        ai_trace["risk_gate"] = risk
        if risk:
            decision = "NEEDS_REVIEW"
        if text is not None and not risk:
            content = CanonicalRichText(text)
            trace, targets = [], []
            for destination in destinations:
                step, target, needs_review = _compile_target(
                    workspace, ws, route, info, content, destination,
                    overrides[str(destination["id"])], compiled, now,
                )
                trace.append(step)
                if target:
                    targets.append(target)
                if needs_review:
                    decision = "NEEDS_REVIEW"
            if not targets:
                decision = "NEEDS_REVIEW"
        elif text is None:
            rights = first("SELECT republish_rights_attested FROM app.sources WHERE id=%s",
                           (info["source_id"],))
            if route["fallback_policy"] == "STOP":
                decision = "AI_FAILED_STOP"
            elif not (route["fallback_policy"] == "SEND_AUTHORIZED_ORIGINAL"
                      and rights and rights["republish_rights_attested"] and not risk):
                decision = "NEEDS_REVIEW"
    result = sink.apply(
        request, workspace, route, info["id"], content, candidate, targets, decision,
    )
    if automated:
        result_ids = {
            key: [str(item) for item in value] if isinstance(value, list) else str(value)
            for key, value in result.items()
        }
        event(request, workspace, "auto.route.run", {
            "route_id": str(route["id"]), "source_content_item_id": str(info["id"]),
            "decision": decision, **ai_trace, **result_ids,
        })
    return {"decision": decision, "deduplication": duplicate, "destinations": trace,
            **ai_trace, **result}


@router.post("/workspaces/{wsId}/routes/{routeId}/dry-run")
def dry_run(request, wsId: UUID, routeId: UUID, data: dto.RouteItem):
    route = _route(wsId, routeId)
    if (data.content_item_id is None) == (data.sample_text is None):
        raise ProblemError(422)
    info = (
        _source_content(wsId, data.content_item_id) if data.content_item_id else
        {"id": None, "source_id": None, "access_class": "PROCESSABLE",
         "plain_text": data.sample_text, "entities_ast_json": []}
    )
    return {"simulation": True, **run_pipeline(request, wsId, route, info, NullDryRunSink())}


@router.post("/workspaces/{wsId}/routes/{routeId}/run-item")
def run_item(request, wsId: UUID, routeId: UUID, data: dto.RouteItem):
    require_permission(request, "edit_content")
    if not data.content_item_id or data.sample_text is not None:
        raise ProblemError(422)
    rows("SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 6))", (str(wsId),))
    route = _route(wsId, routeId, lock=True)
    if route["automation_level"] == "AUTO_PUBLISH_BY_POLICY":
        require_permission(request, "publish")
    return run_pipeline(
        request, wsId, route, _source_content(wsId, data.content_item_id), RouteWriteSink(),
    )


@router.get("/workspaces/{wsId}/reports/automated-runs")
def automated_runs(request, wsId: UUID):
    require_workspace(wsId)
    items = rows("""
        SELECT o.payload_json AS run,o.created_at,coalesce((
            SELECT jsonb_agg(jsonb_build_object('id', t.id, 'status', t.status,
                'remote_message_id', t.remote_message_id) ORDER BY t.id)
            FROM app.delivery_tasks t
            WHERE t.publication_version_id=(o.payload_json->>'publication_version_id')::uuid
        ), '[]'::jsonb) AS delivery_tasks
        FROM app.domain_outbox o WHERE o.workspace_id=%s AND o.event_type='auto.route.run'
        ORDER BY o.created_at DESC,o.id LIMIT 100
    """, (wsId,))
    return {"items": items, "next_cursor": None}
