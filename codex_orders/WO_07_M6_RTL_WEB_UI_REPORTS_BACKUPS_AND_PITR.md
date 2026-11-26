# Work Order `WO-07` — Phase `M6`: Persian RTL Web Studio, Safe Reporting, Immutable Audit, Tenant Safe-Mode Backups & Authenticated Cluster PITR (`OC-03`)

- **Target Repository:** `https://github.com/mytest19861986/RelayForge-Web`
- **Base Branch:** `main` (includes merged `M0..M5` through `PR #6`)
- **Feature Branch to Create & Push:** `feat/m6-rtl-web-ui-reports-backups-and-pitr`
- **Architecture & Contract References (Read-Only — DO NOT MODIFY):**
  - `RelayForge_Architecture/17_UI_UX_ARCHITECTURE.md`
  - `RelayForge_Architecture/19_RELIABILITY_AND_RECOVERY.md`
  - `RelayForge_Architecture/08_API_ARCHITECTURE.md`
  - `RelayForge_Architecture/15_SECURITY_ARCHITECTURE.md`
  - `RelayForge_Architecture/contracts/backup_manifest.schema.json`
  - `RelayForge_Architecture/contracts/openapi.yaml`
  - `RelayForge_Architecture/contracts/database_schema.sql`
  - `PROJECT_MASTER_ROADMAP.md` (`WO-19`, `WO-20`, `WO-21`, `OC-03`, `DEC-02`)

---

## 0. Strict Guardrails & Pre-Approved Sandbox Execution Rules

1. **Read-Only Blueprint Rule:** Do **NOT** modify any file inside `RelayForge_Architecture/` or `PROJECT_MASTER_ROADMAP.md` (`0` diff required).
2. **Zero-Bloat Frontend & Dependency Policy:**
   - Do **NOT** introduce `node_modules`, npm/webpack/vite build artifacts, external CDN scripts/fonts, `boto3`, or GPL/AGPL packages. Keep runtime dependencies strictly permissive (`MIT / BSD / Apache-2.0 / PostgreSQL`) and minimal (reuse existing `Django`, `django-ninja`, `psycopg`, `argon2-cffi`, `cryptography`, `httpx`, `ijson`, `Pillow`).
   - Keep tracked repository size well below `15 MB` (`< 2 MB` expected); never commit binary archives, `.rfbak` dumps, SQLite/PG data dirs, or evidence logs (`tmp/` stays git-ignored).
3. **Database & Security Invariants:**
   - Preserve `51/51 ENABLE + FORCE ROW LEVEL SECURITY`, `0` non-superuser `BYPASSRLS` (`OC-10`), `CONN_MAX_AGE = 0`, `prepare_threshold = None` (`OC-05`), and AST gates (`scripts/gates/check_ast_gates.py`: `core/` purity + zero direct `on_commit` aliases `OC-06`).
   - Tenant logical backup/restore MUST execute strictly inside tenant-scoped RLS (`SET LOCAL app.tenant_id`) with **zero `BYPASSRLS`**.
4. **Sandbox Pre-Approval (No Unnecessary Pauses):**
   - Running native Linux `PostgreSQL 16` (`pg_ctl`, `initdb`, `pg_basebackup`, `psql`) inside your sandbox environment when Docker/containerd is unavailable is **explicitly pre-approved**; GitHub Actions (`ubuntu-24.04`) runs containerized verification automatically on PR push. Proceed all the way to `git push` and opening the Pull Request against `main` without pausing.

---

## 1. Deliverables & Technical Requirements

### A. Close M5 Automated Route Media & `SEND_WITHOUT_MEDIA` Remainder (`MED-14`, `AUT-05`)
1. In `src/relayforge/web/routes_m3.py` and `src/relayforge/web/auto_orchestrator.py`:
   - Support explicit route `media_policy` inside `transform_rules_json` for `AI` and `HYBRID` automated runs:
     - `'HOLD_MEDIA_FOR_REVIEW'` (safe default when source item carries `media_manifest_json` and no automated media policy is attested),
     - `'KEEP_AUTHORIZED_SOURCE_MEDIA'` (forwards authorized source media references when `republish_rights_attested = true` and `access_class = 'PROCESSABLE'`),
     - `'ATTACH_WORKSPACE_ASSET'` (attaches a configured non-deleted workspace `media_asset_id` — e.g. generated infographic/image/audio asset — with active `media_asset_refs` tracking and destination capability compilation),
     - `'STRIP_MEDIA'` (text-only rewrite).
   - Implement full execution of `fallback_policy == 'SEND_WITHOUT_MEDIA'`: when automated media resolution/attachment fails or destination/media step cannot attach media while rewritten text + `AutoPublishRiskGate` pass (with `has_media=False` for the stripped text fallback), strip media attachments cleanly, record `media_fallback_applied: "SEND_WITHOUT_MEDIA"` in `auto.route.run` event/audit metadata, and proceed per `automation_level` instead of dead-ending.

### B. Zero-Bloat Persian RTL Web Studio UI (`17_UI_UX_ARCHITECTURE.md`, `WO-19 + WO-20`)
1. Serve the complete RTL Persian Web Studio directly from Django (`GET /` and `/app/...` in `src/relayforge/web/urls.py` + modular HTML/CSS/vanilla-ES-module assets under `src/relayforge/web/static/relayforge_ui/`):
   - `<html lang="fa" dir="rtl">`, CSS logical properties (`margin-inline`, `padding-inline`, `border-inline-start`, `inset-inline`), system/permissive Persian font stack (`Vazirmatn`, `Tahoma`, `sans-serif`), and bidirectional isolation (`<bdi dir="ltr">` / `unicode-bidi: isolate`) for `@channel` handles, URLs, model IDs, SHAs, and JSON snippets.
   - **Browser Secret Hygiene:** Zero tokens, API keys, or CSRF secrets in `localStorage` or `sessionStorage` (only non-sensitive UI theme/drawer state allowed); session lives exclusively in the `HttpOnly` cookie and `csrf_token` is held only in volatile JS module memory.
   - **Zero-Friction Entry (`APP-01`):** After `/api/v1/auth/login`, automatically enter the user's `default_workspace_id` dashboard without forcing port/infrastructure setup dialogs, while providing a clean top-bar Workspace Switcher.
   - **Stateless Live Updates:** Incremental background polling against `/api/v1/workspaces/{wsId}/events/poll` updating job progress, notifications, and queue counters without holding long DB connections.
2. Implement all **12 progressive-disclosure operational views** (`17_UI_UX_ARCHITECTURE.md` §2):
   1. **Dashboard (`نمای کلی`):** Engine status, real DB counters (zero fake percentages), dual-tier AI budget meter (`OPERATOR_PLAN` read-only ceiling vs tenant sub-budgets), deduplicated unread alerts, and prominent **Emergency Stop (`توقف اضطراری`)** control.
   2. **Sources & Export Wizard (`منابع و استخراج آرشیو`):** Bot source registration + 2-step Presigned S3 Telegram Desktop JSON upload wizard with progress & Bot API limitation notice.
   3. **Destinations & Capability Matrix (`مقصدها و ماتریس قابلیت`):** Maturity badges (`فعال GA` for Telegram/Bale, `فقط ارسال خروجی` for Eitaa, orange `آزمایشی — نیازمند تست زنده` for Rubika & Soroush+) and distinct buttons for **«تست فنی اتصال»** (`test-connection`) vs **«ارسال پست آزمایشی واقعی»** (`test-publish`).
   4. **Routes, Rules & `Dry-Run` (`قوانین، مسیرها و شبیه‌سازی`):** 4-mode route designer (`COPY / RULES / AI / HYBRID`), `Asia/Tehran` schedule & catch-up policy, visual filter builder, and step-by-step `Dry-Run` trace viewer.
   5. **Content Studio (`استودیو تولید محتوا`):** Clean dual-pane source/candidate editor, 4 Freedom Mode selector (`STRICT`, `BALANCED`, `CREATIVE`, `UNRESTRICTED`), `Fact Guard` diff highlighter, and collapsible **«تنظیمات پیشرفته»** drawer for model/routing overrides.
   6. **Channel Intelligence (`کانال‌شناس هوشمند`):** 4 sampling counters, 80/20 statistical & style analysis report, and separate stripped third-party identity review panel.
   7. **Style Library (`کتابخانه الگوها`):** Search, immutable revision history, weighted multi-style composition, style test, and secret-free JSON import/export.
   8. **Media & Podcast Studio (`استودیو رسانه و پادکست`):** Consent-gated stock photo search with attribution, Persian infographic table builder, sticker builder, local `FFmpeg` slideshow composer (clearly distinguished from generative AI video), and single/two-speaker podcast script editor requiring explicit script approval prior to TTS synthesis.
   9. **AI, Consents & Budgets (`تنظیمات هوش مصنوعی و بودجه`):** Masked-only BYOK vault hints, 5-modality model probe/catalog, granular `external_consents` toggles, read-only `OPERATOR_PLAN` ceilings, and tenant sub-budgets.
   10. **Drafts, Multi-Destination Preview & Publish (`پیش‌نویس، پیش‌نمایش مقصد و انتشار`):** Per-platform compiled preview with capability degradation/album-split warnings + honest disclaimer that web preview does not replace native client rendering, and visually/confirmationally separated actions: **«ذخیره پیش‌نویس»**, **«ارسال آزمایشی واقعی»**, and **«انتشار نهایی»** (`explicit_user_confirmation=true`).
   11. **Delivery Queue & `UNCERTAIN` Workbench (`صف تحویل و میز آشتی UNCERTAIN`):** Status filter and dedicated `UNCERTAIN` reconciliation form requiring mandatory duplicate-risk acknowledgement checkbox (`acknowledge_duplicate_risk=true`) before `CONFIRM_NOT_SENT_REQUEUE`.
   12. **Operations, Audit & Encrypted Backups (`عملیات، ممیزی و پشتیبان مستأجر`):** Immutable audit viewer, `CSV/JSON` report export, safe operational reset, encrypted `.rfbak` export & Safe-Mode restore (preview + apply), and owner tenant deletion with explicit 14-day PITR retention disclosure.

### C. Operations, CSV-Safe Reports, Deduplicated Notifications, Cleanup & Safe Reset (`M10`, `OPS-01..07`, `BAK-08`)
1. **Migration `migrations/0007_m6_ops_and_backups.sql` (registered in `db_migrator.py`):**
   - Extend `app.backup_manifests` `cipher_alg` check constraint to permit `'AES_256_GCM'` alongside `'PG_BACKREST_AES256_HMAC'` and `'AGE_CHACHA20_POLY1305'`, and grant `UPDATE (status)` on `app.backup_manifests` to `relayforge_web` so tenant restore verification/completion is recorded cleanly without weakening RLS or audit immutability.
2. **Operational Endpoints (mounted on `/api/v1`):**
   - `GET /workspaces/{wsId}/dashboard`: Real DB operational snapshot (sources, destinations, active routes, drafts by `approval_state`, deliveries by `status` including `UNCERTAIN`, active/queued media jobs, unread notification count, `emergency_stop`, disk pressure state, and dual-tier `OPERATOR_PLAN` vs tenant budget usage).
   - `GET /workspaces/{wsId}/notifications` & `POST /workspaces/{wsId}/notifications/{notificationId}/read`: Paginated notifications + mark-read, backed by a deduplicating alert helper using `idx_notifications_unread_dedup` (`ON CONFLICT (tenant_id, workspace_id, dedup_key) WHERE is_read = false DO UPDATE SET occurrence_count = app.system_notifications.occurrence_count + 1, last_seen_at = clock_timestamp()`) (`OPS-06`), plus disk-space threshold check (`85% WARNING`, `95% CRITICAL` blocking new export/media ingest when exceeded — `APP-06`).
   - `GET /workspaces/{wsId}/audit-events`: Cursor-paginated read-only query over `app.audit_events` (`OPS-04`).
   - `GET /workspaces/{wsId}/reports/ai-usage`: Aggregated AI token/cost ledger report by provider, model, modality, route, and `cost_accuracy` (`ACTUAL_PROVIDER`, `ESTIMATED`, `UNKNOWN`) (`OPS-05`).
   - `GET /workspaces/{wsId}/reports/export?format=csv|json`: Secret-scrubbed operational delivery & AI summary export (`OPS-03`). In `csv` mode, deterministically neutralize **CSV / Spreadsheet Formula Injection** on every cell whose raw or leading-whitespace-trimmed value starts with `=`, `+`, `-`, `@`, `\t`, or `\r` (prefixing `'`) while also running `scrub_sensitive_text`.
   - `POST /workspaces/{wsId}/maintenance/cleanup`: Cleans expired raw export staging objects and soft-deletes/removes S3 objects **only** for unreferenced `media_assets` (`0` rows in `app.media_asset_refs` and not referenced by non-deleted drafts/publications) (`BAK-08`).
   - `POST /workspaces/{wsId}/ops/reset`: Requires `manage_workspaces` permission + `Idempotency-Key` + `backup_password`; atomically generates a pre-reset encrypted tenant logical backup first, disables workspace routes (`is_enabled=false`) and destinations (`is_enabled=false`), cancels waiting `delivery_tasks` (`PENDING/SCHEDULED/READY/RETRY_WAIT`), and audits `OPS_SAFE_RESET` with `remote_deletion_performed=False` (`OPS-07`).

### D. Tenant Encrypted Backup & Safe-Mode Restore + Authenticated Cluster `PITR` Closing `OC-03` (`BAK-01..07`, `AT-19`)
1. **Tenant-Scoped Logical Backup & Safe-Mode Restore (`src/relayforge/adapters/tenant_backup.py` + `/workspaces/{wsId}/backups` & `/workspaces/{wsId}/backups/restore`):**
   - `POST /workspaces/{wsId}/backups`: Executes strictly under `SET LOCAL app.tenant_id` (`0` `BYPASSRLS`), derives a 256-bit key from caller-supplied `backup_password` via `Argon2id` (`memory_kib >= 65536`, `iterations >= 3`, `parallelism >= 1`), encrypts each logical JSONL/config/media/secret entry with `AES-256-GCM` (unique nonce + SHA-256 digests), validates the manifest strictly against `RelayForge_Architecture/contracts/backup_manifest.schema.json`, seals the `.rfbak` bundle with outer authenticated `AES-256-GCM` envelope tag, stores it under `tenants/{tenant_id}/backups/{manifest_id}.rfbak` (or returns/accepts inline base64 bundle as well), records `app.backup_manifests` (`safe_mode_on_restore=true`), and includes `pitr_retention_disclosure_days: 14`.
   - When `includes_reencrypted_secrets=true`, active `secrets_vault` entries are decrypted in memory using the source tenant's `Tenant DEK` and immediately re-encrypted under the backup-derived AEAD key inside `secrets/tenant_vault_rekeyed.aead` (never written in plaintext!).
   - `POST /workspaces/{wsId}/backups/restore`:
     - **Step 1 (Cryptographic & Bomb Gate First):** Verify outer & per-entry `AES-256-GCM` authentication tags **before** applying any state. Reject wrong password, 1-bit tampered ciphertext/manifest, any `logical_path` violating `backup_manifest.schema.json`'s strict relative regex (`../`, `/`, backslash, symlink), or any archive exceeding `limits.max_compression_ratio <= 50` / byte / entry caps (`AT-19, AT-20`).
     - **Step 2 (`mode="PREVIEW"`):** Return deterministic dry-run impact preview (row counts per logical table, secret re-key count, Safe-Mode enforcement flags) with **zero database writes**.
     - **Step 3 (`mode="APPLY"`):** Inside an atomic `SET LOCAL app.tenant_id` transaction, restore logical records, re-wrap any included secrets in memory under the destination tenant's active `Tenant DEK` (`BAK-04`), update/insert `app.backup_manifests` (`status='RESTORED'`), and **unconditionally enforce Safe Mode** (`routes.is_enabled = false`, `destinations.is_enabled = false`, `workspace_settings.allow_external_ai = false`, `external_consents.is_granted = false`) so zero automatic outbound messages or external AI calls can fire after restore (`BAK-03..05`, `AT-19`)!
2. **Cluster-Wide Authenticated `PITR` & Measured `RTO` Drill (`deploy/pitr/` — Closing `OC-03` 100%):**
   - Provide `deploy/pitr/pgbackrest.conf` (authenticated `pgBackRest` configuration for PostgreSQL 16 Base + continuous WAL archiving, `DEC-02` 14-day retention), `deploy/pitr/pitr_aead_archive.py` (authenticated chunked `AES-256-GCM` CLI/library sealer & verifier for `pg_basebackup` tarballs and `archive_command` / `restore_command` WAL segments — **zero unauthenticated `openssl enc -cbc` anywhere!**), and `deploy/pitr/RECOVERY_RUNBOOK.md` (documenting `SPK-09` `max_connections` / `max_worker_processes` / `max_locks_per_transaction` parity invariants and `DEC-02` `RPO <= 5m, RTO <= 30m`).
   - Add an automated integration test in `tests/integration/test_m6_ui_reports_backups_and_pitr.py` that runs a **real PostgreSQL 16 physical PITR drill**:
     1. Creates/uses a temporary local PostgreSQL 16 cluster with `wal_level=replica` and `archive_mode=on` piping archived WAL segments through `pitr_aead_archive.py`,
     2. Takes a real `pg_basebackup` sealed through `pitr_aead_archive.py`, inserts post-basebackup tenant data, forces WAL segment switch (`pg_switch_wal()`),
     3. Proves that flipping 1 byte in either the encrypted basebackup OR an encrypted WAL segment raises cryptographic authentication failure **before** PostgreSQL recovery starts,
     4. Restores the authentic encrypted basebackup + replays the encrypted WAL archive (`restore_command`) to recover the post-basebackup rows on a fresh data directory, measures actual wall-clock `rto_seconds` (`assert rto_seconds > 0 and rto_seconds <= 1800`), and writes non-vacuous `OC-06` structured evidence to git-ignored `tmp/m6-oc03-pitr-evidence.json`!

---

## 2. Mandatory Verification Suite Before Opening PR

Update `.github/workflows/ci-m0.yml` if needed (e.g., ensuring `postgresql-client` / PostgreSQL 16 binaries `pg_basebackup`/`pg_ctl` are on `PATH` for the `OC-03` PITR integration test) and verify all gates pass:

1. `ruff check .` -> `0` errors.
2. `lint-imports` -> `0` broken contracts (`core/` pure).
3. `python scripts/gates/check_ast_gates.py` -> `0` violations (`OC-06`).
4. Full unit + integration suite (`pytest -v tests/unit tests/integration -s`) covering:
   - M5 remainder: `AI`/`HYBRID` route media policy + `SEND_WITHOUT_MEDIA` fallback execution,
   - Persian RTL UI views served with `lang="fa" dir="rtl"`, CSP/security headers, zero `localStorage` secret persistence, and `/events/poll` compatibility,
   - CSV Formula Injection neutralization (`=CMD|...`, `+1`, `-2`, `@SUM`, `  =1+1`, `\t=1`, `\r=1`) + secret scrubbing in `/reports/export`,
   - Notification deduplication (`occurrence_count` increments instead of duplicate unread spam) + maintenance cleanup respecting `media_asset_refs` + safe `/ops/reset`,
   - Tenant `.rfbak` backup & restore (`AT-19`): schema validation against `contracts/backup_manifest.schema.json`, wrong password rejected, 1-byte GCM tamper rejected, `../` traversal & compression-ratio bomb rejected, `PREVIEW` mutates zero rows, `APPLY` re-wraps secrets under destination `Tenant DEK`, enforces **Safe Mode** (`routes`, `destinations`, `allow_external_ai`, `external_consents` all disabled), and strictly isolates Tenant A from Tenant B without `BYPASSRLS`,
   - Real PostgreSQL 16 authenticated Base + WAL `PITR` recovery drill + tamper rejection + measured `rto_seconds` (`OC-03` closed!).
5. `git diff --name-only origin/main...HEAD -- RelayForge_Architecture PROJECT_MASTER_ROADMAP.md` -> empty (`0` modified files).
6. Push `feat/m6-rtl-web-ui-reports-backups-and-pitr` and open the Pull Request to `main`.
