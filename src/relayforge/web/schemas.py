from typing import Annotated, Literal

from ninja import Schema
from pydantic import Field, SecretStr, field_validator

from relayforge.core.security import ConsentScope, SecretKind

TenantSlug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")]
WorkspaceSlug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")]
Label = Annotated[str, Field(min_length=1, max_length=200)]
Password = Annotated[SecretStr, Field(min_length=12, max_length=256)]
Email = Annotated[str, Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+$")]


class Login(Schema):
    email: Email
    password: Password
    tenant_slug: TenantSlug | None = None


class Provision(Schema):
    slug: TenantSlug
    display_name: Label
    owner_email: Email
    owner_password: Password
    owner_display_name: Label


class PlanQuotas(Schema):
    daily_max_cost_micros: Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
    monthly_max_cost_micros: Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]


class Workspace(Schema):
    slug: WorkspaceSlug
    display_name: Label


class Consent(Schema):
    provider_scope: ConsentScope
    is_granted: Annotated[bool, Field(strict=True)]
    consent_version: Annotated[str, Field(min_length=1, max_length=32)] = "1.0"


class Secret(Schema):
    secret_kind: SecretKind
    label: Label
    raw_secret: Annotated[SecretStr, Field(min_length=1, max_length=8192)]


class DeleteAccount(Schema):
    confirmation_slug: TenantSlug
    acknowledge_irreversible: Literal[True]

    @field_validator("acknowledge_irreversible", mode="before")
    @classmethod
    def require_explicit_acknowledgement(cls, value):
        if value is not True:
            raise ValueError("Explicit acknowledgement is required")
        return value
