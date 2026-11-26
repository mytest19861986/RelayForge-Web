import json
import math
import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from relayforge.adapters.net_guard import OutboundTransportError, SafeDialer, SSRFBlockedError

MODALITIES = {"TEXT", "VISION", "IMAGE", "TTS", "VIDEO"}
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")
ERRORS = {400: "BAD_REQUEST", 401: "AUTH_INVALID", 403: "FORBIDDEN", 404: "MODEL_NOT_FOUND"}


class AIError(RuntimeError):
    def __init__(self, code, *, status=None, retry_after=None, spent=False):
        super().__init__(code)
        self.code, self.status, self.retry_after, self.spent = code, status, retry_after, spent


@dataclass(frozen=True)
class Generation:
    text: str
    self_check: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_micros: int | None


def nonnegative(value):
    return value if type(value) is int and 0 <= value <= 2**31 - 1 else None


def compatible_origin(value):
    if not isinstance(value, str):
        raise ValueError("EXPECTED_HTTPS_ORIGIN")
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or (parts.query or parts.fragment or parts.path not in ("", "/", "/v1", "/v1/"))
    ):
        raise ValueError("EXPECTED_HTTPS_ORIGIN")
    return f"https://{parts.netloc}"


def error_response(response, payload, *, generation):
    status = response.status_code
    if status == 429:
        error = payload.get("error", {})
        evidence = json.dumps(error).casefold()
        zero = any(
            marker in evidence
            for marker in (
                "insufficient_quota",
                "quota_zero",
                "quota limit: 0",
                "limit: 0",
                '"quotavalue": "0"',
            )
        )
        retry = response.headers.get("Retry-After", "")
        delay = (
            min(int(retry), 86400)
            if retry.isascii() and retry.isdigit() and (len(retry) <= 8)
            else None
        )
        if isinstance(error, dict) and isinstance(error.get("details"), list):
            for detail in error["details"]:
                if isinstance(detail, dict) and str(detail.get("@type", "")).endswith("RetryInfo"):
                    seconds = str(detail.get("retryDelay", ""))
                    if re.fullmatch(r"[0-9]{1,5}(?:\.[0-9]{1,9})?s", seconds):
                        delay = max(delay or 0, min(math.ceil(float(seconds[:-1])), 86400))
        if zero or delay is None:
            return AIError("QUOTA_ZERO", status=status)
        return AIError("RATE_LIMIT_RETRYABLE", status=status, retry_after=delay)
    return AIError(
        ERRORS.get(status, "UPSTREAM_5XX" if status >= 500 else "BAD_REQUEST"),
        status=status,
        spent=generation and status >= 500,
    )


class OpenAiHttpAdapter:
    def __init__(self, token, base_url=None, *, dialer=None):
        self.token = token
        self.origin = compatible_origin(base_url) if base_url else "https://api.openai.com"
        self.dialer = dialer or SafeDialer(extra_secrets=(token,))
        self.timeout_sec = 60

    def headers(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def exchange(self, path, body=None, *, max_bytes=2 * 1024 * 1024, encoded=None):
        content = json.dumps(body).encode() if body is not None else None
        headers = self.headers()
        if encoded is not None:
            encoded_headers, content = encoded
            headers.update(encoded_headers)
        try:
            response = self.dialer.request(
                "GET" if content is None else "POST",
                self.origin + path,
                headers=headers,
                content=content,
                follow_redirects=False,
                allow_error_response=True,
                max_response_bytes=max_bytes,
                timeout_sec=self.timeout_sec,
            )
        except SSRFBlockedError:
            raise AIError("FORBIDDEN") from None
        except OutboundTransportError:
            raise AIError("TIMEOUT", spent=content is not None) from None
        if response.is_success:
            return response
        try:
            payload = response.json()
        except ValueError:
            payload = None
        raise error_response(
            response, payload if isinstance(payload, dict) else {}, generation=content is not None
        )

    def request(self, path, body=None, *, max_bytes=2 * 1024 * 1024, encoded=None):
        response = self.exchange(path, body, max_bytes=max_bytes, encoded=encoded)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise AIError("SCHEMA_INVALID", spent=body is not None or encoded is not None)
        return payload

    def discover(self):
        models = self.request("/v1/models").get("data")
        if not isinstance(models, list):
            raise AIError("SCHEMA_INVALID")
        result = []
        for row in models:
            model = row.get("id", "") if isinstance(row, dict) else ""
            if not isinstance(model, str) or not MODEL_ID.fullmatch(model):
                continue
            lower = model.lower()
            if re.match(r"^(gpt-image|dall-e)", lower):
                kinds = ["IMAGE"]
            elif "tts" in lower:
                kinds = ["TTS"]
            elif lower.startswith(("sora-", "video-")):
                kinds = ["VIDEO"]
            elif lower.startswith(("gpt-4o", "gpt-4.1")) and not any(
                name in lower for name in ("audio", "transcribe", "realtime", "search", "preview")
            ):
                kinds = ["TEXT", "VISION"]
            elif lower.startswith(("gpt-", "o1", "o3", "o4")) and not any(
                name in lower for name in ("audio", "transcribe", "realtime")
            ):
                kinds = ["TEXT"]
            else:
                continue
            result.append((model, kinds))
        return result

    def generate(self, model, system, prompt, *, image=None):
        content = (
            prompt
            if not image
            else [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image}},
            ]
        )
        payload = self.request(
            "/v1/chat/completions",
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": content},
                ],
                "response_format": {"type": "json_object"},
                "max_completion_tokens": 4096,
            },
        )
        try:
            usage = payload.get("usage") or {}
            return result(
                payload["choices"][0]["message"]["content"],
                usage,
                "prompt_tokens",
                "completion_tokens",
            )
        except (KeyError, TypeError, IndexError):
            raise AIError("SCHEMA_INVALID", spent=True) from None


class GeminiHttpAdapter(OpenAiHttpAdapter):
    def __init__(self, token, *, dialer=None):
        super().__init__(token, dialer=dialer)
        self.origin = "https://generativelanguage.googleapis.com"

    def headers(self):
        return {"x-goog-api-key": self.token, "Content-Type": "application/json"}

    def discover(self):
        payload = self.request("/v1beta/models")
        if not isinstance(models := payload.get("models", []), list):
            raise AIError("SCHEMA_INVALID")
        result_ = []
        for row in models:
            name = row.get("name") if isinstance(row, dict) else None
            if not isinstance(name, str) or not MODEL_ID.fullmatch(name):
                continue
            model = name.removeprefix("models/")
            methods = row.get("supportedGenerationMethods", [])
            if "tts" in model:
                kinds = ["TTS"]
            elif model.startswith("imagen") or "-image" in model:
                kinds = ["IMAGE"]
            elif model.startswith(("veo", "gemini-omni-")):
                kinds = ["VIDEO"]
            elif isinstance(methods, list) and "generateContent" in methods:
                kinds = (
                    ["TEXT", "VISION"] if model.startswith(("gemini-2.", "gemini-3")) else ["TEXT"]
                )
            else:
                continue
            result_.append((model, kinds))
        return result_

    def generate(self, model, system, prompt, *, image=None):
        parts = [{"text": prompt}]
        if image:
            media, data = image.removeprefix("data:").split(";base64,", 1)
            parts.append({"inlineData": {"mimeType": media, "data": data}})
        payload = self.request(
            f"/v1beta/models/{quote(model, safe='')}:generateContent",
            {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "maxOutputTokens": 4096,
                },
            },
        )
        try:
            usage = payload.get("usageMetadata") or {}
            return result(
                payload["candidates"][0]["content"]["parts"][0]["text"],
                usage,
                "promptTokenCount",
                "candidatesTokenCount",
            )
        except (KeyError, TypeError, IndexError):
            raise AIError("SCHEMA_INVALID", spent=True) from None


def result(text, usage, input_key, output_key):
    try:
        value = json.loads(text)
        if not isinstance(value, dict) or set(value) - {"text", "self_check"}:
            raise ValueError
        if not isinstance(value.get("text"), str) or not 0 < len(value["text"].strip()) <= 32768:
            raise ValueError
        if "\x00" in value["text"]:
            raise ValueError
        value["text"].encode()
        check = value.get("self_check")
        if check is not None and (
            not isinstance(check, str) or len(check) > 2000 or "\x00" in check
        ):
            raise ValueError
        if check:
            check.encode()
        if not isinstance(usage, dict):
            usage = {}
        return Generation(
            value["text"],
            check,
            nonnegative(usage.get(input_key)),
            nonnegative(usage.get(output_key)),
            nonnegative(usage.get("cost_micros")),
        )
    except (ValueError, TypeError):
        raise AIError("SCHEMA_INVALID", spent=True) from None


def adapter(provider, token):
    if provider["provider_type"] == "GEMINI":
        return GeminiHttpAdapter(token)
    return OpenAiHttpAdapter(
        token, provider["base_url"] if (provider["provider_type"] == "COMPATIBLE_OPENAI") else None
    )
