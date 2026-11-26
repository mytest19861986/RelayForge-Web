"""Lease-fenced dispatcher; network calls start after the in-flight transaction commits."""

import argparse
import logging
import os
import random
import time
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
from cryptography.exceptions import InvalidTag
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from relayforge.adapters.messengers import CONNECTORS, MessengerError
from relayforge.adapters.net_guard import OutboundTransportError
from relayforge.adapters.publications import outbox
from relayforge.adapters.vault import EnvelopeSecretVault
from relayforge.core.security import SecretRevokedError, TenantKeyShreddedError

LOG = logging.getLogger(__name__)


def tenant_context(conn, tenant):
    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant),))



class Dispatcher:
    def __init__(self, dsn, worker_id, vault, *, ttl=30, connectors=None, hook=None):
        self.dsn, self.worker_id, self.vault = dsn, worker_id, vault
        self.ttl = min(600, max(15, ttl))
        self.connectors = CONNECTORS if connectors is None else connectors
        self.hook = hook

    def _connect(self):
        conn = psycopg.connect(
            self.dsn, autocommit=True, prepare_threshold=None, row_factory=dict_row,
            connect_timeout=5,
        )
        if conn.info.user != "relayforge_worker":
            conn.close()
            raise ValueError("Dispatcher requires relayforge_worker")
        return conn

    def _checkpoint(self, name, task):
        if self.hook is not None:
            self.hook(name, task)

    @staticmethod
    def _transition(conn, task, status, remote=None, retry_at=None):
        conn.execute("SELECT app.fn_worker_transition_task(%s, %s, %s, %s, %s)",
                     (task["id"], task["lease_epoch"], status, remote, retry_at))

    def _finish(self, conn, task, status, outcome, *, code=None, delay=None, remote=None,
                retry_at=None):
        conn.execute("SELECT set_config('app.lease_epoch', %s, true)",
                     (str(task["lease_epoch"]),))
        # The fenced error metadata update happens before transitioning out of the leased state.
        conn.execute("""
            UPDATE app.delivery_tasks SET last_error_class = %s, last_error_safe = %s
            WHERE id = %s AND lease_epoch = %s
        """, (code, code, task["id"], task["lease_epoch"]))
        self._transition(conn, task, status, remote, retry_at)
        conn.execute("""
            INSERT INTO app.delivery_attempts (id, tenant_id, delivery_task_id, attempt_no,
                lease_epoch, outcome, retry_after_sec, remote_message_id, error_safe,
                started_at, finished_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp())
        """, (uuid4(), task["tenant_id"], task["id"], task["attempts"], task["lease_epoch"],
              outcome, delay, remote, code, task["updated_at"]))
        event = {
            "SENT": "delivery.sent", "FAILED": "delivery.failed",
            "UNCERTAIN": "delivery.marked_uncertain",
        }.get(status)
        if event:
            outbox(conn, task["tenant_id"], task["workspace_id"], event,
                   task["payload_json"].get("correlation_id", str(task["id"])), {
                       "delivery_task_id": str(task["id"]), "status": status,
                       "lease_epoch": task["lease_epoch"],
                   })

    def _catch_up(self, conn, task, delivery):
        policy = delivery.get("catch_up")
        now = datetime.now(UTC)
        # A normal polling delay is not a downtime backlog. Retries already have their own policy.
        if not policy or task["attempts"] != 1 or (
            now - task["scheduled_for"] <= timedelta(seconds=60)
        ):
            return False
        mode = policy["mode"]
        if mode == "GRADUAL_THROTTLED" and task["attempts"] < task["max_attempts"]:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 7))",
                         (str(task["route_id"]),))
            previous = conn.execute("""
                SELECT max(next_retry_at) AS latest FROM app.delivery_tasks WHERE route_id = %s
                  AND status = 'RETRY_WAIT' AND last_error_class = 'CATCH_UP_THROTTLED'
            """, (task["route_id"],)).fetchone()["latest"]
            spacing = timedelta(seconds=policy["spacing_sec"])
            self._finish(conn, task, "RETRY_WAIT", "TRANSIENT_ERROR", code="CATCH_UP_THROTTLED",
                         retry_at=max(previous or now, now) + spacing)
            return True

        if mode == "HOLD_FOR_REVIEW":
            draft = uuid5(NAMESPACE_URL, f"relayforge:catch-up:{task['id']}")
            snapshot = task["frozen_payload_json"]
            conn.execute("""
                INSERT INTO app.drafts
                    (id, tenant_id, workspace_id, route_id, route_policy_revision_no,
                     source_content_item_id, approval_state)
                VALUES (%s,%s,%s,%s,%s,%s,'NEEDS_REVIEW')
            """, (draft, task["tenant_id"], task["workspace_id"], task["route_id"],
                  task["route_policy_revision_no"], snapshot["source_content_item_id"]))
            conn.execute("""
                INSERT INTO app.draft_revisions (id, tenant_id, draft_id, revision_no,
                    body_text, entities_ast_json)
                VALUES (%s,%s,%s,1,%s,%s)
            """, (uuid4(), task["tenant_id"], draft, delivery["draft_text"],
                  Jsonb(delivery["draft_spans"])))
            outbox(conn, task["tenant_id"], task["workspace_id"], "draft.saved",
                   task["payload_json"].get("correlation_id", str(task["id"])),
                   {"draft_id": str(draft), "revision_no": 1, "reason": "CATCH_UP_HELD_FOR_REVIEW"})
        code = (
            "CATCH_UP_HELD_FOR_REVIEW" if mode == "HOLD_FOR_REVIEW" else "CATCH_UP_SKIPPED_EXPIRED"
        )
        self._finish(conn, task, "FAILED", "PERMANENT_ERROR", code=code)
        return True

    def _prepare(self, conn, claimed):
        tenant_context(conn, claimed["claimed_tenant_id"])
        conn.execute("SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 1))",
                     (str(claimed["claimed_tenant_id"]),))
        workspace = conn.execute(
            "SELECT workspace_id FROM app.delivery_tasks WHERE id = %s",
            (claimed["claimed_task_id"],),
        ).fetchone()
        if not workspace:
            return None
        conn.execute("SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 6))",
                     (str(workspace["workspace_id"]),))
        task = conn.execute("""
            SELECT t.*, w.emergency_stop, d.is_enabled, d.is_archived, d.secret_id,
                   p.frozen_payload_json, p.route_policy_revision_no
            FROM app.delivery_tasks t JOIN app.workspaces w ON w.id = t.workspace_id
            LEFT JOIN app.destinations d ON d.id = t.destination_id AND d.workspace_id = w.id
            LEFT JOIN app.publication_versions p ON p.id = t.publication_version_id
            WHERE t.id = %s AND t.status = 'RESERVED' AND t.lease_epoch = %s FOR UPDATE OF t
        """, (claimed["claimed_task_id"], claimed["claimed_epoch"])).fetchone()
        if task is None:
            return None
        if task["emergency_stop"] or not task["is_enabled"] or task["is_archived"]:
            if task["attempts"] < task["max_attempts"]:
                self._finish(conn, task, "RETRY_WAIT", "TRANSIENT_ERROR",
                             code="DESTINATION_PAUSED", retry_at=datetime.now(UTC))
            else:
                self._finish(conn, task, "FAILED", "PERMANENT_ERROR", code="DESTINATION_PAUSED")
            return None
        active = conn.execute("SELECT id FROM app.tenants WHERE status = 'ACTIVE'").fetchone()
        delivery = (task["frozen_payload_json"] or {}).get("destinations", {}).get(
            str(task["destination_id"]),
        )
        source_item = (task["frozen_payload_json"] or {}).get("source_content_item_id")
        if source_item and not conn.execute("""
            SELECT id FROM app.content_items WHERE id = %s AND workspace_id = %s
              AND access_class = 'PROCESSABLE' AND NOT is_deleted_upstream
              AND operation_scope = 'INGEST_PIPELINE'
        """, (source_item, task["workspace_id"])).fetchone():
            self._finish(conn, task, "FAILED", "PERMANENT_ERROR",
                         code="SOURCE_CONTENT_NOT_PUBLISHABLE")
            return None
        if active and delivery and self._catch_up(conn, task, delivery):
            return None
        secret = conn.execute("""
            SELECT s.tenant_id, s.workspace_id, s.id AS secret_id, s.secret_kind AS kind,
                   s.nonce_bytes AS nonce, s.ciphertext_aead AS ciphertext,
                   k.key_version, k.wrapped_dek_ciphertext AS wrapped_dek, k.key_state, s.is_revoked
            FROM app.secrets_vault s JOIN app.tenant_encryption_keys k
              ON k.tenant_id = s.tenant_id AND k.key_version = s.dek_version
            WHERE s.id = %s AND s.workspace_id = %s
        """, (task["secret_id"], task["workspace_id"])).fetchone()
        if not active or not delivery or not secret or delivery["platform"] not in self.connectors:
            self._finish(conn, task, "FAILED", "PERMANENT_ERROR", code="DESTINATION_UNAVAILABLE")
            return None
        try:
            token = self.vault.decrypt_secret(**secret)
        except (SecretRevokedError, TenantKeyShreddedError, InvalidTag, ValueError, TypeError):
            self._finish(conn, task, "FAILED", "PERMANENT_ERROR", code="AUTH_REVOKED")
            return None
        self._transition(conn, task, "DISPATCHING_IN_FLIGHT")
        return task, token, delivery

    def _heartbeat(self, task, finished):
        while not finished.wait(self.ttl / 3):
            try:
                with self._connect() as conn, conn.transaction():
                    tenant_context(conn, task["tenant_id"])
                    conn.execute("SELECT app.fn_worker_heartbeat_lease(%s, %s, %s)",
                                 (task["id"], task["lease_epoch"], self.ttl))
            except psycopg.Error as error:
                if error.sqlstate in {"RF014", "RF015"}:
                    return
                LOG.warning("Delivery heartbeat failed")

    def _send(self, task, token, delivery):
        try:
            receipt = self.connectors[delivery["platform"]]().send_message(
                token, delivery["chat_id"], delivery["payload"],
            )
        except OutboundTransportError:
            return "UNCERTAIN", "TIMEOUT_UNCERTAIN", {"code": "OUTBOUND_CONFIRMATION_MISSING"}
        except MessengerError as error:
            code = error.category
            if error.delivery_uncertain:
                return "UNCERTAIN", "TIMEOUT_UNCERTAIN", {"code": "OUTBOUND_CONFIRMATION_MISSING"}
            if code in {"RATE_LIMIT_429", "TRANSIENT_NETWORK"} and (
                task["attempts"] < task["max_attempts"]
            ):
                backoff = 2 ** min(task["attempts"], 10) + random.uniform(0, 1)
                delay = error.retry_after_sec
                retry_at = datetime.now(UTC) + timedelta(seconds=max(backoff, delay or 0))
                return "RETRY_WAIT", "TRANSIENT_ERROR", {
                    "code": code, "delay": delay, "retry_at": retry_at,
                }
            outcome = "QUOTA_EXHAUSTED" if code == "QUOTA_EXHAUSTED" else "PERMANENT_ERROR"
            return "FAILED", outcome, {"code": code}
        return "SENT", "SENT", {"remote": receipt.remote_message_id}

    def process_one(self, *, reap=True):
        with self._connect() as conn:
            if reap:
                conn.execute("SELECT app.fn_reap_expired_delivery_leases(%s)", (100,))
            with conn.transaction():
                claim = conn.execute("SELECT * FROM app.fn_worker_claim_next_jobs(%s, %s, %s)",
                                     (self.worker_id, 1, self.ttl)).fetchone()
            if not claim:
                return False
            self._checkpoint("reserved", claim)
            try:
                with conn.transaction():
                    prepared = self._prepare(conn, claim)
                if not prepared:
                    return True
                task, token, delivery = prepared
                self._checkpoint("in_flight", claim)
                # A paused process can resume after another worker has reaped its lease.
                with conn.transaction():
                    tenant_context(conn, task["tenant_id"])
                    self._transition(conn, task, "DISPATCHING_IN_FLIGHT")
                finished = Event()
                timer = Thread(target=self._heartbeat, args=(task, finished), daemon=True)
                timer.start()
                try:
                    status, outcome, fields = self._send(task, token, delivery)
                    self._checkpoint("response", claim)
                    with conn.transaction():
                        tenant_context(conn, task["tenant_id"])
                        self._finish(conn, task, status, outcome, **fields)
                finally:
                    finished.set()
                    timer.join()
            except psycopg.Error as error:
                if error.sqlstate not in {"RF014", "RF015"}:
                    raise
                LOG.info("Delivery transition fenced (%s)", error.sqlstate)
            return True


def main(dispatcher_type=Dispatcher):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--lease-ttl-sec", type=int, default=30)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    dispatcher = dispatcher_type(
        os.environ["DATABASE_WORKER_URL"], args.worker_id, EnvelopeSecretVault.from_environment(),
        ttl=args.lease_ttl_sec,
    )
    while True:
        try:
            processed = dispatcher.process_one()
        except psycopg.Error:
            LOG.warning("Dispatcher database operation failed")
            if args.once:
                raise SystemExit(1) from None
            processed = False
        if args.once:
            return
        if not processed:
            time.sleep(1)


if __name__ == "__main__":
    main()
