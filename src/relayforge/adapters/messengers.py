"""Bot-only messenger contracts. Probe and publish are intentionally separate."""

import json
import re
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from relayforge.adapters.net_guard import OutboundTransportError, SafeDialer
from relayforge.core.rich_text import CompilationResult


@dataclass(frozen=True)
class PublishReceipt:
    remote_message_id: str


class MessengerError(RuntimeError):
    def __init__(self, category, *, retry_after_sec=None, delivery_uncertain=False):
        super().__init__(category)
        self.category = category
        self.retry_after_sec = retry_after_sec
        self.delivery_uncertain = delivery_uncertain


class BotPollingConflictError(MessengerError):
    def __init__(self):
        super().__init__("TRANSIENT_NETWORK")


def _error(status, document, headers, *, polling=False):
    if polling and status == 409:
        return BotPollingConflictError()
    if status == 429:
        params = document.get("parameters")
        delay = params.get("retry_after") if isinstance(params, dict) else None
        if not isinstance(delay, int) or not 0 < delay <= 86400:
            value = headers.get("Retry-After", "")
            delay = min(int(value), 86400) if re.fullmatch(r"[0-9]{1,8}", value) else None
        return MessengerError("RATE_LIMIT_429", retry_after_sec=delay)
    if status == 402:
        return MessengerError("QUOTA_EXHAUSTED")
    if status == 401:
        return MessengerError("AUTH_REVOKED")
    if status == 403:
        return MessengerError("PERMISSION_DENIED")
    if 500 <= status <= 599 or status in {408, 409}:
        return MessengerError("TRANSIENT_NETWORK")
    return MessengerError("INCOMPATIBLE_PAYLOAD")


class TelegramBotAdapter:
    base_url = "https://api.telegram.org"
    platform_tier = "GA"
    _token_pattern = r"[0-9]+:[A-Za-z0-9_-]+"

    def __init__(self, dialer=None):
        self._dialer = dialer

    def _check_token(self, token):
        if len(token) > 512 or not re.fullmatch(self._token_pattern, token):
            raise MessengerError("AUTH_REVOKED")

    def _url(self, token, method):
        return f"{self.base_url}/bot{quote(token, safe=':')}/{method}"

    def _encode(self, payload):
        return {"Content-Type": "application/json"}, json.dumps(payload).encode()

    def _result(self, document):
        if document.get("ok") is True:
            return document.get("result", {})
        return None

    def _call(self, token, method, payload, *, polling=False):
        self._check_token(token)
        dialer = self._dialer or SafeDialer(extra_secrets=(token,))
        headers, data = self._encode(payload)
        try:
            response = dialer.request(
                "POST", self._url(token, method), headers=headers, content=data,
                allow_error_response=True, follow_redirects=False,
            )
        except OutboundTransportError:
            raise MessengerError("TRANSIENT_NETWORK", delivery_uncertain=True) from None
        try:
            document = response.json()
        except ValueError:
            document = {}
        if not isinstance(document, dict):
            document = {}
        result = self._result(document)
        if response.is_success and result is not None:
            return result
        code = document.get("error_code")
        status = code if response.is_success and type(code) is int else response.status_code
        raise _error(status, document, response.headers, polling=polling)

    def test_connection(self, token):
        bot = self._call(token, "getMe", {})
        if not isinstance(bot, dict) or not bot.get("id"):
            raise MessengerError("INCOMPATIBLE_PAYLOAD")
        return {"status": "BOT_IDENTITY_VERIFIED", "publish_verified": False}

    def resolve_and_verify_source_channel(self, token, chat_ref):
        chat = self._call(token, "getChat", {"chat_id": chat_ref})
        if not isinstance(chat, dict) or chat.get("type") != "channel":
            raise MessengerError("INCOMPATIBLE_PAYLOAD")
        identifier = str(chat.get("id", ""))
        if not self._channel_valid(identifier):
            raise MessengerError("INCOMPATIBLE_PAYLOAD")
        return {"canonical_channel_id": identifier, "display_title": str(chat.get("title") or
                                                                       identifier)[:200]}

    def _channel_valid(self, identifier):
        return re.fullmatch(r"-100[0-9]+", identifier) is not None

    def fetch_live_updates(self, token, offset):
        updates = self._call(token, "getUpdates", {"offset": offset, "limit": 25}, polling=True)
        if not isinstance(updates, list):
            raise MessengerError("INCOMPATIBLE_PAYLOAD")
        output = []
        for update in updates:
            if isinstance(update, dict) and isinstance(post := update.get("channel_post"), dict):
                output.append({"update_id": update.get("update_id"), "post": post,
                               "access_class": bot_access_class(post)})
        return output

    def _message_payload(self, chat_id, payload):
        return {"chat_id": chat_id, "text": payload["plain_text"],
                **({"entities": payload["entities"]} if payload.get("entities") else {})}

    def send_message(self, token, chat_id, compiled_payload):
        if isinstance(compiled_payload, CompilationResult):
            if compiled_payload.length_overflow or (
                compiled_payload.album_action == "CANCEL_FOR_INCOMPATIBLE_DEST"
            ):
                raise MessengerError("INCOMPATIBLE_PAYLOAD")
            compiled_payload = compiled_payload.payload
        sent = self._call(token, "sendMessage", self._message_payload(chat_id, compiled_payload))
        identifier = sent.get("message_id") if isinstance(sent, dict) else None
        if not isinstance(identifier, (str, int)) or not str(identifier):
            raise MessengerError("TRANSIENT_NETWORK", delivery_uncertain=True)
        if token in str(identifier) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(identifier)):
            raise MessengerError("TRANSIENT_NETWORK", delivery_uncertain=True)
        return PublishReceipt(str(identifier))

    def test_publish(self, token, chat_id, compiled_payload, *, explicit_user_confirmation):
        if explicit_user_confirmation is not True:
            raise MessengerError("PERMISSION_DENIED")
        return self.send_message(token, chat_id, compiled_payload)


def bot_access_class(post):
    chat = post.get("chat")
    if post.get("has_protected_content") is True or (
        isinstance(chat, dict) and chat.get("has_protected_content") is True
    ):
        return "PROTECTED_NO_FORWARD"
    for name in ("photo", "video", "document", "audio", "voice", "video_note", "animation",
                 "sticker"):
        if name not in post:
            continue
        values = post[name] if isinstance(post[name], list) else [post[name]]
        if not any(
            isinstance(value, dict) and value.get("file_id")
            and type(value.get("file_size", 0)) is int
            and 0 <= value.get("file_size", 0) <= 20 * 1024 * 1024 for value in values
        ):
            return "REFERENCE_ONLY"
    return "PROCESSABLE"


class BaleBotAdapter(TelegramBotAdapter):
    base_url = "https://tapi.bale.ai"
    _token_pattern = r"[A-Za-z0-9_:-]{10,512}"

    def _channel_valid(self, identifier):
        return re.fullmatch(r"-?[0-9]+", identifier) is not None

    def _message_payload(self, chat_id, payload):
        return {"chat_id": chat_id, "text": payload["html"], "parse_mode": "HTML"}


class EitaayarAdapter(TelegramBotAdapter):
    base_url = "https://eitaayar.ir/api"
    platform_tier = "OUTBOUND_LIMITED"
    _token_pattern = r"[A-Za-z0-9_:-]{12,512}"

    def _url(self, token, method):
        return f"{self.base_url}/{quote(token, safe='')}/{method}"

    def _encode(self, payload):
        return {"Content-Type": "application/x-www-form-urlencoded"}, urlencode(payload).encode()

    def test_connection(self, token):
        self._check_token(token)
        return {"status": "FORMAT_ONLY", "publish_verified": False}


class RubikaV3Adapter(TelegramBotAdapter):
    base_url = "https://botapi.rubika.ir/v3"
    platform_tier = "EXPERIMENTAL"
    _token_pattern = r"[A-Za-z0-9_-]{12,512}"

    def _url(self, token, method):
        return f"{self.base_url}/{quote(token, safe='')}/{method}"

    def _result(self, document):
        return document.get("data", {}) if document.get("status") == "OK" else None

    def test_connection(self, token):
        result = self._call(token, "getMe", {})
        if not isinstance(result, dict) or not result.get("bot"):
            raise MessengerError("INCOMPATIBLE_PAYLOAD")
        return {"status": "BOT_IDENTITY_VERIFIED", "publish_verified": False}

    def _message_payload(self, chat_id, payload):
        return {"chat_id": chat_id, "text": payload["plain_text"], "metadata": {
            "meta_data_parts": payload.get("meta_data_parts", []),
        }}


class SoroushPlusCleanRoomAdapter(TelegramBotAdapter):
    base_url = "https://bot.splus.ir"
    platform_tier = "EXPERIMENTAL"
    _token_pattern = r"[A-Za-z0-9_:-]{12,512}"

    def _url(self, token, method):
        return f"{self.base_url}/{quote(token, safe='')}/{method}"

    def _result(self, document):
        if document.get("resultCode") == 200:
            result = document.get("result") or document
            if isinstance(result, dict):
                return {"message_id": result.get("message_id") or result.get("messageId")}
        return None

    def test_connection(self, token):
        self._check_token(token)
        return {"status": "FORMAT_ONLY", "publish_verified": False}

    def _message_payload(self, chat_id, payload):
        return {"to": chat_id, "type": "TEXT", "body": payload["plain_text"]}


CONNECTORS = {
    "TELEGRAM": TelegramBotAdapter,
    "BALE": BaleBotAdapter,
    "EITAA": EitaayarAdapter,
    "RUBIKA_EXPERIMENTAL": RubikaV3Adapter,
    "SOROUSH_EXPERIMENTAL": SoroushPlusCleanRoomAdapter,
}
