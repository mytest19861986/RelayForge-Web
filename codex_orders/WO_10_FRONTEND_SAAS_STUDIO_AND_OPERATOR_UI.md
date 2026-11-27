# Work Order `WO-10` — Full Modern Persian RTL SaaS Studio UI + SuperAdmin Operator Console (`/operator`) Matching Approved Mockup

- **Target Repository:** `https://github.com/mytest19861986/RelayForge-Web`
- **Base Branch:** `main` (includes merged `M0..M7` through `PR #9`, commit `0f8a270`)
- **Feature Branch to Create & Push:** `feat/m8-modern-rtl-saas-ui-and-operator-console`
- **Visual & Architectural References:**
  - Approved visual mockup attached in your ChatGPT turn (`RelayForge Web SaaS` 2-section UI mockup: Login + 12 Tenant Studio views + `/operator` SuperAdmin Platform Console, responsive Desktop/Tablet/Mobile).
  - `FRONTEND_DESIGN_BRIEF.md`
  - `RelayForge_Architecture/17_UI_UX_ARCHITECTURE.md`
  - `RelayForge_Architecture/contracts/database_schema.sql` & `openapi.yaml`
  - **Read-Only Blueprint Rule:** Do **NOT** modify any file inside `RelayForge_Architecture/` or `PROJECT_MASTER_ROADMAP.md` (`0` diff required).

---

## 0. Strict Zero-Bloat & Security Non-Negotiables

1. **Zero Raw `<pre>JSON</pre>` & Zero Manual UUID Typing in Normal UI Flows:**
   - Replace the current raw-JSON developer harness (`<pre>` dumps & manual `"شناسه رکورد"` UUID text inputs) with a genuine, production-grade Persian RTL SaaS interface matching the attached approved mockup.
   - All foreign-key pickers (`source_id`, `destination_id`, `secret_id`, `style_profile_id`, `brand_identity_id`, `provider_id`, `media_asset_id`, `draft_id`, `podcast_script_id`, `tenant_id`) MUST auto-populate from API lists with human-readable Persian labels/handles (`<select>`, searchable chips, or cards) — **never** ask users to copy/paste UUIDs!
2. **Browser Secret & Security Hygiene (`SEC-01`, `SEC-02`, `CSP`):**
   - `localStorage` and `sessionStorage` MUST NEVER store session tokens, `csrf_token`, API keys, bot tokens, or `OperatorBearerAuth` tokens (`test_rtl_shell_and_reports` asserts `localStorage`/`sessionStorage` absence on auth/session). Keep `csrf_token` and optional operator bearer token strictly in volatile JS module memory!
   - Keep Django `Content-Security-Policy` strictly local (`default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; img-src 'self' data: blob:; ...`) with zero external CDNs, zero inline `<script>`, and zero `eval()`.
3. **Zero `node_modules` Bloat & Total Size Budget:**
   - Implement cleanly with vanilla modular ES scripts + modern CSS Grid/Flexbox + inline clean SVG icons (inside `src/relayforge/web/static/relayforge_ui/`), keeping total tracked Git size well under `< 15 MB` (`check_supply_chain.py` must pass).
4. **Sandbox Pre-Approval & Existing Test Compatibility:**
   - Do **NOT** break any existing `M0..M7` tests (including `test_rtl_shell_and_reports` and `test_explicit_acceptance_matrix_and_studio_journey` checking `<html lang="fa" dir="rtl">`, CSP headers, and UI asset presence of `LIVE_PUBLISH`, `destination_ids`, `/drafts`, etc.).
   - Running native PostgreSQL 16 + `ffmpeg` inside your sandbox is pre-approved; proceed directly through full CI verification, `git push`, and opening the PR against `main`.

---

## 1. Thin, Safe Backend Additions to Power All Mockup Screens (`migrations/0008_m8_ui_operator_support.sql` + REST Helpers)

To support every interactive table, picker, and the SuperAdmin `/operator` panel cleanly without exposing cross-tenant data or weakening RLS:

1. **Migration `migrations/0008_m8_ui_operator_support.sql` (registered in `db_migrator.py`):**
   - Add scoped RLS read policies strictly for **`relayforge_operator`** (preserving `51/51 FORCE RLS` and `0` non-superuser `BYPASSRLS` — `OC-10`):
     - Allow `relayforge_operator` `SELECT` on `app.tenants` (list platform tenants) and `SELECT` on `app.budget_policies` `WHERE policy_origin = 'OPERATOR_PLAN'` (read tenant plan quotas in Operator console). Also add `GRANT SELECT ON app.tenants, app.workspaces, app.budget_policies TO relayforge_operator` as needed (do NOT grant any extra privileges to `relayforge_web`, `relayforge_worker`, or `relayforge_ledger_owner`).
2. **Operator Console List Endpoint (`src/relayforge/web/api.py`):**
   - Add `GET /api/v1/operator/tenants` (`auth=operator_auth`, querying via `using=OPERATOR`): returns paginated platform tenants (`id`, `slug`, `display_name`, `status`, `created_at`, plus active `OPERATOR_PLAN` `daily_max_cost_micros` / `monthly_max_cost_micros`), supporting optional `q` search filter over `slug`/`display_name`.
   - Extend `GET /api/v1/workspaces/{wsId}/dashboard` (in `ops_m6.py`) with non-blocking operational stats needed by the dashboard mockup: real DB disk usage metrics (`disk_used_bytes`, `disk_total_bytes`, `disk_free_bytes`, `disk_used_percent`, `pitr_retention_days: 14`), `recent_notifications`, and active `budget_policies` + consumed micros summary.
3. **List/Lookup Endpoints for Dropdown Pickers & Tables (Workspace-Scoped, RLS-Protected):**
   - Ensure the UI can populate every list cleanly without UUID pasting:
     - `GET /workspaces/{wsId}/channel-intelligence/analyses` (recent analyses in workspace, cursor-paginated, excluding secrets),
     - `GET /workspaces/{wsId}/media/jobs` (recent media jobs in workspace),
     - `GET /workspaces/{wsId}/podcast-scripts` (saved scripts in workspace),
     - `GET /workspaces/{wsId}/backups` (recent `app.backup_manifests` metadata — NEVER returning raw archive bytes in list view).
4. **Static Asset Serving (`src/relayforge/web/studio_ui.py` & `urls.py`):**
   - Serve `/operator` (alongside `/`, `/app/`, `/app/<slug:view>`) with the RTL Studio shell + strict CSP headers (`default-src 'self'`, `frame-ancestors 'none'`, `object-src 'none'`).
   - Support clean multi-file modular ES modules + stylesheets under `src/relayforge/web/static/relayforge_ui/` safely via explicit allowlist (`index.html`, CSS modules, JS modules, optional SVG logo/icons).

---

## 2. Exact UI & UX Implementation Matching Approved Mockup (`src/relayforge/web/static/relayforge_ui/`)

Match the approved mockup's clean emerald/slate-white **RTL SaaS design system** (`#096c60` / `#087c68` primary emerald, crisp `#ffffff` bordered cards over `#f4f7f8` canvas, subtle emerald active sidebar pill with `border-inline-start`, danger crimson `#b91c1c` for Emergency Stop & Owner Delete, amber `#d97706` for Experimental badges & warnings, Persian typography + tabular Persian numerals, `<bdi dir="ltr">` for `@handles`, URLs, SHAs, and model IDs, and **responsive layout** for Desktop, Tablet `~768px`, and Mobile `~390px` with collapsible mobile drawer):

1. **Top App Shell & Persistent Navigation:**
   - Top bar: Brand mark (`استودیو رله‌فورج | RelayForge Studio`), Workspace selector dropdown (populated via `/workspaces`), live Engine status pill (`موتور: در حال اجرا` green vs `متوقف‌شده` red), Notification bell with unread badge + dropdown drawer (marking read via `/notifications/{id}/read`), prominent red **«توقف اضطراری»** button with confirmation modal, user/operator mode switcher button (`پنل اپراتور` + `خروج`), and toast notification stack (zero blocking `window.alert`/`window.confirm`!).
2. **View 0 — Login (`/`):** Centered clean SaaS card (`ورود به حساب کاربری`), Email, optional Tenant Slug (`org-alpha`), Password with show/hide eye toggle, plus clean tab/link to switch to **Operator Console (`/operator`)** login via runtime memory `Bearer` token.
3. **Platform SuperAdmin / Operator Console (`/operator`):** Matches the green-header Operator Console in the mockup:
   - KPI / `OPERATOR_PLAN` Quotas editor card (`14 روز PITR`, `daily_max_cost_micros`, `monthly_max_cost_micros` editable via `PUT /operator/tenants/{id}/plan-quotas`),
   - **«ایجاد مستأجر جدید»** modal (`POST /operator/tenants` — capturing generated `tenant_id` & `default_workspace_id` cleanly),
   - Searchable Tenants table (`نام مستأجر`, `شناسه Slug`, `وضعیت`, `تاریخ ایجاد`, quota actions).
4. **View 1 — Dashboard (`نمای کلی` / `/app/dashboard`):**
   - 5 KPI cards with SVG icons and real DB counts (`منابع فعال`, `مقصدها`, `مسیرها`, `اعلان‌های خوانده‌نشده`, `کارهای زمان‌بندی / رسانه`),
   - **بودجه مستأجر** progress bar comparing real tenant spend vs read-only `OPERATOR_PLAN` ceiling,
   - **اعلان‌های مهم** list with unread click-to-acknowledge & occurrence badge,
   - **وضعیت دیسک** card showing used/free/total space percentage + **`سیاست نگهداری: ۱۴ روز (PITR)`**.
5. **View 2 — Sources & S3 Export Wizard (`منابع و استخراج آرشیو` / `/app/sources`):**
   - Searchable sources table + clean «افزودن منبع جدید» form,
   - 2-step **Telegram Desktop JSON -> S3 Upload Wizard** with source dropdown (no UUID typing!), file picker, live XHR progress bar, and honest Bot API limitation card.
6. **View 3 — Destinations & Capability Matrix (`مقصدها و ماتریس قابلیت` / `/app/destinations`):**
   - Rich capability table matching mockup: `تلگرام (فعال GA)`, `بله (فعال GA)`, `ایتا (فقط ارسال خروجی)`, `روبیکا (آزمایشی — نیازمند تست زنده)`, `سروش‌پلاس (آزمایشی — نیازمند تست زنده)`, displaying capabilities (`ارسال متن`, `رسانه`, `زمان‌بندی`) and Secret Vault picker dropdown, plus separated **«تست فنی اتصال»** (purple/indigo) and **«ارسال پست آزمایشی واقعی»** (emerald/amber with confirmation modal) buttons.
7. **View 4 — Routes, Rules & Visual `Dry-Run` (`/app/routes`):**
   - Existing routes table with live `is_enabled` toggle switches + 4-mode route builder (`COPY / RULES / AI / HYBRID`) with multi-select Source & Destination chips, keyword/regex filter & schedule inputs,
   - **Visual Step-by-Step `Dry-Run` Stepper:** 4-stage visual pipeline (`۱. انتخاب محتوا ← ۲. اعمال قوانین ← ۳. پیش‌نمایش خروجی ← ۴. گزارش نتیجه`) showing matched/filtered status, compiled previews, and degradation report — zero raw JSON!
8. **View 5 — Content Studio (`استودیو تولید محتوا` / `/app/content`):**
   - Visual 4-card Freedom Mode selector (`STRICT`, `BALANCED متعادل پیشنهادی`, `CREATIVE`, `UNRESTRICTED`),
   - Side-by-side **«متن ورودی (مرجع)»** vs **«خروجی پیشنهادی»** cards,
   - Structured **`Fact Guard`** verification card showing badge counts (`تأییدشده / مشکوک / غیرمجاز`), numeric/price mismatch comparison, and sensitive-claim alert,
   - Collapsible **«تنظیمات پیشرفته»** drawer for Style selector, Routing Mode, Provider/Model pickers.
9. **View 6 — Channel Intelligence (`کانال‌شناس هوشمند` / `/app/intelligence`):**
   - Source dropdown + `Analyze` trigger, 4 sampling metric tiles, visual 80/20 Donut/Ring chart (`CSS conic-gradient` or clean SVG — zero heavy chart libraries!), top topics bar breakdown, extracted identity card + amber notice highlighting default third-party identity stripping (`STRIP_BY_DEFAULT`).
10. **View 7 — Style Library (`کتابخانه الگوها` / `/app/styles`):**
    - Searchable style cards grid with version tags + **Weighted 2-Style Mixer** (synced range sliders summing to 100%) + quick inline **«آزمایش سریع سبک»** preview + JSON export/import buttons.
11. **View 8 — Media & Podcast Studio (`استودیو رسانه و پادکست` / `/app/media`):**
    - 4 clean tabs (`کتابخانه رسانه`, `جستجوی مجوزدار`, `سازنده اینفوگرافیک`, `استیکر و کلیپ`, `اسکریپت پادکست`), media grid gallery + attribution cards, dynamic row-by-row Persian infographic builder, native sticker & local `FFmpeg` clip builder (distinct from AI video), two-speaker dialogue script builder with mandatory approval checkbox before TTS synthesis, and live job status table with Cancel action.
12. **View 9 — AI Providers, Consents & Budgets (`تنظیمات هوش مصنوعی و بودجه` / `/app/ai`):**
    - Clean BYOK provider selector (**`OpenAI`**, **`Gemini`**, **`Compatible OpenAI`**) + Vault Secrets manager (masked hints + revoke action),
    - Categorized Consent toggle switches (`OPENAI_TEXT`, `GEMINI_TEXT`, `STOCK_MEDIA_SEARCH`, `OPENAI_IMAGE`, etc.),
    - 5-modality **Model Probe** panel (`TEXT`, `VISION`, `IMAGE`, `TTS`, `VIDEO`) showing measured latency (`ms`) per modality,
    - Read-only **`OPERATOR_PLAN` Ceiling** card alongside Tenant Sub-Budget editor.
13. **View 10 — Drafts, Multi-Destination Preview & Publish (`پیش‌نویس، پیش‌نمایش مقصد و انتشار` / `/app/drafts`):**
    - Drafts list sidebar + rich draft editor (title, body, brand selector, media attachment picker),
    - Per-messenger simulated preview cards (Telegram, Bale, Eitaa, Rubika, Soroush+) showing exact compiled output, capability degradation warnings (formatting drop / album split), and honest disclaimer banner,
    - Three clearly separated action buttons: **«ذخیره پیش‌نویس»**, **«ارسال آزمایشی واقعی»**, and **«انتشار نهایی»** (triggering explicit confirmation modal with revision lock).
14. **View 11 — Delivery Queue & `UNCERTAIN` Reconciliation Workbench (`/app/delivery`):**
    - Status filter pills (`ALL`, `UNCERTAIN`, `FAILED`, `SENT`, `SCHEDULED`, `READY`) + deliveries table,
    - Dedicated **`UNCERTAIN` Reconciliation Workbench**: selecting any `UNCERTAIN` row populates the reconciliation panel with 3 clear radio choices (`CONFIRM_SENT`, `CONFIRM_NOT_SENT_REQUEUE`, `DISCARD_DO_NOT_SEND`) and enforces the mandatory checkbox **«ریسک ارسال تکراری را می‌پذیرم»** before enabling requeue!
15. **View 12 — Operations, Audit Trail & Encrypted Backups (`عملیات، ممیزی و پشتیبان مستأجر` / `/app/operations`):**
    - Immutable Audit Log table + AI Usage summary + `CSV`/`JSON` download buttons,
    - Maintenance Cleanup & Safe Operational Reset cards,
    - Encrypted `.rfbak` Backup & Safe-Mode Restore wizard (`PREVIEW` impact table showing row/media/secret counts -> `APPLY` enforcing Safe Mode),
    - Red **«منطقه خطرناک: حذف حساب مالک»** panel with explicit confirmation slug and **14-day physical PITR retention disclosure**.

---

## 3. Mandatory Verification & PR Gates

1. Update/add integration & UI regression tests (`tests/integration/test_m8_ui_and_operator_console.py`) verifying:
   - `/`, `/app/dashboard`..`/app/operations`, and `/operator` all return HTTP `200`, `lang="fa" dir="rtl"`, strict `CSP` headers, and zero references to `localStorage`/`sessionStorage` for secrets/tokens.
   - `GET /api/v1/operator/tenants` enforces `OperatorBearerAuth` (`401/403` without token), preserves `51/51 FORCE RLS` & `0` non-superuser `BYPASSRLS` (`OC-10`), and works cleanly alongside tenant creation & plan quota updates.
   - Every static JS/CSS module loads with `200` and zero syntax errors (`node --check` or Python syntax/import checks in CI).
2. Run the entire verification pipeline (`ruff check .`, `lint-imports`, `check_ast_gates.py`, `check_supply_chain.py`, `verify_ubuntu2404_host.py --require-ubuntu-2404`, `docker_production_smoke.sh`, and full `pytest -v tests/unit tests/integration -s` covering all `M0..M8` tests) — all 100% green on `ubuntu-24.04` GitHub Actions.
3. Verify `git diff --name-only origin/main...HEAD -- RelayForge_Architecture PROJECT_MASTER_ROADMAP.md` is empty (`0` modified files), push `feat/m8-modern-rtl-saas-ui-and-operator-console`, and open a Pull Request against `main`.
