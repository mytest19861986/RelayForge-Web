import hmac
import os
import secrets
from uuid import UUID, uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from django.conf import settings
from django.db import DatabaseError, transaction
from django.http import JsonResponse
from ninja import NinjaAPI
from ninja.errors import HttpError, ValidationError
from ninja.security import HttpBearer, SessionAuth

from relayforge.adapters.db_migrator import MIGRATIONS
from relayforge.core.security import mask_secret_preview, scrub_sensitive_text
from relayforge.web import (
    ai_m4,
    delivery_m3,
    destinations_m2,
    drafts_m3,
    learning_m4,
    media_m5,
    ops_m6,
    routes_m2,
    routes_m3,
    schemas,
    studio_m4,
    styles_m4,
)
from relayforge.web.db import audit, first, lock_tenant, rows, set_context
from relayforge.web.endpoints import ack, page, require_permission, require_workspace
from relayforge.web.middleware import COOKIE, cookie_name, sha256
from relayforge.web.problems import PROBLEMS, ProblemError, problem

HASHER = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2)
DUMMY_HASH = HASHER.hash(secrets.token_urlsafe(32))
SESSION_SECONDS = 12 * 3600
OPERATOR = "operator"


class OperatorBearerAuth(HttpBearer):
    def authenticate(self, request, token):
        expected = os.environ.get("RF_OPERATOR_BEARER_TOKEN", "")
        if expected and hmac.compare_digest(token.encode(), expected.encode()):
            return True
        return None


class TenantSessionAuth(SessionAuth):
    param_name = COOKIE

    def __call__(self, request):
        return getattr(request, "tenant", None)


api = NinjaAPI(
    title="RelayForge Web", version="2.0.0", auth=TenantSessionAuth(),
    docs_url=None, openapi_url=None,
)
operator_auth = OperatorBearerAuth()

api.add_router("", routes_m2.router)
api.add_router("", destinations_m2.router)
api.add_router("", routes_m3.router)
api.add_router("", drafts_m3.router)
api.add_router("", delivery_m3.router)
api.add_router("", ai_m4.router)
api.add_router("", studio_m4.router)
api.add_router("", styles_m4.router)
api.add_router("", learning_m4.router)
api.add_router("", media_m5.router)
api.add_router("", ops_m6.router)


@api.exception_handler(ProblemError)
def handle_problem(request, exc):
    return problem(request, exc.status, code=exc.code)


@api.exception_handler(ValidationError)
def handle_validation(request, exc):
    return problem(request, 422)


@api.exception_handler(HttpError)
def handle_http(request, exc):
    return problem(request, exc.status_code if exc.status_code in PROBLEMS else 500)


@api.exception_handler(DatabaseError)
def handle_db(request, exc):
    state = getattr(exc.__cause__, "sqlstate", "")
    if state == "RF022":
        status = 422
    elif state in {"RF001", "RF004", "RF005", "RF006"}:
        status = 409
    elif state in {"RF007", "RF020", "RF021", "42501"}:
        status = 403
    elif state and state.startswith("RF"):
        status = 422
    elif state in {"23503", "23505"}:
        status = 409
    elif state and state.startswith(("22", "23")):
        status = 422
    else:
        status = 503
    return problem(request, status, code=state if state and state.startswith("RF") else None)


@api.exception_handler(Exception)
def handle_unexpected(request, exc):
    return problem(request, 500)


def create_workspace(tenant_id, slug, display_name, *, using="default"):
    workspace_id = uuid4()
    rows(
        "INSERT INTO app.workspaces (id, tenant_id, slug, display_name) VALUES (%s, %s, %s, %s)",
        (workspace_id, tenant_id, slug, display_name), using=using,
    )
    rows(
        "INSERT INTO app.workspace_settings (workspace_id, tenant_id) VALUES (%s, %s)",
        (workspace_id, tenant_id), using=using,
    )
    return workspace_id


def update_plan_quotas(tenant_id, daily, monthly):
    for period, amount in (("DAILY", daily), ("MONTHLY", monthly)):
        rows("""
            INSERT INTO app.budget_policies (id, tenant_id, policy_origin, scope_type, scope_ref_id,
                                             period_kind, max_cost_micros)
            VALUES (%s, %s, 'OPERATOR_PLAN', 'TENANT', %s, %s, %s)
            ON CONFLICT (tenant_id, policy_origin, scope_type, scope_ref_id, period_kind)
            DO UPDATE SET max_cost_micros = EXCLUDED.max_cost_micros, is_active = true
        """, (uuid4(), tenant_id, tenant_id, period, amount), using=OPERATOR)


@api.get("/health", auth=None)
def health(request):
    versions = [version for version, _ in MIGRATIONS]
    count = first(
        "SELECT 1 AS connected, count(*) AS applied FROM app.schema_migrations "
        "WHERE version = ANY(%s)", (versions,),
    )
    if count != {"connected": 1, "applied": len(versions)}:
        raise ProblemError(503)
    return ack(request)


@api.get("/operator/tenants", auth=operator_auth)
def operator_tenants(request, limit: int = 50, cursor: UUID | None = None, q: str = ""):
    if not 1 <= limit <= 100 or len(q) > 120:
        raise ProblemError(422)
    return page(rows("""
        SELECT t.id,t.slug,t.display_name,t.status,t.created_at,
          max(p.max_cost_micros) FILTER (WHERE p.period_kind='DAILY') AS daily_max_cost_micros,
          max(p.max_cost_micros) FILTER (WHERE p.period_kind='MONTHLY') AS monthly_max_cost_micros
        FROM app.tenants t LEFT JOIN app.budget_policies p
          ON p.tenant_id=t.id AND p.policy_origin='OPERATOR_PLAN' AND p.is_active
        WHERE (%s::uuid IS NULL OR t.id>%s)
          AND (strpos(lower(t.slug),lower(%s))>0 OR strpos(lower(t.display_name),lower(%s))>0)
        GROUP BY t.id ORDER BY t.id LIMIT %s
    """, (cursor,cursor,q,q,limit+1), using=OPERATOR), limit)


@api.post("/operator/tenants", auth=operator_auth)
def provision(request, data: schemas.Provision):
    tenant_id, user_id = uuid4(), uuid4()
    password_hash = HASHER.hash(data.owner_password.get_secret_value())
    wrapped = settings.VAULT.generate_wrapped_dek(tenant_id, 1)
    with transaction.atomic(using=OPERATOR):
        set_context(tenant_id, user_id, using=OPERATOR)
        rows("INSERT INTO app.tenants (id, slug, display_name) VALUES (%s, %s, %s)",
             (tenant_id, data.slug, data.display_name), using=OPERATOR)
        rows("""
            INSERT INTO app.users (id, email, display_name, password_hash_argon2id)
            VALUES (%s, %s, %s, %s)
        """, (user_id, data.owner_email.lower(), data.owner_display_name, password_hash),
             using=OPERATOR)
        rows("""
            INSERT INTO app.tenant_memberships (id, tenant_id, user_id, role)
            VALUES (%s, %s, %s, 'OWNER')
        """, (uuid4(), tenant_id, user_id), using=OPERATOR)
        rows("""
            INSERT INTO app.tenant_encryption_keys
                (id, tenant_id, key_version, wrapped_dek_ciphertext)
            VALUES (%s, %s, 1, %s)
        """, (uuid4(), tenant_id, wrapped), using=OPERATOR)
        workspace = create_workspace(tenant_id, "default", "پیش‌فرض", using=OPERATOR)
        update_plan_quotas(tenant_id, 2_000_000, 30_000_000)
        request.tenant = {"tenant_id": tenant_id, "role": "OPERATOR"}
        audit(request, "TENANT_PROVISIONED", "TENANT", tenant_id, using=OPERATOR)
    return JsonResponse(ack(request, identifier=tenant_id, default_workspace_id=workspace),
                        status=201)


@api.put("/operator/tenants/{tenantId}/plan-quotas", auth=operator_auth)
def plan_quotas(request, tenantId: UUID, data: schemas.PlanQuotas):
    with transaction.atomic(using=OPERATOR):
        lock_tenant(tenantId, using=OPERATOR)
        set_context(tenantId, using=OPERATOR)
        if not first("SELECT id FROM app.tenants WHERE id = %s AND status = 'ACTIVE'",
                     (tenantId,), using=OPERATOR):
            raise ProblemError(404)
        update_plan_quotas(tenantId, data.daily_max_cost_micros, data.monthly_max_cost_micros)
        request.tenant = {"tenant_id": tenantId, "role": "OPERATOR"}
        audit(request, "OPERATOR_PLAN_QUOTAS_UPDATED", "TENANT", tenantId, using=OPERATOR)
    return ack(request, identifier=tenantId)


def password_matches(encoded, password):
    try:
        return HASHER.verify(encoded, password)
    except InvalidHashError:
        if encoded != DUMMY_HASH:
            password_matches(DUMMY_HASH, password)
        return False
    except VerificationError:
        return False


@api.post("/auth/login", auth=None)
def login(request, data: schemas.Login):
    name = cookie_name(request)
    if name == COOKIE and not request.is_secure():
        raise ProblemError(403)
    query = "SELECT * FROM app.fn_auth_lookup_login_candidate(%s, %s)"
    params = (data.email, data.tenant_slug)
    password = data.password.get_secret_value()
    with transaction.atomic():
        try:
            with transaction.atomic():
                candidate = first(query, params)
        except DatabaseError as exc:
            if getattr(exc.__cause__, "sqlstate", None) == "RF022":
                password_matches(DUMMY_HASH, password)
            raise
        valid = password_matches(candidate["password_hash_argon2id"] if candidate else
                                 DUMMY_HASH, password)
        if not valid or not candidate or not candidate["default_workspace_id"]:
            raise ProblemError(401)
        tenant_id = candidate["tenant_id"]
        lock_tenant(tenant_id)
        if first(query, params) != candidate:
            raise ProblemError(401)
        user_id = candidate["user_id"]
        set_context(tenant_id, user_id)
        session_id, token, csrf = uuid4(), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        rows("""
            INSERT INTO app.web_sessions
                (id, tenant_id, user_id, token_hash_sha256, csrf_token_hash_sha256, expires_at)
            VALUES (%s, %s, %s, %s, %s, clock_timestamp() + %s * interval '1 second')
        """, (session_id, tenant_id, user_id, sha256(token), sha256(csrf), SESSION_SECONDS))
        request.tenant = candidate
        audit(request, "AUTH_LOGIN_SUCCEEDED", "WEB_SESSION", session_id)
    response = JsonResponse({
        "tenant_id": tenant_id, "default_workspace_id": candidate["default_workspace_id"],
        "role": candidate["role"], "csrf_token": csrf,
    })
    response.set_cookie(name, token, max_age=SESSION_SECONDS, secure=name == COOKIE,
                        httponly=True, samesite="Lax", path="/")
    return response


@api.post("/auth/logout")
def logout(request):
    session_id = request.tenant["session_id"]
    rows("UPDATE app.web_sessions SET revoked_at = clock_timestamp() WHERE id = %s",
         (session_id,))
    audit(request, "AUTH_LOGOUT", "WEB_SESSION", session_id)
    response = JsonResponse(ack(request))
    response.delete_cookie(cookie_name(request), path="/", samesite="Lax")
    return response


@api.get("/tenants/current")
def current_tenant(request):
    tenant = first("SELECT id, slug, display_name, status FROM app.tenants WHERE id = %s",
                   (request.tenant["tenant_id"],))
    policies = rows("""
        SELECT policy_origin, scope_type, scope_ref_id, period_kind, max_cost_micros
        FROM app.budget_policies WHERE is_active
        ORDER BY policy_origin, scope_type, scope_ref_id, period_kind
    """)
    return {**tenant, "role": request.tenant["role"], "budget_policies": policies}


@api.post("/tenants/current/delete-account")
def delete_account(request, data: schemas.DeleteAccount):
    require_permission(request, "delete_account")
    rows("SELECT app.fn_owner_crypto_shred_tenant(%s, %s)",
         (data.confirmation_slug, request.correlation_id))
    response = JsonResponse(ack(request, pitr_retention_disclosure_days=14), status=202)
    response.delete_cookie(cookie_name(request), path="/", samesite="Lax")
    return response


@api.get("/workspaces")
def workspaces(request, limit: int = 50, cursor: UUID | None = None):
    if not 1 <= limit <= 100:
        raise ProblemError(422)
    return page(rows("""
        SELECT id, slug, display_name, emergency_stop FROM app.workspaces
        WHERE NOT is_archived AND (%s::uuid IS NULL OR id > %s)
        ORDER BY id LIMIT %s
    """, (cursor, cursor, limit + 1)), limit)


@api.post("/workspaces")
def add_workspace(request, data: schemas.Workspace):
    require_permission(request, "manage_workspaces")
    workspace = create_workspace(request.tenant["tenant_id"], data.slug, data.display_name)
    audit(request, "WORKSPACE_CREATED", "WORKSPACE", workspace, workspace=workspace)
    return JsonResponse(ack(request, identifier=workspace), status=201)


@api.get("/workspaces/{wsId}/security/consents")
def consents(request, wsId: UUID):
    require_workspace(wsId)
    items = rows("""
        SELECT id, provider_scope, is_granted, consent_version, updated_at
        FROM app.external_consents WHERE workspace_id = %s ORDER BY provider_scope
    """, (wsId,))
    return page(items, 50)


@api.put("/workspaces/{wsId}/security/consents")
def put_consent(request, wsId: UUID, data: schemas.Consent):
    require_permission(request, "manage_security")
    require_workspace(wsId)
    item = first("""
        INSERT INTO app.external_consents (id, tenant_id, workspace_id, provider_scope,
            is_granted, granted_by_user_id, consent_version)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (tenant_id, workspace_id, provider_scope) DO UPDATE
        SET is_granted = EXCLUDED.is_granted, granted_by_user_id = EXCLUDED.granted_by_user_id,
            consent_version = EXCLUDED.consent_version, updated_at = clock_timestamp()
        RETURNING id
    """, (uuid4(), request.tenant["tenant_id"], wsId, str(data.provider_scope),
          data.is_granted, request.tenant["user_id"], data.consent_version))
    audit(request, "CONSENT_UPDATED", "EXTERNAL_CONSENT", item["id"], workspace=wsId)
    return ack(request, identifier=item["id"])


@api.get("/workspaces/{wsId}/security/secrets")
def list_secrets(request, wsId: UUID, limit: int = 50, cursor: UUID | None = None):
    require_permission(request, "manage_security")
    require_workspace(wsId)
    if not 1 <= limit <= 100:
        raise ProblemError(422)
    return page(rows("""
        SELECT id, secret_kind, label, masked_hint FROM app.secrets_vault
        WHERE workspace_id = %s AND NOT is_revoked AND (%s::uuid IS NULL OR id > %s)
        ORDER BY id LIMIT %s
    """, (wsId, cursor, cursor, limit + 1)), limit)


@api.post("/workspaces/{wsId}/security/secrets")
def create_secret(request, wsId: UUID, data: schemas.Secret):
    require_permission(request, "manage_security")
    require_workspace(wsId)
    tenant_id = request.tenant["tenant_id"]
    key = first("""
        SELECT key_version, wrapped_dek_ciphertext FROM app.tenant_encryption_keys
        WHERE tenant_id = %s AND key_state = 'ACTIVE' ORDER BY key_version DESC LIMIT 1
    """, (tenant_id,))
    if not key:
        raise ProblemError(409)
    secret_id = uuid4()
    raw = data.raw_secret.get_secret_value()
    nonce, ciphertext = settings.VAULT.encrypt_secret(
        tenant_id, wsId, secret_id, data.secret_kind, raw,
        key_version=key["key_version"], wrapped_dek=key["wrapped_dek_ciphertext"],
    )
    label, masked = scrub_sensitive_text(data.label, (raw,)), mask_secret_preview(raw)
    item = first("""
        INSERT INTO app.secrets_vault (id, tenant_id, workspace_id, secret_kind, label, masked_hint,
            dek_version, ciphertext_aead, nonce_bytes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, secret_kind, label, masked_hint
    """, (secret_id, tenant_id, wsId, str(data.secret_kind), label, masked, key["key_version"],
          ciphertext, nonce))
    audit(request, "SECRET_CREATED", "SECRET", secret_id, workspace=wsId)
    return JsonResponse(item, status=201)


@api.delete("/workspaces/{wsId}/security/secrets/{secretId}")
def revoke_secret(request, wsId: UUID, secretId: UUID):
    require_permission(request, "manage_security")
    require_workspace(wsId)
    item = first("""
        UPDATE app.secrets_vault SET is_revoked = true
        WHERE id = %s AND workspace_id = %s RETURNING id
    """, (secretId, wsId))
    if not item:
        raise ProblemError(404)
    audit(request, "SECRET_REVOKED", "SECRET", secretId, workspace=wsId)
    return ack(request, identifier=secretId)
