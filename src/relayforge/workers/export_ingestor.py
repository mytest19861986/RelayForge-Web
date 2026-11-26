"""Run the Telegram Desktop export worker with a relayforge_worker DSN."""

import argparse
import codecs
import logging
import os
import re
import resource
import time
from contextlib import closing
from pathlib import Path

import psycopg
from ijson.backends import yajl2_c as ijson
from ijson.common import JSONError
from psycopg.rows import dict_row

from relayforge.adapters.content_store import advance_cursor, desktop_message, insert_messages
from relayforge.adapters.s3_storage import MAX_EXPORT_BYTES, S3Storage, StorageError, export_key

MAX_JSON_DEPTH = 32
MAX_TOKEN_BYTES = 262_144
CHUNK_BYTES = 65_536
MAX_ITEM_EVENTS = 4096
MAX_ITEM_BYTES = 1_048_576
BATCH_BYTES = 1_048_576
MAGIC = (b"\x1f\x8b", b"PK\x03\x04", b"BZh", b"\xfd7zXZ\x00", b"\x28\xb5\x2f\xfd",
         b"PK\x05\x06", b"PK\x07\x08")
_OUTSIDE = re.compile(rb'[ \t\r\n,:]+|[\[\]{}"]|[^ \t\r\n,:\[\]{}"]+')
_INSIDE = re.compile(rb'[^"\\]+|["\\]')
LOG = logging.getLogger(__name__)


class HostileExportStreamError(ValueError):
    """A stable, safe code describing an invalid export."""


class HostileJsonStreamGuard:
    def __init__(self, chunks, *, max_bytes=MAX_EXPORT_BYTES):
        self._chunks = iter(chunks)
        self._max_bytes = max_bytes
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self.total_bytes = self._depth = self._token = 0
        self._in_string = self._escaped = False
        self._stream = self._verified()
        self._pending = b""

    def read(self, size):
        # ijson probes read(0); thereafter satisfy bounded reads across arbitrary fragmentation.
        if size == 0:
            return b""
        if size < 0 or size > CHUNK_BYTES:
            raise ValueError("Export ingestion must use bounded reads")
        output = bytearray()
        while len(output) < size:
            if not self._pending:
                self._pending = next(self._stream, b"")
                if not self._pending:
                    break
            available = size - len(output)
            output.extend(self._pending[:available])
            self._pending = self._pending[available:]
        return bytes(output)

    def _utf8(self, chunk, *, final=False):
        try:
            self._decoder.decode(chunk, final=final)
        except UnicodeDecodeError:
            raise HostileExportStreamError("COMPRESSED_OR_BINARY_UPLOAD_FORBIDDEN") from None
        if b"\x00" in chunk:
            raise HostileExportStreamError("COMPRESSED_OR_BINARY_UPLOAD_FORBIDDEN")

    def _verified(self):
        prefix = bytearray()
        started = False
        for chunk in self._chunks:
            if not chunk:
                continue
            self.total_bytes += len(chunk)
            if self.total_bytes > self._max_bytes:
                raise HostileExportStreamError("EXPORT_MAX_BYTES_EXCEEDED")
            if not started:
                needed = 8 - len(prefix)
                prefix.extend(chunk[:needed])
                if len(prefix) < 8:
                    continue
                if prefix.startswith(MAGIC):
                    raise HostileExportStreamError("COMPRESSED_OR_BINARY_UPLOAD_FORBIDDEN")
                chunk = bytes(prefix) + chunk[needed:]
                started = True
            self._utf8(chunk)
            self._scan(chunk)
            yield chunk
        if not started and prefix:
            if prefix.startswith(MAGIC):
                raise HostileExportStreamError("COMPRESSED_OR_BINARY_UPLOAD_FORBIDDEN")
            self._utf8(prefix)
            self._scan(prefix)
            yield bytes(prefix)
        self._utf8(b"", final=True)

    def _count(self, length):
        self._token += length
        if self._token > MAX_TOKEN_BYTES:
            raise HostileExportStreamError("JSON_MAX_TOKEN_BYTES_EXCEEDED")

    def _scan(self, chunk):
        position = 0
        while position < len(chunk):
            if self._in_string and self._escaped:
                self._count(1)
                self._escaped = False
                position += 1
                continue
            matcher = _INSIDE if self._in_string else _OUTSIDE
            match = matcher.match(chunk, position)
            start, position = match.span()
            char = chunk[start]
            if self._in_string:
                if char == 34:
                    self._in_string = False
                    self._token = 0
                else:
                    self._count(position - start)
                    self._escaped = char == 92
            elif char in b'{}[]" \r\n\t,:':
                self._token = 0
                self._in_string = char == 34
                self._depth += (char in b"{[") - (char in b"}]")
                if self._depth < 0:
                    raise HostileExportStreamError("MALFORMED_EXPORT_JSON")
                if self._depth > MAX_JSON_DEPTH:
                    raise HostileExportStreamError("JSON_MAX_DEPTH_EXCEEDED")
            else:
                self._count(position - start)


def guarded_message_events(raw, channel):
    """Bound the memory of each materialized message, including many tiny tokens."""
    found, in_messages = False, False
    depth = events = size = 0
    root_key = None
    for event, value in ijson.basic_parse(raw, buf_size=CHUNK_BYTES):
        if not depth and event != "start_map":
            raise HostileExportStreamError("MALFORMED_EXPORT_JSON")
        if depth == 1 and event == "map_key":
            root_key = value
        elif depth == 1 and root_key == "messages":
            if found or event != "start_array":
                raise HostileExportStreamError("MALFORMED_EXPORT_JSON")
            found = in_messages = True
        elif depth == 1 and root_key == "id" and event in {"number", "string"}:
            if str(value) not in {channel, channel.removeprefix("-100")}:
                raise HostileExportStreamError("EXPORT_CHANNEL_MISMATCH")
        if isinstance(value, str) and "\x00" in value:
            raise HostileExportStreamError("COMPRESSED_OR_BINARY_UPLOAD_FORBIDDEN")
        if in_messages and depth == 2:
            if event == "end_array":
                in_messages = False
            else:
                events = size = 0
        prefix = ""
        if in_messages and depth >= 2:
            events += 1
            size += len(value.encode()) if isinstance(value, str) else 0
            if events > MAX_ITEM_EVENTS or size > MAX_ITEM_BYTES:
                raise HostileExportStreamError("JSON_MESSAGE_LIMIT_EXCEEDED")
            prefix = "messages.item" if depth == 2 else "messages.item.child"
        if depth == 1 and event != "map_key":
            root_key = None
        depth += event in {"start_map", "start_array"}
        depth -= event in {"end_map", "end_array"}
        yield prefix, event, value
    if not found:
        raise HostileExportStreamError("MALFORMED_EXPORT_JSON")


def rss_mib():
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


class S3ExportStreamIngestor:
    def __init__(self, dsn, storage):
        self.dsn, self.storage = dsn, storage

    @staticmethod
    def _set_tenant(conn, tenant_id):
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))

    @staticmethod
    def _check_key(job):
        if job["s3_object_key"] != export_key(job["tenant_id"], job["job_id"]):
            raise HostileExportStreamError("EXPORT_OBJECT_KEY_MISMATCH")

    def _parse(self, conn, job):
        source = conn.execute("""
            SELECT tenant_id, workspace_id, id AS source_id, canonical_channel_id
            FROM app.sources WHERE id = %s AND workspace_id = %s AND is_active
              AND source_kind = 'TELEGRAM_DESKTOP_EXPORT'
        """, (job["source_id"], job["workspace_id"])).fetchone()
        active = conn.execute("SELECT id FROM app.tenants WHERE status = 'ACTIVE'").fetchone()
        if source is None or active is None:
            raise HostileExportStreamError("EXPORT_SOURCE_INACTIVE")
        archive = job["job_mode"] == "ARCHIVE_INGEST"
        origin = "EXPORT_ARCHIVE" if archive else "ANALYSIS_SAMPLE_ONLY"
        scope = "INGEST_PIPELINE" if archive else "ANALYSIS_ISOLATED"
        total = inserted = skipped = size = 0
        baseline = peak = rss_mib()
        last, batch = None, []
        chunks = self.storage.stream_object_chunks(job["s3_object_key"], CHUNK_BYTES,
                                                  etag=job["s3_object_etag"])
        with closing(chunks), conn.cursor() as cursor:
            guard = HostileJsonStreamGuard(chunks)
            events = guarded_message_events(guard, source["canonical_channel_id"])
            for item in ijson.items(events, "messages.item"):
                total += 1
                try:
                    message = desktop_message(item)
                except (UnicodeError, ValueError):
                    message = None
                if message is None:
                    skipped += 1
                    continue
                batch.append(message)
                size += len(message.text.plain_text.encode()) + sum(
                    len((span.url or "").encode()) + 256 for span in message.text.spans_utf16
                )
                if last is None or int(message.external_id) > int(last.external_id):
                    last = message
                if len(batch) >= 256 or size >= BATCH_BYTES:
                    peak = max(peak, rss_mib())
                    inserted += insert_messages(cursor, source, origin, scope, batch)
                    batch.clear()
                    size = 0
            if guard.total_bytes != job["file_size_bytes"]:
                raise HostileExportStreamError("EXPORT_OBJECT_SIZE_MISMATCH")
            if batch:
                inserted += insert_messages(cursor, source, origin, scope, batch)
            if archive and last:
                advance_cursor(cursor, source, last.external_id, last.published_at)
        peak = max(peak, rss_mib(), resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
        return {"messages_total": total, "messages_imported": inserted,
                "messages_skipped": skipped, "baseline_rss_mib": baseline, "peak_rss_mib": peak,
                "delta_rss_mib": peak - baseline}

    def _delete(self, conn, job):
        self._check_key(job)
        self._set_tenant(conn, job["tenant_id"])
        self.storage.delete_object(job["s3_object_key"])
        conn.execute("""
            UPDATE app.export_import_jobs SET raw_deleted_at = clock_timestamp(),
                status = CASE WHEN status = 'UPLOADING' THEN 'EXPIRED_CLEANED' ELSE status END
            WHERE id = %s AND raw_deleted_at IS NULL
        """, (job["job_id"],))

    def _parse_job(self, conn, job):
        self._set_tenant(conn, job["tenant_id"])
        conn.execute("SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 1))",
                     (str(job["tenant_id"]),))
        try:
            with conn.transaction():
                self._check_key(job)
                result = self._parse(conn, job)
        except (
            HostileExportStreamError, StorageError, JSONError, ValueError, OverflowError,
            psycopg.DataError,
        ) as exc:
            if isinstance(exc, HostileExportStreamError):
                code = str(exc)
            elif isinstance(exc, StorageError):
                code = "EXPORT_OBJECT_READ_FAILED"
            else:
                code = "MALFORMED_EXPORT_JSON"
            result = {"messages_total": 0, "messages_imported": 0, "messages_skipped": 0,
                      "error_code": code, "status": "FAILED"}
        else:
            result.update(status="COMPLETED", error_code=None)
        conn.execute("""
            UPDATE app.export_import_jobs SET status = %(status)s, error_code = %(error_code)s,
                messages_total = %(messages_total)s, messages_imported = %(messages_imported)s,
                messages_skipped = %(messages_skipped)s, completed_at = clock_timestamp()
            WHERE id = %(job_id)s
        """, result | {"job_id": job["job_id"]})
        return result

    def run_once(self):
        with psycopg.connect(
            self.dsn, autocommit=True, prepare_threshold=None, row_factory=dict_row,
        ) as conn:
            if conn.info.user != "relayforge_worker":
                raise ValueError("Export ingestion requires relayforge_worker")
            job = None
            try:
                with conn.transaction():
                    job = conn.execute("SELECT * FROM app.fn_worker_claim_export_job()").fetchone()
                    if job is not None:
                        result = self._parse_job(conn, job)
            finally:
                if job is not None:
                    # Only delete a terminal job. A rolled-back parse remains available for retry.
                    with conn.transaction():
                        self._set_tenant(conn, job["tenant_id"])
                        terminal = conn.execute("""
                            SELECT id FROM app.export_import_jobs
                            WHERE id = %s AND status IN ('COMPLETED', 'FAILED')
                              AND raw_deleted_at IS NULL FOR UPDATE
                        """, (job["job_id"],)).fetchone()
                        if terminal and job["s3_object_key"] == export_key(
                            job["tenant_id"], job["job_id"],
                        ):
                            self._delete(conn, job)
            if job is not None:
                return result
            with conn.transaction():
                cleanup = conn.execute("SELECT * FROM app.fn_worker_claim_export_cleanup()")
                if job := cleanup.fetchone():
                    self._delete(conn, job)
                    return {"status": "RAW_DELETED"}
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    storage = S3Storage.from_environment()
    storage.ensure_exports_lifecycle_24h()
    ingestor = S3ExportStreamIngestor(os.environ["DATABASE_WORKER_URL"], storage)
    while True:
        try:
            result = ingestor.run_once()
        except (StorageError, psycopg.Error):
            if args.once:
                raise SystemExit("Export worker failed") from None
            LOG.warning("Export worker will retry after a storage or database failure")
            time.sleep(5)
            continue
        if result:
            LOG.info("Export worker result: %s", result)
        if args.once:
            return
        if not result:
            time.sleep(1)


if __name__ == "__main__":
    main()
