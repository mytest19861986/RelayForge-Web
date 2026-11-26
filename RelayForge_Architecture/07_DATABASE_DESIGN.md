# 07 — طراحی پایگاه داده PostgreSQL 16، توپولوژی نقش‌ها و `FORCE RLS`

**قرارداد ماشینی متناظر:** `contracts/database_schema.sql` (بارگذاری و راستی‌آزمایی‌شده روی کانتینر زنده `PostgreSQL 16.15`: `51/51` جدول با `FORCE RLS` و `0` نقش غیر-superuser دارای `BYPASSRLS`).

---

## ۱. توپولوژی ۶ نقش دیتابیس (اصل کمترین دسترسی و صفر `BYPASSRLS`)

| نام نقش در PostgreSQL | نوع ورود | دسترسی و مسئولیت دقیق | وضعیت `BYPASSRLS` |
|---|---|---|---|
| `relayforge_migrator` | `LOGIN` (فقط در `migrate-job`) | مالک اسکیمای `app` و جداول؛ اجرای مهاجرت و Backfill صرفاً درون تراکنش دارای `SET LOCAL app.migration_mode = 'on'`. | `NOBYPASSRLS` |
| `relayforge_operator` | `LOGIN` (فقط پنل اپراتور پلتفرم) | تعریف مستأجران جدید و تنظیم سقف‌های تغییرناپذیر پلن (`OPERATOR_PLAN` در `budget_policies`). | `NOBYPASSRLS` |
| `relayforge_web` | `LOGIN` (سرویس `web-api`) | عملیات CRUD وب مقید به `app.tenant_id`؛ فاقد `DELETE` روی `delivery_tasks` و `audit_events`؛ مسدود در برابر تغییر `OPERATOR_PLAN` (`RF007`) و نوشتن مستقیم رزرو بودجه. | `NOBYPASSRLS` |
| `relayforge_worker` | `LOGIN` (سرویس `worker-engine`) | پردازش پس‌زمینه مقید به `app.tenant_id` و `app.lease_epoch`؛ فقط‌خواندنی روی `budget_policies`؛ فاقد نوشتن مستقیم رزرو بودجه. | `NOBYPASSRLS` |
| `relayforge_dispatcher_owner` | `NOLOGIN` | مالک توابع `SECURITY DEFINER` برداشتن عادلانه (`fn_worker_claim_next_jobs`) و جمع‌آوری اجاره منقضی (`fn_reap_expired_delivery_leases`) با دسترسی ستونی کمینه. | `NOBYPASSRLS` |
| `relayforge_ledger_owner` | `NOLOGIN` | مالک توابع `SECURITY DEFINER` رزرو و تسویه بودجه (`fn_budget_reserve`, `fn_budget_settle`) که خود نیز تحت `FORCE RLS` مستأجر عمل می‌کنند. | `NOBYPASSRLS` |

*(توجه ممیزی `OC-10`: ابرکاربر ذاتی `postgres` در کاتالوگ سیستمی دارای `rolbypassrls = true` است، اما هیچ سرویس برنامه‌ای با آن متصل نمی‌شود و حتی ابرکاربر نیز در جداول حساس بدون تنظیم `SET LOCAL app.break_glass_reason = 'INC-...'` توسط تریگرها با `RF020` متوقف می‌گردد.)*

---

## ۲. فهرست کامل ۵۱ جدول اسکیمای `app` به تفکیک حوزه

1. **هسته مستأجر، هویت و زیرساخت (`۸` جدول):** `tenants`, `users`, `tenant_memberships`, `web_sessions`, `tenant_encryption_keys`, `tenant_dispatch_fairness`, `schema_migrations`, `rate_limit_counters` (`UNLOGGED`).
2. **فضای کاری، اسرار، رضایت و برند (`۵` جدول):** `workspaces`, `workspace_settings`, `secrets_vault`, `external_consents`, `brand_identities`.
3. **منابع، استخراج S3، مقصدها و محتوای استاندارد (`۶` جدول):** `sources`, `source_cursors`, `export_import_jobs`, `destinations`, `content_items`, `content_item_revisions`.
4. **کانال‌شناس و کتابخانه سبک (`۴` جدول):** `channel_analyses`, `channel_analysis_samples`, `style_profiles`, `style_revisions`.
5. **مسیرها و اثر انگشت ضدتکرار (`۳` جدول):** `routes`, `route_policy_revisions`, `content_fingerprints`.
6. **درگاه AI و دفتر کل بودجه دوسطحی (`۷` جدول):** `ai_providers`, `ai_model_cache`, `ai_routing_profiles`, `budget_policies`, `budget_reservations`, `budget_reservation_allocations`, `ai_executions`.
7. **تولید محتوا، استودیو رسانه و پادکست (`۶` جدول):** `generation_sessions`, `generation_candidates`, `media_assets`, `media_asset_refs`, `podcast_scripts`, `media_jobs`.
8. **پیش‌نویس، انتشار منجمد و صف تحویل (`۶` جدول):** `drafts`, `draft_revisions`, `publication_versions`, `delivery_tasks`, `delivery_attempts`, `reconciliation_records`.
9. **یادگیری بازخورد، Outbox، ممیزی و پشتیبان (`۶` جدول):** `feedback_events`, `learned_preference_rules`, `domain_outbox`, `audit_events`, `system_notifications`, `backup_manifests`.

---

## ۳. الگوهای کلیدی یکپارچگی و کارایی در PostgreSQL 16

- **کلیدهای خارجی مرکب مقید به مستأجر (`Composite Tenant-Anchored FKs`):** تمامی جداول فرزند دارای قید `UNIQUE (tenant_id, id)` در والد و `FOREIGN KEY (tenant_id, parent_id) REFERENCES app.parent(tenant_id, id)` هستند تا حتی در سطح ارجاعات کلید خارجی هیچ پیوند متقاطعی بین دو مستأجر شکل نگیرد.
- **جست‌وجوی تمام‌متن فارسی بدون افزونه خارجی:** ستون محاسباتی ذخیره‌شده `fts_vector` (`to_tsvector('simple', coalesce(plain_text, ''))`) با ایندکس `GIN` جایگزین `SQLite FTS5` شده و تمام متون فارسی، اعداد و نیم‌فاصله‌ها را درون تراکنش اصلی همگام نگه می‌دارد.
- **ایندکس برداشت عادلانه با پیمایش کران‌دار (`idx_delivery_fair_claim`):** روی `(tenant_id, runnable_at ASC) WHERE status IN ('READY', 'SCHEDULED', 'RETRY_WAIT')` تعریف شده است؛ مقایسه با متغیر ثابت `v_now` در `fn_worker_claim_next_jobs` هر دو ستون را در `Index Cond` قرار می‌دهد (`shared hit = 4` روی ۹۵٬۰۰۰ ردیف در `SPK-02`).
- **ممنوعیت بازآفرینی نقش‌ها در مهاجرت‌ها:** مهاجرت‌های محصول هرگز دستور `DROP ROLE` اجرا نمی‌کنند تا کش `OID` نقش‌ها در اتصالات باز `PgBouncer` نامعتبر نشود.
