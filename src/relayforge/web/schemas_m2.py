from typing import Annotated, Literal
from uuid import UUID

from ninja import Schema
from pydantic import Field, field_validator

from relayforge.adapters.s3_storage import MAX_EXPORT_BYTES
from relayforge.web.schemas import Label

Platform = Literal["TELEGRAM", "BALE", "EITAA", "RUBIKA_EXPERIMENTAL", "SOROUSH_EXPERIMENTAL"]
Backlog = Literal["NEW_ONLY", "INCLUDE_BACKLOG"]
Channel = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_@:-]+$")]


class SourceInput(Schema):
    source_kind: Literal["TELEGRAM_BOT_CHANNEL", "TELEGRAM_DESKTOP_EXPORT", "BALE_BOT_CHANNEL",
                         "MANUAL_STUDIO", "RSS_FEED", "WEBHOOK_INBOUND"]
    canonical_channel_id: Channel
    display_title: Label
    secret_id: UUID | None = None
    webhook_platform: Literal["telegram", "bale"] | None = None
    republish_rights_attested: bool = False


class ExportInput(Schema):
    file_size_bytes: Annotated[int, Field(strict=True, ge=1, le=MAX_EXPORT_BYTES)]
    job_mode: Literal["ARCHIVE_INGEST", "CHANNEL_ANALYSIS_ONLY"] = "ARCHIVE_INGEST"


class DestinationInput(Schema):
    platform: Platform
    display_name: Label
    canonical_chat_id: Channel
    secret_id: UUID
    backlog_mode: Backlog = "NEW_ONLY"


class DestinationPatch(Schema):
    display_name: Label | None = None
    backlog_mode: Backlog | None = None
    is_enabled: Annotated[bool, Field(strict=True)] | None = None
    is_archived: Annotated[bool, Field(strict=True)] | None = None


class TestPublishInput(Schema):
    explicit_user_confirmation: Literal[True]
    text: Annotated[str, Field(min_length=1, max_length=8192)] = "پیام آزمایشی RelayForge"

    @field_validator("explicit_user_confirmation", mode="before")
    @classmethod
    def require_explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation is required")
        return value
