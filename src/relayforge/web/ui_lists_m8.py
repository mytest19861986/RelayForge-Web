"""Allowlisted metadata for workspace pickers."""

from relayforge.web.db import rows
from relayforge.web.endpoints import page, require_workspace
from relayforge.web.problems import ProblemError


def listing(ws, cursor, limit, table, columns, scope="workspace_id"):
    if scope == "workspace_id":
        require_workspace(ws)
    if not 1 <= limit <= 100:
        raise ProblemError(422)
    return page(rows(f"""
        SELECT {columns} FROM app.{table} WHERE {scope}=%s
          AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s
    """, (ws, cursor, cursor, limit + 1)), limit)
