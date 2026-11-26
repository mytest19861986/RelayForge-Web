import json

import httpx
import pytest

from relayforge.adapters.messengers import (
    BotPollingConflictError,
    MessengerError,
    TelegramBotAdapter,
)
from relayforge.adapters.net_guard import SafeDialer
from relayforge.core.rich_text import (
    CanonicalRichText,
    FormatSpanUtf16,
    InvalidUtf16SpanBoundaryError,
    SafePlaceholderTemplate,
    compile_rich_text,
    utf16_len,
)

TOKEN = "123456:synthetic-token-ABC"


def test_unicode_boundaries_preservation_and_normalization():
    phrase = "سلام\u200cدنیا 👨‍👩‍👧‍👦 تست"
    assert utf16_len(phrase) == 25
    base = "سلام\u200cدنیا "
    family = "👨‍👩‍👧‍👦"
    rich = CanonicalRichText(phrase, (FormatSpanUtf16(utf16_len(base), 11, "BOLD"),))
    assert rich.plain_text == phrase and utf16_len(family) == 11
    telegram = compile_rich_text(rich, "TELEGRAM")
    assert telegram.payload["entities"] == [{"type": "bold", "offset": 10, "length": 11}]
    normalized = CanonicalRichText("e\u0301 👋🏽", (FormatSpanUtf16(3, 4, "BOLD"),))
    assert normalized.plain_text == "é 👋🏽" and normalized.spans_utf16[0].offset == 2
    for offset, length in [(1, 1), (0, 1), (0, 3), (-1, 2)]:
        with pytest.raises(InvalidUtf16SpanBoundaryError):
            CanonicalRichText("🔥", (FormatSpanUtf16(offset, length, "BOLD"),))


def test_safe_links_tags_degradations_and_album_policies():
    literal = '<script>alert(1)</script> * " &'
    link = CanonicalRichText(literal, (FormatSpanUtf16(0, 8, "LINK", "https://example.com/?a=1&b=2"),
                                     FormatSpanUtf16(8, 2, "SPOILER")))
    bale = compile_rich_text(link, "BALE")
    assert "<script>" not in bale.payload["html"]
    assert '&lt;script&gt;' in bale.payload["html"] and "*" not in bale.payload["html"]
    assert "a=1&amp;b=2" in bale.payload["html"] and "&quot;" in bale.payload["html"]
    assert bale.degradations == ["UNSUPPORTED_SPOILER"]
    for policy in ("SEQUENTIAL_POSTS", "CANCEL_FOR_INCOMPATIBLE_DEST"):
        eitaa = compile_rich_text(link, "EITAA", album_count=2, album_policy=policy)
        assert eitaa.album_action == policy
        assert " (https://example.com/?a=1&b=2)" in eitaa.payload["plain_text"]
        assert "NATIVE_ALBUM_UNAVAILABLE" in eitaa.degradations
    with pytest.raises(ValueError, match="link"):
        CanonicalRichText("x", (FormatSpanUtf16(0, 1, "LINK", "javascript:alert(1)"),))
    with pytest.raises(ValueError, match="link"):
        CanonicalRichText("x", (FormatSpanUtf16(0, 1, "LINK", "https://\nexample.com"),))


def test_compiler_limits_and_split_previews():
    spans = tuple(FormatSpanUtf16(i, 1, "BOLD") for i in range(30))
    rich = CanonicalRichText("x" * 35, spans)
    at_limit = compile_rich_text(rich, "RUBIKA_EXPERIMENTAL")
    assert len(at_limit.payload["meta_data_parts"]) == 30
    over = compile_rich_text(CanonicalRichText("x" * 35, spans + (
        FormatSpanUtf16(30, 1, "LINK", "https://example.com"),
    )), "RUBIKA_EXPERIMENTAL")
    assert over.payload["meta_data_parts"] == []
    assert "https://example.com" in over.payload["plain_text"]
    assert "METADATA_LIMIT_EXCEEDED" in over.degradations
    text = "سطر اول\n\n" + "🔥" * 2200
    for caption, limit in [(True, 1024), (False, 4096)]:
        result = compile_rich_text(CanonicalRichText(text), "TELEGRAM", caption=caption)
        assert result.length_overflow and result.payload["plain_text"] == text
        assert result.split_preview[0] == "سطر اول\n\n"
        assert "".join(result.split_preview) == text
        assert all(utf16_len(piece) <= limit for piece in result.split_preview)


@pytest.mark.parametrize("template", [
    "{title}", "{body} {body}", "{{7*7}} {body}", "{__class__} {body}",
    "{body.upper} ", "{body.__init__}", "{body}{",
])
def test_invalid_templates(template):
    with pytest.raises(ValueError, match="template"):
        SafePlaceholderTemplate(template)


def test_template_shifts_utf16_without_evaluating_values():
    body = CanonicalRichText("🔥 متن", (FormatSpanUtf16(0, 2, "BOLD"),))
    title = "🏳️‍🌈 {body}"
    final = SafePlaceholderTemplate("{title}\n\n{body}\n{brand}").render(
        body, title=title, brand="نام", bold_first_line=True,
    )
    assert final.plain_text == f"{title}\n\n🔥 متن\nنام"
    assert final.spans_utf16 == (
        FormatSpanUtf16(utf16_len(title + "\n\n"), 2, "BOLD"),
        FormatSpanUtf16(0, utf16_len(title), "BOLD"),
    )


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *_a, **_kw: [
        (None, None, None, "", ("93.184.216.34", 443)),
    ])
    calls = []
    responses = []

    def send(request):
        calls.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
        return responses.pop(0)

    return TelegramBotAdapter(SafeDialer(transport=httpx.MockTransport(send))), calls, responses


def test_connection_and_publish_are_distinct(transport):
    bot, calls, responses = transport
    responses.extend([
        httpx.Response(200, json={"ok": True, "result": {"id": 123}}),
        httpx.Response(200, json={"ok": True, "result": {"id": -1001234, "type": "channel",
                                                        "title": "آزمایشی"}}),
        httpx.Response(200, json={"ok": True, "result": {"message_id": 77}}),
    ])
    assert bot.test_connection(TOKEN)["publish_verified"] is False
    info = bot.resolve_and_verify_source_channel(TOKEN, "@test")
    assert info["canonical_channel_id"] == "-1001234"
    with pytest.raises(MessengerError, match="PERMISSION_DENIED"):
        bot.test_publish(TOKEN, "-1001234", {"plain_text": "test"},
                         explicit_user_confirmation=False)
    assert calls == [("getMe", {}), ("getChat", {"chat_id": "@test"})]
    assert bot.test_publish(TOKEN, "-1001234", {"plain_text": "test"},
                            explicit_user_confirmation=True).remote_message_id == "77"
    assert calls[-1] == ("sendMessage", {"chat_id": "-1001234", "text": "test"})


def test_polling_conflicts_protection_and_rate_limit(transport):
    bot, _, responses = transport
    responses.extend([
        httpx.Response(409, json={"ok": False}),
        httpx.Response(200, json={"ok": True, "result": [
            {"update_id": 1, "channel_post": {"has_protected_content": True}},
            {"update_id": 2, "channel_post": {"document": {"file_id": "ref",
                                                          "file_size": 150_000_000}}},
        ]}),
        httpx.Response(429, json={"parameters": {"retry_after": 35}}),
        httpx.Response(401, json={"description": TOKEN}),
        httpx.Response(500, json={"description": TOKEN}),
    ])
    with pytest.raises(BotPollingConflictError):
        bot.fetch_live_updates(TOKEN, 0)
    assert [item["access_class"] for item in bot.fetch_live_updates(TOKEN, 0)] == [
        "PROTECTED_NO_FORWARD", "REFERENCE_ONLY",
    ]
    for category, delay in (("RATE_LIMIT_429", 35), ("AUTH_REVOKED", None),
                            ("TRANSIENT_NETWORK", None)):
        with pytest.raises(MessengerError) as failure:
            bot.send_message(TOKEN, "-1001234", {"plain_text": "test"})
        assert failure.value.category == category
        assert failure.value.retry_after_sec == delay
        assert TOKEN not in str(failure.value) and failure.value.__context__ is None
