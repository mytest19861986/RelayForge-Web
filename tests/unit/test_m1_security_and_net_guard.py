import logging
from uuid import uuid4

import httpx
import pytest
from cryptography.exceptions import InvalidTag

from relayforge.adapters.net_guard import (
    OutboundTransportError,
    SafeDialer,
    SecretLogFilter,
    SSRFBlockedError,
    validate_public_ip,
)
from relayforge.adapters.vault import EnvelopeSecretVault
from relayforge.core.security import SecretKind, mask_secret_preview, scrub_sensitive_text


def test_envelope_authenticates_keys_and_secret_context():
    tenant, workspace, identifier = uuid4(), uuid4(), uuid4()
    kind = SecretKind.OPENAI_API_KEY
    vault = EnvelopeSecretVault(b"m" * 32)
    wrapped = vault.generate_wrapped_dek(tenant, 1)
    nonce, ciphertext = vault.encrypt_secret(
        tenant, workspace, identifier, kind, "secret-value",
        key_version=1, wrapped_dek=wrapped,
    )
    kwargs = {
        "tenant_id": tenant, "workspace_id": workspace, "secret_id": identifier, "kind": kind,
        "nonce": nonce, "ciphertext": ciphertext, "key_version": 1,
        "wrapped_dek": wrapped, "key_state": "ACTIVE", "is_revoked": False,
    }
    assert vault.decrypt_secret(**kwargs) == "secret-value"
    assert mask_secret_preview("short") == "***REDACTED***"
    assert "secret-value" not in mask_secret_preview("secret-value")
    with pytest.raises(InvalidTag):
        EnvelopeSecretVault(b"x" * 32).decrypt_secret(**kwargs)
    for mutation in (
        {"tenant_id": uuid4()},
        {"workspace_id": uuid4()},
        {"secret_id": uuid4()},
        {"kind": SecretKind.GEMINI_API_KEY},
        {"key_version": 2},
        {"ciphertext": bytes([ciphertext[0] ^ 1]) + ciphertext[1:]},
    ):
        with pytest.raises(InvalidTag):
            vault.decrypt_secret(**(kwargs | mutation))


@pytest.mark.parametrize(("pattern", "token"), [
    ("https://api.telegram.org/bot{}/sendMessage", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"),
    ("https://eitaayar.ir/api/{}/sendMessage", "eitaa-api-token"),
    ("https://example.com/?api_key={}", "synthetic-query-token"),
    ("OpenAI {}", "sk-proj-AbcdEfG"),
    ("Gemini {}", "AIzaSyAbcdEfG"),
    ("Authorization: Bearer {}", "synthetic-bearer-token"),
    ("Cookie: __Host-rf_session={}", "synthetic-session-token"),
    ("X-CSRFToken: {}", "synthetic-csrf-token"),
    ("runtime configuration {}", "caller-supplied-secret"),
])
def test_redaction_across_text_transport_and_logs(monkeypatch, pattern, token):
    text = pattern.format(token)
    supplied = (token,) if pattern.startswith("runtime") else ()
    assert token not in scrub_sensitive_text(text, supplied)

    def fail(request):
        raise httpx.ConnectError(text, request=request)

    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **kw: [(None,)*4 + (("8.8.8.8", 443),)])
    dialer = SafeDialer(transport=httpx.MockTransport(fail), extra_secrets=supplied)
    with pytest.raises(OutboundTransportError) as error:
        dialer.request("GET", "https://example.com")
    assert token not in str(error.value)
    assert error.value.__context__ is None

    try:
        raise ValueError(text)
    except ValueError as exc:
        record = logging.LogRecord("test", logging.ERROR, __file__, 1, "%s", (text,),
                                   (type(exc), exc, exc.__traceback__))
    SecretLogFilter(extra_secrets=supplied).filter(record)
    assert token not in logging.Formatter().format(record)


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.5", "172.16.0.1", "192.168.1.1", "169.254.169.254",
    "100.64.0.1", "::1", "fe80::1", "::ffff:127.0.0.1", "::ffff:169.254.169.254",
    "198.18.0.1", "240.0.0.1", "224.0.0.1", "::", "fc00::1", "ff02::1",
])
def test_public_ip_rejects_special_addresses(ip):
    with pytest.raises(SSRFBlockedError):
        validate_public_ip(ip)


def test_pin_address_and_revalidate_dns_and_redirect(monkeypatch):
    visited = []
    answers = {
        "example.com": ["93.184.216.34"],
        "internal.example": ["169.254.169.254"],
        "mixed.example": ["93.184.216.34", "127.0.0.1"],
    }

    def resolve(host, port, **kwargs):
        return [(None, None, None, "", (ip, port)) for ip in answers[host]]

    def send(request):
        visited.append(request)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "https://internal.example/metadata"})
        return httpx.Response(200)

    monkeypatch.setattr("socket.getaddrinfo", resolve)
    dialer = SafeDialer(transport=httpx.MockTransport(send))
    assert dialer.request("GET", "https://example.com/allowed").status_code == 200
    assert visited[0].url.host == "93.184.216.34"
    assert visited[0].headers["Host"] == "example.com"
    assert visited[0].extensions["sni_hostname"] == "example.com"

    for url in ("http://example.com", "https://mixed.example", "https://example.com/redirect"):
        with pytest.raises(SSRFBlockedError):
            dialer.request("GET", url)
    assert [request.url.path for request in visited] == ["/allowed", "/redirect"]


def test_scrubs_http_status_error(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **kw: [(None,)*4 + (("8.8.8.8", 443),)])
    dialer = SafeDialer(transport=httpx.MockTransport(lambda request: httpx.Response(403)))
    with pytest.raises(OutboundTransportError) as error:
        dialer.request("GET", "https://example.com/?token=synthetic-token")
    assert "synthetic-token" not in str(error.value)
    assert "403" in str(error.value)
