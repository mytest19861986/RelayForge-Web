from uuid import NAMESPACE_URL, UUID, uuid5

from ninja import Router

from relayforge.core.security import scrub_sensitive_text
from relayforge.web import schemas_m3 as dto
from relayforge.web.db import audit, first, rows
from relayforge.web.endpoints import ack, page, require_permission, require_workspace
from relayforge.web.problems import ProblemError
from relayforge.web.publications_common import event
from relayforge.web.routes_m2 import get_idempotency_token

router = Router()
WAITING_SQL = "('PENDING', 'SCHEDULED', 'READY', 'RETRY_WAIT')"


@router.get("/workspaces/{wsId}/deliveries")
def deliveries(request, wsId: UUID, limit: int = 50, cursor: UUID | None = None,
               status: str | None = None, publication_version_id: UUID | None = None):
    require_workspace(wsId)
    if not 1 <= limit <= 100:
        raise ProblemError(422)
    return page(rows("""
        SELECT id, destination_id, publication_version_id, status, scheduled_for, next_retry_at,
               attempts, max_attempts, lease_epoch, remote_message_id, last_error_safe
        FROM app.delivery_tasks WHERE workspace_id = %s AND (%s::uuid IS NULL OR id > %s)
            AND (%s::text IS NULL OR status = %s)
            AND (%s::uuid IS NULL OR publication_version_id = %s)
        ORDER BY id LIMIT %s
    """, (wsId, cursor, cursor, status, status, publication_version_id, publication_version_id,
          limit + 1)), limit)


@router.post("/workspaces/{wsId}/deliveries/{taskId}/cancel")
def cancel(request, wsId: UUID, taskId: UUID):
    require_permission(request, "publish")
    task = first("SELECT status FROM app.delivery_tasks WHERE workspace_id = %s AND id = %s "
                 "FOR UPDATE", (wsId, taskId))
    if not task:
        raise ProblemError(404)
    if task["status"] != "CANCELLED":
        result = first(f"""
            UPDATE app.delivery_tasks SET status = 'CANCELLED', lease_epoch = lease_epoch + 1,
                updated_at = clock_timestamp()
            WHERE workspace_id = %s AND id = %s AND status IN {WAITING_SQL} RETURNING id
        """, (wsId, taskId))
        if not result:
            raise ProblemError(409, "DELIVERY_NOT_CANCELLABLE")
        audit(request, "DELIVERY_CANCELLED", "DELIVERY", taskId, workspace=wsId)
    return ack(request, identifier=taskId, status="CANCELLED")


@router.post("/workspaces/{wsId}/deliveries/{taskId}/reconcile")
def reconcile(request, wsId: UUID, taskId: UUID, data: dto.ReconcileInput):
    require_permission(request, "publish")
    key = get_idempotency_token(request)
    tenant = request.tenant["tenant_id"]
    record = uuid5(NAMESPACE_URL, f"relayforge:reconcile:{tenant}:{wsId}:{key}")
    if data.resolution_action == "CONFIRM_NOT_SENT_REQUEUE" and not data.acknowledge_duplicate_risk:
        raise ProblemError(422, "DUPLICATE_RISK_ACKNOWLEDGEMENT_REQUIRED")
    reason = scrub_sensitive_text(data.reason.strip())
    if len(reason) < 3:
        raise ProblemError(422)
    task = first("""
        SELECT id, lease_epoch, status FROM app.delivery_tasks
        WHERE workspace_id = %s AND id = %s FOR UPDATE
    """, (wsId, taskId))
    if not task:
        raise ProblemError(404)
    prior = first("""
        SELECT delivery_task_id, task_lease_epoch, resolution_action, acknowledge_duplicate_risk,
               reason FROM app.reconciliation_records WHERE id = %s
    """, (record,))
    if prior:
        if tuple(prior.values()) != (
            taskId, data.expected_lease_epoch, data.resolution_action,
            data.acknowledge_duplicate_risk, reason,
        ):
            raise ProblemError(409, "IDEMPOTENCY_KEY_REUSED")
        return ack(request, identifier=record)
    if task["status"] != "UNCERTAIN" or task["lease_epoch"] != data.expected_lease_epoch:
        raise ProblemError(409, "RF018")
    rows("SELECT app.fn_web_reconcile_uncertain_task(%s,%s,%s,%s,%s,%s,%s)",
         (record, taskId, data.expected_lease_epoch, data.resolution_action,
          data.acknowledge_duplicate_risk, str(request.tenant["user_id"]), reason))
    audit(request, "DELIVERY_RECONCILED", "DELIVERY", taskId, workspace=wsId)
    event(request, wsId, "delivery.reconciled", {
        "delivery_task_id": str(taskId), "reconciliation_id": str(record),
        "resolution_action": data.resolution_action,
    })
    return ack(request, identifier=record)


@router.post("/workspaces/{wsId}/ops/emergency-stop")
def emergency_stop(request, wsId: UUID, data: dto.StopInput):
    require_permission(request, "manage_workspaces")
    rows("SELECT pg_advisory_xact_lock(hashtextextended(%s, 6))", (str(wsId),))
    if not first("""
        UPDATE app.workspaces SET emergency_stop = %s, updated_at = clock_timestamp()
        WHERE id = %s RETURNING id
    """, (data.enabled, wsId)):
        raise ProblemError(404)
    cancelled = 0
    if data.enabled and data.cancel_waiting:
        cancelled = first(f"""
            WITH changed AS (
                UPDATE app.delivery_tasks SET status = 'CANCELLED', lease_epoch = lease_epoch + 1,
                    updated_at = clock_timestamp()
                WHERE workspace_id = %s AND status IN {WAITING_SQL} RETURNING id
            ) SELECT count(*) AS count FROM changed
        """, (wsId,))["count"]
    in_flight = first("""
        SELECT count(*) AS count FROM app.delivery_tasks WHERE workspace_id = %s
          AND status = 'DISPATCHING_IN_FLIGHT'
    """, (wsId,))["count"]
    audit(request, "EMERGENCY_STOP_TOGGLED", "WORKSPACE", wsId, workspace=wsId)
    if data.enabled:
        event(request, wsId, "ops.emergency_stop.activated", {"in_flight_count": in_flight})
    return ack(request, emergency_stop=data.enabled, cancelled_count=cancelled,
               in_flight_count=in_flight, remote_deletion_performed=False)


@router.get("/workspaces/{wsId}/events/poll")
def poll(request, wsId: UUID, limit: int = 50, cursor: UUID | None = None):
    require_workspace(wsId)
    if not 1 <= limit <= 100:
        raise ProblemError(422)
    marker = None
    if cursor:
        marker = first(
            "SELECT created_at FROM app.domain_outbox WHERE workspace_id = %s AND id = %s",
            (wsId, cursor),
        )
        if not marker:
            raise ProblemError(404)
    payload = page(rows("""
        SELECT id AS event_id, tenant_id, workspace_id, event_type, '1.0' AS schema_version,
               correlation_id, created_at AS occurred_at, 'M09' AS producer_module,
               payload_json AS payload
        FROM app.domain_outbox WHERE workspace_id = %s
          AND (%s::uuid IS NULL OR (created_at, id) > (%s::timestamptz, %s::uuid))
        ORDER BY created_at, id LIMIT %s
    """, (wsId, cursor, marker["created_at"] if marker else None, cursor, limit + 1)),
        limit, cursor_field="event_id")
    items = payload["items"]
    if items:
        payload["page_info"]["next_cursor"] = str(items[-1]["event_id"])
    elif cursor:
        payload["page_info"]["next_cursor"] = str(cursor)
    return payload
