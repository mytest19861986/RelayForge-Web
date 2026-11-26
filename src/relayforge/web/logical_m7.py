"""Ordered logical workspace table export and restore under request-scoped RLS."""

import json
import re
from uuid import UUID, uuid5

from psycopg.types.json import Jsonb

from relayforge.adapters.media_storage import MIMES, read_media
from relayforge.adapters.tenant_backup import packed, sha
from relayforge.core.media_validation import media_format
from relayforge.web.db import first, rows
from relayforge.web.routes_m2 import SECRET_KINDS, storage

PARENTS = {
    "style_revisions": ("style_profiles", "style_profile_id"),
    "route_policy_revisions": ("routes", "route_id"),
    "draft_revisions": ("drafts", "draft_id"),
}
TABLES = (
    "media_assets",
    "brand_identities",
    "sources",
    "destinations",
    "style_profiles",
    "style_revisions",
    "routes",
    "route_policy_revisions",
    "drafts",
    "draft_revisions",
    "podcast_scripts",
)
PREFIX = "logical/tables/"
MEDIA_CAP = 24 * 1024 * 1024


def where(table):
    if table not in PARENTS:
        return "r.workspace_id=%s"
    parent, key = PARENTS[table]
    return f"EXISTS (SELECT 1 FROM app.{parent} p WHERE p.id=r.{key} AND p.workspace_id=%s)"


def logical_export(workspace, tenant, include_media):
    entries, used = {}, 0
    for table in TABLES:
        items = rows(
            f"""
            SELECT to_jsonb(r) AS item FROM app.{table} r
            WHERE {where(table)} ORDER BY r.id LIMIT 5001
        """,
            (workspace,),
        )
        if len(items) > 5000:
            raise ValueError("BACKUP_TOO_LARGE")
        values = [item["item"] for item in items]
        if table == "sources":
            for item in values:
                item.pop("webhook_token_sha256", None)
        if table == "media_assets" and include_media:
            for item in values:
                key = item["s3_object_key"]
                digest = item["sha256_hex"]
                if item["is_deleted"]:
                    continue
                if not re.fullmatch(r"[a-f0-9]{64}", digest) or (
                    key != f"tenants/{tenant}/media/{digest}"
                ):
                    raise ValueError("INVALID_MEDIA_KEY")
                data = read_media(storage(), key)
                if sha(data) != digest:
                    raise ValueError("MEDIA_CHECKSUM_MISMATCH")
                kind, _ = media_format(data)
                path = f"media/{digest[:2]}/{digest}.{kind.lower()}"
                if path not in entries:
                    entries[path] = data
                    used += len(data)
                if used > MEDIA_CAP:
                    raise ValueError("BACKUP_TOO_LARGE")
        document = b"".join(packed(item) + b"\n" for item in values)
        entries[f"{PREFIX}{table}.jsonl"] = document
        used += len(document)
        if used > MEDIA_CAP:
            raise ValueError("BACKUP_TOO_LARGE")
    return entries


def inspect_entries(entries):
    records, media = {}, {}
    for path, data in entries.items():
        if path.startswith(PREFIX):
            table = path.removeprefix(PREFIX).removesuffix(".jsonl")
            if table not in TABLES:
                raise ValueError("INVALID_TABLE")
            objects = [json.loads(line) for line in data.splitlines()]
            if len(objects) > 5000:
                raise ValueError("BACKUP_TOO_LARGE")
            ids = {UUID(row["id"]) for row in objects}
            if len(ids) != len(objects):
                raise ValueError("DUPLICATE_RECORD")
            records[table] = objects
        if path.startswith("media/"):
            digest = sha(data)
            kind, _ = media_format(data)
            if path != f"media/{digest[:2]}/{digest}.{kind.lower()}" or digest in media:
                raise ValueError("INVALID_MEDIA_ENTRY")
            media[digest] = data
    if set(records) != set(TABLES):
        raise ValueError("MISSING_TABLE")
    return records, media


def remap(value, identifiers):
    if isinstance(value, str):
        return identifiers.get(value, value)
    if isinstance(value, list):
        return [remap(item, identifiers) for item in value]
    if isinstance(value, dict):
        return {key: remap(item, identifiers) for key, item in value.items()}
    return value


def apply_logical(workspace, tenant, records, media, secrets, *, placeholder=None):
    identifiers = dict(secrets)
    for table in TABLES:
        for record in records[table]:
            old = record["id"]
            existing = first(
                f"SELECT id FROM app.{table} r WHERE r.id=%s AND {where(table)}",
                (old, workspace),
            )
            identifiers[old] = old if existing else str(uuid5(workspace, old))
    for table in TABLES:
        for original in records[table]:
            record = remap(original, identifiers)
            record["tenant_id"] = str(tenant)
            if table not in PARENTS:
                record["workspace_id"] = str(workspace)
            if table in {"routes", "destinations"}:
                record["is_enabled"] = False
            if table in {"sources", "destinations"}:
                old_secret = original.get("secret_id")
                current = (
                    first(
                        """
                    SELECT id,secret_kind FROM app.secrets_vault WHERE id=%s AND workspace_id=%s
                    AND NOT is_revoked
                """,
                        (record.get("secret_id"), workspace),
                    )
                    if old_secret
                    else None
                )
                record["secret_id"] = str(current["id"]) if current else None
                if table == "sources":
                    record.update(
                        is_active=False,
                        last_health_status="UNKNOWN",
                        webhook_platform=None,
                        webhook_token_sha256=None,
                    )
                else:
                    record["publish_verified_at"] = None
                    kind = SECRET_KINDS.get(record["platform"])
                    if kind is None:
                        raise ValueError("INVALID_BACKUP_DESTINATION")
                    if not current or current["secret_kind"] != kind:
                        if placeholder is None:
                            raise ValueError("DESTINATION_SECRET_REQUIRED")
                        record["secret_id"] = placeholder(original["id"], kind)
            if table == "style_profiles":
                record["analysis_id"] = None
            if table == "drafts":
                record.update(
                    source_content_item_id=None, generation_session_id=None, created_by_user_id=None
                )
            if table == "podcast_scripts":
                record["segment_cache_json"] = {}
                if record["status"] == "SYNTHESIZING":
                    record["status"] = "DRAFT"
            if table == "media_assets":
                digest = record["sha256_hex"]
                if not re.fullmatch(r"[a-f0-9]{64}", digest):
                    raise ValueError("INVALID_MEDIA_KEY")
                key = f"tenants/{tenant}/media/{digest}"
                record["s3_object_key"] = key
                if not record["is_deleted"] and digest in media:
                    kind, _ = media_format(media[digest])
                    if MIMES[kind] != record["mime_type"] or (
                        len(media[digest]) != record["byte_size"]
                    ):
                        raise ValueError("INVALID_MEDIA_ENTRY")
                    storage().put_object(key, media[digest])
                elif not record["is_deleted"] and not first(
                    """
                    SELECT id FROM app.media_assets WHERE workspace_id=%s AND s3_object_key=%s
                      AND NOT is_deleted LIMIT 1
                """,
                    (workspace, key),
                ):
                    record["is_deleted"] = True
            rows(
                f"""
                INSERT INTO app.{table}
                SELECT (jsonb_populate_record(NULL::app.{table},%s)).*
                ON CONFLICT (id) DO NOTHING
            """,
                (Jsonb(record),),
            )
            if table == "draft_revisions":
                for order, asset in enumerate(record.get("media_manifest_json") or []):
                    if not isinstance(asset, str) or not first(
                        """
                        SELECT id FROM app.media_assets WHERE id=%s AND workspace_id=%s
                        AND NOT is_deleted
                    """,
                        (asset, workspace),
                    ):
                        continue
                    rows(
                        """
                        INSERT INTO app.media_asset_refs
                            (id,tenant_id,media_asset_id,owner_entity_type,owner_entity_id,sort_order)
                        VALUES (%s,%s,%s,'DRAFT_REVISION',%s,%s) ON CONFLICT DO NOTHING
                    """,
                        (uuid5(UUID(record["id"]), asset), tenant, asset, record["id"], order),
                    )
