# 05 — معماری مرجع کلان، لایه‌بندی و قوانین طلایی همزمانی (Master Architecture Blueprint)

## ۱. توپولوژی کلان کانتینرهای سیستم روی `Ubuntu 24.04 LTS`

سیستم از ۷ سرویس کانتینری ایزوله در یک فایل `docker-compose.yml` تشکیل می‌شود که هیچ‌کدام دسترسی به `docker.sock` ندارند:

1. **`edge-proxy` (`Caddy 2` یا `Nginx`):** خاتمه TLS (`TLS 1.2` + `TLS 1.3`)، تحویل فایل‌های استاتیک فرانت‌اند RTL، اعمال سقف حجم بدنه درخواست و هدایت `/api/*` و `/webhooks/*` به `web-api` و مسیر آپلود مستقیم Presigned به `s3-store`.
2. **`web-api` (`Django 5.2 LTS` + `Django Ninja` + `Gunicorn Sync/Threads`):** اجرای تمام ویوهای API به صورت توابع همگام (`def` views) درون تراکنش اتمیک مقید به `SET LOCAL app.tenant_id`؛ اتصال به دیتابیس منحصراً از طریق `pgbouncer` با نقش `relayforge_web`.
3. **`worker-engine` (کارگر پایتون مستقل از جنگو):** اجرای حلقه‌های برداشتن عادلانه از صف تحویل، استخراج جریانی فایل‌های Export از S3، تحلیل کانال و اجرای خط لوله AI؛ اتصال از طریق `pgbouncer` با نقش `relayforge_worker`.
4. **`media-worker` (جعبه شنی ثابت پردازش رسانه):** کانتینر غیرریشه (`10001:10001`) با `network_mode: "none"`، فایل‌سیستم ریشه فقط‌خواندنی و `FFmpeg` مقید به لیست سفید Demuxer؛ فقط از طریق سوکت یونیکس `/spool/sock/media.sock` از `worker-engine` فرمان می‌گیرد.
5. **`pgbouncer` (`pool_mode = transaction`):** استخر مالتی‌پلکس اتصالات تراکنشی بین کانتینرهای برنامه و Postgres.
6. **`postgres` (`PostgreSQL 16`):** منبع حقیقت واحد سیستم شامل اسکیمای `app` (`51` جدول با `FORCE RLS`) و آرشیو مستمر رمزنگاری‌شده WAL (`pgBackRest` / `age`).
7. **`s3-store` (`SeaweedFS -s3` با مجوز `Apache-2.0`):** مخزن اشیاء خصوصی برای نگهداری دارایی‌های رسانه (`tenants/<tid>/media/`) و بافر موقت فایل‌های Export (`tenants/<tid>/exports/` با قانون حذف خودکار ۲۴ ساعته).
*(به‌علاوه کانتینر یک‌بارمصرف استقرار `migrate-job` که فقط در لحظه ارتقا با نقش `relayforge_migrator` اجرا و خاموش می‌شود.)*

---

## ۲. ساختار پوشه‌های مخزن و گراف بدون دور وابستگی‌ها (`DAG`)

```text
relayforge/
├── core/                  # ۱۰۰٪ مستقل از Django/Ninja/ORM (فقط پایتون استاندارد + تایپ‌ها + پورت‌ها)
│   ├── domain/            # موجودیت‌ها، ماشین وضعیت‌ها، CanonicalRichText، FactGuard، قوانین مسیر
│   ├── ports/             # رابط‌های انتزاعی مخازن، پیام‌رسان‌ها، درگاه AI، رسانه و خزانه اسرار
│   └── services/          # منطق خالص دامنه برای M01 تا M10
├── adapters/              # پیاده‌سازی پورت‌ها بدون وابستگی به لایه HTTP جنگو
│   ├── postgres/          # مخازن psycopg3 با فراخوانی توابع اسکیمای app
│   ├── messengers/        # آداپتورهای httpx اتاق تمیز (Telegram, Bale, Eitaa, Rubika, Soroush+)
│   ├── ai_providers/      # آداپتورهای OpenAI, Gemini, Stock Media
│   ├── s3_storage/        # آداپتور S3v4 Presigned POST/GET و مدیریت رسانه
│   └── media_ipc/         # کلاینت سوکت AF_UNIX به media-worker
├── workers/               # فرایندهای اجرایی پس‌زمینه (صف تحویل، Reaper، استخراج Export، خط لوله خودکار)
├── web/                   # پوسته وب Django 5.2 LTS + Django Ninja + میان‌افزار RLS/Auth/CSRF
└── ui/                    # برنامه تک‌صفحه‌ای فارسی RTL (React + TypeScript)
```

### ترتیب لایه‌های مجاز میان ۱۰ ماژول دامنه (`M01` تا `M10`):
- **لایه ۰:** `M01` (Tenant & Workspace Kernel)
- **لایه ۱:** `M02` (IAM, Envelope Vault & Governance) ── وابسته به `M01`
- **لایه ۲:** `M03` (Messenger Gateway) و `M06` (AI Gateway & Budget Ledger) ── وابسته به `M01, M02`
- **لایه ۳:** `M04` (Canonical Content & Formatting) ── وابسته به `M01, M02, M03`
- **لایه ۴:** `M05` (Channel, Style & Brand Intelligence) ── وابسته به `M01..M04, M06`
- **لایه ۵:** `M07` (Content Generation & Fact Guard) ── وابسته به `M01, M02, M04, M05, M06`
- **لایه ۶:** `M08` (Media & Podcast Studio) ── وابسته به `M01, M02, M04..M07`
- **لایه ۷:** `M09` (Workflow, Drafts & Reliable Publication) ── وابسته به `M01..M08`
- **لایه ۸:** `M10` (Operations, Audit & Backup) ── مصرف‌کننده خواندنی و ممیز کل ماژول‌ها؛ **هیچ ماژولی به `M10` وابسته نیست**.

---

## ۳. قوانین سخت اجرایی برای سازگاری `Django 5.2 + psycopg3 + PgBouncer (Transaction Mode)`

1. **تنظیمات اجباری اتصال دیتابیس:** `CONN_MAX_AGE = 0`، `DISABLE_SERVER_SIDE_CURSORS = True` و `OPTIONS = {"prepare_threshold": None}` (غیرفعال کردن کامل Server-Side Prepared Statements برای جلوگیری از خطای اشتراک نشست در PgBouncer تراکنشی — `OC-05`).
2. **اجبار تراکنش در میان‌افزار RLS:** از آنجا که دستور `SET LOCAL app.tenant_id` دقیقاً در پایان تراکنش جاری منقضی می‌شود، اجرای کوئری در حالت `autocommit` مطلقاً ممنوع است؛ میان‌افزار کل درخواست را در `with transaction.atomic():` می‌پوشاند.
3. **ممنوعیت `transaction.on_commit` خام و قفل نشستی:** قانون استاتیک CI هرگونه فراخوانی مستقیم `transaction.on_commit` و `pg_advisory_lock` نشستی را رد می‌کند؛ فقط `on_tenant_commit(tenant_id, fn)` و قفل تراکنشی `pg_advisory_xact_lock` مجاز هستند (`SPK-06`).
4. **حذف SSE در MVP به نفع نظرسنجی کوتاه‌مدت بدون حالت (`Stateless Polling`):** برای جلوگیری از اشغال طولانی نخ‌های وب و اتصالات تراکنشی، به‌روزرسانی وضعیت کارها در UI از طریق مسیر سبک `GET /api/v1/workspaces/{wsId}/events/poll` (با فاصله ۲ تا ۵ ثانیه هنگامی که کار فعالی در جریان است) انجام می‌شود.
