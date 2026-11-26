# 00 — فهرست جامع و نقشه ناوبری معماری (RelayForge Web SaaS Blueprint v2.0)

**ویرایش معماری:** `2.0.0 — Multi-Tenant Web SaaS Edition (Ubuntu 24.04 LTS)`  
**وضعیت فاز صفر (`G0 Technical Spike`):** **`G0: PASS WITH CONDITIONS`** (هسته دیتابیس، `FORCE RLS`، صف عادلانه، حصارکشی `lease_epoch` و دفتر کل بودجه دوسطحی `EXECUTED`؛ شروط باز `OC-01` تا `OC-10` به تفکیک جزء ثبت شده‌اند)  
**مرجع نیازمندی‌ها:** `RelayForge_COMPLETE_PRD_SRS_FA.md` (پوشش ۱۰۰٪ هر ۲۲۰ شناسه رسمی + جدول جایگزینی مصوبات تغییر دامنه از Desktop به Web SaaS)

---

## ۱. تغییر دامنه مصوب مالک محصول (از Desktop به Web SaaS)

بر اساس تصمیم قطعی مالک محصول پس از تحویل نسخه اول، توزیع دسکتاپ ویندوز (`PySide6 / Loopback / SQLite / DPAPI`) به‌طور کامل کنار گذاشته شد و محصول به‌صورت **نرم‌افزار تحت وب چندمستأجری (`Multi-Tenant Web SaaS`) مستقر روی سرور لینوکسی `Ubuntu 24.04 LTS`** (با شروع به‌صورت پایلوت بسته دعوت‌نامه‌ای حدود ۵۰ مستأجر و کدنویسی Greenfield) بازطراحی گردید.

| لایه معماری | پشته نهایی نسخه وب (`v2.0`) | وضعیت شواهد در `G0 Spike` |
|---|---|---|
| **سیستم‌عامل و لبه شبکه** | `Ubuntu 24.04 LTS` + `Docker Compose` + `Caddy / Nginx (TLS 1.2 + TLS 1.3)` | `PENDING HOST` (`OC-08`: تست کانتینرها روی Docker Linux انجام شد؛ تست میزبان بومی در استقرار پایلوت) |
| **چارچوب وب و API** | `Python 3.12` + `Django 5.2 LTS` + `Django Ninja (Pydantic v2)` + ویوهای همگام بدون حالت (`Stateless Polling` به جای SSE در MVP) | `EXECUTED` (`SPK-01`, `SPK-10`) |
| **هسته دامنه و کارگرها** | بسته‌های کاملاً مستقل از فریم‌ورک `core/` و `workers/` محافظت‌شده با `import-linter` و گیت AST | `EXECUTED` (`SPK-06`) |
| **پایگاه داده و استخر اتصال** | `PostgreSQL 16` (`51` جدول در اسکیمای `app` با ۱۰۰٪ `FORCE RLS` و ۶ نقش `NOBYPASSRLS`) + `PgBouncer 1.25+ (transaction mode)` بدون نیاز به Redis در MVP | `EXECUTED` (`Step 0 v1.3.2`, `SPK-01..03`, `SPK-07`) |
| **ذخیره‌سازی اشیاء (S3)** | **`SeaweedFS (Apache-2.0)`** به‌عنوان پیش‌فرض پیشنهادی (یا `Garage v2.x` بدون تغییر تحت `AGPL-3.0`)؛ جایگزین `MinIO` آرشیوشده (`dl.min.io -> 410 Gone`) | `PARTIAL` (`SPK-08`: پیشوند مستأجر و پارس جریانی `240 MiB` اجرا شد؛ تست ورودی خصمانه و Lifecycle در `OC-04/09`) |
| **پردازش رسانه ایزوله** | کانتینر ثابت غیرریشه `media-worker` (`uid=10001`, `network_mode: none`, `read_only: true`, `cap_drop: ALL`, `pids_limit: 64`, سوکت `AF_UNIX`, لیست سفید Demuxer در `FFmpeg`) | `EXECUTED (Docker VM)` / `PENDING HOST` (`SPK-05`, `OC-08`) |
| **پشتیبان و بازیابی فاجعه** | کلاستر: `PITR` با رمزنگاری احرازشده بیرونی (`pgBackRest AES-256 + HMAC` یا `age ChaCha20-Poly1305`)؛ مستأجر: خروجی منطقی رمزگذاری‌شده + `Crypto-Shredding` کلیدهای `secrets_vault` | `PARTIAL` (`SPK-09`: بازیابی زمانی به `T1` اجرا شد؛ رمزنگاری احرازشده WAL، ثبت `RTO` و تست `Crypto-Shredding` در `OC-03`) |

---

## ۲. نقشه کامل فایل‌های بسته معماری (`65` فایل)

### الف) اسناد اصلی معماری (`00` تا `26` + مستندات `G0 Spike`)
- `00_ARCHITECTURE_INDEX.md` — نقشه ناوبری و خلاصه وضعیت راستی‌آزمایی
- `01_EXECUTIVE_SUMMARY.md` — خلاصه مدیریتی، ارزش پیشنهادی و ماتریس وضعیت اجزا
- `02_REQUIREMENTS_ANALYSIS.md` — تحلیل نیازمندی‌ها و **جدول بندبه‌بند جایگزینی Desktop به Web SaaS**
- `03_DOMAIN_MODEL.md` — مدل دامنه، زمینه‌های محدود (`Bounded Contexts`) و موجودیت‌های چندمستأجری
- `04_ARCHITECTURE_OPTIONS.md` — مقایسه گزینه‌های معماری وب و دلایل انتخاب پشته فعلی
- `05_MASTER_ARCHITECTURE.md` — معماری مرجع منطقی/فیزیکی، گراف وابستگی ۱۰ ماژول و قوانین PgBouncer
- `06_MODULE_CONTRACTS.md` — قراردادهای دقیق ماژول‌های `M01` تا `M10` با مالکیت انحصاری ۱ به ۱ روی ۵۱ جدول
- `07_DATABASE_DESIGN.md` — طراحی پایگاه داده PostgreSQL 16، توپولوژی ۶ نقش، `FORCE RLS` و توابع هسته
- `08_API_ARCHITECTURE.md` — معماری REST API در Django Ninja، میان‌افزار اتمیک مستأجر، Polling و Idempotency
- `09_AI_ARCHITECTURE.md` — درگاه هوش مصنوعی، مسیریابی مدل، دفتر کل بودجه دوسطحی (`OPERATOR_PLAN` / `TENANT_BUDGET`) و `Fact Guard` چهارحالته
- `10_CHANNEL_INTELLIGENCE.md` — معماری کانال‌شناس هوشمند، نمونه‌گیری طبقه‌بندی‌شده و جداسازی هویت
- `11_STYLE_AND_LEARNING.md` — کتابخانه الگوهای نسخه‌دار، ترکیب وزنی سبک‌ها و یادگیری ترجیحات کاربر
- `12_MEDIA_ARCHITECTURE.md` — استودیو رسانه، کانتینر ایزوله `media-worker`، اینفوگرافیک فارسی، ویدئو و پادکست
- `13_PUBLICATION_ENGINE.md` — موتور قوانین، نسخه منجمد انتشار، صف عادلانه Water-Filling و آشتی `UNCERTAIN`
- `14_MESSENGER_INTEGRATIONS.md` — یکپارچه‌سازی ۵ پیام‌رسان (تلگرام و بله در MVP؛ ایتا خروجی؛ روبیکا و سروش‌پلاس Experimental؛ حذف MTProto مشتری)
- `15_SECURITY_ARCHITECTURE.md` — معماری امنیت وب، خزانه پاکتی `Tenant DEK`، مهار SSRF/CSRF/Prompt-Injection و روال Break-Glass
- `16_THREAT_MODEL.md` — مدل تهدید STRIDE وب چندمستأجری و مرز دقیق تهدید کارگر دیتابیس
- `17_UI_UX_ARCHITECTURE.md` — معماری رابط کاربری وب فارسی/RTL، افشای تدریجی و Polling بدون حالت
- `18_DEPLOYMENT_ARCHITECTURE.md` — معماری استقرار روی `Ubuntu 24.04 LTS`، توپولوژی Docker Compose و مهاجرت بدون `BYPASSRLS`
- `19_RELIABILITY_AND_RECOVERY.md` — پایداری، بازیابی کرش، `PITR` با رمزنگاری احرازشده و سیاست `Crypto-Shredding`
- `20_PERFORMANCE_AND_CAPACITY.md` — کارایی، ظرفیت‌سنجی پایلوت ۵۰ مستأجر و شاخص‌های کالیبره‌شده در `G0 Spike`
- `21_TEST_ARCHITECTURE.md` — معماری آزمون چندلایه، تست‌های جهش (`Mutation Testing`) و دروازه‌های CI
- `22_REQUIREMENTS_TRACEABILITY.md` — ماتریس ردیابی ۱۰۰٪ تمامی ۲۲۰ شناسه رسمی نیازمندی‌ها
- `23_RISK_REGISTER.md` — دفتر ثبت ریسک‌های فنی، امنیتی و عملیاتی نسخه وب
- `24_OPEN_DECISIONS.md` — تصمیم‌های باز مالک و **جدول پیگیری شروط باز ممیزی (`OC-01` تا `OC-10`)**
- `25_INDEPENDENT_REVIEW.md` — گزارش جمع‌بندی ممیزی‌های مستقل چندمرحله‌ای و اصلاحات اعمال‌شده
- `26_FINAL_ARCHITECTURE_REPORT.md` — گزارش تحویل نهایی معماری و نقشه دروازه‌های پیاده‌سازی فاز ۱
- `G0_TECHNICAL_SPIKE_SPEC.md` و `G0_EVIDENCE_REPORT.md` — مشخصات و گزارش شواهد اجرایی فاز صفر (`v1.3.2`)

### ب) تصمیم‌های معماری (`adr/ADR-001` تا `ADR-012`)
- `ADR-001-macro-architecture-modular-hexagonal.md`
- `ADR-002-runtime-stack-django52-ninja-and-workers.md`
- `ADR-003-ui-architecture-rtl-web-stateless-polling.md`
- `ADR-004-database-postgresql16-force-rls-and-seaweedfs-s3.md`
- `ADR-005-security-https-rbac-envelope-vault-and-pitr.md`
- `ADR-006-canonical-content-utf16-ast-and-formatter.md`
- `ADR-007-ai-gateway-dynamic-routing-and-budget-ledger.md`
- `ADR-008-four-freedom-modes-and-fact-guard.md`
- `ADR-009-feedback-learning-preference-memory.md`
- `ADR-010-reliable-delivery-at-least-once-and-uncertain-state.md`
- `ADR-011-messenger-capability-matrix-and-export-ingestion.md`
- `ADR-012-media-worker-isolated-container-and-generative-jobs.md`

### ج) قراردادهای ماشینی (`contracts/`) و نمودارها (`diagrams/01..18`)
- `contracts/database_schema.sql` — اسکیمای کامل ۵۱ جدولی PostgreSQL 16 (اجرا و تأییدشده روی `PostgreSQL 16.15`)
- `contracts/g0_spike_smoke_test.sql` — اسکیمای حداقلی و ۱۸ تست رفتاری ضدپوچی نسخه `1.3.2`
- `contracts/openapi.yaml` — قرارداد کامل OpenAPI 3.1.0 نسخه وب چندمستأجری (`47` مسیر عملیاتی)
- `contracts/internal_events.schema.json` — طرحواره رویدادهای دامنه مقید به `tenant_id` (`49` نوع رویداد)
- `contracts/style_package.schema.json` — طرحواره بسته تبادل سبک بدون اسرار با تضمین تک‌بار بودن `{body}`
- `contracts/backup_manifest.schema.json` — طرحواره مانیفست پشتیبان منطقی مستأجر و `PITR` با رمزنگاری احرازشده
- `diagrams/01_c4_system_context.md` تا `diagrams/18_backup_restore_sequence.md` — ۱۸ نمودار استاندارد Mermaid
