-- ============================================================================
-- RelayForge Web SaaS Edition — Master PostgreSQL 16 Database Contract
-- Target Engine: PostgreSQL 16.x (behind PgBouncer 1.25+ in pool_mode=transaction)
-- Security Invariants:
--   1. Dedicated schema `app` owned by `relayforge_migrator` (REVOKE ALL FROM PUBLIC).
--   2. 6 non-superuser roles, ZERO BYPASSRLS across all non-superuser roles.
--   3. 100% of tables (51/51) enforce ENABLE + FORCE ROW LEVEL SECURITY + migrator RLS.
--   4. Composite tenant-anchored Foreign Keys `(tenant_id, <fk_id>)` prevent cross-tenant RI leaks.
--   5. Operator Plan Quotas (`OPERATOR_PLAN`) separated from Tenant Sub-Budgets (`TENANT_BUDGET`) via `RF007`.
--   6. Delivery state machine, `lease_epoch` fencing, and audited `UNCERTAIN` reconciliation enforced via DB trigger.
-- ============================================================================

-- ============================================================================
-- 1. Roles & Schema Bootstrap (Idempotent Role Creation for Infrastructure Init)
--    WARNING: Never DROP ROLE on a live cluster with active PgBouncer pools (OC-04/PgBouncer OID cache).
-- ============================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'relayforge_migrator') THEN
        CREATE ROLE relayforge_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'relayforge_operator') THEN
        CREATE ROLE relayforge_operator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'relayforge_web') THEN
        CREATE ROLE relayforge_web LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'relayforge_worker') THEN
        CREATE ROLE relayforge_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'relayforge_dispatcher_owner') THEN
        CREATE ROLE relayforge_dispatcher_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'relayforge_ledger_owner') THEN
        CREATE ROLE relayforge_ledger_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
END;
$$;

CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION relayforge_migrator;
REVOKE ALL ON SCHEMA app FROM PUBLIC;
GRANT USAGE ON SCHEMA app TO relayforge_operator, relayforge_web, relayforge_worker, relayforge_dispatcher_owner, relayforge_ledger_owner;

SET ROLE relayforge_migrator;

-- ============================================================================
-- 2. Platform & Multi-Tenant Root Identity Tables (Tables 1..8)
-- ============================================================================

-- [01] tenants
CREATE TABLE app.tenants (
    id UUID PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    display_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'SUSPENDED', 'PENDING_DELETION')),
    active_dek_version INT NOT NULL DEFAULT 1 CHECK (active_dek_version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp()
);

-- [02] users (Global login identities; tenant access governed via tenant_memberships)
CREATE TABLE app.users (
    id UUID PRIMARY KEY,
    email TEXT NOT NULL UNIQUE CHECK (email = lower(email) AND position('@' IN email) > 1),
    display_name TEXT NOT NULL,
    password_hash_argon2id TEXT NOT NULL,
    is_platform_operator BOOLEAN NOT NULL DEFAULT false,
    is_active BOOLEAN NOT NULL DEFAULT true,
    last_login_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp()
);

-- [03] tenant_memberships (RBAC per tenant: OWNER, ADMIN, EDITOR, PUBLISHER, OPERATOR_RO)
CREATE TABLE app.tenant_memberships (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
    user_id UUID NOT NULL REFERENCES app.users(id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK (role IN ('OWNER', 'ADMIN', 'EDITOR', 'PUBLISHER', 'VIEWER')),
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, user_id)
);

-- [04] web_sessions (Server-side revocable cookie sessions with CSRF binding; replaces Desktop loopback session)
CREATE TABLE app.web_sessions (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
    token_hash_sha256 TEXT NOT NULL UNIQUE,
    csrf_token_hash_sha256 TEXT NOT NULL,
    ip_subnet TEXT,
    user_agent_summary TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, user_id) REFERENCES app.tenant_memberships(tenant_id, user_id) ON DELETE CASCADE
);

-- [05] tenant_encryption_keys (Per-tenant DEK wrapped by Master KEK; enables Crypto-Shredding on tenant deletion)
CREATE TABLE app.tenant_encryption_keys (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
    key_version INT NOT NULL CHECK (key_version >= 1),
    wrapped_dek_ciphertext BYTEA,
    wrapping_alg TEXT NOT NULL DEFAULT 'AES-256-GCM',
    key_state TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (key_state IN ('ACTIVE', 'RETIRED', 'SHREDDED')),
    shredded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    CHECK (key_state <> 'SHREDDED' OR (wrapped_dek_ciphertext IS NULL AND shredded_at IS NOT NULL)),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, key_version)
);

-- [06] tenant_dispatch_fairness (Anti-starvation round-robin water-filling tracker)
CREATE TABLE app.tenant_dispatch_fairness (
    tenant_id UUID PRIMARY KEY REFERENCES app.tenants(id) ON DELETE CASCADE,
    last_served_at TIMESTAMPTZ NOT NULL
);

-- [07] schema_migrations (Controlled migration ledger owned by relayforge_migrator)
CREATE TABLE app.schema_migrations (
    version TEXT PRIMARY KEY,
    checksum_sha256 TEXT NOT NULL,
    applied_by TEXT NOT NULL DEFAULT CURRENT_USER,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp()
);

-- [08] rate_limit_counters (UNLOGGED sliding-window rate-limit table replacing Redis in MVP; ephemeral on crash)
CREATE UNLOGGED TABLE app.rate_limit_counters (
    tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
    bucket_key TEXT NOT NULL,
    window_epoch_sec BIGINT NOT NULL,
    hit_count INT NOT NULL DEFAULT 1 CHECK (hit_count >= 0),
    PRIMARY KEY (tenant_id, bucket_key, window_epoch_sec)
);

-- ============================================================================
-- 3. Workspaces, Secrets, Consents & Brand Identity (Tables 9..13)
-- ============================================================================

-- [09] workspaces
CREATE TABLE app.workspaces (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
    slug TEXT NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    display_name TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'Asia/Tehran',
    is_archived BOOLEAN NOT NULL DEFAULT false,
    emergency_stop BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, slug)
);

-- [10] workspace_settings
CREATE TABLE app.workspace_settings (
    workspace_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    default_freedom_mode TEXT NOT NULL DEFAULT 'BALANCED' CHECK (
        default_freedom_mode IN ('STRICT', 'BALANCED', 'CREATIVE', 'UNRESTRICTED')
    ),
    retention_days INT NOT NULL DEFAULT 90 CHECK (retention_days BETWEEN 7 AND 3650),
    allow_external_ai BOOLEAN NOT NULL DEFAULT false,
    allow_external_media_search BOOLEAN NOT NULL DEFAULT false,
    allow_feedback_learning BOOLEAN NOT NULL DEFAULT false,
    settings_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, workspace_id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE CASCADE
);

-- [11] secrets_vault (Envelope-encrypted BYOK & Bot tokens using tenant_encryption_keys; never returned in plaintext)
CREATE TABLE app.secrets_vault (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    secret_kind TEXT NOT NULL CHECK (secret_kind IN (
        'TELEGRAM_BOT_TOKEN', 'BALE_BOT_TOKEN', 'EITAA_API_TOKEN',
        'RUBIKA_BOT_TOKEN', 'SOROUSH_BOT_TOKEN', 'OPENAI_API_KEY',
        'GEMINI_API_KEY', 'MEDIA_SEARCH_API_KEY', 'WEBHOOK_SIGNING_SECRET'
    )),
    label TEXT NOT NULL,
    masked_hint TEXT NOT NULL,
    dek_version INT NOT NULL,
    ciphertext_aead BYTEA NOT NULL,
    nonce_bytes BYTEA NOT NULL,
    is_revoked BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    rotated_at TIMESTAMPTZ,
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, dek_version) REFERENCES app.tenant_encryption_keys(tenant_id, key_version) ON DELETE RESTRICT
);

-- [12] external_consents (Explicit per-provider data transmission consent: AIP-11, SEC-10, LRN-01)
CREATE TABLE app.external_consents (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    provider_scope TEXT NOT NULL CHECK (provider_scope IN (
        'OPENAI_TEXT', 'OPENAI_VISION', 'OPENAI_IMAGE', 'OPENAI_AUDIO',
        'GEMINI_TEXT', 'GEMINI_VISION', 'GEMINI_IMAGE', 'GEMINI_AUDIO', 'GEMINI_VIDEO',
        'STOCK_MEDIA_SEARCH', 'LOCAL_FEEDBACK_MEMORY'
    )),
    is_granted BOOLEAN NOT NULL DEFAULT false,
    granted_by_user_id UUID REFERENCES app.users(id) ON DELETE SET NULL,
    consent_version TEXT NOT NULL DEFAULT '1.0',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, workspace_id, provider_scope),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE CASCADE
);

-- [13] brand_identities (Owner-authorized brand signatures, handles, links & visual tokens: IDN-03..06)
CREATE TABLE app.brand_identities (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    display_name TEXT NOT NULL,
    channel_handle TEXT,
    official_url TEXT,
    signature_template TEXT,
    separator_line TEXT NOT NULL DEFAULT '━━━━━━━━━━━━━━',
    placement_policy TEXT NOT NULL DEFAULT 'FOOTER' CHECK (placement_policy IN ('HEADER', 'FOOTER', 'BOTH', 'NONE')),
    glossary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    visual_tokens_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    ownership_attested BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT
);

-- ============================================================================
-- 4. Sources, S3 Export Ingestion, Destinations & Canonical Content (Tables 14..19)
-- ============================================================================

-- [14] sources (Customer MTProto user-sessions are STRICTLY BLOCKED on Multi-Tenant Cloud!)
CREATE TABLE app.sources (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN (
        'TELEGRAM_BOT_CHANNEL', 'TELEGRAM_DESKTOP_EXPORT', 'BALE_BOT_CHANNEL',
        'MANUAL_STUDIO', 'RSS_FEED', 'WEBHOOK_INBOUND'
    )),
    canonical_channel_id TEXT NOT NULL,
    display_title TEXT NOT NULL,
    secret_id UUID,
    is_active BOOLEAN NOT NULL DEFAULT true,
    republish_rights_attested BOOLEAN NOT NULL DEFAULT false,
    last_health_status TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (
        last_health_status IN ('UNKNOWN', 'HEALTHY', 'AUTH_ERROR', 'RATE_LIMITED', 'UNREACHABLE')
    ),
    last_checked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, workspace_id, source_kind, canonical_channel_id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, secret_id) REFERENCES app.secrets_vault(tenant_id, id) ON DELETE SET NULL
);

-- [15] source_cursors
CREATE TABLE app.source_cursors (
    source_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    last_update_offset BIGINT,
    last_external_message_id TEXT,
    last_message_timestamp TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, source_id),
    FOREIGN KEY (tenant_id, source_id) REFERENCES app.sources(tenant_id, id) ON DELETE CASCADE
);

-- [16] export_import_jobs (S3 Presigned POST -> streaming ijson yajl2_c -> deterministic raw delete)
CREATE TABLE app.export_import_jobs (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    source_id UUID NOT NULL,
    s3_object_key TEXT NOT NULL CHECK (position('..' IN s3_object_key) = 0),
    file_size_bytes BIGINT NOT NULL CHECK (file_size_bytes > 0 AND file_size_bytes <= 272629760),
    job_mode TEXT NOT NULL CHECK (job_mode IN ('ARCHIVE_INGEST', 'CHANNEL_ANALYSIS_ONLY')),
    status TEXT NOT NULL DEFAULT 'UPLOADING' CHECK (
        status IN ('UPLOADING', 'PARSING', 'COMPLETED', 'FAILED', 'EXPIRED_CLEANED')
    ),
    messages_total INT NOT NULL DEFAULT 0,
    messages_imported INT NOT NULL DEFAULT 0,
    messages_skipped INT NOT NULL DEFAULT 0,
    raw_deleted_at TIMESTAMPTZ,
    error_code TEXT,
    error_detail_safe TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    completed_at TIMESTAMPTZ,
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, source_id) REFERENCES app.sources(tenant_id, id) ON DELETE RESTRICT
);

-- [17] destinations (Telegram & Bale GA in MVP; Eitaa outbound-only; Rubika & Soroush+ Experimental pending AT-22)
CREATE TABLE app.destinations (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    platform TEXT NOT NULL CHECK (platform IN (
        'TELEGRAM', 'BALE', 'EITAA', 'RUBIKA_EXPERIMENTAL', 'SOROUSH_EXPERIMENTAL'
    )),
    maturity_tier TEXT NOT NULL DEFAULT 'GA' CHECK (maturity_tier IN ('GA', 'OUTBOUND_LIMITED', 'EXPERIMENTAL')),
    canonical_chat_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    secret_id UUID NOT NULL,
    brand_identity_id UUID,
    capabilities_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    backlog_mode TEXT NOT NULL DEFAULT 'NEW_ONLY' CHECK (backlog_mode IN ('NEW_ONLY', 'INCLUDE_BACKLOG')),
    is_enabled BOOLEAN NOT NULL DEFAULT false,
    publish_verified_at TIMESTAMPTZ,
    last_error_safe TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    CHECK (platform NOT IN ('RUBIKA_EXPERIMENTAL', 'SOROUSH_EXPERIMENTAL') OR maturity_tier = 'EXPERIMENTAL'),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, workspace_id, platform, canonical_chat_id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, secret_id) REFERENCES app.secrets_vault(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, brand_identity_id) REFERENCES app.brand_identities(tenant_id, id) ON DELETE SET NULL
);

-- [18] content_items (Deduplicated by canonical_channel_id + external_message_id + operation_scope: SRC-05..10)
CREATE TABLE app.content_items (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    source_id UUID,
    origin_type TEXT NOT NULL CHECK (origin_type IN (
        'LIVE_BOT_UPDATE', 'EXPORT_ARCHIVE', 'MANUAL_ENTRY', 'ANALYSIS_SAMPLE_ONLY', 'RSS_ITEM'
    )),
    canonical_channel_id TEXT NOT NULL,
    external_message_id TEXT NOT NULL,
    operation_scope TEXT NOT NULL CHECK (operation_scope IN ('INGEST_PIPELINE', 'ANALYSIS_ISOLATED', 'MANUAL_INPUT')),
    media_group_id TEXT,
    access_class TEXT NOT NULL DEFAULT 'PROCESSABLE' CHECK (
        access_class IN ('PROCESSABLE', 'REFERENCE_ONLY', 'PROTECTED_NO_FORWARD')
    ),
    content_hash_sha256 TEXT NOT NULL,
    source_published_at TIMESTAMPTZ,
    is_deleted_upstream BOOLEAN NOT NULL DEFAULT false,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, workspace_id, canonical_channel_id, external_message_id, operation_scope),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, source_id) REFERENCES app.sources(tenant_id, id) ON DELETE SET NULL
);

-- [19] content_item_revisions (UTF-16 semantic AST + PostgreSQL GIN full-text tsvector index)
CREATE TABLE app.content_item_revisions (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    content_item_id UUID NOT NULL,
    revision_no INT NOT NULL CHECK (revision_no >= 1),
    plain_text TEXT NOT NULL DEFAULT '',
    entities_ast_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    media_manifest_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    fts_vector TSVECTOR GENERATED ALWAYS AS (pg_catalog.to_tsvector('simple', COALESCE(plain_text, ''))) STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, content_item_id, revision_no),
    FOREIGN KEY (tenant_id, content_item_id) REFERENCES app.content_items(tenant_id, id) ON DELETE CASCADE
);

-- ============================================================================
-- 5. Channel Intelligence & Style Library (Tables 20..23)
-- ============================================================================

-- [20] channel_analyses
CREATE TABLE app.channel_analyses (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    source_id UUID,
    target_channel_ref TEXT NOT NULL,
    requested_sample_count INT NOT NULL CHECK (requested_sample_count BETWEEN 10 AND 2000),
    unique_sample_count INT NOT NULL DEFAULT 0,
    min_evidence_threshold INT NOT NULL DEFAULT 10,
    vision_enabled BOOLEAN NOT NULL DEFAULT false,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (
        status IN ('PENDING', 'SAMPLING', 'ANALYZING', 'COMPLETED', 'CANCELLED', 'FAILED')
    ),
    statistical_summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    extracted_identity_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    visual_summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    completed_at TIMESTAMPTZ,
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, source_id) REFERENCES app.sources(tenant_id, id) ON DELETE SET NULL
);

-- [21] channel_analysis_samples (Holdout split: EXTRACTION vs EVALUATION: CH-05)
CREATE TABLE app.channel_analysis_samples (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    analysis_id UUID NOT NULL,
    content_item_id UUID NOT NULL,
    sample_partition TEXT NOT NULL DEFAULT 'EXTRACTION' CHECK (sample_partition IN ('EXTRACTION', 'EVALUATION')),
    detected_category TEXT,
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, analysis_id, content_item_id),
    FOREIGN KEY (tenant_id, analysis_id) REFERENCES app.channel_analyses(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, content_item_id) REFERENCES app.content_items(tenant_id, id) ON DELETE CASCADE
);

-- [22] style_profiles
CREATE TABLE app.style_profiles (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    analysis_id UUID,
    name TEXT NOT NULL,
    content_category TEXT NOT NULL DEFAULT 'GENERAL',
    origin_kind TEXT NOT NULL CHECK (origin_kind IN ('EXTRACTED', 'MANUAL', 'BLENDED', 'IMPORTED')),
    evidence_sample_count INT NOT NULL DEFAULT 0,
    confidence_label TEXT NOT NULL DEFAULT 'MANUAL' CHECK (
        confidence_label IN ('MANUAL', 'LOW_EVIDENCE', 'MODERATE_EVIDENCE', 'STRONG_EVIDENCE')
    ),
    pinned_revision_no INT NOT NULL DEFAULT 1,
    is_archived BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, analysis_id) REFERENCES app.channel_analyses(tenant_id, id) ON DELETE SET NULL
);

-- [23] style_revisions (Immutable versioned style specs; pinned on drafts so edits never mutate past posts: STY-08)
CREATE TABLE app.style_revisions (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    style_profile_id UUID NOT NULL,
    revision_no INT NOT NULL CHECK (revision_no >= 1),
    writing_rules_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    layout_template_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    visual_style_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    blend_weights_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, style_profile_id, revision_no),
    FOREIGN KEY (tenant_id, style_profile_id) REFERENCES app.style_profiles(tenant_id, id) ON DELETE RESTRICT
);

-- ============================================================================
-- 6. Routes, Policy Revisions & Deduplication Fingerprints (Tables 24..26)
--    (Declared before budget_reservations so route_id composite FKs resolve cleanly)
-- ============================================================================

-- [24] routes (4 explicit modes: COPY, RULES, AI, HYBRID — never auto-enabled on create/restore: AUT-01)
CREATE TABLE app.routes (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    slug TEXT NOT NULL,
    display_name TEXT NOT NULL,
    route_mode TEXT NOT NULL DEFAULT 'COPY' CHECK (route_mode IN ('COPY', 'RULES', 'AI', 'HYBRID')),
    automation_level TEXT NOT NULL DEFAULT 'DRAFT_ONLY' CHECK (
        automation_level IN ('DRAFT_ONLY', 'APPROVAL_REQUIRED', 'AUTO_PUBLISH_BY_POLICY')
    ),
    is_enabled BOOLEAN NOT NULL DEFAULT false,
    active_policy_revision INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, workspace_id, id),
    UNIQUE (tenant_id, workspace_id, slug),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT
);

-- [25] route_policy_revisions
CREATE TABLE app.route_policy_revisions (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    route_id UUID NOT NULL,
    revision_no INT NOT NULL CHECK (revision_no >= 1),
    source_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    destination_overrides_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    filter_ast_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    transform_rules_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    schedule_policy_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    fallback_policy TEXT NOT NULL DEFAULT 'HOLD_FOR_REVIEW' CHECK (
        fallback_policy IN ('STOP', 'SEND_WITHOUT_MEDIA', 'SEND_AUTHORIZED_ORIGINAL', 'FALLBACK_MODEL', 'HOLD_FOR_REVIEW')
    ),
    daily_post_cap INT NOT NULL DEFAULT 50 CHECK (daily_post_cap BETWEEN 1 AND 5000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, route_id, revision_no),
    FOREIGN KEY (tenant_id, route_id) REFERENCES app.routes(tenant_id, id) ON DELETE RESTRICT
);

-- [26] content_fingerprints (Exact SHA-256 & MinHash/SimHash near-duplicate tracking: RULE-05)
CREATE TABLE app.content_fingerprints (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    route_id UUID,
    content_item_id UUID NOT NULL,
    exact_hash_sha256 TEXT NOT NULL,
    simhash_64 BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, content_item_id) REFERENCES app.content_items(tenant_id, id) ON DELETE CASCADE
);

-- ============================================================================
-- 7. AI Studio, Model Discovery & Server-Derived Budget Ledger (Tables 27..33)
-- ============================================================================

-- [27] ai_providers (BYOK OpenAI, Google Gemini, Stock Media Search: AIP-01)
CREATE TABLE app.ai_providers (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    provider_type TEXT NOT NULL CHECK (provider_type IN ('OPENAI', 'GEMINI', 'COMPATIBLE_OPENAI', 'PEXELS_MEDIA')),
    display_name TEXT NOT NULL,
    base_url TEXT,
    secret_id UUID NOT NULL,
    key_fingerprint_sha256 TEXT NOT NULL,
    is_enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, secret_id) REFERENCES app.secrets_vault(tenant_id, id) ON DELETE RESTRICT
);

-- [28] ai_model_cache (Key-fingerprint bound online discovery cache: AIP-02, AIP-03)
CREATE TABLE app.ai_model_cache (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    provider_id UUID NOT NULL,
    model_id TEXT NOT NULL,
    key_fingerprint_sha256 TEXT NOT NULL,
    modalities_json JSONB NOT NULL DEFAULT '["TEXT"]'::jsonb,
    pricing_source TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (pricing_source IN ('PROVIDER_API', 'USER_CONFIGURED', 'UNKNOWN')),
    rank_basis TEXT NOT NULL DEFAULT 'HEURISTIC' CHECK (rank_basis IN ('BENCHMARK', 'HEURISTIC', 'MANUAL')),
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, provider_id, model_id, key_fingerprint_sha256),
    FOREIGN KEY (tenant_id, provider_id) REFERENCES app.ai_providers(tenant_id, id) ON DELETE CASCADE
);

-- [29] ai_routing_profiles (Independent task-type routing: TEXT, VISION, IMAGE, TTS, VIDEO: AIP-04..06)
CREATE TABLE app.ai_routing_profiles (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    task_modality TEXT NOT NULL CHECK (task_modality IN ('TEXT', 'VISION', 'IMAGE', 'TTS', 'VIDEO')),
    selection_mode TEXT NOT NULL DEFAULT 'MANUAL' CHECK (
        selection_mode IN ('MANUAL', 'AUTO_ECONOMY', 'AUTO_QUALITY')
    ),
    ordered_candidates_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    allow_cross_provider_fallback BOOLEAN NOT NULL DEFAULT false,
    max_attempts INT NOT NULL DEFAULT 2 CHECK (max_attempts BETWEEN 1 AND 5),
    timeout_sec INT NOT NULL DEFAULT 60 CHECK (timeout_sec BETWEEN 5 AND 600),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, workspace_id, task_modality),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE CASCADE
);

-- [30] budget_policies (v1.3.2 OC-01: Immutable OPERATOR_PLAN ceilings + Tenant TENANT_BUDGET sub-caps!)
CREATE TABLE app.budget_policies (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
    policy_origin TEXT NOT NULL CHECK (policy_origin IN ('OPERATOR_PLAN', 'TENANT_BUDGET')),
    scope_type TEXT NOT NULL CHECK (scope_type IN ('TENANT', 'WORKSPACE', 'ROUTE')),
    scope_ref_id UUID NOT NULL,
    period_kind TEXT NOT NULL CHECK (period_kind IN ('DAILY', 'MONTHLY', 'TOTAL')),
    max_cost_micros BIGINT NOT NULL CHECK (max_cost_micros >= 0),
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    CHECK (scope_type <> 'TENANT' OR scope_ref_id = tenant_id),
    CHECK (policy_origin <> 'OPERATOR_PLAN' OR scope_type = 'TENANT'),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, policy_origin, scope_type, scope_ref_id, period_kind)
);

-- [31] budget_reservations (Encapsulated under relayforge_ledger_owner: AIP-09, SEC-11)
CREATE TABLE app.budget_reservations (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    route_id UUID,
    idempotency_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('RESERVED', 'COMMITTED', 'RELEASED')),
    reserved_micros BIGINT NOT NULL CHECK (reserved_micros >= 0),
    settled_micros BIGINT CHECK (settled_micros IS NULL OR settled_micros >= 0),
    cost_accuracy TEXT NOT NULL DEFAULT 'ESTIMATED' CHECK (cost_accuracy IN ('ACTUAL_PROVIDER', 'ESTIMATED', 'UNKNOWN')),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, workspace_id, route_id) REFERENCES app.routes(tenant_id, workspace_id, id) ON DELETE RESTRICT
);

-- [32] budget_reservation_allocations
CREATE TABLE app.budget_reservation_allocations (
    reservation_id UUID NOT NULL,
    policy_id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    period_bucket TEXT NOT NULL,
    PRIMARY KEY (reservation_id, policy_id),
    FOREIGN KEY (tenant_id, reservation_id) REFERENCES app.budget_reservations(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, policy_id) REFERENCES app.budget_policies(tenant_id, id) ON DELETE RESTRICT
);

-- [33] ai_executions (Full audit of prompt modality, model, latency, error class & token usage: AIP-08, AIP-12, OPS-05)
CREATE TABLE app.ai_executions (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    provider_id UUID,
    reservation_id UUID,
    task_modality TEXT NOT NULL CHECK (task_modality IN ('TEXT', 'VISION', 'IMAGE', 'TTS', 'VIDEO')),
    model_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('IN_PROGRESS', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT')),
    http_status INT,
    error_category TEXT CHECK (error_category IN (
        'BAD_REQUEST', 'AUTH_INVALID', 'FORBIDDEN', 'MODEL_NOT_FOUND',
        'QUOTA_ZERO', 'RATE_LIMIT_RETRYABLE', 'UPSTREAM_5XX', 'TIMEOUT', 'SCHEMA_INVALID'
    )),
    input_tokens INT,
    output_tokens INT,
    latency_ms INT,
    correlation_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    completed_at TIMESTAMPTZ,
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, provider_id) REFERENCES app.ai_providers(tenant_id, id) ON DELETE SET NULL,
    FOREIGN KEY (tenant_id, reservation_id) REFERENCES app.budget_reservations(tenant_id, id) ON DELETE SET NULL
);

-- ============================================================================
-- 8. Content Generation, Fact Guard, Media Studio & Podcast (Tables 34..39)
-- ============================================================================

-- [34] generation_sessions (4 Freedom Modes + Fact Guard configuration: GEN-01..14)
CREATE TABLE app.generation_sessions (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    source_content_item_id UUID,
    style_profile_id UUID,
    pinned_style_revision_no INT,
    freedom_mode TEXT NOT NULL CHECK (freedom_mode IN ('STRICT', 'BALANCED', 'CREATIVE', 'UNRESTRICTED')),
    operation_kind TEXT NOT NULL CHECK (operation_kind IN (
        'NEW_FROM_IDEA', 'REWRITE', 'SUMMARIZE', 'EXPAND', 'TRANSLATE', 'COPYEDIT', 'PODCAST_SCRIPT'
    )),
    protected_facts_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_by_user_id UUID REFERENCES app.users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, source_content_item_id) REFERENCES app.content_items(tenant_id, id) ON DELETE SET NULL,
    FOREIGN KEY (tenant_id, style_profile_id) REFERENCES app.style_profiles(tenant_id, id) ON DELETE SET NULL
);

-- [35] generation_candidates (Candidate outputs + Fact Guard diff report: GEN-06..12)
CREATE TABLE app.generation_candidates (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    session_id UUID NOT NULL,
    ai_execution_id UUID,
    candidate_rank INT NOT NULL CHECK (candidate_rank >= 1),
    is_fallback_output BOOLEAN NOT NULL DEFAULT false,
    output_text TEXT NOT NULL,
    fact_guard_verdict TEXT NOT NULL CHECK (
        fact_guard_verdict IN ('PASSED', 'COSMETIC_DIFF_ONLY', 'FACT_MISMATCH_BLOCKED', 'NOT_APPLICABLE_UNRESTRICTED')
    ),
    fact_diff_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    quality_signals_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, session_id) REFERENCES app.generation_sessions(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, ai_execution_id) REFERENCES app.ai_executions(tenant_id, id) ON DELETE SET NULL
);

-- [36] media_assets (Stored in S3 under `tenants/<tenant_id>/media/<sha256>` with license metadata: MED-01..15)
CREATE TABLE app.media_assets (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    media_kind TEXT NOT NULL CHECK (media_kind IN (
        'IMAGE', 'GIF', 'VIDEO', 'AUDIO', 'VOICE_NOTE', 'STICKER_IMAGE', 'NATIVE_STICKER', 'INFOGRAPHIC', 'DOCUMENT'
    )),
    origin_kind TEXT NOT NULL CHECK (origin_kind IN (
        'USER_UPLOAD', 'SOURCE_INGESTED', 'STOCK_LICENSED', 'AI_GENERATED_IMAGE',
        'AI_EDITED_IMAGE', 'LOCAL_RENDERED_CLIP', 'AI_GENERATIVE_VIDEO', 'TTS_AUDIO'
    )),
    s3_object_key TEXT NOT NULL CHECK (position('..' IN s3_object_key) = 0),
    sha256_hex TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    byte_size BIGINT NOT NULL CHECK (byte_size > 0),
    width_px INT,
    height_px INT,
    duration_sec NUMERIC(10, 2),
    license_attribution_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_deleted BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT
);

-- [37] media_asset_refs (Prevents deleting active media referenced by drafts or queued publications: MED-13, BAK-08)
CREATE TABLE app.media_asset_refs (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    media_asset_id UUID NOT NULL,
    owner_entity_type TEXT NOT NULL CHECK (owner_entity_type IN ('CONTENT_ITEM', 'DRAFT_REVISION', 'PUBLICATION_VERSION')),
    owner_entity_id UUID NOT NULL,
    sort_order INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, media_asset_id, owner_entity_type, owner_entity_id),
    FOREIGN KEY (tenant_id, media_asset_id) REFERENCES app.media_assets(tenant_id, id) ON DELETE RESTRICT
);

-- [38] podcast_scripts (Editable 1/2-speaker script prior to TTS synthesis: AUD-01..05)
CREATE TABLE app.podcast_scripts (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    speaker_mode TEXT NOT NULL CHECK (speaker_mode IN ('SINGLE_SPEAKER', 'DUAL_SPEAKER')),
    target_duration_sec INT NOT NULL CHECK (target_duration_sec BETWEEN 15 AND 3600),
    segments_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    synthesized_media_asset_id UUID,
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT', 'APPROVED', 'SYNTHESIZING', 'READY', 'FAILED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, synthesized_media_asset_id) REFERENCES app.media_assets(tenant_id, id) ON DELETE SET NULL
);

-- [39] media_jobs (Async isolated `media-worker` AF_UNIX jobs & generative video polling: MED-11, MED-12, SEC-07)
CREATE TABLE app.media_jobs (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    job_kind TEXT NOT NULL CHECK (job_kind IN (
        'FFMPEG_TRANSCODE', 'LOCAL_SLIDESHOW_CLIP', 'INFOGRAPHIC_RENDER',
        'AI_IMAGE_GEN', 'AI_VIDEO_GEN_POLL', 'TTS_SYNTHESIS'
    )),
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (
        status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'TIMED_OUT', 'OOM_KILLED', 'CANCELLED')
    ),
    input_spec_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_media_asset_id UUID,
    reservation_id UUID,
    error_safe TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    completed_at TIMESTAMPTZ,
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, output_media_asset_id) REFERENCES app.media_assets(tenant_id, id) ON DELETE SET NULL,
    FOREIGN KEY (tenant_id, reservation_id) REFERENCES app.budget_reservations(tenant_id, id) ON DELETE SET NULL
);

-- ============================================================================
-- 9. Drafts, Immutable Publication Versions & Reliable Delivery Queue (Tables 40..45)
-- ============================================================================

-- [40] drafts
CREATE TABLE app.drafts (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    route_id UUID,
    source_content_item_id UUID,
    generation_session_id UUID,
    current_revision_no INT NOT NULL DEFAULT 1,
    approval_state TEXT NOT NULL DEFAULT 'EDITING' CHECK (
        approval_state IN ('EDITING', 'NEEDS_REVIEW', 'APPROVED', 'ARCHIVED')
    ),
    created_by_user_id UUID REFERENCES app.users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, route_id) REFERENCES app.routes(tenant_id, id) ON DELETE SET NULL,
    FOREIGN KEY (tenant_id, source_content_item_id) REFERENCES app.content_items(tenant_id, id) ON DELETE SET NULL,
    FOREIGN KEY (tenant_id, generation_session_id) REFERENCES app.generation_sessions(tenant_id, id) ON DELETE SET NULL
);

-- [41] draft_revisions
CREATE TABLE app.draft_revisions (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    draft_id UUID NOT NULL,
    revision_no INT NOT NULL CHECK (revision_no >= 1),
    title_text TEXT,
    body_text TEXT NOT NULL,
    entities_ast_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    brand_identity_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, draft_id, revision_no),
    FOREIGN KEY (tenant_id, draft_id) REFERENCES app.drafts(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, brand_identity_id) REFERENCES app.brand_identities(tenant_id, id) ON DELETE SET NULL
);

-- [42] publication_versions (Frozen snapshot of text, entities, media & policy prior to dispatch: PUB-07, PUB-08)
CREATE TABLE app.publication_versions (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    draft_id UUID,
    draft_revision_no INT,
    route_id UUID,
    route_policy_revision_no INT,
    publish_intent TEXT NOT NULL CHECK (publish_intent IN ('LIVE_PUBLISH', 'EXPLICIT_TEST_SEND', 'EVERGREEN_REPUBLISH')),
    frozen_payload_json JSONB NOT NULL,
    approved_by_user_id UUID REFERENCES app.users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, draft_id) REFERENCES app.drafts(tenant_id, id) ON DELETE SET NULL,
    FOREIGN KEY (tenant_id, route_id) REFERENCES app.routes(tenant_id, id) ON DELETE SET NULL
);

-- [43] delivery_tasks (Core reliable queue with lease_epoch fencing & stored runnable_at: QUE-01..12)
CREATE TABLE app.delivery_tasks (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    route_id UUID,
    destination_id UUID,
    publication_version_id UUID,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'SCHEDULED', 'READY', 'RESERVED', 'DISPATCHING_IN_FLIGHT',
        'SENT', 'RETRY_WAIT', 'FAILED', 'UNCERTAIN', 'SKIPPED', 'CANCELLED'
    )),
    scheduled_for TIMESTAMPTZ NOT NULL,
    next_retry_at TIMESTAMPTZ,
    runnable_at TIMESTAMPTZ GENERATED ALWAYS AS (COALESCE(next_retry_at, scheduled_for)) STORED,
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    lease_epoch BIGINT NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
    attempts INT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INT NOT NULL DEFAULT 4 CHECK (max_attempts BETWEEN 1 AND 100),
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    remote_message_id TEXT,
    last_error_class TEXT,
    last_error_safe TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    CHECK (status <> 'RETRY_WAIT' OR next_retry_at IS NOT NULL),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, workspace_id, idempotency_key),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, workspace_id, route_id) REFERENCES app.routes(tenant_id, workspace_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, destination_id) REFERENCES app.destinations(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, publication_version_id) REFERENCES app.publication_versions(tenant_id, id) ON DELETE RESTRICT
);

-- [44] delivery_attempts (Immutable per-attempt diagnostic log: QUE-09)
CREATE TABLE app.delivery_attempts (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    delivery_task_id UUID NOT NULL,
    attempt_no INT NOT NULL CHECK (attempt_no >= 1),
    lease_epoch BIGINT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('SENT', 'TRANSIENT_ERROR', 'PERMANENT_ERROR', 'QUOTA_EXHAUSTED', 'TIMEOUT_UNCERTAIN')),
    http_status INT,
    retry_after_sec INT,
    remote_message_id TEXT,
    error_safe TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    finished_at TIMESTAMPTZ,
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, delivery_task_id, attempt_no),
    FOREIGN KEY (tenant_id, delivery_task_id) REFERENCES app.delivery_tasks(tenant_id, id) ON DELETE RESTRICT
);

-- [45] reconciliation_records (Mandatory human/audited decision record to exit UNCERTAIN: QUE-08)
CREATE TABLE app.reconciliation_records (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    delivery_task_id UUID NOT NULL,
    task_lease_epoch BIGINT NOT NULL,
    resolution_action TEXT NOT NULL CHECK (resolution_action IN (
        'CONFIRM_SENT', 'CONFIRM_NOT_SENT_REQUEUE', 'DISCARD_DO_NOT_SEND'
    )),
    acknowledge_duplicate_risk BOOLEAN NOT NULL DEFAULT false CHECK (
        resolution_action <> 'CONFIRM_NOT_SENT_REQUEUE' OR acknowledge_duplicate_risk = true
    ),
    decided_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    UNIQUE (delivery_task_id, task_lease_epoch),
    FOREIGN KEY (tenant_id, delivery_task_id) REFERENCES app.delivery_tasks(tenant_id, id) ON DELETE RESTRICT
);

-- ============================================================================
-- 10. Preference Learning, Transactional Outbox, Audit & Operations (Tables 46..51)
-- ============================================================================

-- [46] feedback_events (Opt-in user edit feedback, never sent for hidden external fine-tuning: LRN-01, LRN-06)
CREATE TABLE app.feedback_events (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    brand_identity_id UUID,
    draft_id UUID,
    feedback_kind TEXT NOT NULL CHECK (feedback_kind IN (
        'TEXT_EDIT_DIFF', 'CANDIDATE_SELECTED', 'STYLE_CHANGED', 'MEDIA_REPLACED', 'REJECTED_WITH_REASON'
    )),
    diff_summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, brand_identity_id) REFERENCES app.brand_identities(tenant_id, id) ON DELETE SET NULL,
    FOREIGN KEY (tenant_id, draft_id) REFERENCES app.drafts(tenant_id, id) ON DELETE SET NULL
);

-- [47] learned_preference_rules (Scoped strictly to workspace + brand + destination; requires evidence >= min: LRN-02..05)
CREATE TABLE app.learned_preference_rules (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    brand_identity_id UUID,
    destination_id UUID,
    rule_statement TEXT NOT NULL,
    supporting_evidence_count INT NOT NULL DEFAULT 1 CHECK (supporting_evidence_count >= 1),
    review_state TEXT NOT NULL DEFAULT 'PROPOSED' CHECK (review_state IN ('PROPOSED', 'ACCEPTED', 'REJECTED', 'RESET')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, brand_identity_id) REFERENCES app.brand_identities(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, destination_id) REFERENCES app.destinations(tenant_id, id) ON DELETE CASCADE
);

-- [48] domain_outbox (Transactional outbox for reliable internal domain events)
CREATE TABLE app.domain_outbox (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    event_type TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE CASCADE
);

-- [49] audit_events (Immutable append-only security & operational audit trail: OPS-04)
CREATE TABLE app.audit_events (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID,
    actor_user_id UUID,
    actor_role TEXT NOT NULL,
    action_code TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    details_safe_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id) REFERENCES app.tenants(id) ON DELETE RESTRICT
);

-- [50] system_notifications (Deduplicated unread operational alerts via partial unique index: OPS-06)
CREATE TABLE app.system_notifications (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    dedup_key TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'ERROR', 'CRITICAL')),
    title_fa TEXT NOT NULL,
    body_fa TEXT NOT NULL,
    is_read BOOLEAN NOT NULL DEFAULT false,
    occurrence_count INT NOT NULL DEFAULT 1 CHECK (occurrence_count >= 1),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE CASCADE
);

-- [51] backup_manifests (Records cluster PITR checkpoints & tenant logical export / crypto-shredding events: BAK-01..08)
CREATE TABLE app.backup_manifests (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    backup_scope TEXT NOT NULL CHECK (backup_scope IN ('TENANT_SETTINGS_EXPORT', 'TENANT_FULL_LOGICAL', 'CLUSTER_PITR_MARKER')),
    cipher_alg TEXT NOT NULL CHECK (cipher_alg IN ('PG_BACKREST_AES256_HMAC', 'AGE_CHACHA20_POLY1305')),
    archive_checksum_sha256 TEXT NOT NULL,
    byte_size BIGINT NOT NULL CHECK (byte_size >= 0),
    safe_mode_on_restore BOOLEAN NOT NULL DEFAULT true CHECK (safe_mode_on_restore = true),
    status TEXT NOT NULL CHECK (status IN ('COMPLETED', 'VERIFIED', 'RESTORED', 'EXPIRED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id) REFERENCES app.tenants(id) ON DELETE RESTRICT
);

-- ============================================================================
-- 11. Performance & Integrity Indexes
-- ============================================================================
CREATE UNIQUE INDEX idx_notifications_unread_dedup
    ON app.system_notifications (tenant_id, workspace_id, dedup_key)
    WHERE is_read = false;

CREATE INDEX idx_content_revisions_fts_gin
    ON app.content_item_revisions USING GIN (fts_vector);

CREATE INDEX idx_delivery_fair_claim
    ON app.delivery_tasks (tenant_id, runnable_at ASC)
    WHERE status IN ('READY', 'SCHEDULED', 'RETRY_WAIT');

CREATE INDEX idx_delivery_reap_expired
    ON app.delivery_tasks (lease_expires_at ASC)
    WHERE status IN ('RESERVED', 'DISPATCHING_IN_FLIGHT');

CREATE INDEX idx_budget_policies_active_scope
    ON app.budget_policies (tenant_id, scope_type, scope_ref_id)
    WHERE is_active = true;

CREATE INDEX idx_budget_alloc_period
    ON app.budget_reservation_allocations (policy_id, period_bucket);

CREATE INDEX idx_domain_outbox_unpublished
    ON app.domain_outbox (tenant_id, created_at ASC)
    WHERE published_at IS NULL;

CREATE INDEX idx_audit_events_tenant_time
    ON app.audit_events (tenant_id, created_at DESC);

-- ============================================================================
-- 12. Least-Privilege Table & Column Grants
-- ============================================================================
REVOKE ALL ON ALL TABLES IN SCHEMA app FROM PUBLIC;

GRANT SELECT ON app.tenants, app.users, app.tenant_memberships, app.schema_migrations
    TO relayforge_operator, relayforge_web, relayforge_worker;
GRANT INSERT, UPDATE ON app.tenants, app.users, app.tenant_memberships, app.tenant_encryption_keys, app.backup_manifests
    TO relayforge_operator;
GRANT SELECT ON app.tenant_encryption_keys, app.backup_manifests TO relayforge_operator, relayforge_web, relayforge_worker;
GRANT INSERT ON app.backup_manifests TO relayforge_web;

GRANT SELECT (id) ON app.tenants TO relayforge_dispatcher_owner;
GRANT SELECT, INSERT, UPDATE, DELETE ON app.web_sessions, app.rate_limit_counters TO relayforge_web;
GRANT SELECT, INSERT, UPDATE, DELETE ON app.rate_limit_counters TO relayforge_worker;

-- Workspace & Content CRUD for web; worker gets bounded read/write on operational tables
GRANT SELECT, INSERT, UPDATE, DELETE ON
    app.workspaces, app.workspace_settings, app.secrets_vault, app.external_consents,
    app.brand_identities, app.sources, app.source_cursors, app.export_import_jobs,
    app.destinations, app.content_items, app.content_item_revisions,
    app.channel_analyses, app.channel_analysis_samples, app.style_profiles, app.style_revisions,
    app.routes, app.route_policy_revisions, app.content_fingerprints,
    app.ai_providers, app.ai_model_cache, app.ai_routing_profiles,
    app.generation_sessions, app.generation_candidates,
    app.media_assets, app.media_asset_refs, app.podcast_scripts, app.media_jobs,
    app.drafts, app.draft_revisions, app.publication_versions,
    app.feedback_events, app.learned_preference_rules, app.domain_outbox, app.system_notifications
    TO relayforge_web;

GRANT SELECT ON
    app.workspaces, app.workspace_settings, app.secrets_vault, app.external_consents,
    app.brand_identities, app.sources, app.destinations, app.style_profiles, app.style_revisions,
    app.routes, app.route_policy_revisions, app.ai_providers, app.ai_routing_profiles,
    app.publication_versions
    TO relayforge_worker;

GRANT SELECT, INSERT, UPDATE ON
    app.source_cursors, app.export_import_jobs, app.content_items, app.content_item_revisions,
    app.channel_analyses, app.channel_analysis_samples, app.content_fingerprints,
    app.ai_model_cache, app.ai_executions, app.generation_sessions, app.generation_candidates,
    app.media_assets, app.media_asset_refs, app.podcast_scripts, app.media_jobs,
    app.drafts, app.draft_revisions, app.delivery_attempts, app.domain_outbox, app.system_notifications
    TO relayforge_worker;

GRANT SELECT ON app.workspaces, app.routes TO relayforge_ledger_owner;
GRANT SELECT ON app.ai_executions TO relayforge_web;
GRANT SELECT ON app.delivery_attempts TO relayforge_web;

-- Delivery tasks & reconciliation: NO DELETE for any app role!
GRANT SELECT, INSERT, UPDATE ON app.delivery_tasks TO relayforge_web, relayforge_worker;
GRANT SELECT, INSERT ON app.reconciliation_records TO relayforge_web;
GRANT SELECT (id, tenant_id, status, scheduled_for, next_retry_at, runnable_at, lease_expires_at, lease_epoch, attempts, max_attempts),
      UPDATE (status, next_retry_at, lease_owner, lease_expires_at, lease_epoch, attempts, updated_at)
      ON app.delivery_tasks TO relayforge_dispatcher_owner;
GRANT SELECT, INSERT, UPDATE ON app.tenant_dispatch_fairness TO relayforge_dispatcher_owner;

-- Budget encapsulation (OC-01 + S4):
GRANT SELECT, INSERT, UPDATE, DELETE ON app.budget_policies TO relayforge_operator, relayforge_web;
GRANT SELECT ON app.budget_policies TO relayforge_worker;
GRANT SELECT, UPDATE ON app.budget_policies TO relayforge_ledger_owner;
GRANT SELECT ON app.budget_reservations, app.budget_reservation_allocations TO relayforge_operator, relayforge_web, relayforge_worker;
GRANT SELECT, INSERT, UPDATE ON app.budget_reservations TO relayforge_ledger_owner;
GRANT SELECT, INSERT ON app.budget_reservation_allocations TO relayforge_ledger_owner;

-- Append-only audit_events: SELECT + INSERT only (NO UPDATE, NO DELETE!)
GRANT SELECT, INSERT ON app.audit_events TO relayforge_operator, relayforge_web, relayforge_worker;

-- ============================================================================
-- 13. Universal ENABLE + FORCE ROW LEVEL SECURITY & Migrator Policies (51/51 Tables)
-- ============================================================================
DO $$
DECLARE
    v_tbl TEXT;
BEGIN
    FOREACH v_tbl IN ARRAY ARRAY[
        'tenants', 'users', 'tenant_memberships', 'web_sessions', 'tenant_encryption_keys',
        'tenant_dispatch_fairness', 'schema_migrations', 'rate_limit_counters',
        'workspaces', 'workspace_settings', 'secrets_vault', 'external_consents', 'brand_identities',
        'sources', 'source_cursors', 'export_import_jobs', 'destinations',
        'content_items', 'content_item_revisions', 'channel_analyses', 'channel_analysis_samples',
        'style_profiles', 'style_revisions', 'routes', 'route_policy_revisions', 'content_fingerprints',
        'ai_providers', 'ai_model_cache', 'ai_routing_profiles', 'budget_policies',
        'budget_reservations', 'budget_reservation_allocations', 'ai_executions',
        'generation_sessions', 'generation_candidates', 'media_assets', 'media_asset_refs',
        'podcast_scripts', 'media_jobs', 'drafts', 'draft_revisions', 'publication_versions',
        'delivery_tasks', 'delivery_attempts', 'reconciliation_records',
        'feedback_events', 'learned_preference_rules', 'domain_outbox', 'audit_events',
        'system_notifications', 'backup_manifests'
    ]
    LOOP
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY;', v_tbl);
        EXECUTE format('ALTER TABLE app.%I FORCE ROW LEVEL SECURITY;', v_tbl);
        EXECUTE format(
            'CREATE POLICY %I ON app.%I FOR ALL TO relayforge_migrator '
            'USING (pg_catalog.current_setting(''app.migration_mode'', true) = ''on'') '
            'WITH CHECK (pg_catalog.current_setting(''app.migration_mode'', true) = ''on'');',
            v_tbl || '_migrator_rls', v_tbl
        );
    END LOOP;
END;
$$;

-- Root & global table RLS policies
CREATE POLICY tenants_tenant_rls ON app.tenants
    FOR ALL TO relayforge_operator, relayforge_web, relayforge_worker
    USING (id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY users_scoped_rls ON app.users
    FOR ALL TO relayforge_operator, relayforge_web, relayforge_worker
    USING (
        id = NULLIF(pg_catalog.current_setting('app.user_id', true), '')::uuid
        OR EXISTS (
            SELECT 1 FROM app.tenant_memberships tm
            WHERE tm.user_id = app.users.id
              AND tm.tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid
        )
    )
    WITH CHECK (true);

CREATE POLICY schema_migrations_read_rls ON app.schema_migrations
    FOR SELECT TO relayforge_operator, relayforge_web, relayforge_worker USING (true);

-- Tenant-scoped RLS across all 47 `tenant_id`-bearing tables
DO $$
DECLARE
    v_tbl TEXT;
BEGIN
    FOREACH v_tbl IN ARRAY ARRAY[
        'tenant_memberships', 'web_sessions', 'tenant_encryption_keys', 'rate_limit_counters',
        'workspaces', 'workspace_settings', 'secrets_vault', 'external_consents', 'brand_identities',
        'sources', 'source_cursors', 'export_import_jobs', 'destinations',
        'content_items', 'content_item_revisions', 'channel_analyses', 'channel_analysis_samples',
        'style_profiles', 'style_revisions', 'routes', 'route_policy_revisions', 'content_fingerprints',
        'ai_providers', 'ai_model_cache', 'ai_routing_profiles', 'budget_policies',
        'budget_reservations', 'budget_reservation_allocations', 'ai_executions',
        'generation_sessions', 'generation_candidates', 'media_assets', 'media_asset_refs',
        'podcast_scripts', 'media_jobs', 'drafts', 'draft_revisions', 'publication_versions',
        'delivery_tasks', 'delivery_attempts', 'reconciliation_records',
        'feedback_events', 'learned_preference_rules', 'domain_outbox', 'audit_events',
        'system_notifications', 'backup_manifests'
    ]
    LOOP
        EXECUTE format(
            'CREATE POLICY %I ON app.%I FOR ALL '
            'TO relayforge_operator, relayforge_web, relayforge_worker, relayforge_ledger_owner '
            'USING (tenant_id = NULLIF(pg_catalog.current_setting(''app.tenant_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = NULLIF(pg_catalog.current_setting(''app.tenant_id'', true), '''')::uuid);',
            v_tbl || '_tenant_rls', v_tbl
        );
    END LOOP;
END;
$$;

-- Dispatcher Policies (Includes UNCERTAIN & FAILED in SELECT USING for UPDATE...RETURNING!)
CREATE POLICY dispatcher_tenants_select_rls ON app.tenants
    FOR SELECT TO relayforge_dispatcher_owner USING (true);

CREATE POLICY dispatcher_fairness_rls ON app.tenant_dispatch_fairness
    FOR ALL TO relayforge_dispatcher_owner USING (true) WITH CHECK (true);

CREATE POLICY dispatcher_select_rls ON app.delivery_tasks
    FOR SELECT TO relayforge_dispatcher_owner
    USING (status IN ('SCHEDULED', 'READY', 'RETRY_WAIT', 'RESERVED', 'DISPATCHING_IN_FLIGHT', 'UNCERTAIN', 'FAILED'));

CREATE POLICY dispatcher_update_rls ON app.delivery_tasks
    FOR UPDATE TO relayforge_dispatcher_owner
    USING (status IN ('SCHEDULED', 'READY', 'RETRY_WAIT', 'RESERVED', 'DISPATCHING_IN_FLIGHT'))
    WITH CHECK (status IN ('READY', 'RESERVED', 'FAILED', 'UNCERTAIN'));

-- ============================================================================
-- 14. Database Guard Triggers (State Machine, Plan Quota Origin & Immutable Audit)
-- ============================================================================
CREATE OR REPLACE FUNCTION app.trg_fn_protect_append_only_audit()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF CURRENT_USER = 'relayforge_migrator' AND pg_catalog.current_setting('app.migration_mode', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;
    RAISE EXCEPTION 'AUDIT_EVENTS_ARE_IMMUTABLE_APPEND_ONLY' USING ERRCODE = 'RF021';
END;
$$;

CREATE TRIGGER trg_protect_append_only_audit
BEFORE UPDATE OR DELETE ON app.audit_events
FOR EACH ROW EXECUTE FUNCTION app.trg_fn_protect_append_only_audit();

CREATE OR REPLACE FUNCTION app.trg_fn_enforce_budget_policy_origin()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_caller TEXT := CURRENT_USER;
    v_is_super BOOLEAN;
BEGIN
    IF v_caller = 'relayforge_migrator' AND pg_catalog.current_setting('app.migration_mode', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;

    SELECT r.rolsuper INTO v_is_super FROM pg_catalog.pg_roles r WHERE r.rolname = v_caller;
    IF COALESCE(v_is_super, false) AND COALESCE(pg_catalog.current_setting('app.break_glass_reason', true), '') <> '' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;

    IF v_caller = 'relayforge_operator' THEN
        IF (TG_OP IN ('UPDATE', 'DELETE') AND OLD.policy_origin <> 'OPERATOR_PLAN')
           OR (TG_OP IN ('INSERT', 'UPDATE') AND NEW.policy_origin <> 'OPERATOR_PLAN') THEN
            RAISE EXCEPTION 'OPERATOR_ROLE_ONLY_MANAGES_OPERATOR_PLAN' USING ERRCODE = 'RF007';
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;

    IF v_caller = 'relayforge_web' THEN
        IF (TG_OP IN ('UPDATE', 'DELETE') AND OLD.policy_origin = 'OPERATOR_PLAN')
           OR (TG_OP IN ('INSERT', 'UPDATE') AND NEW.policy_origin = 'OPERATOR_PLAN') THEN
            RAISE EXCEPTION 'TENANT_WEB_CANNOT_MUTATE_OPERATOR_PLAN_QUOTA' USING ERRCODE = 'RF007';
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;

    RAISE EXCEPTION 'UNAUTHORIZED_BUDGET_POLICY_DML_ROLE: %', v_caller USING ERRCODE = 'RF007';
END;
$$;

CREATE TRIGGER trg_enforce_budget_policy_origin
BEFORE INSERT OR UPDATE OR DELETE ON app.budget_policies
FOR EACH ROW EXECUTE FUNCTION app.trg_fn_enforce_budget_policy_origin();

CREATE OR REPLACE FUNCTION app.trg_fn_enforce_delivery_state_machine()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_caller TEXT := CURRENT_USER;
    v_is_super BOOLEAN;
    v_supplied_epoch BIGINT;
    v_now TIMESTAMPTZ := pg_catalog.clock_timestamp();
BEGIN
    IF v_caller = 'relayforge_migrator' AND pg_catalog.current_setting('app.migration_mode', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;

    SELECT r.rolsuper INTO v_is_super FROM pg_catalog.pg_roles r WHERE r.rolname = v_caller;
    IF COALESCE(v_is_super, false) AND COALESCE(pg_catalog.current_setting('app.break_glass_reason', true), '') <> '' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'DELIVERY_TASK_DELETE_FORBIDDEN' USING ERRCODE = 'RF010';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF v_caller NOT IN ('relayforge_web', 'relayforge_worker') THEN
            RAISE EXCEPTION 'UNAUTHORIZED_INSERT_ROLE: %', v_caller USING ERRCODE = 'RF020';
        END IF;
        IF NEW.status NOT IN ('PENDING', 'SCHEDULED', 'READY')
           OR NEW.lease_epoch <> 0
           OR NEW.attempts <> 0
           OR NEW.max_attempts > 10
           OR NEW.next_retry_at IS NOT NULL
           OR NEW.lease_owner IS NOT NULL
           OR NEW.lease_expires_at IS NOT NULL
           OR NEW.remote_message_id IS NOT NULL THEN
            RAISE EXCEPTION 'ILLEGAL_INITIAL_DELIVERY_TASK_STATE' USING ERRCODE = 'RF010';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.id <> OLD.id
       OR NEW.tenant_id <> OLD.tenant_id
       OR NEW.workspace_id <> OLD.workspace_id
       OR NEW.route_id IS DISTINCT FROM OLD.route_id
       OR NEW.idempotency_key <> OLD.idempotency_key THEN
        RAISE EXCEPTION 'IMMUTABLE_IDENTITY_COLUMNS' USING ERRCODE = 'RF010';
    END IF;

    IF v_caller = 'relayforge_dispatcher_owner' THEN
        IF NEW.lease_epoch <> OLD.lease_epoch + 1 THEN
            RAISE EXCEPTION 'DISPATCHER_MUST_INCREMENT_EPOCH' USING ERRCODE = 'RF011';
        END IF;

        IF OLD.status IN ('SCHEDULED', 'READY', 'RETRY_WAIT') AND NEW.status = 'RESERVED' THEN
            IF COALESCE(OLD.next_retry_at, OLD.scheduled_for) > v_now
               OR OLD.attempts >= OLD.max_attempts
               OR NEW.attempts <> OLD.attempts + 1
               OR NEW.next_retry_at IS NOT NULL
               OR NEW.lease_owner IS NULL
               OR NEW.lease_expires_at IS NULL THEN
                RAISE EXCEPTION 'DISPATCHER_CLAIM_INVARIANT_VIOLATION' USING ERRCODE = 'RF012';
            END IF;
            RETURN NEW;
        END IF;

        IF ((OLD.status = 'RESERVED' AND NEW.status IN ('READY', 'FAILED'))
            OR (OLD.status = 'DISPATCHING_IN_FLIGHT' AND NEW.status = 'UNCERTAIN')) THEN
            IF OLD.lease_expires_at IS NULL OR OLD.lease_expires_at >= v_now
               OR NEW.attempts <> OLD.attempts
               OR (OLD.status = 'RESERVED' AND NEW.status = 'READY' AND OLD.attempts >= OLD.max_attempts)
               OR (OLD.status = 'RESERVED' AND NEW.status = 'FAILED' AND OLD.attempts < OLD.max_attempts)
               OR NEW.lease_owner IS NOT NULL
               OR NEW.lease_expires_at IS NOT NULL THEN
                RAISE EXCEPTION 'DISPATCHER_REAP_INVARIANT_VIOLATION' USING ERRCODE = 'RF012';
            END IF;
            RETURN NEW;
        END IF;

        RAISE EXCEPTION 'ILLEGAL_DISPATCHER_TRANSITION: % -> %', OLD.status, NEW.status USING ERRCODE = 'RF012';
    END IF;

    IF v_caller = 'relayforge_worker' THEN
        IF NEW.lease_epoch <> OLD.lease_epoch OR NEW.attempts <> OLD.attempts OR NEW.max_attempts <> OLD.max_attempts THEN
            RAISE EXCEPTION 'WORKER_CANNOT_MUTATE_EPOCH_OR_ATTEMPTS' USING ERRCODE = 'RF013';
        END IF;

        IF OLD.status = 'PENDING' AND NEW.status IN ('SCHEDULED', 'READY', 'SKIPPED')
           AND NEW.lease_owner IS NULL AND NEW.lease_expires_at IS NULL
           AND NEW.remote_message_id IS NULL THEN
            RETURN NEW;
        END IF;

        IF OLD.status IN ('RESERVED', 'DISPATCHING_IN_FLIGHT') THEN
            v_supplied_epoch := NULLIF(pg_catalog.current_setting('app.lease_epoch', true), '')::bigint;
            IF v_supplied_epoch IS NULL OR v_supplied_epoch <> OLD.lease_epoch THEN
                RAISE EXCEPTION 'MISSING_OR_STALE_FENCING_EPOCH' USING ERRCODE = 'RF014';
            END IF;
            IF OLD.lease_expires_at IS NULL OR OLD.lease_expires_at < v_now THEN
                RAISE EXCEPTION 'WORKER_LEASE_EXPIRED' USING ERRCODE = 'RF015';
            END IF;

            IF NEW.status = OLD.status AND NEW.lease_owner = OLD.lease_owner
               AND NEW.lease_expires_at IS NOT NULL AND NEW.lease_expires_at > v_now THEN
                RETURN NEW;
            END IF;

            IF NEW.status = 'RETRY_WAIT' THEN
                IF OLD.attempts >= OLD.max_attempts THEN
                    RAISE EXCEPTION 'MAX_ATTEMPTS_EXCEEDED_MUST_FAIL' USING ERRCODE = 'RF016';
                END IF;
                IF NEW.next_retry_at IS NULL THEN
                    RAISE EXCEPTION 'RETRY_WAIT_REQUIRES_NEXT_RETRY_AT' USING ERRCODE = 'RF017';
                END IF;
            END IF;

            IF (OLD.status = 'RESERVED' AND NEW.status IN ('DISPATCHING_IN_FLIGHT', 'RETRY_WAIT', 'FAILED'))
               OR (OLD.status = 'DISPATCHING_IN_FLIGHT' AND NEW.status IN ('SENT', 'RETRY_WAIT', 'FAILED', 'UNCERTAIN')) THEN
                RETURN NEW;
            END IF;
        END IF;

        RAISE EXCEPTION 'ILLEGAL_WORKER_TRANSITION: % -> %', OLD.status, NEW.status USING ERRCODE = 'RF017';
    END IF;

    IF v_caller = 'relayforge_web' THEN
        IF OLD.status IN ('PENDING', 'SCHEDULED', 'READY') AND NEW.status = OLD.status
           AND NEW.lease_epoch = OLD.lease_epoch AND NEW.attempts = OLD.attempts
           AND NEW.max_attempts <= 10
           AND NEW.lease_owner IS NULL AND NEW.lease_expires_at IS NULL
           AND NEW.remote_message_id IS NULL THEN
            RETURN NEW;
        END IF;

        IF OLD.status = 'PENDING' AND NEW.status IN ('SCHEDULED', 'READY', 'SKIPPED')
           AND NEW.lease_epoch = OLD.lease_epoch AND NEW.attempts = OLD.attempts
           AND NEW.max_attempts <= 10
           AND NEW.lease_owner IS NULL AND NEW.lease_expires_at IS NULL
           AND NEW.remote_message_id IS NULL THEN
            RETURN NEW;
        END IF;

        IF OLD.status IN ('PENDING', 'SCHEDULED', 'READY', 'RETRY_WAIT') AND NEW.status = 'CANCELLED'
           AND NEW.lease_epoch = OLD.lease_epoch + 1 AND NEW.attempts = OLD.attempts
           AND NEW.max_attempts = OLD.max_attempts
           AND NEW.remote_message_id IS NOT DISTINCT FROM OLD.remote_message_id THEN
            RETURN NEW;
        END IF;

        IF OLD.status = 'UNCERTAIN' AND NEW.status IN ('SENT', 'READY', 'CANCELLED')
           AND NEW.lease_epoch = OLD.lease_epoch + 1 AND NEW.attempts = OLD.attempts
           AND NEW.max_attempts = (
               CASE WHEN NEW.status = 'READY' AND OLD.attempts >= OLD.max_attempts
                    THEN OLD.attempts + 1
                    ELSE OLD.max_attempts
               END
           ) THEN
            IF NOT EXISTS (
                SELECT 1 FROM app.reconciliation_records rr
                WHERE rr.delivery_task_id = OLD.id
                  AND rr.task_lease_epoch = OLD.lease_epoch
                  AND (
                      (rr.resolution_action = 'CONFIRM_SENT' AND NEW.status = 'SENT')
                      OR (rr.resolution_action = 'CONFIRM_NOT_SENT_REQUEUE' AND NEW.status = 'READY' AND rr.acknowledge_duplicate_risk = true)
                      OR (rr.resolution_action = 'DISCARD_DO_NOT_SEND' AND NEW.status = 'CANCELLED')
                  )
            ) THEN
                RAISE EXCEPTION 'UNCERTAIN_EXIT_REQUIRES_MATCHING_RECONCILIATION_RECORD' USING ERRCODE = 'RF018';
            END IF;
            RETURN NEW;
        END IF;

        RAISE EXCEPTION 'ILLEGAL_WEB_TRANSITION: % -> %', OLD.status, NEW.status USING ERRCODE = 'RF019';
    END IF;

    RAISE EXCEPTION 'UNAUTHORIZED_ROLE_FOR_DELIVERY_UPDATE: %', v_caller USING ERRCODE = 'RF020';
END;
$$;

CREATE TRIGGER trg_enforce_delivery_state_machine
BEFORE INSERT OR UPDATE OR DELETE ON app.delivery_tasks
FOR EACH ROW EXECUTE FUNCTION app.trg_fn_enforce_delivery_state_machine();

RESET ROLE;

-- ============================================================================
-- 15. Atomic Queue, Heartbeat, Reconciliation & Ledger SQL Functions
-- ============================================================================
CREATE OR REPLACE FUNCTION app.fn_worker_claim_next_jobs(
    p_worker_id TEXT,
    p_batch_size INT,
    p_lease_ttl_sec INT
)
RETURNS TABLE (claimed_task_id UUID, claimed_tenant_id UUID, claimed_epoch BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_limit INT;
    v_rem INT;
    v_ttl INTERVAL;
    v_now TIMESTAMPTZ := pg_catalog.clock_timestamp();
    v_tenants UUID[];
    v_next_tenants UUID[];
    v_exhausted UUID[] := ARRAY[]::UUID[];
    v_touched_tenants UUID[] := ARRAY[]::UUID[];
    v_tid UUID;
    v_idx INT;
    v_rem_tenants INT;
    v_share INT;
    v_got INT;
BEGIN
    IF p_worker_id IS NULL OR p_batch_size IS NULL OR p_lease_ttl_sec IS NULL THEN
        RAISE EXCEPTION 'NULL_ARGUMENT_FORBIDDEN' USING ERRCODE = '22004';
    END IF;

    v_limit := LEAST(GREATEST(p_batch_size, 1), 100);
    v_rem := v_limit;
    v_ttl := LEAST(GREATEST(p_lease_ttl_sec, 15), 600) * INTERVAL '1 second';

    WHILE v_rem > 0 LOOP
        SELECT ARRAY(
            SELECT t.id
            FROM app.tenants t
            LEFT JOIN app.tenant_dispatch_fairness f ON f.tenant_id = t.id
            WHERE NOT (t.id = ANY(v_exhausted))
              AND EXISTS (
                  SELECT 1 FROM app.delivery_tasks dt
                  WHERE dt.tenant_id = t.id
                    AND dt.status IN ('READY', 'SCHEDULED', 'RETRY_WAIT')
                    AND dt.runnable_at <= v_now
                    AND dt.attempts < dt.max_attempts
              )
            ORDER BY COALESCE(f.last_served_at, '-infinity'::timestamptz) ASC, t.id ASC
            LIMIT v_rem
        ) INTO v_tenants;

        IF COALESCE(pg_catalog.cardinality(v_tenants), 0) = 0 THEN
            EXIT;
        END IF;

        WHILE v_rem > 0 AND COALESCE(pg_catalog.cardinality(v_tenants), 0) > 0 LOOP
            v_next_tenants := ARRAY[]::UUID[];
            v_rem_tenants := pg_catalog.cardinality(v_tenants);

            FOR v_idx IN 1..v_rem_tenants LOOP
                EXIT WHEN v_rem <= 0;
                v_tid := v_tenants[v_idx];
                v_share := GREATEST((v_rem + (v_rem_tenants - v_idx)) / (v_rem_tenants - v_idx + 1), 1);

                RETURN QUERY
                WITH locked AS (
                    SELECT dt.id
                    FROM app.delivery_tasks dt
                    WHERE dt.tenant_id = v_tid
                      AND dt.status IN ('READY', 'SCHEDULED', 'RETRY_WAIT')
                      AND dt.runnable_at <= v_now
                      AND dt.attempts < dt.max_attempts
                    ORDER BY dt.runnable_at ASC
                    LIMIT v_share
                    FOR UPDATE OF dt SKIP LOCKED
                ),
                upd AS (
                    UPDATE app.delivery_tasks t
                    SET status = 'RESERVED',
                        next_retry_at = NULL,
                        lease_owner = p_worker_id,
                        lease_expires_at = v_now + v_ttl,
                        lease_epoch = t.lease_epoch + 1,
                        attempts = t.attempts + 1,
                        updated_at = v_now
                    FROM locked l
                    WHERE t.id = l.id
                    RETURNING t.id, t.tenant_id, t.lease_epoch
                )
                SELECT u.id, u.tenant_id, u.lease_epoch FROM upd u;

                GET DIAGNOSTICS v_got = ROW_COUNT;

                IF v_got > 0 THEN
                    v_rem := v_rem - v_got;
                    v_touched_tenants := pg_catalog.array_append(v_touched_tenants, v_tid);
                END IF;

                IF v_got = v_share AND v_rem > 0 THEN
                    v_next_tenants := pg_catalog.array_append(v_next_tenants, v_tid);
                ELSE
                    v_exhausted := pg_catalog.array_append(v_exhausted, v_tid);
                END IF;
            END LOOP;

            v_tenants := v_next_tenants;
        END LOOP;
    END LOOP;

    IF COALESCE(pg_catalog.cardinality(v_touched_tenants), 0) > 0 THEN
        INSERT INTO app.tenant_dispatch_fairness (tenant_id, last_served_at)
        SELECT d.tid, v_now
        FROM (
            SELECT DISTINCT u.tid
            FROM pg_catalog.unnest(v_touched_tenants) AS u(tid)
            ORDER BY u.tid ASC
        ) d
        ON CONFLICT (tenant_id) DO UPDATE
            SET last_served_at = EXCLUDED.last_served_at
            WHERE app.tenant_dispatch_fairness.last_served_at < v_now - INTERVAL '100 milliseconds';
    END IF;
END;
$$;

ALTER FUNCTION app.fn_worker_claim_next_jobs(TEXT, INT, INT) OWNER TO relayforge_dispatcher_owner;
REVOKE ALL ON FUNCTION app.fn_worker_claim_next_jobs(TEXT, INT, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_worker_claim_next_jobs(TEXT, INT, INT) TO relayforge_worker;

CREATE OR REPLACE FUNCTION app.fn_reap_expired_delivery_leases(p_batch_size INT)
RETURNS INT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_limit INT;
    v_now TIMESTAMPTZ := pg_catalog.clock_timestamp();
    v_count INT;
BEGIN
    IF p_batch_size IS NULL THEN
        RAISE EXCEPTION 'NULL_ARGUMENT_FORBIDDEN' USING ERRCODE = '22004';
    END IF;
    v_limit := LEAST(GREATEST(p_batch_size, 1), 200);

    WITH expired AS (
        SELECT id
        FROM app.delivery_tasks
        WHERE status IN ('RESERVED', 'DISPATCHING_IN_FLIGHT')
          AND lease_expires_at < v_now
        ORDER BY lease_expires_at ASC
        LIMIT v_limit
        FOR UPDATE SKIP LOCKED
    ),
    updated AS (
        UPDATE app.delivery_tasks t
        SET status = CASE
                WHEN t.status = 'DISPATCHING_IN_FLIGHT' THEN 'UNCERTAIN'
                WHEN t.attempts >= t.max_attempts THEN 'FAILED'
                ELSE 'READY'
            END,
            lease_owner = NULL,
            lease_expires_at = NULL,
            lease_epoch = t.lease_epoch + 1,
            updated_at = v_now
        FROM expired e
        WHERE t.id = e.id
        RETURNING t.id
    )
    SELECT count(*)::int INTO v_count FROM updated;
    RETURN v_count;
END;
$$;

ALTER FUNCTION app.fn_reap_expired_delivery_leases(INT) OWNER TO relayforge_dispatcher_owner;
REVOKE ALL ON FUNCTION app.fn_reap_expired_delivery_leases(INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_reap_expired_delivery_leases(INT) TO relayforge_worker;

CREATE OR REPLACE FUNCTION app.fn_worker_heartbeat_lease(
    p_task_id UUID,
    p_expected_epoch BIGINT,
    p_extend_sec INT
)
RETURNS TIMESTAMPTZ
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_new_exp TIMESTAMPTZ;
BEGIN
    IF p_task_id IS NULL OR p_expected_epoch IS NULL OR p_extend_sec IS NULL THEN
        RAISE EXCEPTION 'NULL_ARGUMENT_FORBIDDEN' USING ERRCODE = '22004';
    END IF;

    PERFORM pg_catalog.set_config('app.lease_epoch', p_expected_epoch::text, true);

    UPDATE app.delivery_tasks
    SET lease_expires_at = pg_catalog.clock_timestamp() + (LEAST(GREATEST(p_extend_sec, 15), 600) * INTERVAL '1 second'),
        updated_at = pg_catalog.clock_timestamp()
    WHERE id = p_task_id
      AND lease_epoch = p_expected_epoch
      AND status IN ('RESERVED', 'DISPATCHING_IN_FLIGHT')
    RETURNING lease_expires_at INTO v_new_exp;

    IF v_new_exp IS NULL THEN
        RAISE EXCEPTION 'HEARTBEAT_FENCING_OR_TASK_NOT_ACTIVE' USING ERRCODE = 'RF014';
    END IF;
    RETURN v_new_exp;
END;
$$;

ALTER FUNCTION app.fn_worker_heartbeat_lease(UUID, BIGINT, INT) OWNER TO relayforge_migrator;
REVOKE ALL ON FUNCTION app.fn_worker_heartbeat_lease(UUID, BIGINT, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_worker_heartbeat_lease(UUID, BIGINT, INT) TO relayforge_worker;

CREATE OR REPLACE FUNCTION app.fn_worker_transition_task(
    p_task_id UUID,
    p_expected_epoch BIGINT,
    p_new_status TEXT,
    p_remote_message_id TEXT DEFAULT NULL,
    p_next_retry_at TIMESTAMPTZ DEFAULT NULL
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_rows INT;
BEGIN
    IF p_task_id IS NULL OR p_expected_epoch IS NULL OR p_new_status IS NULL THEN
        RAISE EXCEPTION 'NULL_ARGUMENT_FORBIDDEN' USING ERRCODE = '22004';
    END IF;

    PERFORM pg_catalog.set_config('app.lease_epoch', p_expected_epoch::text, true);

    UPDATE app.delivery_tasks
    SET status = p_new_status,
        remote_message_id = COALESCE(p_remote_message_id, remote_message_id),
        next_retry_at = CASE WHEN p_new_status = 'RETRY_WAIT' THEN p_next_retry_at ELSE NULL END,
        lease_owner = CASE WHEN p_new_status IN ('SENT', 'RETRY_WAIT', 'FAILED', 'UNCERTAIN') THEN NULL ELSE lease_owner END,
        lease_expires_at = CASE WHEN p_new_status IN ('SENT', 'RETRY_WAIT', 'FAILED', 'UNCERTAIN') THEN NULL ELSE lease_expires_at END,
        updated_at = pg_catalog.clock_timestamp()
    WHERE id = p_task_id AND lease_epoch = p_expected_epoch;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows <> 1 THEN
        RAISE EXCEPTION 'FENCING_OR_TASK_NOT_FOUND' USING ERRCODE = 'RF014';
    END IF;
END;
$$;

ALTER FUNCTION app.fn_worker_transition_task(UUID, BIGINT, TEXT, TEXT, TIMESTAMPTZ) OWNER TO relayforge_migrator;
REVOKE ALL ON FUNCTION app.fn_worker_transition_task(UUID, BIGINT, TEXT, TEXT, TIMESTAMPTZ) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_worker_transition_task(UUID, BIGINT, TEXT, TEXT, TIMESTAMPTZ) TO relayforge_worker;

CREATE OR REPLACE FUNCTION app.fn_web_reconcile_uncertain_task(
    p_reconciliation_id UUID,
    p_task_id UUID,
    p_expected_epoch BIGINT,
    p_action TEXT,
    p_ack_duplicate BOOLEAN,
    p_decided_by TEXT,
    p_reason TEXT
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_tenant_id UUID := NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid;
    v_target_status TEXT;
    v_rows INT;
BEGIN
    IF p_reconciliation_id IS NULL OR p_task_id IS NULL OR p_expected_epoch IS NULL
       OR p_action IS NULL OR p_ack_duplicate IS NULL OR p_decided_by IS NULL OR p_reason IS NULL THEN
        RAISE EXCEPTION 'NULL_ARGUMENT_FORBIDDEN' USING ERRCODE = '22004';
    END IF;
    IF v_tenant_id IS NULL THEN
        RAISE EXCEPTION 'MISSING_TENANT_CONTEXT' USING ERRCODE = 'RF003';
    END IF;

    v_target_status := CASE p_action
        WHEN 'CONFIRM_SENT' THEN 'SENT'
        WHEN 'CONFIRM_NOT_SENT_REQUEUE' THEN 'READY'
        WHEN 'DISCARD_DO_NOT_SEND' THEN 'CANCELLED'
        ELSE NULL
    END;
    IF v_target_status IS NULL THEN
        RAISE EXCEPTION 'INVALID_RECONCILIATION_ACTION' USING ERRCODE = '22023';
    END IF;

    INSERT INTO app.reconciliation_records (
        id, tenant_id, delivery_task_id, task_lease_epoch,
        resolution_action, acknowledge_duplicate_risk, decided_by, reason
    ) VALUES (
        p_reconciliation_id, v_tenant_id, p_task_id, p_expected_epoch,
        p_action, p_ack_duplicate, p_decided_by, p_reason
    );

    UPDATE app.delivery_tasks
    SET status = v_target_status,
        max_attempts = CASE WHEN v_target_status = 'READY' AND attempts >= max_attempts THEN attempts + 1 ELSE max_attempts END,
        lease_epoch = lease_epoch + 1,
        updated_at = pg_catalog.clock_timestamp()
    WHERE id = p_task_id AND status = 'UNCERTAIN' AND lease_epoch = p_expected_epoch;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows <> 1 THEN
        RAISE EXCEPTION 'UNCERTAIN_TASK_OR_EPOCH_MISMATCH' USING ERRCODE = 'RF018';
    END IF;
END;
$$;

ALTER FUNCTION app.fn_web_reconcile_uncertain_task(UUID, UUID, BIGINT, TEXT, BOOLEAN, TEXT, TEXT) OWNER TO relayforge_migrator;
REVOKE ALL ON FUNCTION app.fn_web_reconcile_uncertain_task(UUID, UUID, BIGINT, TEXT, BOOLEAN, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_web_reconcile_uncertain_task(UUID, UUID, BIGINT, TEXT, BOOLEAN, TEXT, TEXT) TO relayforge_web;

CREATE OR REPLACE FUNCTION app.fn_budget_reserve(
    p_reservation_id UUID,
    p_workspace_id UUID,
    p_route_id UUID,
    p_idempotency_key TEXT,
    p_amount_micros BIGINT,
    p_ttl_sec INT
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_tenant_id UUID;
    v_existing RECORD;
    v_pol RECORD;
    v_locked_plan_policies INT := 0;
    v_bucket TEXT;
    v_used BIGINT;
    v_now TIMESTAMPTZ := pg_catalog.clock_timestamp();
BEGIN
    IF p_reservation_id IS NULL OR p_workspace_id IS NULL OR p_idempotency_key IS NULL
       OR p_amount_micros IS NULL OR p_ttl_sec IS NULL THEN
        RAISE EXCEPTION 'NULL_ARGUMENT_FORBIDDEN' USING ERRCODE = '22004';
    END IF;

    IF p_amount_micros < 0 THEN
        RAISE EXCEPTION 'NEGATIVE_AMOUNT_FORBIDDEN' USING ERRCODE = '22023';
    END IF;

    v_tenant_id := NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid;
    IF v_tenant_id IS NULL THEN
        RAISE EXCEPTION 'MISSING_TENANT_CONTEXT' USING ERRCODE = 'RF003';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM app.workspaces w WHERE w.id = p_workspace_id) THEN
        RAISE EXCEPTION 'INVALID_OR_UNAUTHORIZED_WORKSPACE' USING ERRCODE = 'RF002';
    END IF;

    IF p_route_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM app.routes r WHERE r.id = p_route_id AND r.workspace_id = p_workspace_id
    ) THEN
        RAISE EXCEPTION 'INVALID_OR_UNAUTHORIZED_ROUTE' USING ERRCODE = 'RF002';
    END IF;

    FOR v_pol IN
        SELECT bp.id, bp.policy_origin
        FROM app.budget_policies bp
        WHERE bp.is_active = true
          AND (
              (bp.scope_type = 'TENANT' AND bp.scope_ref_id = v_tenant_id)
              OR (bp.scope_type = 'WORKSPACE' AND bp.scope_ref_id = p_workspace_id)
              OR (p_route_id IS NOT NULL AND bp.scope_type = 'ROUTE' AND bp.scope_ref_id = p_route_id)
          )
        ORDER BY bp.id ASC
        FOR UPDATE
    LOOP
        IF v_pol.policy_origin = 'OPERATOR_PLAN' THEN
            v_locked_plan_policies := v_locked_plan_policies + 1;
        END IF;
    END LOOP;

    IF v_locked_plan_policies = 0 THEN
        RAISE EXCEPTION 'NO_ACTIVE_OPERATOR_PLAN_QUOTA_FOR_TENANT' USING ERRCODE = 'RF006';
    END IF;

    SELECT br.id, br.workspace_id, br.route_id, br.reserved_micros
    INTO v_existing
    FROM app.budget_reservations br
    WHERE br.tenant_id = v_tenant_id AND br.idempotency_key = p_idempotency_key;

    IF v_existing.id IS NOT NULL THEN
        IF v_existing.workspace_id <> p_workspace_id
           OR v_existing.route_id IS DISTINCT FROM p_route_id
           OR v_existing.reserved_micros <> p_amount_micros THEN
            RAISE EXCEPTION 'IDEMPOTENCY_PARAMETER_MISMATCH' USING ERRCODE = 'RF005';
        END IF;
        RETURN v_existing.id;
    END IF;

    FOR v_pol IN
        SELECT bp.id, bp.period_kind, bp.max_cost_micros
        FROM app.budget_policies bp
        WHERE bp.is_active = true
          AND (
              (bp.scope_type = 'TENANT' AND bp.scope_ref_id = v_tenant_id)
              OR (bp.scope_type = 'WORKSPACE' AND bp.scope_ref_id = p_workspace_id)
              OR (p_route_id IS NOT NULL AND bp.scope_type = 'ROUTE' AND bp.scope_ref_id = p_route_id)
          )
        ORDER BY bp.id ASC
    LOOP
        v_bucket := CASE v_pol.period_kind
            WHEN 'DAILY' THEN pg_catalog.to_char(v_now AT TIME ZONE 'UTC', 'YYYY-MM-DD')
            WHEN 'MONTHLY' THEN pg_catalog.to_char(v_now AT TIME ZONE 'UTC', 'YYYY-MM')
            ELSE 'TOTAL'
        END;

        SELECT COALESCE(SUM(
            CASE
                WHEN br.state = 'COMMITTED' THEN COALESCE(br.settled_micros, br.reserved_micros)
                WHEN br.state = 'RESERVED' AND br.expires_at > v_now THEN br.reserved_micros
                ELSE 0
            END
        ), 0) INTO v_used
        FROM app.budget_reservation_allocations bra
        JOIN app.budget_reservations br
          ON br.tenant_id = bra.tenant_id AND br.id = bra.reservation_id
        WHERE bra.policy_id = v_pol.id AND bra.period_bucket = v_bucket;

        IF (v_used + p_amount_micros) > v_pol.max_cost_micros THEN
            RAISE EXCEPTION 'BUDGET_EXCEEDED on policy % bucket % (used=%, req=%, max=%)',
                v_pol.id, v_bucket, v_used, p_amount_micros, v_pol.max_cost_micros
                USING ERRCODE = 'RF001';
        END IF;
    END LOOP;

    INSERT INTO app.budget_reservations (
        id, tenant_id, workspace_id, route_id, idempotency_key,
        state, reserved_micros, expires_at, created_at
    ) VALUES (
        p_reservation_id, v_tenant_id, p_workspace_id, p_route_id, p_idempotency_key,
        'RESERVED', p_amount_micros,
        v_now + (LEAST(GREATEST(p_ttl_sec, 10), 3600) * INTERVAL '1 second'),
        v_now
    );

    INSERT INTO app.budget_reservation_allocations (reservation_id, policy_id, tenant_id, period_bucket)
    SELECT p_reservation_id,
           bp.id,
           v_tenant_id,
           CASE bp.period_kind
               WHEN 'DAILY' THEN pg_catalog.to_char(v_now AT TIME ZONE 'UTC', 'YYYY-MM-DD')
               WHEN 'MONTHLY' THEN pg_catalog.to_char(v_now AT TIME ZONE 'UTC', 'YYYY-MM')
               ELSE 'TOTAL'
           END
    FROM app.budget_policies bp
    WHERE bp.is_active = true
      AND (
          (bp.scope_type = 'TENANT' AND bp.scope_ref_id = v_tenant_id)
          OR (bp.scope_type = 'WORKSPACE' AND bp.scope_ref_id = p_workspace_id)
          OR (p_route_id IS NOT NULL AND bp.scope_type = 'ROUTE' AND bp.scope_ref_id = p_route_id)
      );

    RETURN p_reservation_id;
END;
$$;

ALTER FUNCTION app.fn_budget_reserve(UUID, UUID, UUID, TEXT, BIGINT, INT) OWNER TO relayforge_ledger_owner;
REVOKE ALL ON FUNCTION app.fn_budget_reserve(UUID, UUID, UUID, TEXT, BIGINT, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_budget_reserve(UUID, UUID, UUID, TEXT, BIGINT, INT) TO relayforge_web, relayforge_worker;

CREATE OR REPLACE FUNCTION app.fn_budget_settle(
    p_reservation_id UUID,
    p_final_state TEXT,
    p_settled_micros BIGINT DEFAULT NULL
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_tenant_id UUID := NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid;
    v_res RECORD;
    v_alloc RECORD;
    v_other_used BIGINT;
    v_now TIMESTAMPTZ := pg_catalog.clock_timestamp();
BEGIN
    IF p_reservation_id IS NULL OR p_final_state IS NULL THEN
        RAISE EXCEPTION 'NULL_ARGUMENT_FORBIDDEN' USING ERRCODE = '22004';
    END IF;
    IF v_tenant_id IS NULL THEN
        RAISE EXCEPTION 'MISSING_TENANT_CONTEXT' USING ERRCODE = 'RF003';
    END IF;
    IF p_final_state NOT IN ('COMMITTED', 'RELEASED') THEN
        RAISE EXCEPTION 'INVALID_BUDGET_SETTLE_STATE' USING ERRCODE = '22023';
    END IF;
    IF p_final_state = 'COMMITTED' AND (p_settled_micros IS NULL OR p_settled_micros < 0) THEN
        RAISE EXCEPTION 'COMMITTED_REQUIRES_NON_NEGATIVE_SETTLED_MICROS' USING ERRCODE = '22023';
    END IF;

    FOR v_alloc IN
        SELECT bp.id
        FROM app.budget_reservation_allocations bra
        JOIN app.budget_policies bp ON bp.tenant_id = bra.tenant_id AND bp.id = bra.policy_id
        WHERE bra.reservation_id = p_reservation_id
        ORDER BY bp.id ASC
        FOR UPDATE OF bp
    LOOP
        NULL;
    END LOOP;

    SELECT br.id, br.state, br.reserved_micros, br.expires_at
    INTO v_res
    FROM app.budget_reservations br
    WHERE br.id = p_reservation_id
    FOR UPDATE;

    IF v_res.id IS NULL OR v_res.state <> 'RESERVED' THEN
        RAISE EXCEPTION 'RESERVATION_NOT_ACTIVE_OR_NOT_FOUND' USING ERRCODE = 'RF004';
    END IF;

    IF p_final_state = 'COMMITTED' AND (v_res.expires_at <= v_now OR p_settled_micros > v_res.reserved_micros) THEN
        FOR v_alloc IN
            SELECT bp.id AS policy_id, bp.max_cost_micros, bra.period_bucket
            FROM app.budget_reservation_allocations bra
            JOIN app.budget_policies bp ON bp.tenant_id = bra.tenant_id AND bp.id = bra.policy_id
            WHERE bra.reservation_id = p_reservation_id
            ORDER BY bp.id ASC
        LOOP
            SELECT COALESCE(SUM(
                CASE
                    WHEN br.state = 'COMMITTED' THEN COALESCE(br.settled_micros, br.reserved_micros)
                    WHEN br.state = 'RESERVED' AND br.expires_at > v_now THEN br.reserved_micros
                    ELSE 0
                END
            ), 0) INTO v_other_used
            FROM app.budget_reservation_allocations bra2
            JOIN app.budget_reservations br ON br.tenant_id = bra2.tenant_id AND br.id = bra2.reservation_id
            WHERE bra2.policy_id = v_alloc.policy_id
              AND bra2.period_bucket = v_alloc.period_bucket
              AND br.id <> p_reservation_id;

            IF (v_other_used + p_settled_micros) > v_alloc.max_cost_micros THEN
                RAISE EXCEPTION 'LATE_OR_OVERAGE_SETTLEMENT_EXCEEDS_CAP on policy %', v_alloc.policy_id
                    USING ERRCODE = 'RF004';
            END IF;
        END LOOP;
    END IF;

    UPDATE app.budget_reservations
    SET state = p_final_state,
        settled_micros = CASE WHEN p_final_state = 'COMMITTED' THEN p_settled_micros ELSE 0 END
    WHERE id = p_reservation_id;
END;
$$;

ALTER FUNCTION app.fn_budget_settle(UUID, TEXT, BIGINT) OWNER TO relayforge_ledger_owner;
REVOKE ALL ON FUNCTION app.fn_budget_settle(UUID, TEXT, BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_budget_settle(UUID, TEXT, BIGINT) TO relayforge_web, relayforge_worker;
