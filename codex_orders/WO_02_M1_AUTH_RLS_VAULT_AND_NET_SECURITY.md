# Codex Work Order `WO-02` (Phase `M1`): Multi-Tenant Auth, Atomic RLS/PgBouncer Middleware, Envelope Vault, Crypto-Shredding & SSRF/Secret Guards

- **Project:** RelayForge Web SaaS (`v2.0.0`)
- **Repository:** `https://github.com/mytest19861986/RelayForge-Web`
- **Base Branch:** `main` (includes merged Phase `M0` commit `f26d4f4`)
- **Target Feature Branch / PR:** `feat/m1-auth-rls-vault-and-net-security` -> open Pull Request to `main`
- **Roadmap Coverage:** Phase `M1` (`WO-03` + `WO-04` + `WO-05` in `PROJECT_MASTER_ROADMAP.md`)
- **Authoritative Contracts (DO NOT MODIFY):**
  - `RelayForge_Architecture/contracts/database_schema.sql`
  - `RelayForge_Architecture/contracts/openapi.yaml`
  - `RelayForge_Architecture/05_MASTER_ARCHITECTURE.md`
  - `RelayForge_Architecture/07_DATABASE_DESIGN.md`
  - `RelayForge_Architecture/15_SECURITY_ARCHITECTURE.md`

---

## 1. Sandbox Execution & Zero-Bloat Rules (READ FIRST)

1. **Native Linux Sandbox Approved (No `containerd` Blocker):**
   - When running inside a Linux cloud sandbox without Docker/`containerd`, running **native `PostgreSQL 16.x`** (and `pgbouncer` if available in `apt`) directly in the sandbox is **100% approved as a full substitute** for `docker compose`. Do **NOT** block pushing or opening the PR because `containerd` is unavailable.
2. **Zero-Bloat Dependency & Repo Policy:**
   - Only add the **3 minimal required libraries** for `M1` to `pyproject.toml`:
     - `argon2-cffi>=23.1,<26.0`
     - `cryptography>=43.0,<47.0`
     - `httpx>=0.27,<1.0`
   - Do **NOT** add `celery`, `redis`, `boto3`, `django-cors-headers`, `djangorestframework`, or ORM-heavy third-party auth frameworks.
   - Do **NOT** modify any file inside `RelayForge_Architecture/` or `PROJECT_MASTER_ROADMAP.md`.
   - Keep `.gitignore` strictly enforced: zero `.venv/`, caches, `.env`, or binary artifacts in Git.
   - Do **NOT** build custom Docker images in `M1`; keep `.github/workflows/ci-m0.yml` (or upgrade/rename workflow cleanly so CI runs **both** `M0` and `M1` unit + integration suites against `postgres:16-alpine` + `pgbouncer` if needed, finishing in `< 90s`).

---

## 2. Exact Scope of Phase `M1` (`M01` + `M02`)

Implement the complete Multi-Tenant Platform Kernel (`M01`) and Security/Vault/Network Boundary (`M02`) across **5 tightly integrated deliverables**:

### 2.1 Forward SQL Migration `migrations/0002_m1_auth_and_shredding_functions.sql` & Multi-Step `db_migrator.py`
`RelayForge_Architecture/contracts/database_schema.sql` (`0001_initial_web_saas_v2`) MUST remain untouched. Extend `src/relayforge/adapters/db_migrator.py` so it applies:
1. `0001_initial_web_saas_v2` (`RelayForge_Architecture/contracts/database_schema.sql`)
2. `0002_m1_auth_and_shredding_functions` (`migrations/0002_m1_auth_and_shredding_functions.sql`)

Each migration version is tracked idempotently in `app.schema_migrations (version, checksum_sha256)` under `pg_advisory_xact_lock`, verifying checksum match on already-applied versions and failing closed (`MigrationChecksumMismatchError`) on any checksum drift. Never issue `DROP ROLE`.

**Why `0002_m1_auth_and_shredding_functions.sql` is required under `51/51 FORCE RLS` + `0 BYPASSRLS` (`OC-10`):**
Before `POST /auth/login` or cookie resolution knows `app.tenant_id`, `relayforge_web` has `app.tenant_id = ''` and rightly sees **0 rows** on direct `SELECT` from `app.users`, `app.tenants`, `app.tenant_memberships`, and `app.web_sessions`. To preserve strict `FORCE RLS` (so `relayforge_web` can STILL never scan cross-tenant tables directly!) while keeping **`0` non-superuser `BYPASSRLS` roles (`OC-10`)**, implement in `0002_m1_auth_and_shredding_functions.sql`:

1. **Role `relayforge_auth_owner`:**
   - Idempotently create `relayforge_auth_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;` and `GRANT USAGE ON SCHEMA app TO relayforge_auth_owner;`.
   - Grant narrow `SELECT` column/table privileges + scoped `FOR SELECT TO relayforge_auth_owner USING (true)` RLS policies **only** on `app.users`, `app.tenants`, `app.tenant_memberships`, `app.workspaces`, and `app.web_sessions`.
   - Update the role-set assertion in `tests/integration/test_db_schema_and_smoke_gates.py` if needed while keeping `non_super_bypassrls_count == 0` and `51/51 FORCE RLS` strictly enforced!
2. **`app.fn_auth_lookup_login_candidate(p_email TEXT, p_tenant_slug TEXT DEFAULT NULL)`:**
   - `SECURITY DEFINER`, owned by `relayforge_auth_owner`, `SET search_path = pg_catalog, pg_temp`, `REVOKE ALL FROM PUBLIC`, `GRANT EXECUTE TO relayforge_web`.
   - Normalizes `lower(trim(p_email))`. Joins `app.users u` (`u.is_active = true`), `app.tenant_memberships tm` (`tm.is_active = true`), `app.tenants t` (`t.status = 'ACTIVE'` and `(p_tenant_slug IS NULL OR t.slug = p_tenant_slug)`), and resolves `default_workspace_id` from `app.workspaces w` (`w.tenant_id = t.id AND w.is_archived = false ORDER BY (w.slug = 'default') DESC, w.created_at ASC LIMIT 1`).
   - Deterministic multi-tenant rule: if `p_tenant_slug IS NULL` and the user belongs to `> 1` active tenants, raise `AMBIGUOUS_TENANT_SLUG_REQUIRED` (`ERRCODE = 'RF022'`) so login requires explicit `tenant_slug` instead of guessing! Otherwise return at most 1 row: `(user_id UUID, password_hash_argon2id TEXT, tenant_id UUID, tenant_slug TEXT, role TEXT, default_workspace_id UUID)`.
3. **`app.fn_auth_resolve_session(p_token_hash_sha256 TEXT)`:**
   - `SECURITY DEFINER`, owned by `relayforge_auth_owner`, `SET search_path = pg_catalog, pg_temp`, `REVOKE ALL FROM PUBLIC`, `GRANT EXECUTE TO relayforge_web`.
   - Looks up `app.web_sessions ws` where `ws.token_hash_sha256 = p_token_hash_sha256 AND ws.revoked_at IS NULL AND ws.expires_at > pg_catalog.clock_timestamp()`, joined with active `app.tenants` (`status = 'ACTIVE'`), active `app.users` (`is_active = true`), and active `app.tenant_memberships` (`is_active = true`).
   - Returns at most 1 row: `(session_id UUID, tenant_id UUID, user_id UUID, role TEXT, csrf_token_hash_sha256 TEXT)`.
4. **Operator Onboarding Grants & Owner `Crypto-Shredding` Function (`OC-03`):**
   - Grant `INSERT, SELECT, UPDATE` on `app.workspaces, app.workspace_settings` to `relayforge_operator` so the Operator Control Plane can atomically provision a pilot tenant (`tenants` + `users` + `tenant_memberships` + `tenant_encryption_keys` + default `workspaces` + `workspace_settings` + `OPERATOR_PLAN` daily `$2` = `2_000_000` & monthly `$30` = `30_000_000` quotas per `DEC-03`) under `SET LOCAL app.tenant_id` & `SET LOCAL app.user_id`.
   - Implement `app.fn_owner_crypto_shred_tenant(p_confirmation_slug TEXT, p_correlation_id TEXT)` (`SECURITY DEFINER` owned by `relayforge_operator`, `SET search_path = pg_catalog, pg_temp`, `GRANT EXECUTE TO relayforge_web`, with narrow `relayforge_operator` update/delete RLS policies as needed still scoped strictly to `current_setting('app.tenant_id', true)::uuid`!):
     - Verifies `v_tenant_id := NULLIF(current_setting('app.tenant_id', true), '')::uuid` and `v_user_id := NULLIF(current_setting('app.user_id', true), '')::uuid` are non-null.
     - Verifies caller has active `'OWNER'` membership in `app.tenant_memberships` for `(v_tenant_id, v_user_id)` (otherwise raise `ERRCODE = '42501'`).
     - Verifies `app.tenants.slug = p_confirmation_slug` for `id = v_tenant_id` (otherwise raise `CONFIRMATION_SLUG_MISMATCH` `ERRCODE = '22023'`).
     - Atomically:
       1) Updates `app.tenant_encryption_keys` for `v_tenant_id`: `key_state = 'SHREDDED'`, `wrapped_dek_ciphertext = NULL`, `shredded_at = pg_catalog.clock_timestamp()` (satisfying the `CHECK` constraint on line 112 of `database_schema.sql`!).
       2) Updates `app.secrets_vault` for `v_tenant_id`: `is_revoked = true`.
       3) Revokes all `app.web_sessions` for `v_tenant_id`: `revoked_at = COALESCE(revoked_at, pg_catalog.clock_timestamp())`.
       4) Sets `app.workspaces.emergency_stop = true` for `v_tenant_id`.
       5) Updates `app.tenants` (`id = v_tenant_id`): `status = 'PENDING_DELETION'`, `updated_at = pg_catalog.clock_timestamp()`.
       6) Inserts an append-only row into `app.audit_events` (`action_code = 'TENANT_CRYPTO_SHREDDED'`, `actor_user_id = v_user_id`, `actor_role = 'OWNER'`, `target_type = 'TENANT'`, `target_id = v_tenant_id::text`, `correlation_id = p_correlation_id`, `details_safe_json = '{"pitr_retention_disclosure_days": 14}'::jsonb`).

---

### 2.2 Pure Domain Contracts & Services (`src/relayforge/core/` — Zero Framework Imports!)
Keep `src/relayforge/core/` 100% pure standard-library Python (verified by `lint-imports` and `check_ast_gates.py`):
- `src/relayforge/core/security.py`:
  - `Role` enum (`OWNER`, `ADMIN`, `EDITOR`, `PUBLISHER`, `VIEWER`) + explicit RBAC permission matrix helper (`require_minimum_role` / action permissions: e.g., `VIEWER` read-only; `EDITOR` workspace content; `ADMIN` secrets/consents/workspaces; `OWNER` `delete-account`).
  - `SecretKind` & `ConsentScope` enums matching exact SQL `CHECK` values in `database_schema.sql` (`TELEGRAM_BOT_TOKEN`, ..., `OPENAI_TEXT`, ...).
  - Deterministic secret masking helper `mask_secret_preview(raw_secret: str) -> str` (never reveals full secret; for short secrets `< 10` chars returns `"***REDACTED***"`, otherwise safe prefix/last-4 mask without leaking token body).
  - Pure redaction pattern utility `scrub_sensitive_text(text: str, extra_secrets: Sequence[str] = ()) -> str` that scrubs:
    - Telegram/Bale/Rubika/Soroush bot URL paths (`/bot<id>:<token>/...`, `eitaayar.ir/api/<token>/...`)
    - OpenAI/Gemini/Bearer tokens (`sk-...`, `AIza...`, `Bearer ...`, `ghp_...`, `gho_...`)
    - Query params (`key=`, `api_key=`, `token=`, `access_token=`, `secret=`)
    - Cookie/CSRF headers (`__Host-rf_session=...`, `X-CSRFToken:...`)
    - Any caller-supplied runtime `extra_secrets`.
  - Standard error code & `ProblemDetails` data structures (`code`, `category`, `message_fa`, `suggested_action_fa`, `retryable`, `retry_after_sec`, `correlation_id`) matching `openapi.yaml`.

---

### 2.3 Envelope Secret Vault (`src/relayforge/adapters/vault.py` — `ADR-005` & `OC-03`)
Implement `EnvelopeSecretVault` using `cryptography.hazmat.primitives.ciphers.aead.AESGCM`:
- **Master KEK (`RF_MASTER_KEK_B64`):** 32-byte key loaded from environment (validate exact decoded length == 32 bytes at startup; support optional retired KEK map if needed, never log or serialize KEK).
- **Per-Tenant DEK (`app.tenant_encryption_keys`):**
  - Generated via `os.urandom(32)`.
  - Wrapped under `Master KEK` using `AES-256-GCM` with fresh 12-byte random nonce (`wrapped_dek_ciphertext` stores `nonce_12b || ciphertext_and_tag` as `BYTEA`, `wrapping_alg = 'AES-256-GCM'`).
  - AAD (`associated_data`) binds `f"relayforge:dek:v1:{tenant_id}:{key_version}".encode("utf-8")` so a wrapped DEK copied across tenants or versions fails GCM authentication immediately!
- **Per-Secret Encryption (`app.secrets_vault`):**
  - Unwraps active `Tenant DEK` in memory only (zero disk writes), encrypts secret UTF-8 bytes with `AES-256-GCM` using fresh 12-byte `nonce_bytes` (`BYTEA`) and `ciphertext_aead` (`BYTEA`), binding AAD `f"relayforge:secret:v1:{tenant_id}:{workspace_id}:{secret_id}:{secret_kind}".encode("utf-8")`.
  - Stores `masked_hint` via `mask_secret_preview(raw_secret)` and never returns plaintext via any web DTO!
  - If `key_state == 'SHREDDED'` (after `Crypto-Shredding`) or `is_revoked == True`, `decrypt_secret(...)` MUST raise a deterministic domain error (`TenantKeyShreddedError` / `SecretRevokedError`).

---

### 2.4 Outbound Anti-SSRF `SafeDialer` & `SecretScrubbingTransport` (`src/relayforge/adapters/net_guard.py` — `SEC-03, SEC-06, DST-08`)
1. **IP Validator (`validate_public_ip`):**
   - Rejects IPv4/IPv6 loopback (`127.0.0.0/8`, `::1`), private (`10/8`, `172.16/12`, `192.168/16`, `fc00::/7`), link-local & cloud metadata (`169.254.0.0/16`, `fe80::/10`), CGNAT (`100.64.0.0/10`), multicast, unspecified (`0.0.0.0/8`, `::`), reserved/benchmarking (`198.18.0.0/15`, `240.0.0.0/4`), and IPv4-mapped IPv6 equivalents (`::ffff:127.0.0.1`, `::ffff:169.254.169.254`)!
   - Enforces `https` scheme only by default (reject `http://`, `file://`, `ftp://`, `gopher://`).
2. **Post-DNS Socket Pinning & Redirect Hop Re-Validation (`SafeDialer` / `httpx.BaseTransport`):**
   - Resolves hostname via `socket.getaddrinfo`, validates **every** resolved IP via `validate_public_ip` (if ANY resolved record is non-public, abort before opening a socket -> `SSRFBlockedError`), and connects **directly to the validated IP** (preventing TOCTOU DNS rebinding between resolution and `connect()`!) while preserving TLS SNI (`server_hostname` / `extensions={"sni_hostname": host}`) and `Host` header!
   - Disables automatic blind redirects: handles `301/302/303/307/308` manually (max `3` hops), validating scheme + host + resolved IPs on **every single redirect target** before connecting!
3. **`SecretScrubbingTransport` & Structured JSON Log Filter:**
   - Wraps `httpx` exceptions (`RequestError`, `HTTPStatusError`) so `.request.url` or error strings NEVER leak bot tokens in paths (`/bot123456:ABC-DEF.../sendMessage`) or API keys; re-raises a scrubbed domain transport error.
   - Provides a `logging.Filter` / formatter that scrubs `record.msg`, `record.args`, and exception tracebacks via `scrub_sensitive_text`.

---

### 2.5 Django 5.2 + Django Ninja Web Application, PgBouncer-Safe Atomic RLS Middleware & M1 Endpoints (`src/relayforge/web/`)
Configure minimal, fast Django 5.2 + Django Ninja under `src/relayforge/web/`:
1. **Database & PgBouncer Transaction-Mode Settings (`settings.py` — `OC-05`):**
   - Two explicit PostgreSQL connection aliases:
     - `"default"`: connects as **`relayforge_web`** (`DATABASE_WEB_URL`), `CONN_MAX_AGE = 0`, `DISABLE_SERVER_SIDE_CURSORS = True`, `ATOMIC_REQUESTS = False` (we manage explicit atomic scope in `TenantRLSMiddleware`!), `OPTIONS = {"prepare_threshold": None}`.
     - `"operator"`: connects as **`relayforge_operator`** (`DATABASE_OPERATOR_URL`), `CONN_MAX_AGE = 0`, `DISABLE_SERVER_SIDE_CURSORS = True`, `OPTIONS = {"prepare_threshold": None}`, used **exclusively** by `/api/v1/operator/*` endpoints!
   - Password hashing via `argon2.PasswordHasher` (`Argon2id`: `time_cost>=2`, `memory_cost>=65536`, `parallelism>=2`), including constant-time dummy hash verification on unknown email to prevent user enumeration timing leaks.
2. **Security Headers, Cookie, CSRF, Origin & Correlation Middleware (`middleware.py`):**
   - Assign/validate `X-Correlation-ID` (UUIDv4 generated if missing/invalid) on every request & response, including error `ProblemDetails`.
   - Enforce security response headers (`Content-Security-Policy: default-src 'self'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `Cache-Control: no-store` on API responses). Do **NOT** emit wildcard CORS headers.
   - Session cookie name: `__Host-rf_session` in HTTPS mode (`Secure=True, HttpOnly=True, SameSite="Lax", Path="/"`), with controlled test/local HTTP fallback toggle (`RF_ALLOW_INSECURE_LOCAL_COOKIE=true` ONLY for `127.0.0.1` local dev/test when TLS proxy is absent, defaulting to `False` in production!).
   - **State-Mutating CSRF + Origin Guard (`POST`, `PUT`, `PATCH`, `DELETE` on session-authenticated routes):**
     - Require `X-CSRFToken` header whose SHA-256 matches `csrf_token_hash_sha256` of the active `web_sessions` row (`hmac.compare_digest`).
     - If `Origin` header is present, verify its host/scheme matches allowed `ALLOWED_ORIGINS` / request host; reject mismatches with `403 ProblemDetails`.
   - **Atomic `TenantRLSMiddleware`:**
     - For session-authenticated routes: wraps the entire view execution (and any query evaluation) inside `with transaction.atomic(using="default"):`, calls `app.fn_auth_resolve_session(sha256(raw_cookie))`, rejects invalid/expired/revoked sessions with `401 ProblemDetails`, and executes:
       ```sql
       SELECT pg_catalog.set_config('app.tenant_id', %s, true),
              pg_catalog.set_config('app.user_id', %s, true);
       ```
       so **every** SQL statement in that request runs inside one transaction with `SET LOCAL` active and resets automatically on commit/rollback (`PgBouncer transaction mode` safe!).
     - Enforces sliding-window rate limiting via `app.rate_limit_counters` (`INSERT ... ON CONFLICT (tenant_id, bucket_key, window_epoch_sec) DO UPDATE SET hit_count = app.rate_limit_counters.hit_count + 1 RETURNING hit_count`) on authenticated endpoints (and IP-keyed pre-auth login throttle in memory/DB) returning `429 ProblemDetails` (`retry_after_sec` + `Retry-After` header) when exceeded!
3. **Phase `M1` REST Endpoints (conforming to `RelayForge_Architecture/contracts/openapi.yaml` under `/api/v1`):**
   - `GET /api/v1/health` — checks PostgreSQL `SELECT 1` + `app.schema_migrations` readiness, returns `AckResponse` (`200` / `503 ProblemDetails`).
   - `POST /api/v1/operator/tenants` *(Control-Plane Onboarding helper under `OperatorBearerAuth`)* — constant-time bearer check against `RF_OPERATOR_BEARER_TOKEN`; inside `transaction.atomic(using="operator")` with `SET LOCAL app.tenant_id` & `SET LOCAL app.user_id`, provisions `tenants`, initial `OWNER` `users` (`Argon2id`) + `tenant_memberships`, wrapped `tenant_encryption_keys` (`version=1`), default `workspaces` (`slug='default'`) + `workspace_settings`, default `OPERATOR_PLAN` daily (`2_000_000`) & monthly (`30_000_000`) rows in `budget_policies` (`DEC-03`), and an `audit_events` row (`TENANT_PROVISIONED`).
   - `PUT /api/v1/operator/tenants/{tenantId}/plan-quotas` (`OperatorBearerAuth`, `OC-01`) — updates/upserts `OPERATOR_PLAN` `DAILY` / `MONTHLY` `max_cost_micros` as `relayforge_operator` and records `audit_events`.
   - `POST /api/v1/auth/login` — validates credentials via `fn_auth_lookup_login_candidate` + `Argon2id`, issues 32-byte CSPRNG session token + 32-byte CSPRNG CSRF token, stores SHA-256 hashes in `app.web_sessions` under `SET LOCAL app.tenant_id`, records `audit_events` (`AUTH_LOGIN_SUCCEEDED`), sets `__Host-rf_session` cookie, and returns `{tenant_id, default_workspace_id, role, csrf_token}`.
   - `POST /api/v1/auth/logout` — revokes current `app.web_sessions` row (`revoked_at = clock_timestamp()`), records `audit_events` (`AUTH_LOGOUT`), clears cookie, returns `AckResponse`.
   - `GET /api/v1/tenants/current` — returns current tenant info, caller role, and read-only `OPERATOR_PLAN` + `TENANT_BUDGET` ceilings.
   - `POST /api/v1/tenants/current/delete-account` (`OWNER` only + explicit `{confirmation_slug, acknowledge_irreversible: true}`) — invokes `app.fn_owner_crypto_shred_tenant(...)`, clears session cookie, and returns `202 AckResponse` including `pitr_retention_disclosure_days: 14` (`DEC-02`, `OC-03`).
   - `GET /api/v1/workspaces` & `POST /api/v1/workspaces` — lists/creates tenant workspaces (`workspaces` + `workspace_settings`, `ADMIN`/`OWNER` required for `POST`, audited).
   - `GET /api/v1/workspaces/{wsId}/security/consents` & `PUT /api/v1/workspaces/{wsId}/security/consents` — lists/upserts `app.external_consents` (`ADMIN`/`OWNER` required for `PUT`, audited in `audit_events`).
   - `GET /api/v1/workspaces/{wsId}/security/secrets` *(masked list)*, `POST /api/v1/workspaces/{wsId}/security/secrets` *(envelope-encrypt new secret into `app.secrets_vault`, returning ONLY `id`, `secret_kind`, `label`, `masked_hint` — NEVER plaintext!)*, and `DELETE /api/v1/workspaces/{wsId}/security/secrets/{secretId}` *(sets `is_revoked = true`, audited — `SEC-03, SEC-04, DST-08`)*.
   - Global exception handler converting domain/validation/auth/DB errors (`RF001..RF022`, `401`, `403`, `404`, `422`, `429`) into exact `ProblemDetails` JSON **scrubbed** via `scrub_sensitive_text`!

---

## 3. Mandatory Non-Vacuous Test Suite (`tests/unit/` & `tests/integration/`)

All existing `M0` tests MUST continue passing (`0` regressions), plus add comprehensive `M1` tests:

1. **Unit Tests (`tests/unit/test_m1_security_and_net_guard.py`):**
   - **Vault Cryptographic Tests:** roundtrip encrypt/decrypt; wrong `Master KEK` fails GCM auth; tampered ciphertext byte fails GCM auth; cross-tenant AAD swap (moving ciphertext from `tenant_a` to `tenant_b` or wrong `workspace_id` / `secret_kind`) fails GCM auth!
   - **Redaction & Scrubber Tests (`DST-08`, `SEC-03`):** Telegram URL `https://api.telegram.org/bot123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11/sendMessage`, Eitaa URL, OpenAI `sk-proj-...`, Gemini `AIzaSy...`, `Bearer ...`, and `__Host-rf_session=...` are 100% scrubbed in strings, `httpx` transport errors, and log records.
   - **Anti-SSRF `SafeDialer` Tests (`SEC-06`):**
     - Rejects `127.0.0.1`, `10.0.0.5`, `172.16.0.1`, `192.168.1.1`, `169.254.169.254`, `100.64.0.1`, `::1`, `fe80::1`, `::ffff:127.0.0.1`, `::ffff:169.254.169.254`, and `http://` plain URLs.
     - Rejects multi-A-record DNS response where 1st IP is public (`93.184.216.34`) and 2nd IP is `127.0.0.1`!
     - Rejects `302` redirect from public host `example.com` -> `https://internal.example/metadata` resolving to `169.254.169.254`!
     - Allows mocked public IP `93.184.216.34` over `https://` and verifies socket dial target uses the resolved IP (anti-DNS-rebinding).

2. **Integration Tests on Real PostgreSQL 16 (`tests/integration/test_m1_auth_rls_vault_and_shredding.py`):**
   - **100-Request Concurrent Multi-Tenant Isolation Test (`SPK-01` / `AT-18` / `SEC-09`):**
     - Provision `Tenant A` and `Tenant B` via `/api/v1/operator/tenants`, log in both, create workspaces/secrets/consents under `ThreadPoolExecutor` (100 concurrent requests across both tenants), and prove:
       - **Zero cross-tenant read leaks** (`Tenant A` gets `404`/empty when querying `Tenant B`'s `wsId` or `secretId`).
       - **Zero cross-tenant write/revoke leaks** (`Tenant A` attempting `DELETE` on `Tenant B`'s `secretId` or `PUT` on `Tenant B`'s `wsId` fails with `404` and leaves `Tenant B`'s data untouched).
       - Nested `transaction.atomic()` savepoint rollback inside a request does **not** clear `SET LOCAL app.tenant_id` for subsequent queries in the same request!
   - **Auth, Session Revocation, CSRF, Origin & RBAC Tests (`AT-01`, `SEC-01`, `SEC-02`, `SEC-04`):**
     - Unauthenticated request -> `401 ProblemDetails`.
     - State-mutating `POST /api/v1/workspaces` without `X-CSRFToken` -> `403 ProblemDetails`; with wrong `X-CSRFToken` -> `403`; with hostile `Origin: https://evil.example` -> `403`.
     - `VIEWER` role blocked from `POST /workspaces`, `POST /security/secrets`, `PUT /security/consents`, and `POST /tenants/current/delete-account` (`403`).
     - `POST /api/v1/auth/logout` immediately invalidates the cookie (`401` on replay).
   - **Operator Plan Immutability (`OC-01`):**
     - Operator `PUT /api/v1/operator/tenants/{tenantId}/plan-quotas` updates `OPERATOR_PLAN` ceiling cleanly; tenant `relayforge_web` session cannot mutate `OPERATOR_PLAN` (`RF007`), and `GET /api/v1/tenants/current` reflects the updated ceiling.
   - **Secret Vault & Empirical `Crypto-Shredding` Proof (`OC-03`, `SEC-03`, `SEC-09`):**
     - Store a secret via `POST /workspaces/{wsId}/security/secrets` -> verify API response has `masked_hint` and **zero** plaintext; verify raw `SELECT ciphertext_aead FROM app.secrets_vault` does not contain plaintext bytes; verify `EnvelopeSecretVault.decrypt_secret` recovers exact plaintext while tenant is active.
     - Execute `POST /api/v1/tenants/current/delete-account` as `OWNER`:
       - Verify `app.tenant_encryption_keys.key_state == 'SHREDDED'` and `wrapped_dek_ciphertext IS NULL`.
       - Verify `EnvelopeSecretVault.decrypt_secret` on the existing `ciphertext_aead` now **fails deterministically (`TenantKeyShreddedError`)** even with possession of `Master KEK` and raw DB row bytes!
       - Verify `Tenant A`'s session now returns `401`, `audit_events` contains `TENANT_CRYPTO_SHREDDED` (and `UPDATE`/`DELETE` on `audit_events` is rejected with `RF021`), while `Tenant B`'s keys, secrets, and sessions remain **100% intact and decryptable**!

---

## 4. Verification & PR Submission Checklist

Run all gates before opening the PR:
1. `ruff check .`
2. `lint-imports`
3. `python scripts/gates/check_ast_gates.py`
4. `pytest -v tests/unit tests/integration` (against PostgreSQL 16 — native sandbox PG16 or `postgres:16-alpine` in GitHub Actions CI)
5. Confirm `RelayForge_Architecture/` and `PROJECT_MASTER_ROADMAP.md` have **0** diff, `non_super_bypassrls_count == 0`, `force_rls_count >= 51` (100% of tables in `app`), and tracked repo size remains lean.
6. Push branch `feat/m1-auth-rls-vault-and-net-security` and open a Pull Request into `main` on `mytest19861986/RelayForge-Web`.
