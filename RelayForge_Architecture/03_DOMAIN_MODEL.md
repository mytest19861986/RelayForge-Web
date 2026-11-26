# 03 — مدل دامنه و زمینه‌های محدود (Domain Model & Multi-Tenant Bounded Contexts)

## ۱. سلسله‌مراتب مالکیت در دامنه چندمستأجری

در نسخه وب RelayForge، ریشه مطلق مالکیت داده موجودیت **`Tenant` (مستأجر / سازمان)** است:
`Tenant (1) ──< Workspace (N) ──< { Sources, Destinations, Routes, Styles, Brands, Media, Drafts, Deliveries }`

- هیچ موجودیت عملیاتی بدون `(tenant_id, workspace_id)` معتبر نیست.
- کاربران (`User`) از طریق عضویت صریح (`TenantMembership` با نقش‌های `OWNER`, `ADMIN`, `EDITOR`, `PUBLISHER`, `VIEWER`) به یک یا چند مستأجر دسترسی می‌یابند، اما هر نشست وب (`WebSession`) در هر لحظه روی یک `tenant_id` مشخص مقید است.

---

## ۲. ده زمینه محدود دامنه (`Bounded Contexts` — نگاشت ۱ به ۱ به `M01..M10`)

1. **هسته مستأجر، فضای کاری و زمان اجرا (`Tenant & Workspace Kernel — M01`):** مدیریت چرخه حیات مستأجران، فضاهای کاری، تنظیمات پایه، عدالت توزیع کار بین مستأجران (`TenantDispatchFairness`) و شمارنده‌های محدودیت نرخ.
2. **هویت، خزانه پاکتی و حاکمیت داده (`IAM, Envelope Vault & Governance — M02`):** احراز هویت `Argon2id`، نشست‌های وب قابل ابطال، کلیدهای رمزنگاری اختصاصی هر مستأجر (`TenantEncryptionKey`)، خزانه اسرار (`SecretsVault`) و دروازه رضایت ارسال خارجی (`ExternalConsent`).
3. **درگاه پیام‌رسان‌ها و ورود آرشیو (`Messenger Gateway & Export Ingestion — M03`):** انتزاع دریافت از ربات/فید، مدیریت کارهای استخراج جریانی فایل Export تلگرام از S3 (`ExportImportJob`) و آداپتورهای ارسال به ۵ پیام‌رسان با ثبت صریح رده بلوغ (`GA`, `OUTBOUND_LIMITED`, `EXPERIMENTAL`).
4. **محتوای استاندارد و قالب‌بندی (`Canonical Content & Formatting — M04`):** مدل تغییرناپذیر `ContentItem` و `ContentItemRevision` مبتنی بر متن ساده `NFC` + درخت بازه‌های معنایی `UTF-16` و شاخص جست‌وجوی تمام‌متن `GIN tsvector`.
5. **کانال‌شناس، سبک، برند و یادگیری (`Style, Brand & Preference Intelligence — M05`):** جداسازی کامل الگوی نگارشی (`StyleProfile / StyleRevision`) از هویت برند مالک (`BrandIdentity`) و استخراج قواعد ترجیحی از ویرایش‌های کاربر (`LearnedPreferenceRule`).
6. **درگاه هوش مصنوعی و دفتر کل بودجه دوسطحی (`AI Gateway & Dual-Tier Budget Ledger — M06`):** کشف آنلاین مدل‌ها به تفکیک کلید، مسیریابی مستقل برای متن/تصویر/صوت/ویدئو، و دفتر کل بودجه با تفکیک سخت سقف پلن اپراتور (`OPERATOR_PLAN`) از زیربودجه‌های تعیینی مستأجر (`TENANT_BUDGET`).
7. **تولید محتوا، `Fact Guard` و کیفیت (`Generation & Fact Guard — M07`):** اجرای چهار حالت آزادی (`STRICT`, `BALANCED`, `CREATIVE`, `UNRESTRICTED`)، نرمال‌ساز حقایق فارسی و ارزیابی کیفیت سه‌لایه بدون امتیاز ساختگی.
8. **استودیو رسانه و پادکست (`Media & Podcast Studio — M08`):** مدیریت دارایی‌های رسانه در S3 با شمارش ارجاع (`MediaAssetRef`)، سناریوی قابل ویرایش پادکست (`PodcastScript`) و صف کارهای پردازش رسانه در کانتینر ایزوله `media-worker`.
9. **گردش کار، پیش‌نویس و تحویل مطمئن (`Workflow & Reliable Delivery — M09`):** مسیرهای ۴ حالته، انجماد نسخه انتشار (`PublicationVersion`) پیش از ارسال، ماشین وضعیت ۱۱ حالته تحویل با توکن حصارکشی `lease_epoch` و فرآیند آشتی ممیزی‌شده `UNCERTAIN`.
10. **عملیات، ممیزی و تداوم کسب‌وکار (`Operations, Audit & Continuity — M10`):** دفتر ممیزی فقط‌افزودنی (`AuditEvent`)، اعلان‌های ادغام‌شونده (`SystemNotification`) و مانیفست‌های پشتیبان منطقی و `PITR`.

---

## ۳. نامتغیرهای کلیدی دامنه (`Core Domain Invariants`)

- **جداسازی اسکوپ تحلیل از انتشار (`SRC-10`):** رکوردهای جمع‌آوری‌شده برای کانال‌شناس با `operation_scope = 'ANALYSIS_ISOLATED'` ثبت می‌شوند و از نظر ساختاری هرگز رویداد ورود به خط لوله انتشار (`INGEST_PIPELINE`) تولید نمی‌کنند.
- **خنثی ماندن کاندید تولیدی نسبت به برند مقصد (`IDN-05`):** خروجی هوش مصنوعی بدون امضای کانال ذخیره می‌شود و `BrandIdentity` اختصاصی هر مقصد منحصراً هنگام پیش‌نمایش یا انجماد `PublicationVersion` به‌صورت قطعی تزریق می‌گردد.
- **عدم بازپخش خودکار در ناحیه ابهام (`QUE-08`):** به محض عبور کار به وضعیت `DISPATCHING_IN_FLIGHT`، هرگونه قطعی یا انقضای اجاره منحصراً به وضعیت `UNCERTAIN` ختم می‌شود و خروج به `READY` مستلزم ثبت رکورد در `reconciliation_records` با پذیرش صریح ریسک تکرار (`acknowledge_duplicate_risk = true`) است.
