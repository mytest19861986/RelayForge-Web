from relayforge.core.security import Role, permitted
from relayforge.web.db import first
from relayforge.web.problems import ProblemError


def ack(request, *, identifier=None, **fields):
    return {"ok": True, "correlation_id": request.correlation_id, "id": identifier, **fields}


def require_permission(request, action):
    if not permitted(Role(request.tenant["role"]), action):
        raise ProblemError(403)


def page(items, limit, *, cursor_field="id"):
    has_more = len(items) > limit
    return {
        "items": items[:limit],
        "page_info": {
            "limit": limit, "has_more": has_more,
            "next_cursor": str(items[limit - 1][cursor_field]) if has_more else None,
        },
    }


def require_workspace(workspace_id):
    if not first("SELECT id FROM app.workspaces WHERE id = %s", (workspace_id,)):
        raise ProblemError(404)
