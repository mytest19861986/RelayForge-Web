import base64
import io
import os
import socket
import subprocess
import sys
import time
import wave
from threading import Lock
from uuid import uuid4

import psycopg
import pytest
from PIL import Image
from psycopg.conninfo import make_conninfo
from test_m1_auth_rls_vault_and_shredding import expect, login, provision
from test_m4_ai_gateway_channel_and_learning import configure, grant, key, provider, send

from relayforge.adapters.ai_media import MediaResult
from relayforge.adapters.media_rpc import media_rpc as socket_rpc
from relayforge.workers.media_jobs import MediaJobs
from relayforge.workers.media_worker import MediaServer


class MemoryStorage:
    def __init__(self):
        self.objects = {}

    def put_object(self, key, data):
        self.objects[key] = data

    def stream_object_chunks(self, key):
        yield self.objects[key]


@pytest.fixture
def daemon(db, tmp_path, monkeypatch):
    try:
        with socket.socket(socket.AF_UNIX):
            pass
    except PermissionError:
        if os.environ.get("CI"):
            raise
        server = object.__new__(MediaServer)
        server.staging, server.jobs, server.lock = tmp_path, {}, Lock()

        def direct_rpc(command, **payload):
            return server.command({"command": command, **payload})

        monkeypatch.setattr("relayforge.workers.media_jobs.media_rpc", direct_rpc)
        monkeypatch.setattr("relayforge.web.media_m5.media_rpc", direct_rpc)
        yield tmp_path, direct_rpc
        return
    address = tmp_path / "media.sock"
    monkeypatch.setenv("RF_MEDIA_SOCKET_PATH", str(address))
    monkeypatch.setenv("RF_MEDIA_STAGING_ROOT", str(tmp_path))
    process = subprocess.Popen([sys.executable, "-m", "relayforge.workers.media_worker"])
    try:
        deadline = time.monotonic() + 5
        while not address.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.025)
        assert process.poll() is None
        assert socket_rpc("ping")["ffmpeg"] is True
        yield tmp_path, socket_rpc
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.fixture
def account(db):
    handle = "m5-" + uuid4().hex[:12]
    provision(handle)
    return login(handle)


def test_real_clip_and_voice_note(daemon):
    daemon, media_rpc = daemon
    job = uuid4()
    root = daemon / str(job)
    root.mkdir()
    Image.new("RGB", (128, 128), "green").save(root / "slide.png")
    result = media_rpc(
        "compose_clip_ffmpeg",
        job_id=str(job),
        spec={"sources": ["slide.png"], "caption": "گزارش"},
    )
    assert result["status"] == "SUCCEEDED", result
    clip = base64.b64decode(result["content_base64"])
    assert clip[4:8] == b"ftyp"
    assert result["duration_sec"] == 2
    assert not root.exists()
    job = uuid4()
    root = daemon / str(job)
    root.mkdir()
    (root / "speech.wav").write_bytes(wav())
    result = media_rpc(
        "assemble_podcast_audio",
        job_id=str(job),
        spec={"sources": ["speech.wav"], "output_format": "VOICE_NOTE"},
    )
    assert result["status"] == "SUCCEEDED", result
    assert base64.b64decode(result["content_base64"]).startswith(b"OggS")
    assert result["duration_sec"] == pytest.approx(0.25)
    assert not root.exists()


def wav():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        writer.writeframes(b"\x00\x00" * 6000)
    return stream.getvalue()


def worker_url(db):
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute("ALTER ROLE relayforge_worker PASSWORD 'm5_test_only'")
    return make_conninfo(db, user="relayforge_worker", password="m5_test_only")


def test_podcast_edit_reuses_nine_segments(db, account, daemon, monkeypatch):
    from relayforge.workers import media_jobs

    selected = provider(account)
    configure(account)
    grant(account, "OPENAI_AUDIO")
    expect(send(account, "POST", f"/ai/providers/{selected}/models", {
        "model_id": "tts-local", "modalities": ["TTS"],
    }), 200)
    calls = []

    def fake_speech(api, model, text, voice):
        calls.append((model, text, voice))
        return MediaResult(data=wav(), cost_micros=8)

    monkeypatch.setattr(media_jobs, "speech", fake_speech)
    storage = MemoryStorage()
    worker = MediaJobs(worker_url(db), storage, daemon[0])
    data = {
        "speaker_mode": "DUAL_SPEAKER",
        "target_duration_sec": 30,
        "approved": True,
        "segments": [
            {"speaker": "A" if i % 2 else "B", "text": f"دیالوگ {i}", "voice_id": "alloy"}
            for i in range(10)
        ],
    }
    script = expect(send(account, "POST", "/podcast-scripts", data), 201)["id"]
    assert not calls
    spec = {
        "job_kind": "TTS_SYNTHESIS",
        "provider_id": selected,
        "model_id": "tts-local",
        "podcast_script_id": script,
    }
    job = expect(send(account, "POST", "/media/jobs", spec, **key()), 202)["id"]
    assert worker.run_once() == "SUCCEEDED"
    assert len(calls) == 10
    status = expect(send(account, "GET", f"/media/jobs/{job}"), 200)
    assert status["output_media_asset_id"]
    data["segments"][0]["text"] = "دیالوگ تغییر کرد"
    expect(send(account, "PATCH", f"/podcast-scripts/{script}", data), 200)
    expect(send(account, "POST", "/media/jobs", spec | {"output_format": "AUDIO"}, **key()), 202)
    assert worker.run_once() == "SUCCEEDED"
    assert len(calls) == 11
    assert any(data.startswith(b"OggS") for data in storage.objects.values())


def test_probe_media_modalities(db, account, monkeypatch):
    from relayforge.web import studio_m4

    selected = provider(account, provider_type="GEMINI")
    configure(account)
    calls = []

    def fake_probe(api, model, modality):
        calls.append((model, modality))
        return MediaResult(operation="operations/example" if modality == "VIDEO" else None)

    monkeypatch.setattr(studio_m4, "synthetic_probe", fake_probe)
    for modality, scope in (("IMAGE", "IMAGE"), ("TTS", "AUDIO"), ("VIDEO", "VIDEO")):
        payload = {
            "provider_id": selected,
            "modality": modality,
            "model_id": "test-model",
            "explicit_token_spend_confirmation": True,
        }
        expect(send(account, "POST", "/ai/models/probe", payload), 403)
        grant(account, f"GEMINI_{scope}")
        expect(send(account, "POST", "/ai/models/probe", payload), 200)
    assert calls == [("test-model", kind) for kind in ("IMAGE", "TTS", "VIDEO")]


def test_ffmpeg_preflight_without_daemon(db, account, monkeypatch):
    monkeypatch.delenv("RF_MEDIA_SOCKET_PATH", raising=False)
    result = expect(
        send(
            account,
            "POST",
            "/media/jobs",
            {
                "job_kind": "LOCAL_SLIDESHOW_CLIP",
            },
            **key(),
        ),
        503,
    )
    assert result["error"]["code"] == "FFMPEG_UNAVAILABLE"


def test_ai_route_holds_failures_and_enforces_cap(db, account, monkeypatch):
    from django.test import Client
    from test_m1_auth_rls_vault_and_shredding import request
    from test_m3_publishing_and_real_sigkill import CHANNEL, destination

    from relayforge.adapters.ai_gateway import AIError, Generation
    from relayforge.web import ai_m4

    secret = expect(
        send(
            account,
            "POST",
            "/security/secrets",
            {
                "secret_kind": "TELEGRAM_BOT_TOKEN",
                "label": "M5 bot",
                "raw_secret": "123456:m5-local-fixture",
            },
        ),
        201,
    )["id"]
    dest = destination(db, account | {"secret_id": secret}, CHANNEL + "1")
    source = expect(
        send(
            account,
            "POST",
            "/sources",
            {
                "source_kind": "WEBHOOK_INBOUND",
                "canonical_channel_id": CHANNEL,
                "display_title": "M5 source",
                "webhook_platform": "telegram",
            },
        ),
        201,
    )
    selected = provider(account)
    configure(account)
    grant(account, "OPENAI_TEXT")
    outputs = iter(
        (
            "قیمت ۹۹۹ تومان",
            AIError("UPSTREAM_5XX", status=503),
            "دریا و گزارش تصویری ۳۰۱ تومان",
            "فناوری و آزمایش مدل ۴۰۲ تومان",
            "آموزش و معماری شهری ۵۰۳ تومان",
        )
    )
    seen = []

    class Stub:
        def generate(self, *_args, **_kwargs):
            output = next(outputs)
            seen.append(output)
            if isinstance(output, Exception):
                raise output
            return Generation(output, None, 1, 1, 2)

    monkeypatch.setattr(ai_m4, "adapter", lambda *_: Stub())
    route = expect(
        send(
            account,
            "POST",
            "/routes",
            {
                "source_ids": [source["id"]],
                "destination_ids": [dest],
                "daily_post_cap": 3,
                "slug": "m5-ai",
                "display_name": "M5 AI",
                "route_mode": "AI",
                "automation_level": "AUTO_PUBLISH_BY_POLICY",
                "transform_rules": {
                    "routing_mode": "MANUAL",
                    "manual_provider_id": selected,
                    "manual_model_id": "gpt-local",
                    "freedom_mode": "BALANCED",
                },
            },
        ),
        201,
    )
    path = f"/routes/{route['id']}"
    expect(send(account, "PATCH", path, {"is_enabled": True}), 200)
    token = source["webhook_token"]

    def run(message, text):
        post = {
            "channel_post": {
                "message_id": message,
                "text": text,
                "chat": {"id": int(CHANNEL), "type": "channel"},
            }
        }
        expect(
            request(
                Client(),
                "POST",
                f"/webhooks/inbound/telegram/{token}",
                post,
                HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=token,
            ),
            200,
        )
        with psycopg.connect(db) as conn:
            item = conn.execute(
                """
                SELECT ci.id FROM app.content_items ci JOIN app.content_item_revisions r
                 ON r.content_item_id=ci.id WHERE ci.source_id=%s AND r.plain_text=%s
            """,
                (source["id"], text),
            ).fetchone()[0]
        return expect(
            send(account, "POST", path + "/run-item", {"content_item_id": str(item)}), 200
        )

    mismatch = run(1, "قیمت ۱۰۰ تومان")
    assert mismatch["decision"] == "NEEDS_REVIEW" and mismatch["risk_gate"] == "FACT_GUARD"
    failed = run(2, "فروش ویژه باغچه ۲۰۰ تومان")
    assert failed["decision"] == "NEEDS_REVIEW" and failed["ai_error"] == "UPSTREAM_5XX"
    for i, text in enumerate(
        (
            "دریا و گزارش تصویری ۳۰۱ تومان",
            "فناوری و آزمایش مدل ۴۰۲ تومان",
            "آموزش و معماری شهری ۵۰۳ تومان",
        ),
        3,
    ):
        assert run(i, text)["decision"] == "ENQUEUED"
    assert run(6, "نجوم و برنامه شبانه ۶۰۴ تومان")["decision"] == "DAILY_CAP_REACHED"
    with psycopg.connect(db) as conn:
        conn.execute(
            "UPDATE app.workspaces SET emergency_stop=true WHERE id=%s",
            (account["default_workspace_id"],),
        )
    report = expect(send(account, "GET", "/reports/automated-runs"), 200)["items"]
    assert len(report) == 6
    assert len(seen) == 5
    assert sum(len(row["delivery_tasks"]) for row in report) == 3


def test_cancel_api_kills_process_group(db, account, daemon, monkeypatch):
    import signal
    from concurrent.futures import ThreadPoolExecutor

    staging, rpc = daemon
    process_marker = staging / "ffmpeg.pid"
    # Execute a separate daemon configured before it resolves ffmpeg in CI.
    ffmpeg = staging / "ffmpeg"
    ffmpeg.write_text(f"#!/bin/sh\nprintf '%s' $$ > '{process_marker}'\nexec sleep 60\n")
    ffmpeg.chmod(0o700)
    monkeypatch.setenv("PATH", f"{staging}:{os.environ['PATH']}")
    restart = None
    if rpc is socket_rpc:
        address = staging / "cancel.sock"
        monkeypatch.setenv("RF_MEDIA_SOCKET_PATH", str(address))
        restart = subprocess.Popen([sys.executable, "-m", "relayforge.workers.media_worker"])
        deadline = time.monotonic() + 5
        while not address.exists() and time.monotonic() < deadline:
            time.sleep(0.025)
        assert address.exists()
    try:
        queued = expect(
            send(
                account,
                "POST",
                "/media/jobs",
                {
                    "job_kind": "LOCAL_SLIDESHOW_CLIP",
                },
                **key(),
            ),
            202,
        )
        job = queued["id"]
        root = staging / job
        root.mkdir()
        Image.new("RGB", (20, 20)).save(root / "clip.png")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                rpc,
                "compose_clip_ffmpeg",
                job_id=job,
                spec={"sources": ["clip.png"]},
            )
            deadline = time.monotonic() + 8
            while not process_marker.exists() and time.monotonic() < deadline:
                time.sleep(0.025)
            assert process_marker.exists()
            pid = int(process_marker.read_text())
            assert os.getpgid(pid) == pid
            expect(send(account, "DELETE", f"/media/jobs/{job}"), 200)
            if rpc is socket_rpc:
                assert future.result(timeout=8)["status"] == "CANCELLED"
            else:
                with pytest.raises(ValueError, match="CANCELLED"):
                    future.result(timeout=8)
            with pytest.raises(ProcessLookupError):
                os.kill(pid, signal.SIGCONT)
            assert not root.exists()
    finally:
        if restart:
            restart.terminate()
            restart.wait(timeout=5)


def test_stock_album_delivery_and_reference_guard(db, account, monkeypatch):
    from django.conf import settings
    from test_m3_publishing_and_real_sigkill import CHANNEL, destination

    from relayforge.adapters.messengers import EitaayarAdapter, TelegramBotAdapter
    from relayforge.web import media_m5
    from relayforge.workers.dispatcher import Dispatcher

    storage = MemoryStorage()
    monkeypatch.setattr(media_m5, "storage", lambda: storage)
    monkeypatch.setenv("RF_PEXELS_API_KEY", "synthetic-pexels-test")
    stock_requests = []

    class Stock:
        def __init__(self, _key):
            pass

        def search(self, query, *, video=False):
            stock_requests.append(query)
            return [{"asset_id": "1", "license_url": "https://www.pexels.com/license/"}]

        def download(self, identifier, *, video=False):
            content = io.BytesIO()
            Image.new("RGB", (40, 40), (int(identifier) * 10, 0, 0)).save(content, format="PNG")
            return content.getvalue(), {
                "asset_id": identifier,
                "license_url": "https://www.pexels.com/license/",
            }

    monkeypatch.setattr(media_m5, "PexelsStockAdapter", Stock)
    expect(send(account, "POST", "/media/search", {"text": "طبیعت"}), 403)
    expect(
        send(
            account,
            "PUT",
            "/ai/settings",
            {
                "allow_external_ai": False,
                "allow_feedback_learning": False,
                "allow_external_media_search": True,
            },
        ),
        200,
    )
    grant(account, "STOCK_MEDIA_SEARCH")
    expect(send(account, "POST", "/media/search", {"text": "طبیعت @private"}), 200)
    assert stock_requests == ["nature"]
    assets = [
        expect(send(account, "POST", "/media/assets/stock-download", {"stock_id": str(i)}), 201)[
            "id"
        ]
        for i in (1, 2, 3)
    ]
    telegram_secret = expect(
        send(
            account,
            "POST",
            "/security/secrets",
            {
                "secret_kind": "TELEGRAM_BOT_TOKEN",
                "label": "T bot",
                "raw_secret": "1234:fixture-telegram",
            },
        ),
        201,
    )["id"]
    eitaa_secret = expect(
        send(
            account,
            "POST",
            "/security/secrets",
            {
                "secret_kind": "EITAA_API_TOKEN",
                "label": "E bot",
                "raw_secret": "fixture-eitaa-token",
            },
        ),
        201,
    )["id"]
    telegram = destination(db, account | {"secret_id": telegram_secret}, CHANNEL + "2")
    eitaa = expect(
        send(
            account,
            "POST",
            "/destinations",
            {
                "platform": "EITAA",
                "canonical_chat_id": "@stock_test",
                "display_name": "Eitaa",
                "secret_id": eitaa_secret,
            },
        ),
        201,
    )["id"]
    with psycopg.connect(db) as conn:
        conn.execute(
            "UPDATE app.destinations SET publish_verified_at=clock_timestamp() WHERE id=%s",
            (eitaa,),
        )
    expect(send(account, "PATCH", f"/destinations/{eitaa}", {"is_enabled": True}), 200)
    draft = expect(
        send(
            account,
            "POST",
            "/drafts",
            {
                "body_text": "مجموعه طبیعت",
                "media_asset_ids": assets,
            },
        ),
        201,
    )["id"]
    expect(send(account, "DELETE", f"/media/assets/{assets[0]}"), 409)
    receipt = expect(
        send(
            account,
            "POST",
            f"/drafts/{draft}/publish",
            {
                "mode": "LIVE_PUBLISH",
                "destination_ids": [telegram, eitaa],
                "expected_draft_revision_no": 1,
                "explicit_user_confirmation": True,
                "album_fallback_policy": "SEQUENTIAL_POSTS",
            },
            **key(),
        ),
        202,
    )
    assert len(receipt["delivery_task_ids"]) == 2
    calls = []

    def call(self, token, method, fields, *, files=None):
        calls.append((method, fields, files))
        return (
            [{"message_id": i} for i in (1, 2, 3)]
            if method == "sendMediaGroup"
            else {"message_id": len(calls)}
        )

    monkeypatch.setattr(TelegramBotAdapter, "_call", call)
    worker = Dispatcher(
        worker_url(db),
        "m5-dispatcher",
        settings.VAULT,
        media_storage=storage,
        connectors={"TELEGRAM": TelegramBotAdapter, "EITAA": EitaayarAdapter},
    )
    for _ in range(2):
        assert worker.process_one()
    assert [item[0] for item in calls].count("sendMediaGroup") == 1
    docs = [item for item in calls if item[0] == "sendDocument"]
    assert len(docs) == 3
    assert [
        Image.open(io.BytesIO(item[2]["document"][1])).getpixel((0, 0))[0] for item in docs
    ] == [10, 20, 30]
