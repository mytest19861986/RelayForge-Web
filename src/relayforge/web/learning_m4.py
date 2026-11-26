from typing import Literal
from uuid import UUID, uuid4

from ninja import Router, Schema
from psycopg.types.json import Jsonb
from pydantic import Field

from relayforge.core.intelligence import edit_signals
from relayforge.web.db import audit, first, rows
from relayforge.web.endpoints import ack, page, require_permission, require_workspace
from relayforge.web.problems import ProblemError
from relayforge.web.publications_common import draft_record, event, get_brand, get_destinations

router = Router()
ROOT = "/workspaces/{wsId}/learning"


class Feedback(Schema):
    brand_identity_id: UUID
    destination_id: UUID | None = None
    draft_id: UUID | None = None
    before: str = Field(max_length=32768)
    after: str = Field(max_length=32768)


class Review(Schema):
    review_state: Literal["ACCEPTED", "REJECTED"]


def lock_learning(workspace):
    rows("SELECT pg_advisory_xact_lock(hashtextextended(%s,11))", (str(workspace),))


def _rule(workspace, identifier):
    record = first(
        """
        SELECT id,brand_identity_id,destination_id,rule_statement,review_state
        FROM app.learned_preference_rules WHERE workspace_id=%s AND id=%s FOR UPDATE
    """,
        (workspace, identifier),
    )
    if not record:
        raise ProblemError(404)
    return record


@router.post(ROOT + "/feedback")
def feedback(request, wsId: UUID, data: Feedback):
    require_permission(request, "edit_content")
    require_workspace(wsId)
    lock_learning(wsId)
    if not first(
        """
        SELECT 1 AS allowed FROM app.workspace_settings s JOIN app.external_consents c
          ON c.workspace_id=s.workspace_id AND c.tenant_id=s.tenant_id
        WHERE s.workspace_id=%s AND s.allow_feedback_learning
          AND c.provider_scope='LOCAL_FEEDBACK_MEMORY' AND c.is_granted
    """,
        (wsId,),
    ):
        return ack(request, status="OPT_IN_REQUIRED")
    brand, dest = data.brand_identity_id, data.destination_id
    get_brand(wsId, brand)
    if dest and get_destinations(wsId, [dest])[0]["brand_identity_id"] not in {brand, None}:
        raise ProblemError(422, "DESTINATION_BRAND_MISMATCH")
    if data.draft_id:
        draft_record(wsId, data.draft_id)
    signals = edit_signals(data.before, data.after)
    tenant, identifier = request.tenant["tenant_id"], uuid4()
    rows(
        """
        INSERT INTO app.feedback_events (id,tenant_id,workspace_id,brand_identity_id,destination_id,
            draft_id,feedback_kind,diff_summary_json)
        VALUES (%s,%s,%s,%s,%s,%s,'TEXT_EDIT_DIFF',%s)
    """,
        (identifier, tenant, wsId, brand, dest, data.draft_id, Jsonb({"signals": signals})),
    )
    for signal in signals:
        count = first(
            """
            SELECT count(*) AS count FROM app.feedback_events WHERE workspace_id=%s
              AND brand_identity_id=%s AND destination_id IS NOT DISTINCT FROM %s
              AND jsonb_exists(diff_summary_json->'signals',%s)
        """,
            (wsId, brand, dest, signal),
        )["count"]
        if count < 3:
            continue
        existing = first(
            """
            SELECT id FROM app.learned_preference_rules WHERE workspace_id=%s
              AND brand_identity_id=%s AND destination_id IS NOT DISTINCT FROM %s
              AND rule_statement=%s
        """,
            (wsId, brand, dest, signal),
        )
        if existing:
            rows(
                """
                UPDATE app.learned_preference_rules SET supporting_evidence_count=%s,
                  updated_at=clock_timestamp() WHERE id=%s
            """,
                (count, existing["id"]),
            )
        else:
            rule = uuid4()
            rows(
                """
                INSERT INTO app.learned_preference_rules (id,tenant_id,workspace_id,
                    brand_identity_id,destination_id,rule_statement,supporting_evidence_count)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
                (rule, tenant, wsId, brand, dest, signal, count),
            )
            event(request, wsId, "learning.rule.proposed", {"rule_id": str(rule)})
    return ack(request, identifier=identifier)


@router.get(ROOT + "/rules")
def rules(request, wsId: UUID, limit: int = 50, cursor: UUID | None = None):
    require_workspace(wsId)
    if not 1 <= limit <= 100:
        raise ProblemError(422)
    return page(
        rows(
            """
        SELECT id,brand_identity_id,destination_id,rule_statement,review_state,
            supporting_evidence_count FROM app.learned_preference_rules WHERE workspace_id=%s
              AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s
    """,
            (wsId, cursor, cursor, limit + 1),
        ),
        limit,
    )


@router.patch(ROOT + "/rules/{ruleId}")
def review(request, wsId: UUID, ruleId: UUID, data: Review):
    require_permission(request, "manage_workspaces")
    lock_learning(wsId)
    _rule(wsId, ruleId)
    rows(
        "UPDATE app.learned_preference_rules SET review_state=%s WHERE id=%s",
        (data.review_state, ruleId),
    )
    audit(request, "LEARNING_RULE_REVIEWED", "LEARNING_RULE", ruleId, workspace=wsId)
    return ack(request, identifier=ruleId)


@router.delete(ROOT + "/rules/{ruleId}")
def forget(request, wsId: UUID, ruleId: UUID):
    require_permission(request, "manage_workspaces")
    lock_learning(wsId)
    rule = _rule(wsId, ruleId)
    rows(
        """
        DELETE FROM app.feedback_events WHERE workspace_id=%s AND brand_identity_id=%s
          AND destination_id IS NOT DISTINCT FROM %s
          AND jsonb_exists(diff_summary_json->'signals',%s)
    """,
        (wsId, rule["brand_identity_id"], rule["destination_id"], rule["rule_statement"]),
    )
    rows("DELETE FROM app.learned_preference_rules WHERE id=%s", (ruleId,))
    return ack(request, identifier=ruleId)


@router.post(ROOT + "/reset")
def reset(request, wsId: UUID):
    require_permission(request, "manage_workspaces")
    require_workspace(wsId)
    lock_learning(wsId)
    rows("DELETE FROM app.learned_preference_rules WHERE workspace_id=%s", (wsId,))
    rows("DELETE FROM app.feedback_events WHERE workspace_id=%s", (wsId,))
    audit(request, "LEARNING_RESET", "WORKSPACE", wsId, workspace=wsId)
    return ack(request)
