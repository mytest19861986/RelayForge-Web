import json
import os
import select
import signal
import subprocess
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

import psycopg
import pytest
from django.test import Client
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from test_m1_auth_rls_vault_and_shredding import authorized, expect, login, provision, request

CHANNEL = "-1009000000000"


class MessengerHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        assert self.path == "/sendMessage"
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        chat = data["chat_id"]
        self.server.posts.append(data)
        if self.server.slow == chat:
            self.server.received.set()
            assert self.server.release.wait(30)
        status = self.server.status.get(chat, 200)
        content = json.dumps({"message_id": len(self.server.posts)}).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


@pytest.fixture(scope="module")
def messenger():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MessengerHandler)
    server.posts, server.status, server.slow = [], {}, None
    server.received, server.release = Event(), Event()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def account(db):
    provision("m3-publisher")
    result = login("m3-publisher")
    ws = workspace(result)
    secret = expect(authorized(result, "POST", ws + "/security/secrets", {
        "secret_kind": "TELEGRAM_BOT_TOKEN", "label": "M3 bot",
        "raw_secret": "123456:local-m3-fixture",
    }), 201)["id"]
    return result | {"secret_id": secret}


@pytest.fixture(scope="module")
def worker_dsn(db):
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute("ALTER ROLE relayforge_worker PASSWORD 'm3_fixture_only'")
    return make_conninfo(db, user="relayforge_worker", password="m3_fixture_only")


def workspace(session):
    return f"/workspaces/{session['default_workspace_id']}"


def idempotency():
    return {"HTTP_IDEMPOTENCY_KEY": str(uuid4())}


def destination(db, session, chat, brand=None):
    ws = workspace(session)
    identifier = expect(authorized(session, "POST", ws + "/destinations", {
        "platform": "TELEGRAM", "canonical_chat_id": chat, "display_name": chat,
        "secret_id": session["secret_id"], "brand_identity_id": brand,
    }), 201)["id"]
    # Test fixture certifies the synthetic provider target without a real service call.
    with psycopg.connect(db) as conn:
        conn.execute("""
            UPDATE app.destinations SET publish_verified_at = clock_timestamp() WHERE id = %s
        """, (identifier,))
    expect(authorized(session, "PATCH", ws + f"/destinations/{identifier}", {
        "is_enabled": True,
    }), 200)
    return identifier


def publish(session, targets, *, text="محتوای آزمایشی 🔥", mode="LIVE_PUBLISH", scheduled=None):
    path = workspace(session) + "/drafts"
    draft = expect(authorized(session, "POST", path, {"body_text": text}), 201)["id"]
    data = {"mode": mode, "destination_ids": targets, "expected_draft_revision_no": 1,
            "explicit_user_confirmation": True}
    if scheduled:
        data["schedule"] = {"kind": "AT_TIMESTAMP", "scheduled_for": scheduled.isoformat()}
    key = idempotency()
    receipt = expect(authorized(session, "POST", f"{path}/{draft}/publish", data, **key), 202)
    return draft, receipt, data, key


def get_task(db, task_id):
    with psycopg.connect(db, row_factory=dict_row) as conn:
        return conn.execute("SELECT * FROM app.delivery_tasks WHERE id = %s", (task_id,)).fetchone()


@contextmanager
def child(dsn, server, point="", *, ttl=15):
    env = os.environ | {
        "DATABASE_WORKER_URL": dsn, "RF_M3_TEST_PORT": str(server.server_port),
        "RF_M3_TEST_POINT": point,
        "PYTHONPATH": str(Path(__file__).resolve().parent),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "m3_worker_process", "--worker-id", str(uuid4()), "--once",
         "--lease-ttl-sec", str(ttl)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def finish(process, expected=0):
    out, err = process.communicate(timeout=35)
    assert process.returncode == expected, (out, err)
    return err


def checkpoint(process, marker, task):
    assert select.select([process.stdout], [], [], 8)[0], "worker reached no checkpoint"
    assert process.stdout.readline().decode().strip() == f"CHECKPOINT {marker} {task}"


def reaper(dsn):
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute("SELECT app.fn_reap_expired_delivery_leases(100)").fetchone()[0]


def await_lease_expiry(db, task_id):
    task = get_task(db, task_id)
    delay = (task["lease_expires_at"] - datetime.now(UTC)).total_seconds()
    if delay > 0:
        time.sleep(delay + 0.1)
    return task


def reconcile(session, task, action, *, duplicate=False, key=None, expected=200):
    return expect(authorized(
        session, "POST", workspace(session) + f"/deliveries/{task['id']}/reconcile",
        {"expected_lease_epoch": task["lease_epoch"], "resolution_action": action,
         "acknowledge_duplicate_risk": duplicate, "reason": "Checked synthetic destination"},
        **(key or idempotency()),
    ), expected)


def test_ai_free_rules_preview_delivery_independence_and_immutable(db, account, worker_dsn,
                                                                  messenger):
    ws, a, b, unused = workspace(account), CHANNEL + "1", CHANNEL + "2", CHANNEL + "3"
    brand_a = expect(authorized(account, "POST", ws + "/brands", {
        "display_name": "Brand A", "signature_template": "برند الف",
    }), 201)["id"]
    brand_b = expect(authorized(account, "POST", ws + "/brands", {
        "display_name": "Brand B", "signature_template": "برند ب",
    }), 201)["id"]
    expect(authorized(account, "POST", ws + "/brands", {
        "display_name": "Missing attestation", "visual_tokens": {"color": "red"},
    }), 422)
    dest_a = destination(db, account, a, brand_a)
    dest_b = destination(db, account, b, brand_b)
    dest_unused = destination(db, account, unused)
    source = expect(authorized(account, "POST", ws + "/sources", {
        "source_kind": "WEBHOOK_INBOUND", "canonical_channel_id": CHANNEL,
        "display_title": "M3 source", "webhook_platform": "telegram",
    }), 201)
    token = source["webhook_token"]
    post = {"channel_post": {"message_id": 907, "text": "خبر فوری @ads 🔥",
                             "chat": {"id": int(CHANNEL), "type": "channel"}}}
    expect(request(Client(), "POST", f"/webhooks/inbound/telegram/{token}", post,
                   HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=token), 200)
    item = expect(authorized(account, "GET", ws + "/content-items"), 200)["items"][0]["id"]
    policy = {"source_ids": [source["id"]], "destination_ids": [dest_a, dest_b],
              "filter_ast": {"op": "regex_match", "value": "خبر"},
              "transform_rules": {"strip_third_party_mentions": True, "prefix": "🔔 "}}
    route = expect(authorized(account, "POST", ws + "/routes", policy | {
        "slug": "test-rules", "display_name": "Rules", "route_mode": "RULES",
        "automation_level": "AUTO_PUBLISH_BY_POLICY",
    }), 201)
    assert route["is_enabled"] is False and route["revision_no"] == 1
    path = ws + f"/routes/{route['id']}"
    expect(authorized(account, "PATCH", path, {"is_enabled": True}), 200)
    payload = {"content_item_id": item}
    simulated = expect(authorized(account, "POST", path + "/dry-run", payload), 200)
    assert simulated["decision"] == "ENQUEUED" and simulated["simulation"]
    assert not messenger.posts
    assert expect(authorized(account, "GET", ws + "/deliveries"), 200)["items"] == []
    compiled = [step["formatting"]["payload"]["plain_text"] for step in simulated["destinations"]]
    assert [text.splitlines()[-1] for text in compiled] == ["برند الف", "برند ب"]
    assert all(text.startswith("🔔 ") and "@ads" not in text for text in compiled)
    with psycopg.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM app.ai_providers").fetchone()[0] == 0
        assert not conn.execute("SELECT bool_or(allow_external_ai) FROM app.workspace_settings"
                                ).fetchone()[0]
        assert conn.execute("SELECT count(*) FROM app.content_fingerprints").fetchone()[0] == 0

    executed = expect(authorized(account, "POST", path + "/run-item", payload), 200)
    assert executed["decision"] == "ENQUEUED" and len(executed["delivery_task_ids"]) == 2
    repeat = expect(authorized(account, "POST", path + "/run-item", payload), 200)
    assert repeat["decision"] == "DUPLICATE_EXACT_SKIP"
    pub = executed["publication_version_id"]
    updated = expect(authorized(account, "PATCH", path, {
        "policy": policy | {"transform_rules": {"prefix": "changed "}},
    }), 200)
    assert updated["revision_no"] == 2

    messenger.status[b] = 500
    for _ in range(2):
        with child(worker_dsn, messenger) as p:
            finish(p)
    counts = Counter(post["chat_id"] for post in messenger.posts)
    assert counts == {a: 1, b: 1}
    assert all(post["text"] in compiled for post in messenger.posts)
    with psycopg.connect(db, row_factory=dict_row) as conn:
        queued = conn.execute("""
            SELECT id, status, destination_id, remote_message_id FROM app.delivery_tasks
            WHERE publication_version_id = %s
        """, (pub,)).fetchall()
        task_by_dest = {str(t["destination_id"]): t for t in queued}
        assert task_by_dest[dest_a]["status"] == "SENT"
        assert task_by_dest[dest_a]["remote_message_id"]
        assert task_by_dest[dest_b]["status"] == "RETRY_WAIT"
        assert dest_unused not in task_by_dest
        assert conn.execute("""
            SELECT route_policy_revision_no FROM app.publication_versions WHERE id = %s
        """, (pub,)).fetchone()["route_policy_revision_no"] == 1
        for statement in (
            "UPDATE app.publication_versions SET publish_intent = 'EVERGREEN_REPUBLISH' "
            "WHERE id=%s",
            "DELETE FROM app.publication_versions WHERE id=%s",
        ):
            with pytest.raises(psycopg.Error) as rejected, conn.transaction():
                conn.execute(statement, (pub,))
            assert rejected.value.sqlstate == "RF023"
    retry = get_task(db, task_by_dest[dest_b]["id"])["next_retry_at"]
    time.sleep(max(0, (retry - datetime.now(UTC)).total_seconds()) + 0.05)
    messenger.status[b] = 403
    with child(worker_dsn, messenger) as p:
        finish(p)
    assert Counter(post["chat_id"] for post in messenger.posts) == {a: 1, b: 2}
    assert get_task(db, task_by_dest[dest_b]["id"])["status"] == "FAILED"

    draft, first_pub, original, key = publish(
        account, [dest_a], scheduled=datetime.now(UTC) + timedelta(minutes=10),
    )
    draft_path = ws + f"/drafts/{draft}"
    preview = expect(authorized(account, "POST", draft_path + "/preview", {
        "destination_ids": [dest_a, dest_b],
    }), 200)
    assert preview["simulated"] and not preview["platform_rendering_verified"]
    assert preview["destinations"][0]["payload"]["plain_text"].endswith("برند الف")
    expect(authorized(account, "PATCH", draft_path, {
        "expected_draft_revision_no": 1, "body_text": "after edit",
    }), 200)
    expect(authorized(account, "POST", draft_path + "/publish", original, **idempotency()), 409)
    replay = expect(authorized(account, "POST", draft_path + "/publish", original, **key), 202)
    assert replay["id"] == first_pub["id"]
    new = original | {"expected_draft_revision_no": 2, "mode": "EVERGREEN_REPUBLISH"}
    second_pub = expect(authorized(account, "POST", draft_path + "/publish", new,
                                   **idempotency()), 202)
    assert second_pub["id"] != first_pub["id"]
    with psycopg.connect(db) as conn:
        old, actual_new = [conn.execute("""
            SELECT frozen_payload_json FROM app.publication_versions WHERE id = %s
        """, (identifier,)).fetchone()[0]["destinations"][dest_a]["payload"]["plain_text"]
            for identifier in (first_pub["id"], second_pub["id"])]
        assert "after edit" not in old and "after edit" in actual_new
    cancel_path = ws + f"/deliveries/{first_pub['delivery_task_ids'][0]}/cancel"
    expect(authorized(account, "POST", cancel_path), 200)
    _, queued, _, _ = publish(account, [dest_a])
    stop = expect(authorized(account, "POST", ws + "/ops/emergency-stop", {"enabled": True}), 200)
    assert stop["in_flight_count"] == 0 and not stop["remote_deletion_performed"]
    with child(worker_dsn, messenger) as p:
        finish(p)
    assert Counter(post["chat_id"] for post in messenger.posts) == {a: 1, b: 2}
    assert get_task(db, queued["delivery_task_ids"][0])["status"] == "READY"
    stopped = expect(authorized(account, "POST", ws + "/ops/emergency-stop", {
        "enabled": True, "cancel_waiting": True,
    }), 200)
    assert stopped["cancelled_count"] == 2
    expect(authorized(account, "POST", ws + "/ops/emergency-stop", {"enabled": False}), 200)
    batch = expect(authorized(account, "GET", ws + "/events/poll?limit=100"), 200)
    event_types = {item["event_type"] for item in batch["items"]}
    assert {"delivery.sent", "delivery.failed", "publication.version.frozen"} <= event_types
    tail = expect(authorized(account, "GET", ws + "/events/poll?cursor=" +
                             batch["page_info"]["next_cursor"]), 200)
    assert tail["items"] == []


def test_oc02_real_sigkill_fencing_and_heartbeat(db, account, worker_dsn, messenger):
    exit_codes, final = [], []
    for index, (point, original_state, resolved_state, before_post) in enumerate([
        ("reserved", "RESERVED", "READY", 0),
        ("in_flight", "DISPATCHING_IN_FLIGHT", "UNCERTAIN", 0),
        ("response", "DISPATCHING_IN_FLIGHT", "UNCERTAIN", 1),
    ], 1):
        chat = f"{CHANNEL}10{index}"
        target = destination(db, account, chat)
        _, pub, _, _ = publish(account, [target])
        task_id = pub["delivery_task_ids"][0]
        with child(worker_dsn, messenger, point) as p:
            checkpoint(p, point, task_id)
            assert get_task(db, task_id)["status"] == original_state
            assert sum(post["chat_id"] == chat for post in messenger.posts) == before_post
            os.kill(p.pid, signal.SIGKILL)
            assert p.wait(timeout=8) == -signal.SIGKILL
            exit_codes.append(p.returncode)
        previous = await_lease_expiry(db, task_id)
        assert reaper(worker_dsn) == 1
        recovered = get_task(db, task_id)
        assert recovered["status"] == resolved_state
        assert recovered["lease_epoch"] == previous["lease_epoch"] + 1
        with child(worker_dsn, messenger) as p:
            finish(p)
        if index == 2:
            assert sum(post["chat_id"] == chat for post in messenger.posts) == 0
            reconcile(account, recovered, "CONFIRM_NOT_SENT_REQUEUE", expected=422)
            key = idempotency()
            recorded = reconcile(account, recovered, "CONFIRM_NOT_SENT_REQUEUE", duplicate=True,
                                 key=key)
            assert reconcile(account, recovered, "CONFIRM_NOT_SENT_REQUEUE", duplicate=True,
                             key=key)["id"] == recorded["id"]
            assert get_task(db, task_id)["status"] == "READY"
            with child(worker_dsn, messenger) as p:
                finish(p)
        if index == 3:
            assert sum(post["chat_id"] == chat for post in messenger.posts) == 1
            reconcile(account, recovered, "CONFIRM_SENT")
        task = get_task(db, task_id)
        assert task["status"] == "SENT"
        assert sum(post["chat_id"] == chat for post in messenger.posts) == 1
        final.append((chat, task_id))

    chat = CHANNEL + "104"
    _, pub, _, _ = publish(account, [destination(db, account, chat)])
    task_id = pub["delivery_task_ids"][0]
    with child(worker_dsn, messenger, "in_flight") as stale:
        checkpoint(stale, "in_flight", task_id)
        before = await_lease_expiry(db, task_id)
        assert reaper(worker_dsn) == 1
        after = get_task(db, task_id)
        assert after["lease_epoch"] == before["lease_epoch"] + 1
        stale.stdin.write(b"continue\n")
        stale.stdin.flush()
        assert b"RF014" in finish(stale)
    assert sum(post["chat_id"] == chat for post in messenger.posts) == 0
    reconcile(account, after, "CONFIRM_NOT_SENT_REQUEUE", duplicate=True)
    with child(worker_dsn, messenger) as p:
        finish(p)
    assert get_task(db, task_id)["status"] == "SENT"
    final.append((chat, task_id))

    chat = CHANNEL + "105"
    messenger.slow = chat
    _, pub, _, _ = publish(account, [destination(db, account, chat)])
    task_id = pub["delivery_task_ids"][0]
    with child(worker_dsn, messenger) as p:
        assert messenger.received.wait(8)
        try:
            before = await_lease_expiry(db, task_id)
            after = get_task(db, task_id)
            assert after["status"] == "DISPATCHING_IN_FLIGHT"
            assert after["lease_epoch"] == before["lease_epoch"]
            assert after["lease_expires_at"] > before["lease_expires_at"]
            assert reaper(worker_dsn) == 0
        finally:
            messenger.release.set()
        finish(p)
    assert get_task(db, task_id)["status"] == "SENT"
    final.append((chat, task_id))

    post_counts = Counter(post["chat_id"] for post in messenger.posts)
    duplicate_posts = sum(max(0, post_counts[chat] - 1) for chat, _ in final)
    lost_tasks = sum(
        post_counts[chat] == 0 or get_task(db, task)["status"] != "SENT" for chat, task in final
    )
    with psycopg.connect(db) as conn:
        assert conn.execute("""
            SELECT count(*) FROM app.audit_events WHERE action_code = 'DELIVERY_RECONCILED'
        """).fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM app.reconciliation_records").fetchone()[0] == 3
        assert conn.execute("""
            SELECT count(*) FROM app.delivery_attempts WHERE outcome = 'TIMEOUT_UNCERTAIN'
        """).fetchone()[0] == 3
    assert exit_codes == [-9, -9, -9] and duplicate_posts == lost_tasks == 0
    report = {"sigkill_exit_codes": exit_codes, "duplicate_posts": duplicate_posts,
              "lost_tasks": lost_tasks, "scenarios_passed": 5, "stale_sqlstate": "RF014"}
    print("OC-02 " + json.dumps(report, sort_keys=True))
    if output := os.environ.get("RF_OC02_EVIDENCE_OUT"):
        Path(output).write_text(json.dumps(report, sort_keys=True) + "\n")



def test_downtime_catch_up_and_database_fairness(db, account, worker_dsn, messenger):
    workspace_path = workspace(account)
    source_chat, dest_chat = CHANNEL + "200", CHANNEL + "201"
    dest = destination(db, account, dest_chat)
    source = expect(authorized(account, "POST", workspace_path + "/sources", {
        "source_kind": "WEBHOOK_INBOUND", "canonical_channel_id": source_chat,
        "display_title": "Backlog source", "webhook_platform": "telegram",
    }), 201)
    token = source["webhook_token"]
    expect(request(Client(), "POST", f"/webhooks/inbound/telegram/{token}", {
        "channel_post": {"message_id": 2000, "text": "خبرمعوق 🔥",
                         "chat": {"id": int(source_chat), "type": "channel"}},
    }, HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=token), 200)
    item = expect(authorized(account, "GET", workspace_path + "/content-items?q=خبرمعوق"),
                  200)["items"][0]["id"]
    back_source = expect(authorized(account, "POST", workspace_path + "/sources", {
        "source_kind": "TELEGRAM_DESKTOP_EXPORT", "canonical_channel_id": dest_chat,
        "display_title": "Cycle",
    }), 201)["id"]
    back_dest = destination(db, account, source_chat)
    policy = {"source_ids": [source["id"]], "destination_ids": [dest],
              "schedule_policy": {"mode": "AT_TIMESTAMP",
                                  "at_timestamp": (datetime.now(UTC) + timedelta(
                                      minutes=10,
                                  )).isoformat()}}
    route = expect(authorized(account, "POST", workspace_path + "/routes", policy | {
        "slug": "backlog-test", "display_name": "Backlog",
        "automation_level": "AUTO_PUBLISH_BY_POLICY",
    }), 201)["id"]
    cyc = expect(authorized(account, "POST", workspace_path + "/routes", {
        "slug": "would-cycle", "display_name": "Cycle", "source_ids": [back_source],
        "destination_ids": [back_dest],
    }), 409)
    assert cyc["error"]["code"] == "ROUTE_CYCLE_DETECTED"
    route_path = workspace_path + f"/routes/{route}"
    expect(authorized(account, "PATCH", route_path, {"is_enabled": True}), 200)
    queued = expect(authorized(account, "POST", route_path + "/run-item",
                               {"content_item_id": item}), 200)
    task_id = queued["delivery_task_ids"][0]
    assert get_task(db, task_id)["status"] == "SCHEDULED"
    # Move only the fixture's due time to represent an outage without spending a minute asleep.
    with psycopg.connect(db) as conn:
        conn.execute("SET LOCAL ROLE relayforge_migrator")
        conn.execute("SET LOCAL app.migration_mode = 'on'")
        conn.execute("""
            UPDATE app.delivery_tasks SET scheduled_for = clock_timestamp() - interval '2 minutes'
            WHERE id = %s
        """, (task_id,))
    calls = len(messenger.posts)
    with child(worker_dsn, messenger) as proc:
        finish(proc)
    assert len(messenger.posts) == calls
    assert get_task(db, task_id)["last_error_safe"] == "CATCH_UP_HELD_FOR_REVIEW"
    with psycopg.connect(db) as conn:
        assert conn.execute("""
            SELECT approval_state FROM app.drafts WHERE route_id = %s
        """, (route,)).fetchone() == ("NEEDS_REVIEW",)

    provision("m3-second")
    other = login("m3-second")
    other_secret = expect(authorized(other, "POST", workspace(other) + "/security/secrets", {
        "secret_kind": "TELEGRAM_BOT_TOKEN", "label": "Second bot",
        "raw_secret": "123456:local-m3-fixture",
    }), 201)["id"]
    other = other | {"secret_id": other_secret}
    other_dest = destination(db, other, CHANNEL + "202")
    for _ in range(4):
        publish(account, [dest])
    publish(other, [other_dest])
    # Observe a mixed fair claim and deliberately roll it back to release fixture rows.
    with psycopg.connect(worker_dsn) as conn, conn.transaction(force_rollback=True):
        claimed = conn.execute("""
            SELECT claimed_tenant_id FROM app.fn_worker_claim_next_jobs('fair-test', 2, 15)
        """).fetchall()
        assert {str(row[0]) for row in claimed} == {account["tenant_id"], other["tenant_id"]}
