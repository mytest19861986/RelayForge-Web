import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum


class Role(StrEnum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    EDITOR = "EDITOR"
    PUBLISHER = "PUBLISHER"
    VIEWER = "VIEWER"


PERMISSIONS = {
    "read": frozenset(Role),
    "edit_content": frozenset({Role.OWNER, Role.ADMIN, Role.EDITOR}),
    "publish": frozenset({Role.OWNER, Role.ADMIN, Role.PUBLISHER}),
    "manage_security": frozenset({Role.OWNER, Role.ADMIN}),
    "manage_workspaces": frozenset({Role.OWNER, Role.ADMIN}),
    "delete_account": frozenset({Role.OWNER}),
}


def permitted(role: Role, action: str) -> bool:
    return role in PERMISSIONS[action]


class SecretKind(StrEnum):
    TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
    BALE_BOT_TOKEN = "BALE_BOT_TOKEN"
    EITAA_API_TOKEN = "EITAA_API_TOKEN"
    RUBIKA_BOT_TOKEN = "RUBIKA_BOT_TOKEN"
    SOROUSH_BOT_TOKEN = "SOROUSH_BOT_TOKEN"
    OPENAI_API_KEY = "OPENAI_API_KEY"
    GEMINI_API_KEY = "GEMINI_API_KEY"
    MEDIA_SEARCH_API_KEY = "MEDIA_SEARCH_API_KEY"
    WEBHOOK_SIGNING_SECRET = "WEBHOOK_SIGNING_SECRET"


class ConsentScope(StrEnum):
    OPENAI_TEXT = "OPENAI_TEXT"
    OPENAI_VISION = "OPENAI_VISION"
    OPENAI_IMAGE = "OPENAI_IMAGE"
    OPENAI_AUDIO = "OPENAI_AUDIO"
    GEMINI_TEXT = "GEMINI_TEXT"
    GEMINI_VISION = "GEMINI_VISION"
    GEMINI_IMAGE = "GEMINI_IMAGE"
    GEMINI_AUDIO = "GEMINI_AUDIO"
    GEMINI_VIDEO = "GEMINI_VIDEO"
    STOCK_MEDIA_SEARCH = "STOCK_MEDIA_SEARCH"
    LOCAL_FEEDBACK_MEMORY = "LOCAL_FEEDBACK_MEMORY"


def mask_secret_preview(raw_secret: str) -> str:
    return "***REDACTED***" if len(raw_secret) < 10 else f"{raw_secret[:2]}…{raw_secret[-4:]}"


_SECRET_VALUE = re.compile(
    r"(?i)((?:/bot|/bot/|eitaayar\.ir/api/)|(?:bearer\s+)|"
    r"(?:\b(?:key|api_key|token|access_token|secret|(?:__Host-)?rf_session)=)|"
    r"(?:\bx-csrftoken\s*:\s*))([^\s&/?#;,\])\"'<>]+)"
)
_PROVIDER_TOKEN = re.compile(r"(?<![\w-])(?:sk-|AIza|gh[po]_)[\w-]+")


def scrub_sensitive_text(text: str, extra_secrets: Sequence[str] = ()) -> str:
    for secret in sorted(set(extra_secrets) - {""}, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return _PROVIDER_TOKEN.sub("[REDACTED]", _SECRET_VALUE.sub(r"\1[REDACTED]", text))


class ErrorCode(StrEnum):
    VALIDATION = "VALIDATION_ERROR"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    UNAVAILABLE = "UNAVAILABLE"
    INTERNAL = "INTERNAL_ERROR"


@dataclass(frozen=True)
class ProblemDetails:
    code: str
    category: str
    message_fa: str
    correlation_id: str
    suggested_action_fa: str | None = None
    retryable: bool = False
    retry_after_sec: int | None = None

    def payload(self) -> dict:
        return {
            "error": {
                key: scrub_sensitive_text(value) if isinstance(value, str) else value
                for key, value in asdict(self).items()
            }
        }


class TenantKeyShreddedError(RuntimeError):
    """The tenant DEK has been destroyed."""


class SecretRevokedError(RuntimeError):
    """The requested secret has been revoked."""
