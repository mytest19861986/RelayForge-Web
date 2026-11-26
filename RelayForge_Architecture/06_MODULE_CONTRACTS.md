# 06 — قراردادهای دقیق ۱۰ ماژول معماری و مالکیت انحصاری ۵۱ جدول (Module Contracts)

**وضعیت همگامی:** منطبق ۱۰۰٪ و ۱ به ۱ با ۵۱ جدول تعریف‌شده در `contracts/database_schema.sql` و ۴۹ رویداد تعریف‌شده در `contracts/internal_events.schema.json`.

---

## اصول حاکم بر مرز ماژول‌ها

1. **مالکیت انحصاری ۱ به ۱ جداول (`51 / 51 Tables`):** هر یک از ۵۱ جدول اسکیمای `app` دقیقاً متعلق به یک ماژول است (`6 + 6 + 4 + 2 + 7 + 7 + 2 + 4 + 10 + 3 = 51`). هیچ ماژولی مجاز به نوشتن مستقیم در جدول ماژول دیگر نیست و ارتباط فقط از طریق پورت‌های دامنه یا رویدادهای تراکنشی انجام می‌شود.
2. **صفر نشت اسرار خارج از `M02`:** سایر ماژول‌ها صرفاً شناسه `secret_id: UUID` را نگهداری می‌کنند؛ رمزگشایی در حافظه با کلید `Tenant DEK` فقط در لحظه فراخوانی شبکه درون درگاه خروجی انجام می‌شود.
3. **همگامی کامل رویدادها:** تمامی رویدادهای تولیدی در `app.domain_outbox` حاوی `tenant_id` و `workspace_id` بوده و دقیقاً عضو فهرست مجاز `contracts/internal_events.schema.json` هستند.

---

## M01 — Tenant, Workspace & Runtime Kernel
- **مسئولیت:** مدیریت چرخه حیات مستأجران (`tenants`)، ایجاد و جداسازی فضاهای کاری (`workspaces`, `workspace_settings`)، ثبت نسخه‌های مهاجرت اسکیما (`schema_migrations`)، ردیابی نوبت عدالت توزیع بین مستأجران (`tenant_dispatch_fairness`) و شمارنده‌های محدودیت نرخ بدون Redis (`rate_limit_counters`).
- **مالکیت انحصاری جداول (۶ جدول):** `tenants`, `workspaces`, `workspace_settings`, `schema_migrations`, `tenant_dispatch_fairness`, `rate_limit_counters`.
- **وابستگی مجاز:** لایه ۰ (مستقل).
- **رویدادهای تولیدی:** `tenant.provisioned`, `workspace.initialized`, `workspace.switched`, `workspace.migration.completed`, `ops.disk_space.low`.
- **رویدادهای مصرفی:** `backup.restored.safe_mode`.
- **نیازمندی‌ها:** `APP-01`, `APP-03`..`APP-07`, `QUE-12`, `NFR-09`.

---

## M02 — IAM, Envelope Vault & Governance Core
- **مسئولیت:** احراز هویت وب با `Argon2id` (`users`)، مدیریت نقش‌های RBAC مستأجر (`tenant_memberships`)، نشست‌های وب قابل ابطال و CSRF (`web_sessions`)، مدیریت چرخه حیات و امحای کلیدهای پاکتی مستأجر (`tenant_encryption_keys` برای `Crypto-Shredding`)، خزانه اسرار (`secrets_vault`)، دروازه رضایت ارسال خارجی (`external_consents`) و کلاینت HTTP ضد-SSRF و سانسورگر توکن.
- **مالکیت انحصاری جداول (۶ جدول):** `users`, `tenant_memberships`, `web_sessions`, `tenant_encryption_keys`, `secrets_vault`, `external_consents`.
- **وابستگی مجاز:** `M01`.
- **رویدادهای تولیدی:** `tenant.crypto_shredded`, `security.secret.stored`, `security.secret.revoked`, `security.consent.updated`, `security.unauthorized_blocked`.
- **رویدادهای مصرفی:** `tenant.provisioned`, `workspace.initialized`.
- **نیازمندی‌ها:** `APP-02`, `DST-08`, `AIP-11`, `SEC-01`..`SEC-06`, `SEC-09`, `SEC-10`.

---

## M03 — Messenger Gateway & S3 Export Ingestion
- **مسئولیت:** مدیریت منابع مجاز (`sources`, `source_cursors` — با مسدودیت سخت نشست کاربری `MTProto` در ابر)، صدور Presigned POST و استخراج جریانی فایل‌های آرشیو تلگرام از S3 (`export_import_jobs`)، و مدیریت مقصدها و آداپتورهای ارسال ۵ پیام‌رسان (`destinations` با رده‌بندی `GA`, `OUTBOUND_LIMITED`, `EXPERIMENTAL`).
- **مالکیت انحصاری جداول (۴ جدول):** `sources`, `source_cursors`, `export_import_jobs`, `destinations`.
- **وابستگی مجاز:** `M01`, `M02`.
- **رویدادهای تولیدی:** `source.status.changed`, `export.presigned.issued`, `export.stream_parsed.completed`, `destination.status.changed`, `content.batch.fetched`, `connector.rate_limit.cooldown`.
- **رویدادهای مصرفی:** `security.secret.revoked`.
- **نیازمندی‌ها:** `SRC-01`..`SRC-04`, `SRC-07`, `SRC-13`, `DST-01`..`DST-08`, `NFR-05`, `NFR-06`.

---

## M04 — Canonical Content & Full-Text Search
- **مسئولیت:** نرمال‌سازی محتوای ورودی به `CanonicalRichText` (`UTF-16 Span AST`)، حذف تکرار همزمان زنده و آرشیو، جست‌وجوی سریع فارسی با ستون محاسباتی `tsvector` و ایندکس `GIN`، قالب‌ساز امن placeholder تک‌`{body}` و کامپایلرهای قالب‌بندی مخصوص هر پیام‌رسان.
- **مالکیت انحصاری جداول (۲ جدول):** `content_items`, `content_item_revisions`.
- **وابستگی مجاز:** `M01`, `M02`, `M03`.
- **رویدادهای تولیدی:** `content.item.ingested`, `content.source.edited`, `content.source.deleted`, `channel.samples.collected`.
- **رویدادهای مصرفی:** `content.batch.fetched`, `export.stream_parsed.completed`.
- **نیازمندی‌ها:** `SRC-05`, `SRC-06`, `SRC-08`..`SRC-12`, `FMT-01`..`FMT-08`.

---

## M05 — Channel, Style, Brand & Preference Intelligence
- **مسئولیت:** کانال‌شناس هوشمند با نمونه‌گیری ۸۰/۲۰ استخراج و ارزیابی، جداسازی هویت کانال ثالث از سبک، کتابخانه الگوهای نسخه‌دار و ترکیب وزنی، مدیریت هویت برند مالک به تفکیک مقصد، و یادگیری قواعد ترجیحی از اصلاحات کاربر با حد نصاب شواهد.
- **مالکیت انحصاری جداول (۷ جدول):** `brand_identities`, `channel_analyses`, `channel_analysis_samples`, `style_profiles`, `style_revisions`, `feedback_events`, `learned_preference_rules`.
- **وابستگی مجاز:** `M01`..`M04`, `M06`.
- **رویدادهای تولیدی:** `channel.analysis.completed`, `style.revision.created`, `brand.identity.updated`, `learning.rule.proposed`.
- **رویدادهای مصرفی:** `channel.samples.collected`, `draft.approved.human`.
- **نیازمندی‌ها:** `CH-01`..`CH-11`, `IDN-01`..`IDN-06`, `STY-01`..`STY-09`, `LRN-01`..`LRN-06`.

---

## M06 — AI Gateway, Model Routing & Dual-Tier Budget Ledger
- **مسئولیت:** پورت‌های تفکیک‌شده هوش مصنوعی (`TEXT`, `VISION`, `IMAGE`, `TTS`, `VIDEO`)، کش کاتالوگ مدل مقید به اثر انگشت کلید، مسیریابی هوشمند مدل‌ها، و دفتر کل بودجه سرورمحور تحت مالکیت `relayforge_ledger_owner` با تفکیک سخت سقف پلن اپراتور (`OPERATOR_PLAN` در برابر `TENANT_BUDGET`).
- **مالکیت انحصاری جداول (۷ جدول):** `ai_providers`, `ai_model_cache`, `ai_routing_profiles`, `budget_policies`, `budget_reservations`, `budget_reservation_allocations`, `ai_executions`.
- **وابستگی مجاز:** `M01`, `M02`.
- **رویدادهای تولیدی:** `ai.catalog.refreshed`, `budget.plan_quota.updated`, `budget.threshold.reached`, `ai.quota.exhausted`, `ai.execution.finished`.
- **رویدادهای مصرفی:** `security.secret.stored`, `security.secret.revoked`, `security.consent.updated`.
- **نیازمندی‌ها:** `AIP-01`..`AIP-13`, `SEC-11`, `SEC-15`, `NFR-12`.

---

## M07 — Content Generation, 4-Mode Fact Guard & Quality Engine
- **مسئولیت:** تولید و بازآفرینی متن و پیش‌نویس سناریوی پادکست، اجرای چهار حالت آزادی (`STRICT`, `BALANCED`, `CREATIVE`, `UNRESTRICTED` با خاموشی کامل `Fact Guard` در حالت آزاد)، نرمال‌ساز اعداد و مبالغ فارسی، و ارزیاب کیفیت سه‌لایه.
- **مالکیت انحصاری جداول (۲ جدول):** `generation_sessions`, `generation_candidates`.
- **وابستگی مجاز:** `M01`, `M02`, `M04`, `M05`, `M06`.
- **رویدادهای تولیدی:** `generation.candidate.created`, `generation.fact_discrepancy.detected`.
- **رویدادهای مصرفی:** ندارد.
- **نیازمندی‌ها:** `GEN-01`..`GEN-12`, `AUD-01`, `NFR-11`.

---

## M08 — Media Studio, Isolated Media-Worker & Podcast
- **مسئولیت:** جست‌وجو و دانلود امن رسانه دارای مجوز، تولید و ویرایش تصویر، رندر قطعی اینفوگرافیک فارسی، استیکر بومی، ساخت کلیپ محلی و سنتز پادکست تک/دوگوینده از طریق سوکت یونیکس به کانتینر بدون شبکه `media-worker`، پیگیری ویدئوی مولد و کتابخانه رسانه S3 دارای شمارش ارجاع.
- **مالکیت انحصاری جداول (۴ جدول):** `media_assets`, `media_asset_refs`, `podcast_scripts`, `media_jobs`.
- **وابستگی مجاز:** `M01`, `M02`, `M04`..`M07`.
- **رویدادهای تولیدی:** `media.asset.stored`, `media.job.progress`, `media.job.completed`, `media.job.failed`, `media.orphan.cleaned`.
- **رویدادهای مصرفی:** ندارد.
- **نیازمندی‌ها:** `MED-01`..`MED-15`, `AUD-01`..`AUD-05`, `BAK-08`, `SEC-07`.

---

## M09 — Workflow, Drafts, Publications & Reliable Delivery Queue
- **مسئولیت:** موتور مسیرهای ۴ حالته، تشخیص تکرار دقیق و معنایی (`content_fingerprints`)، مدیریت پیش‌نویس‌ها، انجماد تغییرناپذیر نسخه انتشار (`publication_versions`)، صف تحویل عادلانه Water-Filling (`fn_worker_claim_next_jobs`)، تمدید اجاره (`Heartbeat`)، Reaper، حصارکشی `lease_epoch`، چرخه آشتی ممیزی‌شده `UNCERTAIN` (`reconciliation_records`) و جعبه خروجی رویدادها (`domain_outbox`).
- **مالکیت انحصاری جداول (۱۰ جدول):** `routes`, `route_policy_revisions`, `content_fingerprints`, `drafts`, `draft_revisions`, `publication_versions`, `delivery_tasks`, `delivery_attempts`, `reconciliation_records`, `domain_outbox`.
- **وابستگی مجاز:** `M01`..`M08`.
- **رویدادهای تولیدی:** `route.dry_run.completed`, `draft.saved`, `draft.approved.human`, `publication.version.frozen`, `delivery.sent`, `delivery.failed`, `delivery.marked_uncertain`, `delivery.reconciled`, `ops.emergency_stop.activated`.
- **رویدادهای مصرفی:** `content.item.ingested`, `content.source.edited`, `budget.threshold.reached`.
- **نیازمندی‌ها:** `RULE-01`..`RULE-09`, `QUE-01`..`QUE-12`, `PUB-01`..`PUB-08`, `AUT-01`..`AUT-08`, `NFR-01`, `NFR-02`, `NFR-04`.

---

## M10 — Operations, Immutable Audit & Continuity
- **مسئولیت:** شمارنده‌های دقیق داشبورد، گزارش‌گیری امن و فرمول‌زدایی‌شده `CSV/JSON`، دفتر کل ممیزی فقط‌افزودنی (`audit_events` با تریگر منع تغییر/حذف)، اعلان‌های ادغام‌شونده (`system_notifications` با ایندکس یکتای شرطی `WHERE is_read = false`) و مدیریت پشتیبان‌های منطقی مستأجر و نشانگرهای `PITR` (`backup_manifests`).
- **مالکیت انحصاری جداول (۳ جدول):** `audit_events`, `system_notifications`, `backup_manifests`.
- **وابستگی مجاز:** `M01`..`M09` (خواندنی و هماهنگی عملیات).
- **رویدادهای تولیدی:** `backup.created`, `backup.restored.safe_mode`, `ops.alert.raised`, `ops.reset.completed`.
- **رویدادهای مصرفی:** مشترک رویدادهای ممیزی از `M01`..`M09`.
- **نیازمندی‌ها:** `OPS-01`..`OPS-07`, `BAK-01`..`BAK-08`, `SEC-12`, `NFR-07`, `NFR-08`.
