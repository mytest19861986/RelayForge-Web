# 08 — معماری API وب، میان‌افزار تراکنشی RLS و Polling بدون حالت

**قرارداد ماشینی متناظر:** `contracts/openapi.yaml` (`OpenAPI 3.1.0` — ۴۷ مسیر عملیاتی در فضای نام `/api/v1`).

---

## ۱. چرخه حیات درخواست در `Django 5.2 LTS + Django Ninja`

1. **خاتمه TLS و محافظت لبه (`Caddy / Nginx`):** پذیرش صرفاً HTTPS (`TLS 1.2` و `TLS 1.3`)، اعمال هدرهای امنیتی (`Strict-Transport-Security`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy`) و محدودسازی حجم بدنه درخواست‌های JSON معمولی به `2 MiB`.
2. **احراز هویت کوکی سرورمحور و CSRF:**
   - کوکی نشست با پیشوند امن `__Host-rf_session` (`Secure`, `HttpOnly`, `SameSite=Lax`, بدون ویژگی `Domain` و مقید به مسیر `/`) صادر می‌شود و هش `SHA-256` آن در جدول `app.web_sessions` بررسی می‌گردد.
   - تمامی متدهای تغییر وضعیت (`POST`, `PUT`, `PATCH`, `DELETE`) ملزم به ارسال هدر `X-CSRFToken` منطبق با نشست و بررسی `Origin` مجاز هستند.
3. **میان‌افزار اتمیک مستأجر (`TenantRLSMiddleware`):**
   - هر ویو همگام (`def`) درون یک بلوک `with transaction.atomic():` اجرا می‌شود.
   - اولین دستور تراکنش `SELECT pg_catalog.set_config('app.tenant_id', %s, true)` و `set_config('app.user_id', %s, true)` است.
   - به لطف `CONN_MAX_AGE = 0` و `pool_mode = transaction` در PgBouncer، در لحظه خروج از بلوک `atomic` تراکنش خاتمه یافته و هیچ متغیر نشستی به درخواست بعدی نشت نمی‌کند (`0` نشت در ۱٬۰۰۰ تراکنش متوالی در `SPK-01`).

---

## ۲. جایگزینی SSE با نظرسنجی کوتاه‌مدت بدون حالت (`Stateless Short-Interval Polling`)

نگه داشتن اتصالات باز طولانی‌مدت `Server-Sent Events (SSE)` روی کارگرهای همگام جنگو و استخر تراکنشی PgBouncer باعث اشغال نخ‌ها و تخلیه استخر دیتابیس می‌شود. در MVP:
- مسیر سبک `GET /api/v1/workspaces/{wsId}/events/poll?cursor=<last_event_id>&limit=50` رویدادهای تازه جدول `domain_outbox` و وضعیت کارهای فعال (`export_import_jobs`, `channel_analyses`, `media_jobs`, `delivery_tasks`) را در یک تراکنش چند میلی‌ثانیه‌ای برمی‌گرداند.
- کلاینت وب در حالت عادی هر ۱۰ ثانیه و هنگام فعال بودن یک کار پردازشی هر ۲ ثانیه (با توقف خودکار در صورت مخفی بودن تب مرورگر `Page Visibility API`) نظرسنجی می‌کند.

---

## ۳. استاندارد خطا، کلید یکتایی (`Idempotency-Key`) و محدودیت نرخ

- **کلید یکتایی اجباری:** تمام مسیرهای ایجاد کار، رزرو بودجه، انتشار پیش‌نویس، ارسال تست و آشتی `UNCERTAIN` ملزم به ارسال هدر `Idempotency-Key: <UUID>` هستند. ارسال تکراری با پارامتر یکسان همان نتیجه قبلی را برمی‌گرداند و ارسال تکراری با پارامتر متناقض کد خطای `RF005` (`409 Conflict`) تولید می‌کند.
- **نگاشت کدهای خطای دیتابیس (`RF001..RF021`) به HTTP:**
  - `RF001` (`BUDGET_EXCEEDED`) -> `429 Too Many Requests` / `402 Quota` با پیام فارسی شفاف تفکیک سقف پلن از زیربودجه مستأجر.
  - `RF006` (`NO_ACTIVE_OPERATOR_PLAN_QUOTA`) -> `403 Forbidden` (مسدودیت Fail-Closed مستأجر فاقد پلن فعال).
  - `RF007` (`TENANT_WEB_CANNOT_MUTATE_OPERATOR_PLAN_QUOTA`) -> `403 Forbidden`.
  - `RF014..RF019` (تعارض وضعیت یا فقدان رکورد آشتی `UNCERTAIN`) -> `409 Conflict`.
