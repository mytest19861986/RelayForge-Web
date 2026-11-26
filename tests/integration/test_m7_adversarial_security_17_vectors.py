"""Defensive acceptance checks against the application's public and tenant boundaries."""

from pathlib import Path
from uuid import uuid4

import pytest
from django.test import Client
from test_m1_auth_rls_vault_and_shredding import COOKIE, expect, login, provision, request
from test_m4_ai_gateway_channel_and_learning import key, send

from relayforge.adapters.net_guard import SSRFBlockedError, validate_public_ip
from relayforge.adapters.tenant_backup import valid_path
from relayforge.core.fact_guard import candidate
from relayforge.core.rich_text import CanonicalRichText, SafePlaceholderTemplate, compile_rich_text


def test_safe_html_csp_and_tenant_mutations(db):
    provision("m7-boundary-a")
    provision("m7-boundary-b")
    a, b = login("m7-boundary-a"), login("m7-boundary-b")
    ws = b["default_workspace_id"]
    text = '<script>alert(1)</script><img src=x onerror="alert(1)">'
    compiled = compile_rich_text(CanonicalRichText(text), "BALE")
    assert "<script>" not in compiled.payload["html"]
    assert "<img " not in compiled.payload["html"]
    response = Client().get("/")
    assert "object-src 'none'" in response["Content-Security-Policy"]
    client = Client()
    client.cookies[COOKIE] = b["cookie"]
    for method, path in (
        ("POST", "/drafts"),
        ("PUT", "/security/consents"),
        ("PATCH", f"/drafts/{uuid4()}"),
        ("DELETE", f"/media/assets/{uuid4()}"),
    ):
        full = f"/workspaces/{ws}{path}"
        for headers in (
            {},
            {"HTTP_X_CSRFTOKEN": "forged"},
            {
                "HTTP_X_CSRFTOKEN": b["csrf_token"],
                "HTTP_ORIGIN": "https://untrusted.example",
            },
        ):
            expect(request(client, method, full, {}, **headers), 403)
    draft = expect(send(b, "POST", "/drafts", {"body_text": "متن متعلق به مستأجر دوم"}), 201)["id"]
    owner = Client()
    owner.cookies[COOKIE] = a["cookie"]
    for method, path, data in (
        ("GET", "/dashboard", None),
        ("GET", f"/drafts/{draft}", None),
        ("PATCH", f"/drafts/{draft}", {"body_text": "تغییر"}),
        ("DELETE", f"/media/assets/{uuid4()}", None),
        ("POST", "/backups", {"backup_password": "test-backup-only"}),
    ):
        expect(
            request(
                owner, method, f"/workspaces/{ws}{path}", data, HTTP_X_CSRFTOKEN=a["csrf_token"]
            ),
            404,
        )
    expect(send(b, "GET", f"/drafts/{draft}"), 200)


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "10.0.0.1", "::1"])
def test_non_public_destinations_rejected(address):
    with pytest.raises(SSRFBlockedError):
        validate_public_ip(address)


@pytest.mark.parametrize("mode", ["STRICT", "BALANCED"])
def test_fact_tamper_is_blocked(mode):
    source = "قیمت ۲۵۰ هزار تومان است"
    altered = "قیمت ۲۵ میلیون تومان است"
    assert candidate(source, altered, mode)["fact_guard_verdict"] == "FACT_MISMATCH_BLOCKED"


@pytest.mark.parametrize("value", ["{body.__class__}", "{0}", "متن", "{body}{body}"])
def test_template_fields_are_restricted(value):
    with pytest.raises(ValueError):
        SafePlaceholderTemplate(value)


def test_bad_backup_paths_denied():
    for path in ("../escape", "/absolute", "config/../escape.json", "config\\escape.json"):
        assert not valid_path(path)


def test_full_logical_workspace_backup_roundtrip(db):
    provision("m7-backup-from")
    provision("m7-backup-to")
    a, b = login("m7-backup-from"), login("m7-backup-to")
    expect(send(a, "POST", "/drafts", {"body_text": "یادداشت بازیابی"}), 201)
    data = {"backup_password": "test-full-backup-only", "backup_scope": "TENANT_FULL_LOGICAL"}
    backup = expect(send(a, "POST", "/backups", data), 200)
    expect(
        send(
            b,
            "POST",
            "/backups/restore",
            data
            | {
                "manifest_id": backup["manifest_id"],
                "mode": "PREVIEW",
            },
        ),
        422,
    )
    args = data | {"bundle_base64": backup["bundle_base64"]}
    preview = expect(send(b, "POST", "/backups/restore", args | {"mode": "PREVIEW"}), 200)
    assert preview["table_counts"]["drafts"] == 1
    assert expect(send(b, "GET", "/drafts"), 200)["items"] == []
    for _ in range(2):
        expect(send(b, "POST", "/backups/restore", args | {"mode": "APPLY"}, **key()), 200)
    assert len(expect(send(b, "GET", "/drafts"), 200)["items"]) == 1
    assert expect(send(b, "GET", "/dashboard"), 200)["emergency_stop"]


def test_regex_runtime_upper_bound():
    from time import perf_counter

    from relayforge.core.rules import ThompsonNFA

    matcher = ThompsonNFA("(a+)+$")
    start = perf_counter()
    assert not matcher.search("a" * 4000 + "!")
    assert perf_counter() - start < 0.05


def test_export_guard_boundaries(tmp_path):
    import json

    from relayforge.workers.export_ingestor import HostileExportStreamError, HostileJsonStreamGuard

    samples = (
        b"[" * 33 + b"0" + b"]" * 33,
        b'"' + b"x" * (256 * 1024 + 1) + b'"',
        b"\x1f\x8b" + bytes(16),
        b"PK\x03\x04" + bytes(16),
    )
    codes = []
    for document in samples:
        stream = HostileJsonStreamGuard([document])
        with pytest.raises(HostileExportStreamError) as failure:
            while stream.read(65536):
                pass
        codes.append(str(failure.value))
    assert len(set(codes)) == 3
    path = tmp_path / "oc04.json"
    path.write_text(json.dumps({"rejected": len(samples), "codes": codes}))
    target = Path("tmp/m7-oc04-evidence.json")
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(path.read_bytes())


def test_local_media_staging_confinement(tmp_path):
    from relayforge.workers.media_worker import confined, input_bytes

    target = tmp_path / "real.png"
    target.write_bytes(b"#EXTM3U\n")
    (tmp_path / "link.png").symlink_to(target)
    for name in ("../real.png", str(target), "link.png"):
        with pytest.raises(ValueError, match="INVALID_STAGING_PATH"):
            confined(tmp_path, name)
    with pytest.raises(ValueError, match="FORBIDDEN_MEDIA_PROTOCOL"):
        input_bytes(tmp_path, "real.png", {"PNG"})
