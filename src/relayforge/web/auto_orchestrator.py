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
