import base64
import hashlib
import hmac
import json
import re
import shutil
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qsl, quote, unquote, urlsplit
from uuid import uuid4
from xml.etree import ElementTree

import httpx
import psycopg
import pytest
from django.test import Client
from psycopg.conninfo import make_conninfo
from test_m1_auth_rls_vault_and_shredding import authorized, expect, login, provision, request

from relayforge.adapters.messengers import MessengerError, PublishReceipt
from relayforge.adapters.s3_storage import MAX_EXPORT_BYTES, S3Storage, StorageError
from relayforge.workers.export_ingestor import (
    HostileExportStreamError,
    HostileJsonStreamGuard,
    S3ExportStreamIngestor,
)

BUCKET = "rf-test-exports"
ACCESS_KEY = "test-access-key"
SECRET_KEY = "synthetic-s3-signing-key"
CHANNEL = "-1001234567890"
ALGORITHM = "AWS4-HMAC-SHA256"


def _signature(scope, payload):
    day, region, service, terminator = scope.split("/")
    if region != "us-east-1" or service != "s3" or terminator != "aws4_request":
        raise ValueError("Invalid credential scope")
    key = b"AWS4" + SECRET_KEY.encode()
    for part in (day, region, service, terminator):
        key = hmac.new(key, part.encode(), "sha256").digest()
    return hmac.new(key, payload.encode(), "sha256").hexdigest()


class S3FixtureHandler(BaseHTTPRequestHandler):
    """Strict verifier independent of the client implementation."""

    def log_message(self, *_args):
        pass

    def reply(self, status, *, size=0, etag=None):
        self.send_response(status)
        self.send_header("Content-Length", str(size))
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()

    def verify_authorization(self, body):
        path = urlsplit(self.path)
        match = re.fullmatch(
            r"AWS4-HMAC-SHA256 Credential=([^/]+)/([^,]+), SignedHeaders=([^,]+), "
            r"Signature=([0-9a-f]{64})", self.headers.get("Authorization", ""),
        )
        if not match:
            return False
        access, scope, signed, incoming = match.groups()
        fields = signed.split(";")
        if sorted(fields) != fields or not {
            "host", "x-amz-content-sha256", "x-amz-date",
        }.issubset(fields):
            return False
        timestamp = self.headers["x-amz-date"]
        query = "&".join(
            f"{quote(key, safe='')}={quote(value, safe='')}" for key, value in
            sorted(parse_qsl(path.query, keep_blank_values=True))
        )
        header_lines = [f"{key}:{self.headers[key].strip()}" for key in fields]
        body_hash = hashlib.sha256(body).hexdigest()
        if body_hash != self.headers["x-amz-content-sha256"] or scope[:8] != timestamp[:8]:
            return False
        canonical = "\n".join((self.command, path.path, query, *header_lines, "", signed,
                              body_hash))
        string = "\n".join((ALGORITHM, timestamp, scope,
                           hashlib.sha256(canonical.encode()).hexdigest()))
        return access == ACCESS_KEY and hmac.compare_digest(_signature(scope, string), incoming)

    def do_POST(self):
        if self.path != f"/{BUCKET}":
            return self.reply(HTTPStatus.NOT_FOUND)
        try:
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            multipart = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + raw
            )
            parts = {}
            for part in multipart.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if name in parts:
                    raise ValueError("Duplicate multipart field")
                parts[name] = part.get_payload(decode=True)
            file = parts.pop("file")
            fields = {name: value.decode() for name, value in parts.items()}
            encoded = fields["policy"]
            document = json.loads(base64.b64decode(encoded, validate=True))
            expiry = datetime.fromisoformat(document["expiration"])
            if expiry < datetime.now(UTC):
                raise ValueError("Expired")
            access, scope = fields["x-amz-credential"].split("/", 1)
            if access != ACCESS_KEY or fields["x-amz-algorithm"] != ALGORITHM:
                raise ValueError("Unexpected credential")
            if not hmac.compare_digest(_signature(scope, encoded), fields["x-amz-signature"]):
                raise ValueError("Invalid signature")
            if fields.get("bucket") != BUCKET:
                raise ValueError("Missing bucket")
            conditions = document["conditions"]
            match_prefix = False
            match_size_range = False
            constrained = {"policy", "x-amz-signature"}
            for condition in conditions:
                if isinstance(condition, dict):
                    for name, value in condition.items():
                        constrained.add(name)
                        if fields.get(name) != value:
                            raise ValueError("Field tampering")
                elif condition[0] == "starts-with" and condition[1] == "$key":
                    if not fields["key"].startswith(condition[2]):
                        raise ValueError("Key outside tenant prefix")
                    match_prefix = True
                elif condition[0] == "content-length-range":
                    if not condition[1] <= len(file) <= condition[2]:
                        raise ValueError("Size outside policy")
                    match_size_range = True
                else:
                    raise ValueError("Unsupported condition")
            if not match_prefix or not match_size_range or set(fields) != constrained:
                raise ValueError("Upload is not constrained")
            self.save(fields["key"], file)
        except (ValueError, KeyError, AttributeError, TypeError):
            return self.reply(HTTPStatus.FORBIDDEN)
        return self.reply(HTTPStatus.NO_CONTENT)

    def object_path(self, key):
        # Keep fixture data isolated without relying on client-side key validation.
        return self.server.root / hashlib.sha256(key.encode()).hexdigest()

    def save(self, key, content):
        path = self.object_path(key)
        path.write_bytes(content)
        self.server.objects[key] = (path, '"' + hashlib.sha256(content).hexdigest() + '"')

    def object_request(self):
        path = urlsplit(self.path)
        if not path.path.startswith(f"/{BUCKET}/") or path.query:
            return None
        return unquote(path.path.removeprefix(f"/{BUCKET}/"))

    def do_PUT(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if not self.verify_authorization(body):
            return self.reply(HTTPStatus.FORBIDDEN)
        if self.path == f"/{BUCKET}?lifecycle=":
            tree = ElementTree.fromstring(body)
            assert self.headers["Content-MD5"] == base64.b64encode(
                hashlib.md5(body, usedforsecurity=False).digest(),
            ).decode()
            self.server.lifecycle = (
                tree.findtext(".//{*}Days"), tree.findtext(".//{*}Prefix"),
                tree.findtext(".//{*}Status"),
            )
        elif key := self.object_request():
            self.save(key, body)
        else:
            return self.reply(HTTPStatus.NOT_FOUND)
        return self.reply(HTTPStatus.OK)

    def do_HEAD(self):
        self.read_object(send=False)

    def do_GET(self):
        self.read_object(send=True)

    def read_object(self, *, send):
        if not self.verify_authorization(b""):
            return self.reply(HTTPStatus.FORBIDDEN)
        key = self.object_request()
        entry = self.server.objects.get(key)
        if not entry:
            return self.reply(HTTPStatus.NOT_FOUND)
        path, etag = entry
        if self.headers.get("If-Match", etag) != etag:
            return self.reply(HTTPStatus.PRECONDITION_FAILED)
        self.reply(HTTPStatus.OK, size=path.stat().st_size, etag=etag)
        if send:
            try:
                with path.open("rb") as data:
                    shutil.copyfileobj(data, self.wfile, 65536)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The hostile stream guard deliberately stops reading early.

    def do_DELETE(self):
        if not self.verify_authorization(b""):
            return self.reply(HTTPStatus.FORBIDDEN)
        key = self.object_request()
        if key is None:
            return self.reply(HTTPStatus.NOT_FOUND)
        entry = self.server.objects.pop(key, None)
        if entry:
            entry[0].unlink()
        return self.reply(HTTPStatus.NO_CONTENT)


@pytest.fixture(scope="module")
def s3(tmp_path_factory):
    server = ThreadingHTTPServer(("127.0.0.1", 0), S3FixtureHandler)
    server.root = tmp_path_factory.mktemp("s3")
    server.objects, server.lifecycle = {}, None
    url = f"http://127.0.0.1:{server.server_port}"
    runner = Thread(target=server.serve_forever, daemon=True)
    runner.start()
    with pytest.MonkeyPatch.context() as env:
        env.setenv("RF_S3_ENDPOINT_URL", url)
        env.setenv("RF_S3_PUBLIC_UPLOAD_URL", url)
        env.setenv("RF_S3_BUCKET", BUCKET)
        env.setenv("RF_S3_ACCESS_KEY", ACCESS_KEY)
        env.setenv("RF_S3_SECRET_KEY", SECRET_KEY)
        try:
            yield S3Storage.from_environment(), server
        finally:
            server.shutdown()
            server.server_close()
            runner.join(timeout=5)


@pytest.fixture(scope="module")
def tenant_pair(db):
    provision("m2-alpha")
    provision("m2-beta")
    a, b = login("m2-alpha"), login("m2-beta")
    return a, b


@pytest.fixture
def worker(db, s3):
    with psycopg.connect(db, autocommit=True) as admin:
        admin.execute("ALTER ROLE relayforge_worker PASSWORD 'm2_test_only'")
    worker_dsn = make_conninfo(db, user="relayforge_worker", password="m2_test_only")
    return S3ExportStreamIngestor(worker_dsn, s3[0])


def post_upload(presign, body, **overrides):
    fields = presign["form_fields"] | overrides
    return httpx.post(presign["upload_url"], data=fields,
                      files={"file": ("result.json", body, "application/json")}, trust_env=False)


def add_source(session, kind, *, channel=CHANNEL, **kwargs):
    path = f"/workspaces/{session['default_workspace_id']}/sources"
    return expect(authorized(session, "POST", path, {
        "source_kind": kind, "canonical_channel_id": channel, "display_title": "کانال آزمایشی",
    } | kwargs), 201)


def presign_job(session, source, size, mode="ARCHIVE_INGEST", key=None):
    path = f"/workspaces/{session['default_workspace_id']}/sources/{source}/exports"
    identity = key or str(uuid4())
    presigned = expect(authorized(session, "POST", path + "/presign", {
        "file_size_bytes": size, "job_mode": mode,
    }, HTTP_IDEMPOTENCY_KEY=identity), 201)
    uid = presigned["export_job_id"]
    return path + f"/{uid}/complete", presigned


def assert_job(db, job, *, status, code=None):
    with psycopg.connect(db) as conn:
        record = conn.execute("""
            SELECT status, error_code, raw_deleted_at IS NOT NULL, messages_total,
                   messages_imported, messages_skipped
            FROM app.export_import_jobs WHERE id = %s
        """, (job,)).fetchone()
    assert record[:3] == (status, code, True)
    return record[3:]


def test_signed_upload_lifecycle_and_cross_tenant_policy(s3, tenant_pair):
    backend, server = s3
    backend.ensure_exports_lifecycle_24h()
    assert server.lifecycle == ("1", "tenants/", "Enabled")
    a, b = tenant_pair
    signed = backend.generate_export_presigned_post(a["tenant_id"], uuid4(), max_bytes=80)
    key = signed["form_fields"]["key"]
    assert signed["form_fields"]["bucket"] == BUCKET
    spoof = f"tenants/{b['tenant_id']}/exports/spoof.json"
    assert post_upload(signed, b"{}", key=spoof).status_code == 403
    assert post_upload(signed, b"{}", key=key.replace(".json", "-other.json")).status_code == 403
    assert post_upload(signed, b"x" * 81).status_code == 403
    assert post_upload(signed, b"{}", bucket="wrong").status_code == 403
    assert backend.head_object(spoof) is None
    assert post_upload(signed, b"{}").status_code == 204
    assert backend.head_object(key)[0] == 2
    assert b"".join(backend.stream_object_chunks(key)) == b"{}"
    backend.delete_object(key)
    assert key not in server.objects
    backend.put_object(key, b'{"messages":[]}')
    assert b"".join(backend.stream_object_chunks(key, 3)) == b'{"messages":[]}'
    backend.delete_object(key)


@pytest.mark.parametrize(("content", "error"), [
    (b"\x1f\x8b" + b"synthetic", "COMPRESSED_OR_BINARY_UPLOAD_FORBIDDEN"),
    (b"PK\x03\x04" + b"synthetic", "COMPRESSED_OR_BINARY_UPLOAD_FORBIDDEN"),
    (b'{"messages":[' + b'[' * 60 + b']' * 60 + b']}', "JSON_MAX_DEPTH_EXCEEDED"),
    (b'{"messages":[{"text":"' + b"x" * 1_048_576 + b'"}]}', "JSON_MAX_TOKEN_BYTES_EXCEEDED"),
    (b'{"messages":[{"value":' + b'1' * 1_048_576 + b'}]}', "JSON_MAX_TOKEN_BYTES_EXCEEDED"),
    (b'{"messages":[{"type":"message","id":1,"text":"truncate"}', "MALFORMED_EXPORT_JSON"),
    (b'{"ignored":"\xff","messages":[]}', "COMPRESSED_OR_BINARY_UPLOAD_FORBIDDEN"),
    (b'{"messages":[[' + b'0,' * 4500 + b'0]]}', "JSON_MESSAGE_LIMIT_EXCEEDED"),
], ids=["gzip", "zip", "depth", "string", "scalar", "truncated", "utf8", "item-width"])
def test_hostile_stream_fails_and_deletes_raw(db, tenant_pair, s3, worker, content, error):
    a, _ = tenant_pair
    source = add_source(a, "TELEGRAM_DESKTOP_EXPORT", channel=f"-100{uuid4().int % 10**13}")["id"]
    complete, signed = presign_job(a, source, len(content))
    assert post_upload(signed, content).status_code == 204
    expect(authorized(a, "POST", complete), 202)
    assert worker.run_once()["error_code"] == error
    assert assert_job(db, signed["export_job_id"], status="FAILED", code=error) == (0, 0, 0)
    assert signed["form_fields"]["key"] not in s3[1].objects


def test_byte_ceiling_and_chunk_boundary_state():
    chunk = b" " * 65536
    size = MAX_EXPORT_BYTES // len(chunk)
    guard = HostileJsonStreamGuard(chunk for _ in range(size + 1))
    with pytest.raises(HostileExportStreamError, match="EXPORT_MAX_BYTES_EXCEEDED"):
        while guard.read(65536):
            pass
    assert guard.total_bytes == MAX_EXPORT_BYTES + len(chunk)
    body = json.dumps({"messages": [{"text": '🔥"\\ literal [ ]'}]}, ensure_ascii=False).encode()
    one_byte = HostileJsonStreamGuard(bytes([char]) for char in body)
    assert b"".join(iter(lambda: one_byte.read(3), b"")) == body


def test_streaming_export_dedup_search_and_analysis_isolation(
    db, tenant_pair, s3, worker, tmp_path,
):
    a, b = tenant_pair
    workspace = a["default_workspace_id"]
    created = add_source(a, "WEBHOOK_INBOUND", webhook_platform="telegram")
    webhook_path = f"/webhooks/inbound/telegram/{created['webhook_token']}"
    update = {"channel_post": {"message_id": 1, "text": "یافتنی 👋🏽",
                               "chat": {"id": int(CHANNEL), "type": "channel"}}}
    expect(request(Client(), "POST", webhook_path, update), 403)
    response = request(Client(), "POST", webhook_path, update,
                       HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=created["webhook_token"])
    expect(response, 200)
    assert created["webhook_token"].encode() not in response.content
    path = f"/workspaces/{workspace}/sources"
    assert created["webhook_token"] not in authorized(a, "GET", path).content.decode()
    source = add_source(a, "TELEGRAM_DESKTOP_EXPORT")["id"]

    n = 15_500
    raw = tmp_path / "archive.json"
    with raw.open("wb") as file:
        file.write(b'{"id":1234567890,"messages":[')
        for i in range(1, n + 1):
            if i > 1:
                file.write(b",")
            item = {"id": i, "type": "message", "text": "نسخهٔ بایگانی 👨‍👩‍👧‍👦 " * 5}
            if i % 250 == 0:
                item["type"] = "service"
            elif i % 251 == 0:
                item["is_deleted"] = True
            file.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode())
        file.write(b"]}")
    assert raw.stat().st_size > 3 * 1024 * 1024
    identity = str(uuid4())
    complete, signed = presign_job(a, source, raw.stat().st_size, key=identity)
    assert presign_job(a, source, raw.stat().st_size, key=identity) == (complete, signed)
    with raw.open("rb") as content:
        assert post_upload(signed, content).status_code == 204
    expect(authorized(b, "POST", complete), 404)
    expect(authorized(a, "POST", complete), 202)
    metrics = worker.run_once()
    skipped = sum(i % 250 == 0 or i % 251 == 0 for i in range(1, n + 1))
    assert metrics["status"] == "COMPLETED"
    assert metrics["messages_total"] == n and metrics["messages_skipped"] == skipped
    assert metrics["messages_imported"] == n - skipped - 1
    assert 0 <= metrics["delta_rss_mib"] <= 128.0
    print(json.dumps({key: value for key, value in metrics.items() if key.endswith("_mib")}))
    assert assert_job(db, signed["export_job_id"], status="COMPLETED") == (
        n, n - skipped - 1, skipped,
    )
    assert signed["form_fields"]["key"] not in s3[1].objects
    expect(authorized(a, "POST", complete), 202)
    assert worker.run_once() is None

    sample = json.dumps({"messages": [{"id": 1, "type": "message", "text": "یافتنی 🔥"}]},
                        ensure_ascii=False).encode()
    complete, signed = presign_job(a, source, len(sample), "CHANNEL_ANALYSIS_ONLY")
    assert post_upload(signed, sample).status_code == 204
    expect(authorized(a, "POST", complete), 202)
    assert worker.run_once()["messages_imported"] == 1
    assert assert_job(db, signed["export_job_id"], status="COMPLETED") == (1, 1, 0)
    assert signed["form_fields"]["key"] not in s3[1].objects

    listing = f"/workspaces/{workspace}/content-items"
    normal = expect(authorized(a, "GET", listing + "?q=یافتنی"), 200)["items"]
    isolated = expect(authorized(a, "GET", listing + "?q=یافتنی&operation_scope=ANALYSIS_ISOLATED"),
                      200)["items"]
    assert len(normal) == len(isolated) == 1
    assert normal[0]["origin_type"] == "LIVE_BOT_UPDATE"
    assert normal[0]["spans_utf16"] == []
    assert isolated[0]["origin_type"] == "ANALYSIS_SAMPLE_ONLY"
    expect(authorized(b, "GET", listing), 404)
    with psycopg.connect(db) as conn:
        assert conn.execute("""
            SELECT operation_scope, count(*) FROM app.content_items
            WHERE tenant_id = %s AND external_message_id = '1' GROUP BY operation_scope
        """, (a["tenant_id"],)).fetchall() == [("ANALYSIS_ISOLATED", 1), ("INGEST_PIPELINE", 1)]
        assert conn.execute("""
            SELECT last_external_message_id FROM app.source_cursors WHERE source_id = %s
        """, (source,)).fetchone()[0] == str(max(
            i for i in range(1, n + 1) if i % 250 and i % 251
        ))
        assert conn.execute("""
            SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'app'
                AND tablename = 'content_item_revisions'
                AND indexdef ILIKE '%USING gin%fts_vector%')
        """).fetchone()[0]


def test_worker_claim_skips_locked_job_and_recovers_cleanup(db, tenant_pair, s3, worker):
    a, _ = tenant_pair
    source = add_source(a, "TELEGRAM_DESKTOP_EXPORT", channel="-100000001")["id"]
    body = b'{"messages":[]}'
    complete, signed = presign_job(a, source, len(body))
    assert post_upload(signed, body).status_code == 204
    expect(authorized(a, "POST", complete), 202)
    key = signed["form_fields"]["key"]
    worker_dsn = make_conninfo(db, user="relayforge_worker", password="m2_test_only")
    with psycopg.connect(worker_dsn) as locked:
        assert locked.execute("SELECT job_id FROM app.fn_worker_claim_export_job()").fetchone()
        assert worker.run_once() is None
    with psycopg.connect(db) as conn:
        conn.execute("""
            UPDATE app.export_import_jobs SET status = 'FAILED', error_code = 'TEST_CLEANUP_PENDING'
            WHERE id = %s
        """, (signed["export_job_id"],))
    assert worker.run_once() == {"status": "RAW_DELETED"}
    assert key not in s3[1].objects
    assert_job(db, signed["export_job_id"], status="FAILED", code="TEST_CLEANUP_PENDING")



def test_destination_source_probes_idempotent_publish_and_purge(db, tenant_pair, monkeypatch):
    from relayforge.web import routes_m2

    a, b = tenant_pair
    ws = a["default_workspace_id"]
    prefix = f"/workspaces/{ws}"
    sender_token = "123456:fixture-bot-token"
    secret = expect(authorized(a, "POST", prefix + "/security/secrets", {
        "secret_kind": "TELEGRAM_BOT_TOKEN", "label": "Bot", "raw_secret": sender_token,
    }), 201)["id"]
    calls = []

    class LocalTelegram:
        platform_tier = "GA"

        def resolve_and_verify_source_channel(self, token, chat_ref):
            assert token == sender_token and chat_ref == "@example"
            calls.append("resolve")
            return {"canonical_channel_id": CHANNEL, "display_title": "آزمایشی"}

        def test_connection(self, token):
            assert token == sender_token
            calls.append("probe")
            return {"status": "BOT_IDENTITY_VERIFIED", "publish_verified": False}

        def test_publish(self, token, chat_id, payload, *, explicit_user_confirmation):
            assert token == sender_token and chat_id == CHANNEL
            assert explicit_user_confirmation and not payload.length_overflow
            calls.append("send")
            if payload.payload["plain_text"] == "uncertain result":
                raise MessengerError("TRANSIENT_NETWORK")
            return PublishReceipt("1234")

    monkeypatch.setitem(routes_m2.CONNECTORS, "TELEGRAM", LocalTelegram)
    source = add_source(a, "TELEGRAM_BOT_CHANNEL", channel="@example", secret_id=secret)
    check = expect(authorized(a, "POST", f"{prefix}/sources/{source['id']}/test"), 200)
    assert check["canonical_channel_id"] == CHANNEL and check["status"] == "HEALTHY"
    expect(authorized(a, "POST", prefix + "/sources", {
        "source_kind": "TELEGRAM_MTPROTO_SESSION", "canonical_channel_id": CHANNEL,
        "display_title": "unsupported",
    }), 422)
    destinations = prefix + "/destinations"
    data = {"platform": "TELEGRAM", "display_name": "Main", "canonical_chat_id": CHANNEL,
            "secret_id": secret}
    identifier = expect(authorized(a, "POST", destinations, data), 201)["id"]
    other_destinations = f"/workspaces/{b['default_workspace_id']}/destinations"
    expect(authorized(b, "POST", other_destinations, data), 404)
    target = f"{destinations}/{identifier}"
    row = expect(authorized(a, "GET", destinations), 200)["items"][0]
    assert row["backlog_mode"] == "NEW_ONLY" and row["publish_verified_at"] is None
    assert row["capabilities_json"]["native_albums"] is True
    expect(authorized(a, "PATCH", target, {"is_enabled": True}), 409)
    probe = expect(authorized(a, "POST", target + "/test-connection"), 200)
    assert not probe["publish_verified"]
    assert calls == ["resolve", "probe"]

    publish = target + "/test-publish"
    idempotency = {"HTTP_IDEMPOTENCY_KEY": str(uuid4())}
    expect(authorized(a, "POST", publish, {"explicit_user_confirmation": True}), 422)
    expect(authorized(a, "POST", publish, {"explicit_user_confirmation": 1}, **idempotency), 422)
    confirmed = {"explicit_user_confirmation": True, "text": "پست آزمایشی 🔥"}
    sent = expect(authorized(a, "POST", publish, confirmed, **idempotency), 200)
    replay = expect(authorized(a, "POST", publish, confirmed, **idempotency), 200)
    assert sent["remote_message_id"] == replay["remote_message_id"] == "1234"
    assert calls == ["resolve", "probe", "send"]
    expect(authorized(a, "POST", publish, confirmed | {"text": "changed"}, **idempotency), 409)
    uncertain = {"HTTP_IDEMPOTENCY_KEY": str(uuid4())}
    uncertain_payload = confirmed | {"text": "uncertain result"}
    expect(authorized(a, "POST", publish, uncertain_payload, **uncertain), 503)
    result = expect(authorized(a, "POST", publish, uncertain_payload, **uncertain), 409)
    assert result["error"]["code"] == "TEST_PUBLISH_OUTCOME_UNCERTAIN"
    assert calls == ["resolve", "probe", "send", "send"]

    expect(authorized(a, "PATCH", target, {"is_enabled": True}), 200)
    with psycopg.connect(db) as conn:
        conn.execute("SET LOCAL ROLE relayforge_migrator")
        conn.execute("SET LOCAL app.migration_mode = 'on'")
        for status in ("PENDING", "SCHEDULED", "READY", "RETRY_WAIT", "SENT"):
            conn.execute("""
                INSERT INTO app.delivery_tasks (
                    id, tenant_id, workspace_id, destination_id, idempotency_key,
                    status, scheduled_for, next_retry_at
                ) VALUES (%s, %s, %s, %s, %s, %s, clock_timestamp(), clock_timestamp())
            """, (uuid4(), a["tenant_id"], ws, identifier, str(uuid4()), status))
    purge = expect(authorized(a, "POST", target + "/purge-queue"), 200)
    assert purge["cancelled_count"] == 4
    with psycopg.connect(db) as conn:
        assert dict(conn.execute("""
            SELECT status, count(*) FROM app.delivery_tasks WHERE destination_id = %s
            GROUP BY status
        """, (identifier,)).fetchall()) == {"CANCELLED": 4, "SENT": 1}
        assert conn.execute("""
            SELECT count(*) FROM app.delivery_tasks
            WHERE destination_id = %s AND status = 'CANCELLED' AND lease_epoch = 1
        """, (identifier,)).fetchone()[0] == 4
    expect(authorized(a, "PATCH", target, {"is_archived": True}), 200)
    assert expect(authorized(a, "GET", destinations), 200)["items"] == []


def test_pinned_export_object_and_raw_cleanup_retry(db, tenant_pair, s3, worker, monkeypatch):
    a, _ = tenant_pair
    backend, server = s3
    source = add_source(a, "TELEGRAM_DESKTOP_EXPORT", channel="-100000012")["id"]
    body = b'{"messages":[],"x":1}'
    complete, signed = presign_job(a, source, len(body))
    assert post_upload(signed, body).status_code == 204
    expect(authorized(a, "POST", complete), 202)
    assert post_upload(signed, body.replace(b'"x":1', b'"x":2')).status_code == 204
    key = signed["form_fields"]["key"]
    with monkeypatch.context() as temporary:
        def fail_delete(_key):
            raise StorageError("S3_DELETE_TRANSPORT")

        temporary.setattr(backend, "delete_object", fail_delete)
        with pytest.raises(StorageError, match="S3_DELETE_TRANSPORT"):
            worker.run_once()
    with psycopg.connect(db) as conn:
        assert conn.execute("""
            SELECT status, error_code, raw_deleted_at FROM app.export_import_jobs WHERE id = %s
        """, (signed["export_job_id"],)).fetchone() == ("FAILED", "EXPORT_OBJECT_READ_FAILED", None)
    assert key in server.objects
    assert worker.run_once() == {"status": "RAW_DELETED"}
    assert_job(db, signed["export_job_id"], status="FAILED", code="EXPORT_OBJECT_READ_FAILED")
    assert key not in server.objects


def test_malformed_tail_rolls_back_partial_batches(db, tenant_pair, s3, worker):
    a, _ = tenant_pair
    source = add_source(a, "TELEGRAM_DESKTOP_EXPORT", channel="-100000013")["id"]
    prefix = b'{"messages":['
    messages = [json.dumps({"id": i, "type": "message", "text": "batch"}).encode()
                for i in range(1, 400)]
    invalid = prefix + b",".join(messages) + b", {"
    complete, signed = presign_job(a, source, len(invalid))
    assert post_upload(signed, invalid).status_code == 204
    expect(authorized(a, "POST", complete), 202)
    report = worker.run_once()
    assert report["error_code"] == "MALFORMED_EXPORT_JSON"
    assert_job(db, signed["export_job_id"], status="FAILED", code="MALFORMED_EXPORT_JSON")
    assert signed["form_fields"]["key"] not in s3[1].objects
    with psycopg.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM app.content_items WHERE source_id = %s",
                            (source,)).fetchone()[0] == 0
