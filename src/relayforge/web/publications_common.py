from dataclasses import asdict
from uuid import UUID, uuid4

from django.db import connections
from psycopg.types.json import Jsonb

from relayforge.adapters.publications import outbox
from relayforge.core.rich_text import CanonicalRichText, FormatSpanUtf16
from relayforge.core.rules import BrandSpec
from relayforge.web.db import first, rows
from relayforge.web.problems import ProblemError


def list_page(query, workspace, limit, cursor):
    if not 1 <= limit <= 100:
        raise ProblemError(422)
    return rows(query, (workspace, cursor, cursor, limit + 1))


def get_brand(workspace, identifier):
    if identifier is None:
        return None
    try:
        identifier = UUID(str(identifier))
    except (TypeError, ValueError):
        raise ProblemError(422, "INVALID_BRAND_ID") from None
    row = first("""
        SELECT signature_template, placement_policy, separator_line FROM app.brand_identities
        WHERE workspace_id = %s AND id = %s
    """, (workspace, identifier))
    if not row:
        raise ProblemError(404)
    return BrandSpec(
        row["signature_template"] or "", row["placement_policy"], row["separator_line"],
    )


def rich_text(text, spans):
    try:
        return CanonicalRichText(text, tuple(FormatSpanUtf16(**span) for span in spans))
    except (ValueError, TypeError, UnicodeError):
        raise ProblemError(422, "INVALID_RICH_TEXT") from None


def get_destinations(workspace, identifiers):
    ids = [UUID(str(identifier)) for identifier in identifiers]
    if len(set(ids)) != len(ids):
        raise ProblemError(422, "DUPLICATE_DESTINATION")
    found = rows("""
        SELECT id, platform, canonical_chat_id, is_enabled, brand_identity_id
        FROM app.destinations WHERE workspace_id = %s AND id = ANY(%s) AND NOT is_archived
    """, (workspace, ids))
    by_id = {row["id"]: row for row in found}
    if len(by_id) != len(ids):
        raise ProblemError(404)
    return [by_id[identifier] for identifier in ids]


def event(request, workspace, name, payload):
    with connections["default"].cursor() as cursor:
        outbox(
            cursor, request.tenant["tenant_id"], workspace, name, request.correlation_id, payload,
        )


def save_revision(tenant, draft, revision, content, title, brand, attachments):
    identifier = uuid4()
    rows("""
        INSERT INTO app.draft_revisions (id, tenant_id, draft_id, revision_no,
            body_text, title_text, entities_ast_json, brand_identity_id, media_manifest_json)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (identifier, tenant, draft, revision, content.plain_text, title,
          Jsonb([asdict(span) for span in content.spans_utf16]), brand, Jsonb(attachments)))
    for index, asset in enumerate(attachments):
        rows("""
            INSERT INTO app.media_asset_refs (id, tenant_id, media_asset_id, owner_entity_type,
                owner_entity_id, sort_order) VALUES (%s, %s, %s, 'DRAFT_REVISION', %s, %s)
        """, (uuid4(), tenant, asset, identifier, index))



def draft_record(workspace, identifier, *, lock=False):
    row = first("""
        SELECT d.id, d.route_id, d.route_policy_revision_no, d.source_content_item_id,
               d.current_revision_no, d.approval_state, r.title_text, r.body_text,
               r.entities_ast_json AS spans_utf16, r.brand_identity_id, r.media_manifest_json
        FROM app.drafts d JOIN app.draft_revisions r ON r.tenant_id = d.tenant_id
            AND r.draft_id = d.id AND r.revision_no = d.current_revision_no
        WHERE d.workspace_id = %s AND d.id = %s
    """ + (" FOR UPDATE OF d" if lock else ""), (workspace, identifier))
    if not row:
        raise ProblemError(404)
    return row
