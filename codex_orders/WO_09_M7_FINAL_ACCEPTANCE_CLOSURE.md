# Work Order `WO-09` — Final Acceptance Closure (`M7` 100% Sign-Off): Complete 17-Vector Matrix (`V01..V17`), Full Media & Secret-Less Logical Restore, and Real 2-Image Docker Smoke in CI

- **Target Repository:** `https://github.com/mytest19861986/RelayForge-Web`
- **Base Branch:** `main` (includes merged `M0..M7` through `PR #8`)
- **Feature Branch to Create & Push:** `feat/m7-final-closure-17vectors-media-and-docker-smoke`
- **Read-Only Blueprint Rule:** Do **NOT** modify any file inside `RelayForge_Architecture/` or `PROJECT_MASTER_ROADMAP.md` (`0` diff required).

---

## 1. Surgical Scope to Reach 100% Unconditional `M0..M7` Sign-Off

Your honest `PR #8` summary noted three remaining verification gaps before unconditional final acceptance. Close all three completely in this branch:

### A. Full Media & Secret-Less `TENANT_FULL_LOGICAL` Restore (`src/relayforge/web/logical_m7.py`, `src/relayforge/web/ops_m6.py`, `AT-19`)
1. **Fix Secret-Less Destination Restore (`BAK-02..05`):**
   - `app.destinations.secret_id` is `NOT NULL`, while `includes_reencrypted_secrets` is optional (`BAK-02`). Currently `logical_m7.py` raises `DESTINATION_SECRET_REQUIRED` if a workspace with destinations is backed up with `includes_reencrypted_secrets=False` and restored into a workspace/tenant where that secret does not exist.
   - Update `apply_logical` (or `ops_m6.py` + `logical_m7.py`) so that when `includes_reencrypted_secrets=False` and a restored `destination`'s `secret_id` is absent in the target workspace, a **revoked placeholder secret** (`is_revoked=True`, `masked_hint="[REKEY_REQUIRED]"`, encrypted placeholder value under the target tenant's active `Tenant DEK`) is deterministically created (`ON CONFLICT (id) DO NOTHING`), allowing `destinations` (`is_enabled=False`, `publish_verified_at=None`) and their `routes` (`is_enabled=False`) to restore cleanly in Safe Mode until the owner supplies a new token.
2. **Verify All Media & S3-Backed Restore Paths in Integration Tests:**
   - Add integration tests proving:
     1) `TENANT_FULL_LOGICAL` with `include_media_assets=True` and `includes_reencrypted_secrets=True` across two isolated tenants: restores S3 media bytes (`tenants/<target_tenant>/media/<sha256>`), `media_assets`, `drafts` + `draft_revisions` + `media_asset_refs`, `destinations` + `routes`, re-wraps secrets under the destination `Tenant DEK` (verifying the restored secret decrypts under Tenant B's DEK), and enforces **Safe Mode** (`emergency_stop=True`, all `routes.is_enabled=False`, all `destinations.is_enabled=False`, `allow_external_ai=False`, `external_consents.is_granted=False`),
     2) `TENANT_FULL_LOGICAL` with `include_media_assets=True` and `includes_reencrypted_secrets=False` on a workspace containing a `destination` + `route`: succeeds cleanly in Safe Mode using the revoked placeholder secret (`is_revoked=True`),
     3) S3-stored backup restore via `manifest_id` (`bundle_base64=None`, e.g. when archive exceeds inline threshold or forced S3 path) succeeds for the owning tenant and rejects cross-tenant `manifest_id` access.

### B. Complete All 17 Explicit Security Vectors (`V01..V17`) + `AT-21` Journey (`tests/integration/test_m7_adversarial_security_17_vectors.py`)
Expand `tests/integration/test_m7_adversarial_security_17_vectors.py` so **every single one of the 17 threat vectors (`V01..V17`)** plus `AT-21` is explicitly exercised and recorded in `tmp/m7-at20-17vectors-evidence.json`:
- `V01`: Stored/compiled XSS neutralization (`<script>`, `onerror=`) + UI `Content-Security-Policy` (`default-src 'self'; frame-ancestors 'none'; object-src 'none'`).
- `V02`: Missing & forged `X-CSRFToken` rejected (`403`) on state-changing cookie requests.
- `V03`: Untrusted `Origin` header (`https://untrusted.example`) rejected (`403`).
- `V04`: Direct SSRF (`127.0.0.1`, `169.254.169.254`, `10.0.0.1`, `::1`) blocked by `SafeDialer` / `validate_public_ip`.
- `V05`: **Redirect SSRF (`302` hop from public upstream to `http://127.0.0.1:...` or `http://169.254.169.254/...`)** re-validated and blocked before connecting to the redirect target.
- `V06`: **Prompt Injection + Fact Tamper**: adversarial source text containing system-delimiter escape instructions + altered numeric price (`۲۵۰ هزار تومان` -> `۲۵ میلیون تومان`) uses random-nonce boundary prompts and is blocked (`FACT_MISMATCH_BLOCKED`) in `STRICT`/`BALANCED`.
- `V07`: `{body}` `SafePlaceholderTemplate` format-string/attribute injection (`{body.__class__}`, `{0}`, missing/double `{body}`) rejected.
- `V08`: Pathological `ReDoS` `(a+)+$` on `'a' * 4000 + '!'` completes in `< 50 ms` via `ThompsonNFA`.
- `V09`: Path traversal (`../`), absolute paths, and symlinks rejected in both `media-worker` staging and `.rfbak` backup manifests.
- `V10`: Hostile export stream (`> 32` JSON depth, `> 256 KiB` token, gzip/zip bombs) rejected + S3 presigned policy enforces `content-length-range` and tenant prefix.
- `V11`: **Media Pixel Decompression Bomb (`> 40 MP` image header/dimensions, e.g. `8000x6000 = 48 MP`)** rejected by `media_validation` / upload endpoint, plus forbidden `ffconcat` / `#EXTM3U` / `http://` rejected before `ffmpeg`.
- `V12`: Cross-tenant REST RLS isolation (`Tenant A` gets `404` / `0` rows against `Tenant B`'s workspace, drafts, routes, deliveries, media, and backups).
- `V13`: **Cross-tenant S3 object key isolation** (`tenants/<other_tenant>/...` rejected in export/media/backup key checks and cleanup).
- `V14`: **`EnvelopeSecretVault` AAD binding**: replaying `Tenant A`'s `ciphertext_aead` under `Tenant B`, wrong `workspace_id`, or wrong `secret_kind` raises cryptographic `InvalidTag`.
- `V15`: **Cross-workspace & cross-brand learning isolation (`AT-17`)**: `learned_preference_rules` created in Workspace/Brand 1 are invisible in Workspace/Brand 2.
- `V16`: **Operator Plan ceiling tamper (`OC-01`, `RF007`)**: `relayforge_web` SQL `INSERT`, `UPDATE`, and `DELETE` against `policy_origin = 'OPERATOR_PLAN'` all fail with `sqlstate == 'RF007'`.
- `V17`: **Database immutability & state-machine guards (`RF021`, `RF023`, `RF010`)**: `UPDATE`/`DELETE` on `audit_events` fails with `RF021`; `UPDATE`/`DELETE` on `publication_versions`, `route_policy_revisions`, and `style_revisions` all fail with `RF023`; `DELETE` on `delivery_tasks` fails with `RF010`.
- `AT-21`: End-to-end authenticated Studio workflow (login -> UI shell & structured `views.js` asset fetch -> create source & destination -> create draft -> per-destination preview -> explicit confirmed publish -> dispatcher delivery -> dashboard & CSV-safe report verification), writing `tmp/m7-at20-17vectors-evidence.json` (`assert len(evidence["verified_vectors"]) == 17`).

### C. Align `SEC-13` License Policy & Run Real Compose + 2-Image Docker Smoke in CI (`.github/workflows/ci-m0.yml`, `scripts/gates/check_supply_chain.py`)
1. **License Policy Alignment (`18_DEPLOYMENT_ARCHITECTURE.md` §3 & `05_MASTER_ARCHITECTURE.md`):**
   - `18_DEPLOYMENT_ARCHITECTURE.md` §3 explicitly specifies blocking viral copyleft **`GPLv3` / `AGPLv3`** packages (*"مسدود کردن هرگونه کتابخانه دارای مجوز `GPLv3/AGPLv3`"*), while `psycopg 3` (`LGPL-3.0`, dynamic Python library import without copyleft propagation) is locked by `05_MASTER_ARCHITECTURE.md` & `ADR-002`. Update `check_supply_chain.py` so `LGPL-3.0` on `psycopg`/`psycopg-binary` is recognized as an architecture-sanctioned linking license (not a policy violation), while strictly failing on any `GPL` / `AGPL` package or unknown non-permissive dependency.
2. **Real Docker Compose Validation & 2-Image Build/Run Smoke in `.github/workflows/ci-m0.yml` (`ubuntu-24.04`):**
   - Add a CI verification step on `ubuntu-24.04` that:
     1) Writes a temporary synthetic `.env.prod` (deleted after the step, never committed) and runs `docker compose --project-directory deploy --env-file <tmp_env> -f deploy/docker-compose.prod.yml config -q` to prove 100% valid Compose syntax, interpolation, networks, and volumes,
     2) Builds both production images (`docker build -f deploy/Dockerfile.app -t relayforge-app:ci .` and `docker build -f deploy/Dockerfile.media-worker -t relayforge-media:ci .`),
     3) Runs non-root smoke checks on both built containers:
        - `relayforge-app:ci`: verifies `id -u` is `10000` and `python -c "import relayforge.web.wsgi"` succeeds,
        - `relayforge-media:ci`: runs with `--network none --read-only --cap-drop ALL` over a temp volume, pings `media_worker` over `AF_UNIX`, and renders a real PNG infographic inside the isolated container!

---

## 2. Final Gate Checklist Before Opening PR

1. `ruff check .` -> `0` errors.
2. `lint-imports` -> `0` broken contracts.
3. `python scripts/gates/check_ast_gates.py` -> `0` violations.
4. `python scripts/gates/check_supply_chain.py` -> `0` violations (`2` Dockerfiles, `< 15 MB` tracked size, `0` GPL/AGPL, CycloneDX SBOM emitted).
5. `python scripts/gates/verify_ubuntu2404_host.py --require-ubuntu-2404` -> passes on CI.
6. `pytest -v tests/unit tests/integration -s` -> 100% green across `M0..M7`, emitting all `tmp/*.json` evidence files (`m6-oc03-pitr-evidence.json`, `m7-oc02-evidence.json`, `m7-oc04-evidence.json`, `m7-oc06-db-evidence.json`, `m7-oc07-load-evidence.json`, `m7-oc08-ubuntu2404-evidence.json`, `m7-at20-17vectors-evidence.json`, `sbom-cyclonedx.json`).
7. Docker Compose config + 2-image non-root `--network none` container smoke step passes in GitHub Actions `ubuntu-24.04`.
8. `git diff --name-only origin/main...HEAD -- RelayForge_Architecture PROJECT_MASTER_ROADMAP.md` -> empty (`0` modified files).
9. Push `feat/m7-final-closure-17vectors-media-and-docker-smoke` and open the Pull Request to `main`.
