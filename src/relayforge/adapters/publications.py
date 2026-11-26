"""Transactional publication and outbox writes shared across entry points."""

from uuid import uuid4

from psycopg.types.json import Jsonb


def outbox(cursor, tenant, workspace, event, correlation, payload):
    # Per-workspace commit ordering makes a polling watermark safe across writers.
    cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 4))", (str(workspace),))
    cursor.execute("""
        INSERT INTO app.domain_outbox
            (id, tenant_id, workspace_id, event_type, correlation_id, payload_json)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (uuid4(), tenant, workspace, event, str(correlation), Jsonb(payload)))


def freeze_publication(
    cursor, tenant, workspace, identifier, targets, *, intent, correlation, now,
    draft_id=None, draft_revision=None, route_id=None, route_revision=None, actor=None,
    request_hash=None, source_item=None,
):
    snapshot = {
        "request_sha256": request_hash,
        "source_content_item_id": str(source_item) if source_item else None,
        "destinations": {str(target["id"]): {
            "platform": target["platform"], "chat_id": target["chat_id"],
            "payload": target["payload"], "degradations": target["degradations"],
            "scheduled_for": target["scheduled_for"].isoformat(),
            "catch_up": target.get("catch_up"), "draft_text": target.get("draft_text"),
            "draft_spans": target.get("draft_spans"),
        } for target in targets},
    }
    cursor.execute("""
        INSERT INTO app.publication_versions
            (id, tenant_id, workspace_id, draft_id, draft_revision_no, route_id,
             route_policy_revision_no, publish_intent, frozen_payload_json, approved_by_user_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (identifier, tenant, workspace, draft_id, draft_revision, route_id,
          route_revision, intent, Jsonb(snapshot), actor))
    task_ids = []
    for target in targets:
        task = uuid4()
        scheduled = target["scheduled_for"]
        cursor.execute("""
            INSERT INTO app.delivery_tasks
                (id, tenant_id, workspace_id, route_id, destination_id, publication_version_id,
                 idempotency_key, status, scheduled_for, payload_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (task, tenant, workspace, route_id, target["id"], identifier,
              f"{identifier}:{target['id']}", "SCHEDULED" if scheduled > now else "READY",
              scheduled, Jsonb({"correlation_id": str(correlation)})))
        task_ids.append(task)
    outbox(cursor, tenant, workspace, "publication.version.frozen", correlation,
           {"publication_version_id": str(identifier), "delivery_task_count": len(task_ids)})
    return task_ids
