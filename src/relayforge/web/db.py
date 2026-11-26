from uuid import uuid4

from django.db import connections


def rows(query, params=(), *, using="default"):
    with connections[using].cursor() as cursor:
        cursor.execute(query, params)
        if not cursor.description:
            return []
        columns = [column.name for column in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def first(query, params=(), *, using="default"):
    return next(iter(rows(query, params, using=using)), None)


def set_context(tenant_id, user_id=None, *, using="default"):
    rows(
        "SELECT set_config('app.tenant_id', %s, true), set_config('app.user_id', %s, true)",
        (str(tenant_id), str(user_id) if user_id else ""), using=using,
    )


def lock_tenant(tenant_id, *, exclusive=False, using="default"):
    if exclusive:
        query = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 1))"
    else:
        query = "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 1))"
    rows(query, (str(tenant_id),), using=using)


def audit(request, action, target_type, target_id, *, workspace=None, using="default"):
    rows("""
        INSERT INTO app.audit_events
            (id, tenant_id, workspace_id, actor_user_id, actor_role, action_code,
             target_type, target_id, correlation_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        uuid4(), request.tenant["tenant_id"], workspace, request.tenant.get("user_id"),
        request.tenant["role"], action, target_type, str(target_id), request.correlation_id,
    ), using=using)
