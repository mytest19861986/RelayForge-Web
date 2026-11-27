# Work Order `WO-11` — Visual & UX Final Polish on RTL Studio (`PR #10` Visual Browser Audit Fixes)

- **Target Repository:** `https://github.com/mytest19861986/RelayForge-Web`
- **Base Branch:** `main` (includes merged `PR #10`)
- **Feature Branch to Create & Push:** `feat/m8-ui-visual-polish`
- **Read-Only Blueprint Rule:** Do **NOT** modify `RelayForge_Architecture/` or `PROJECT_MASTER_ROADMAP.md` (`0` diff).

---

## Context (Observed in Real Chromium Desktop `1400x900` + Mobile `390x844` Browser Audit)

`PR #10` works cleanly with zero JS/CSP errors and zero raw JSON/UUID inputs, and the Dashboard matches the mockup structure well. Fix these 5 exact visual/UX defects observed in Chromium so the UI is 100% production-grade before final user handoff:

1. **Semantic Status Pill Colors (`src/relayforge/web/static/relayforge_ui/studio.js` + `operator.js`):**
   - Update `formatStatus(row)` (and Operator status pill rendering) so status badges use distinct semantic colors already defined in `studio.css`:
     - `UNCERTAIN`, `NEEDS_REVIEW`, `RETRY_WAIT`, `PENDING`, `RUNNING`, `DISABLED` -> `pill warning` (amber `#fff0db; color:#8c4207`)
     - `FAILED`, `CANCELLED`, `STOPPED`, `PENDING_DELETION` -> `pill stopped` (crimson `#ffeded; color:#a52020`)
     - `ACTIVE`, `SENT`, `SUCCEEDED`, `COMPLETED`, `APPROVED`, `READY`, `TRUE` -> `pill` (emerald `#e4f5ef; color:#075f4f`)
   - Make `عیار/نشان` badges in notification cards clearly separated from the `خواندم` button by placing them inside a flex `.toolbar` row with `justify-content: space-between` rather than inline flow clutter.
2. **Human-Readable Storage Size (`bytes()` -> MiB/GiB in `studio.js`):**
   - When disk size `>= 1024 ** 3`, format cleanly in **`گیگابایت`** (e.g. `۳۲۵ گیگابایت از ۱٬۰۰۰ گیگابایت`) instead of huge million-megabyte strings; otherwise show `مگابایت` (or `کیلوبایت` under 1 MiB).
3. **Explicit RTL Card Alignment, Typography ZWNJ & Auth Form Width (`src/relayforge/web/static/relayforge_ui/index.html`, `studio.css`, `components.js`, `views.js`):**
   - Set explicit `text-align: start` on `.card, #actions > form, .panel, h2, h3, p` and keep `.metric { text-align: center; }` strictly for the top 5 KPI stat tiles so lower Dashboard cards (`بودجه مستأجر`, `اعلان‌های مهم`, `وضعیت دیسک`) align crisp and flush-right (`start` in RTL) instead of shifting inward.
   - Make primary submit / password-toggle / operator-link layout inside `.auth` clean and balanced (primary submit button `width: 100%`, operator link cleanly centered at footer).
   - Add crisp spacing above `<fieldset>` legends (`padding: 10px 12px 12px; margin-block: 4px;`) so legend text (`آزادی بازنویسی`) never overlaps card borders.
   - Fix minor missing ZWNJ / spacing in visible Persian labels (`اعلان‌ها`, `افزوده‌شده`, `رله‌فورج`, `پیش‌نویس`, `پنل اپراتور`).
4. **Prevent Awkward Word Breaking on Slugs / Handles on Mobile `390px` (`studio.css`):**
   - Add `white-space: nowrap` to `td bdi, .pill` inside `.table-wrap` so Latin tenant slugs (`org-aftab`) and status pills stay on one clean line inside the horizontally scrollable `.table-wrap` without splitting mid-word (`org- / afta / b`).
5. **Preserve Security & Existing Gates (`SEC-01/02`, `CSP`, `AT-20/21`):**
   - Zero `localStorage`/`sessionStorage`, zero `innerHTML`, zero manual UUIDs, `node --input-type=module --check` passing, and all `151+` CI tests passing on `ubuntu-24.04`. Push `feat/m8-ui-visual-polish` and open PR against `main`.
