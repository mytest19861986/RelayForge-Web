import uuid
from collections.abc import Callable

from django.db import transaction


def on_tenant_commit(
    tenant_id: uuid.UUID, callback: Callable[[uuid.UUID], None], *, using: str | None = None
) -> None:
    if not isinstance(tenant_id, uuid.UUID):
        raise ValueError("tenant_id must be a UUID")
    bound_tid = uuid.UUID(str(tenant_id))
    transaction.on_commit(lambda: callback(bound_tid), using=using)  # noqa: TID251  # rf-gate: allow-raw-on-commit
