# Codex Work Order `WO-04` (Phase `M3`): Deterministic Rules Engine, Drafts & Frozen Publications, AI-Free Copy/Rules Pipeline, Fair Dispatcher Worker, `UNCERTAIN` Reconciliation & Real OS `SIGKILL` Suite (`OC-02`)

- **Project:** RelayForge Web SaaS (`v2.0.0`)
- **Repository:** `https://github.com/mytest19861986/RelayForge-Web`
- **Base Branch:** `main` (includes merged Phases `M0` + `M1` + `M2`, commit `dce6f88`)
- **Target Feature Branch / PR:** `feat/m3-rules-drafts-fair-queue-and-sigkill` -> open Pull Request to `main`
- **Roadmap Coverage:** Phase `M3` (`WO-09` + `WO-10` + `WO-11` in `PROJECT_MASTER_ROADMAP.md`, Core Module `M09` + Brand Identity `IDN-04/05`)
- **Authoritative Contracts (DO NOT MODIFY):**
  - `RelayForge_Architecture/contracts/database_schema.sql`
  - `RelayForge_Architecture/contracts/openapi.yaml`
  - `RelayForge_Architecture/contracts/internal_events.schema.json`
  - `RelayForge_Architecture/13_PUBLICATION_ENGINE.md`
  - `RelayForge_Architecture/19_RELIABILITY_AND_RECOVERY.md`

---

## 1. Sandbox Execution & Zero-Bloat Rules (MANDATORY)

1. **Native Linux Sandbox Approved:**
   - Native `PostgreSQL 16.x` and real Linux child processes (`subprocess.Popen` + `os.kill(pid, signal.SIGKILL)`) in your sandbox are pre-approved and authoritative. Run all unit + integration suites locally, then push and open the PR immediately without stopping.
2. **Strict Zero-Bloat Dependency Rule (`0` New Heavy Packages!):**
   - Implement Phase `M3` using **existing dependencies + Python 3.12 stdlib (`zoneinfo`, `hashlib`, `subprocess`, `signal`) ONLY** (add **zero** new packages to `pyproject.toml` unless a tiny pure-Python wheel is strictly unavoidable; implement the linear-time Thompson NFA regex matcher in ~90 lines of pure Python inside `src/relayforge/core/rules.py` so `ReDoS` immunity is mathematically `O(N * M)` with zero native build bloat!).
   - Keep `RelayForge_Architecture/` and `PROJECT_MASTER_ROADMAP.md` **100% untouched** (`0` diff).

---

## 2. Exact Scope of Phase `M3`

### 2.1 Forward Migration `migrations/0004_m3_publication_immutability_and_outbox.sql`
Register `0004_m3_publication_immutability_and_outbox` in `MIGRATIONS` inside `src/relayforge/adapters/db_migrator.py` while keeping `100%` of `app.*` tables under `FORCE RLS` and **`0` non-superuser `BYPASSRLS` roles (`OC-10`)**:
1. **Immutable `app.publication_versions` Trigger (`PUB-07`):**
   - Just like `app.trg_protect_append_only_audit` on `app.audit_events`, add trigger `trg_protect_immutable_publication_versions` (`BEFORE UPDATE OR DELETE ON app.publication_versions`) raising `PUBLICATION_VERSIONS_ARE_IMMUTABLE` (`ERRCODE = 'RF023'`) unless `CURRENT_USER = 'relayforge_migrator' AND current_setting('app.migration_mode', true) = 'on'`.
2. **Worker Route/Draft Read & Outbox Helper Grants (Verify Least-Privilege Completeness):**
   - Inspect grants in `database_schema.sql` lines 1074–1099: verify `relayforge_worker` has `SELECT` on `app.content_item_revisions`, `app.publication_versions`, `app.destinations`, `app.secrets_vault`, `app.tenant_encryption_keys`, `app.workspaces`, `app.routes`, `app.route_policy_revisions`, `app.brand_identities`, and `INSERT` on `app.publication_versions` (if auto/copy route worker freezes publications!) and `app.delivery_tasks`, `app.delivery_attempts`, `app.domain_outbox`, `app.audit_events`. Add any missing narrow `GRANT` on existing `app.*` tables needed for `COPY`/`RULES` route execution and `dispatcher-worker` delivery without weakening RLS!

---

### 2.2 Pure Domain Engines (`src/relayforge/core/rules.py` & `src/relayforge/core/scheduling.py` — Pure `core/`!)
Keep `src/relayforge/core/` 100% free of external framework imports (`lint-imports` & `check_ast_gates.py` enforced):

1. **Linear-Time Thompson NFA Safe Pattern Matcher (`RULE-09`, `AT-20`):**
   - Implement a bounded, deterministic Thompson NFA regex engine (supporting literals, `.`, character classes `[...]`, `|`, `(...)`, `*`, `+`, `?`, `^`, `$`, case-insensitive flag, max pattern length `256`, max NFA states `512`, and rejecting backreferences) that executes in strict `O(len(states) * len(text))` time with **zero backtracking**!
   - Pathological ReDoS inputs like pattern `"(a+)+$"` on `"a" * 5000 + "!"` MUST finish in **`< 50 ms`**.
2. **Boolean Filter AST Evaluator (`RULE-01`, `RULE-02`):**
   - Enforce `MAX_FILTER_DEPTH = 12` and `MAX_FILTER_NODES = 64`.
   - Support combinators `ALL` (`AND`), `ANY` (`OR`), `NOT`, and leaf predicates: `content_kind_in` (`TEXT`, `IMAGE`, `VIDEO`, `AUDIO`, `VOICE_NOTE`, `DOCUMENT`, `ALBUM`), `keyword_any`, `regex_match` (via Thompson NFA!), `has_link`, `has_hashtag`, `has_mention`, `min_utf16_len`, `max_utf16_len`, `source_id_in`, returning `(matched: bool, reason_code: str)`.
   - Automatically reject `access_class == 'PROTECTED_NO_FORWARD'` (`SRC-08`) and reject `'REFERENCE_ONLY'` for non-Telegram or transformed routes (`SRC-04`).
3. **Deterministic AST-Preserving Rule Transformer & Brand Identity Injector (`RULE-03`, `RULE-04`, `IDN-04`, `IDN-05`):**
   - Replace/strip phrases, strip unauthorized promotional/third-party mentions, apply prefix/suffix or `SafePlaceholderTemplate` while preserving valid `FormatSpanUtf16` boundaries!
   - `inject_brand_identity(content: CanonicalRichText, brand: BrandSpec) -> CanonicalRichText`: deterministically injects `HEADER` / `FOOTER` / `BOTH` / `NONE` with `separator_line` without duplicating an already-present signature line (`IDN-04`), supporting distinct `BrandIdentity` per destination (`IDN-05`).
   - Policy precedence (`RULE-04`): `destination_override > route_policy > workspace_default`.
4. **Exact + `SimHash-64` Near-Duplicate Classifier (`RULE-05`):**
   - Compute `exact_hash_sha256` (normalized text) and 64-bit signed `simhash_64` (`-2**63 .. 2**63 - 1` fitting PostgreSQL `BIGINT`) over character/word 3-shingles.
   - Exact match (`exact_hash_sha256` seen on route) -> `DUPLICATE_EXACT_SKIP`; near-duplicate (`0 < hamming_distance <= 6`) -> **`AMBIGUOUS_NEAR_DUPLICATE_HOLD_REVIEW`** (never silently drop borderline similar news items — `RULE-05`!); `> 6` -> `UNIQUE_PASS`.
5. **Route DAG Cycle Detector (`RULE-06`):**
   - Given workspace routes `(source_canonical_ids -> destination_canonical_ids)`, detect any directed cycle (`A -> B -> A` or `A -> B -> C -> A`) via DFS and reject route creation/activation with `ROUTE_CYCLE_DETECTED`.
6. **Timezone-Aware Calendar Scheduler & Catch-Up Coordinator (`QUE-02`, `QUE-03`):**
   - Compute UTC `scheduled_for` using `zoneinfo.ZoneInfo(workspace_timezone)` for `IMMEDIATE`, `INTERVAL_MINUTES`, `DAILY_AT`, `WEEKLY_AT`, and explicit `AT_TIMESTAMP` (handling DST/midnight boundaries cleanly via injectable `ClockPort`).
   - Catch-up policy for overdue items after downtime (`QUE-03`): default **`HOLD_FOR_REVIEW`** (creates `NEEDS_REVIEW` draft instead of flooding channel!), plus `GRADUAL_THROTTLED` (staggers `scheduled_for` by `spacing_sec`) and `SKIP_EXPIRED`.

---

### 2.3 AI-Free `COPY` & `RULES` Route Processor + Dispatcher Worker (`src/relayforge/workers/dispatcher.py` — **Closes `OC-02`!**)
1. **AI-Free Route Execution (`UC-02`, `AT-03`):**
   - Implement route evaluation for `route_mode IN ('COPY', 'RULES')` with **100% zero dependency on AI providers/keys** (`allow_external_ai = false`, zero AI provider rows):
     - Evaluates filter AST, deduplication (`content_fingerprints`), deterministic transforms, per-destination brand injection & compilation (`compile_rich_text`), daily post cap (`routes.daily_post_cap`), and `automation_level`:
       - `DRAFT_ONLY` / `APPROVAL_REQUIRED` (or near-duplicate / overflow): creates `drafts` (`EDITING` or `NEEDS_REVIEW`) + `draft_revisions` + `domain_outbox` event.
       - `AUTO_PUBLISH_BY_POLICY` (when route `is_enabled=True` and workspace `emergency_stop=False`): freezes `publication_versions` and enqueues per-destination `delivery_tasks` (`READY` / `SCHEDULED`).
   - **`NullDryRunSink` (`POST /workspaces/{wsId}/routes/{routeId}/dry-run` — `RULE-07`):** executes the exact same filter/dedup/transform/brand/compile pipeline in read-only simulation mode, returning the full step-by-step trace per destination while writing **0** `delivery_tasks` and making **0** outbound HTTP calls!
2. **Reliable Fair Dispatcher Worker (`src/relayforge/workers/dispatcher.py`):**
   - Connects strictly as **`relayforge_worker`** (`prepare_threshold=None`).
   - Loop step A: calls `SELECT app.fn_reap_expired_delivery_leases(%s)` to reap expired `RESERVED -> READY/FAILED` and `DISPATCHING_IN_FLIGHT -> UNCERTAIN` leases.
   - Loop step B: calls `SELECT * FROM app.fn_worker_claim_next_jobs(%s, %s, %s)` (Water-Filling anti-starvation claim across tenants!).
   - Loop step C (per claimed task `(claimed_task_id, claimed_tenant_id, claimed_epoch)`):
     1) Opens transaction with `SET LOCAL app.tenant_id = claimed_tenant_id`:
        - Checks workspace `emergency_stop` and destination `is_enabled AND NOT is_archived`; if stopped/disabled, leaves/transitions safely without sending (`QUE-10`).
        - Decrypts destination bot secret in memory via `EnvelopeSecretVault` and loads `frozen_payload_json` from `app.publication_versions`.
        - Calls `SELECT app.fn_worker_transition_task(claimed_task_id, claimed_epoch, 'DISPATCHING_IN_FLIGHT', NULL, NULL)` and **commits** BEFORE dialing outbound network (`AT-16`)! *(Provide an optional test-only deterministic hook/FIFO/signal barrier env var `RF_TEST_CRASH_POINT` strictly gated to test mode or injectable hook so subprocess `SIGKILL` tests can kill the worker at exact deterministic points!)*
     2) Starts a lightweight background heartbeat timer (when outbound call takes longer than `lease_ttl / 3`) calling `app.fn_worker_heartbeat_lease(claimed_task_id, claimed_epoch, lease_ttl_sec)` under `SET LOCAL app.tenant_id`.
     3) Invokes destination `CONNECTORS[platform]().send_message(...)`:
        - **On success (`PublishReceipt`):** inside transaction (`SET LOCAL app.tenant_id`), calls `fn_worker_transition_task(claimed_task_id, claimed_epoch, 'SENT', receipt.remote_message_id, NULL)`, inserts `app.delivery_attempts` (`outcome='SENT'`), and inserts `app.domain_outbox` (`event_type='delivery.succeeded'`).
        - **On `OutboundTransportError` / network timeout AFTER bytes may have left socket (`TIMEOUT_UNCERTAIN`):** NEVER auto-retry (`QUE-07`, `QUE-08`)! Calls `fn_worker_transition_task(..., 'UNCERTAIN', NULL, NULL)`, inserts `delivery_attempts` (`outcome='TIMEOUT_UNCERTAIN'`), and emits `delivery.marked_uncertain` to `domain_outbox`.
        - **On `RATE_LIMIT_429` / retryable transient error (when `attempts < max_attempts`):** computes exponential backoff + jitter respecting `max(backoff, retry_after_sec)` (`QUE-07`), calls `fn_worker_transition_task(..., 'RETRY_WAIT', NULL, next_retry_at)`, records `delivery_attempts` (`TRANSIENT_ERROR`).
        - **On `QUOTA_EXHAUSTED`, `AUTH_REVOKED`, `PERMISSION_DENIED`, `INCOMPATIBLE_PAYLOAD`, or `attempts >= max_attempts`:** immediately transitions to `'FAILED'` (zero blind retries on `QUOTA_EXHAUSTED` — `QUE-07`!), records `delivery_attempts`, and scrubs `last_error_safe`.
   - Provide CLI `python -m relayforge.workers.dispatcher --worker-id <id> --once` (and continuous loop mode).

---

### 2.4 Phase `M3` Web API Endpoints (`src/relayforge/web/` — Matching `openapi.yaml`)
Mount clean modular routers for:
1. **Brands (`IDN-04..06`):**
   - `GET / POST /api/v1/workspaces/{wsId}/brands` (validates `ownership_attested=True` when visual/logo tokens are set — `IDN-06`; allows linking `brand_identity_id` on destinations & drafts).
2. **Routes & Dry-Run (`RULE-01..09`, `AUT-01`):**
   - `GET / POST /api/v1/workspaces/{wsId}/routes` (always creates disabled `is_enabled=False`, `automation_level='DRAFT_ONLY'` by default unless explicitly configured; runs DAG cycle check `RULE-06`; creates initial `route_policy_revisions` `revision_no=1`).
   - `PATCH /api/v1/workspaces/{wsId}/routes/{routeId}` (creates new immutable `route_policy_revisions` `revision_no + 1` when policy changes so already-queued tasks keep their pinned revision — `RULE-08`!).
   - `POST /api/v1/workspaces/{wsId}/routes/{routeId}/dry-run` (executes `NullDryRunSink` on a `content_item_id` or sample text, zero side effects — `RULE-07`).
   - `POST /api/v1/workspaces/{wsId}/routes/{routeId}/run-item` *(or synchronous pipeline trigger for `COPY`/`RULES` testing & manual route trigger)* so `AT-03` can also be driven cleanly via API + worker.
3. **Drafts, Preview, Publish & Evergreen Republish (`PUB-01..08`, `QUE-11`):**
   - `GET / POST /api/v1/workspaces/{wsId}/drafts`, `GET /api/v1/workspaces/{wsId}/drafts/{draftId}`, `PATCH /api/v1/workspaces/{wsId}/drafts/{draftId}` (appends new `draft_revisions` `revision_no + 1` without invoking AI — `PUB-01`).
   - `POST /api/v1/workspaces/{wsId}/drafts/{draftId}/preview` (compiles per target destination injecting each destination's `BrandIdentity` + capability degradation/overflow/album report + honest simulator disclaimer flag — `PUB-03`, `IDN-05`).
   - `POST /api/v1/workspaces/{wsId}/drafts/{draftId}/publish` (`Idempotency-Key` header + `PublishDraftRequest`: enforces `explicit_user_confirmation: True`, verifies `expected_draft_revision_no == current_revision_no` — `PUB-07`!, freezes immutable `app.publication_versions`, and enqueues independent `app.delivery_tasks` **only** for the explicitly listed `destination_ids` — `PUB-04`, `DST-06`). Supports `EXPLICIT_TEST_SEND`, `LIVE_PUBLISH`, and `EVERGREEN_REPUBLISH` (`QUE-11`, audited).
4. **Deliveries, `UNCERTAIN` Reconciliation, Emergency Stop & Stateless Event Polling (`QUE-08..10`, `OPS-01`):**
   - `GET /api/v1/workspaces/{wsId}/deliveries` (cursor-paginated with `status` & `publication_version_id` filters).
   - `POST /api/v1/workspaces/{wsId}/deliveries/{taskId}/cancel` (cancels waiting task `status IN ('PENDING','SCHEDULED','READY','RETRY_WAIT')` with `lease_epoch = lease_epoch + 1`).
   - `POST /api/v1/workspaces/{wsId}/deliveries/{taskId}/reconcile` (`Idempotency-Key` + `ReconcileUncertainRequest`: invokes `app.fn_web_reconcile_uncertain_task(...)`, enforcing `acknowledge_duplicate_risk: True` on `CONFIRM_NOT_SENT_REQUEUE`, and writes `audit_events` — `QUE-08`).
   - `POST /api/v1/workspaces/{wsId}/ops/emergency-stop` (toggles `workspaces.emergency_stop`, optionally cancels waiting tasks when requested, honestly reports count of `DISPATCHING_IN_FLIGHT` tasks without claiming remote deletion — `QUE-10`, `AT-15`).
   - `GET /api/v1/workspaces/{wsId}/events/poll` (stateless cursor-paginated incremental reader over `app.domain_outbox` for `(tenant_id, wsId)` replacing SSE — `ADR-003`).

---

## 3. Mandatory Non-Vacuous Test Suite (`M0 + M1 + M2 + M3`)

1. **Unit Tests (`tests/unit/test_m3_rules_dedup_and_scheduling.py`):**
   - **ReDoS Immunity Benchmark (`RULE-09`, `AT-20`):** evaluate `(a+)+$` and `([a-zA-Z]+)*$` against `5,000`-char non-matching strings -> assert elapsed `< 50 ms`!
   - **Filter AST, Cycle Detection, SimHash & Brand Precedence:** verify `AND/OR/NOT` depth guard, `A -> B -> A` cycle rejection, exact duplicate vs borderline near-duplicate (`AMBIGUOUS_NEAR_DUPLICATE_HOLD_REVIEW`), `BrandInjector` non-duplication & per-destination brand injection (`AT-09`), and `Asia/Tehran` timezone scheduling + `HOLD_FOR_REVIEW` catch-up default (`QUE-02`, `QUE-03`).

2. **Integration & Real OS `SIGKILL` Crash Suite (`tests/integration/test_m3_publishing_and_real_sigkill.py` — **Closes `OC-02`!**):**
   - **AI-Free Live/Copy/Rules End-to-End (`AT-03`, `AT-14`):**
     - With zero AI providers and `allow_external_ai=False`, ingest a message -> run `RULES` route + `dry-run` (assert `0` network calls on dry-run!) -> publish to Destination A (succeeds) and Destination B (simulated `500`/`403` failure) -> assert Destination A is `SENT` once (`remote_message_id` recorded) and is **never** re-sent when Destination B fails/retries (`DST-06`, `QUE-09`, `AT-14`)!
     - Verify `app.publication_versions` rejects `UPDATE` and `DELETE` with `RF023` (`PUB-07`), and editing a draft after scheduling requires publishing a new `PublicationVersion`.
     - Verify `emergency-stop` immediately prevents worker from sending queued tasks (`AT-15`).
   - **Real OS Process `SIGKILL` Crash Recovery Suite (`OC-02` — NO in-process simulation!):**
     - Start a real local mock messenger HTTP server (on `127.0.0.1` test port via injectable transport/test dialer in child worker process) tracking exact received HTTP requests.
     - Spawn **real OS child worker processes** (`subprocess.Popen([sys.executable, "-m", "relayforge.workers.dispatcher", ...])`) and kill them with **`os.kill(proc.pid, signal.SIGKILL)`** (`assert proc.wait() == -signal.SIGKILL`), proving **all 5 scenarios**:
       1) `SIGKILL` right after `RESERVED` claim (before `DISPATCHING_IN_FLIGHT`) -> after lease expiry, Reaper moves task `RESERVED -> READY`, second worker process delivers it -> `mock_server_post_count == 1`.
       2) `SIGKILL` right after `DISPATCHING_IN_FLIGHT` commit **before** HTTP POST leaves worker -> Reaper moves task `DISPATCHING_IN_FLIGHT -> UNCERTAIN`; second worker process sends **0** duplicates (`mock_server_post_count == 0`); `reconcile` without `acknowledge_duplicate_risk=True` is rejected (`422`); `reconcile(CONFIRM_NOT_SENT_REQUEUE, acknowledge_duplicate_risk=True)` moves to `READY` and worker delivers (`mock_server_post_count == 1`).
       3) `SIGKILL` **after** mock server receives HTTP POST (before worker commits `SENT`) -> Reaper moves task `DISPATCHING_IN_FLIGHT -> UNCERTAIN`; worker sends **0** duplicates (`mock_server_post_count == 1`); `reconcile(CONFIRM_SENT)` moves task to `SENT` with full `audit_events` + `reconciliation_records`!
       4) Stale fencing epoch (`RF014`): pause/stall worker 1 past lease expiry -> Reaper increments `lease_epoch` -> worker 1 attempting `fn_worker_transition_task` with stale epoch is rejected by PostgreSQL with `sqlstate == 'RF014'`!
       5) Heartbeat extension: slow mock server response (`> initial_ttl`, with heartbeat active) extends `lease_expires_at` and completes `SENT` cleanly without Reaper stealing it!
     - Print/record structured `OC-06` JSON evidence confirming `sigkill_exit_codes == [-9, -9, -9]`, `duplicate_posts == 0`, `lost_tasks == 0`.

---

## 4. Verification & PR Checklist

1. `ruff check .`
2. `lint-imports`
3. `python scripts/gates/check_ast_gates.py`
4. `pytest -v tests/unit tests/integration` (all `M0 + M1 + M2 + M3` suites green on PostgreSQL 16!)
5. Confirm `0` diff on `RelayForge_Architecture/` and `PROJECT_MASTER_ROADMAP.md`, and push branch `feat/m3-rules-drafts-fair-queue-and-sigkill` + open PR to `main`.
