"""Consent-gated route text generation and deterministic publication decisions."""

from uuid import UUID

from relayforge.adapters.ai_gateway import AIError
from relayforge.core.fact_guard import candidate, prompts
from relayforge.core.security import scrub_sensitive_text
from relayforge.web.ai_m4 import authorized_adapter, provider
from relayforge.web.db import first
from relayforge.web.problems import ProblemError
from relayforge.web.studio_m4 import StudioInput, _new_connection, _style, routing, run_attempt


class AutoPublishRiskGate:
    @staticmethod
    def evaluate(report, freedom, policy, *, has_media=False):
        if report.get("verdict") == "FACT_MISMATCH_BLOCKED" or report.get(
            "sensitive_claim_flagged"
        ):
            return "FACT_GUARD"
        if freedom == "UNRESTRICTED" and policy.get("free_mode_auto_risk_ack") is not True:
            return "UNRESTRICTED_ACK_REQUIRED"
        if has_media:
            return "MEDIA_REVIEW_REQUIRED"
        return None


def rewrite(request, workspace, route, source):
    policy = route["transform_rules_json"]
    freedom = policy.get("freedom_mode", "BALANCED")
    trace = {"freedom_mode": freedom, "fallback_policy": route["fallback_policy"]}
    try:
        settings = StudioInput(
            input_mode="MANUAL_TEXT",
            raw_text=source,
            task_type="REWRITE",
            freedom_mode=freedom,
            routing_mode=policy.get("routing_mode", "AUTO_ECONOMY"),
            manual_provider_id=policy.get("manual_provider_id"),
            manual_model_id=policy.get("manual_model_id"),
            style_profile_id=policy.get("style_profile_id"),
            pinned_style_revision_no=policy.get("pinned_style_revision_no"),
        )
        style, revision = _style(workspace, settings)
        trace.update(
            style_profile_id=str(settings.style_profile_id) if settings.style_profile_id else None,
            style_revision_no=revision,
        )
        options, cross, timeout = routing(workspace, settings)
        failure = "MODEL_NOT_FOUND"
        with _new_connection() as connection:
            for option in options:
                if option["provider_id"] != options[0]["provider_id"] and not cross:
                    continue
                row = provider(workspace, UUID(option["provider_id"]))
                api, key = authorized_adapter(workspace, row, "TEXT")
                api.timeout_sec = timeout
                system, prompt = prompts(
                    scrub_sensitive_text(source, (key,)),
                    "REWRITE",
                    freedom,
                    str(style.get("writing_rules_json", {})),
                )
                trace["model_id"] = option["model_id"]
                try:
                    execution, result = run_attempt(
                        connection,
                        request,
                        workspace,
                        row,
                        api,
                        option["model_id"],
                        "TEXT",
                        option["estimated_micros"],
                        system,
                        prompt,
                        route_id=route["id"],
                    )
                except AIError as error:
                    failure = error.code
                    if route["fallback_policy"] != "FALLBACK_MODEL" or failure == "QUOTA_ZERO":
                        break
                    continue
                text = scrub_sensitive_text(result.text, (key,))
                outcome = candidate(source, text, freedom)
                execution_row = first(
                    "SELECT reservation_id FROM app.ai_executions WHERE id=%s", (execution,)
                )
                trace.update(
                    execution_id=str(execution), reservation_id=str(execution_row["reservation_id"])
                )
                return (
                    text,
                    outcome["fact_diff_json"] | {"verdict": outcome["fact_guard_verdict"]},
                    trace,
                )
    except ProblemError as error:
        failure = error.code or "AI_NOT_AVAILABLE"
    except ValueError:
        failure = "INVALID_ROUTE_AI_POLICY"
    trace["ai_error"] = failure
    return None, {}, trace


def resolve_route_media(workspace, info, policy):
    mode = policy.get("media_policy", "HOLD_MEDIA_FOR_REVIEW")
    manifest = info.get("media_manifest_json", [])
    if mode == "STRIP_MEDIA":
        return [], None
    if mode == "HOLD_MEDIA_FOR_REVIEW":
        return [], "MEDIA_REVIEW_REQUIRED" if manifest else None
    if mode == "ATTACH_WORKSPACE_ASSET":
        identifiers = [policy.get("media_asset_id")]
    elif mode == "KEEP_AUTHORIZED_SOURCE_MEDIA":
        rights = first(
            "SELECT republish_rights_attested FROM app.sources WHERE workspace_id=%s AND id=%s",
            (workspace, info["source_id"]),
        )
        if manifest and (not rights or not rights["republish_rights_attested"]):
            return [], "SOURCE_MEDIA_RIGHTS_REQUIRED"
        identifiers = [
            item.get("media_asset_id") if isinstance(item, dict) else item for item in manifest
        ]
    else:
        return [], "UNKNOWN_MEDIA_POLICY"
    if len(identifiers) > 10:
        return [], "DESTINATION_MEDIA_INCOMPATIBLE"
    assets = []
    for identifier in identifiers:
        try:
            identifier = UUID(str(identifier))
        except ValueError:
            return [], "MEDIA_ASSET_UNAVAILABLE"
        asset = first("""
            SELECT id,media_kind,mime_type,byte_size,s3_object_key,origin_kind,
                license_attribution_json FROM app.media_assets
            WHERE workspace_id=%s AND id=%s AND NOT is_deleted FOR UPDATE
        """, (workspace, identifier))
        if not asset:
            return [], "MEDIA_ASSET_UNAVAILABLE"
        if asset["origin_kind"] == "STOCK_LICENSED" and not asset["license_attribution_json"]:
            return [], "MEDIA_LICENSE_REQUIRED"
        assets.append(asset)
    return assets, None
