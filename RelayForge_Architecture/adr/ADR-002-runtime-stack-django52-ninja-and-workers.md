# ADR-002: انتخاب پشته اجرایی `Python 3.12 + Django 5.2 LTS + Django Ninja` و کارگرهای مستقل
- **وضعیت:** پذیرفته‌شده (`EXECUTED` در `SPK-01` و `SPK-10`)
- **زمینه:** نیاز به چارچوب وب پایدار با پشتیبانی طولانی‌مدت (LTS)، امنیت بالغ نشست/CSRF، تایپ‌چکینگ صریح `Pydantic v2` و سازگاری کامل با تراکنش‌های اتمیک PostgreSQL پشت `PgBouncer (transaction mode)`.
- **تصمیم:** استفاده از `Django 5.2 LTS` + `Django Ninja` با ویوهای همگام (`def` views) مقید به `transaction.atomic()`، `CONN_MAX_AGE = 0`، `DISABLE_SERVER_SIDE_CURSORS = True` و `OPTIONS = {"prepare_threshold": None}` در `psycopg3`؛ اجرای کارهای پس‌زمینه در سرویس مستقل `worker-engine`.
- **پیامدها:** حذف نیاز به نگهداری دو فریم‌ورک وب، حذف نشت متغیرهای نشست در استخر تراکنشی PgBouncer و запрет استفاده از `transaction.on_commit` خام به نفع `on_tenant_commit`.
