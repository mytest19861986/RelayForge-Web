# Codex Work Order `WO-03` (Phase `M2`): Messenger Adapters, SeaweedFS S3v4 Presigned Upload, Hostile-JSON Streaming Ingestion (`OC-04`) & Canonical `UTF-16` Rich-Text AST

- **Project:** RelayForge Web SaaS (`v2.0.0`)
- **Repository:** `https://github.com/mytest19861986/RelayForge-Web`
- **Base Branch:** `main` (includes merged Phase `M0` + `M1` commit `6364467`)
- **Target Feature Branch / PR:** `feat/m2-messengers-s3-export-and-utf16-content` -> open Pull Request to `main`
- **Roadmap Coverage:** Phase `M2` (`WO-06` + `WO-07` + `WO-08` in `PROJECT_MASTER_ROADMAP.md`, Modules `M03` & `M04`)
- **Authoritative Contracts (DO NOT MODIFY):**
  - `RelayForge_Architecture/contracts/database_schema.sql`
  - `RelayForge_Architecture/contracts/openapi.yaml`
  - `RelayForge_Architecture/contracts/style_package.schema.json`
  - `RelayForge_Architecture/13_PUBLICATION_ENGINE.md`
  - `RelayForge_Architecture/14_MESSENGER_INTEGRATIONS.md`

---

## 1. Sandbox Execution & Zero-Bloat Rules (MANDATORY)

1. **Native Linux Sandbox Approved:**
   - Native `PostgreSQL 16.x` in the Codex Linux sandbox is pre-approved whenever Docker/`containerd` is unavailable. Run all unit + integration tests locally against PostgreSQL 16 (and the deterministic S3v4 local server fixture; in `.github/workflows/ci-m0.yml` you may optionally add a lightweight `chrislusf/seaweedfs` S3 service container or test both!). Once green, push and open the PR immediately without stopping.
2. **Strict Zero-Bloat Dependency Rule (NO `boto3` / `botocore`!):**
   - `botocore` adds ~100 MB of unused AWS service JSON descriptors. Do **NOT** add `boto3`, `botocore`, `minio`, `telethon`, or `pyrogram`!
   - Implement AWS Signature Version 4 (`S3v4` Presigned POST policy + `HEAD`/`GET`/`PUT`/`DELETE` object + S3 Bucket Lifecycle XML `PUT /<bucket>?lifecycle`) directly in clean Python (`hmac`, `hashlib`, `urllib.parse`, `httpx`) in ~120 lines!
   - The **only** new dependency allowed in `pyproject.toml` for `M2` is:
     - `ijson>=3.3,<4.0` (uses fast `yajl2_c` C backend on Linux wheels).
   - Keep `RelayForge_Architecture/` and `PROJECT_MASTER_ROADMAP.md` **100% untouched** (`0` diff).

---

## 2. Exact Scope of Phase `M2` (`M03` + `M04`)

### 2.1 Forward Migration `migrations/0003_m2_webhook_lookup_and_export_claim.sql`
Register `0003_m2_webhook_lookup_and_export_claim` in `MIGRATIONS` inside `src/relayforge/adapters/db_migrator.py` while keeping `51/51` (or `100%` of `app.*` tables) `FORCE RLS` and **`0` non-superuser `BYPASSRLS` roles (`OC-10`)**:
1. **Inbound Webhook Lookup under `FORCE RLS`:**
   - Inbound Bot API webhooks (`POST /api/v1/webhooks/inbound/{platform}/{webhookToken}`) arrive unauthenticated without a session cookie.
   - Add an optional `webhook_token_sha256 TEXT UNIQUE` column on `app.sources` (via `ALTER TABLE app.sources ADD COLUMN IF NOT EXISTS webhook_token_sha256 TEXT UNIQUE;`) OR resolve via `relayforge_auth_owner` `SECURITY DEFINER` function `app.fn_resolve_inbound_webhook_source(p_source_kind TEXT, p_webhook_token_sha256 TEXT)` returning `(tenant_id UUID, workspace_id UUID, source_id UUID, canonical_channel_id TEXT, secret_id UUID)` only when `sources.is_active = true` and `tenants.status = 'ACTIVE'`! Grant narrow `SELECT` on `app.sources` to `relayforge_auth_owner` + `FOR SELECT TO relayforge_auth_owner USING (true)` policy, and `GRANT EXECUTE` to `relayforge_web`.
2. **Worker Export-Job Claim Function (for `ingestor-worker`):**
   - Create `SECURITY DEFINER` function `app.fn_worker_claim_export_job()` (owned by `relayforge_dispatcher_owner` with narrow `SELECT/UPDATE` grant & RLS policy on `app.export_import_jobs` where `status = 'UPLOADING'` / queued — note exact `CHECK` constraint on `app.export_import_jobs.status` in `database_schema.sql` line 289: `('UPLOADING', 'PARSING', 'COMPLETED', 'FAILED', 'EXPIRED_CLEANED')`! When `/complete` is called by web, transition `UPLOADING -> PARSING` or have worker process `PARSING` jobs safely with `FOR UPDATE SKIP LOCKED`, returning `(job_id, tenant_id, workspace_id, source_id, s3_object_key, file_size_bytes, job_mode)`).

---

### 2.2 Pure Canonical `UTF-16` Rich-Text AST, Compilers & Safe `{body}` Template (`src/relayforge/core/rich_text.py` — Pure `core/`!)
Keep `src/relayforge/core/` 100% framework-free (`lint-imports` & `check_ast_gates.py` enforced):
1. **Unicode Normalization & Exact `UTF-16` Code-Unit Indexing (`FMT-01`, `AT-04`):**
   - Normalize `plain_text` to `unicodedata.normalize("NFC", text)` while **strictly preserving** Persian half-space `ZWNJ` (`\u200c`), directional marks if needed, newlines, and multi-codepoint ZWJ/skin-tone emojis (`👨‍👩‍👧‍👦`, `🏳️‍🌈`, `🔥`).
   - Implement exact `utf16_len(s: str) -> int` (`len(s.encode("utf-16-le")) // 2`) and UTF-16 code-unit <-> Python codepoint slice mapping.
   - Validate every `FormatSpanUtf16(offset, length, kind, url)`:
     - `offset >= 0`, `length >= 1`, `offset + length <= utf16_len(plain_text)`.
     - **Surrogate-pair boundary guard:** reject any span whose `offset` or `offset + length` splits a UTF-16 surrogate pair in the encoded `utf-16-le` buffer (`InvalidUtf16SpanBoundaryError`)!
     - If `kind == "LINK"`, require `url` with scheme strictly in `{"http", "https", "tg"}` (reject `javascript:`, `data:`, `file:` — `FMT-05`, `AT-20`).
2. **Safe Single-`{body}` Placeholder Template (`SafePlaceholderTemplate` — `FMT-06`, `AT-20`):**
   - Enforce the exact contract regex from `RelayForge_Architecture/contracts/style_package.schema.json`:
     `^(?:[^{}]|\{(?:title|brand|destination|hashtags|source_credit)\})*\{body\}(?:[^{}]|\{(?:title|brand|destination|hashtags|source_credit)\})*$`
   - Reject missing `{body}`, duplicate `{body}`, unclosed braces, `{{7*7}}`, `{__class__}`, `{body.__init__}`, or any unknown placeholder.
   - Render deterministically while **shifting `FormatSpanUtf16` offsets** of `{body}` by the exact UTF-16 prefix length (and optionally bolding the first non-empty line when `bold_first_line=True`!).
3. **Per-Destination Capability Matrix & Format Compilers (`FMT-02..08`, `DST-05`):**
   - Define `DestinationCapabilityProfile` defaults per platform (`TELEGRAM`, `BALE`, `EITAA`, `RUBIKA_EXPERIMENTAL`, `SOROUSH_EXPERIMENTAL`): max text length (`4096`), max caption length (`1024`), native album support (`True` for Telegram/Bale, `False` for Eitaa), supported span kinds, max metadata parts (`30` for Rubika).
   - **No silent truncation (`FMT-08`):** if rendered UTF-16/char length exceeds target text/caption limit, return an explicit `length_overflow: True` + deterministic paragraph-aware split preview in `CompilationResult` rather than silently chopping text!
   - Platform outputs:
     - `TELEGRAM`: `plain_text` + Telegram Bot API `entities` (`offset`, `length` in UTF-16, `type`, `url`).
     - `BALE`: HTML-escaped text (`<`, `>`, `&`, `"`) with safe tags (`<b>`, `<i>`, `<code>`, `<pre>`, `<a href="...">`) — zero raw unescaped HTML (`FMT-07`, `AT-20`) and zero stray raw `*` Markdown stars (`AT-04`), plus `degradations: list[str]` for unsupported span kinds (e.g., `SPOILER`, `UNDERLINE`).
     - `EITAA`: plain text (links expanded safely as `text (url)` if needed) + explicit `degradations` list + album policy check (`SEQUENTIAL_POSTS` vs `CANCEL_FOR_INCOMPATIBLE_DEST` — `FMT-04`).
     - `RUBIKA_EXPERIMENTAL`: `plain_text` + `meta_data_parts` (`from_index`, `length` in UTF-16, `type`); if spans > `30`, report/degrade deterministically without crashing.
     - `SOROUSH_EXPERIMENTAL`: plain text / safe caption + `EXPERIMENTAL` tier notice.

---

### 2.3 Clean-Room Messenger Adapters (`src/relayforge/adapters/messengers.py` — `M03`, `ADR-011`)
All outbound HTTP calls MUST route through `SafeDialer` (`M1` anti-SSRF + secret scrubbing — never `httpx.get/post` directly!):
- **`TelegramBotAdapter`:**
  - `test_connection(token)` -> `POST /bot<token>/getMe` (verifies bot identity ONLY; never claims channel publish permission — `DST-02`).
  - `resolve_and_verify_source_channel(token, chat_ref)` -> `POST /bot<token>/getChat`, normalizing numeric `-100...` `canonical_channel_id` + `display_title` (`SRC-07`).
  - `fetch_live_updates(token, offset)` -> `POST /bot<token>/getUpdates`; explicitly detects `HTTP 409 Conflict` (shared bot polling/webhook collision -> raises `BotPollingConflictError` — `SRC-02`); maps `has_protected_content: true` -> `access_class = 'PROTECTED_NO_FORWARD'` (`SRC-08`) and non-downloadable Bot API references -> `'REFERENCE_ONLY'` (`SRC-04`).
  - `send_message(token, chat_id, compiled_payload)` -> `POST /bot<token>/sendMessage`, returning `remote_message_id` and classifying errors into the 6 canonical error classes (`TRANSIENT_NETWORK`, `RATE_LIMIT_429` with `retry_after_sec`, `QUOTA_EXHAUSTED`, `AUTH_REVOKED`, `PERMISSION_DENIED`, `INCOMPATIBLE_PAYLOAD`).
- **`BaleBotAdapter`** (`https://tapi.bale.ai/bot<token>/...`), **`EitaayarAdapter`** (`https://eitaayar.ir/api/<token>/sendMessage` — `OUTBOUND_LIMITED`: `test_connection` validates token format/probe honestly and `test_publish` sends explicit user-confirmed test message), **`RubikaV3Adapter`** (`RUBIKA_EXPERIMENTAL`), and **`SoroushPlusCleanRoomAdapter`** (`SOROUSH_EXPERIMENTAL` — 100% clean-room JSON over `SafeDialer`, zero GPLv3 code).

---

### 2.4 `SeaweedFS` S3v4 Presigned POST & Hostile-JSON Streaming Export Ingestor (`src/relayforge/adapters/s3_storage.py` & `src/relayforge/workers/export_ingestor.py` — **Closes `OC-04` & `OC-09`!**)
1. **Pure-Python S3v4 Client & Presigner (`s3_storage.py`):**
   - Configurable endpoint (`RF_S3_ENDPOINT_URL`, `RF_S3_PUBLIC_UPLOAD_URL`, `RF_S3_BUCKET`, `RF_S3_ACCESS_KEY`, `RF_S3_SECRET_KEY`, `RF_S3_REGION="us-east-1"`).
   - `generate_export_presigned_post(tenant_id, export_job_id, max_bytes=272_629_760, expires_in_sec=900)`:
     - Object key: `f"tenants/{tenant_id}/exports/{export_job_id}.json"` (strictly no `..`).
     - Policy `conditions` MUST include:
       1) `{"bucket": bucket}` (AND include `"bucket": bucket` explicitly in `form_fields` for strict S3v4 parsers like SeaweedFS/Garage!)
       2) `["starts-with", "$key", f"tenants/{tenant_id}/exports/"]`
       3) `["content-length-range", 1, max_bytes]` (`272_629_760`)
       4) `{"x-amz-algorithm": "AWS4-HMAC-SHA256"}`, `{"x-amz-credential": ...}`, `{"x-amz-date": ...}`.
   - `ensure_exports_lifecycle_24h()` — issues signed S3 `PUT /<bucket>?lifecycle` XML rule expiring prefix `tenants/` exports or `exports/` / `Rule` prefix after `1` day (`24h` cleanup safety net — `OC-04`).
   - `head_object`, `stream_object_chunks(key, chunk_size=65536)`, and `delete_object(key)`.
2. **`HostileJsonStreamGuard` & `S3ExportStreamIngestor` (`export_ingestor.py` — Closing `OC-04`):**
   - Wraps the raw `64 KiB` byte iterator **before** `ijson.items(..., "messages.item")`:
     - **Magic-byte bomb guard:** inspect first 8 bytes; immediately reject `gzip` (`\x1f\x8b`), `zip` (`PK\x03\x04`), `bzip2` (`BZh`), `xz` (`\xfd7zXZ\x00`), `zstd` (`\x28\xb5\x2f\xfd`), or non-UTF-8 binary blobs (`HostileExportStreamError("COMPRESSED_OR_BINARY_UPLOAD_FORBIDDEN")`).
     - **Hard byte-ceiling guard:** count total streamed bytes; abort the instant `total_bytes > 272_629_760` (`HostileExportStreamError("EXPORT_MAX_BYTES_EXCEEDED")`).
     - **Lexer-level depth & token-length guard (`O(1)` RAM):** scan chunks statefully (tracking `in_string` & `escaped` across chunk boundaries!):
       - Reject if `{` / `[` nesting depth exceeds **`MAX_JSON_DEPTH = 32`** (`HostileExportStreamError("JSON_MAX_DEPTH_EXCEEDED")`).
       - Reject if any single string literal or unquoted scalar token exceeds **`MAX_TOKEN_BYTES = 262_144` (`256 KiB`)** (`HostileExportStreamError("JSON_MAX_TOKEN_BYTES_EXCEEDED")`) **before** `ijson` allocates memory for it!
   - Streams valid Telegram Desktop `result.json` `messages.item` entries via `ijson`:
     - Skips service/deleted/empty messages safely (`messages_skipped += 1` — `SRC-03`).
     - Maps `job_mode == 'ARCHIVE_INGEST'` -> `origin_type = 'EXPORT_ARCHIVE'`, `operation_scope = 'INGEST_PIPELINE'`; maps `job_mode == 'CHANNEL_ANALYSIS_ONLY'` -> `origin_type = 'ANALYSIS_SAMPLE_ONLY'`, `operation_scope = 'ANALYSIS_ISOLATED'` (`SRC-10`: structural isolation from delivery pipeline!).
     - Converts Telegram `text` / `text_entities` into `CanonicalRichText` (`plain_text` NFC + `spans_utf16`) and batch-inserts idempotently (`ON CONFLICT (tenant_id, workspace_id, canonical_channel_id, external_message_id, operation_scope) DO NOTHING`) into `app.content_items` + `app.content_item_revisions` (`revision_no = 1`) as `relayforge_worker` under `SET LOCAL app.tenant_id`!
     - Updates `app.source_cursors` (`last_external_message_id`) and `app.export_import_jobs` (`status = 'COMPLETED'` or `'FAILED'` with safe error code).
     - **Deterministic Raw S3 Deletion (`finally:`):** ALWAYS deletes the raw S3 object `s3_object_key` and sets `raw_deleted_at = clock_timestamp()` whether parsing succeeded or failed (`SRC-03`)!
     - Captures `baseline_rss_mib`, `peak_rss_mib`, `delta_rss_mib` via Linux `/proc/self/status` (`VmRSS`) / `resource.getrusage`.

---

### 2.5 Phase `M2` Web API Endpoints (`src/relayforge/web/` — Matching `openapi.yaml`)
Add the Phase `M2` endpoints cleanly (keeping files modular, e.g., `src/relayforge/web/routes_m2.py` mounted on `api`):
1. `POST /api/v1/webhooks/inbound/{platform}/{webhookToken}` (`auth=None`, `platform in {"telegram", "bale"}`):
   - Resolves source via `app.fn_resolve_inbound_webhook_source`, sets `SET LOCAL app.tenant_id`, decrypts `WEBHOOK_SIGNING_SECRET` (if configured) or verifies `X-Telegram-Bot-Api-Secret-Token` via `hmac.compare_digest`, normalizes inbound channel post into `app.content_items` (`origin_type='LIVE_BOT_UPDATE'`, `operation_scope='INGEST_PIPELINE'`) + `app.content_item_revisions` + `app.source_cursors` idempotently (`SRC-02`, `SRC-05`).
2. `GET /api/v1/workspaces/{wsId}/sources` & `POST /api/v1/workspaces/{wsId}/sources`:
   - Reject any attempt to pass MTProto user-session kind (`422 ProblemDetails` — `SRC-01` Web SaaS rule).
   - Validate `secret_id` belongs to `(tenant_id, wsId)` when provided; generate & return one-time webhook token (storing only SHA-256 hash) for `WEBHOOK_INBOUND`.
3. `POST /api/v1/workspaces/{wsId}/sources/{sourceId}/test`:
   - Decrypts bound bot secret in memory via `EnvelopeSecretVault`, calls connector `resolve_and_verify_source_channel`, updates `canonical_channel_id`, `last_health_status`, `last_checked_at`, and audits.
4. `POST /api/v1/workspaces/{wsId}/sources/{sourceId}/exports/presign` (requires `Idempotency-Key` UUID header):
   - Validates `source_kind == 'TELEGRAM_DESKTOP_EXPORT'` and `file_size_bytes <= 272_629_760`, creates `app.export_import_jobs` (`status='UPLOADING'`), returns `201 PresignExportResponse` (`export_job_id`, `upload_url`, `form_fields` with explicit `bucket` & `key`, `max_bytes=272629760`, `expires_in_sec`).
5. `POST /api/v1/workspaces/{wsId}/sources/{sourceId}/exports/{exportJobId}/complete`:
   - Verifies object exists in S3 (`head_object` size `<= 272_629_760`), transitions `app.export_import_jobs.status = 'PARSING'`, audits, returns `202 AckResponse`.
6. `GET /api/v1/workspaces/{wsId}/content-items`:
   - Cursor-paginated list joining latest `content_item_revisions`, supporting optional `q` full-text search (`cir.fts_vector @@ plainto_tsquery('simple', %s)`), `access_class` filter, and `operation_scope` filter (defaulting to `'INGEST_PIPELINE'` so `'ANALYSIS_ISOLATED'` channel samples never mix with pipeline items — `SRC-10`).
7. `GET / POST /api/v1/workspaces/{wsId}/destinations`, `PATCH .../destinations/{destId}`, `POST .../destinations/{destId}/purge-queue`, `POST .../destinations/{destId}/test-connection`, `POST .../destinations/{destId}/test-publish`:
   - Enforce `backlog_mode = 'NEW_ONLY'` default (`DST-04`) and `maturity_tier = 'EXPERIMENTAL'` on `RUBIKA_EXPERIMENTAL` / `SOROUSH_EXPERIMENTAL`.
   - `purge-queue`: cancels waiting `delivery_tasks` (`status IN ('PENDING','SCHEDULED','READY','RETRY_WAIT')`) for `destId` by setting `status = 'CANCELLED', lease_epoch = lease_epoch + 1` (satisfying `trg_fn_enforce_delivery_state_machine`!).
   - `test-connection` vs `test-publish`: strictly separated (`DST-02`); `test-publish` requires `Idempotency-Key` + `explicit_user_confirmation: True` (`DST-07`), sends real test post via adapter, updates `publish_verified_at`, and never leaks raw tokens on error (`DST-08`).

---

## 3. Mandatory Non-Vacuous Test Suite (`M0 + M1 + M2`)

1. **Unit Tests (`tests/unit/test_m2_rich_text_templates_and_adapters.py`):**
   - **Persian + ZWNJ + Surrogate Emoji `UTF-16` Property/Edge Tests (`AT-04`):**
     - Verify `utf16_len("سلام\u200cدنیا 👨‍👩‍👧‍👦 تست")` and span compilation across Telegram `entities`, Bale escaped HTML (`<script>alert(1)</script>` -> `&lt;script&gt;...`, zero raw `*`), Eitaa degradation + album fallback report, and Rubika `<= 30` `meta_data_parts`.
     - Verify splitting a surrogate pair (`offset` landing on low surrogate) raises `InvalidUtf16SpanBoundaryError`, and `javascript:alert(1)` link raises validation error.
     - Verify length overflow (`> 4096` / `> 1024`) is flagged without silent truncation (`FMT-08`).
   - **`SafePlaceholderTemplate` Security Tests (`FMT-06`, `AT-20`):**
     - Rejects `"{title}"` (missing `{body}`), `"{body} {body}"` (duplicate `{body}`), `"{{7*7}} {body}"`, `"{__class__} {body}"`, `"{body.upper} "`!
     - Renders valid `"{title}\n\n{body}\n{brand}"` while shifting `{body}` `FormatSpanUtf16` offsets accurately.
   - **Messenger Adapter & Error Classification Tests (`SRC-02`, `DST-02`, `DST-08`):**
     - Mocked transport via `SafeDialer`: verify Telegram `409 Conflict` -> `BotPollingConflictError`, `429` with `retry_after: 35` -> `RATE_LIMIT_429 (retry_after_sec=35)`, token scrubbing on `401`/`500` errors, `has_protected_content: true` -> `PROTECTED_NO_FORWARD`, and separation of `test-connection` vs `test-publish`.

2. **Integration & `OC-04` Security/Memory Suite (`tests/integration/test_m2_s3_export_and_ingestion.py`):**
   - Spin up a strict local S3v4-verifying HTTP test server (verifying AWS4-HMAC-SHA256 signature, `bucket` field, `starts-with $key`, `content-length-range`, and `PUT ?lifecycle` XML!) + real PostgreSQL 16 (`relayforge_web` + `relayforge_worker` roles!):
   - **Cross-Tenant S3 Policy Enforcement (`SPK-08`):**
     - Presign export upload for `Tenant A` -> attempt upload using `Tenant A`'s signed form with tampered `key = "tenants/<tenant_b>/exports/spoof.json"` -> rejected (`HTTP 400/403`)!
   - **Closing `OC-04` Completely (Oversize, Hostile JSON, Bombs, Lifecycle & `delta_rss <= 128 MiB`):**
     - **Oversize upload rejection:** upload exceeding `content-length-range` (or simulated oversize stream `> 272_629_760` bytes) -> rejected deterministically!
     - **Compressed bomb rejection:** upload starting with `gzip` (`\x1f\x8b`) or `zip` (`PK\x03\x04`) header -> rejected immediately (`COMPRESSED_OR_BINARY_UPLOAD_FORBIDDEN`) and raw S3 object deleted!
     - **Deeply nested hostile JSON (`depth = 60 > 32`):** `[` * 60 -> rejected (`JSON_MAX_DEPTH_EXCEEDED`), job marked `FAILED` safely, raw S3 object deleted!
     - **Giant scalar token (`1 MiB` single string `> 256 KiB`):** rejected (`JSON_MAX_TOKEN_BYTES_EXCEEDED`) in `O(1)` memory before `ijson` buffers it, raw S3 object deleted!
     - **Truncated / malformed JSON:** job marked `FAILED` cleanly, raw S3 object deleted!
     - **Streaming scale & RSS delta check (`OC-04`):** stream a multi-megabyte synthetic Telegram export (`>= 15,000` Persian + emoji messages including deleted/service items and `ANALYSIS_ISOLATED` vs `INGEST_PIPELINE` scopes), verify exact deduplication (`SRC-05`: live webhook + export of same `external_message_id` produces **1** row in `content_items`, while `ANALYSIS_ISOLATED` stays isolated from default `/content-items` — `SRC-10`), verify GIN full-text search via `GET /content-items?q=...`, verify raw S3 object is deleted (`raw_deleted_at IS NOT NULL`), verify `ensure_exports_lifecycle_24h()` configures 1-day expiration, and assert **`delta_rss_mib <= 128.0`**!

---

## 4. Verification & PR Checklist

1. `ruff check .`
2. `lint-imports`
3. `python scripts/gates/check_ast_gates.py`
4. `pytest -v tests/unit tests/integration` (all `M0 + M1 + M2` suites green, `51/51 FORCE RLS`, `0` non-superuser `BYPASSRLS`, `4/4` SQL mutants killed!)
5. Confirm `0` diff on `RelayForge_Architecture/` and `PROJECT_MASTER_ROADMAP.md`, and push branch `feat/m2-messengers-s3-export-and-utf16-content` + open PR to `main`.
