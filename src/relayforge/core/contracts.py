from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class TenantContext:
    tenant_id: UUID


class ClockPort(Protocol):
    def now(self) -> datetime: ...


class UnitOfWorkPort(Protocol):
    def commit(self) -> None: ...

    def rollback(self) -> None: ...
