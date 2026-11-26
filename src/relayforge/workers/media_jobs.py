"""Database-backed media orchestration outside the isolated renderer."""

import argparse
import base64
import logging
import os
import shutil
import time
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from relayforge.adapters.ai_gateway import AIError, adapter
from relayforge.adapters.ai_media import image, poll_video, speech, start_video
from relayforge.adapters.media_rpc import media_rpc
from relayforge.adapters.media_storage import MIMES, put_media, read_media
from relayforge.adapters.s3_storage import S3Storage, StorageError
from relayforge.adapters.vault import EnvelopeSecretVault
from relayforge.core.intelligence import digest
from relayforge.core.media_validation import media_format

LOG = logging.getLogger(__name__)
TERMINAL = {"TIMED_OUT", "OOM_KILLED", "CANCELLED"}
ESTIMATE = 600


class MediaJobs:
    def __init__(self, dsn, storage, staging, vault=None):
        self.dsn, self.storage, self.vault = dsn, storage, vault
        self.staging = Path(staging).resolve(strict=True)

    @staticmethod
    def tenant(conn, job):
        conn.execute("SELECT set_config('app.tenant_id',%s,true)", (str(job["tenant_id"]),))

    @staticmethod
    def active(conn, job):
        row = conn.execute(
            "SELECT status FROM app.media_jobs WHERE id=%s",
            (job["job_id"],),
        ).fetchone()
        if not row or row["status"] != "RUNNING":
            raise ValueError("CANCELLED")

    def stage_asset(self, conn, job, identifier, path):
        with conn.transaction():
            self.tenant(conn, job)
            row = conn.execute(
                """
                SELECT s3_object_key FROM app.media_assets
                WHERE workspace_id=%s AND id=%s AND NOT is_deleted
                """,
                (job["workspace_id"], identifier),
            ).fetchone()
        if not row:
            raise ValueError("ASSET_NOT_FOUND")
        path.write_bytes(read_media(self.storage, row["s3_object_key"]))
        return path.name

    def local_render(self, conn, job, root):
        spec = job["input_spec_json"]
        kind = spec["job_kind"]
        if kind == "INFOGRAPHIC_RENDER":
            with conn.transaction():
                self.tenant(conn, job)
                brand = conn.execute(
                    """
                    SELECT visual_tokens_json,ownership_attested FROM app.brand_identities
                    WHERE id=%s AND workspace_id=%s
                    """,
                    (spec.get("brand_identity_id"), job["workspace_id"]),
                ).fetchone()
            command = "render_infographic"
            render = {
                "title": spec["title"],
                "rows": spec["table_rows"],
                "visual_tokens": (brand or {}).get("visual_tokens_json", {}),
                "ownership_attested": (brand or {}).get("ownership_attested", False),
            }
            media_kind = "INFOGRAPHIC"
        elif kind == "LOCAL_SLIDESHOW_CLIP":
            command, media_kind = "compose_clip_ffmpeg", "VIDEO"
            render = {
                "sources": [
                    self.stage_asset(conn, job, asset, root / f"source-{position}")
                    for position, asset in enumerate(spec["asset_ids"])
                ],
                "caption": spec["caption"],
            }
            if asset := spec.get("audio_asset_id"):
                render["audio"] = self.stage_asset(conn, job, asset, root / "input-audio")
        else:
            if len(spec["asset_ids"]) != 1:
                raise ValueError("STICKER_SOURCE_REQUIRED")
            command, media_kind = "build_sticker", kind
            render = {
                "source": self.stage_asset(conn, job, spec["asset_ids"][0], root / "input-image"),
                "native": kind == "NATIVE_STICKER",
            }
        origin = "LOCAL_RENDERED_CLIP" if media_kind == "VIDEO" else "USER_UPLOAD"
        return self.render(conn, job, command, render, media_kind, origin)

    def render(self, conn, job, command, spec, kind, origin):
        with conn.transaction():
            self.tenant(conn, job)
            self.active(conn, job)
        reply = media_rpc(command, job_id=str(job["job_id"]), spec=spec)
        if reply["status"] != "SUCCEEDED":
            raise ValueError(reply["status"])
        return (
            base64.b64decode(reply["content_base64"], validate=True),
            kind,
            origin,
            reply.get("duration_sec"),
        )

    def provider(self, conn, job, modality):
        spec = job["input_spec_json"]
        with conn.transaction():
            self.tenant(conn, job)
            self.active(conn, job)
            row = conn.execute(
                """
                SELECT p.id,p.provider_type,p.base_url,s.tenant_id,s.workspace_id,s.id AS secret_id,
                   s.secret_kind AS kind,s.nonce_bytes AS nonce,s.ciphertext_aead AS ciphertext,
                   s.dek_version AS key_version,s.is_revoked,
                   k.wrapped_dek_ciphertext AS wrapped_dek,
                   k.key_state
                FROM app.ai_providers p JOIN app.secrets_vault s ON s.id=p.secret_id
                JOIN app.tenant_encryption_keys k ON k.tenant_id=s.tenant_id
                  AND k.key_version=s.dek_version
                JOIN app.workspace_settings w ON w.workspace_id=p.workspace_id
                  AND w.allow_external_ai
                JOIN app.external_consents c ON c.workspace_id=p.workspace_id AND c.is_granted
                WHERE p.id=%s AND p.workspace_id=%s AND p.is_enabled AND NOT s.is_revoked
                  AND c.provider_scope=(CASE p.provider_type WHEN 'GEMINI' THEN 'GEMINI_'
                                       ELSE 'OPENAI_' END)||%s
                """,
                (
                    spec["provider_id"],
                    job["workspace_id"],
                    "AUDIO" if modality == "TTS" else modality,
                ),
            ).fetchone()
        if not row:
            raise ValueError("EXTERNAL_AI_CONSENT_REQUIRED")
        fields = {k: v for k, v in row.items() if k not in {"id", "provider_type", "base_url"}}
        vault = self.vault or EnvelopeSecretVault.from_environment()
        return adapter(row, vault.decrypt_secret(**fields))

    def metered(self, conn, job, attempt):
        reservation = uuid4()
        with conn.transaction():
            self.tenant(conn, job)
            self.active(conn, job)
            conn.execute(
                "SELECT app.fn_budget_reserve(%s,%s,NULL,%s,%s,1800)",
                (reservation, job["workspace_id"], str(reservation), ESTIMATE),
            )
            conn.execute(
                "UPDATE app.media_jobs SET reservation_id=%s WHERE id=%s",
                (reservation, job["job_id"]),
            )
        result = error = None
        try:
            result = attempt()
        except AIError as exc:
            error = exc
        spent = result is not None or (error and error.spent)
        actual = result.cost_micros if result is not None else None
        accuracy = (
            "ACTUAL_PROVIDER" if actual is not None else ("ESTIMATED" if result else "UNKNOWN")
        )
        with conn.transaction():
            self.tenant(conn, job)
            try:
                with conn.transaction():
                    conn.execute(
                        "SELECT app.fn_budget_settle_with_accuracy(%s,%s,%s,%s)",
                        (
                            reservation,
                            "COMMITTED" if spent else "RELEASED",
                            (ESTIMATE if actual is None else actual) if spent else None,
                            accuracy,
                        ),
                    )
            except psycopg.Error as exc:
                if exc.sqlstate != "RF004":
                    raise
                conn.execute(
                    "SELECT app.fn_budget_settle_with_accuracy(%s,'COMMITTED',%s,'UNKNOWN')",
                    (reservation, ESTIMATE),
                )
                raise ValueError("QUOTA_ZERO") from None
        if error:
            raise error
        return result

    def podcast(self, conn, job, root, api):
        spec = job["input_spec_json"]
        script_id = spec["podcast_script_id"]
        with conn.transaction():
            self.tenant(conn, job)
            script = conn.execute(
                """
                SELECT segments_json,segment_cache_json FROM app.podcast_scripts
                WHERE id=%s AND workspace_id=%s AND status='SYNTHESIZING'
                """,
                (script_id, job["workspace_id"]),
            ).fetchone()
        if not script:
            raise ValueError("SCRIPT_APPROVAL_REQUIRED")
        cache = script["segment_cache_json"]
        retained = {}
        sources = []
        for index, segment in enumerate(script["segments_json"]):
            voice = segment["voice_id"]
            text = " ".join(segment["pronunciation"].split())
            key = digest([spec["provider_id"], spec["model_id"], voice, text])
            object_key = retained.get(key) or cache.get(key)
            if object_key:
                data = read_media(self.storage, object_key)
            else:
                response = self.metered(
                    conn,
                    job,
                    lambda text=text, voice=voice: speech(api, spec["model_id"], text, voice),
                )
                data = response.data
                object_key = f"tenants/{job['tenant_id']}/media/tts/{script_id}/{key}.wav"
                self.storage.put_object(object_key, data)
            retained[key] = object_key
            output = root / f"speech-{index}.wav"
            output.write_bytes(data)
            sources.append(output.name)
        with conn.transaction():
            self.tenant(conn, job)
            self.active(conn, job)
            conn.execute(
                "UPDATE app.podcast_scripts SET segment_cache_json=%s WHERE id=%s",
                (Jsonb(retained), script_id),
            )
        return self.render(
            conn,
            job,
            "assemble_podcast_audio",
            {"sources": sources, "output_format": spec["output_format"]},
            spec["output_format"],
            "TTS_AUDIO",
        )

    def produce(self, conn, job, root):
        spec = job["input_spec_json"]
        kind = spec["job_kind"]
        if kind == "TTS_SYNTHESIS":
            return self.podcast(conn, job, root, self.provider(conn, job, "TTS"))
        if kind == "AI_IMAGE_GEN":
            api = self.provider(conn, job, "IMAGE")
            source = None
            if spec["asset_ids"]:
                path = root / self.stage_asset(conn, job, spec["asset_ids"][0], root / "edit-input")
                data = path.read_bytes()
                file_kind, _ = media_format(data)
                if file_kind not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError("INVALID_IMAGE_EDIT_INPUT")
                source = MIMES[file_kind], data
            result = self.metered(
                conn,
                job,
                lambda: image(
                    api,
                    spec["model_id"],
                    spec["prompt"],
                    resolution=spec["resolution"],
                    source=source,
                ),
            )
            return (
                result.data,
                "IMAGE",
                ("AI_EDITED_IMAGE" if source else "AI_GENERATED_IMAGE"),
                None,
            )
        if kind == "AI_VIDEO_GEN_POLL":
            api = self.provider(conn, job, "VIDEO")
            result = self.metered(
                conn,
                job,
                lambda: start_video(
                    api, spec["model_id"], spec["prompt"], resolution=spec["resolution"]
                ),
            )
            with conn.transaction():
                self.tenant(conn, job)
                conn.execute(
                    "UPDATE app.media_jobs SET remote_operation_id=%s "
                    "WHERE id=%s AND status='RUNNING'",
                    (result.operation, job["job_id"]),
                )
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                with conn.transaction():
                    self.tenant(conn, job)
                    self.active(conn, job)
                if completed := poll_video(api, result.operation):
                    return completed.data, "VIDEO", "AI_GENERATIVE_VIDEO", None
                time.sleep(2)
            raise ValueError("TIMED_OUT")
        return self.local_render(conn, job, root)

    def run_once(self):
        with psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row) as conn:
            if conn.info.user != "relayforge_worker":
                raise ValueError("Media orchestration requires relayforge_worker")
            job = conn.execute("SELECT * FROM app.fn_worker_claim_media_job()").fetchone()
            if job is None:
                return None
            root = self.staging / str(job["job_id"])
            script = job["input_spec_json"].get("podcast_script_id")
            try:
                root.mkdir(mode=0o770 if os.getenv("RF_MEDIA_SHARED_GROUP") == "true" else 0o700)
                if os.getenv("RF_MEDIA_SHARED_GROUP") == "true":
                    root.chmod(0o2770)
                data, kind, origin, duration = self.produce(conn, job, root)
                with conn.transaction():
                    self.tenant(conn, job)
                    row = conn.execute(
                        "SELECT status FROM app.media_jobs WHERE id=%s FOR UPDATE", (job["job_id"],)
                    ).fetchone()
                    if row["status"] != "RUNNING":
                        return row["status"]
                    asset = put_media(
                        lambda sql, bindings: conn.execute(sql, bindings).fetchone(),
                        self.storage,
                        job["tenant_id"],
                        job["workspace_id"],
                        data,
                        kind=kind,
                        origin=origin,
                        duration=duration,
                    )
                    conn.execute(
                        """
                        UPDATE app.media_jobs SET status='SUCCEEDED',output_media_asset_id=%s,
                            completed_at=clock_timestamp() WHERE id=%s
                        """,
                        (asset, job["job_id"]),
                    )
                    if script:
                        conn.execute(
                            "UPDATE app.podcast_scripts SET status='READY', "
                            "synthesized_media_asset_id=%s WHERE id=%s",
                            (asset, script),
                        )
                return "SUCCEEDED"
            except (ValueError, OSError, StorageError, AIError, psycopg.Error) as error:
                if isinstance(error, AIError):
                    code = error.code
                elif isinstance(error, psycopg.Error):
                    code = error.sqlstate or "MEDIA_JOB_FAILED"
                else:
                    code = str(error)
                if not code.isascii() or not code.replace("_", "").isalnum() or not code.isupper():
                    code = "MEDIA_JOB_FAILED"
                status = code if code in TERMINAL else "FAILED"
                with conn.transaction():
                    self.tenant(conn, job)
                    changed = conn.execute(
                        """
                        UPDATE app.media_jobs
                          SET status=%s,error_safe=%s,completed_at=clock_timestamp()
                        WHERE id=%s AND status='RUNNING' RETURNING id
                        """,
                        (status, code, job["job_id"]),
                    ).fetchone()
                    if changed and script:
                        conn.execute(
                            "UPDATE app.podcast_scripts SET status='FAILED' WHERE id=%s", (script,)
                        )
                return status if changed else "CANCELLED"
            finally:
                if root.is_dir() and not root.is_symlink():
                    shutil.rmtree(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    worker = MediaJobs(
        os.environ["DATABASE_WORKER_URL"],
        S3Storage.from_environment(),
        os.environ["RF_MEDIA_STAGING_ROOT"],
    )
    while True:
        result = worker.run_once()
        if args.once:
            return
        if not result:
            time.sleep(1)


if __name__ == "__main__":
    main()
