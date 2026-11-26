# Codex Work Order `WO-06` (Phase `M5`): Isolated `media-worker` (`AF_UNIX`), Media Library, Stock Search, Persian Infographic/Sticker/Clip Studio, Podcast TTS & `AutoPublishRiskGate`

- **Project:** RelayForge Web SaaS (`v2.0.0`)
- **Repository:** `https://github.com/mytest19861986/RelayForge-Web`
- **Base Branch:** `main` (includes merged Phases `M0..M4`)
- **Target Feature Branch / PR:** `feat/m5-media-studio-podcast-worker-and-risk-gate` -> open Pull Request to `main`
- **Roadmap Coverage:** Phase `M5` (`WO-16` + `WO-17` + `WO-18` in `PROJECT_MASTER_ROADMAP.md`, Module `M08` + Module `M09` Advanced Auto-Orchestrator)
- **Authoritative Contracts (DO NOT MODIFY):**
  - `RelayForge_Architecture/contracts/database_schema.sql`
  - `RelayForge_Architecture/contracts/openapi.yaml`
  - `RelayForge_Architecture/12_MEDIA_ARCHITECTURE.md`
  - `RelayForge_Architecture/13_PUBLICATION_ENGINE.md`
  - `RelayForge_Architecture/adr/ADR-012-media-worker-isolated-container-and-generative-jobs.md`

---

## 1. Sandbox Execution & Zero-Bloat Rules (MANDATORY)

1. **Native Linux Sandbox Approved:**
   - Run `media-worker` directly as a local Linux daemon over a temporary `AF_UNIX` socket + native `PostgreSQL 16.x` + `ffmpeg` (if `ffmpeg` is installed in sandbox/CI; in `.github/workflows/ci-m0.yml` add `sudo apt-get update && sudo apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core` so GitHub Actions `ubuntu-24.04` executes real `ffmpeg` clip/audio transcoding!).
2. **Strict Zero-Bloat Dependency Rule:**
   - The **ONLY** new Python dependency allowed in `pyproject.toml` for `M5` is **`Pillow>=10.4,<12.0`** (~3.5 MB binary wheel).
   - Do **NOT** add `opencv`, `moviepy`, `pydub`, `playwright`, `cairo`, `weasyprint`, or AI SDKs!
   - Implement Persian/Arabic contextual glyph joining + BiDi visual ordering for deterministic infographic rendering in ~80 lines of pure Python (`src/relayforge/core/persian_text_render.py` using Unicode Arabic Presentation Forms-B contextual table + RTL digit/run ordering) rendered onto `Pillow` `ImageDraw` with `DejaVuSans.ttf` (or Pillow fallback) so Persian letters & Persian digits (`۰-۹`) render legibly on every Linux runner without external C shaping libraries!
   - Keep `RelayForge_Architecture/` and `PROJECT_MASTER_ROADMAP.md` **100% untouched** (`0` diff).

---

## 2. Exact Scope of Phase `M5` (`M08 + M09 Advanced`)

### 2.1 Forward Migration `migrations/0006_m5_media_podcast_and_auto_routes.sql`
Register `0006_m5_media_podcast_and_auto_routes` in `MIGRATIONS` inside `src/relayforge/adapters/db_migrator.py` while preserving `100%` `FORCE RLS` and **`0` non-superuser `BYPASSRLS` roles (`OC-10`)**:
1. Add any narrow column/index or `relayforge_worker` / `relayforge_web` grants needed on `app.media_assets`, `app.media_asset_refs`, `app.podcast_scripts`, `app.media_jobs`, `app.routes`, `app.route_policy_revisions`, and `app.generation_sessions` (e.g., `tts_segment_cache` JSONB or dedicated `ALTER TABLE app.podcast_scripts ADD COLUMN IF NOT EXISTS segment_cache_json JSONB NOT NULL DEFAULT '{}'::jsonb;` for segment-level TTS caching `AUD-05`, and `worker_pid` / `cancel_requested BOOLEAN NOT NULL DEFAULT false` on `app.media_jobs` for mid-flight job cancellation `MED-11, MED-12`!).
2. Ensure `relayforge_dispatcher_owner` / `relayforge_worker` can claim `PENDING` `app.media_jobs` across active tenants safely via a narrow `SECURITY DEFINER` function `app.fn_worker_claim_media_job()` (`FOR UPDATE SKIP LOCKED`, returning `(job_id, tenant_id, workspace_id, job_kind, input_spec_json, reservation_id)`), owned by `relayforge_dispatcher_owner` with narrow `SELECT/UPDATE` grant & RLS policy on `app.media_jobs`.

---

### 2.2 Isolated Networkless `media-worker` Daemon over `AF_UNIX` (`src/relayforge/workers/media_worker.py` + `deploy/Dockerfile.media-worker` — `SEC-07`, `ADR-012`, `SPK-05`)
1. **Daemon Architecture (`media_worker.py`):**
   - Listens **exclusively** on an `AF_UNIX` stream socket (`RF_MEDIA_SOCKET_PATH`), zero database imports/connections, zero outbound HTTP imports!
   - Accepts length-prefixed / newline JSON RPC commands (`render_infographic`, `build_sticker`, `compose_clip_ffmpeg`, `assemble_podcast_audio`, `cancel_job`, `ping`).
   - **Hard Sandbox Defenses (`SEC-07`, `AT-20`):**
     - **Path confinement:** all input/output files must reside strictly inside `staging_root / <job_id>` (`Path.resolve()` prefix check + reject `..`, reject any symlink via `.is_symlink()`, delete `<job_id>` staging directory deterministically in `finally:`).
     - **Decompression-bomb guard:** set `PIL.Image.MAX_IMAGE_PIXELS = 40_000_000` (`40 MP`); validate real magic bytes (`PNG`, `JPEG`, `WEBP`, `GIF`, `MP4`, `OGG`, `MP3`, `WAV`) — reject `.exe`/ELF/script disguised as `.jpg` (`MED-01`, `SEC-05`)!
     - **FFmpeg safe flags:** invoke `ffmpeg` ONLY with `["ffmpeg", "-nostdin", "-y", "-protocol_whitelist", "file,pipe", ...]` — explicitly reject inputs containing `ffconcat`, `#EXTM3U` (`hls`/`m3u8`), `http://`, `https://`, `tcp://`, `file://`!
     - **Process-group timeout, cancel & OOM survival:** spawn child `ffmpeg` / heavy render workers in `start_new_session=True`, track active `job_id -> pgid` so `cancel_job` or wall-clock timeout immediately calls `os.killpg(pgid, signal.SIGKILL)` (`TIMED_OUT` / `CANCELLED`), and classify child exit code `-9` / `137` (when not cancelled/timed-out) as `'OOM_KILLED'` while keeping the parent `media-worker` daemon alive!
2. **Media Production Capabilities (`AT-12`):**
   - **Deterministic Persian Infographic (`INFOGRAPHIC_RENDER` — `MED-09`):** renders title, editable key-value/table rows (converting digits to Persian `۰-۹` and shaping RTL text cleanly), brand palette (`visual_tokens_json` ONLY when `ownership_attested=True` — never sample channel's logo `IDN-06, MED-08`!), outputting verified `PNG` bytes + dimensions.
   - **Sticker Image vs Native `512px WebP` Sticker (`STICKER_IMAGE` vs `NATIVE_STICKER` — `MED-10`):** builds exact `512x512` RGBA `.webp` (`< 512 KiB`) for `NATIVE_STICKER` and clearly distinguishes it from standard `STICKER_IMAGE`!
   - **Local Composed Clip (`LOCAL_SLIDESHOW_CLIP` — `MED-11`):** checks `shutil.which("ffmpeg")` **before** any AI/budget spend (failing fast with `FFMPEG_UNAVAILABLE` and `0` cost if absent!), renders valid playable `.mp4` (`H.264/AAC` or `mpeg4` fallback depending on installed `ffmpeg` encoders) from 1+ images + motion/caption/audio.
   - **Podcast Audio Normalization & Assembly (`TTS_SYNTHESIS` — `AUD-03, AUD-04`):** concatenates & normalizes synthesized speaker segments via `ffmpeg` into playable `OGG/Opus` (`VOICE_NOTE`) or `MP3/M4A`/`WAV` (`AUDIO`), computing exact `duration_sec` and `byte_size`!
3. **Lean Production Container Spec (`deploy/Dockerfile.media-worker`):**
   - Minimal `alpine:3.20` (`ffmpeg`, `python3`, `py3-pillow`, `ttf-dejavu`), non-root user `10001:10001`, read-only rootfs ready (do **not** build Docker images during tests!).

---

### 2.3 Media Library, Stock Search, AI Image/Video/TTS & End-to-End Media Publishing (`M08` + `M03/M09`)
1. **Media Library & Active Reference Guard (`MED-01, MED-02, MED-13, MED-15`):**
   - `GET / POST /api/v1/workspaces/{wsId}/media/assets` & `DELETE /api/v1/workspaces/{wsId}/media/assets/{assetId}`:
     - `POST` validates magic bytes, size ceiling, and dimensions, deduplicates/stores content in S3 under `tenants/{tenant_id}/media/{sha256_hex}`, and records `app.media_assets`.
     - `DELETE` checks `app.media_asset_refs` (`ON DELETE RESTRICT` / active draft or non-terminal publication reference check) and blocks deletion with `409 MEDIA_ASSET_IN_USE` while referenced (`MED-13`)!
2. **Data-Minimized Licensed Stock Search & Download (`MED-03..05`, `AT-13`):**
   - `POST /api/v1/workspaces/{wsId}/media/suggest-query` (pure/deterministic or AI-assisted query minimizer stripping phone numbers, emails, `@handles`, numbers, and private names from draft text — `MED-03`, `SEC-10`!).
   - `POST /api/v1/workspaces/{wsId}/media/search` (`PexelsStockAdapter` over `SafeDialer`, gated strictly on `workspace_settings.allow_external_media_search` + `STOCK_MEDIA_SEARCH` consent, returning photographer/license/source URLs — `MED-04`).
   - Stock download stores `origin_kind='STOCK_LICENSED'` + immutable `license_attribution_json` (never stripping watermarks; `SafeDialer` blocks SSRF on stock download URLs — `MED-05`, `AT-13`, `AT-20`).
3. **Async Media Jobs, AI Image/Edit, Generative Video Polling & Segment-Cached Podcast TTS (`MED-06..12`, `AUD-01..05`, `AT-12`):**
   - `POST /api/v1/workspaces/{wsId}/podcast-scripts` & `PATCH .../podcast-scripts/{scriptId}`: creates/edits 1- or 2-speaker podcast script **before** spending any TTS budget (`AUD-01`), converting numbers to readable Persian words helper for pronunciation preview (`AUD-02`).
   - `POST /api/v1/workspaces/{wsId}/media/jobs` (`Idempotency-Key`) & `DELETE .../media/jobs/{jobId}`:
     - Validates model capability & supported resolution (`1K/2K/4K` gated on model metadata before spend — `MED-07`), checks modality consent (`OPENAI_IMAGE`/`GEMINI_IMAGE`, `OPENAI_AUDIO`/`GEMINI_AUDIO`, `GEMINI_VIDEO`), reserves budget via `fn_budget_reserve` before external AI/TTS/Video calls, and settles/releases accurately!
     - **`TTS_SYNTHESIS` Segment Cache (`AUD-05`):** hashes `(provider_id, model_id, voice_id, normalized_dialogue_text)`; if 1 segment of a 10-segment script is edited, synthesizes & charges **only** the 1 changed segment while reusing the 9 cached segment audio blobs!
     - **`AI_VIDEO_GEN_POLL` (`MED-12`):** tracks remote long-running operation ID, supports cancellation (`DELETE /media/jobs/{jobId}`), downloads completed video through `SafeDialer` (anti-SSRF!), and labels `origin_kind='AI_GENERATIVE_VIDEO'` distinctly from `'LOCAL_RENDERED_CLIP'`.
4. **Full Media Attachment Publishing & Album Policy Execution (`FMT-04`, `AUD-04`, `MED-02, MED-15`):**
   - Upgrade `drafts_m3.py`, `publications.py`, `messengers.py`, and `dispatcher.py` so drafts with `media_asset_ids` record `app.media_asset_refs`, validate destination file-size & format limits before publish (`MED-15`), show `VOICE_NOTE` vs `AUDIO` distinction (`AUD-04`), and execute `SEQUENTIAL_POSTS` vs `CANCEL_FOR_INCOMPATIBLE_DEST` cleanly on non-album destinations like `EITAA` (`FMT-04`)!

---

### 2.4 `AI` & `HYBRID` Automated Routes + `AutoPublishRiskGate` (`src/relayforge/web/routes_m3.py` / `auto_orchestrator.py` — `AUT-02..08`, `AT-15`)
Upgrade `run_pipeline` so `route_mode IN ('AI', 'HYBRID')` executes the complete audited automated chain while **models still have zero publish tools (`AUT-02`)**:
1. Evaluates filter AST, deduplication, daily post cap (`routes.daily_post_cap`), allowed hours window, and `emergency_stop` (`AUT-06`).
2. Executes AI rewrite/generation under the route's pinned style, brand, and freedom mode (with atomic budget reservation).
3. **Explicit AI Failure `fallback_policy` (`AUT-05`):** when AI fails (`QUOTA_ZERO`, `UPSTREAM_5XX`, `TIMEOUT`, `SCHEMA_INVALID`), strictly obey `route_policy_revisions.fallback_policy`:
   - `'HOLD_FOR_REVIEW'` (default): save draft in `'NEEDS_REVIEW'` (zero publish!).
   - `'STOP'`: record failed run without publishing.
   - `'SEND_WITHOUT_MEDIA'`: proceed without failed AI media if text succeeded.
   - `'FALLBACK_MODEL'`: escalate within consented routing profile.
   - `'SEND_AUTHORIZED_ORIGINAL'`: allowed **only** when source `republish_rights_attested == True`!
4. **`AutoPublishRiskGate` (`AUT-04`):** even when `automation_level == 'AUTO_PUBLISH_BY_POLICY'`, automatically divert to `'NEEDS_REVIEW'` draft if:
   - `fact_guard_verdict == 'FACT_MISMATCH_BLOCKED'` or `sensitive_claim_flagged == True`,
   - `freedom_mode == 'UNRESTRICTED'` without explicit `free_mode_auto_risk_ack: True` in route policy,
   - media attachment lacks required license/ownership or violates destination capability!
5. Expose `GET /api/v1/workspaces/{wsId}/reports/automated-runs` returning the full secret-scrubbed decision chain (`source_item -> style -> model -> reservation -> risk_gate -> publication_version -> delivery_tasks / remote_message_id` — `AUT-08`).

---

## 3. Mandatory Non-Vacuous Test Suite (`M0..M5`)

1. **Unit Tests (`tests/unit/test_m5_media_sandbox_persian_render_and_minimizer.py`):**
   - **Data Minimization (`MED-03`, `SEC-10`):** input text containing `09123456789`, `user@example.com`, `@private_id` -> suggested search query strips all PII!
   - **Persian Infographic & `512px WebP` Sticker (`MED-09`, `MED-10`, `AT-12`):** render Persian table with numbers `۱۲۳۴۵` -> verify valid `PNG` header, dimensions, non-blank pixel variance, and ownership attestation enforcement (`IDN-06`); build `512x512` RGBA `.webp` (`< 512 KiB`) native sticker vs regular image!
   - **Sandbox Security Guards (`SEC-07`, `AT-20`):** reject disguised `.exe` uploaded as `.jpg`, reject `> 40 MP` decompression-bomb header, reject `../` path traversal & symlink in `media-worker`, and reject `ffconcat` / `#EXTM3U` inputs!

2. **Integration Tests (`tests/integration/test_m5_media_worker_podcast_and_auto_routes.py`):**
   - **Isolated `AF_UNIX` `media-worker` Clip, Timeout/Cancel & FFmpeg Preflight (`MED-11`, `SEC-07`, `AT-12`):**
     - Simulate missing `ffmpeg` -> fails preflight **before** reserving/charging AI budget (`0` micros spent)!
     - With real `ffmpeg` (or sandbox `ffmpeg`), render `LOCAL_SLIDESHOW_CLIP` `.mp4` and cancel a slow running job via `DELETE /media/jobs/{jobId}` verifying process-group kill (`CANCELLED`) and deterministic staging cleanup!
   - **2-Speaker Podcast Script Edit & 1-of-10 Segment TTS Cache (`AUD-01..05`, `AT-12`):**
     - Create 10-segment 2-speaker podcast script (assert `0` TTS spend before approval!), synthesize audio -> `10` TTS calls; edit **1** segment and re-synthesize -> assert **exactly `1` new TTS call** (9 segments served from cache!) and playable `VOICE_NOTE` (`OGG`) / `AUDIO` (`WAV`/`MP3`) stored in S3 & referenced safely (`MED-13` blocks `DELETE` while attached to draft/publication)!
   - **Licensed Stock Search/Download + Multi-Destination Media/Album Publish (`AT-13`, `FMT-04`):**
     - Search stock media (consent-gated), download via `SafeDialer` (blocking SSRF redirect to `169.254.169.254`!), attach 3-image album to draft, publish to `TELEGRAM` (native album) and `EITAA` (`SEQUENTIAL_POSTS` policy -> 3 ordered posts, zero silent drop)!
   - **`AutoPublishRiskGate`, Daily Post Cap & Emergency Stop (`AT-15`, `AUT-04..08`):**
     - Run active `AI` route with `daily_post_cap = 3`:
       - Post with altered number in `BALANCED` mode -> held in `NEEDS_REVIEW` by `AutoPublishRiskGate` (`0` deliveries)!
       - AI upstream `503` failure with default `HOLD_FOR_REVIEW` -> held in `NEEDS_REVIEW` without publishing raw original!
       - Send 4 valid posts -> first 3 enqueue & deliver, **4th post stops deterministically at `DAILY_CAP_REACHED`**, emergency stop halts further dispatch, and `/reports/automated-runs` shows the complete scrubbed chain!

---

## 4. Verification & PR Checklist

1. `ruff check .`
2. `lint-imports`
3. `python scripts/gates/check_ast_gates.py`
4. `pytest -v tests/unit tests/integration` (all `M0..M5` suites green!)
5. Confirm `0` diff on `RelayForge_Architecture/` and `PROJECT_MASTER_ROADMAP.md`, and push branch `feat/m5-media-studio-podcast-worker-and-risk-gate` + open PR to `main`.
