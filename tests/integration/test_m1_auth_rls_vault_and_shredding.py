import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BoundedSemaphore
from uuid import uuid4

import psycopg
import pytest
from django.conf import settings
from django.db import connections, transaction
from django.http import JsonResponse
from django.test import Client, RequestFactory
from psycopg import sql

from relayforge.adapters.db_migrator import (
    M1_VERSION,
    MigrationChecksumMismatchError,
    apply_baseline_schema,
)
from relayforge.core.security import SecretRevokedError, TenantKeyShreddedError

API = "/api/v1"
PASSWORD = "Synthetic test password 42!"
OPERATOR = {"HTTP_AUTHORIZATION": "Bearer synthetic-operator-token"}
COOKIE = "__Host-rf_session"



def request(client, method, path, data=None, **headers):
    return client.generic(
        method, API + path, json.dumps(data) if data is not None else "",
        content_type="application/json", secure=True, **headers,
    )


def expect(response, status):
    assert response.status_code == status, response.content
    return json.loads(response.content)


def provision(slug):
    response = request(Client(), "POST", "/operator/tenants", {
        "slug": slug, "display_name": slug, "owner_email": f"{slug}@example.com",
        "owner_password": PASSWORD, "owner_display_name": slug,
    }, **OPERATOR)
    return expect(response, 201)["id"]


def login(slug, **headers):
    client = Client()
    response = request(client, "POST", "/auth/login", {
        "email": f"{slug.upper()}@example.COM", "password": PASSWORD,
    }, **headers)
    session = expect(response, 200)
    cookie = response.cookies[COOKIE]
    assert cookie["secure"] and cookie["httponly"]
    assert cookie["samesite"] == "Lax" and cookie["path"] == "/" and not cookie["domain"]
    return session | {"cookie": cookie.value}


def authorized(session, method, path, data=None, **headers):
    client = Client()
    client.cookies[COOKIE] = session["cookie"]
    headers.setdefault("HTTP_X_CSRFTOKEN", session["csrf_token"])
    return request(client, method, path, data, **headers)


def secret_path(workspace, secret=None):
    path = f"/workspaces/{workspace}/security/secrets"
    return path if secret is None else f"{path}/{secret}"


def secret_input(raw):
    return {"secret_kind": "TELEGRAM_BOT_TOKEN", "label": "My bot", "raw_secret": raw}


def consent_path(workspace):
    return f"/workspaces/{workspace}/security/consents"


CONSENT = {"provider_scope": "GEMINI_TEXT", "is_granted": True}


@pytest.fixture(scope="module")
def tenants(db):
    provision("pilot-alpha")
    provision("pilot-beta")
    return login("pilot-alpha"), login("pilot-beta")


def test_catalog_and_direct_pre_auth_scope(db):
    with psycopg.connect(db) as conn:
        assert conn.execute("""
            SELECT count(*) FROM pg_class JOIN pg_namespace ON pg_namespace.oid = relnamespace
            WHERE nspname = 'app' AND relkind = 'r' AND relrowsecurity AND relforcerowsecurity
        """).fetchone()[0] == 51
        assert conn.execute(
            "SELECT count(*) FROM pg_roles WHERE NOT rolsuper AND rolbypassrls"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM app.schema_migrations WHERE version = %s", (M1_VERSION,)
        ).fetchone()[0] == 1
        conn.execute("SET LOCAL ROLE relayforge_web")
        for table in ("users", "tenants", "tenant_memberships", "web_sessions"):
            query = sql.SQL("SELECT count(*) FROM app.{}").format(sql.Identifier(table))
            assert conn.execute(query).fetchone()[0] == 0
    expect(request(Client(), "GET", "/health"), 200)


def test_auth_csrf_origin_viewer_logout_and_ambiguity(db, tenants):
    session, other = tenants
    workspace = session["default_workspace_id"]
    missing = request(Client(), "GET", "/workspaces", HTTP_X_CORRELATION_ID="invalid")
    assert expect(missing, 401)["error"]["correlation_id"] == missing["X-Correlation-ID"]
    assert missing["Cache-Control"] == "no-store"
    assert missing["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in missing["Content-Security-Policy"]

    payload = {"slug": "new", "display_name": "New"}
    expect(authorized(session, "POST", "/workspaces", payload, HTTP_X_CSRFTOKEN=""), 403)
    expect(authorized(session, "POST", "/workspaces", payload, HTTP_X_CSRFTOKEN="wrong"), 403)
    expect(authorized(session, "POST", "/workspaces", payload,
                      HTTP_ORIGIN="https://evil.example"), 403)

    provision("observer")
    observer = login("observer")
    with psycopg.connect(db) as conn:
        user = conn.execute("SELECT id FROM app.users WHERE email = 'observer@example.com'")
        user_id = user.fetchone()[0]
        conn.execute("UPDATE app.tenant_memberships SET role = 'VIEWER' WHERE tenant_id = %s",
                     (observer["tenant_id"],))
        conn.execute("""
            INSERT INTO app.tenant_memberships (id, tenant_id, user_id, role)
            VALUES (%s, %s, %s, 'VIEWER')
        """, (uuid4(), session["tenant_id"], user_id))
    expect(authorized(observer, "GET", "/workspaces"), 200)
    for method, path, data in (
        ("POST", "/workspaces", payload),
        ("POST", secret_path(observer["default_workspace_id"]), secret_input("sensitive")),
        ("PUT", consent_path(observer["default_workspace_id"]), CONSENT),
        ("POST", "/tenants/current/delete-account", {
            "confirmation_slug": "observer", "acknowledge_irreversible": True,
        }),
    ):
        expect(authorized(observer, method, path, data), 403)
    credentials = {"email": "observer@example.com", "password": PASSWORD}
    ambiguous = request(Client(), "POST", "/auth/login", credentials)
    assert expect(ambiguous, 422)["error"]["code"] == "RF022"
    explicit = expect(request(Client(), "POST", "/auth/login", credentials | {
        "tenant_slug": "pilot-alpha",
    }), 200)
    assert explicit["default_workspace_id"] == workspace and explicit["role"] == "VIEWER"
    expect(authorized(observer, "POST", "/auth/logout"), 200)
    expect(authorized(observer, "GET", "/tenants/current"), 401)
    expect(authorized(other, "GET", "/tenants/current"), 200)


def test_concurrent_tenant_reads_writes_and_savepoint(db, tenants):
    a, b = tenants
    workspace_a, workspace_b = a["default_workspace_id"], b["default_workspace_id"]
    existing_b = expect(authorized(b, "POST", secret_path(workspace_b),
                                   secret_input("synthetic-beta-key")), 201)
    expect(authorized(b, "PUT", consent_path(workspace_b), CONSENT), 200)

    def exercise(index):
        sessions = ((a, workspace_a), (b, workspace_b))
        session, workspace = sessions[index % 2]
        operation = index % 5
        if operation == 0:
            expect(authorized(session, "POST", "/workspaces", {
                "slug": f"batch-{index}", "display_name": f"Batch {index}",
            }), 201)
        elif operation == 1:
            expect(authorized(session, "POST", secret_path(workspace),
                              secret_input(f"synthetic-batch-{index}")), 201)
        elif operation == 2:
            expect(authorized(session, "PUT", consent_path(workspace), CONSENT), 200)
        elif operation == 3:
            expect(authorized(a, "DELETE", secret_path(workspace_a, existing_b["id"])), 404)
        else:
            expect(authorized(a, "PUT", consent_path(workspace_b), CONSENT | {
                "is_granted": False,
            }), 404)

    simultaneous_start = Barrier(100, timeout=15)
    # Keep connection admission under the default PostgreSQL service limit.
    connection_slots = BoundedSemaphore(64)

    def concurrent_request(index):
        simultaneous_start.wait()
        with connection_slots:
            try:
                exercise(index)
            finally:
                connections.close_all()

    with ThreadPoolExecutor(max_workers=100) as pool:
        assert len(list(pool.map(concurrent_request, range(100)))) == 100

    expect(authorized(a, "GET", secret_path(workspace_b)), 404)
    items = expect(authorized(b, "GET", secret_path(workspace_b)), 200)["items"]
    assert existing_b in items
    consents = expect(authorized(b, "GET", consent_path(workspace_b)), 200)["items"]
    assert len(consents) == 1 and consents[0]["is_granted"] is True
    for session in (a, b):
        other_id = workspace_b if session is a else workspace_a
        workspaces = expect(authorized(session, "GET", "/workspaces"), 200)["items"]
        assert len(workspaces) == 11 and all(item["id"] != other_id for item in workspaces)

    from relayforge.web.db import first
    from relayforge.web.middleware import TenantRLSMiddleware

    def nested_view(request):
        with transaction.atomic():
            transaction.set_rollback(True)
        current = first("SELECT current_setting('app.tenant_id') AS tenant")
        assert current["tenant"] == a["tenant_id"]
        return JsonResponse({"tenant": current["tenant"]})

    probe = RequestFactory().get(API + "/workspaces", secure=True)
    probe.COOKIES[COOKIE] = a["cookie"]
    assert expect(TenantRLSMiddleware(nested_view)(probe), 200)["tenant"] == a["tenant_id"]
    assert first("SELECT NULLIF(current_setting('app.tenant_id', true), '') AS tenant") == {
        "tenant": None,
    }


def test_plan_ceiling_is_operator_only(db, tenants):
    a, _ = tenants
    path = f"/operator/tenants/{a['tenant_id']}/plan-quotas"
    payload = {"daily_max_cost_micros": 3_100_000, "monthly_max_cost_micros": 31_000_000}
    expect(authorized(a, "PUT", path, payload), 401)
    expect(request(Client(), "PUT", path, payload, **OPERATOR), 200)
    with psycopg.connect(db) as conn, pytest.raises(psycopg.Error) as rejection:
        conn.execute("SET LOCAL ROLE relayforge_web")
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (a["tenant_id"],))
        conn.execute("UPDATE app.budget_policies SET max_cost_micros = 1 "
                     "WHERE policy_origin = 'OPERATOR_PLAN'")
    assert rejection.value.sqlstate == "RF007"
    state = expect(authorized(a, "GET", "/tenants/current"), 200)
    assert {row["period_kind"]: row["max_cost_micros"] for row in state["budget_policies"]} == {
        "DAILY": 3_100_000, "MONTHLY": 31_000_000,
    }


def vault_inputs(conn, identifier):
    record = conn.execute("""
        SELECT s.tenant_id, s.workspace_id, s.id, s.secret_kind, s.nonce_bytes, s.ciphertext_aead,
               k.key_version, k.wrapped_dek_ciphertext, k.key_state, s.is_revoked
        FROM app.secrets_vault s JOIN app.tenant_encryption_keys k
          ON k.tenant_id = s.tenant_id AND k.key_version = s.dek_version
        WHERE s.id = %s
    """, (identifier,)).fetchone()
    return dict(zip((
        "tenant_id", "workspace_id", "secret_id", "kind", "nonce", "ciphertext",
        "key_version", "wrapped_dek", "key_state", "is_revoked",
    ), record, strict=True))


def test_envelope_revocation_shredding_and_append_only_audit(db):
    provision("erase-alpha")
    provision("keep-beta")
    a, b = login("erase-alpha"), login("keep-beta")
    secrets = []
    for session in (a, b):
        raw = f"plaintext-for-{session['tenant_id']}"
        response = authorized(session, "POST", secret_path(session["default_workspace_id"]),
                              secret_input(raw))
        dto = expect(response, 201)
        assert set(dto) == {"id", "secret_kind", "label", "masked_hint"}
        assert raw.encode() not in response.content
        secrets.append((dto["id"], raw))
    with psycopg.connect(db) as conn:
        for identifier, raw in secrets:
            record = vault_inputs(conn, identifier)
            assert raw.encode() not in record["ciphertext"]
            assert settings.VAULT.decrypt_secret(**record) == raw
    identifier, _ = secrets[0]
    expect(authorized(a, "DELETE", secret_path(a["default_workspace_id"], identifier)), 200)
    with psycopg.connect(db) as conn, pytest.raises(SecretRevokedError):
        settings.VAULT.decrypt_secret(**vault_inputs(conn, identifier))

    delete_path = "/tenants/current/delete-account"
    payload = {"confirmation_slug": "erase-alpha", "acknowledge_irreversible": True}
    expect(authorized(a, "POST", delete_path, payload | {"confirmation_slug": "keep-beta"}), 422)
    for invalid in (False, 1, "true"):
        expect(authorized(a, "POST", delete_path, payload | {
            "acknowledge_irreversible": invalid,
        }), 422)
    assert expect(authorized(a, "POST", delete_path, payload), 202)[
        "pitr_retention_disclosure_days"
    ] == 14
    expect(authorized(a, "GET", "/workspaces"), 401)
    expect(authorized(b, "GET", "/workspaces"), 200)

    with psycopg.connect(db) as conn:
        destroyed = vault_inputs(conn, identifier)
        assert destroyed["key_state"] == "SHREDDED" and destroyed["wrapped_dek"] is None
        assert destroyed["is_revoked"]
        with pytest.raises(TenantKeyShreddedError):
            settings.VAULT.decrypt_secret(**destroyed)
        keep_id, keep_raw = secrets[1]
        assert settings.VAULT.decrypt_secret(**vault_inputs(conn, keep_id)) == keep_raw
        assert conn.execute("""
            SELECT status, emergency_stop, revoked_at IS NOT NULL
            FROM app.tenants t JOIN app.workspaces w ON w.tenant_id = t.id
            JOIN app.web_sessions s ON s.tenant_id = t.id WHERE t.id = %s
        """, (a["tenant_id"],)).fetchone() == ("PENDING_DELETION", True, True)
        event = conn.execute("""
            SELECT id, details_safe_json FROM app.audit_events
            WHERE tenant_id = %s AND action_code = 'TENANT_CRYPTO_SHREDDED'
        """, (a["tenant_id"],)).fetchone()
        assert event[1] == {"pitr_retention_disclosure_days": 14}
        for statement in (
            "UPDATE app.audit_events SET target_id = 'different' WHERE id = %s",
            "DELETE FROM app.audit_events WHERE id = %s",
        ):
            with pytest.raises(psycopg.Error) as immutable, conn.transaction():
                conn.execute(statement, (event[0],))
            assert immutable.value.sqlstate == "RF021"



def test_rate_limits_and_migration_checksum_drift(db, tenants, monkeypatch):
    from relayforge.web import middleware

    a, _ = tenants
    monkeypatch.setattr(middleware, "SESSION_LIMIT", 1)
    session = login("pilot-beta")
    expect(authorized(session, "GET", "/workspaces"), 200)
    for _ in range(2):
        response = authorized(session, "GET", "/workspaces")
        assert expect(response, 429)["error"]["retry_after_sec"] == 60
        assert response["Retry-After"] == "60"
    monkeypatch.setattr(middleware, "LOGIN_LIMIT", 1)
    credentials = {"email": "unknown@example.com", "password": PASSWORD}
    address = {"REMOTE_ADDR": "192.0.2.8"}
    expect(request(Client(), "POST", "/auth/login", credentials, **address), 401)
    expect(request(Client(), "POST", "/auth/login", credentials, **address), 429)
    assert a["tenant_id"] != session["tenant_id"]

    with psycopg.connect(db) as conn:
        original = conn.execute(
            "SELECT checksum_sha256 FROM app.schema_migrations WHERE version = %s",
            (M1_VERSION,),
        ).fetchone()[0]
        conn.execute("UPDATE app.schema_migrations SET checksum_sha256 = %s WHERE version = %s",
                     ("drift", M1_VERSION))
    try:
        with pytest.raises(MigrationChecksumMismatchError, match=M1_VERSION):
            apply_baseline_schema(db)
    finally:
        with psycopg.connect(db) as conn:
            conn.execute("UPDATE app.schema_migrations SET checksum_sha256 = %s WHERE version = %s",
                         (original, M1_VERSION))
