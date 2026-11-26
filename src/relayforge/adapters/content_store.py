"""Shared initial-revision insert path for webhook and archive ingestion."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from uuid import uuid4

from psycopg.types.json import Jsonb

from relayforge.adapters.messengers import bot_access_class
from relayforge.core.rich_text import (
    KINDS,
    CanonicalRichText,
    FormatSpanUtf16,
    safe_link,
    utf16_len,
)

_TYPES = {value: key for key, value in KINDS.items()} | {"link": "LINK", "quote": "QUOTE"}


@dataclass(frozen=True)
class IncomingMessage:
    external_id: str
    text: CanonicalRichText
    access_class: str = "PROCESSABLE"
    published_at: str | None = None
    media_group_id: str | None = None


def message_id(value):
    if type(value) is int and 0 < value <= 2**63 - 1:
        return str(value)
    return None


def bot_post(post):
    identifier = message_id(post.get("message_id"))
    if identifier is None:
        return None
    text = post.get("text", post.get("caption", ""))
    entities = post.get("entities", post.get("caption_entities", []))
    if not isinstance(text, str) or not isinstance(entities, list):
        raise ValueError("Invalid bot text")
    spans = []
    for entity in entities:
        if not isinstance(entity, dict) or not isinstance(entity.get("type"), str):
            continue
        kind = _TYPES.get(entity["type"])
        url = entity.get("url")
        if kind == "LINK" and (not isinstance(url, str) or not safe_link(url)):
            continue
        if kind and type(entity.get("offset")) is int and type(entity.get("length")) is int:
            spans.append(FormatSpanUtf16(entity["offset"], entity["length"], kind,
                                         url if kind == "LINK" else None))
    timestamp = post.get("date")
    published = datetime.fromtimestamp(timestamp, UTC).isoformat() if (
        type(timestamp) is int and 0 <= timestamp <= 253402300799
    ) else None
    group = post.get("media_group_id")
    has_media = any(name in post for name in ("photo", "video", "document", "audio", "voice",
                                              "video_note", "animation", "sticker"))
    if not text.strip() and not has_media:
        return None
    return IncomingMessage(identifier, CanonicalRichText(text, tuple(spans)),
                           bot_access_class(post), published, str(group)[:100] if group else None)


def desktop_message(item):
    if not isinstance(item, dict) or item.get("type") != "message" or item.get("is_deleted"):
        return None
    identifier = message_id(item.get("id"))
    if identifier is None:
        return None
    elements = item.get("text_entities") or item.get("text", "")
    elements = [elements] if isinstance(elements, str) else elements
    if not isinstance(elements, list):
        return None
    pieces, spans, offset = [], [], 0
    for element in elements:
        if isinstance(element, str):
            text, kind, url = element, None, None
        elif isinstance(element, dict) and isinstance(element.get("text"), str):
            text = element["text"]
            entity_type = element.get("type")
            kind = _TYPES.get(entity_type) if isinstance(entity_type, str) else None
            url = element.get("href", text) if kind == "LINK" else None
        else:
            continue
        length = utf16_len(text)
        if length and kind and (
            kind != "LINK" or (isinstance(url, str) and safe_link(url))
        ):
            spans.append(FormatSpanUtf16(offset, length, kind, url))
        pieces.append(text)
        offset += length
    text = "".join(pieces)
    if not text.strip():
        return None
    # Bot/export payloads have no authority to turn protected items into shippable content.
    protection = "PROTECTED_NO_FORWARD" if item.get("has_protected_content") else "PROCESSABLE"
    timestamp = item.get("date_unixtime")
    timestamp = int(timestamp) if isinstance(timestamp, str) and (
        timestamp.isascii() and timestamp.isdecimal() and len(timestamp) <= 12
    ) else timestamp
    published = datetime.fromtimestamp(timestamp, UTC).isoformat() if (
        type(timestamp) is int and 0 <= timestamp <= 253402300799
    ) else None
    return IncomingMessage(identifier, CanonicalRichText(text, tuple(spans)), protection, published)


_INSERT = """
WITH incoming AS (
    SELECT * FROM jsonb_to_recordset(%s) AS data(
        id uuid, revision_id uuid, message_id text, hash text, published timestamptz,
        access text, plain text, spans jsonb, media_group_id text)
), inserted AS (
    INSERT INTO app.content_items (id, tenant_id, workspace_id, source_id, origin_type,
        canonical_channel_id, external_message_id, operation_scope, media_group_id, access_class,
        content_hash_sha256, source_published_at)
    SELECT id, %s, %s, %s, %s, %s, message_id, %s, media_group_id, access, hash, published
    FROM incoming
    ON CONFLICT (tenant_id, workspace_id, canonical_channel_id,
                 external_message_id, operation_scope)
    DO NOTHING RETURNING id, tenant_id
)
INSERT INTO app.content_item_revisions (id, tenant_id, content_item_id, revision_no,
                                       plain_text, entities_ast_json)
SELECT incoming.revision_id, inserted.tenant_id, inserted.id, 1, incoming.plain, incoming.spans
FROM inserted JOIN incoming USING (id)
"""


def insert_messages(cursor, source, origin, scope, messages):
    # Use identical lock order when concurrent imports overlap. Keep output counts per row.
    records = []
    for message in sorted(messages, key=lambda item: int(item.external_id)):
        spans = [asdict(span) for span in message.text.spans_utf16]
        canonical = (message.text.plain_text, spans)
        digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=False).encode()).hexdigest()
        records.append({
            "id": str(uuid4()), "revision_id": str(uuid4()), "message_id": message.external_id,
            "hash": digest, "published": message.published_at, "access": message.access_class,
            "plain": message.text.plain_text, "spans": spans,
            "media_group_id": message.media_group_id,
        })
    cursor.execute(_INSERT, (
        Jsonb(records), source["tenant_id"], source["workspace_id"], source["source_id"],
        origin, source["canonical_channel_id"], scope,
    ))
    inserted = cursor.rowcount
    protected = [
        item.external_id for item in messages if item.access_class == "PROTECTED_NO_FORWARD"
    ]
    if protected:
        cursor.execute("""
            UPDATE app.content_items SET access_class = 'PROTECTED_NO_FORWARD'
            WHERE tenant_id = %s AND workspace_id = %s AND canonical_channel_id = %s
              AND operation_scope = %s AND external_message_id = ANY(%s)
              AND access_class <> 'PROTECTED_NO_FORWARD'
        """, (source["tenant_id"], source["workspace_id"], source["canonical_channel_id"],
              scope, protected))
    return inserted


def advance_cursor(cursor, source, external_id, timestamp=None):
    cursor.execute("""
        INSERT INTO app.source_cursors (source_id, tenant_id, last_external_message_id,
                                       last_message_timestamp)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (source_id) DO UPDATE SET
            last_external_message_id = EXCLUDED.last_external_message_id,
            last_message_timestamp = EXCLUDED.last_message_timestamp, updated_at = clock_timestamp()
        WHERE app.source_cursors.last_external_message_id IS NULL
           OR app.source_cursors.last_external_message_id::numeric
                < EXCLUDED.last_external_message_id::numeric
    """, (source["source_id"], source["tenant_id"], external_id, timestamp))
