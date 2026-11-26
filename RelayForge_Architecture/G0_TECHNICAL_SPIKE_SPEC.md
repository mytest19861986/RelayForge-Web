# مشخصات فنی فاز صفر: آزمایشگاه اثبات معماری (`G0 Technical Spike Specification — v1.3.1`)

**نسخه سند:** `1.3.2-POST-PEER-AUDIT`  
**وضعیت سند:** **`G0: PASS WITH CONDITIONS`** — هسته دیتابیس و اسکیمای نسخه `1.3.2` (شامل تفکیک سقف پلن اپراتور `OPERATOR_PLAN` از بودجه داخلی مستأجر `TENANT_BUDGET` با کد خطای `RF007`) روی `PostgreSQL 16.15` اجرا و تأیید شد (`EXECUTED`)؛ جدول تفکیکی وضعیت هر جزء (`EXECUTED` / `PARTIAL` / `PENDING`) و فهرست شرایط باز (`OC-01` تا `OC-10`) در [G0_EVIDENCE_REPORT.md](file:///d:/project/bot%20telegram%20copy/RelayForge/RelayForge_Architecture/G0_EVIDENCE_REPORT.md) ثبت شده است.
**محیط هدف آزمایشگاه:** `Ubuntu 24.04 LTS` + `Docker Compose` + `PostgreSQL 16` + `PgBouncer (Transaction Mode)` + `Python 3.12` + `Django 5.2 LTS` + `Django Ninja` + `S3-Compatible Object Storage (SeaweedFS Apache-2.0 / Garage v2.x AGPL-3.0 — جایگزین MinIO آرشیوشده)`

---

## ۰. دفتر ثبت اصلاحات نسخه `1.3.1` (رفع ۴ ایراد شدید، ۶ ایراد متوسط و ۴ مورد جزئی ممیزی PG 16.15)

### ۰.۱ اصلاحات اعمال‌شده در `v1.3.1` نسبت به `v1.3.0`

| کد یافته | شدت | ریشه خطای اثبات‌شده در ممیزی اجرایی `v1.3.0` | اصلاح مهندسی اعمال‌شده در `v1.3.1` |
|---|---|---|---|
| **S1** | **شدید** | شکست `fn_reap_expired_delivery_leases` هنگام گذار `DISPATCHING_IN_FLIGHT -> UNCERTAIN` و `RESERVED -> FAILED` با خطای RLS (چون در PostgreSQL دستور `UPDATE ... RETURNING` سیاست `SELECT` را روی تصویر جدید ردیف هم اعمال می‌کند). | افزودن `'UNCERTAIN', 'FAILED'` به عبارت `USING` در سیاست `dispatcher_select_rls` + پوشش هر ۳ مسیر خروجی Reaper (`UNCERTAIN`, `FAILED`, `READY`) در مجموعه تست دود. |
| **S2** | **شدید** | پوچ بودن تست سقف بودجه (`RAISE EXCEPTION` بدون کد همان `P0001` پیش‌فرض را تولید می‌کرد و در `WHEN SQLSTATE 'P0001'` بلعیده می‌شد؛ حتی با `IF false` هم پاس می‌شد) و کسری پوشش تست‌ها. | بازنویسی تمامی تست‌های منفی با پرچم بولی **`v_caught BOOLEAN := false` + `ASSERT v_caught`**، مهاجرت به کدهای خطای اختصاصی **`RF001..RF020`** و افزودن تست‌های کامل Reaper، ایزولاسیون مستأجر (خواندن، درج متقاطع و کانتکست خالی)، Fencing اپوک کهنه/خالی، Heartbeat، گردش باکت `DAILY` در برابر `TOTAL`، سقف سرورمحور `ROUTE`، Requeue در سقف ۱۰ تلاش، تداخل Idempotency، تسویه دیرهنگام و روال Break-Glass. |
| **S3** | **شدید** | باز بودن `INSERT` جعلی (`status='SENT', lease_epoch=99`) و `DELETE` مستقیم روی `delivery_tasks` برای نقش `worker` چون تریگر فقط `BEFORE UPDATE` بود. | اجرای `REVOKE DELETE` از هر دو نقش `web` و `worker` + ارتقای تریگر به **`BEFORE INSERT OR UPDATE OR DELETE`** (درج فقط با `status IN ('PENDING','SCHEDULED','READY')`، `lease_epoch = 0`، `attempts = 0`، `max_attempts <= 10` و بدون `lease_owner`/`remote_message_id`). |
| **S4** | **شدید** | دور زدن بودجه: ۱) امکان `UPDATE budget_policies` و `UPDATE budget_reservations` توسط `worker`؛ ۲) ارسال دستی فهرست سیاست‌ها توسط کلاینت و نبود اتصال رابطه‌ای `scope_type` به Workspace و Route. | ۱) حذف مجوز نوشتن `worker` روی `budget_policies`، حذف مجوز نوشتن مستقیم `web` و `worker` روی `budget_reservations` و `budget_reservation_allocations`، و کپسوله‌سازی نوشتن دفتر کل در توابع `SECURITY DEFINER` تحت مالکیت نقش `relayforge_ledger_owner` (`NOLOGIN`، `NOBYPASSRLS` و مقید به `FORCE RLS` مستأجر)؛ ۲) افزودن جدول `app.routes` و مشتق‌سازی اجباری کلیه سیاست‌های فعال `TENANT + WORKSPACE + ROUTE` در سمت سرور از روی `(v_tenant_id, p_workspace_id, p_route_id)` به همراه رفتار **Fail-Closed (`RF006`)** در نبود سیاست فعال. |
| **M5** | متوسط | ناممکن بودن ویرایش فیلدهای غیروضعیتی کار منتظر توسط `web`، و نبود تمدید اجاره (`Heartbeat`) در کارگر برای ارسال‌های طولانی بیش از ۶۰۰ ثانیه. | مجاز شدن ویرایش غیروضعیتی کارهای منتظر بدون اجاره در شاخه `web` تریگر (با حفظ سقف `max_attempts <= 10`) + افزودن تابع حصارکشی‌شده **`app.fn_worker_heartbeat_lease`** برای تمدید امن `lease_expires_at`. |
| **M6** | متوسط | ناهماهنگی متن سند و کد تریگر در گذار `PENDING -> SCHEDULED/READY/SKIPPED` برای `web` و بررسی‌نشدن سررسید، انقضا و `attempts+1` در شاخه `dispatcher_owner` تریگر. | همگام‌سازی کامل کد تریگر با ماتریس گذار برای هر سه نقش (`web`، `worker`، `dispatcher_owner`) شامل کنترل دقیق سررسید، انقضا، `attempts + 1` و تناسب `attempts` با `max_attempts`. |
| **M7** | متوسط | گیر کردن کار `UNCERTAIN` دارای `attempts=10, max_attempts=10` هنگام `CONFIRM_NOT_SENT_REQUEUE` به علت قید `CHECK (max_attempts <= 10)`. | محدود کردن سقف اولیه هنگام `INSERT` و ویرایش عادی وب به `<= 10` در تریگر، و افزایش سقف ستون جدول تا `100` صرفاً برای تصمیم ممیزی‌شده Requeue از `UNCERTAIN` (`max_attempts = attempts + 1`). |
| **M8** | متوسط | برخورد کدهای `P0002..P0004` با استثناهای داخلی PL/pgSQL (`NO_DATA_FOUND`, `ASSERT_FAILURE`) و اشتراک `P0001` با `RAISE EXCEPTION` عمومی. | انتقال کلیه کدهای خطای دامنه به فضای نام اختصاصی ۵-کاراکتری **`RF001` تا `RF020`** (حذف کامل `P00xx`). |
| **M9** | متوسط | ۱) بازگشت بی‌صدای شناسه قدیمی در تکرار `idempotency_key` با مبلغ یا مسیر متفاوت؛ ۲) شکستن سقف بودجه در تسویه دیرهنگام (`COMMITTED`) پس از انقضای رزرو. | ۱) بررسی دقیق تطابق `(workspace_id, route_id, reserved_micros)` در تکرار کلید و پرتاب `RF005` در صورت مغایرت؛ ۲) افزودن تابع تسویه **`app.fn_budget_settle`** با بازبینی مجدد سقف بودجه در صورت انقضای مهلت رزرو یا فزونی مصرف واقعی بر مبلغ رزرو (`RF004`). |
| **M10** | متوسط | پیمایش خطی کل Backlog در پنجره `ROW_NUMBER() OVER (PARTITION BY tenant_id)` تابع `claim` و نبود پوشش مرتب‌سازی در ایندکس. | افزودن ستون محاسباتی ذخیره‌شده `runnable_at` به همراه ایندکس `idx_delivery_fair_claim (tenant_id, runnable_at ASC)` و بازنویسی تابع `fn_worker_claim_next_jobs` با الگوریتم **سهمیه‌بندی عادلانه مبتنی بر پیمایش مستقیم ایندکس (`FOR UPDATE OF dt SKIP LOCKED` با `LIMIT v_share`)** و به‌روزرسانی مرتب‌شده `tenant_dispatch_fairness` بر اساس `tenant_id ASC`؛ هم پیمایش از اندازه کل Backlog مستقل می‌شود و هم همزمانی `100 + 100` بین دو نشست بدون قفل اضافه حفظ می‌گردد (تأیید نهایی پلان و زمان اجرا موکول به اجرای واقعی `SPK-02` و `SPK-10` با `EXPLAIN (ANALYZE, BUFFERS)` است). |
| **L1–L4** | جزئی | ۱) مرز تهدید GUC در کارگر؛ ۲) نبود روال Break-Glass ابرکاربر دیتابیس؛ ۳) نداشتن رمزنگاری داخلی در `pg_basebackup` و ماندگاری متن `payload_json` در پشتیبان‌ها؛ ۴) هشدار یک‌بارمصرف بودن اسکریپت تست. | مستندسازی صریح هر ۴ مورد در بخش ۱.۱، کد تریگر (`app.break_glass_reason`) و سرصفحه فایل تست. |

### ۰.۲ خلاصه اصلاحات پیشین (`v1.2` به `v1.3.0` — تأییدشده در اجرای PG 16.15)
موارد زیر در اجرای واقعی بازبین تأیید شدند و در `v1.3.1` عیناً حفظ شده‌اند: اسکیمای اختصاصی `app` تحت مالکیت `relayforge_migrator`، حذف پیشوند `pg_catalog.` از کلمات کلیدی نحوی (`NULLIF`, `COALESCE`, `LEAST`, `GREATEST`)، رد صریح آرگومان `NULL` با کد `22004`، پوشش کارهای سررسیدشده `SCHEDULED` و `RETRY_WAIT`، و حضور سیاست‌های `*_migrator_rls` روی تمامی جداول دارای `FORCE RLS`.

---

## ۱. قراردادهای معماری پیشینی برای راستی‌آزمایی در Spike

### ۱.۱ توپولوژی ۵ نقش دیتابیس، مدل تهدید کارگر، Break-Glass و پشتیبان `PITR`
1. **تفکیک ۵ نقش بدون هیچ‌گونه `BYPASSRLS`:**
   - `relayforge_migrator` (`LOGIN`, `NOBYPASSRLS`): مالک جداول اسکیمای `app`؛ فقط در کانتینر یک‌بارمصرف مهاجرت (`migrate-job`) با `SET LOCAL app.migration_mode = 'on'` اجرا می‌شود و اعتبارنامه آن در محیط `web` و `worker` وجود ندارد.
   - `relayforge_web` (`LOGIN`, `NOBYPASSRLS`): نقش برنامه وب؛ مقید به `SET LOCAL app.tenant_id`؛ فاقد دسترسی `DELETE` روی `delivery_tasks` و فاقد دسترسی نوشتن مستقیم روی جداول رزرو بودجه.
   - `relayforge_worker` (`LOGIN`, `NOBYPASSRLS`): نقش کارگران پردازش و تحویل؛ مقید به `app.tenant_id` و توکن حصارکشی `app.lease_epoch`؛ فقط خواندنی روی `budget_policies` و فاقد دسترسی نوشتن مستقیم روی جداول رزرو بودجه.
   - `relayforge_dispatcher_owner` (`NOLOGIN`, `NOBYPASSRLS`): مالک توابع `SECURITY DEFINER` مربوط به برداشتن عادلانه از صف (`fn_worker_claim_next_jobs`) و جمع‌آوری اجاره‌های منقضی (`fn_reap_expired_delivery_leases`) با دسترسی ستونی کمینه.
   - `relayforge_ledger_owner` (`NOLOGIN`, `NOBYPASSRLS`): مالک توابع `SECURITY DEFINER` دفتر کل بودجه (`fn_budget_reserve`, `fn_budget_settle`) که همچنان تحت سیاست‌های `FORCE RLS` مبتنی بر `app.tenant_id` اجرا می‌شوند و جلوی هرگونه دستکاری مستقیم جداول رزرو توسط `web` یا `worker` را می‌گیرند.
2. **مرز دقیق مدل تهدید کارگر (`Bug/Zombie Safety` در برابر `Compromised Worker`):**
   - از آنجا که فرایند `relayforge_worker` مجاز است متغیرهای تراکنشی `app.tenant_id` و `app.lease_epoch` را تنظیم کند و تابع سراسری `fn_reap_expired_delivery_leases` را فراخوانی نماید، کنترل‌های RLS و Fencing در لایه دیتابیس **در برابر باگ‌های منطقی برنامه، شرایط مسابقه، نشت استخر اتصالات و کارگرهای متوقف‌شده (`Zombie Workers`) محافظت می‌کنند**؛ اما در برابر یک فرایند کارگر کاملاً تسخیرشده با اجرای کد دلخواه مهاجم (`RCE inside Worker`) مرز امنیتی مطلق نیستند. مهار کد غیرقابل‌اعتماد بر عهده ایزولاسیون کانتینر بدون شبکه `media-worker`، عدم اجرای کد تولیدی مدل AI و کمینه‌سازی اسرار در حافظه است.
3. **روال اضطراری ابرکاربر دیتابیس (`Audited Break-Glass`):**
   - تریگر ماشین وضعیت به‌صورت پیش‌فرض حتی تغییرات مستقیم ابرکاربر (`postgres`) را با خطای `RF020` رد می‌کند تا از ویرایش دستی تصادفی در محیط عملیاتی جلوگیری شود. در رخدادهای اضطراری، ابرکاربر موظف است درون تراکنش شناسه رخداد را با `SET LOCAL app.break_glass_reason = 'INC-...'` ثبت کند تا تریگر اجازه عبور دهد.
4. **پشتیبان فیزیکی `PITR` با رمزنگاری بیرونی و شفافیت پنجره ماندگاری داده:**
   - ابزار `pg_basebackup` به خودی خود رمزنگاری داخلی ندارد؛ بنابراین در `SPK-09` و محیط عملیاتی الزاماً باید با ابزار پشتیبان رمزنگاری‌کننده بیرونی نظیر **`pgBackRest` (`cipher-type=aes-256-cbc`)** یا لوله رمزنگاری **`age` / `gpg`** روی آرشیو پایه و فایل‌های WAL (`wal_level = replica`, `archive_mode = on`) اجرا شود.
   - در صورت درخواست حذف مستأجر: الف) ردیف‌های مستأجر در دیتابیس زنده حذف می‌شوند؛ ب) کلید رمزنگاری اسرار مستأجر (`Tenant DEK`) امحا می‌شود (**Crypto-Shredding**) که اسرار جدول `secrets_vault` را در پشتیبان‌های قبلی بلافاصله غیرقابل بازیابی می‌سازد؛ ج) داده‌های متنی غیررمزنگاری‌شده (مانند `payload_json`) تا پایان دوره نگهداری پشتیبان فیزیکی (مثلاً ۷ تا ۱۴ روز) در آرشیو رمزنگاری‌شده باقی می‌مانند و سپس با چرخش Retention به‌طور کامل حذف می‌شوند (این مورد باید صریحاً در قرارداد حریم خصوصی درج شود).

### ۱.۲ ماتریس کامل گذار وضعیت تحویل و جدول کدهای خطای دامنه (`RF0xx`)

| عملیات / گذار مجاز (`OLD -> NEW`) | نقش مجاز (`CURRENT_USER`) | شروط اجبارشده در تریگر `trg_enforce_delivery_state_machine` |
|---|---|---|
| `INSERT` اولیه | `relayforge_web` یا `relayforge_worker` | فقط `status IN ('PENDING','SCHEDULED','READY')`، `lease_epoch = 0`، `attempts = 0`، `max_attempts <= 10` و تهی بودن `lease_owner`/`lease_expires_at`/`next_retry_at`/`remote_message_id` |
| `DELETE` مستقیم | هیچ نقش برنامه‌ای (`REVOKE DELETE`) | مسدود برای همه نقش‌ها (`RF010`) به جز مهاجرت کنترل‌شده یا Break-Glass ممیزی‌شده |
| ویرایش غیروضعیتی کار منتظر (`S -> S`) | فقط `relayforge_web` | فقط در `PENDING \| SCHEDULED \| READY`، بدون تغییر `lease_epoch`/`attempts`، `max_attempts <= 10` و بدون اجاره فعال |
| `PENDING -> SCHEDULED \| READY \| SKIPPED` | `relayforge_web` یا `relayforge_worker` | بدون تغییر `lease_epoch` و `attempts`، `max_attempts <= 10` و بدون اجاره فعال |
| `(SCHEDULED \| READY \| RETRY_WAIT) -> RESERVED` | فقط `relayforge_dispatcher_owner` | سررسید موعد (`runnable_at <= now`)، `OLD.attempts < OLD.max_attempts`، `NEW.attempts = OLD.attempts + 1`، `NEW.lease_epoch = OLD.lease_epoch + 1` و پاک شدن `next_retry_at` |
| `RESERVED (منقضی) -> READY \| FAILED` | فقط `relayforge_dispatcher_owner` | `OLD.lease_expires_at < now`، افزایش `lease_epoch + 1`، `READY` فقط اگر `attempts < max_attempts` و `FAILED` فقط اگر `attempts >= max_attempts` |
| `DISPATCHING_IN_FLIGHT (منقضی) -> UNCERTAIN` | فقط `relayforge_dispatcher_owner` | `OLD.lease_expires_at < now`، پاک شدن `lease_owner`/`lease_expires_at` و افزایش `lease_epoch + 1` |
| تمدید اجاره (`Heartbeat`: `S -> S`) | فقط `relayforge_worker` (`fn_worker_heartbeat_lease`) | فقط در `RESERVED \| DISPATCHING_IN_FLIGHT`، تطابق دقیق `app.lease_epoch == OLD.lease_epoch`، منقضی نبودن اجاره فعلی و تمدید `lease_expires_at > now` |
| `RESERVED -> DISPATCHING_IN_FLIGHT \| RETRY_WAIT \| FAILED` | فقط `relayforge_worker` (`fn_worker_transition_task`) | تطابق `app.lease_epoch == OLD.lease_epoch`، منقضی نبودن اجاره، و در `RETRY_WAIT`: `OLD.attempts < OLD.max_attempts` و `NEW.next_retry_at IS NOT NULL` |
| `DISPATCHING_IN_FLIGHT -> SENT \| RETRY_WAIT \| FAILED \| UNCERTAIN` | فقط `relayforge_worker` (`fn_worker_transition_task`) | تطابق `app.lease_epoch == OLD.lease_epoch`، منقضی نبودن اجاره و عدم تغییر دستی `lease_epoch`/`attempts` |
| `(PENDING \| SCHEDULED \| READY \| RETRY_WAIT) -> CANCELLED` | فقط `relayforge_web` | افزایش `NEW.lease_epoch = OLD.lease_epoch + 1` و حفظ `attempts`/`max_attempts` |
| **`UNCERTAIN -> SENT \| READY \| CANCELLED`** | **فقط `relayforge_web`** (`fn_web_reconcile_uncertain_task`) | **الزام وجود رکورد منطبق در `app.reconciliation_records` برای `(OLD.id, OLD.lease_epoch)`**، افزایش `lease_epoch + 1` و در حالت `READY` تنظیم `max_attempts = GREATEST(OLD.max_attempts, OLD.attempts + 1)` |

| کد `SQLSTATE` | معنای قراردادی در لایه داده و برنامه |
|---|---|
| `RF001` | عبور از سقف بودجه حداقل یکی از سیاست‌های فعال مشتق‌شده (`TENANT` / `WORKSPACE` / `ROUTE`) |
| `RF002` | شناسه `workspace_id` یا `route_id` نامعتبر یا خارج از مرز مستأجر جاری |
| `RF003` | عدم تنظیم متغیر الزامی `app.tenant_id` درون تراکنش |
| `RF004` | رزرو بودجه ناموجود/تسویه‌شده، یا رد تسویه دیرهنگام (`COMMITTED`) پس از انقضا به علت عبور از سقف باقی‌مانده |
| `RF005` | مغایرت پارامترهای مبلغ/دامنه در ارسال مجدد یک `idempotency_key` تکراری |
| `RF006` | خطای Fail-Closed: نبود هیچ سیاست بودجه فعال برای مستأجر/دامنه مورد تقاضا |
| `RF010` | تلاش غیرمجاز برای `DELETE`، تغییر ستون‌های هویتی، یا `INSERT` با وضعیت/اپوک غیراولیه |
| `RF011`..`RF012` | نقض قوانین اپوک، سررسید، انقضا یا شمارنده تلاش در شاخه `relayforge_dispatcher_owner` |
| `RF013`..`RF017` | تلاش `relayforge_worker` برای دستکاری اپوک/تلاش، فقدان یا کهنگی `app.lease_epoch`، انقضای اجاره یا گذار غیرمجاز |
| `RF018`..`RF019` | تلاش `relayforge_web` برای خروج از `UNCERTAIN` بدون رکورد آشتی معتبر یا گذار غیرمجاز وب |
| `RF020` | نقش غیرمجاز یا ویرایش مستقیم ابرکاربر بدون ثبت `app.break_glass_reason` |

### ۱.۳ قراردادهای Django 5.2 + PgBouncer، کانتینر `media-worker` و داده مصنوعی
1. **تنظیمات اجباری Django 5.2 و `psycopg3` پشت `PgBouncer (pool_mode = transaction)`:**
   - `CONN_MAX_AGE = 0`، `DISABLE_SERVER_SIDE_CURSORS = True` و بررسی صریح `OPTIONS["prepare_threshold"] = None` (یا `0`) در `psycopg3` جهت جلوگیری از خطای Prepared Statement بین نشست‌های اشتراکی PgBouncer.
   - میان‌افزار ایزولاسیون مستأجر کل چرخه درخواست احراز هویت‌شده را درون `with transaction.atomic():` قرار می‌دهد و در ابتدای آن `SELECT set_config('app.tenant_id', %s, true)` اجرا می‌کند.
   - استفاده مستقیم از `transaction.on_commit` خام توسط قانون سفارشی `ruff` مسدود می‌شود و تنها تابع کمکی `on_tenant_commit(tenant_id, fn)` که شناسه مستأجر را صریحاً به کار پس‌زمینه منتقل می‌کند مجاز است.
   - جایگزین‌های بدون Redis در MVP: قفل تراکنشی `pg_advisory_xact_lock` (ممنوعیت مطلق قفل نشستی `pg_advisory_lock`) و جدول `UNLOGGED` برای پنجره‌های کوتاه Rate-Limit (با آگاهی از پاک شدن آن در کرش غیرعادی Postgres).
2. **قرارداد امنیتی کانتینر ثابت `media-worker` روی Ubuntu 24.04:**
   - بدون `Bubblewrap` و بدون مانت `docker.sock`؛ کانتینر ثابت غیرریشه (`user: "10001:10001"`) با `network_mode: "none"`، `read_only: true`، `cap_drop: ["ALL"]`، `security_opt: ["no-new-privileges:true"]`، `pids_limit: 64`، `mem_limit: "512m"` و ارتباط از طریق سوکت یونیکس روی حجم مشترک `/spool` با پرچم `noexec,nosuid,nodev`.
   - اعتبارسنجی اولیه با `ffprobe` و اجرای `ffmpeg` با لیست سفید صریح `-f <allowed_demuxer>` (`mov,mp4,m4a,3gp,3g2,mj2`, `ogg`, `wav`, `mp3`, `image2`) و `-protocol_whitelist file,pipe` (رد کامل `concat`, `hls`, `m3u8` و مراجع خارجی).
3. **تطبیق آستانه کارایی با `NFR-01` و داده ۱۰۰٪ مصنوعی در `SPK-08`:**
   - معیار قبولی تاخیر APIهای داخلی در آزمون بار پایه مطابق `NFR-01` برابر **`p95 <= 500 ms`** روی محیط مرجع (`4 vCPU, 8 GiB RAM, NVMe`، ۵۰ مستأجر، ۱۰۰٬۰۰۰ رکورد) است.
   - در `SPK-08` استفاده از هرگونه خروجی کانال واقعی ممنوع است و صرفاً خروجی‌های تولیدشده توسط `generate_synthetic_tg_export.py` در سه اندازه `5 MiB`، `80 MiB` و `240 MiB` به کار می‌رود.

---

## ۲. کد کامل اسکیمای حداقلی نسخه `1.3.1` (همسان با فایل مستقل Smoke Test)

فایل مستقل حاوی همین اسکیما به انضمام مقدمه پاک‌سازی محیط آزمایشگاهی یک‌بارمصرف و مجموعه تست‌های رفتاری بدون پوچی در مسیر [g0_spike_smoke_test.sql](file:///d:/project/bot%20telegram%20copy/RelayForge/RelayForge_Architecture/contracts/g0_spike_smoke_test.sql) قرار دارد (توجه: دستورات `DROP SCHEMA/ROLE` و گذرواژه‌های ثابت ابتدای فایل مستقل صرفاً مخصوص کانتینر یک‌بارمصرف آزمایشگاه هستند):

```sql
-- ============================================================================
-- 1. Roles & Dedicated Schema 'app' (Zero BYPASSRLS across all non-superuser app roles)
-- ============================================================================
CREATE ROLE relayforge_migrator LOGIN PASSWORD 'spike_mig_only' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE relayforge_operator LOGIN PASSWORD 'spike_ops_only' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE relayforge_web LOGIN PASSWORD 'spike_web_only' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE relayforge_worker LOGIN PASSWORD 'spike_wrk_only' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE relayforge_dispatcher_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE relayforge_ledger_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

CREATE SCHEMA app AUTHORIZATION relayforge_migrator;
REVOKE ALL ON SCHEMA app FROM PUBLIC;
GRANT USAGE ON SCHEMA app TO relayforge_operator, relayforge_web, relayforge_worker, relayforge_dispatcher_owner, relayforge_ledger_owner;

-- ============================================================================
-- 2. Tables & Indexes Owned by relayforge_migrator (9 FORCE-RLS Tables)
-- ============================================================================
SET ROLE relayforge_migrator;

CREATE TABLE app.tenants (
    id UUID PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp()
);

CREATE TABLE app.workspaces (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
    slug TEXT NOT NULL,
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, slug)
);

CREATE TABLE app.routes (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    slug TEXT NOT NULL,
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, workspace_id, id),
    UNIQUE (tenant_id, workspace_id, slug),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT
);

CREATE TABLE app.tenant_dispatch_fairness (
    tenant_id UUID PRIMARY KEY REFERENCES app.tenants(id) ON DELETE CASCADE,
    last_served_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE app.delivery_tasks (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    route_id UUID,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'SCHEDULED', 'READY', 'RESERVED', 'DISPATCHING_IN_FLIGHT',
        'SENT', 'RETRY_WAIT', 'FAILED', 'UNCERTAIN', 'SKIPPED', 'CANCELLED'
    )),
    scheduled_for TIMESTAMPTZ NOT NULL,
    next_retry_at TIMESTAMPTZ,
    runnable_at TIMESTAMPTZ GENERATED ALWAYS AS (COALESCE(next_retry_at, scheduled_for)) STORED,
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    lease_epoch BIGINT NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
    attempts INT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INT NOT NULL DEFAULT 4 CHECK (max_attempts BETWEEN 1 AND 100),
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    remote_message_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    CHECK (status <> 'RETRY_WAIT' OR next_retry_at IS NOT NULL),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, workspace_id, idempotency_key),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, workspace_id, route_id) REFERENCES app.routes(tenant_id, workspace_id, id) ON DELETE RESTRICT
);

CREATE TABLE app.reconciliation_records (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    delivery_task_id UUID NOT NULL,
    task_lease_epoch BIGINT NOT NULL,
    resolution_action TEXT NOT NULL CHECK (resolution_action IN (
        'CONFIRM_SENT', 'CONFIRM_NOT_SENT_REQUEUE', 'DISCARD_DO_NOT_SEND'
    )),
    acknowledge_duplicate_risk BOOLEAN NOT NULL DEFAULT false CHECK (
        resolution_action <> 'CONFIRM_NOT_SENT_REQUEUE' OR acknowledge_duplicate_risk = true
    ),
    decided_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (delivery_task_id, task_lease_epoch),
    FOREIGN KEY (tenant_id, delivery_task_id) REFERENCES app.delivery_tasks(tenant_id, id) ON DELETE RESTRICT
);

-- Server-Derived Scoped Budget Policies (v1.3.2: Separates immutable OPERATOR_PLAN ceiling from TENANT_BUDGET sub-caps!)
CREATE TABLE app.budget_policies (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
    policy_origin TEXT NOT NULL CHECK (policy_origin IN ('OPERATOR_PLAN', 'TENANT_BUDGET')),
    scope_type TEXT NOT NULL CHECK (scope_type IN ('TENANT', 'WORKSPACE', 'ROUTE')),
    scope_ref_id UUID NOT NULL,
    period_kind TEXT NOT NULL CHECK (period_kind IN ('DAILY', 'MONTHLY', 'TOTAL')),
    max_cost_micros BIGINT NOT NULL CHECK (max_cost_micros >= 0),
    is_active BOOLEAN NOT NULL DEFAULT true,
    CHECK (scope_type <> 'TENANT' OR scope_ref_id = tenant_id),
    CHECK (policy_origin <> 'OPERATOR_PLAN' OR scope_type = 'TENANT'),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, policy_origin, scope_type, scope_ref_id, period_kind)
);

CREATE TABLE app.budget_reservations (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    route_id UUID,
    idempotency_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('RESERVED', 'COMMITTED', 'RELEASED')),
    reserved_micros BIGINT NOT NULL CHECK (reserved_micros >= 0),
    settled_micros BIGINT CHECK (settled_micros IS NULL OR settled_micros >= 0),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, workspace_id, route_id) REFERENCES app.routes(tenant_id, workspace_id, id) ON DELETE RESTRICT
);

CREATE TABLE app.budget_reservation_allocations (
    reservation_id UUID NOT NULL,
    policy_id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    period_bucket TEXT NOT NULL,
    PRIMARY KEY (reservation_id, policy_id),
    FOREIGN KEY (tenant_id, reservation_id) REFERENCES app.budget_reservations(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, policy_id) REFERENCES app.budget_policies(tenant_id, id) ON DELETE RESTRICT
);

-- Index supporting O(batch_size) per-tenant index-order SKIP LOCKED scan without full backlog sort (Fixes M10)
CREATE INDEX idx_delivery_fair_claim
    ON app.delivery_tasks (tenant_id, runnable_at ASC)
    WHERE status IN ('READY', 'SCHEDULED', 'RETRY_WAIT');

CREATE INDEX idx_delivery_reap_expired
    ON app.delivery_tasks (lease_expires_at ASC)
    WHERE status IN ('RESERVED', 'DISPATCHING_IN_FLIGHT');

CREATE INDEX idx_budget_policies_active_scope
    ON app.budget_policies (tenant_id, scope_type, scope_ref_id)
    WHERE is_active = true;

CREATE INDEX idx_budget_alloc_period
    ON app.budget_reservation_allocations (policy_id, period_bucket);

-- ============================================================================
-- 3. Strict Privilege Separation (Fixes S3 & S4: No DELETE; Encapsulated Ledger)
-- ============================================================================
REVOKE ALL ON ALL TABLES IN SCHEMA app FROM PUBLIC;

GRANT SELECT ON app.tenants TO relayforge_web, relayforge_worker;
GRANT SELECT (id) ON app.tenants TO relayforge_dispatcher_owner;

GRANT SELECT, INSERT, UPDATE, DELETE ON app.workspaces, app.routes TO relayforge_web;
GRANT SELECT ON app.workspaces, app.routes TO relayforge_worker, relayforge_ledger_owner;

-- Neither web nor worker has DELETE on delivery_tasks! Trigger guards INSERT & UPDATE.
GRANT SELECT, INSERT, UPDATE ON app.delivery_tasks TO relayforge_web, relayforge_worker;
GRANT SELECT, INSERT ON app.reconciliation_records TO relayforge_web;

-- Dispatcher owner gets minimal column-level access ONLY:
GRANT SELECT (id, tenant_id, status, scheduled_for, next_retry_at, runnable_at, lease_expires_at, lease_epoch, attempts, max_attempts),
      UPDATE (status, next_retry_at, lease_owner, lease_expires_at, lease_epoch, attempts, updated_at)
      ON app.delivery_tasks TO relayforge_dispatcher_owner;
GRANT SELECT, INSERT, UPDATE ON app.tenant_dispatch_fairness TO relayforge_dispatcher_owner;

-- Budget encapsulation (Fixes S4 & v1.3.2 OC-01):
--   1. OPERATOR_PLAN ceilings are writable ONLY by relayforge_operator / relayforge_migrator; relayforge_web can only mutate TENANT_BUDGET!
--   2. Worker CANNOT mutate budget_policies (read-only).
--   3. Neither web nor worker can directly INSERT/UPDATE/DELETE budget_reservations or allocations!
GRANT SELECT, INSERT, UPDATE, DELETE ON app.budget_policies TO relayforge_operator, relayforge_web;
GRANT SELECT ON app.budget_policies TO relayforge_worker;
GRANT SELECT, UPDATE ON app.budget_policies TO relayforge_ledger_owner;

GRANT SELECT ON app.budget_reservations, app.budget_reservation_allocations TO relayforge_web, relayforge_worker;
GRANT SELECT, INSERT, UPDATE ON app.budget_reservations TO relayforge_ledger_owner;
GRANT SELECT, INSERT ON app.budget_reservation_allocations TO relayforge_ledger_owner;

-- ============================================================================
-- 4. Enable & Force RLS + Complete Policies (Fixes S1: UNCERTAIN/FAILED in SELECT)
-- ============================================================================
ALTER TABLE app.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.tenants FORCE ROW LEVEL SECURITY;
ALTER TABLE app.workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.workspaces FORCE ROW LEVEL SECURITY;
ALTER TABLE app.routes ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.routes FORCE ROW LEVEL SECURITY;
ALTER TABLE app.tenant_dispatch_fairness ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.tenant_dispatch_fairness FORCE ROW LEVEL SECURITY;
ALTER TABLE app.delivery_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.delivery_tasks FORCE ROW LEVEL SECURITY;
ALTER TABLE app.reconciliation_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.reconciliation_records FORCE ROW LEVEL SECURITY;
ALTER TABLE app.budget_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.budget_policies FORCE ROW LEVEL SECURITY;
ALTER TABLE app.budget_reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.budget_reservations FORCE ROW LEVEL SECURITY;
ALTER TABLE app.budget_reservation_allocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.budget_reservation_allocations FORCE ROW LEVEL SECURITY;

-- Tenant Isolation Policies (Applies to web, worker, AND ledger_owner!)
CREATE POLICY tenants_tenant_rls ON app.tenants
    FOR SELECT TO relayforge_web, relayforge_worker
    USING (id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY workspaces_tenant_rls ON app.workspaces
    FOR ALL TO relayforge_web, relayforge_worker, relayforge_ledger_owner
    USING (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY routes_tenant_rls ON app.routes
    FOR ALL TO relayforge_web, relayforge_worker, relayforge_ledger_owner
    USING (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY delivery_tasks_tenant_rls ON app.delivery_tasks
    FOR ALL TO relayforge_web, relayforge_worker
    USING (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY reconciliation_tenant_rls ON app.reconciliation_records
    FOR ALL TO relayforge_web
    USING (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY budget_policies_tenant_rls ON app.budget_policies
    FOR ALL TO relayforge_operator, relayforge_web, relayforge_worker, relayforge_ledger_owner
    USING (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY budget_reservations_tenant_rls ON app.budget_reservations
    FOR ALL TO relayforge_web, relayforge_worker, relayforge_ledger_owner
    USING (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY budget_allocations_tenant_rls ON app.budget_reservation_allocations
    FOR ALL TO relayforge_web, relayforge_worker, relayforge_ledger_owner
    USING (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid);

-- Migrator Policies on ALL 9 FORCE-RLS Tables
CREATE POLICY tenants_migrator_rls ON app.tenants FOR ALL TO relayforge_migrator
    USING (pg_catalog.current_setting('app.migration_mode', true) = 'on')
    WITH CHECK (pg_catalog.current_setting('app.migration_mode', true) = 'on');
CREATE POLICY workspaces_migrator_rls ON app.workspaces FOR ALL TO relayforge_migrator
    USING (pg_catalog.current_setting('app.migration_mode', true) = 'on')
    WITH CHECK (pg_catalog.current_setting('app.migration_mode', true) = 'on');
CREATE POLICY routes_migrator_rls ON app.routes FOR ALL TO relayforge_migrator
    USING (pg_catalog.current_setting('app.migration_mode', true) = 'on')
    WITH CHECK (pg_catalog.current_setting('app.migration_mode', true) = 'on');
CREATE POLICY fairness_migrator_rls ON app.tenant_dispatch_fairness FOR ALL TO relayforge_migrator
    USING (pg_catalog.current_setting('app.migration_mode', true) = 'on')
    WITH CHECK (pg_catalog.current_setting('app.migration_mode', true) = 'on');
CREATE POLICY delivery_tasks_migrator_rls ON app.delivery_tasks FOR ALL TO relayforge_migrator
    USING (pg_catalog.current_setting('app.migration_mode', true) = 'on')
    WITH CHECK (pg_catalog.current_setting('app.migration_mode', true) = 'on');
CREATE POLICY reconciliation_migrator_rls ON app.reconciliation_records FOR ALL TO relayforge_migrator
    USING (pg_catalog.current_setting('app.migration_mode', true) = 'on')
    WITH CHECK (pg_catalog.current_setting('app.migration_mode', true) = 'on');
CREATE POLICY budget_policies_migrator_rls ON app.budget_policies FOR ALL TO relayforge_migrator
    USING (pg_catalog.current_setting('app.migration_mode', true) = 'on')
    WITH CHECK (pg_catalog.current_setting('app.migration_mode', true) = 'on');
CREATE POLICY budget_reservations_migrator_rls ON app.budget_reservations FOR ALL TO relayforge_migrator
    USING (pg_catalog.current_setting('app.migration_mode', true) = 'on')
    WITH CHECK (pg_catalog.current_setting('app.migration_mode', true) = 'on');
CREATE POLICY budget_allocations_migrator_rls ON app.budget_reservation_allocations FOR ALL TO relayforge_migrator
    USING (pg_catalog.current_setting('app.migration_mode', true) = 'on')
    WITH CHECK (pg_catalog.current_setting('app.migration_mode', true) = 'on');

-- Dispatcher Policies (Fixes S1: UNCERTAIN & FAILED included in SELECT USING for UPDATE...RETURNING!)
CREATE POLICY dispatcher_tenants_select_rls ON app.tenants
    FOR SELECT TO relayforge_dispatcher_owner USING (true);

CREATE POLICY dispatcher_fairness_rls ON app.tenant_dispatch_fairness
    FOR ALL TO relayforge_dispatcher_owner USING (true) WITH CHECK (true);

CREATE POLICY dispatcher_select_rls ON app.delivery_tasks
    FOR SELECT TO relayforge_dispatcher_owner
    USING (status IN ('SCHEDULED', 'READY', 'RETRY_WAIT', 'RESERVED', 'DISPATCHING_IN_FLIGHT', 'UNCERTAIN', 'FAILED'));

CREATE POLICY dispatcher_update_rls ON app.delivery_tasks
    FOR UPDATE TO relayforge_dispatcher_owner
    USING (status IN ('SCHEDULED', 'READY', 'RETRY_WAIT', 'RESERVED', 'DISPATCHING_IN_FLIGHT'))
    WITH CHECK (status IN ('READY', 'RESERVED', 'FAILED', 'UNCERTAIN'));

-- ============================================================================
-- 5. Complete DML Guard Trigger: BEFORE INSERT OR UPDATE OR DELETE (Fixes S3, M5, M6, M7, M8)
-- ============================================================================
CREATE OR REPLACE FUNCTION app.trg_fn_enforce_delivery_state_machine()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_caller TEXT := CURRENT_USER;
    v_is_super BOOLEAN;
    v_supplied_epoch BIGINT;
    v_now TIMESTAMPTZ := pg_catalog.clock_timestamp();
BEGIN
    -- 1. Controlled Migration Bypass
    IF v_caller = 'relayforge_migrator' AND pg_catalog.current_setting('app.migration_mode', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;

    -- 2. Audited Break-Glass Superuser Escape Hatch (Fixes Minor #2)
    SELECT r.rolsuper INTO v_is_super FROM pg_catalog.pg_roles r WHERE r.rolname = v_caller;
    IF COALESCE(v_is_super, false) AND COALESCE(pg_catalog.current_setting('app.break_glass_reason', true), '') <> '' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;

    -- 3. Block all direct DELETEs (Fixes S3)
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'DELIVERY_TASK_DELETE_FORBIDDEN' USING ERRCODE = 'RF010';
    END IF;

    -- 4. Guard INSERT invariants (Fixes S3 & M7: No forged SENT/RESERVED/epoch on insert; initial max_attempts <= 10)
    IF TG_OP = 'INSERT' THEN
        IF v_caller NOT IN ('relayforge_web', 'relayforge_worker') THEN
            RAISE EXCEPTION 'UNAUTHORIZED_INSERT_ROLE: %', v_caller USING ERRCODE = 'RF020';
        END IF;
        IF NEW.status NOT IN ('PENDING', 'SCHEDULED', 'READY')
           OR NEW.lease_epoch <> 0
           OR NEW.attempts <> 0
           OR NEW.max_attempts > 10
           OR NEW.next_retry_at IS NOT NULL
           OR NEW.lease_owner IS NOT NULL
           OR NEW.lease_expires_at IS NOT NULL
           OR NEW.remote_message_id IS NOT NULL THEN
            RAISE EXCEPTION 'ILLEGAL_INITIAL_DELIVERY_TASK_STATE' USING ERRCODE = 'RF010';
        END IF;
        RETURN NEW;
    END IF;

    -- 5. Guard UPDATE identity immutability
    IF NEW.id <> OLD.id
       OR NEW.tenant_id <> OLD.tenant_id
       OR NEW.workspace_id <> OLD.workspace_id
       OR NEW.route_id IS DISTINCT FROM OLD.route_id
       OR NEW.idempotency_key <> OLD.idempotency_key THEN
        RAISE EXCEPTION 'IMMUTABLE_IDENTITY_COLUMNS' USING ERRCODE = 'RF010';
    END IF;

    -- 6. Dispatcher Owner (Strict verification of due time, expiry, attempts+1, and epoch+1 — Fixes M6)
    IF v_caller = 'relayforge_dispatcher_owner' THEN
        IF NEW.lease_epoch <> OLD.lease_epoch + 1 THEN
            RAISE EXCEPTION 'DISPATCHER_MUST_INCREMENT_EPOCH' USING ERRCODE = 'RF011';
        END IF;

        -- Path A: Claiming due runnable task -> RESERVED
        IF OLD.status IN ('SCHEDULED', 'READY', 'RETRY_WAIT') AND NEW.status = 'RESERVED' THEN
            IF COALESCE(OLD.next_retry_at, OLD.scheduled_for) > v_now
               OR OLD.attempts >= OLD.max_attempts
               OR NEW.attempts <> OLD.attempts + 1
               OR NEW.next_retry_at IS NOT NULL
               OR NEW.lease_owner IS NULL
               OR NEW.lease_expires_at IS NULL THEN
                RAISE EXCEPTION 'DISPATCHER_CLAIM_INVARIANT_VIOLATION' USING ERRCODE = 'RF012';
            END IF;
            RETURN NEW;
        END IF;

        -- Path B: Reaping expired RESERVED or DISPATCHING_IN_FLIGHT lease
        IF ((OLD.status = 'RESERVED' AND NEW.status IN ('READY', 'FAILED'))
            OR (OLD.status = 'DISPATCHING_IN_FLIGHT' AND NEW.status = 'UNCERTAIN')) THEN
            IF OLD.lease_expires_at IS NULL OR OLD.lease_expires_at >= v_now
               OR NEW.attempts <> OLD.attempts
               OR (OLD.status = 'RESERVED' AND NEW.status = 'READY' AND OLD.attempts >= OLD.max_attempts)
               OR (OLD.status = 'RESERVED' AND NEW.status = 'FAILED' AND OLD.attempts < OLD.max_attempts)
               OR NEW.lease_owner IS NOT NULL
               OR NEW.lease_expires_at IS NOT NULL THEN
                RAISE EXCEPTION 'DISPATCHER_REAP_INVARIANT_VIOLATION' USING ERRCODE = 'RF012';
            END IF;
            RETURN NEW;
        END IF;

        RAISE EXCEPTION 'ILLEGAL_DISPATCHER_TRANSITION: % -> %', OLD.status, NEW.status USING ERRCODE = 'RF012';
    END IF;

    -- 7. Worker Role (Fenced transitions + Lease Heartbeat renewal — Fixes M5)
    IF v_caller = 'relayforge_worker' THEN
        IF NEW.lease_epoch <> OLD.lease_epoch OR NEW.attempts <> OLD.attempts OR NEW.max_attempts <> OLD.max_attempts THEN
            RAISE EXCEPTION 'WORKER_CANNOT_MUTATE_EPOCH_OR_ATTEMPTS' USING ERRCODE = 'RF013';
        END IF;

        IF OLD.status = 'PENDING' AND NEW.status IN ('SCHEDULED', 'READY', 'SKIPPED')
           AND NEW.lease_owner IS NULL AND NEW.lease_expires_at IS NULL
           AND NEW.remote_message_id IS NULL THEN
            RETURN NEW;
        END IF;

        IF OLD.status IN ('RESERVED', 'DISPATCHING_IN_FLIGHT') THEN
            v_supplied_epoch := NULLIF(pg_catalog.current_setting('app.lease_epoch', true), '')::bigint;
            IF v_supplied_epoch IS NULL OR v_supplied_epoch <> OLD.lease_epoch THEN
                RAISE EXCEPTION 'MISSING_OR_STALE_FENCING_EPOCH' USING ERRCODE = 'RF014';
            END IF;
            IF OLD.lease_expires_at IS NULL OR OLD.lease_expires_at < v_now THEN
                RAISE EXCEPTION 'WORKER_LEASE_EXPIRED' USING ERRCODE = 'RF015';
            END IF;

            -- Heartbeat lease renewal (same status, extended lease_expires_at — Fixes M5)
            IF NEW.status = OLD.status AND NEW.lease_owner = OLD.lease_owner
               AND NEW.lease_expires_at IS NOT NULL AND NEW.lease_expires_at > v_now THEN
                RETURN NEW;
            END IF;

            IF NEW.status = 'RETRY_WAIT' THEN
                IF OLD.attempts >= OLD.max_attempts THEN
                    RAISE EXCEPTION 'MAX_ATTEMPTS_EXCEEDED_MUST_FAIL' USING ERRCODE = 'RF016';
                END IF;
                IF NEW.next_retry_at IS NULL THEN
                    RAISE EXCEPTION 'RETRY_WAIT_REQUIRES_NEXT_RETRY_AT' USING ERRCODE = 'RF017';
                END IF;
            END IF;

            IF (OLD.status = 'RESERVED' AND NEW.status IN ('DISPATCHING_IN_FLIGHT', 'RETRY_WAIT', 'FAILED'))
               OR (OLD.status = 'DISPATCHING_IN_FLIGHT' AND NEW.status IN ('SENT', 'RETRY_WAIT', 'FAILED', 'UNCERTAIN')) THEN
                RETURN NEW;
            END IF;
        END IF;

        RAISE EXCEPTION 'ILLEGAL_WORKER_TRANSITION: % -> %', OLD.status, NEW.status USING ERRCODE = 'RF017';
    END IF;

    -- 8. Web Role (Non-status edits on waiting tasks, PENDING promotion, Cancel, and Audited UNCERTAIN exit — Fixes M5, M6, M7)
    IF v_caller = 'relayforge_web' THEN
        -- Allow non-status metadata/schedule edits on unreserved waiting tasks (Fixes M5)
        IF OLD.status IN ('PENDING', 'SCHEDULED', 'READY') AND NEW.status = OLD.status
           AND NEW.lease_epoch = OLD.lease_epoch AND NEW.attempts = OLD.attempts
           AND NEW.max_attempts <= 10
           AND NEW.lease_owner IS NULL AND NEW.lease_expires_at IS NULL
           AND NEW.remote_message_id IS NULL THEN
            RETURN NEW;
        END IF;

        -- Allow PENDING -> SCHEDULED/READY/SKIPPED (Fixes M6)
        IF OLD.status = 'PENDING' AND NEW.status IN ('SCHEDULED', 'READY', 'SKIPPED')
           AND NEW.lease_epoch = OLD.lease_epoch AND NEW.attempts = OLD.attempts
           AND NEW.max_attempts <= 10
           AND NEW.lease_owner IS NULL AND NEW.lease_expires_at IS NULL
           AND NEW.remote_message_id IS NULL THEN
            RETURN NEW;
        END IF;

        -- Allow cancellation of waiting tasks
        IF OLD.status IN ('PENDING', 'SCHEDULED', 'READY', 'RETRY_WAIT') AND NEW.status = 'CANCELLED'
           AND NEW.lease_epoch = OLD.lease_epoch + 1 AND NEW.attempts = OLD.attempts
           AND NEW.max_attempts = OLD.max_attempts
           AND NEW.remote_message_id IS NOT DISTINCT FROM OLD.remote_message_id THEN
            RETURN NEW;
        END IF;

        -- Audited reconciliation exit from UNCERTAIN (Fixes M7: allows max_attempts = attempts + 1 on requeue)
        IF OLD.status = 'UNCERTAIN' AND NEW.status IN ('SENT', 'READY', 'CANCELLED')
           AND NEW.lease_epoch = OLD.lease_epoch + 1 AND NEW.attempts = OLD.attempts
           AND NEW.max_attempts = (
               CASE WHEN NEW.status = 'READY' AND OLD.attempts >= OLD.max_attempts
                    THEN OLD.attempts + 1
                    ELSE OLD.max_attempts
               END
           ) THEN
            IF NOT EXISTS (
                SELECT 1 FROM app.reconciliation_records rr
                WHERE rr.delivery_task_id = OLD.id
                  AND rr.task_lease_epoch = OLD.lease_epoch
                  AND (
                      (rr.resolution_action = 'CONFIRM_SENT' AND NEW.status = 'SENT')
                      OR (rr.resolution_action = 'CONFIRM_NOT_SENT_REQUEUE' AND NEW.status = 'READY' AND rr.acknowledge_duplicate_risk = true)
                      OR (rr.resolution_action = 'DISCARD_DO_NOT_SEND' AND NEW.status = 'CANCELLED')
                  )
            ) THEN
                RAISE EXCEPTION 'UNCERTAIN_EXIT_REQUIRES_MATCHING_RECONCILIATION_RECORD' USING ERRCODE = 'RF018';
            END IF;
            RETURN NEW;
        END IF;

        RAISE EXCEPTION 'ILLEGAL_WEB_TRANSITION: % -> %', OLD.status, NEW.status USING ERRCODE = 'RF019';
    END IF;

    RAISE EXCEPTION 'UNAUTHORIZED_ROLE_FOR_DELIVERY_UPDATE: %', v_caller USING ERRCODE = 'RF020';
END;
$$;

CREATE TRIGGER trg_enforce_delivery_state_machine
BEFORE INSERT OR UPDATE OR DELETE ON app.delivery_tasks
FOR EACH ROW EXECUTE FUNCTION app.trg_fn_enforce_delivery_state_machine();

-- Budget Policy Origin Guard Trigger (v1.3.2 OC-01: Blocks relayforge_web from mutating OPERATOR_PLAN ceilings!)
CREATE OR REPLACE FUNCTION app.trg_fn_enforce_budget_policy_origin()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_caller TEXT := CURRENT_USER;
    v_is_super BOOLEAN;
BEGIN
    IF v_caller = 'relayforge_migrator' AND pg_catalog.current_setting('app.migration_mode', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;

    SELECT r.rolsuper INTO v_is_super FROM pg_catalog.pg_roles r WHERE r.rolname = v_caller;
    IF COALESCE(v_is_super, false) AND COALESCE(pg_catalog.current_setting('app.break_glass_reason', true), '') <> '' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;

    -- Lock acquisition via FOR UPDATE inside fn_budget_reserve / fn_budget_settle does not fire BEFORE UPDATE triggers,
    -- only actual DML writes fire this trigger!
    IF v_caller = 'relayforge_operator' THEN
        IF (TG_OP IN ('UPDATE', 'DELETE') AND OLD.policy_origin <> 'OPERATOR_PLAN')
           OR (TG_OP IN ('INSERT', 'UPDATE') AND NEW.policy_origin <> 'OPERATOR_PLAN') THEN
            RAISE EXCEPTION 'OPERATOR_ROLE_ONLY_MANAGES_OPERATOR_PLAN' USING ERRCODE = 'RF007';
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;

    IF v_caller = 'relayforge_web' THEN
        IF (TG_OP IN ('UPDATE', 'DELETE') AND OLD.policy_origin = 'OPERATOR_PLAN')
           OR (TG_OP IN ('INSERT', 'UPDATE') AND NEW.policy_origin = 'OPERATOR_PLAN') THEN
            RAISE EXCEPTION 'TENANT_WEB_CANNOT_MUTATE_OPERATOR_PLAN_QUOTA' USING ERRCODE = 'RF007';
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;

    RAISE EXCEPTION 'UNAUTHORIZED_BUDGET_POLICY_DML_ROLE: %', v_caller USING ERRCODE = 'RF007';
END;
$$;

CREATE TRIGGER trg_enforce_budget_policy_origin
BEFORE INSERT OR UPDATE OR DELETE ON app.budget_policies
FOR EACH ROW EXECUTE FUNCTION app.trg_fn_enforce_budget_policy_origin();

RESET ROLE;

-- ============================================================================
-- 6. Functions: Water-Filling Fair Claim, Reaper, Heartbeat, Transition, Reconcile & Budget
-- ============================================================================

-- Water-Filling Per-Tenant Index-Order SKIP LOCKED Claim (Fixes M10 + preserves 100+100 concurrency!)
--   * Never scans full tenant backlog: walks idx_delivery_fair_claim (tenant_id, runnable_at ASC) up to LIMIT v_share.
--   * Never over-locks unclaimed rows: every row locked by FOR UPDATE SKIP LOCKED is claimed in the same statement.
--   * Concurrent sessions on 200 tasks with batch_size=100 claim 100 + 100 with zero overlap.
--   * Updates tenant_dispatch_fairness in deterministic tenant_id ASC order (zero cross-tenant deadlock).
CREATE OR REPLACE FUNCTION app.fn_worker_claim_next_jobs(
    p_worker_id TEXT,
    p_batch_size INT,
    p_lease_ttl_sec INT
)
RETURNS TABLE (claimed_task_id UUID, claimed_tenant_id UUID, claimed_epoch BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_limit INT;
    v_rem INT;
    v_ttl INTERVAL;
    v_now TIMESTAMPTZ := pg_catalog.clock_timestamp();
    v_tenants UUID[];
    v_next_tenants UUID[];
    v_exhausted UUID[] := ARRAY[]::UUID[];
    v_touched_tenants UUID[] := ARRAY[]::UUID[];
    v_tid UUID;
    v_idx INT;
    v_rem_tenants INT;
    v_share INT;
    v_got INT;
BEGIN
    IF p_worker_id IS NULL OR p_batch_size IS NULL OR p_lease_ttl_sec IS NULL THEN
        RAISE EXCEPTION 'NULL_ARGUMENT_FORBIDDEN' USING ERRCODE = '22004';
    END IF;

    v_limit := LEAST(GREATEST(p_batch_size, 1), 100);
    v_rem := v_limit;
    v_ttl := LEAST(GREATEST(p_lease_ttl_sec, 15), 600) * INTERVAL '1 second';

    WHILE v_rem > 0 LOOP
        SELECT ARRAY(
            SELECT t.id
            FROM app.tenants t
            LEFT JOIN app.tenant_dispatch_fairness f ON f.tenant_id = t.id
            WHERE NOT (t.id = ANY(v_exhausted))
              AND EXISTS (
                  SELECT 1 FROM app.delivery_tasks dt
                  WHERE dt.tenant_id = t.id
                    AND dt.status IN ('READY', 'SCHEDULED', 'RETRY_WAIT')
                    AND dt.runnable_at <= v_now
                    AND dt.attempts < dt.max_attempts
              )
            ORDER BY COALESCE(f.last_served_at, '-infinity'::timestamptz) ASC, t.id ASC
            LIMIT v_rem
        ) INTO v_tenants;

        IF COALESCE(pg_catalog.cardinality(v_tenants), 0) = 0 THEN
            EXIT;
        END IF;

        WHILE v_rem > 0 AND COALESCE(pg_catalog.cardinality(v_tenants), 0) > 0 LOOP
            v_next_tenants := ARRAY[]::UUID[];
            v_rem_tenants := pg_catalog.cardinality(v_tenants);

            FOR v_idx IN 1..v_rem_tenants LOOP
                EXIT WHEN v_rem <= 0;
                v_tid := v_tenants[v_idx];
                v_share := GREATEST((v_rem + (v_rem_tenants - v_idx)) / (v_rem_tenants - v_idx + 1), 1);

                RETURN QUERY
                WITH locked AS (
                    SELECT dt.id
                    FROM app.delivery_tasks dt
                    WHERE dt.tenant_id = v_tid
                      AND dt.status IN ('READY', 'SCHEDULED', 'RETRY_WAIT')
                      AND dt.runnable_at <= v_now
                      AND dt.attempts < dt.max_attempts
                    ORDER BY dt.runnable_at ASC
                    LIMIT v_share
                    FOR UPDATE OF dt SKIP LOCKED
                ),
                upd AS (
                    UPDATE app.delivery_tasks t
                    SET status = 'RESERVED',
                        next_retry_at = NULL,
                        lease_owner = p_worker_id,
                        lease_expires_at = v_now + v_ttl,
                        lease_epoch = t.lease_epoch + 1,
                        attempts = t.attempts + 1,
                        updated_at = v_now
                    FROM locked l
                    WHERE t.id = l.id
                    RETURNING t.id, t.tenant_id, t.lease_epoch
                )
                SELECT u.id, u.tenant_id, u.lease_epoch FROM upd u;

                GET DIAGNOSTICS v_got = ROW_COUNT;

                IF v_got > 0 THEN
                    v_rem := v_rem - v_got;
                    v_touched_tenants := pg_catalog.array_append(v_touched_tenants, v_tid);
                END IF;

                IF v_got = v_share AND v_rem > 0 THEN
                    v_next_tenants := pg_catalog.array_append(v_next_tenants, v_tid);
                ELSE
                    v_exhausted := pg_catalog.array_append(v_exhausted, v_tid);
                END IF;
            END LOOP;

            v_tenants := v_next_tenants;
        END LOOP;
    END LOOP;

    IF COALESCE(pg_catalog.cardinality(v_touched_tenants), 0) > 0 THEN
        INSERT INTO app.tenant_dispatch_fairness (tenant_id, last_served_at)
        SELECT d.tid, v_now
        FROM (
            SELECT DISTINCT u.tid
            FROM pg_catalog.unnest(v_touched_tenants) AS u(tid)
            ORDER BY u.tid ASC
        ) d
        ON CONFLICT (tenant_id) DO UPDATE
            SET last_served_at = EXCLUDED.last_served_at
            WHERE app.tenant_dispatch_fairness.last_served_at < v_now - INTERVAL '100 milliseconds';
    END IF;
END;
$$;

ALTER FUNCTION app.fn_worker_claim_next_jobs(TEXT, INT, INT) OWNER TO relayforge_dispatcher_owner;
REVOKE ALL ON FUNCTION app.fn_worker_claim_next_jobs(TEXT, INT, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_worker_claim_next_jobs(TEXT, INT, INT) TO relayforge_worker;

CREATE OR REPLACE FUNCTION app.fn_reap_expired_delivery_leases(p_batch_size INT)
RETURNS INT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_limit INT;
    v_now TIMESTAMPTZ := pg_catalog.clock_timestamp();
    v_count INT;
BEGIN
    IF p_batch_size IS NULL THEN
        RAISE EXCEPTION 'NULL_ARGUMENT_FORBIDDEN' USING ERRCODE = '22004';
    END IF;
    v_limit := LEAST(GREATEST(p_batch_size, 1), 200);

    WITH expired AS (
        SELECT id
        FROM app.delivery_tasks
        WHERE status IN ('RESERVED', 'DISPATCHING_IN_FLIGHT')
          AND lease_expires_at < v_now
        ORDER BY lease_expires_at ASC
        LIMIT v_limit
        FOR UPDATE SKIP LOCKED
    ),
    updated AS (
        UPDATE app.delivery_tasks t
        SET status = CASE
                WHEN t.status = 'DISPATCHING_IN_FLIGHT' THEN 'UNCERTAIN'
                WHEN t.attempts >= t.max_attempts THEN 'FAILED'
                ELSE 'READY'
            END,
            lease_owner = NULL,
            lease_expires_at = NULL,
            lease_epoch = t.lease_epoch + 1,
            updated_at = v_now
        FROM expired e
        WHERE t.id = e.id
        RETURNING t.id
    )
    SELECT count(*)::int INTO v_count FROM updated;
    RETURN v_count;
END;
$$;

ALTER FUNCTION app.fn_reap_expired_delivery_leases(INT) OWNER TO relayforge_dispatcher_owner;
REVOKE ALL ON FUNCTION app.fn_reap_expired_delivery_leases(INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_reap_expired_delivery_leases(INT) TO relayforge_worker;

-- Worker Heartbeat Lease Renewal (Fixes M5: prevents false UNCERTAIN on large media uploads)
CREATE OR REPLACE FUNCTION app.fn_worker_heartbeat_lease(
    p_task_id UUID,
    p_expected_epoch BIGINT,
    p_extend_sec INT
)
RETURNS TIMESTAMPTZ
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_new_exp TIMESTAMPTZ;
BEGIN
    IF p_task_id IS NULL OR p_expected_epoch IS NULL OR p_extend_sec IS NULL THEN
        RAISE EXCEPTION 'NULL_ARGUMENT_FORBIDDEN' USING ERRCODE = '22004';
    END IF;

    PERFORM pg_catalog.set_config('app.lease_epoch', p_expected_epoch::text, true);

    UPDATE app.delivery_tasks
    SET lease_expires_at = pg_catalog.clock_timestamp() + (LEAST(GREATEST(p_extend_sec, 15), 600) * INTERVAL '1 second'),
        updated_at = pg_catalog.clock_timestamp()
    WHERE id = p_task_id
      AND lease_epoch = p_expected_epoch
      AND status IN ('RESERVED', 'DISPATCHING_IN_FLIGHT')
    RETURNING lease_expires_at INTO v_new_exp;

    IF v_new_exp IS NULL THEN
        RAISE EXCEPTION 'HEARTBEAT_FENCING_OR_TASK_NOT_ACTIVE' USING ERRCODE = 'RF014';
    END IF;
    RETURN v_new_exp;
END;
$$;

ALTER FUNCTION app.fn_worker_heartbeat_lease(UUID, BIGINT, INT) OWNER TO relayforge_migrator;
REVOKE ALL ON FUNCTION app.fn_worker_heartbeat_lease(UUID, BIGINT, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_worker_heartbeat_lease(UUID, BIGINT, INT) TO relayforge_worker;

CREATE OR REPLACE FUNCTION app.fn_worker_transition_task(
    p_task_id UUID,
    p_expected_epoch BIGINT,
    p_new_status TEXT,
    p_remote_message_id TEXT DEFAULT NULL,
    p_next_retry_at TIMESTAMPTZ DEFAULT NULL
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_rows INT;
BEGIN
    IF p_task_id IS NULL OR p_expected_epoch IS NULL OR p_new_status IS NULL THEN
        RAISE EXCEPTION 'NULL_ARGUMENT_FORBIDDEN' USING ERRCODE = '22004';
    END IF;

    PERFORM pg_catalog.set_config('app.lease_epoch', p_expected_epoch::text, true);

    UPDATE app.delivery_tasks
    SET status = p_new_status,
        remote_message_id = COALESCE(p_remote_message_id, remote_message_id),
        next_retry_at = CASE WHEN p_new_status = 'RETRY_WAIT' THEN p_next_retry_at ELSE NULL END,
        lease_owner = CASE WHEN p_new_status IN ('SENT', 'RETRY_WAIT', 'FAILED', 'UNCERTAIN') THEN NULL ELSE lease_owner END,
        lease_expires_at = CASE WHEN p_new_status IN ('SENT', 'RETRY_WAIT', 'FAILED', 'UNCERTAIN') THEN NULL ELSE lease_expires_at END,
        updated_at = pg_catalog.clock_timestamp()
    WHERE id = p_task_id AND lease_epoch = p_expected_epoch;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows <> 1 THEN
        RAISE EXCEPTION 'FENCING_OR_TASK_NOT_FOUND' USING ERRCODE = 'RF014';
    END IF;
END;
$$;

ALTER FUNCTION app.fn_worker_transition_task(UUID, BIGINT, TEXT, TEXT, TIMESTAMPTZ) OWNER TO relayforge_migrator;
REVOKE ALL ON FUNCTION app.fn_worker_transition_task(UUID, BIGINT, TEXT, TEXT, TIMESTAMPTZ) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_worker_transition_task(UUID, BIGINT, TEXT, TEXT, TIMESTAMPTZ) TO relayforge_worker;

CREATE OR REPLACE FUNCTION app.fn_web_reconcile_uncertain_task(
    p_reconciliation_id UUID,
    p_task_id UUID,
    p_expected_epoch BIGINT,
    p_action TEXT,
    p_ack_duplicate BOOLEAN,
    p_decided_by TEXT,
    p_reason TEXT
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_tenant_id UUID := NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid;
    v_target_status TEXT;
    v_rows INT;
BEGIN
    IF p_reconciliation_id IS NULL OR p_task_id IS NULL OR p_expected_epoch IS NULL
       OR p_action IS NULL OR p_ack_duplicate IS NULL OR p_decided_by IS NULL OR p_reason IS NULL THEN
        RAISE EXCEPTION 'NULL_ARGUMENT_FORBIDDEN' USING ERRCODE = '22004';
    END IF;
    IF v_tenant_id IS NULL THEN
        RAISE EXCEPTION 'MISSING_TENANT_CONTEXT' USING ERRCODE = 'RF003';
    END IF;

    v_target_status := CASE p_action
        WHEN 'CONFIRM_SENT' THEN 'SENT'
        WHEN 'CONFIRM_NOT_SENT_REQUEUE' THEN 'READY'
        WHEN 'DISCARD_DO_NOT_SEND' THEN 'CANCELLED'
        ELSE NULL
    END;
    IF v_target_status IS NULL THEN
        RAISE EXCEPTION 'INVALID_RECONCILIATION_ACTION' USING ERRCODE = '22023';
    END IF;

    INSERT INTO app.reconciliation_records (
        id, tenant_id, delivery_task_id, task_lease_epoch,
        resolution_action, acknowledge_duplicate_risk, decided_by, reason
    ) VALUES (
        p_reconciliation_id, v_tenant_id, p_task_id, p_expected_epoch,
        p_action, p_ack_duplicate, p_decided_by, p_reason
    );

    UPDATE app.delivery_tasks
    SET status = v_target_status,
        max_attempts = CASE WHEN v_target_status = 'READY' AND attempts >= max_attempts THEN attempts + 1 ELSE max_attempts END,
        lease_epoch = lease_epoch + 1,
        updated_at = pg_catalog.clock_timestamp()
    WHERE id = p_task_id AND status = 'UNCERTAIN' AND lease_epoch = p_expected_epoch;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows <> 1 THEN
        RAISE EXCEPTION 'UNCERTAIN_TASK_OR_EPOCH_MISMATCH' USING ERRCODE = 'RF018';
    END IF;
END;
$$;

ALTER FUNCTION app.fn_web_reconcile_uncertain_task(UUID, UUID, BIGINT, TEXT, BOOLEAN, TEXT, TEXT) OWNER TO relayforge_migrator;
REVOKE ALL ON FUNCTION app.fn_web_reconcile_uncertain_task(UUID, UUID, BIGINT, TEXT, BOOLEAN, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_web_reconcile_uncertain_task(UUID, UUID, BIGINT, TEXT, BOOLEAN, TEXT, TEXT) TO relayforge_web;

-- Server-Derived Scoped Atomic Budget Reserve (Fixes S4, M8, M9; Fail-Closed on missing policy)
-- Owned by relayforge_ledger_owner (NOBYPASSRLS + subject to app.tenant_id FORCE RLS!)
CREATE OR REPLACE FUNCTION app.fn_budget_reserve(
    p_reservation_id UUID,
    p_workspace_id UUID,
    p_route_id UUID,
    p_idempotency_key TEXT,
    p_amount_micros BIGINT,
    p_ttl_sec INT
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_tenant_id UUID;
    v_existing RECORD;
    v_pol RECORD;
    v_locked_plan_policies INT := 0;
    v_bucket TEXT;
    v_used BIGINT;
    v_now TIMESTAMPTZ := pg_catalog.clock_timestamp();
BEGIN
    IF p_reservation_id IS NULL OR p_workspace_id IS NULL OR p_idempotency_key IS NULL
       OR p_amount_micros IS NULL OR p_ttl_sec IS NULL THEN
        RAISE EXCEPTION 'NULL_ARGUMENT_FORBIDDEN' USING ERRCODE = '22004';
    END IF;

    IF p_amount_micros < 0 THEN
        RAISE EXCEPTION 'NEGATIVE_AMOUNT_FORBIDDEN' USING ERRCODE = '22023';
    END IF;

    v_tenant_id := NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid;
    IF v_tenant_id IS NULL THEN
        RAISE EXCEPTION 'MISSING_TENANT_CONTEXT' USING ERRCODE = 'RF003';
    END IF;

    -- Verify workspace (and optional route) exist within current tenant RLS boundary
    IF NOT EXISTS (SELECT 1 FROM app.workspaces w WHERE w.id = p_workspace_id) THEN
        RAISE EXCEPTION 'INVALID_OR_UNAUTHORIZED_WORKSPACE' USING ERRCODE = 'RF002';
    END IF;

    IF p_route_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM app.routes r WHERE r.id = p_route_id AND r.workspace_id = p_workspace_id
    ) THEN
        RAISE EXCEPTION 'INVALID_OR_UNAUTHORIZED_ROUTE' USING ERRCODE = 'RF002';
    END IF;

    -- 1. Server derives & locks ALL active policies (mandatory OPERATOR_PLAN + optional TENANT_BUDGET sub-caps) in deterministic ID order!
    FOR v_pol IN
        SELECT bp.id, bp.policy_origin
        FROM app.budget_policies bp
        WHERE bp.is_active = true
          AND (
              (bp.scope_type = 'TENANT' AND bp.scope_ref_id = v_tenant_id)
              OR (bp.scope_type = 'WORKSPACE' AND bp.scope_ref_id = p_workspace_id)
              OR (p_route_id IS NOT NULL AND bp.scope_type = 'ROUTE' AND bp.scope_ref_id = p_route_id)
          )
        ORDER BY bp.id ASC
        FOR UPDATE
    LOOP
        IF v_pol.policy_origin = 'OPERATOR_PLAN' THEN
            v_locked_plan_policies := v_locked_plan_policies + 1;
        END IF;
    END LOOP;

    -- Fail-Closed (v1.3.2 OC-01): Every tenant MUST have at least one active OPERATOR_PLAN quota; TENANT_BUDGET alone is NOT sufficient!
    IF v_locked_plan_policies = 0 THEN
        RAISE EXCEPTION 'NO_ACTIVE_OPERATOR_PLAN_QUOTA_FOR_TENANT' USING ERRCODE = 'RF006';
    END IF;

    -- 2. Idempotent replay check with strict parameter verification (Fixes M9a)
    SELECT br.id, br.workspace_id, br.route_id, br.reserved_micros
    INTO v_existing
    FROM app.budget_reservations br
    WHERE br.tenant_id = v_tenant_id AND br.idempotency_key = p_idempotency_key;

    IF v_existing.id IS NOT NULL THEN
        IF v_existing.workspace_id <> p_workspace_id
           OR v_existing.route_id IS DISTINCT FROM p_route_id
           OR v_existing.reserved_micros <> p_amount_micros THEN
            RAISE EXCEPTION 'IDEMPOTENCY_PARAMETER_MISMATCH' USING ERRCODE = 'RF005';
        END IF;
        RETURN v_existing.id;
    END IF;

    -- 3. Evaluate ceiling on EVERY derived active policy within its UTC period_bucket
    FOR v_pol IN
        SELECT bp.id, bp.period_kind, bp.max_cost_micros
        FROM app.budget_policies bp
        WHERE bp.is_active = true
          AND (
              (bp.scope_type = 'TENANT' AND bp.scope_ref_id = v_tenant_id)
              OR (bp.scope_type = 'WORKSPACE' AND bp.scope_ref_id = p_workspace_id)
              OR (p_route_id IS NOT NULL AND bp.scope_type = 'ROUTE' AND bp.scope_ref_id = p_route_id)
          )
        ORDER BY bp.id ASC
    LOOP
        v_bucket := CASE v_pol.period_kind
            WHEN 'DAILY' THEN pg_catalog.to_char(v_now AT TIME ZONE 'UTC', 'YYYY-MM-DD')
            WHEN 'MONTHLY' THEN pg_catalog.to_char(v_now AT TIME ZONE 'UTC', 'YYYY-MM')
            ELSE 'TOTAL'
        END;

        SELECT COALESCE(SUM(
            CASE
                WHEN br.state = 'COMMITTED' THEN COALESCE(br.settled_micros, br.reserved_micros)
                WHEN br.state = 'RESERVED' AND br.expires_at > v_now THEN br.reserved_micros
                ELSE 0
            END
        ), 0) INTO v_used
        FROM app.budget_reservation_allocations bra
        JOIN app.budget_reservations br
          ON br.tenant_id = bra.tenant_id AND br.id = bra.reservation_id
        WHERE bra.policy_id = v_pol.id AND bra.period_bucket = v_bucket;

        IF (v_used + p_amount_micros) > v_pol.max_cost_micros THEN
            RAISE EXCEPTION 'BUDGET_EXCEEDED on policy % bucket % (used=%, req=%, max=%)',
                v_pol.id, v_bucket, v_used, p_amount_micros, v_pol.max_cost_micros
                USING ERRCODE = 'RF001';
        END IF;
    END LOOP;

    -- 4. Insert reservation + allocation row for EVERY derived active policy
    INSERT INTO app.budget_reservations (
        id, tenant_id, workspace_id, route_id, idempotency_key,
        state, reserved_micros, expires_at, created_at
    ) VALUES (
        p_reservation_id, v_tenant_id, p_workspace_id, p_route_id, p_idempotency_key,
        'RESERVED', p_amount_micros,
        v_now + (LEAST(GREATEST(p_ttl_sec, 10), 3600) * INTERVAL '1 second'),
        v_now
    );

    INSERT INTO app.budget_reservation_allocations (reservation_id, policy_id, tenant_id, period_bucket)
    SELECT p_reservation_id,
           bp.id,
           v_tenant_id,
           CASE bp.period_kind
               WHEN 'DAILY' THEN pg_catalog.to_char(v_now AT TIME ZONE 'UTC', 'YYYY-MM-DD')
               WHEN 'MONTHLY' THEN pg_catalog.to_char(v_now AT TIME ZONE 'UTC', 'YYYY-MM')
               ELSE 'TOTAL'
           END
    FROM app.budget_policies bp
    WHERE bp.is_active = true
      AND (
          (bp.scope_type = 'TENANT' AND bp.scope_ref_id = v_tenant_id)
          OR (bp.scope_type = 'WORKSPACE' AND bp.scope_ref_id = p_workspace_id)
          OR (p_route_id IS NOT NULL AND bp.scope_type = 'ROUTE' AND bp.scope_ref_id = p_route_id)
      );

    RETURN p_reservation_id;
END;
$$;

ALTER FUNCTION app.fn_budget_reserve(UUID, UUID, UUID, TEXT, BIGINT, INT) OWNER TO relayforge_ledger_owner;
REVOKE ALL ON FUNCTION app.fn_budget_reserve(UUID, UUID, UUID, TEXT, BIGINT, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_budget_reserve(UUID, UUID, UUID, TEXT, BIGINT, INT) TO relayforge_web, relayforge_worker;

-- Budget Settlement with Expired/Overage Ceiling Re-Check (Fixes S4b & M9b)
CREATE OR REPLACE FUNCTION app.fn_budget_settle(
    p_reservation_id UUID,
    p_final_state TEXT,
    p_settled_micros BIGINT DEFAULT NULL
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_tenant_id UUID := NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid;
    v_res RECORD;
    v_alloc RECORD;
    v_other_used BIGINT;
    v_now TIMESTAMPTZ := pg_catalog.clock_timestamp();
BEGIN
    IF p_reservation_id IS NULL OR p_final_state IS NULL THEN
        RAISE EXCEPTION 'NULL_ARGUMENT_FORBIDDEN' USING ERRCODE = '22004';
    END IF;
    IF v_tenant_id IS NULL THEN
        RAISE EXCEPTION 'MISSING_TENANT_CONTEXT' USING ERRCODE = 'RF003';
    END IF;
    IF p_final_state NOT IN ('COMMITTED', 'RELEASED') THEN
        RAISE EXCEPTION 'INVALID_BUDGET_SETTLE_STATE' USING ERRCODE = '22023';
    END IF;
    IF p_final_state = 'COMMITTED' AND (p_settled_micros IS NULL OR p_settled_micros < 0) THEN
        RAISE EXCEPTION 'COMMITTED_REQUIRES_NON_NEGATIVE_SETTLED_MICROS' USING ERRCODE = '22023';
    END IF;

    -- Lock policies linked to this reservation in deterministic order
    FOR v_alloc IN
        SELECT bp.id
        FROM app.budget_reservation_allocations bra
        JOIN app.budget_policies bp ON bp.tenant_id = bra.tenant_id AND bp.id = bra.policy_id
        WHERE bra.reservation_id = p_reservation_id
        ORDER BY bp.id ASC
        FOR UPDATE OF bp
    LOOP
        NULL;
    END LOOP;

    SELECT br.id, br.state, br.reserved_micros, br.expires_at
    INTO v_res
    FROM app.budget_reservations br
    WHERE br.id = p_reservation_id
    FOR UPDATE;

    IF v_res.id IS NULL OR v_res.state <> 'RESERVED' THEN
        RAISE EXCEPTION 'RESERVATION_NOT_ACTIVE_OR_NOT_FOUND' USING ERRCODE = 'RF004';
    END IF;

    -- Fixes M9b: If hold already expired OR settled > reserved, verify ceiling is not breached!
    IF p_final_state = 'COMMITTED' AND (v_res.expires_at <= v_now OR p_settled_micros > v_res.reserved_micros) THEN
        FOR v_alloc IN
            SELECT bp.id AS policy_id, bp.max_cost_micros, bra.period_bucket
            FROM app.budget_reservation_allocations bra
            JOIN app.budget_policies bp ON bp.tenant_id = bra.tenant_id AND bp.id = bra.policy_id
            WHERE bra.reservation_id = p_reservation_id
            ORDER BY bp.id ASC
        LOOP
            SELECT COALESCE(SUM(
                CASE
                    WHEN br.state = 'COMMITTED' THEN COALESCE(br.settled_micros, br.reserved_micros)
                    WHEN br.state = 'RESERVED' AND br.expires_at > v_now THEN br.reserved_micros
                    ELSE 0
                END
            ), 0) INTO v_other_used
            FROM app.budget_reservation_allocations bra2
            JOIN app.budget_reservations br ON br.tenant_id = bra2.tenant_id AND br.id = bra2.reservation_id
            WHERE bra2.policy_id = v_alloc.policy_id
              AND bra2.period_bucket = v_alloc.period_bucket
              AND br.id <> p_reservation_id;

            IF (v_other_used + p_settled_micros) > v_alloc.max_cost_micros THEN
                RAISE EXCEPTION 'LATE_OR_OVERAGE_SETTLEMENT_EXCEEDS_CAP on policy %', v_alloc.policy_id
                    USING ERRCODE = 'RF004';
            END IF;
        END LOOP;
    END IF;

    UPDATE app.budget_reservations
    SET state = p_final_state,
        settled_micros = CASE WHEN p_final_state = 'COMMITTED' THEN p_settled_micros ELSE 0 END
    WHERE id = p_reservation_id;
END;
$$;

ALTER FUNCTION app.fn_budget_settle(UUID, TEXT, BIGINT) OWNER TO relayforge_ledger_owner;
REVOKE ALL ON FUNCTION app.fn_budget_settle(UUID, TEXT, BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_budget_settle(UUID, TEXT, BIGINT) TO relayforge_web, relayforge_worker;

-- ============================================================================
-- 7. Non-Vacuous Behavioral Verification Suite (Mutation-Proof via v_caught flags)
-- ============================================================================
DO $$
DECLARE
    v_t1 UUID := '11111111-1111-1111-1111-111111111111';
    v_t2 UUID := '22222222-2222-2222-2222-222222222222';
    v_ws1 UUID := 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
    v_ws2 UUID := 'aaaaaaaa-aaaa-aaaa-aaaa-bbbbbbbbbbbb';
    v_rt1 UUID := '99999999-9999-9999-9999-999999999901';
    v_rt2 UUID := '99999999-9999-9999-9999-999999999902';
    v_pol_tenant UUID := 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
    v_pol_ws UUID := 'cccccccc-cccc-cccc-cccc-cccccccccccc';
    v_pol_route UUID := 'cccccccc-cccc-cccc-cccc-dddddddddddd';
    v_task1 UUID := 'dddddddd-dddd-dddd-dddd-dddddddddd01';
    v_task_t2 UUID := 'dddddddd-dddd-dddd-dddd-dddddddddd09';
    v_task_reap_unc UUID := 'dddddddd-dddd-dddd-dddd-dddddddddd02';
    v_task_reap_fail UUID := 'dddddddd-dddd-dddd-dddd-dddddddddd03';
    v_task_reap_ready UUID := 'dddddddd-dddd-dddd-dddd-dddddddddd04';
    v_yesterday_bucket TEXT := to_char((clock_timestamp() - interval '1 day') AT TIME ZONE 'UTC', 'YYYY-MM-DD');
    v_claim RECORD;
    v_rows INT;
    v_caught BOOLEAN;
    v_replay_id UUID;
    v_hb_exp TIMESTAMPTZ;
BEGIN
    -- [A] Migrator Seeding, Multi-Table Backfill & Yesterday DAILY Rollover Fixture
    EXECUTE 'SET LOCAL ROLE relayforge_migrator';
    PERFORM set_config('app.migration_mode', 'on', true);

    INSERT INTO app.tenants (id, slug) VALUES (v_t1, 'tenant-a'), (v_t2, 'tenant-b');
    INSERT INTO app.workspaces (id, tenant_id, slug) VALUES (v_ws1, v_t1, 'ws-a'), (v_ws2, v_t2, 'ws-b');
    INSERT INTO app.routes (id, tenant_id, workspace_id, slug) VALUES (v_rt1, v_t1, v_ws1, 'rt-a1'), (v_rt2, v_t2, v_ws2, 'rt-b1');

    UPDATE app.workspaces SET slug = 'ws-a-migrated' WHERE id = v_ws1;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    ASSERT v_rows = 1, 'Migrator backfill failed on workspaces';

    INSERT INTO app.budget_policies (id, tenant_id, policy_origin, scope_type, scope_ref_id, period_kind, max_cost_micros)
    VALUES (v_pol_tenant, v_t1, 'OPERATOR_PLAN', 'TENANT', v_t1, 'TOTAL', 1000),
           (v_pol_ws, v_t1, 'TENANT_BUDGET', 'WORKSPACE', v_ws1, 'DAILY', 400),
           (v_pol_route, v_t1, 'TENANT_BUDGET', 'ROUTE', v_rt1, 'DAILY', 250);

    -- Seed a COMMITTED reservation of 400 in YESTERDAY's DAILY bucket on ws-a (counts toward TOTAL=1000, NOT today's DAILY=400!)
    INSERT INTO app.budget_reservations (id, tenant_id, workspace_id, route_id, idempotency_key, state, reserved_micros, settled_micros, expires_at)
    VALUES ('00000000-0000-0000-0000-0000000000aa', v_t1, v_ws1, NULL, 'idem-yesterday', 'COMMITTED', 400, 400, clock_timestamp() - interval '23 hours');
    INSERT INTO app.budget_reservation_allocations (reservation_id, policy_id, tenant_id, period_bucket)
    VALUES ('00000000-0000-0000-0000-0000000000aa', v_pol_ws, v_t1, v_yesterday_bucket),
           ('00000000-0000-0000-0000-0000000000aa', v_pol_tenant, v_t1, 'TOTAL');

    PERFORM set_config('app.migration_mode', '', true);
    RESET ROLE;

    -- [B] Cross-Tenant RLS Isolation (Empty Context, Cross-Read, Cross-Insert) & Web Task Lifecycle
    EXECUTE 'SET LOCAL ROLE relayforge_web';
    PERFORM set_config('app.tenant_id', '', true);
    ASSERT (SELECT count(*) FROM app.workspaces) = 0, 'Empty tenant_id context leaked rows!';

    PERFORM set_config('app.tenant_id', v_t2::text, true);
    ASSERT (SELECT count(*) FROM app.workspaces WHERE id = v_ws1) = 0, 'Cross-tenant leak: Tenant 2 saw Tenant 1 workspace!';

    v_caught := false;
    BEGIN
        INSERT INTO app.workspaces (id, tenant_id, slug) VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-cccccccccccc', v_t1, 'cross-spoof');
    EXCEPTION WHEN insufficient_privilege THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Cross-tenant INSERT was NOT blocked by WITH CHECK RLS!';

    INSERT INTO app.delivery_tasks (id, tenant_id, workspace_id, route_id, idempotency_key, status, scheduled_for, max_attempts)
    VALUES (v_task_t2, v_t2, v_ws2, v_rt2, 'idem-t2-1', 'READY', clock_timestamp() - interval '1 second', 4);

    PERFORM set_config('app.tenant_id', v_t1::text, true);
    INSERT INTO app.delivery_tasks (id, tenant_id, workspace_id, route_id, idempotency_key, status, scheduled_for, max_attempts)
    VALUES (v_task1, v_t1, v_ws1, v_rt1, 'idem-1', 'PENDING', clock_timestamp() - interval '2 seconds', 10);

    -- Web non-status edit on PENDING + promotion PENDING -> SCHEDULED (Fixes M5 & M6)
    UPDATE app.delivery_tasks SET payload_json = '{"edited":true}'::jsonb WHERE id = v_task1;
    UPDATE app.delivery_tasks SET status = 'SCHEDULED' WHERE id = v_task1;
    ASSERT (SELECT status FROM app.delivery_tasks WHERE id = v_task1) = 'SCHEDULED', 'Web PENDING->SCHEDULED failed';

    -- [C] Operator Plan Immutability (OC-01), Period-Bucket Rollover, Multi-Scope Caps, Idempotency & Fail-Closed
    -- 0. Tenant 2 creates a generous TENANT_BUDGET (5000), yet STILL fails closed with RF006 because it has NO OPERATOR_PLAN!
    PERFORM set_config('app.tenant_id', v_t2::text, true);
    INSERT INTO app.budget_policies (id, tenant_id, policy_origin, scope_type, scope_ref_id, period_kind, max_cost_micros)
    VALUES ('cccccccc-2222-2222-2222-222222222222', v_t2, 'TENANT_BUDGET', 'WORKSPACE', v_ws2, 'DAILY', 5000);

    v_caught := false;
    BEGIN
        PERFORM app.fn_budget_reserve('00000000-0000-0000-0000-0000000000f0', v_ws2, v_rt2, 'idem-nop', 10, 60);
    EXCEPTION WHEN SQLSTATE 'RF006' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Expected RF006 fail-closed when tenant has TENANT_BUDGET but 0 active OPERATOR_PLAN quotas!';

    PERFORM set_config('app.tenant_id', v_t1::text, true);

    -- 1. Verify relayforge_web CANNOT raise, delete, or forge OPERATOR_PLAN ceiling (Fixes High #1 / OC-01 -> RF007!)
    v_caught := false;
    BEGIN
        UPDATE app.budget_policies SET max_cost_micros = 99999999 WHERE id = v_pol_tenant;
    EXCEPTION WHEN SQLSTATE 'RF007' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'relayforge_web was able to raise OPERATOR_PLAN max_cost_micros!';

    v_caught := false;
    BEGIN
        DELETE FROM app.budget_policies WHERE id = v_pol_tenant;
    EXCEPTION WHEN SQLSTATE 'RF007' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'relayforge_web was able to DELETE OPERATOR_PLAN policy!';

    v_caught := false;
    BEGIN
        INSERT INTO app.budget_policies (id, tenant_id, policy_origin, scope_type, scope_ref_id, period_kind, max_cost_micros)
        VALUES ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb99', v_t1, 'OPERATOR_PLAN', 'TENANT', v_t1, 'DAILY', 99999999);
    EXCEPTION WHEN SQLSTATE 'RF007' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'relayforge_web was able to INSERT forged OPERATOR_PLAN policy!';

    -- 2. Cross-tenant / invalid route_id MUST raise RF002
    v_caught := false;
    BEGIN
        PERFORM app.fn_budget_reserve('00000000-0000-0000-0000-0000000000f1', v_ws1, v_rt2, 'idem-bad-rt', 10, 60);
    EXCEPTION WHEN SQLSTATE 'RF002' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Expected RF002 on cross-tenant/invalid route_id!';

    -- 3. Server-derived ROUTE cap (250) rejects 260 even though WORKSPACE (400 today) & TENANT (600 left) allow it!
    v_caught := false;
    BEGIN
        PERFORM app.fn_budget_reserve('00000000-0000-0000-0000-0000000000f2', v_ws1, v_rt1, 'idem-rt-cap', 260, 60);
    EXCEPTION WHEN SQLSTATE 'RF001' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Expected RF001 against server-derived ROUTE cap=250!';

    -- 4. Today's 300 workspace reserve SUCCEEDS despite yesterday's 400 COMMITTED in DAILY bucket (proves period_bucket rollover!)
    PERFORM app.fn_budget_reserve('00000000-0000-0000-0000-000000000001', v_ws1, NULL, 'idem-b1', 300, 10);

    -- 5. Idempotent replay with identical params returns existing ID; different amount raises RF005
    v_replay_id := app.fn_budget_reserve('00000000-0000-0000-0000-000000000099', v_ws1, NULL, 'idem-b1', 300, 10);
    ASSERT v_replay_id = '00000000-0000-0000-0000-000000000001'::uuid, 'Idempotent replay did not return original ID';

    v_caught := false;
    BEGIN
        PERFORM app.fn_budget_reserve('00000000-0000-0000-0000-000000000098', v_ws1, NULL, 'idem-b1', 100, 10);
    EXCEPTION WHEN SQLSTATE 'RF005' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Expected RF005 on mismatched idempotency replay was NOT raised!';

    -- 6. Second reserve of 300 today MUST fail with RF001 against server-derived 400 workspace DAILY cap (Non-vacuous — Fixes S2!)
    v_caught := false;
    BEGIN
        PERFORM app.fn_budget_reserve('00000000-0000-0000-0000-000000000002', v_ws1, NULL, 'idem-b2', 300, 60);
    EXCEPTION WHEN SQLSTATE 'RF001' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Expected RF001 on 300+300 > 400 workspace cap was NOT raised!';

    -- 7. NULL argument MUST raise 22004
    v_caught := false;
    BEGIN
        PERFORM app.fn_budget_reserve(NULL, v_ws1, NULL, 'idem-null', 10, 60);
    EXCEPTION WHEN SQLSTATE '22004' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Expected 22004 on NULL argument was NOT raised!';

    -- [D] Worker Privilege Lockdown Tests (Fixes S3 & S4: No forged INSERT, no DELETE, no budget tampering)
    RESET ROLE;
    EXECUTE 'SET LOCAL ROLE relayforge_worker';
    PERFORM set_config('app.tenant_id', v_t1::text, true);

    v_caught := false;
    BEGIN
        UPDATE app.budget_policies SET max_cost_micros = 999999 WHERE id = v_pol_ws;
    EXCEPTION WHEN insufficient_privilege THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Worker was able to UPDATE budget_policies!';

    v_caught := false;
    BEGIN
        UPDATE app.budget_reservations SET state = 'RELEASED' WHERE id = '00000000-0000-0000-0000-000000000001';
    EXCEPTION WHEN insufficient_privilege THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Worker was able to directly UPDATE budget_reservations!';

    v_caught := false;
    BEGIN
        INSERT INTO app.delivery_tasks (id, tenant_id, workspace_id, idempotency_key, status, scheduled_for, lease_epoch)
        VALUES ('dddddddd-dddd-dddd-dddd-dddddddddd99', v_t1, v_ws1, 'forged', 'SENT', clock_timestamp(), 99);
    EXCEPTION WHEN SQLSTATE 'RF010' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Worker was able to INSERT forged SENT/epoch=99 task!';

    v_caught := false;
    BEGIN
        DELETE FROM app.delivery_tasks WHERE id = v_task1;
    EXCEPTION WHEN insufficient_privilege OR SQLSTATE 'RF010' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Worker was able to DELETE delivery_tasks!';

    -- [E] Multi-Tenant Fair Claim, Heartbeat, Stale/Missing Epoch Fencing & Reaper (All 3 Paths: UNCERTAIN, FAILED, READY)
    SELECT count(*) INTO v_rows FROM app.fn_worker_claim_next_jobs('w1', 10, 60);
    ASSERT v_rows = 2, 'Fair claim across Tenant 1 & Tenant 2 did not claim both runnable tasks!';

    -- Heartbeat lease renewal succeeds on Tenant 1's claimed task (epoch=1)
    v_hb_exp := app.fn_worker_heartbeat_lease(v_task1, 1, 120);
    ASSERT v_hb_exp > clock_timestamp() + interval '100 seconds', 'Heartbeat did not extend lease_expires_at';

    -- Missing epoch on direct UPDATE MUST fail with RF014
    PERFORM set_config('app.lease_epoch', '', true);
    v_caught := false;
    BEGIN
        UPDATE app.delivery_tasks SET status = 'DISPATCHING_IN_FLIGHT' WHERE id = v_task1;
    EXCEPTION WHEN SQLSTATE 'RF014' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Direct worker UPDATE with empty app.lease_epoch was NOT rejected!';

    -- Stale epoch transition MUST fail with RF014
    v_caught := false;
    BEGIN
        PERFORM app.fn_worker_transition_task(v_task1, 999, 'DISPATCHING_IN_FLIGHT', NULL, NULL);
    EXCEPTION WHEN SQLSTATE 'RF014' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Stale fencing epoch was NOT rejected!';

    -- Seed 3 expired tasks via migrator to test ALL 3 Reaper paths: -> UNCERTAIN, -> FAILED, -> READY (Fixes S1!)
    RESET ROLE;
    EXECUTE 'SET LOCAL ROLE relayforge_migrator';
    PERFORM set_config('app.migration_mode', 'on', true);

    INSERT INTO app.delivery_tasks (
        id, tenant_id, workspace_id, idempotency_key, status, scheduled_for,
        lease_owner, lease_expires_at, lease_epoch, attempts, max_attempts
    ) VALUES
    (v_task_reap_unc, v_t1, v_ws1, 'reap-unc', 'DISPATCHING_IN_FLIGHT', clock_timestamp() - interval '60 seconds',
     'dead-w1', clock_timestamp() - interval '5 seconds', 1, 10, 10),
    (v_task_reap_fail, v_t1, v_ws1, 'reap-fail', 'RESERVED', clock_timestamp() - interval '60 seconds',
     'dead-w2', clock_timestamp() - interval '5 seconds', 1, 4, 4),
    (v_task_reap_ready, v_t1, v_ws1, 'reap-ready', 'RESERVED', clock_timestamp() - interval '60 seconds',
     'dead-w3', clock_timestamp() - interval '5 seconds', 1, 1, 4);

    -- Expire reservation 1 to test M9b late-settle over-cap rejection
    UPDATE app.budget_reservations SET expires_at = clock_timestamp() - interval '1 second'
    WHERE id = '00000000-0000-0000-0000-000000000001';
    PERFORM set_config('app.migration_mode', '', true);

    RESET ROLE;
    EXECUTE 'SET LOCAL ROLE relayforge_worker';
    PERFORM set_config('app.tenant_id', v_t1::text, true);

    v_rows := app.fn_reap_expired_delivery_leases(50);
    ASSERT v_rows = 3, 'Reaper did not reap expected 3 expired tasks';
    ASSERT (SELECT status FROM app.delivery_tasks WHERE id = v_task_reap_unc) = 'UNCERTAIN', 'Reaper failed IN_FLIGHT -> UNCERTAIN';
    ASSERT (SELECT status FROM app.delivery_tasks WHERE id = v_task_reap_fail) = 'FAILED', 'Reaper failed max-attempts RESERVED -> FAILED';
    ASSERT (SELECT status FROM app.delivery_tasks WHERE id = v_task_reap_ready) = 'READY', 'Reaper failed retryable RESERVED -> READY';

    -- Test M9b: Consume 300 in new active hold, then attempt late settle of 300 on expired hold 1 (300+300 > 400 -> RF004!)
    PERFORM app.fn_budget_reserve('00000000-0000-0000-0000-000000000003', v_ws1, NULL, 'idem-b3', 300, 60);
    v_caught := false;
    BEGIN
        PERFORM app.fn_budget_settle('00000000-0000-0000-0000-000000000001', 'COMMITTED', 300);
    EXCEPTION WHEN SQLSTATE 'RF004' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Expected RF004 on late COMMITTED settle breaching cap after expiry!';

    -- [F] Audited UNCERTAIN Requeue at attempts=10 -> Re-Claimable & Superuser Break-Glass Check (Fixes M7 & Minor #2)
    RESET ROLE;
    EXECUTE 'SET LOCAL ROLE relayforge_web';
    PERFORM set_config('app.tenant_id', v_t1::text, true);

    PERFORM app.fn_web_reconcile_uncertain_task(
        'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', v_task_reap_unc, 2,
        'CONFIRM_NOT_SENT_REQUEUE', true, 'owner-1', 'Verified missing in destination; approved manual requeue'
    );
    ASSERT (SELECT status FROM app.delivery_tasks WHERE id = v_task_reap_unc) = 'READY', 'Requeue from UNCERTAIN at attempts=10 failed';
    ASSERT (SELECT max_attempts FROM app.delivery_tasks WHERE id = v_task_reap_unc) = 11, 'max_attempts did not increment to 11 on requeue';

    -- Verify worker can now re-claim both reap_ready AND the requeued reap_unc task!
    RESET ROLE;
    EXECUTE 'SET LOCAL ROLE relayforge_worker';
    SELECT count(*) INTO v_rows FROM app.fn_worker_claim_next_jobs('w2', 10, 60);
    ASSERT v_rows = 2, 'Worker failed to re-claim requeued UNCERTAIN task (attempts=10, max_attempts=11) + reap_ready!';

    -- Verify superuser break-glass guard: blocked without reason (RF020), allowed with reason
    RESET ROLE;
    PERFORM set_config('app.break_glass_reason', '', true);
    v_caught := false;
    BEGIN
        UPDATE app.delivery_tasks SET payload_json = '{"bg":1}'::jsonb WHERE id = v_task1;
    EXCEPTION WHEN SQLSTATE 'RF020' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Superuser UPDATE without app.break_glass_reason was NOT blocked!';

    PERFORM set_config('app.break_glass_reason', 'INC-2026-SMOKE-VERIFY', true);
    UPDATE app.delivery_tasks SET payload_json = '{"bg":2}'::jsonb WHERE id = v_task1;
    PERFORM set_config('app.break_glass_reason', '', true);

    RAISE NOTICE 'ALL G0 v1.3.2 NON-VACUOUS BEHAVIORAL & SECURITY ASSERTIONS PASSED!';
END;
$$;
```

---

## ۳. مشخصات اجرایی و ابطال‌پذیر آزمایش‌های فاز صفر (`Step 0` + `SPK-01`..`SPK-10`)

| شناسه آزمایش | هدف و پروتکل اجرا | معیار قبولی قطعی (`Pass Criteria`) | شرط ابطال و رد معماری (`Falsification / Fail Condition`) |
|---|---|---|---|
| **Step 0 (`Smoke + Mutation`)** | اجرای مستقیم [g0_spike_smoke_test.sql](file:///d:/project/bot%20telegram%20copy/RelayForge/RelayForge_Architecture/contracts/g0_spike_smoke_test.sql) روی PostgreSQL 16.15 با `ON_ERROR_STOP=1`، اجرای مجدد برای بررسی تکرارپذیری، و **آزمون جهش (`Mutation Test`)**: تغییر موقت شرط سقف بودجه در `fn_budget_reserve` به `IF false` و تغییر موقت شرط اپوک در تریگر و اطمینان از اینکه اسکریپت تست واقعاً با خطای `ASSERT` متوقف می‌شود. | عبور کامل اسکریپت اصلی + شکست قطعی اسکریپت در هر دو تست جهش. | هرگونه خطای نحوی/مجوز/RLS در اجرای اصلی، یا سبز ماندن دروغین تست هنگام غیرفعال کردن شرط سقف بودجه یا شرط تریگر. |
| **`SPK-06` (`Static & Catalog Gates`)** | اجرای `import-linter` روی مرز `core/` (تست واردسازی مستقیم، باواسطه و پویا از `django.*`)، قانون سفارشی `ruff` علیه `transaction.on_commit` خام و `pg_advisory_lock` نشستی، و کوئری ممیزی کاتالوگ `pg_class`/`pg_policies`/`pg_roles` روی اسکیمای `app`. | شکست قطعی گیت در برابر نمونه نقض + تأیید `relforcerowsecurity = true` و حضور سیاست `*_migrator_rls` روی هر ۹ جدول `app` و `count(*) = 0` برای `rolbypassrls = true` در نقش‌های غیرسیستمی. | عبور هرگونه وابستگی جنگو به درون `core/` یا وجود حتی یک جدول بدون `FORCE RLS` / سیاست مهاجرت در اسکیمای `app`. |
| **`SPK-01` (`RLS + PgBouncer Tx Mode`)** | اجرای ۱۰۰ درخواست موازی بین ۱۰ مستأجر از مسیر `Django 5.2 -> PgBouncer (transaction mode) -> PG 16` شامل `SELECT`, `INSERT`, `UPDATE`, `DELETE` (روی `workspaces`/`routes`)، `INSERT ... ON CONFLICT`، کلید خارجی مرکب، `SAVEPOINT` تودرتو، خطای میانه تراکنش و `on_tenant_commit`. | نشت صفر بین مستأجرها؛ بازگشت صفر ردیف در نشست فاقد `app.tenant_id`؛ صفر خطای `prepared statement already exists` در ۱٬۰۰۰ تراکنش متوالی روی استخر ۵ اتصالی PgBouncer. | مشاهده حتی ۱ ردیف از مستأجر دیگر، نشت متغیر نشست به درخواست بعدی در PgBouncer، یا ناسازگاری تراکنش اتمیک جنگو با `pool_mode = transaction`. |
| **`SPK-02` (`Fair Claim + Concurrency + EXPLAIN`)** | بارگذاری ۱۰۰٬۰۰۰ کار قابل اجرا با توزیع نامتوازن (۹۵٬۰۰۰ ردیف در مستأجر پرکار و ۵٬۰۰۰ ردیف پخش در ۴۹ مستأجر کم‌کار) شامل هر سه وضعیت `READY`, `SCHEDULED`, `RETRY_WAIT`؛ اجرای همزمان ۸ کارگر و دو نشست باز همزمان با `batch_size = 100`؛ ثبت خروجی `EXPLAIN (ANALYZE, BUFFERS)` کوئری درونی برداشتن. | همپوشانی صفر (`Zero Duplicate Claim`)؛ برداشتن `100 + 100` ردیف در دو نشست همزمان باز بدون قفل اضافه؛ عدم گرسنگی مستأجران کم‌کار؛ استفاده اثبات‌شده از `Index Scan using idx_delivery_fair_claim` متناسب با `v_share` نه کل ۱۰۰٬۰۰۰ ردیف؛ صفر بن‌بست (`40P01`) روی `tenant_dispatch_fairness`. | پیمایش خطی کل Backlog مستأجر پرکار در `EXPLAIN`، بروز Deadlock روی جدول عدالت، همپوشانی در برداشتن، یا افت خروجی دو نشست موازی به علت قفل شدن ردیف‌های مازاد. |
| **`SPK-03` (`Server-Derived Multi-Scope Ledger`)** | اجرای ۵۰ نخ موازی از مسیر وب و کارگر برای رزرو و تسویه (`COMMITTED` / `RELEASED`) تحت ۳ سطح سیاست همزمان (`TENANT`, `WORKSPACE`, `ROUTE`)؛ تست گردش باکت `DAILY`/`MONTHLY` در برابر `TOTAL`؛ تست مغایرت `idempotency_key` (`RF005`)، تسویه دیرهنگام پس از انقضا (`RF004`) و نبود سیاست فعال (`RF006`). | مجموع مصرف نهایی در هیچ سیاستی و هیچ باکتی حتی ۱ میکروواحد از `max_cost_micros` عبور نکند؛ صفر بن‌بست به لطف قفل مرتب‌شده `ORDER BY bp.id ASC`؛ مسدود بودن کامل دستکاری مستقیم جداول رزرو توسط `web` و `worker`. | عبور مجموع مصرف از سقف در شرایط مسابقه، امکان حذف سیاست بالاسری توسط فراخواننده، یا پذیرش تسویه دیرهنگامی که سقف دوره را بشکند. |
| **`SPK-04` (`Crash Matrix x100 + Heartbeat + Outage`)** | اجرای ماتریس ۵ نقطه‌ای کرش کارگر با سرور مقصد Mock مجهز به Barrier همگام‌ساز (هر نقطه ۱۰۰ تکرار = ۵۰۰ تزریق کرش با `SIGKILL`)؛ تست کار طولانی بیش از ۶۰۰ ثانیه با `fn_worker_heartbeat_lease`؛ تست کارگر زامبی متوقف‌شده با `SIGSTOP/SIGCONT` پس از اجرای Reaper؛ تزریق قطعی موقت کانتینر Postgres و پر شدن دیسک (`tmpfs 32m -> ENOSPC`). | کرش پس از ارسال شبکه پیش از ثبت دیتابیس ۱۰۰٪ به `UNCERTAIN` منتقل شود و هرگز خودکار بازپخش نشود؛ تلاش کارگر زامبی با اپوک کهنه ۱۰۰٪ با `RF014` رد شود؛ کار دارای Heartbeat فعال به خطا `UNCERTAIN` نشود؛ خروج از `UNCERTAIN` بدون رکورد آشتی با `RF018` مسدود باشد. | ارسال تکراری خودکار در ناحیه ابهام، پذیرش نوشتن کارگر زامبی، یا گیر کردن دائمی کار در `DISPATCHING_IN_FLIGHT` پس از کرش کارگر یا قطعی موقت دیتابیس. |
| **`SPK-05` (`Isolated media-worker Hardening`)** | اجرای کانتینر ثابت غیرریشه `media-worker` روی Ubuntu 24.04؛ تلاش برای اتصال شبکه خروجی، نوشتن روی سیستم‌فایل ریشه، ارسال پلی‌لیست‌های مخرب `concat`/`hls`/`m3u8` و Symlink به `/etc/passwd`، بمب حافظه برای القای `OOM-Kill (137)` و حلقه بی‌نهایت برای تست Timeout و کشتن گروه فرایند. | مسدود بودن کامل شبکه و نوشتن ریشه؛ رد فایل‌های `concat`/`m3u8` پیش از بازگشایی مراجع؛ زنده‌ماندن یا بازیابی تمیز سرویس پس از `OOM-Kill` بدون نشت فرایند یتیم و پاک‌سازی قطعی فایل‌های موقت `/spool`. | نشت حتی یک بایت از فایل خارج از مسیر مجاز، دسترسی شبکه از درون `media-worker`، یا باقی ماندن فرایند فرزند پس از اتمام مهلت زمانی. |
| **`SPK-07` (`Zero-Bypass Migration & Backfill`)** | اجرای مهاجرت اسکیما و Backfill چندجدولی (`workspaces`, `routes`, `delivery_tasks`) توسط کانتینر کوتاه‌عمر `relayforge_migrator` روی دیتابیس دارای `FORCE RLS` و داده‌های چند مستأجر، به همراه تست Rollback مهاجرت معیوب. | اعمال کامل تغییرات روی ۱۰۰٪ ردیف‌های مستأجران تحت `app.migration_mode = 'on'` بدون نیاز به `SUPERUSER` یا `BYPASSRLS`، و برگشت کامل تراکنش مهاجرت در صورت بروز خطا. | صفر شدن ردیف‌های به‌روزشده به علت کسری سیاست RLS مهاجرت یا نیاز به اعطای `BYPASSRLS`. |
| **`SPK-08` (`MinIO Presigned + Synthetic Export`)** | تولید ۳ بسته Export کاملاً مصنوعی (`5 MiB`, `80 MiB`, `240 MiB`) با `generate_synthetic_tg_export.py`؛ آپلود مستقیم با Presigned `POST` مقید به پیشوند مستأجر و سقف حجم؛ پارس جریانی با `ijson` (بک‌اند `yajl2_c`)؛ تست قطع موقت کانتینر MinIO در میانه خواندن و بررسی پاک‌سازی قطعی شیء خام پس از استخراج. | اوج مصرف حافظه RSS کارگر در پردازش فایل `240 MiB` کمتر از `250 MiB` بماند؛ تلاش برای آپلود در پیشوند مستأجر دیگر توسط MinIO رد شود؛ شیء خام پس از اتمام یا شکست قطعی طبق سیاست پاک شود. | بارگذاری کل JSON در حافظه (`MemoryError`/`OOM`)، امکان نوشتن در مسیر مستأجر دیگر، یا باقی ماندن دائمی فایل خام Export پس از استخراج. |
| **`SPK-09` (`Encrypted PITR Backup & Restore`)** | فعال‌سازی آرشیو WAL و گرفتن پشتیبان فیزیکی رمزنگاری‌شده (`pgBackRest` با `AES-256` یا `pg_basebackup | age`)؛ درج تراکنش‌های مرجع با برچسب زمانی `T1` و تراکنش‌های مخرب فرضی در `T2`؛ بازیابی کامل به لحظه `T1` روی کلاستر خام جدید و اجرای مجدد ممیزی کاتالوگ `SPK-06` و تست دود. | بازیابی دقیق تا لحظه `T1` (حضور داده‌های `T1` و نبود داده‌های `T2`)، رمزنگاری اثبات‌شده فایل‌های آرشیو روی دیسک، و برقراری کامل `FORCE RLS` پس از بازیابی. | ناتوانی در بازیابی زمانی بدون خطا، ذخیره غیررمزنگاری‌شده پشتیبان روی دیسک، یا از دست رفتن سیاست‌های امنیتی پس از Restore. |
| **`SPK-10` (`50-Tenant Baseline Load & NFR-01`)** | اجرای همزمان بار ترکیبی شبیه‌ساز پایلوت بسته (۵۰ مستأجر فعال، نظرسنجی وضعیت کارها، ایجاد کار و پردازش ۱٬۰۰۰ کار تحویل در صف) روی منابع محدودشده آزمایشگاه (`4 vCPU, 8 GiB RAM`) و ثبت صدک‌های تاخیر و آمار `PgBouncer`/`Postgres`. | صدک ۹۵ تاخیر ویوهای داخلی وب **`p95 <= 500 ms`** (منطبق با `NFR-01`)، نرخ خطای ۵xx برابر صفر، و نبود اشباع اتصالات در PgBouncer. | عبور `p95` از `500 ms` یا بروز خطاهای اشباع استخر اتصالات و قفل‌های طولانی در بار پایه ۵۰ مستأجر. |

---

## ۴. چک‌لیست تصمیم و دروازه مجوز اجرا (`Owner Go / No-Go Gate`)

پیش از ساخت پوشه کدهای آزمایشگاهی `g0-spike/` و اجرای کانتینرها، موارد زیر باید تأیید شوند:

1. **اجرای مستقل نسخه `1.3.1` اسکریپت [g0_spike_smoke_test.sql](file:///d:/project/bot%20telegram%20copy/RelayForge/RelayForge_Architecture/contracts/g0_spike_smoke_test.sql) روی `PostgreSQL 16.15` به همراه تست جهش (`Step 0`)** و تأیید رفع کامل هر ۴ ایراد شدید (`S1`–`S4`) و ۶ ایراد متوسط (`M5`–`M10`).
2. **تأیید مالک محصول روی سیاست پشتیبان و حریم خصوصی:** پذیرش پشتیبان فیزیکی `PITR` با رمزنگاری بیرونی (`AES-256`) به همراه درج پنجره ماندگاری متون غیررمزنگاری‌شده (`payload_json`) تا پایان دوره Retention پشتیبان در قرارداد حریم خصوصی مستأجران.
3. **تأیید مالک محصول روی حذف Redis در MVP** (استفاده از `pg_advisory_xact_lock` و جدول `UNLOGGED` برای محدودیت نرخ) و **استفاده انحصاری از داده مصنوعی در `SPK-08`**.
4. **صدور مجوز صریح مالک** برای ایجاد محیط کد آزمایشگاهی ایزوله `g0-spike/` (خارج از کد محصول اصلی) جهت اجرای کامل آزمایش‌های `SPK-01` تا `SPK-10` و تولید گزارش شواهد `G0_EVIDENCE_REPORT.md`.
