from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from ninja import Schema
from pydantic import Field, field_validator

from relayforge.web.schemas import Label, WorkspaceSlug

StrictBool = Annotated[bool, Field(strict=True)]
Destinations = Annotated[list[UUID], Field(min_length=1, max_length=50)]
Automation = Literal["DRAFT_ONLY", "APPROVAL_REQUIRED", "AUTO_PUBLISH_BY_POLICY"]


class BrandInput(Schema):
    id: UUID | None = None
    display_name: Label
    channel_handle: str | None = None
    official_url: str | None = None
    signature_template: Annotated[str, Field(max_length=2000)] = ""
    separator_line: Annotated[str, Field(max_length=100)] = "━━━━━━━━━━━━━━"
    placement_policy: Literal["HEADER", "FOOTER", "BOTH", "NONE"] = "FOOTER"
    visual_tokens: dict = Field(default_factory=dict)
    logo_asset_id: UUID | None = None
    ownership_attested: StrictBool = False


class RoutePolicy(Schema):
    source_ids: Annotated[list[UUID], Field(min_length=1, max_length=50)]
    destination_ids: Destinations
    destination_overrides: dict[UUID, dict] = Field(default_factory=dict)
    filter_ast: dict = Field(default_factory=dict)
    transform_rules: dict = Field(default_factory=dict)
    schedule_policy: dict = Field(default_factory=dict)
    daily_post_cap: Annotated[int, Field(strict=True, ge=1, le=5000)] = 50


class RouteInput(RoutePolicy):
    slug: WorkspaceSlug
    display_name: Label
    route_mode: Literal["COPY", "RULES"] = "COPY"
    automation_level: Automation = "DRAFT_ONLY"


class RoutePatch(Schema):
    display_name: Label | None = None
    route_mode: Literal["COPY", "RULES"] | None = None
    automation_level: Automation | None = None
    is_enabled: StrictBool | None = None
    policy: RoutePolicy | None = None


class RouteItem(Schema):
    content_item_id: UUID | None = None
    sample_text: Annotated[str, Field(max_length=32768)] | None = None


class Span(Schema):
    offset: Annotated[int, Field(strict=True, ge=0)]
    length: Annotated[int, Field(strict=True, ge=1)]
    kind: str
    url: str | None = None


class DraftInput(Schema):
    title_text: Annotated[str, Field(max_length=200)] | None = None
    body_text: Annotated[str, Field(max_length=32768)]
    spans_utf16: Annotated[list[Span], Field(max_length=1000)] = Field(default_factory=list)
    brand_identity_id: UUID | None = None
    media_asset_ids: Annotated[list[UUID], Field(max_length=20)] = Field(default_factory=list)


class DraftPatch(Schema):
    expected_draft_revision_no: int | None = Field(default=None, ge=1)
    title_text: Annotated[str, Field(max_length=200)] | None = None
    body_text: Annotated[str, Field(max_length=32768)] | None = None
    spans_utf16: Annotated[list[Span], Field(max_length=1000)] | None = None
    brand_identity_id: UUID | None = None
    media_asset_ids: Annotated[list[UUID], Field(max_length=20)] | None = None


class PreviewInput(Schema):
    destination_ids: Destinations
    album_fallback_policy: Literal["SEQUENTIAL_POSTS", "CANCEL_FOR_INCOMPATIBLE_DEST"] = (
        "SEQUENTIAL_POSTS"
    )


class PublishSchedule(Schema):
    kind: Literal["IMMEDIATE", "AT_TIMESTAMP"]
    scheduled_for: datetime | None = None


class PublishInput(PreviewInput):
    expected_draft_revision_no: Annotated[int, Field(strict=True, ge=1)]
    mode: Literal["EXPLICIT_TEST_SEND", "LIVE_PUBLISH", "EVERGREEN_REPUBLISH"]
    explicit_user_confirmation: Literal[True]
    schedule: PublishSchedule | None = None

    @field_validator("explicit_user_confirmation", mode="before")
    @classmethod
    def require_explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation is required")
        return value


class ReconcileInput(Schema):
    expected_lease_epoch: Annotated[int, Field(strict=True, ge=1)]
    resolution_action: Literal["CONFIRM_SENT", "CONFIRM_NOT_SENT_REQUEUE", "DISCARD_DO_NOT_SEND"]
    reason: Annotated[str, Field(min_length=3, max_length=1000)]
    acknowledge_duplicate_risk: StrictBool = False


class StopInput(Schema):
    enabled: StrictBool
    cancel_waiting: StrictBool = False
