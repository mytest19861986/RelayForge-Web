"""Metered media requests over the existing SafeDialer transport."""

import base64
import io
import re
import wave
from dataclasses import dataclass
from time import monotonic, sleep
from urllib.parse import quote, urlsplit

from relayforge.adapters.ai_gateway import AIError, GeminiHttpAdapter, nonnegative
from relayforge.adapters.net_guard import OutboundTransportError, SSRFBlockedError, multipart
from relayforge.core.media_validation import IMAGE_TYPES, MAX_BYTES, media_format

VIDEO_FILE = re.compile(r"files/[A-Za-z0-9._-]+")
OPERATION = re.compile(r"(?:models/[A-Za-z0-9._-]+/)?operations/[A-Za-z0-9._-]+")
PROMPTS = {
    "IMAGE": "Render a single blue circle on a white background.",
    "TTS": "سلام.",
    "VIDEO": "A stationary blue circle on a white background.",
}


@dataclass(frozen=True)
class MediaResult:
    data: bytes | None = None
    operation: str | None = None
    cost_micros: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


def decoded(value):
    if not isinstance(value, str) or len(value) > MAX_BYTES * 4 // 3 + 4:
        raise ValueError
    return base64.b64decode(value, validate=True)


def checked(data, formats, cost=None):
    if media_format(data)[0] not in formats:
        raise ValueError
    return MediaResult(data=data, cost_micros=nonnegative(cost))


def image(api, model, prompt, *, resolution="1K", source=None):
    parts = [{"text": prompt}]
    if source is not None:
        mime, content = source
        parts.append(
            {
                "inlineData": {
                    "mimeType": mime,
                    "data": base64.b64encode(content).decode("ascii"),
                }
            }
        )
    try:
        if isinstance(api, GeminiHttpAdapter):
            endpoint = f"/v1beta/models/{quote(model, safe='')}"
            if model.startswith("imagen"):
                if source is not None:
                    raise AIError("BAD_REQUEST")
                response = api.request(
                    endpoint + ":predict",
                    {"instances": [{"prompt": prompt}], "parameters": {"sampleCount": 1}},
                    max_bytes=MAX_BYTES,
                )
                encoded = response["predictions"][0]["bytesBase64Encoded"]
            else:
                response = api.request(
                    endpoint + ":generateContent",
                    {
                        "contents": [{"parts": parts}],
                        "generationConfig": {
                            "responseModalities": ["IMAGE"],
                            "imageConfig": {"imageSize": resolution},
                        },
                    },
                    max_bytes=MAX_BYTES,
                )
                parts = response["candidates"][0]["content"]["parts"]
                encoded = next(part["inlineData"]["data"] for part in parts if "inlineData" in part)
        else:
            body = {"model": model, "prompt": prompt, "n": 1, "size": "1024x1024"}
            if model.startswith("dall-e"):
                body["response_format"] = "b64_json"
            if source is None:
                response = api.request("/v1/images/generations", body, max_bytes=MAX_BYTES)
            else:
                response = api.request(
                    "/v1/images/edits",
                    max_bytes=MAX_BYTES,
                    encoded=multipart(body, {"image": source}),
                )
            encoded = response["data"][0]["b64_json"]
        return checked(decoded(encoded), IMAGE_TYPES, response.get("cost_micros"))
    except (ValueError, KeyError, IndexError, TypeError, StopIteration):
        raise AIError("SCHEMA_INVALID", spent=True) from None


def speech(api, model, text, voice=None):
    try:
        if isinstance(api, GeminiHttpAdapter):
            response = api.request(
                f"/v1beta/models/{quote(model, safe='')}:generateContent",
                {
                    "contents": [{"parts": [{"text": text}]}],
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {
                            "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice or "Kore"}}
                        },
                    },
                },
                max_bytes=MAX_BYTES,
            )
            audio = response["candidates"][0]["content"]["parts"][0]["inlineData"]
            mime = audio["mimeType"].lower()
            pcm = decoded(audio["data"])
            if mime == "audio/wav":
                return checked(pcm, {"WAV"}, response.get("cost_micros"))
            if not mime.startswith("audio/l16"):
                raise ValueError
            if not pcm or len(pcm) % 2:
                raise ValueError
            stream = io.BytesIO()
            with wave.open(stream, "wb") as writer:
                writer.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                writer.writeframes(pcm)
            return checked(stream.getvalue(), {"WAV"}, response.get("cost_micros"))
        response = api.exchange(
            "/v1/audio/speech",
            {"model": model, "input": text, "voice": voice or "alloy", "response_format": "wav"},
            max_bytes=MAX_BYTES,
        )
        return checked(response.content, {"WAV"})
    except (KeyError, IndexError, TypeError, ValueError):
        raise AIError("SCHEMA_INVALID", spent=True) from None


def start_video(api, model, prompt, *, resolution="1K"):
    if not isinstance(api, GeminiHttpAdapter):
        raise AIError("BAD_REQUEST")
    if model.startswith("gemini-omni-"):
        payload = api.request(
            "/v1beta/interactions",
            {
                "model": model,
                "input": prompt,
                "response_format": {
                    "type": "video",
                    "delivery": "uri",
                    "duration": "3s",
                    "resolution": {"1K": "720p", "4K": "4k"}[resolution],
                },
            },
        )
        try:
            file = payload["output_video"]["uri"]
            if not isinstance(file, str) or not VIDEO_FILE.fullmatch(file):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise AIError("SCHEMA_INVALID", spent=True) from None
        return MediaResult(operation=file, cost_micros=nonnegative(payload.get("cost_micros")))
    payload = api.request(
        f"/v1beta/models/{quote(model, safe='')}:predictLongRunning",
        {"instances": [{"prompt": prompt}], "parameters": {"sampleCount": 1}},
    )
    operation = payload.get("name")
    if not isinstance(operation, str) or not OPERATION.fullmatch(operation):
        raise AIError("SCHEMA_INVALID", spent=True)
    return MediaResult(operation=operation, cost_micros=nonnegative(payload.get("cost_micros")))


def poll_video(api, operation):
    try:
        if VIDEO_FILE.fullmatch(operation):
            response = api.request("/v1beta/" + operation)
            state = response.get("state")
            if state == "PROCESSING":
                return None
            if state != "ACTIVE":
                raise AIError("UPSTREAM_5XX", spent=True)
            url = api.origin + "/v1beta/" + operation + ":download?alt=media"
        elif OPERATION.fullmatch(operation):
            response = api.request("/v1beta/" + operation)
            if not response.get("done"):
                return None
            if "error" in response:
                raise AIError("UPSTREAM_5XX", spent=True)
            url = response["response"]["generateVideoResponse"]["generatedSamples"][0]["video"][
                "uri"
            ]
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.netloc != urlsplit(api.origin).netloc:
                raise AIError("FORBIDDEN", spent=True)
        else:
            raise AIError("BAD_REQUEST")
        download = api.dialer.request(
            "GET",
            url,
            headers=api.headers(),
            follow_redirects=True,
            max_response_bytes=MAX_BYTES,
            timeout_sec=api.timeout_sec,
        )
        return checked(download.content, {"MP4"})
    except SSRFBlockedError:
        raise AIError("FORBIDDEN", spent=True) from None
    except OutboundTransportError:
        raise AIError("TIMEOUT", spent=True) from None
    except (KeyError, IndexError, TypeError, ValueError):
        raise AIError("SCHEMA_INVALID", spent=True) from None


def synthetic_probe(api, model, modality):
    if modality == "IMAGE":
        return image(api, model, PROMPTS[modality])
    if modality == "TTS":
        return speech(api, model, PROMPTS[modality])
    api.timeout_sec = min(api.timeout_sec, 15)
    operation = start_video(api, model, PROMPTS[modality])
    deadline = monotonic() + 30
    try:
        while monotonic() < deadline:
            if completed := poll_video(api, operation.operation):
                return MediaResult(data=completed.data, cost_micros=operation.cost_micros)
            sleep(2)
    except AIError as error:
        raise AIError(error.code, status=error.status, spent=True) from None
    raise AIError("TIMEOUT", spent=True)
