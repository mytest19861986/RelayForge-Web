"""Content-addressed media storage shared by web and orchestration workers."""

from hashlib import sha256
from uuid import uuid4

from psycopg.types.json import Jsonb

from relayforge.core.media_validation import MAX_BYTES, media_format

MIMES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "WAV": "audio/wav",
    "MP3": "audio/mpeg",
    "OGG": "audio/ogg",
    "MP4": "video/mp4",
}
KINDS = {
    "PNG": "IMAGE",
    "JPEG": "IMAGE",
    "WEBP": "IMAGE",
    "GIF": "GIF",
    "WAV": "AUDIO",
    "MP3": "AUDIO",
    "OGG": "AUDIO",
    "MP4": "VIDEO",
}


def put_media(
    first,
    storage,
    tenant,
    workspace,
    data,
    *,
    kind=None,
    origin="USER_UPLOAD",
    attribution=None,
    duration=None,
):
    file_format, dimensions = media_format(data)
    width, height = dimensions or (None, None)
    fingerprint = sha256(data).hexdigest()
    object_key = f"tenants/{tenant}/media/{fingerprint}"
    kind = kind or KINDS[file_format]
    attribution = Jsonb(attribution or {})
    first("SELECT pg_advisory_xact_lock(hashtextextended(%s,6)) AS locked", (object_key,))
    previous = first(
        """
        SELECT id FROM app.media_assets WHERE workspace_id=%s AND s3_object_key=%s
        AND media_kind=%s AND origin_kind=%s AND license_attribution_json=%s AND NOT is_deleted
        ORDER BY created_at LIMIT 1
        """,
        (workspace, object_key, kind, origin, attribution),
    )
    if previous:
        return previous["id"]
    storage.put_object(object_key, data)
    return first(
        """
        INSERT INTO app.media_assets (id,tenant_id,workspace_id,media_kind,origin_kind,
            s3_object_key,sha256_hex,mime_type,byte_size,width_px,height_px,duration_sec,
            license_attribution_json)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """,
        (
            uuid4(),
            tenant,
            workspace,
            kind,
            origin,
            object_key,
            fingerprint,
            MIMES[file_format],
            len(data),
            width,
            height,
            duration,
            attribution,
        ),
    )["id"]


def read_media(storage, key):
    data = bytearray()
    for chunk in storage.stream_object_chunks(key):
        data.extend(chunk)
        if len(data) > MAX_BYTES:
            raise ValueError("MEDIA_SIZE_INVALID")
    return bytes(data)
