# ADR-012: کانتینر ثابت بدون شبکه `media-worker` و مدیریت ناهمگام کارهای رسانه و پادکست
- **وضعیت:** پذیرفته‌شده (`EXECUTED` در محیط Docker Linux در `SPK-05`؛ تکرار روی میزبان بومی در `OC-08`)
- **زمینه:** روی `Ubuntu 24.04 LTS` محدودیت پیش‌فرض AppArmor روی `unprivileged userns` مانع اجرای `Bubblewrap` درون کانتینر می‌شود و مانت `docker.sock` معادل واگذاری دسترسی root میزبان است.
- **تصمیم:** جداسازی پردازش باینری رسانه در کانتینر ثابت غیرریشه `media-worker` (`10001:10001`) با `network_mode: "none"`، `read_only: true`، `cap_drop: ["ALL"]`، `pids_limit: 64` و سقف cgroup حافظه/CPU؛ ارتباط از طریق سوکت یونیکس `/spool/sock/media.sock` و اجرای `FFmpeg` با لیست سفید صریح Demuxer و `-protocol_whitelist file,pipe`.
- **پیامدها:** مهار کامل آسیب‌پذیری‌های احتمالی کدک‌ها بدون دسترسی شبکه یا دیسک میزبان و بقای سرویس اصلی حتی هنگام `OOM-Kill (137)` فرزند پرمصرف.
