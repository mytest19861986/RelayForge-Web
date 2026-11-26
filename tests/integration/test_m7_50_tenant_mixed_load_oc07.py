import hashlib
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from uuid import uuid4

import pytest
from django.conf import settings
from PIL import Image
from test_m1_auth_rls_vault_and_shredding import expect, login, provision
from test_m3_publishing_and_real_sigkill import CHANNEL, destination, publish
from test_m4_ai_gateway_channel_and_learning import send
from test_m5_media_worker_podcast_and_auto_routes import daemon as daemon
from test_m5_media_worker_podcast_and_auto_routes import worker_url

from relayforge.adapters.messengers import PublishReceipt
from relayforge.workers.dispatcher import Dispatcher


@pytest.mark.usefixtures("db")
def test_50_tenant_mixed_load(db, daemon):
    staging, rpc = daemon
    tenants = []
    for index in range(50):
        name = f"load-{index:02d}"
        provision(name)
        account = login(name, REMOTE_ADDR=f"192.0.2.{index + 1}")
        secret = expect(
            send(
                account,
                "POST",
                "/security/secrets",
                {
                    "secret_kind": "TELEGRAM_BOT_TOKEN",
                    "label": "Load fixture",
                    "raw_secret": "12345:load-test-fixture",
                },
            ),
            201,
        )["id"]
        channel = f"{CHANNEL}{index:02d}"
        target = destination(db, account | {"secret_id": secret}, channel)
        source = expect(
            send(
                account,
                "POST",
                "/sources",
                {
                    "source_kind": "WEBHOOK_INBOUND",
                    "canonical_channel_id": CHANNEL,
                    "display_title": "Load source",
                    "webhook_platform": "telegram",
                },
            ),
            201,
        )["id"]
        expect(
            send(
                account,
                "POST",
                "/routes",
                {
                    "slug": "load-route",
                    "display_name": "Load route",
                    "source_ids": [source],
                    "destination_ids": [target],
                },
            ),
            201,
        )
        _, receipt, _, _ = publish(account, [target], text=f"گزارش آزمایشی {index}")
        tenants.append((account, receipt["delivery_task_ids"][0]))

    seen = set()
    seen_lock = Lock()
    dsn = worker_url(db)

    class Messenger:
        def send_message(self, _token, chat, _payload):
            with seen_lock:
                assert chat not in seen
                seen.add(chat)
            return PublishReceipt(chat)

    def dispatch(index):
        worker = Dispatcher(
            dsn, f"load-{index}", settings.VAULT, connectors={"TELEGRAM": Messenger}
        )
        while worker.process_one(reap=False):
            pass

    def web(entry):
        account, task_id = entry
        samples = []
        for endpoint in ("/dashboard", "/events/poll", "/deliveries"):
            start = time.perf_counter()
            response = send(account, "GET", endpoint)
            samples.append((time.perf_counter() - start) * 1000)
            data = expect(response, 200)
            if endpoint == "/deliveries":
                assert [item["id"] for item in data["items"]] == [task_id]
            if endpoint == "/events/poll":
                assert all(item["tenant_id"] == account["tenant_id"] for item in data["items"])
        return samples

    def render(index):
        identifier = str(uuid4())
        directory = staging / identifier
        directory.mkdir()
        if index % 2:
            Image.new("RGB", (960, 540), "blue").save(directory / "slide.png")
            result = rpc(
                "compose_clip_ffmpeg",
                job_id=identifier,
                spec={"sources": ["slide.png"] * 3, "caption": "آزمون بار"},
            )
        else:
            result = rpc(
                "render_infographic",
                job_id=identifier,
                spec={
                    "title": "آزمون بار",
                    "rows": [["مقدار", "۱۲۳۴۵"]] * 12,
                },
            )
        assert result["status"] == "SUCCEEDED", result
        return time.perf_counter()

    with (
        ThreadPoolExecutor(max_workers=2) as media_pool,
        ThreadPoolExecutor(max_workers=3) as dispatch_pool,
        ThreadPoolExecutor(max_workers=5) as web_pool,
    ):
        start = time.perf_counter()
        rendering = [media_pool.submit(render, index) for index in range(10)]
        dispatching = [dispatch_pool.submit(dispatch, index) for index in range(3)]
        timings = [sample for batch in web_pool.map(web, tenants) for sample in batch]
        for task in dispatching:
            task.result()
        end = time.perf_counter()
        overlap = sum(start <= task.result() <= end for task in rendering)
    assert len(seen) == 50
    assert overlap >= 2
    timings.sort()

    def percentile(fraction):
        return round(timings[min(len(timings) - 1, int(len(timings) * fraction))], 3)

    path = Path("tmp/m7-oc07-load-evidence.json")
    path.parent.mkdir(exist_ok=True)
    schema = Path("RelayForge_Architecture/contracts/database_schema.sql")
    evidence = {
        "git_commit_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "sql_sha256": hashlib.sha256(schema.read_text().encode()).hexdigest(),
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "tenant_count": 50,
        "total_web_requests": len(timings),
        "dispatched_tasks": len(seen),
        "completed_media_renders": overlap,
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "pass_threshold_ms": 500.0,
        "errors_5xx": 0,
        "cross_tenant_leaks": 0,
        "duplicate_deliveries": 0,
    }
    path.write_text(json.dumps(evidence, indent=2))
    assert evidence["p95_ms"] <= 500 and evidence["p99_ms"] <= 500, evidence
