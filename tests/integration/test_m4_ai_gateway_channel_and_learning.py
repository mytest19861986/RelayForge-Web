from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import psycopg
import pytest
from django.db import connections
from test_m1_auth_rls_vault_and_shredding import authorized, expect, login, provision

from relayforge.adapters.ai_gateway import AIError, Generation


def send(account, method, path, body=None, **headers):
    return authorized(account, method, f"/workspaces/{account['default_workspace_id']}" + path,
                      body, **headers)


def key():
    return {"HTTP_IDEMPOTENCY_KEY": str(uuid4())}


def grant(account, scope):
    expect(send(account, "PUT", "/security/consents", {
        "provider_scope": scope, "is_granted": True,
    }), 200)


def configure(account, *, learning=False):
    expect(send(account, "PUT", "/ai/settings", {
        "allow_external_ai": True, "allow_feedback_learning": learning,
    }), 200)


@pytest.fixture
def account(db):
    handle = "m4-" + uuid4().hex[:12]
    provision(handle)
    return login(handle)


def provider(account, **changes):
    return expect(send(account, "PUT", "/ai/providers", {
        "provider_type": "OPENAI", "display_name": "Fixture AI",
        "api_key": "synthetic-fixture-ai-token",
    } | changes), 200)["id"]


def studio(identifier, **changes):
    return {"input_mode": "MANUAL_TEXT", "raw_text": "خبر آزمایشی",
            "task_type": "REWRITE", "freedom_mode": "UNRESTRICTED", "routing_mode": "MANUAL",
            "manual_provider_id": identifier, "manual_model_id": "gpt-local"} | changes


def test_cache_and_escalation(db, account, monkeypatch):
    from relayforge.web import ai_m4

    identifier = provider(account)
    calls = []
    state = {"zero": False}

    class Stub:
        def discover(self):
            calls.append("discover")
            return [("gpt-local", ["TEXT"])]

        def generate(self, model, system, prompt, **kwargs):
            calls.append(model)
            if state["zero"]:
                raise AIError("QUOTA_ZERO", status=429)
            if model == "weak":
                raise AIError("SCHEMA_INVALID", spent=True)
            return Generation("خبر نهایی", None, 11, 5, 90)

    monkeypatch.setattr(ai_m4, "adapter", lambda *_: Stub())
    path = f"/ai/providers/{identifier}/models"
    expect(send(account, "GET", path + "?modality=IMAGE&refresh=true"), 403)
    assert not calls
    configure(account)
    grant(account, "OPENAI_IMAGE")
    grant(account, "OPENAI_TEXT")
    assert expect(send(account, "GET", path + "?modality=IMAGE&refresh=true"), 200)["items"] == []
    assert calls == ["discover"]
    expect(send(account, "POST", path, {"model_id": "image-custom", "modalities": ["IMAGE"]}), 200)
    assert provider(account, id=identifier) == identifier
    items = expect(send(account, "GET", path + "?modality=IMAGE"), 200)["items"]
    assert [item["model_id"] for item in items] == ["image-custom"]
    provider(account, id=identifier, api_key="synthetic-new-token")
    assert expect(send(account, "GET", path + "?modality=IMAGE"), 200)["items"] == []
    provider(account, id=identifier, api_key="synthetic-new-token", routing_profiles=[{
        "task_modality": "TEXT", "ordered_candidates": [
            {"provider_id": identifier, "model_id": "weak", "estimated_micros": 100},
            {"provider_id": identifier, "model_id": "strong", "estimated_micros": 200},
            {"provider_id": identifier, "model_id": "unused", "estimated_micros": 300},
        ], "max_attempts": 3,
    }])
    data = studio(identifier, routing_mode="AUTO_ECONOMY")
    operation = key()
    output = expect(send(account, "POST", "/studio/generate", data, **operation), 200)
    replay = expect(send(account, "POST", "/studio/generate", data, **operation), 200)
    assert replay == output | {"replayed": True}
    assert output["candidates"][0]["text"] == "خبر نهایی"
    assert calls[-2:] == ["weak", "strong"]
    calls.clear()
    state["zero"] = True
    error = expect(send(account, "POST", "/studio/generate", data, **key()), 409)
    assert error["error"]["code"] == "QUOTA_ZERO" and calls == ["weak"]
    assert "synthetic-new-token" not in str(error)


def test_budget_contention_and_cancel(db, account, monkeypatch):
    from relayforge.web import ai_m4

    identifier = provider(account)
    configure(account)
    grant(account, "OPENAI_TEXT")
    expect(send(account, "PUT", "/ai/budgets", {
        "period_kind": "DAILY", "max_cost_micros": 1000,
    }), 200)
    entered, release = Event(), Event()
    starting = Barrier(2, timeout=8)

    class Stub:
        def generate(self, *_args, **_kwargs):
            entered.set()
            assert release.wait(15)
            return Generation("خبر", None, 2, 2, 600)

    monkeypatch.setattr(ai_m4, "adapter", lambda *_: Stub())

    def start():
        try:
            starting.wait()
            return send(account, "POST", "/studio/generate", studio(identifier), **key())
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        requests = [executor.submit(start) for _ in range(2)]
        try:
            assert entered.wait(10)
            import concurrent.futures

            completed, _ = concurrent.futures.wait(
                requests, timeout=10, return_when=concurrent.futures.FIRST_COMPLETED,
            )
            assert len(completed) == 1
            assert expect(next(iter(completed)).result(), 409)["error"]["code"] == "RF001"
        finally:
            release.set()
        assert sorted(item.result().status_code for item in requests) == [200, 409]
    spend = expect(send(account, "GET", "/ai/budgets"), 200)
    assert spend["settled_spend_micros"] == 600 and spend["active_holds_micros"] == 0
    expect(send(account, "PUT", "/ai/budgets", {
        "period_kind": "DAILY", "max_cost_micros": 2000,
    }), 200)
    entered.clear()
    release.clear()
    with ThreadPoolExecutor(max_workers=1) as executor:
        def generate():
            try:
                return send(account, "POST", "/studio/generate", studio(identifier), **key())
            finally:
                connections.close_all()

        pending = executor.submit(generate)
        try:
            assert entered.wait(10)
            with psycopg.connect(db) as conn:
                execution = conn.execute("""
                    SELECT id FROM app.ai_executions WHERE workspace_id=%s AND status='IN_PROGRESS'
                """, (account["default_workspace_id"],)).fetchone()[0]
            expect(send(account, "POST", f"/ai/executions/{execution}/cancel"), 200)
        finally:
            release.set()
        assert expect(pending.result(), 409)["error"]["code"] == "AI_EXECUTION_CANCELLED"
    with psycopg.connect(db) as conn:
        assert conn.execute("""
            SELECT count(*) FROM app.generation_candidates WHERE ai_execution_id=%s
        """, (execution,)).fetchone()[0] == 0
        assert conn.execute("""
            SELECT status FROM app.ai_executions WHERE id=%s
        """, (execution,)).fetchone()[0] == "CANCELLED"
        conn.execute("SET LOCAL ROLE relayforge_web")
        conn.execute("SELECT set_config('app.tenant_id',%s,true)", (account["tenant_id"],))
        with pytest.raises(psycopg.Error) as rejected, conn.transaction():
            conn.execute("UPDATE app.budget_policies SET max_cost_micros=1 "
                         "WHERE policy_origin='OPERATOR_PLAN'")
        assert rejected.value.sqlstate == "RF007"


def test_analysis_styles_and_learning(db, account, monkeypatch):
    from relayforge.web import ai_m4

    tenant, workspace = account["tenant_id"], account["default_workspace_id"]
    source = expect(send(account, "POST", "/sources", {
        "source_kind": "TELEGRAM_DESKTOP_EXPORT", "canonical_channel_id": "-100499",
        "display_title": "Sample source",
    }), 201)["id"]
    from test_m3_publishing_and_real_sigkill import destination

    secret = expect(send(account, "POST", "/security/secrets", {
        "secret_kind": "TELEGRAM_BOT_TOKEN", "label": "Fixture",
        "raw_secret": "123456:local-m3-fixture",
    }), 201)["id"]
    target = destination(db, account | {"secret_id": secret}, "-100599")
    route = expect(send(account, "POST", "/routes", {
        "source_ids": [source], "destination_ids": [target], "slug": "analysis-test",
        "display_name": "Isolation test", "automation_level": "AUTO_PUBLISH_BY_POLICY",
    }), 201)["id"]
    expect(send(account, "PATCH", f"/routes/{route}", {"is_enabled": True}), 200)
    with psycopg.connect(db) as conn:
        conn.execute("""
            WITH samples AS (
                INSERT INTO app.content_items (id,tenant_id,workspace_id,source_id,origin_type,
                    canonical_channel_id,external_message_id,operation_scope,content_hash_sha256)
                SELECT gen_random_uuid(),%s,%s,%s,'ANALYSIS_SAMPLE_ONLY','-100499',n::text,
                       'ANALYSIS_ISOLATED',n::text FROM generate_series(1,120) n
                RETURNING id,tenant_id,external_message_id
            )
            INSERT INTO app.content_item_revisions
                (id,tenant_id,content_item_id,revision_no,plain_text)
            SELECT gen_random_uuid(),tenant_id,id,1,'خبر ' || external_message_id ||
                 repeat(' تحلیل', CASE WHEN external_message_id::int<=60 THEN 3 ELSE 80 END)
                 || chr(10) || '@SampleChannel' FROM samples
        """, (tenant, workspace, source))
    path = "/channel-intelligence/analyses"
    report = expect(send(account, "POST", path, {
        "source_id": source, "vision_enabled": True,
    }, **key()), 202)
    assert report["unique_sample_count"] == 120
    assert len(report["statistical_summary_json"]["clusters"]) == 2
    assert report["extracted_identity_json"]["footers"] == ["@SampleChannel"]
    assert report["visual_summary_json"]["status"] == "UNKNOWN_NO_SAMPLE"
    assert expect(send(account, "GET", "/deliveries"), 200)["items"] == []
    with psycopg.connect(db) as conn:
        item = conn.execute("""
            SELECT id FROM app.content_items WHERE source_id=%s LIMIT 1
        """, (source,)).fetchone()[0]
        expect(send(account, "POST", f"/routes/{route}/run-item",
                    {"content_item_id": str(item)}), 404)
        assert dict(conn.execute("""
            SELECT sample_partition,count(*) FROM app.channel_analysis_samples
            WHERE analysis_id=%s GROUP BY sample_partition
        """, (report["id"],)).fetchall()) == {"EXTRACTION": 96, "EVALUATION": 24}
    with psycopg.connect(db) as conn:
        conn.execute("""
            UPDATE app.content_item_revisions r SET media_manifest_json='[{"kind":"IMAGE"}]'
            FROM app.content_items i WHERE i.source_id=%s AND r.content_item_id=i.id
        """, (source,))
    blocked = expect(send(account, "POST", path, {
        "source_id": source, "vision_enabled": True,
    }, **key()), 403)
    assert blocked["error"]["code"] == "EXTERNAL_AI_CONSENT_REQUIRED"
    style = expect(send(account, "POST", "/styles", {
        "slug": "sample", "title": "Sample", "analysis_id": report["id"],
    }), 201)["id"]
    package = expect(send(account, "GET", f"/styles/{style}/export"), 200)
    assert "@SampleChannel" not in str(package)
    expect(send(account, "POST", "/styles/import", package), 201)
    expect(send(account, "POST", "/styles/import", package | {"bad": 1}), 422)
    assert expect(send(account, "PATCH", f"/styles/{style}", {
        "writing_rules": {"tone": "news"},
    }), 200)["revision_no"] == 2

    brands = [expect(send(account, "POST", "/brands", {
        "display_name": label, "signature_template": label,
    }), 201)["id"] for label in ("برند الف", "برند ب")]
    prose = "این متن بلند برای بررسی الگوی ویرایش متن خبر است"
    feedback = {"brand_identity_id": brands[0], "before": prose + " #آگاهی", "after": prose}
    assert expect(send(account, "POST", "/learning/feedback", feedback), 200)[
        "status"
    ] == "OPT_IN_REQUIRED"
    configure(account, learning=True)
    grant(account, "LOCAL_FEEDBACK_MEMORY")
    grant(account, "OPENAI_TEXT")
    for index in range(3):
        expect(send(account, "POST", "/learning/feedback", feedback | {
            "before": feedback["before"] + f" {index}", "after": feedback["after"] + f" {index}",
        }), 200)
        items = expect(send(account, "GET", "/learning/rules"), 200)["items"]
        assert len(items) == (1 if index == 2 else 0)
    rule = items[0]["id"]
    expect(send(account, "PATCH", f"/learning/rules/{rule}", {"review_state": "ACCEPTED"}), 200)
    identifier = provider(account)
    prompts = []

    class Stub:
        def generate(self, _model, _system, prompt, **kwargs):
            prompts.append(prompt)
            return Generation("خروجی نمونه\n@SampleChannel\n<script>alert(1)</script>",
                              None, None, None, 100)

    monkeypatch.setattr(ai_m4, "adapter", lambda *_: Stub())
    for brand, label in zip(brands, ("برند الف", "برند ب"), strict=True):
        output = expect(send(account, "POST", "/studio/generate", studio(
            identifier, brand_identity_id=brand, style_profile_id=style,
            pinned_style_revision_no=1,
        ), **key()), 200)["candidates"][0]
        assert output["text"].count(label) == 1 and "@SampleChannel" not in output["text"]
        assert "&lt;script&gt;" in output["escaped_html"]
    assert "REDUCE_HASHTAGS" in prompts[0] and "REDUCE_HASHTAGS" not in prompts[1]
    expect(send(account, "DELETE", f"/learning/rules/{rule}"), 200)
    expect(send(account, "POST", "/learning/reset"), 200)
    with psycopg.connect(db) as conn:
        for table in ("feedback_events", "learned_preference_rules"):
            assert conn.execute(f"SELECT count(*) FROM app.{table} WHERE workspace_id=%s",
                                (workspace,)).fetchone()[0] == 0
        with pytest.raises(psycopg.Error) as failure, conn.transaction():
            conn.execute("DELETE FROM app.style_revisions WHERE style_profile_id=%s", (style,))
        assert failure.value.sqlstate == "RF023"
