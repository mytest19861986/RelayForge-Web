import io
import os
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from django.http import HttpResponse, JsonResponse
from ninja import File, Router, Schema, UploadedFile
from PIL import Image
from psycopg.types.json import Jsonb
from pydantic import Field

from relayforge.adapters.disk_pressure import disk_state
from relayforge.adapters.media_rpc import media_rpc
from relayforge.adapters.media_storage import put_media, read_media
from relayforge.adapters.net_guard import OutboundTransportError, SSRFBlockedError
from relayforge.adapters.s3_storage import StorageError
from relayforge.adapters.stock_media import PexelsStockAdapter
from relayforge.core.intelligence import digest
from relayforge.core.media_queries import pronunciation, suggest_query
from relayforge.core.media_validation import MAX_BYTES
from relayforge.web.ai_m4 import consent, provider
from relayforge.web.db import first, rows
from relayforge.web.db_tx import on_tenant_commit
from relayforge.web.endpoints import require_permission, require_workspace
from relayforge.web.problems import ProblemError
from relayforge.web.routes_m2 import get_idempotency_token, storage
from relayforge.web.ui_lists_m8 import listing

router = Router()
ROOT = "/workspaces/{wsId}"
ASSET = ROOT + "/media/assets"
JOB = ROOT + "/media/jobs"
SCRIPT = ROOT + "/podcast-scripts"


class MediaText(Schema):
    text: str = Field(min_length=1, max_length=16000)


class StockSearch(MediaText):
    media_kind: Literal["IMAGE", "VIDEO"] = "IMAGE"


class StockDownload(Schema):
    media_kind: Literal["IMAGE", "VIDEO"] = "IMAGE"
    stock_id: str = Field(pattern=r"^[0-9]{1,20}$")


class Segment(Schema):
    speaker: Literal["A", "B"]
    text: str = Field(min_length=1, max_length=3000)
    voice_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


class ScriptInput(Schema):
    speaker_mode: Literal["SINGLE_SPEAKER", "DUAL_SPEAKER"]
    target_duration_sec: int = Field(ge=15, le=3600)
    segments: list[Segment] = Field(min_length=1, max_length=48)
    approved: bool = False


class MediaJobInput(Schema):
    job_kind: Literal[
        "INFOGRAPHIC_RENDER",
        "STICKER_IMAGE",
        "NATIVE_STICKER",
        "LOCAL_SLIDESHOW_CLIP",
        "AI_IMAGE_GEN",
        "AI_VIDEO_GEN_POLL",
        "TTS_SYNTHESIS",
    ]
    asset_ids: list[UUID] = Field(default_factory=list, max_length=48)
    audio_asset_id: UUID | None = None
    brand_identity_id: UUID | None = None
    title: str = Field(default="", max_length=120)
    caption: str = Field(default="", max_length=120)
    table_rows: list[tuple[str, str]] = Field(default_factory=list, max_length=18)
    provider_id: UUID | None = None
    model_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,199}$")
    podcast_script_id: UUID | None = None
    resolution: Literal["1K", "2K", "4K"] = "1K"
    output_format: Literal["VOICE_NOTE", "AUDIO"] = "VOICE_NOTE"
    prompt: str = Field(default="", max_length=4000)


def authorize(request, workspace, action="edit_content"):
    require_permission(request, action)
    require_workspace(workspace)


@router.get(ASSET)
def list_assets(request, wsId: UUID, cursor: UUID | None = None, limit: int = 50):
    require_workspace(wsId)
    if not 1 <= limit <= 100:
        raise ProblemError(422)
    result = rows(
        """
        SELECT a.id,a.media_kind,a.origin_kind,a.mime_type,a.byte_size,a.width_px,a.height_px,
               a.duration_sec,a.license_attribution_json,
              (SELECT count(*) FROM app.media_asset_refs r
                 WHERE r.media_asset_id=a.id) AS reference_count
        FROM app.media_assets a WHERE a.workspace_id=%s AND NOT a.is_deleted
          AND (%s::uuid IS NULL OR a.id>%s) ORDER BY a.id LIMIT %s
        """,
        (wsId, cursor, cursor, limit + 1),
    )
    return {
        "items": result[:limit],
        "next_cursor": result[limit - 1]["id"] if len(result) > limit else None,
    }


def store(request, workspace, content, **metadata):
    try:
        identifier = put_media(
            first, storage(), request.tenant["tenant_id"], workspace, content, **metadata
        )
    except ValueError:
        raise ProblemError(422, "INVALID_MEDIA") from None
    except StorageError:
        raise ProblemError(503, "MEDIA_STORAGE_UNAVAILABLE") from None
    return JsonResponse({"id": identifier}, status=201)


@router.post(ASSET)
def upload(request, wsId: UUID, file: File[UploadedFile]):
    if disk_state() == "CRITICAL":
        raise ProblemError(503, "DISK_PRESSURE_CRITICAL")
    authorize(request, wsId)
    return store(request, wsId, file.read(MAX_BYTES + 1))


def stock(workspace):
    if not first(
        """
        SELECT 1 AS enabled FROM app.workspace_settings s JOIN app.external_consents c
          USING (tenant_id, workspace_id)
        WHERE s.workspace_id=%s AND s.allow_external_media_search
          AND c.provider_scope='STOCK_MEDIA_SEARCH' AND c.is_granted
        """,
        (workspace,),
    ):
        raise ProblemError(403, "STOCK_MEDIA_CONSENT_REQUIRED")
    key = os.environ.get("RF_PEXELS_API_KEY")
    if not key:
        raise ProblemError(503, "STOCK_MEDIA_UNAVAILABLE")
    return PexelsStockAdapter(key)


@router.post(ROOT + "/media/suggest-query")
def suggest(request, wsId: UUID, data: MediaText):
    authorize(request, wsId)
    return {"query": suggest_query(data.text)}


@router.post(ROOT + "/media/search")
def search(request, wsId: UUID, data: StockSearch):
    authorize(request, wsId)
    try:
        return {
            "items": stock(wsId).search(suggest_query(data.text), video=data.media_kind == "VIDEO"),
            "next_cursor": None,
        }
    except (ValueError, KeyError, TypeError, SSRFBlockedError, OutboundTransportError):
        raise ProblemError(503, "STOCK_MEDIA_UNAVAILABLE") from None


@router.post(ASSET + "/stock-download")
def download_stock(request, wsId: UUID, data: StockDownload):
    if disk_state() == "CRITICAL":
        raise ProblemError(503, "DISK_PRESSURE_CRITICAL")
    authorize(request, wsId)
    try:
        content, attribution = stock(wsId).download(data.stock_id, video=data.media_kind == "VIDEO")
    except (ValueError, KeyError, TypeError, SSRFBlockedError, OutboundTransportError):
        raise ProblemError(422, "STOCK_DOWNLOAD_DENIED") from None
    return store(request, wsId, content, origin="STOCK_LICENSED", attribution=attribution)


@router.delete(ASSET + "/{assetId}")
def delete_asset(request, wsId: UUID, assetId: UUID):
    authorize(request, wsId)
    asset = first(
        "SELECT id FROM app.media_assets WHERE workspace_id=%s AND id=%s "
        "AND NOT is_deleted FOR UPDATE",
        (wsId, assetId),
    )
    if not asset:
        raise ProblemError(404)
    if first("SELECT id FROM app.media_asset_refs WHERE media_asset_id=%s LIMIT 1", (assetId,)):
        raise ProblemError(409, "MEDIA_ASSET_IN_USE")
    first("UPDATE app.media_assets SET is_deleted=true WHERE id=%s RETURNING id", (assetId,))
    return {"id": assetId, "status": "DELETED"}


def save_script(request, workspace, data, identifier=None):
    authorize(request, workspace)
    speakers = {segment.speaker for segment in data.segments}
    expected = {"A", "B"} if data.speaker_mode == "DUAL_SPEAKER" else {"A"}
    if speakers != expected:
        raise ProblemError(422, "INVALID_SPEAKER_ASSIGNMENT")
    values = [
        segment.model_dump() | {"pronunciation": pronunciation(segment.text)}
        for segment in data.segments
    ]
    status = "APPROVED" if data.approved else "DRAFT"
    bindings = (data.speaker_mode, data.target_duration_sec, Jsonb(values), status)
    if identifier is None:
        identifier = uuid4()
        first(
            """
            INSERT INTO app.podcast_scripts (id,tenant_id,workspace_id,speaker_mode,
               target_duration_sec,segments_json,status) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """,
            (identifier, request.tenant["tenant_id"], workspace, *bindings),
        )
        code = 201
    else:
        row = first(
            """
            UPDATE app.podcast_scripts SET speaker_mode=%s,target_duration_sec=%s,segments_json=%s,
                status=%s,updated_at=clock_timestamp(),synthesized_media_asset_id=NULL
            WHERE workspace_id=%s AND id=%s AND status<>'SYNTHESIZING' RETURNING id
            """,
            (*bindings, workspace, identifier),
        )
        if not row:
            raise ProblemError(409, "SCRIPT_NOT_EDITABLE")
        code = 200
    return JsonResponse({"id": identifier, "status": status, "segments": values}, status=code)


@router.post(SCRIPT)
def create_script(request, wsId: UUID, data: ScriptInput):
    return save_script(request, wsId, data)


@router.patch(SCRIPT + "/{scriptId}")
def edit_script(request, wsId: UUID, scriptId: UUID, data: ScriptInput):
    return save_script(request, wsId, data, scriptId)


@router.post(JOB)
def enqueue(request, wsId: UUID, data: MediaJobInput):
    if disk_state() == "CRITICAL":
        raise ProblemError(503, "DISK_PRESSURE_CRITICAL")
    authorize(request, wsId)
    key = get_idempotency_token(request)
    tenant = request.tenant["tenant_id"]
    identifier = uuid5(NAMESPACE_URL, f"relayforge:media:{tenant}:{wsId}:{key}")
    spec = data.model_dump(mode="json")
    checksum = digest(spec)
    first("SELECT pg_advisory_xact_lock(hashtextextended(%s,7)) AS locked", (str(identifier),))
    old = first("SELECT request_sha256,status FROM app.media_jobs WHERE id=%s", (identifier,))
    if old:
        if old["request_sha256"] != checksum:
            raise ProblemError(409, "IDEMPOTENCY_KEY_REUSED")
        return JsonResponse({"id": identifier, "status": old["status"]}, status=202)
    if data.job_kind in {"LOCAL_SLIDESHOW_CLIP", "TTS_SYNTHESIS"}:
        try:
            ready = media_rpc("ping").get("ffmpeg")
        except ValueError:
            ready = False
        if not ready:
            raise ProblemError(503, "FFMPEG_UNAVAILABLE")
    modality = {"AI_IMAGE_GEN": "IMAGE", "AI_VIDEO_GEN_POLL": "VIDEO", "TTS_SYNTHESIS": "TTS"}.get(
        data.job_kind
    )
    if modality:
        if not data.provider_id or not data.model_id:
            raise ProblemError(422, "MEDIA_MODEL_REQUIRED")
        row = provider(wsId, data.provider_id)
        if not row["is_enabled"] or not consent(wsId, row["provider_type"], modality):
            raise ProblemError(403, "EXTERNAL_AI_CONSENT_REQUIRED")
        capability = first(
            """
            SELECT modalities_json,supported_resolutions_json FROM app.ai_model_cache
            WHERE provider_id=%s AND model_id=%s AND key_fingerprint_sha256=%s
            """,
            (row["id"], data.model_id, row["key_fingerprint_sha256"]),
        )
        if not capability or modality not in capability["modalities_json"]:
            raise ProblemError(422, "MODEL_MODALITY_UNVERIFIED")
        supports_hires = row["provider_type"] == "GEMINI" and (
            (modality == "IMAGE" and not data.model_id.startswith("imagen"))
            or (
                modality == "VIDEO"
                and data.model_id.startswith("gemini-omni-")
                and data.resolution == "4K"
            )
        )
        if data.resolution not in capability["supported_resolutions_json"] or (
            data.resolution != "1K" and not supports_hires
        ):
            raise ProblemError(422, "MODEL_RESOLUTION_UNVERIFIED")
        if modality != "TTS" and not data.prompt.strip():
            raise ProblemError(422, "MEDIA_PROMPT_REQUIRED")
    if data.job_kind == "AI_IMAGE_GEN" and len(data.asset_ids) > 1:
        raise ProblemError(422, "INVALID_IMAGE_EDIT_INPUT")
    if data.job_kind == "TTS_SYNTHESIS" and not first(
        """
        UPDATE app.podcast_scripts SET status='SYNTHESIZING' WHERE id=%s AND workspace_id=%s
          AND status IN ('APPROVED','READY') RETURNING id
        """,
        (data.podcast_script_id, wsId),
    ):
        raise ProblemError(409, "SCRIPT_APPROVAL_REQUIRED")
    kind = (
        "FFMPEG_TRANSCODE"
        if data.job_kind in {"STICKER_IMAGE", "NATIVE_STICKER"}
        else data.job_kind
    )
    first(
        """
        INSERT INTO app.media_jobs
          (id,tenant_id,workspace_id,job_kind,input_spec_json,request_sha256)
        VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
        """,
        (identifier, tenant, wsId, kind, Jsonb(spec), checksum),
    )
    return JsonResponse({"id": identifier, "status": "PENDING"}, status=202)


@router.get(JOB + "/{jobId}")
def media_job(request, wsId: UUID, jobId: UUID):
    require_workspace(wsId)
    job = first(
        """
        SELECT id,job_kind,status,output_media_asset_id,error_safe,created_at,completed_at
        FROM app.media_jobs WHERE id=%s AND workspace_id=%s
        """,
        (jobId, wsId),
    )
    if not job:
        raise ProblemError(404)
    return job


@router.delete(JOB + "/{jobId}")
def cancel(request, wsId: UUID, jobId: UUID):
    authorize(request, wsId)
    row = first(
        """
        UPDATE app.media_jobs
           SET status='CANCELLED',cancel_requested=true,completed_at=clock_timestamp()
        WHERE workspace_id=%s AND id=%s AND status IN ('PENDING','RUNNING')
        RETURNING input_spec_json
        """,
        (wsId, jobId),
    )
    if not row:
        raise ProblemError(409, "MEDIA_JOB_NOT_CANCELLABLE")
    if script := row["input_spec_json"].get("podcast_script_id"):
        first(
            """
            UPDATE app.podcast_scripts SET status='APPROVED'
            WHERE id=%s AND workspace_id=%s AND status='SYNTHESIZING' RETURNING id
            """,
            (script, wsId),
        )

    def notify_worker(_tenant):
        try:
            media_rpc("cancel_job", job_id=str(jobId))
        except ValueError:
            pass

    on_tenant_commit(request.tenant["tenant_id"], notify_worker)
    return {"id": jobId, "status": "CANCELLED"}


@router.get(ROOT + "/media/jobs")
def jobs(request, wsId: UUID, cursor: UUID | None = None, limit: int = 50):
    return listing(wsId, cursor, limit, "media_jobs", "id,job_kind,status,created_at")


@router.get(ROOT + "/podcast-scripts")
def scripts(request, wsId: UUID, cursor: UUID | None = None, limit: int = 50):
    return listing(wsId, cursor, limit, "podcast_scripts", "id,speaker_mode,status,created_at")

@router.get(ASSET + "/{assetId}/thumbnail")
def thumbnail(request, wsId: UUID, assetId: UUID):
    require_workspace(wsId)
    asset = first("""
        SELECT s3_object_key,sha256_hex FROM app.media_assets
        WHERE id=%s AND workspace_id=%s AND NOT is_deleted AND byte_size<=8388608
          AND mime_type IN ('image/png','image/jpeg','image/webp','image/gif')
    """, (assetId, wsId))
    if not asset:
        raise ProblemError(404)
    safe_key = f"tenants/{request.tenant['tenant_id']}/media/{asset['sha256_hex']}"
    if asset["s3_object_key"] != safe_key:
        raise ProblemError(404)
    try:
        source = io.BytesIO(read_media(storage(), safe_key))
        with Image.open(source, formats=("PNG", "JPEG", "WEBP", "GIF")) as picture:
            if picture.width * picture.height > 40_000_000:
                raise ProblemError(422)
            picture.thumbnail((360, 240))
            output = io.BytesIO()
            picture.convert("RGB").save(output, format="JPEG", quality=78)
    except (StorageError, OSError, ValueError, Image.DecompressionBombError):
        raise ProblemError(404) from None
    return HttpResponse(output.getvalue(), content_type="image/jpeg")
