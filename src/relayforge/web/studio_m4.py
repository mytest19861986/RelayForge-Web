from dataclasses import asdict
from html import escape
from time import monotonic
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg
from django.db import connections
from ninja import Router, Schema
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import Field

from relayforge.adapters.ai_gateway import MODEL_ID, AIError
from relayforge.core.fact_guard import candidate, prompts
from relayforge.core.intelligence import digest, strip_identity
from relayforge.core.rich_text import CanonicalRichText, FormatSpanUtf16, SafePlaceholderTemplate
from relayforge.core.rules import inject_brand_identity
from relayforge.core.security import scrub_sensitive_text
from relayforge.web.ai_m4 import ai_problem, authorized_adapter, provider
from relayforge.web.db import first, rows
from relayforge.web.endpoints import require_permission, require_workspace
from relayforge.web.problems import ProblemError
from relayforge.web.publications_common import event, get_brand, get_destinations, save_revision
from relayforge.web.routes_m2 import get_idempotency_token

router = Router()
ROOT = "/workspaces/{wsId}"


class StudioInput(Schema):
    input_mode: Literal["MANUAL_TEXT", "SOURCE_ITEM", "TOPIC_PROMPT"]
    raw_text: str | None = Field(default=None, max_length=32768)
    source_content_item_id: UUID | None = None
    task_type: Literal[
        "REWRITE", "SUMMARIZE", "EXPAND", "TRANSLATE", "COPYEDIT", "NEW_FROM_IDEA", "PODCAST_SCRIPT"
    ]
    freedom_mode: Literal["STRICT", "BALANCED", "CREATIVE", "UNRESTRICTED"]
    routing_mode: Literal["MANUAL", "AUTO_ECONOMY", "AUTO_QUALITY"]
    manual_provider_id: UUID | None = None
    manual_model_id: str | None = None
    style_profile_id: UUID | None = None
    pinned_style_revision_no: int | None = Field(default=None, ge=1)
    brand_identity_id: UUID | None = None
    target_destination_ids: list[UUID] = Field(default_factory=list, max_length=50)
    option_count: int = Field(default=1, ge=1, le=4)
    save_draft: bool = False


class ProbeInput(Schema):
    provider_id: UUID
    model_id: str
    modality: Literal["TEXT", "VISION", "IMAGE", "TTS", "VIDEO"] = "TEXT"
    explicit_token_spend_confirmation: bool = Field(strict=True)


def _new_connection():
    parameters = connections["default"].get_connection_params()
    for key in ("context", "cursor_factory"):
        parameters.pop(key, None)
    return psycopg.connect(**parameters, autocommit=True, row_factory=dict_row)


def _context(conn, request):
    conn.execute(
        "SELECT set_config('app.tenant_id',%s,true), set_config('app.user_id',%s,true)",
        (str(request.tenant["tenant_id"]), str(request.tenant["user_id"])),
    )


def run_attempt(
    conn, request, workspace, row, api, model, kind, estimate, system, prompt, *, image=None
):
    tenant = request.tenant["tenant_id"]
    execution, reservation = uuid4(), uuid4()
    with conn.transaction():
        _context(conn, request)
        try:
            conn.execute(
                "SELECT app.fn_budget_reserve(%s,%s,NULL,%s,%s,1800)",
                (reservation, workspace, str(execution), estimate),
            )
        except psycopg.Error as error:
            if error.sqlstate in {"RF001", "RF006"}:
                raise ProblemError(409, error.sqlstate) from None
            raise
        conn.execute(
            """
            INSERT INTO app.ai_executions (id,tenant_id,workspace_id,provider_id,reservation_id,
                task_modality,model_id,status,correlation_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'IN_PROGRESS',%s)
        """,
            (
                execution,
                tenant,
                workspace,
                row["id"],
                reservation,
                kind,
                model,
                request.correlation_id,
            ),
        )
    started = monotonic()
    error = output = None
    try:
        output = api.generate(model, system, prompt, image=image)
    except AIError as failure:
        error = failure
    spent = output is not None or (error and error.spent)
    amount = output.cost_micros if output and output.cost_micros is not None else estimate
    accuracy = (
        "ACTUAL_PROVIDER"
        if output and output.cost_micros is not None
        else "ESTIMATED"
        if output
        else "UNKNOWN"
    )
    with conn.transaction():
        _context(conn, request)
        current = conn.execute(
            "SELECT status FROM app.ai_executions WHERE id=%s FOR UPDATE", (execution,)
        ).fetchone()["status"]
        try:
            with conn.transaction():
                conn.execute(
                    "SELECT app.fn_budget_settle_with_accuracy(%s,%s,%s,%s)",
                    (
                        reservation,
                        "COMMITTED" if spent else "RELEASED",
                        amount if spent else None,
                        accuracy,
                    ),
                )
        except psycopg.Error as failure:
            if failure.sqlstate != "RF004":
                raise
            # Keep the original charge when a late/overage settlement would breach a ceiling.
            conn.execute(
                "SELECT app.fn_budget_settle_with_accuracy(%s,'COMMITTED',%s,'UNKNOWN')",
                (reservation, estimate),
            )
            error = AIError("QUOTA_ZERO")
        if current != "CANCELLED":
            status = (
                "SUCCEEDED"
                if error is None
                else ("TIMED_OUT" if error.code == "TIMEOUT" else "FAILED")
            )
            conn.execute(
                """
                UPDATE app.ai_executions SET status=%s,error_category=%s,http_status=%s,
                    input_tokens=%s,output_tokens=%s,latency_ms=%s,completed_at=clock_timestamp()
                WHERE id=%s
            """,
                (
                    status,
                    error.code if error else None,
                    error.status if error else None,
                    output.input_tokens if output else None,
                    output.output_tokens if output else None,
                    int((monotonic() - started) * 1000),
                    execution,
                ),
            )
    if current == "CANCELLED":
        raise ProblemError(409, "AI_EXECUTION_CANCELLED")
    if error:
        raise error
    return execution, output


def routing(workspace, data):
    profile = first(
        """
        SELECT ordered_candidates_json,allow_cross_provider_fallback,max_attempts,timeout_sec
        FROM app.ai_routing_profiles WHERE workspace_id=%s AND task_modality='TEXT'
    """,
        (workspace,),
    )
    configs = (profile or {}).get("ordered_candidates_json", [])
    if data.routing_mode == "MANUAL":
        if not data.manual_provider_id or not data.manual_model_id:
            raise ProblemError(422, "MANUAL_MODEL_REQUIRED")
        return (
            [
                dict(
                    provider_id=str(data.manual_provider_id),
                    model_id=data.manual_model_id,
                    estimated_micros=next(
                        (
                            item["estimated_micros"]
                            for item in configs
                            if item["provider_id"] == str(data.manual_provider_id)
                            and item["model_id"] == data.manual_model_id
                        ),
                        600,
                    ),
                )
            ],
            False,
            (profile or {}).get("timeout_sec", 60),
        )
    if not profile:
        raise ProblemError(422, "ROUTING_PROFILE_REQUIRED")
    ordered = sorted(
        configs,
        key=lambda item: item["estimated_micros"],
        reverse=data.routing_mode == "AUTO_QUALITY",
    )
    return (
        ordered[: profile["max_attempts"]],
        profile["allow_cross_provider_fallback"],
        profile["timeout_sec"],
    )


def _input(workspace, data):
    if data.input_mode == "SOURCE_ITEM":
        if not data.source_content_item_id:
            raise ProblemError(422)
        found = first(
            """
            SELECT r.plain_text FROM app.content_items i JOIN app.content_item_revisions r
                ON r.tenant_id=i.tenant_id AND r.content_item_id=i.id
            WHERE i.workspace_id=%s AND i.id=%s AND i.operation_scope <> 'ANALYSIS_ISOLATED'
                AND i.access_class='PROCESSABLE' AND NOT i.is_deleted_upstream
            ORDER BY revision_no DESC LIMIT 1
        """,
            (workspace, data.source_content_item_id),
        )
        if not found:
            raise ProblemError(404)
        return found["plain_text"]
    if data.source_content_item_id or not data.raw_text or not data.raw_text.strip():
        raise ProblemError(422)
    return data.raw_text


def _style(workspace, data):
    if not data.style_profile_id:
        return {}, None
    row = first(
        """
        SELECT r.revision_no,r.writing_rules_json,r.layout_template_json
        FROM app.style_profiles p JOIN app.style_revisions r ON r.style_profile_id=p.id
          AND r.tenant_id=p.tenant_id
        WHERE p.id=%s AND p.workspace_id=%s AND NOT p.is_archived
          AND r.revision_no=COALESCE(%s,p.pinned_revision_no)
    """,
        (data.style_profile_id, workspace, data.pinned_style_revision_no),
    )
    if not row:
        raise ProblemError(404)
    return row, row["revision_no"]


def active_preferences(workspace, brand, destination):
    if brand is None:
        return []
    return [
        row["rule_statement"]
        for row in rows(
            """
        SELECT r.rule_statement FROM app.learned_preference_rules r
        JOIN app.workspace_settings s ON s.workspace_id=r.workspace_id
        JOIN app.external_consents c ON c.workspace_id=r.workspace_id
            AND c.provider_scope='LOCAL_FEEDBACK_MEMORY'
        WHERE r.workspace_id=%s AND r.brand_identity_id=%s
          AND r.destination_id IS NOT DISTINCT FROM %s AND r.review_state='ACCEPTED'
          AND s.allow_feedback_learning AND c.is_granted ORDER BY r.rule_statement
    """,
            (workspace, brand, destination),
        )
    ]


@router.post(ROOT + "/ai/models/probe")
def probe(request, wsId: UUID, data: ProbeInput):
    require_permission(request, "manage_security")
    if not data.explicit_token_spend_confirmation or not MODEL_ID.fullmatch(data.model_id):
        raise ProblemError(422)
    row = provider(wsId, data.provider_id)
    api, _ = authorized_adapter(wsId, row, data.modality)
    if data.modality not in {"TEXT", "VISION"}:
        raise ProblemError(422, "PROBE_MODALITY_NOT_IMPLEMENTED")
    image = (
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42m"
        "P8/x8AAwMCAO+jf2sAAAAASUVORK5CYII="
        if data.modality == "VISION"
        else None
    )
    with _new_connection() as conn:
        try:
            identifier, _ = run_attempt(
                conn,
                request,
                wsId,
                row,
                api,
                data.model_id,
                data.modality,
                600,
                "Return a JSON object with a text field.",
                "Synthetic prompt: reply with OK.",
                image=image,
            )
        except AIError as error:
            return ai_problem(request, error)
    return {"execution_id": identifier, "status": "SUCCEEDED", "verified_modality": data.modality}


@router.post(ROOT + "/studio/generate")
def generate(request, wsId: UUID, data: StudioInput):
    require_permission(request, "edit_content")
    require_workspace(wsId)
    identity_key = get_idempotency_token(request)
    tenant, actor = request.tenant["tenant_id"], request.tenant["user_id"]
    session = uuid5(NAMESPACE_URL, f"relayforge:studio:{tenant}:{wsId}:{identity_key}")
    request_hash = digest(data.model_dump(mode="json"))
    rows("SELECT pg_advisory_xact_lock(hashtextextended(%s,9))", (str(session),))
    prior = first(
        "SELECT request_sha256,completed_at,result_json FROM app.generation_sessions WHERE id=%s",
        (session,),
    )
    if prior:
        if prior["request_sha256"] != request_hash:
            raise ProblemError(409, "IDEMPOTENCY_KEY_REUSED")
        if not prior["completed_at"]:
            raise ProblemError(409, "AI_EXECUTION_OUTCOME_PENDING")
        return {**prior["result_json"], "replayed": True}
    source = _input(wsId, data)
    style, revision = _style(wsId, data)
    identities = rows(
        """
        SELECT extracted_identity_json AS identity FROM app.channel_analyses
        WHERE workspace_id=%s AND status='COMPLETED' ORDER BY id LIMIT 100
    """,
        (wsId,),
    )
    for identity in identities:
        source = strip_identity(source, identity["identity"])
    destinations = get_destinations(wsId, data.target_destination_ids)
    brand_id = data.brand_identity_id
    if len(destinations) == 1:
        brand_id = destinations[0]["brand_identity_id"] or brand_id
    brand = get_brand(wsId, brand_id)
    destination_brands = [
        (
            item["id"],
            get_brand(
                wsId,
                item["brand_identity_id"] or data.brand_identity_id,
            ),
        )
        for item in destinations
    ]
    preferred = active_preferences(
        wsId, brand_id, destinations[0]["id"] if len(destinations) == 1 else None
    )
    options, cross, timeout = routing(wsId, data)
    candidates = []
    failure = None
    with _new_connection() as conn:
        with conn.transaction():
            _context(conn, request)
            conn.execute(
                """
                INSERT INTO app.generation_sessions (id,tenant_id,workspace_id,
                    source_content_item_id,style_profile_id,pinned_style_revision_no,freedom_mode,
                    operation_kind,created_by_user_id,request_sha256)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
                (
                    session,
                    tenant,
                    wsId,
                    data.source_content_item_id,
                    data.style_profile_id,
                    revision,
                    data.freedom_mode,
                    data.task_type,
                    actor,
                    request_hash,
                ),
            )
        for _ in range(data.option_count):
            for entry in options:
                if entry["provider_id"] != options[0]["provider_id"] and not cross:
                    continue
                model = entry["model_id"]
                if not MODEL_ID.fullmatch(model):
                    raise ProblemError(422, "INVALID_MODEL")
                row = provider(wsId, UUID(entry["provider_id"]))
                try:
                    api, key = authorized_adapter(wsId, row, "TEXT")
                except ProblemError:
                    if entry is options[0] or not cross:
                        raise
                    continue
                api.timeout_sec = timeout
                system, prompt = prompts(
                    scrub_sensitive_text(source, (key,)),
                    data.task_type,
                    data.freedom_mode,
                    str(
                        {
                            "writing": style.get("writing_rules_json", {}),
                            "preferences": preferred,
                        }
                    ),
                )
                try:
                    execution, output = run_attempt(
                        conn,
                        request,
                        wsId,
                        row,
                        api,
                        model,
                        "TEXT",
                        entry["estimated_micros"],
                        system,
                        prompt,
                    )
                except AIError as error:
                    failure = error
                    if error.code not in {
                        "SCHEMA_INVALID",
                        "UPSTREAM_5XX",
                        "RATE_LIMIT_RETRYABLE",
                        "TIMEOUT",
                        "MODEL_NOT_FOUND",
                    }:
                        return ai_problem(request, error)
                    continue
                clean = scrub_sensitive_text(output.text, (key,))
                for identity in identities:
                    clean = strip_identity(clean, identity["identity"])
                result = candidate(
                    source,
                    clean,
                    data.freedom_mode,
                    brand,
                    self_check=(
                        scrub_sensitive_text(output.self_check, (key,))
                        if output.self_check
                        else None
                    ),
                )
                if style:
                    layout = style["layout_template_json"]
                    rich = SafePlaceholderTemplate(layout["template_tokens"]).render(
                        CanonicalRichText(result["text"]),
                        bold_first_line=layout["bold_first_line"],
                    )
                    result["text"] = rich.plain_text
                    result["escaped_html"] = escape(rich.plain_text)
                    result["spans_utf16"] = [asdict(span) for span in rich.spans_utf16]
                if destination_brands:
                    result["destinations"] = {}
                    for destination_id, destination_brand in destination_brands:
                        if len(destination_brands) == 1:
                            rendered = result["text"]
                        else:
                            rendered = inject_brand_identity(
                                CanonicalRichText(clean),
                                destination_brand,
                            ).plain_text
                        result["destinations"][str(destination_id)] = {
                            "text": rendered,
                            "escaped_html": escape(rendered),
                        }
                result["execution_id"] = str(execution)
                result["is_fallback_output"] = False
                candidates.append(result)
                break
            else:
                if not candidates:
                    return ai_problem(request, failure or AIError("FORBIDDEN"))
                break
    for index, result in enumerate(candidates, 1):
        rows(
            """
            INSERT INTO app.generation_candidates (id,tenant_id,session_id,ai_execution_id,
                candidate_rank,output_text,fact_guard_verdict,fact_diff_json,quality_signals_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
            (
                uuid4(),
                tenant,
                session,
                result["execution_id"],
                index,
                result["text"],
                result["fact_guard_verdict"],
                Jsonb(result["fact_diff_json"]),
                Jsonb(result["quality_signals"]),
            ),
        )
        event(
            request,
            wsId,
            "generation.candidate.created",
            {"session_id": str(session), "candidate_rank": index},
        )
    draft = None
    if data.save_draft:
        draft = uuid4()
        result = candidates[0]
        rows(
            """
            INSERT INTO app.drafts (id,tenant_id,workspace_id,generation_session_id,approval_state,
                created_by_user_id) VALUES (%s,%s,%s,%s,%s,%s)
        """,
            (draft, tenant, wsId, session, result["approval_state"], actor),
        )
        spans = tuple(FormatSpanUtf16(**span) for span in result.get("spans_utf16", ()))
        save_revision(tenant, draft, 1, CanonicalRichText(result["text"], spans), None, None, [])
    response = {
        "session_id": str(session),
        "draft_id": str(draft) if draft else None,
        "candidates": candidates,
    }
    rows(
        """
        UPDATE app.generation_sessions SET completed_at=clock_timestamp(),result_json=%s
        WHERE id=%s
    """,
        (Jsonb(response), session),
    )
    return response
