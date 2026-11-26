# Work Order `WO-08` — Final Milestone `M7` (+ `M6` Full-Scope Polish): 17-Vector Security Suite (`AT-20`), 50-Tenant Mixed-Load Benchmark (`OC-07`), Lean 2-Image `Ubuntu 24.04 LTS` Production Bundle (`OC-08`, `SEC-13`) & `AT-22` Live Probe CLI

- **Target Repository:** `https://github.com/mytest19861986/RelayForge-Web`
- **Base Branch:** `main` (includes merged `M0..M6` through `PR #7`)
- **Feature Branch to Create & Push:** `feat/m7-security-load-benchmark-and-ubuntu2404-bundle`
- **Architecture & Contract References (Read-Only — DO NOT MODIFY):**
  - `RelayForge_Architecture/15_SECURITY_ARCHITECTURE.md`
  - `RelayForge_Architecture/16_THREAT_MODEL.md`
  - `RelayForge_Architecture/17_UI_UX_ARCHITECTURE.md`
  - `RelayForge_Architecture/18_DEPLOYMENT_ARCHITECTURE.md`
  - `RelayForge_Architecture/19_RELIABILITY_AND_RECOVERY.md`
  - `RelayForge_Architecture/20_PERFORMANCE_AND_CAPACITY.md`
  - `RelayForge_Architecture/21_TEST_ARCHITECTURE.md`
  - `RelayForge_Architecture/contracts/backup_manifest.schema.json`
  - `PROJECT_MASTER_ROADMAP.md` (`WO-22`, `WO-23`, `WO-24`, `OC-06`, `OC-07`, `OC-08`, `OC-09`, `SEC-13`, `AT-20..AT-22`)

---

## 0. Strict Guardrails & Zero-Bloat Rules

1. **Read-Only Blueprint Rule:** Do **NOT** modify any file inside `RelayForge_Architecture/` or `PROJECT_MASTER_ROADMAP.md` (`0` diff required).
2. **Strict 2-Image & Zero-Bloat Policy (`PROJECT_MASTER_ROADMAP.md` §2):**
   - Total custom Dockerfiles in the entire repository MUST be **at most 2**:
     1. `deploy/Dockerfile.app` (lean multi-stage `python:3.12-slim` non-root image shared across `web-api`, `worker-engine` / `dispatcher-worker`, `export-ingestor`, `media-jobs`, and one-shot `migrate-job` via command arguments),
     2. `deploy/Dockerfile.media-worker` (existing lean `alpine:3.20` non-root `10001:10001` `AF_UNIX` renderer).
   - Zero `node_modules`, zero `MinIO` (`SeaweedFS -s3` `Apache-2.0` only — `OC-09`), zero `boto3`, zero `GPLv3/AGPL` Python dependencies, and total tracked Git file size strictly `< 15 MB` (`< 2 MB` expected). All runtime test/benchmark outputs go under git-ignored `tmp/`.
3. **Sandbox Pre-Approval:**
   - Running native Linux `PostgreSQL 16`, `ffmpeg`, and `media-worker` Unix-socket processes inside your sandbox when nested `containerd`/Docker is unavailable is **explicitly pre-approved**; GitHub Actions (`ubuntu-24.04`) executes native Ubuntu 24.04 verification automatically on push/PR. Proceed straight through `git push` and opening the Pull Request to `main`.

---

## 1. Deliverables & Technical Requirements

### A. Close Remaining `M6` Full-Scope Items (`TENANT_FULL_LOGICAL` Backup + Structured Persian RTL Studio Forms)

1. **`TENANT_FULL_LOGICAL` Backup & Safe-Mode Restore (`src/relayforge/adapters/tenant_backup.py`, `src/relayforge/web/ops_m6.py`, `BAK-01..05`, `AT-19`):**
   - Extend `BackupInput` with `backup_scope: Literal["TENANT_SETTINGS_EXPORT", "TENANT_FULL_LOGICAL"] = "TENANT_SETTINGS_EXPORT"` and `include_media_assets: bool = False`.
   - In `TENANT_FULL_LOGICAL` mode (strictly under `SET LOCAL app.tenant_id`, zero `BYPASSRLS`), package tenant-scoped `logical/tables/<table>.jsonl` entries covering workspace operational records (`brand_identities`, `sources`, `destinations`, `style_profiles`, `style_revisions`, `routes`, `route_policy_revisions`, `drafts`, `draft_revisions`, `podcast_scripts`, `media_assets`) plus `config/workspace.json`, optional `secrets/tenant_vault_rekeyed.aead`, and (when `include_media_assets=True`) S3 media objects at `media/{sha256[:2]}/{sha256}.{ext}` matching `contracts/backup_manifest.schema.json`.
   - Keep strict size & entry bounds (`contracts/backup_manifest.schema.json`), store larger archives in S3 `tenants/{tenant_id}/backups/{manifest_id}.rfbak` (while continuing to support `bundle_base64` or `manifest_id` on `/backups/restore`), report per-table & media counts in `mode="PREVIEW"` with zero DB writes, and in `mode="APPLY"` restore records + S3 media objects idempotently within `SET LOCAL app.tenant_id`, re-wrap secrets under the destination `Tenant DEK`, and **unconditionally enforce Safe Mode** (`emergency_stop = true`, all `routes.is_enabled = false`, all `destinations.is_enabled = false`, `allow_external_ai = false`, `external_consents.is_granted = false`).

2. **Structured Persian RTL Studio UI Completion (`src/relayforge/web/static/relayforge_ui/views.js` & `studio.js`, `17_UI_UX_ARCHITECTURE.md`):**
   - Replace raw generic `"تنظیمات JSON"` (`spec` textarea) forms in `views.js` with structured Persian form controls across all 12 views (advanced optional JSON override may stay inside a `<details><summary>تنظیمات پیشرفته</summary>...</details>` progressive-disclosure drawer, never as the only primary input):
     - **Destinations:** Render explicit platform maturity badges (`فعال GA` for `telegram`/`bale`, `فقط ارسال خروجی` for `eitaa`, and orange `آزمایشی — نیازمند تست زنده` for `rubika`/`soroush_plus`) and structured fields for destination registration & `test-publish` (`body_text` + confirmation).
     - **Content Studio:** Render structured source/candidate view, `Fact Guard` verdict & diff highlights when present, and a collapsible `<details>` «تنظیمات پیشرفته» drawer for routing/model/style options.
     - **Channel Intelligence, Styles, Media & Podcast, Drafts, Delivery Reconciliation & Operations:** Structured inputs for starting channel analysis, style creation/weighted composition, media jobs (`INFOGRAPHIC_RENDER`, `NATIVE_STICKER`, `LOCAL_SLIDESHOW_CLIP`, `PODCAST_TTS`) & two-speaker podcast script creation/approval, draft multi-destination preview (showing per-platform capability degradation warnings) & live publish, `UNCERTAIN` reconciliation (`expected_lease_epoch`, `resolution_action` select, `reason`, and mandatory `acknowledge_duplicate_risk` checkbox), `TENANT_SETTINGS_EXPORT` vs `TENANT_FULL_LOGICAL` backup selector, and Owner **Delete Account** action disclosing the **14-day physical PITR retention window** (`SEC-09`).
   - Keep `localStorage` and `sessionStorage` 100% unused for any token/secret/session state.

---

### B. `WO-22` — Automated 17-Vector Adversarial Security & Isolation Suite (`AT-20`, `AT-01`, `AT-21`)

Create `tests/integration/test_m7_adversarial_security_17_vectors.py` asserting non-vacuous blocking across **all 17 threat vectors**:
1. **XSS & CSP (`SEC-02`):** Malicious `<script>alert(1)</script>` / event-handler payloads escaped in rich-text HTML compiler & UI responses; `Content-Security-Policy` (`default-src 'self'; frame-ancestors 'none'; object-src 'none'`) enforced on UI routes.
2. **CSRF Enforcement (`SEC-01`):** State-changing `POST/PUT/PATCH/DELETE` with valid session cookie but missing or forged `X-CSRFToken` rejected (`403`).
3. **Cross-Origin Forgery (`SEC-01`):** State-changing request with untrusted `Origin: https://evil.example` rejected (`403`).
4. **Direct SSRF (`SEC-06`):** `SafeDialer` blocks `127.0.0.1`, `169.254.169.254` (cloud metadata), `10.0.0.1`, and IPv6 `::1`.
5. **Redirect SSRF (`SEC-06`):** Public upstream returning `302 Location: http://127.0.0.1:...` or `http://169.254.169.254/...` is re-validated hop-by-hop and blocked before socket connect.
6. **Prompt Injection & Fact Tamper (`SEC-08`, `AT-08`):** Untrusted channel text attempting system-delimiter escape + altering `۲۵۰ هزار تومان` to `۲۵ میلیون تومان` is caught and blocked (`FACT_MISMATCH_BLOCKED`) in `STRICT`/`BALANCED` modes.
7. **Template Injection (`AT-20`):** `{body.__class__}`, `{0}`, missing `{body}`, or double `{body}` in `SafePlaceholderTemplate` rejected deterministically.
8. **Linear-Time `ReDoS` Immunity (`RULE-09`):** Pathological regex `(a+)+$` against `'a' * 4000 + '!'` completes in `< 50 ms` via `ThompsonNFA`.
9. **Path Traversal / Symlink Escape (`SEC-05`, `AT-20`):** `../` paths, absolute paths, and symlinks rejected in both `media-worker` staging and `.rfbak` backup manifests.
10. **Hostile Stream / Oversize Export (`OC-04`):** Deeply nested JSON (`> 32` levels), oversized token (`> 256 KiB`), and gzip/zip magic-byte uploads rejected deterministically.
11. **Media Pixel Bomb & Protocol Smuggling (`SEC-05`):** `> 40 MP` image decompression bomb and `ffconcat` / `#EXTM3U` / `http://` media inputs rejected before `ffmpeg`.
12. **Cross-Tenant REST RLS Isolation (`AT-01`):** `Tenant A` cannot read, patch, publish, reconcile, or delete `Tenant B`'s workspaces, drafts, routes, deliveries, media, or backups (`404` / `0` rows).
13. **Cross-Tenant S3 Key Isolation (`SEC-05`):** Attempt to reference or clean up `tenants/<other_tenant_id>/...` S3 keys rejected.
14. **Envelope Vault AAD Binding (`SEC-03`):** Replaying `Tenant A`'s `ciphertext_aead` under `Tenant B`, another `workspace_id`, or another `secret_kind` fails AES-GCM authentication (`InvalidTag`).
15. **Brand/Workspace Learning Isolation (`AT-17`):** Learned preference rules in `Brand 1` / `Workspace 1` never leak into `Brand 2` / `Workspace 2`.
16. **Operator Plan Quota Tamper (`OC-01`, `RF007`):** `relayforge_web` SQL attempt to `INSERT`, `UPDATE`, or `DELETE` `policy_origin = 'OPERATOR_PLAN'` raises `sqlstate == 'RF007'`.
17. **Append-Only & Immutability Tamper (`RF021`, `RF023`, `RF010`):** `UPDATE`/`DELETE` on `app.audit_events` raises `RF021`, `UPDATE`/`DELETE` on `publication_versions` / `route_policy_revisions` / `style_revisions` raises `RF023`, and `DELETE` on `delivery_tasks` raises `RF010`; plus an end-to-end authenticated Studio workflow check (`AT-21`).

---

### C. `WO-23` — 50-Tenant Concurrent Mixed-Load Benchmark Closing `OC-07`

Create `tests/integration/test_m7_50_tenant_mixed_load_oc07.py`:
1. Provision **50 active, isolated tenants** on PostgreSQL 16 (each with its own workspace, route/destination, and queued `READY` delivery tasks).
2. Start a real `media-worker` daemon over `AF_UNIX` and keep it **actively rendering concurrent `ffmpeg` slideshow clips & Persian infographics** throughout the benchmark window so real CPU/process contention is present on the host (`OC-07`).
3. Simultaneously drive:
   - Concurrent authenticated `web-api` requests across all 50 tenants (`/dashboard`, `/events/poll`, `/deliveries`), AND
   - Concurrent `dispatcher-worker` Water-Filling claim + delivery cycles (`fn_worker_claim_next_jobs` -> `DISPATCHING_IN_FLIGHT` -> `SENT`) against a fast in-process local mock messenger transport.
4. Measure wall-clock latencies and assert non-vacuously:
   - `errors_5xx == 0` and zero DB/pool errors,
   - `cross_tenant_leaks == 0` and `duplicate_deliveries == 0`,
   - `completed_media_renders >= 2` overlapped with the web/dispatcher load window,
   - `p95_ms <= 500.0` and **`p99_ms <= 500.0`** (`20_PERFORMANCE_AND_CAPACITY.md` §1 & `OC-07`).
5. Write `OC-06`-compliant machine-readable evidence to `tmp/m7-oc07-load-evidence.json` containing `git_commit_sha`, `sql_sha256`, `timestamp_utc`, `tenant_count: 50`, `total_web_requests`, `dispatched_tasks`, `completed_media_renders`, `p50_ms`, `p95_ms`, `p99_ms`, and `pass_threshold_ms: 500.0`.

---

### D. `WO-24` — Lean `Ubuntu 24.04 LTS` Production Bundle (`OC-08`), Supply-Chain Gate (`SEC-13`) & `AT-22` Probe CLI

1. **Production Deployment Bundle (`deploy/`, `18_DEPLOYMENT_ARCHITECTURE.md`):**
   - `deploy/Dockerfile.app`: Multi-stage `python:3.12-slim` image running as non-root UID `10000:10000` (installing only `.` runtime wheels, zero compilers in final stage) shared by `web-api`, `worker-engine`, `export-ingestor`, `media-jobs`, and one-shot `migrate-job`.
   - `deploy/Dockerfile.media-worker`: Keep lean `alpine:3.20` non-root `10001:10001` renderer (ensuring **strictly 2 `Dockerfile*` files** in the repo).
   - `deploy/docker-compose.prod.yml`:
     - Services: `edge-proxy` (`caddy:2.8-alpine` mounting `deploy/Caddyfile` TLS 1.2/1.3), `web-api`, `worker-engine`, `media-jobs`, `media-worker` (`network_mode: "none"`, `cpus: "1.5"`, `mem_limit: "1024m"`, `memswap_limit: "1024m"`, `pids_limit: 64`, `read_only: true`, `cap_drop: ["ALL"]`, `security_opt: ["no-new-privileges:true"]`), one-shot `migrate-job` (`relayforge_migrator` direct to `postgres:5432`), `pgbouncer` (`pool_mode = transaction` via `deploy/pgbouncer/pgbouncer.ini`), `postgres` (`postgres:16-alpine`), and `s3-store` (`chrislusf/seaweedfs` `server -s3`, `Apache-2.0` — `OC-09`).
     - Networks: `edge_net`, `data_net` (`internal: true` — zero internet egress for `postgres`, `pgbouncer`, `s3-store`!), and `egress_net`. Zero public host port bindings on `5432` or `6432`!
   - `deploy/systemd/relayforge.service` & `deploy/ufw/setup_ufw.sh` (`80/tcp`, `443/tcp`, SSH key-only; compatible with Ubuntu 24.04 `apparmor_restrict_unprivileged_userns=1` and zero `privileged: true`).

2. **Supply-Chain, License, Secret & Bloat Gate (`scripts/gates/check_supply_chain.py` — `SEC-13`):**
   - Verify via `importlib.metadata` that every installed runtime package uses a permissive license (`MIT`, `BSD`, `Apache`, `PSF`, `ISC`, `MPL-2.0`, `CMU`/`HPND`) and **zero `GPLv3` / `AGPL` packages** are present.
   - Emit CycloneDX 1.5 SBOM JSON to `tmp/sbom-cyclonedx.json`.
   - Scan all `git ls-files` for accidentally committed private keys (`BEGIN .* PRIVATE KEY`), live bot/API tokens, forbidden artifacts (`node_modules/`, `.venv/`, `*.rfbak`), tracked size `< 15_000_000` bytes, `Dockerfile*` count `<= 2`, and validate `deploy/docker-compose.prod.yml` security invariants (`network_mode: "none"`, `internal: true`, `cpus`, `mem_limit`, `pids_limit`, zero `privileged: true`).

3. **Native `Ubuntu 24.04 LTS` Host Acceptance Verifier (`scripts/gates/verify_ubuntu2404_host.py` — Closing `OC-08`):**
   - Inspects Linux kernel (`platform.release()`), `/etc/os-release` (enforcing `Ubuntu 24.04` when `--require-ubuntu-2404` is passed in GitHub Actions), verifies unprivileged execution (`os.geteuid() != 0` or non-privileged subprocess/AF_UNIX `0600` confinement without needing `apparmor_restrict_unprivileged_userns=0`), verifies `PostgreSQL 16` + `ffmpeg` + `database_schema.sql` SHA-256, and writes `OC-06` evidence to `tmp/m7-oc08-ubuntu2404-evidence.json`.

4. **Owner-Authorized Live Capability Probe CLI (`src/relayforge/adapters/live_probe_cli.py` — `AT-22`):**
   - Implement `python -m relayforge.adapters.live_probe_cli` (with unit tests using mock transports; **never** making live external network calls unless `--confirm-live-probe` is explicitly passed by the Product Owner with env tokens).
   - Supports probing `telegram`, `bale`, `eitaa`, `rubika`, and `soroush_plus` (`test_connection` + optional `send_test_post`), scrubs 100% of tokens via `scrub_sensitive_text`, and writes a structured capability & maturity verification report (`tmp/at22-live-probe-report.json`).

---

## 2. Mandatory CI & Final Acceptance Gates Before Opening PR

Update `.github/workflows/ci-m0.yml` (on `ubuntu-24.04`) so the workflow runs:
1. `ruff check .`
2. `lint-imports`
3. `python scripts/gates/check_ast_gates.py` (`OC-06`)
4. `python scripts/gates/check_supply_chain.py` (`SEC-13` + 2-Dockerfile & `<15MB` Zero-Bloat gate)
5. `python scripts/gates/verify_ubuntu2404_host.py --require-ubuntu-2404` (`OC-08` native Ubuntu 24.04 kernel gate)
6. `pytest -v tests/unit tests/integration -s` (running **all `M0..M7` tests**, including the 4 SQL mutants, 5-scenario `SIGKILL` `OC-02`, hostile stream `OC-04`, physical `PostgreSQL 16` `PITR` `OC-03`, `TENANT_FULL_LOGICAL` Safe-Mode restore `AT-19`, 17-vector security suite `AT-20`, and 50-tenant mixed-load + active `ffmpeg` contention benchmark `OC-07`)
7. Upload `tmp/*.json` (`OC-02/03/04/06/07/08` + `SBOM` evidence files) via `actions/upload-artifact@v4`!
8. Confirm `git diff --name-only origin/main...HEAD -- RelayForge_Architecture PROJECT_MASTER_ROADMAP.md` is empty (`0` modified files), push `feat/m7-security-load-benchmark-and-ubuntu2404-bundle`, and open the Pull Request to `main`.
