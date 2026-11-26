import hashlib
from typing import Literal
from uuid import UUID, uuid4

from django.conf import settings
from ninja import Router, Schema
from psycopg.types.json import Jsonb
from pydantic import Field, SecretStr

from relayforge.adapters.ai_gateway import MODALITIES, MODEL_ID, AIError, adapter, compatible_origin
from relayforge.core.security import SecretKind, mask_secret_preview
from relayforge.web.db import audit, first, rows
from relayforge.web.endpoints import ack, require_permission, require_workspace
from relayforge.web.problems import ProblemError, problem
from relayforge.web.routes_m2 import credentials

router = Router()
ROOT = "/workspaces/{wsId}/ai"
PROVIDER_FIELDS = "id, provider_type, display_name, base_url, is_enabled, key_fingerprint_sha256"


class ProviderInput(Schema):
    id: UUID | None = None
    provider_type: Literal["OPENAI", "COMPATIBLE_OPENAI", "GEMINI"]
    display_name: str = Field(min_length=1, max_length=120)
    api_key: SecretStr
    base_url: str | None = None
    is_enabled: bool = True
    routing_profiles: list[dict] = Field(default_factory=list, max_length=5)


class ManualModel(Schema):
    model_id: str
    modalities: list[str] = Field(min_length=1, max_length=5)


class BudgetInput(Schema):
    period_kind: Literal["DAILY", "MONTHLY", "TOTAL"]
    scope_type: Literal["TENANT", "WORKSPACE", "ROUTE"] = "WORKSPACE"
    route_id: UUID | None = None
    max_cost_micros: int = Field(strict=True, ge=0, le=2**63 - 1)


def provider(workspace, identifier):
    row = first(
        f"""
        SELECT {PROVIDER_FIELDS}, secret_id FROM app.ai_providers
        WHERE workspace_id = %s AND id = %s
    """,
        (workspace, identifier),
    )
    if not row:
        raise ProblemError(404)
    return row


def modality(value):
    if value not in MODALITIES:
        raise ProblemError(422, "INVALID_MODALITY")
    return value


def consent(workspace, provider_type, kind):
    prefix = "GEMINI" if provider_type == "GEMINI" else "OPENAI"
    scope = f"{prefix}_{'AUDIO' if kind == 'TTS' else kind}"
    return bool(
        first(
            """
        SELECT 1 AS allowed FROM app.workspace_settings s JOIN app.external_consents c
          ON c.tenant_id = s.tenant_id AND c.workspace_id = s.workspace_id
        WHERE s.workspace_id = %s AND s.allow_external_ai AND c.provider_scope = %s AND c.is_granted
    """,
            (workspace, scope),
        )
    )


def secret_kind(provider_type):
    return SecretKind.GEMINI_API_KEY if provider_type == "GEMINI" else SecretKind.OPENAI_API_KEY


def authorized_adapter(workspace, row, kind):
    if not row["is_enabled"] or not consent(workspace, row["provider_type"], kind):
        raise ProblemError(403, "EXTERNAL_AI_CONSENT_REQUIRED")
    token = credentials(workspace, row["secret_id"], secret_kind(row["provider_type"]))
    return adapter(row, token), token


def ai_problem(request, error):
    status = {
        "QUOTA_ZERO": 409,
        "RATE_LIMIT_RETRYABLE": 429,
        "AUTH_INVALID": 403,
        "FORBIDDEN": 403,
        "MODEL_NOT_FOUND": 404,
        "BAD_REQUEST": 422,
    }.get(error.code, 503)
    return problem(request, status, code=error.code, retry_after=error.retry_after)


@router.get(ROOT + "/providers")
def providers(request, wsId: UUID):
    require_permission(request, "manage_security")
    require_workspace(wsId)
    return {
        "items": rows(
            f"""
        SELECT {PROVIDER_FIELDS} FROM app.ai_providers WHERE workspace_id = %s ORDER BY id
    """,
            (wsId,),
        ),
        "routing_profiles": rows(
            """
        SELECT task_modality, selection_mode, ordered_candidates_json,
            allow_cross_provider_fallback, max_attempts, timeout_sec
        FROM app.ai_routing_profiles WHERE workspace_id = %s ORDER BY task_modality
    """,
            (wsId,),
        ),
    }


def save_profiles(tenant, workspace, profiles):
    for profile in profiles:
        try:
            kind = modality(profile["task_modality"])
            mode = profile.get("selection_mode", "MANUAL")
            attempts = profile.get("max_attempts", 2)
            fallback = profile.get("allow_cross_provider_fallback", False)
            timeout = profile.get("timeout_sec", 60)
            candidates = profile["ordered_candidates"]
            if mode not in {"MANUAL", "AUTO_ECONOMY", "AUTO_QUALITY"} or (
                type(attempts) is not int
                or not 1 <= attempts <= 5
                or type(fallback) is not bool
                or type(timeout) is not int
                or not 5 <= timeout <= 600
                or not isinstance(candidates, list)
                or not 1 <= len(candidates) <= 5
            ):
                raise ValueError
            clean = []
            for item in candidates:
                identifier = UUID(item["provider_id"])
                provider(workspace, identifier)
                model = item["model_id"]
                estimate = item["estimated_micros"]
                if (
                    not MODEL_ID.fullmatch(model)
                    or type(estimate) is not int
                    or not (1 <= estimate <= 2**31 - 1)
                ):
                    raise ValueError
                clean.append(
                    {
                        "provider_id": str(identifier),
                        "model_id": model,
                        "estimated_micros": estimate,
                    }
                )
        except (KeyError, TypeError, ValueError):
            raise ProblemError(422, "INVALID_ROUTING_PROFILE") from None
        rows(
            """
            INSERT INTO app.ai_routing_profiles (id,tenant_id,workspace_id,task_modality,
                selection_mode,ordered_candidates_json,allow_cross_provider_fallback,
                max_attempts,timeout_sec) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (tenant_id,workspace_id,task_modality) DO UPDATE SET
                selection_mode=excluded.selection_mode,
                ordered_candidates_json=excluded.ordered_candidates_json,
                allow_cross_provider_fallback=excluded.allow_cross_provider_fallback,
                max_attempts=excluded.max_attempts,timeout_sec=excluded.timeout_sec
        """,
            (uuid4(), tenant, workspace, kind, mode, Jsonb(clean), fallback, attempts, timeout),
        )


@router.put(ROOT + "/providers")
def save_provider(request, wsId: UUID, data: ProviderInput):
    require_permission(request, "manage_security")
    require_workspace(wsId)
    tenant = request.tenant["tenant_id"]
    identifier = data.id or uuid4()
    raw = data.api_key.get_secret_value()
    if not 1 <= len(raw) <= 4096 or any(ord(char) < 33 for char in raw):
        raise ProblemError(422, "INVALID_API_KEY")
    if data.provider_type == "COMPATIBLE_OPENAI":
        try:
            base = compatible_origin(data.base_url)
        except ValueError:
            raise ProblemError(422, "EXPECTED_HTTPS_ORIGIN") from None
    else:
        base = None
    rows("SELECT pg_advisory_xact_lock(hashtextextended(%s, 8))", (str(identifier),))
    previous = provider(wsId, identifier) if data.id else None
    if previous and previous["provider_type"] != data.provider_type:
        raise ProblemError(409, "PROVIDER_TYPE_IMMUTABLE")
    fingerprint = hashlib.sha256(
        f"relayforge:ai:v1:{tenant}:{identifier}:{base}:{raw}".encode()
    ).hexdigest()
    secret = previous["secret_id"] if previous else None
    if not previous or fingerprint != previous["key_fingerprint_sha256"]:
        key = first(
            """
            SELECT key_version, wrapped_dek_ciphertext FROM app.tenant_encryption_keys
            WHERE tenant_id = %s AND key_state = 'ACTIVE' ORDER BY key_version DESC LIMIT 1
        """,
            (tenant,),
        )
        if not key:
            raise ProblemError(409)
        secret = uuid4()
        kind = secret_kind(data.provider_type)
        nonce, encrypted = settings.VAULT.encrypt_secret(
            tenant,
            wsId,
            secret,
            kind,
            raw,
            key_version=key["key_version"],
            wrapped_dek=key["wrapped_dek_ciphertext"],
        )
        rows(
            """
            INSERT INTO app.secrets_vault (id, tenant_id, workspace_id, secret_kind, label,
                masked_hint, dek_version, ciphertext_aead, nonce_bytes)
            VALUES (%s,%s,%s,%s,'AI provider',%s,%s,%s,%s)
        """,
            (
                secret,
                tenant,
                wsId,
                str(kind),
                mask_secret_preview(raw),
                key["key_version"],
                encrypted,
                nonce,
            ),
        )
        if previous:
            rows(
                "UPDATE app.secrets_vault SET is_revoked = true WHERE id = %s",
                (previous["secret_id"],),
            )
    rows(
        """
        INSERT INTO app.ai_providers (id,tenant_id,workspace_id,provider_type,display_name,
            base_url,secret_id,key_fingerprint_sha256,is_enabled)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET
            display_name=excluded.display_name,base_url=excluded.base_url,
            secret_id=excluded.secret_id,key_fingerprint_sha256=excluded.key_fingerprint_sha256,
            is_enabled=excluded.is_enabled,updated_at=clock_timestamp()
    """,
        (
            identifier,
            tenant,
            wsId,
            data.provider_type,
            data.display_name,
            base,
            secret,
            fingerprint,
            data.is_enabled,
        ),
    )
    save_profiles(tenant, wsId, data.routing_profiles)
    audit(request, "AI_PROVIDER_SAVED", "AI_PROVIDER", identifier, workspace=wsId)
    return ack(request, identifier=identifier)


def cache_model(tenant, row, model, kinds, basis):
    rows(
        """
        INSERT INTO app.ai_model_cache (id,tenant_id,provider_id,model_id,
            key_fingerprint_sha256,modalities_json,rank_basis) VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (tenant_id,provider_id,model_id,key_fingerprint_sha256)
        DO UPDATE SET modalities_json=excluded.modalities_json,rank_basis=excluded.rank_basis,
            discovered_at=clock_timestamp()
        WHERE app.ai_model_cache.rank_basis <> 'MANUAL' OR excluded.rank_basis = 'MANUAL'
    """,
        (
            uuid4(),
            tenant,
            row["id"],
            model,
            row["key_fingerprint_sha256"],
            Jsonb(sorted(set(kinds))),
            basis,
        ),
    )


@router.get(ROOT + "/providers/{providerId}/models")
def models(request, wsId: UUID, providerId: UUID, modality: str = "TEXT", refresh: bool = False):
    require_permission(request, "manage_security")
    if modality not in MODALITIES:
        raise ProblemError(422, "INVALID_MODALITY")
    row = provider(wsId, providerId)
    if refresh:
        api, _ = authorized_adapter(wsId, row, modality)
        try:
            discovered = api.discover()
        except AIError as error:
            return ai_problem(request, error)
        rows(
            """
            DELETE FROM app.ai_model_cache WHERE provider_id = %s
                AND key_fingerprint_sha256 = %s AND rank_basis <> 'MANUAL'
        """,
            (providerId, row["key_fingerprint_sha256"]),
        )
        for model, kinds in discovered[:1500]:
            cache_model(request.tenant["tenant_id"], row, model, kinds, "HEURISTIC")
    items = rows(
        """
        SELECT model_id, modalities_json, pricing_source, rank_basis
        FROM app.ai_model_cache WHERE provider_id = %s AND key_fingerprint_sha256 = %s
          AND jsonb_exists(modalities_json, %s) ORDER BY model_id
    """,
        (providerId, row["key_fingerprint_sha256"], modality),
    )
    return {"items": items}


@router.post(ROOT + "/providers/{providerId}/models")
def manual_model(request, wsId: UUID, providerId: UUID, data: ManualModel):
    require_permission(request, "manage_security")
    if not MODEL_ID.fullmatch(data.model_id) or not set(data.modalities) <= MODALITIES:
        raise ProblemError(422, "INVALID_MODEL")
    row = provider(wsId, providerId)
    cache_model(request.tenant["tenant_id"], row, data.model_id, data.modalities, "MANUAL")
    return ack(request, identifier=data.model_id)


@router.get(ROOT + "/budgets")
def budgets(request, wsId: UUID):
    require_permission(request, "manage_security")
    require_workspace(wsId)
    policies = rows(
        """
        SELECT policy_origin,scope_type,scope_ref_id,period_kind,max_cost_micros,is_active
        FROM app.budget_policies WHERE scope_type='TENANT' OR
            (scope_type='WORKSPACE' AND scope_ref_id=%s) OR
            (scope_type='ROUTE' AND scope_ref_id IN (
                SELECT id FROM app.routes WHERE workspace_id=%s))
        ORDER BY policy_origin, scope_type, period_kind
    """,
        (wsId, wsId),
    )
    spend = first(
        """
        SELECT COALESCE(SUM(reserved_micros) FILTER (WHERE state='RESERVED'
                   AND expires_at > clock_timestamp()),0)::bigint AS active_holds_micros,
               COALESCE(SUM(settled_micros) FILTER (WHERE state='COMMITTED'),0)::bigint
                   AS settled_spend_micros
        FROM app.budget_reservations WHERE workspace_id=%s
    """,
        (wsId,),
    )
    return {"policies": policies, **spend}


@router.put(ROOT + "/budgets")
def save_budget(request, wsId: UUID, data: BudgetInput):
    require_permission(request, "manage_security")
    require_workspace(wsId)
    tenant = request.tenant["tenant_id"]
    ref = tenant if data.scope_type == "TENANT" else wsId
    if data.scope_type == "ROUTE":
        if not data.route_id or not first(
            "SELECT id FROM app.routes WHERE id=%s AND workspace_id=%s", (data.route_id, wsId)
        ):
            raise ProblemError(404)
        ref = data.route_id
    row = first(
        """
        INSERT INTO app.budget_policies (id,tenant_id,policy_origin,scope_type,scope_ref_id,
            period_kind,max_cost_micros) VALUES (%s,%s,'TENANT_BUDGET',%s,%s,%s,%s)
        ON CONFLICT (tenant_id,policy_origin,scope_type,scope_ref_id,period_kind)
        DO UPDATE SET max_cost_micros=excluded.max_cost_micros,is_active=true RETURNING id
    """,
        (uuid4(), tenant, data.scope_type, ref, data.period_kind, data.max_cost_micros),
    )
    audit(request, "TENANT_BUDGET_SAVED", "BUDGET_POLICY", row["id"], workspace=wsId)
    return ack(request, identifier=row["id"])


@router.post(ROOT + "/executions/{execId}/cancel")
def cancel_execution(request, wsId: UUID, execId: UUID):
    require_permission(request, "edit_content")
    updated = first(
        """
        UPDATE app.ai_executions SET status='CANCELLED',completed_at=clock_timestamp()
        WHERE workspace_id=%s AND id=%s AND status='IN_PROGRESS' RETURNING id
    """,
        (wsId, execId),
    )
    if not updated:
        prior = first(
            "SELECT status FROM app.ai_executions WHERE workspace_id=%s AND id=%s", (wsId, execId)
        )
        if not prior or prior["status"] != "CANCELLED":
            raise ProblemError(409 if prior else 404)
    return ack(request, identifier=execId, status="CANCELLED")


class WorkspaceAISettings(Schema):
    allow_external_ai: bool = Field(strict=True)
    allow_feedback_learning: bool = Field(strict=True)


@router.put(ROOT + "/settings")
def save_workspace_settings(request, wsId: UUID, data: WorkspaceAISettings):
    require_permission(request, "manage_security")
    require_workspace(wsId)
    rows("SELECT pg_advisory_xact_lock(hashtextextended(%s,11))", (str(wsId),))
    rows("""
        UPDATE app.workspace_settings SET allow_external_ai=%s,allow_feedback_learning=%s,
            updated_at=clock_timestamp() WHERE workspace_id=%s
    """, (data.allow_external_ai, data.allow_feedback_learning, wsId))
    audit(request, "WORKSPACE_AI_SETTINGS_UPDATED", "WORKSPACE", wsId, workspace=wsId)
    return ack(request, identifier=wsId)


@router.get(ROOT + "/executions")
def executions(request, wsId: UUID):
    require_permission(request, "edit_content")
    require_workspace(wsId)
    return {"items": rows("""
        SELECT id,status,correlation_id,task_modality,model_id,error_category,
               created_at,completed_at
        FROM app.ai_executions WHERE workspace_id=%s ORDER BY created_at DESC,id LIMIT 100
    """, (wsId,))}
