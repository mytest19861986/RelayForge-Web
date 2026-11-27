import { views } from "./views.js";
import { mountOperator } from "./operator.js";
import {
  $,
  el,
  fa,
  ltr,
  date,
  platform,
  state,
  statusPill,
  maturityBadge,
  bytes,
  icon,
  card,
  meter,
  button,
  table,
  setOptions,
  confirmAction,
  download,
} from "./components.js";

const SUBTITLES = {
  dashboard: "وضعیت کلی سامانه، مصرف منابع، بودجه و رویدادهای مهم",
  sources: "مدیریت منابع محتوا و واردسازی آرشیو کانال‌ها به S3",
  destinations: "مدیریت مقصدهای انتشار، رده بلوغ و تست فنی/آزمایشی",
  routes: "تعریف مسیرهای پردازش ۴ حالته، فیلترها و شبیه‌ساز Dry-Run",
  content: "تولید، بازنویسی و بهینه‌سازی محتوا با ۴ حالت آزادی و گارد صحت حقایق",
  intelligence: "تحلیل آماری ۸۰/۲۰ محتوای کانال و استخراج هویت بدون هویت ثالث",
  styles: "مدیریت سبک‌های نگارش، نسخه‌بندی و ترکیب وزنی دو الگو",
  media: "کتابخانه رسانه، جستجوی مجوزدار، اینفوگرافیک فارسی، استیکر، کلیپ و پادکست",
  ai: "مدیریت ارائه‌دهندگان BYOK، رضایت‌نامه‌های خارجی، آزمون مدل و بودجه",
  drafts: "ویرایش پیش‌نویس، پیش‌نمایش تفکیک‌شده پیام‌رسان‌ها و انتشار با تأیید صریح",
  delivery: "پایش صف تحویل و میز کار اختصاصی تعیین تکلیف موارد UNCERTAIN",
  operations: "دفتر ممیزی تغییرناپذیر، گزارش CSV/JSON، پاکسازی و پشتیبان رمزگذاری‌شده",
};

let csrf = "";
let workspace = "";
let current = "";
let eventCursor = "";
let toastTimer;
let generation = 0;

function status(text) {
  const box = $("status");
  box.textContent = text;
  box.hidden = !text;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    box.hidden = true;
  }, 4500);
}

async function request(path, method = "GET", body) {
  const headers = {};
  if (method !== "GET") {
    if (!(body instanceof FormData)) headers["Content-Type"] = "application/json";
    headers["Idempotency-Key"] = crypto.randomUUID();
    if (csrf) headers["X-CSRFToken"] = csrf;
  }
  const response = await fetch("/api/v1" + path, {
    method,
    headers,
    credentials: "same-origin",
    body: body instanceof FormData || body === undefined ? body : JSON.stringify(body),
  });
  const isJson = response.headers.get("content-type")?.includes("json");
  const value = isJson ? await response.json() : await response.text();
  if (!response.ok) throw Error(value?.error?.message_fa || "درخواست انجام نشد.");
  return value;
}

const scoped = path => "/workspaces/" + encodeURIComponent(workspace) + path;
const query = path => request(scoped(path));
const safe = fn => async (...args) => {
  try {
    await fn(...args);
  } catch (err) {
    status(err.message);
  }
};

async function fetchList(path) {
  const res = await query(path + (path.includes("?") ? "&" : "?") + "limit=100");
  return res.items || [];
}

const itemLabel = (item, idx = 0) =>
  item.display_name ||
  item.display_title ||
  item.title ||
  item.title_text ||
  item.name ||
  item.label ||
  item.target_channel_ref ||
  item.canonical_chat_id ||
  item.model_id ||
  [state(item.job_kind || item.media_kind || item.backup_scope || item.status), fa(idx + 1)]
    .filter(Boolean)
    .join(" · ");

function syncTopbar(dash) {
  const pill = $("engine");
  $("engine-label").textContent = dash.emergency_stop ? "موتور: متوقف‌شده" : "موتور: در حال اجرا";
  pill.classList.toggle("stopped", Boolean(dash.emergency_stop));
  $("unread").textContent = fa(dash.unread || 0);
  $("stop").disabled = Boolean(dash.emergency_stop);
}

function renderAlertsList(list, onRefresh) {
  if (!list?.length) return el("p", "اعلان جدیدی وجود ندارد.", "empty");
  const wrap = el("div", null, "alert-list");
  for (const item of list) {
    const row = el("div", null, "alert-item");
    const body = el("div");
    body.append(el("strong", item.title_fa), el("p", item.body_fa));
    const right = el("div", null, "toolbar");
    right.append(el("span", fa(item.occurrence_count) + " بار", item.severity === "WARNING" ? "pill warning" : "pill"));
    if (!item.is_read) {
      right.append(
        button(
          "خواندم",
          safe(async () => {
            await request(scoped("/notifications/" + item.id + "/read"), "POST", {});
            await onRefresh();
          }),
          "btn-outline btn-sm",
        ),
      );
    }
    row.append(body, right);
    wrap.append(row);
  }
  return wrap;
}

/* ================= VIEW 1: DASHBOARD ================= */
async function renderDashboard(mount, stamp) {
  const data = await query("/dashboard");
  if (stamp !== generation) return;
  syncTopbar(data);

  const kpis = [
    ["منابع فعال", data.sources, "فعال در فضا", "blue", 1],
    ["مقصدها", data.destinations, "کانال‌های ثبت‌شده", "emerald", 2],
    ["مسیرهای فعال", data.routes, "در حال اجرا", "purple", 3],
    ["اعلان‌های خوانده‌نشده", data.unread, data.unread ? "نیازمند توجه" : "همه خوانده‌شده", "crimson", 12],
    ["کارهای رسانه‌ای", data.active_media_jobs, "در صف پردازش", "amber", 7],
  ];
  const kpiGrid = el("div", null, "kpi-grid");
  for (const [title, val, sub, color, ic] of kpis) {
    const tile = el("article", null, "kpi-card");
    const badge = el("div", null, "kpi-icon " + color);
    badge.append(icon(ic));
    tile.append(
      badge,
      el("div", title, "kpi-title"),
      el("div", fa(val), "kpi-value"),
      el("div", sub, "kpi-sub" + (color === "crimson" && val > 0 ? " danger-text" : "")),
    );
    kpiGrid.append(tile);
  }

  const plan = data.budgets?.policies?.find(
    p => p.policy_origin === "OPERATOR_PLAN" && p.period_kind === "MONTHLY",
  );
  const monthlySpend = data.budgets?.monthly_settled_spend_micros ?? data.budgets?.settled_spend_micros ?? 0;
  const maxCost = plan?.max_cost_micros || 0;
  const pct = maxCost ? Math.min(100, Math.round((monthlySpend / maxCost) * 100)) : 0;

  const budgetCard = card(
    "بودجه مستأجر",
    el("div", null, "meter-meta"),
    meter(monthlySpend, maxCost || 1, pct >= 75 ? "amber" : ""),
    el("p", "مصرف ثبت‌شده: " + fa(monthlySpend) + " میکرو-دلار", "view-subtitle"),
    el("p", plan ? "سقف پلن اپراتور (فقط خواندنی): " + fa(maxCost) + " میکرو" : "سقف پلن ثبت نشده است.", "pill"),
  );
  budgetCard.querySelector(".meter-meta").append(
    el("span", "نسبت مصرف ماهانه"),
    el("strong", fa(pct) + "٪"),
  );

  const alertsCard = card(
    "اعلان‌های مهم",
    renderAlertsList(data.recent_notifications, () => show("dashboard")),
  );

  const diskCard = card(
    "وضعیت دیسک",
    el("div", null, "meter-meta"),
    meter(data.disk_used_percent || 0, 100, (data.disk_used_percent || 0) >= 85 ? "amber" : ""),
    el("p", bytes(data.disk_used_bytes) + " مصرف‌شده از " + bytes(data.disk_total_bytes), "view-subtitle"),
    el("p", "فضای آزاد: " + bytes(data.disk_free_bytes), "view-subtitle"),
    el("span", "سیاست نگهداری: " + fa(data.pitr_retention_days || 14) + " روز (PITR)", "pill"),
  );
  diskCard.querySelector(".meter-meta").append(
    el("span", "ظرفیت استفاده‌شده"),
    el("strong", fa(data.disk_used_percent || 0) + "٪"),
  );

  const bottom = el("div", null, "grid-3");
  bottom.append(budgetCard, alertsCard, diskCard);
  mount.replaceChildren(kpiGrid, bottom);
}

/* ================= VIEW 2: SOURCES ================= */
async function renderSources(mount, stamp) {
  const list = await fetchList("/sources");
  if (stamp !== generation) return;

  const search = el("input");
  search.type = "search";
  search.placeholder = "جست‌وجو در منابع...";
  const tblHost = el("div");
  const draw = (q = "") => {
    const filtered = list.filter(s =>
      (s.display_title + " " + s.canonical_channel_id).toLowerCase().includes(q.toLowerCase()),
    );
    tblHost.replaceChildren(
      table(
        [
          ["نام منبع", "display_title"],
          ["شناسه کانال", r => ltr(r.canonical_channel_id)],
          ["نوع", r => ltr(r.source_kind)],
          ["وضعیت", r => statusPill(r.is_active ? "ACTIVE" : "DISABLED")],
        ],
        filtered,
      ),
    );
  };
  search.oninput = () => draw(search.value);
  draw();

  const addForm = el("form", null, "form-stack");
  const kindSel = el("select");
  kindSel.name = "source_kind";
  kindSel.add(new Option("وب‌هوک ربات تلگرام (WEBHOOK_INBOUND)", "WEBHOOK_INBOUND"));
  kindSel.add(new Option("آرشیو تلگرام دسکتاپ (TELEGRAM_DESKTOP_EXPORT)", "TELEGRAM_DESKTOP_EXPORT"));
  const titleIn = el("input");
  titleIn.name = "display_title";
  titleIn.required = true;
  titleIn.placeholder = "خبرگزاری آفتاب";
  const chanIn = el("input");
  chanIn.name = "canonical_channel_id";
  chanIn.required = true;
  chanIn.dir = "ltr";
  chanIn.placeholder = "@tech_news_ir";
  const addBtn = el("button", "افزودن منبع", "btn-primary");
  addBtn.type = "submit";
  addForm.append(
    wrapField("نوع منبع", kindSel),
    wrapField("نام منبع", titleIn),
    wrapField("شناسه کانال", chanIn),
    addBtn,
  );
  addForm.onsubmit = safe(async e => {
    e.preventDefault();
    const d = Object.fromEntries(new FormData(addForm));
    if (d.source_kind === "WEBHOOK_INBOUND") d.webhook_platform = "telegram";
    await request(scoped("/sources"), "POST", d);
    status("منبع جدید با موفقیت ثبت شد.");
    await show("sources");
  });

  // S3 Export Wizard
  const exportForm = el("form", null, "form-stack");
  const expSel = el("select");
  expSel.required = true;
  setOptions(expSel, list.filter(s => s.source_kind === "TELEGRAM_DESKTOP_EXPORT"), itemLabel);
  const fileIn = el("input");
  fileIn.type = "file";
  fileIn.accept = ".json,application/json";
  fileIn.required = true;
  const prog = el("progress");
  prog.max = 1;
  prog.value = 0;
  const expBtn = el("button", "بارگذاری و ثبت آرشیو در S3", "btn-primary");
  expBtn.type = "submit";
  exportForm.append(
    wrapField("انتخاب منبع آرشیو", expSel),
    wrapField("فایل خروجی JSON از Telegram Desktop", fileIn),
    prog,
    expBtn,
  );
  exportForm.onsubmit = safe(async e => {
    e.preventDefault();
    const chosen = fileIn.files[0];
    const base = scoped("/sources/" + encodeURIComponent(expSel.value) + "/exports");
    const job = await request(base + "/presign", "POST", {
      file_size_bytes: chosen.size,
      job_mode: "ARCHIVE_INGEST",
    });
    const fd = new FormData();
    for (const [k, v] of Object.entries(job.form_fields || {})) fd.append(k, v);
    fd.append("file", chosen);
    await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", job.upload_url);
      xhr.upload.onprogress = ev => {
        if (ev.lengthComputable) prog.value = ev.loaded / ev.total;
      };
      xhr.onerror = () => reject(Error("ارتباط با مخزن S3 برقرار نشد."));
      xhr.onload = () => (xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(Error("بارگذاری رد شد.")));
      xhr.send(fd);
    });
    prog.value = 1;
    await request(base + "/" + job.export_job_id + "/complete", "POST", {});
    status("آرشیو با موفقیت به S3 منتقل و ثبت شد.");
  });

  const leftCol = el("div", null, "stack");
  leftCol.append(
    card("منابع موجود", search, tblHost),
    card("استخراج آرشیو از Telegram Desktop (ارسال مستقیم به S3)", exportForm),
  );
  const rightCol = el("div", null, "stack");
  rightCol.append(
    card("افزودن منبع جدید", addForm),
    el("div", "نکته مهم: به دلیل محدودیت‌های Bot API تلگرام، استخراج کامل آرشیو پیام‌های قبلی کانال‌ها تنها از طریق فایل Export تلگرام دسکتاپ امکان‌پذیر است.", "notice"),
  );
  const grid = el("div", null, "grid-2");
  grid.append(leftCol, rightCol);
  mount.replaceChildren(grid);
}

/* ================= VIEW 3: DESTINATIONS ================= */
async function renderDestinations(mount, stamp) {
  const [dests, secrets] = await Promise.all([fetchList("/destinations"), fetchList("/security/secrets").catch(() => [])]);
  if (stamp !== generation) return;

  const selectedSel = el("select");
  setOptions(selectedSel, dests, itemLabel, false);

  const testConnBtn = button(
    "تست فنی اتصال",
    safe(async () => {
      if (!selectedSel.value) return status("ابتدا یک مقصد انتخاب کنید.");
      await request(scoped("/destinations/" + encodeURIComponent(selectedSel.value) + "/test-connection"), "POST", {});
      status("تست فنی اتصال با موفقیت انجام شد.");
    }),
    "btn-violet btn-block",
  );

  const testText = el("textarea");
  testText.placeholder = "متن پست آزمایشی واقعی برای ارسال به کانال مقصد...";
  const testPubBtn = button(
    "ارسال پست آزمایشی واقعی",
    safe(async () => {
      if (!selectedSel.value || !testText.value.trim()) return status("مقصد و متن آزمایشی را وارد کنید.");
      if (!(await confirmAction("ارسال پست آزمایشی واقعی", "یک پیام واقعی به کانال مقصد ارسال خواهد شد."))) return;
      await request(scoped("/destinations/" + encodeURIComponent(selectedSel.value) + "/test-publish"), "POST", {
        text: testText.value,
        explicit_user_confirmation: true,
      });
      status("پست آزمایشی واقعی با موفقیت ارسال شد.");
    }),
    "btn-primary btn-block",
  );

  const addForm = el("form", null, "form-stack");
  const platSel = el("select");
  platSel.name = "platform";
  for (const p of ["TELEGRAM", "BALE", "EITAA", "RUBIKA_EXPERIMENTAL", "SOROUSH_EXPERIMENTAL"]) {
    platSel.add(new Option(platform(p), p));
  }
  const nameIn = el("input");
  nameIn.name = "display_name";
  nameIn.required = true;
  nameIn.placeholder = "کانال رسمی تلگرام";
  const chatIn = el("input");
  chatIn.name = "canonical_chat_id";
  chatIn.required = true;
  chatIn.dir = "ltr";
  chatIn.placeholder = "@channel_id";
  const secSel = el("select");
  secSel.name = "secret_id";
  secSel.required = true;
  setOptions(secSel, secrets, itemLabel);
  const addBtn = el("button", "ثبت مقصد جدید", "btn-primary");
  addBtn.type = "submit";
  addForm.append(
    wrapField("پیام‌رسان", platSel),
    wrapField("نام مقصد", nameIn),
    wrapField("شناسه کانال مقصد", chatIn),
    wrapField("انتخاب کلید از خزانه اسرار (Secret Vault)", secSel),
    addBtn,
  );
  addForm.onsubmit = safe(async e => {
    e.preventDefault();
    await request(scoped("/destinations"), "POST", Object.fromEntries(new FormData(addForm)));
    status("مقصد جدید با موفقیت ثبت شد.");
    await show("destinations");
  });

  const tbl = table(
    [
      ["مقصد", "display_name"],
      ["پیام‌رسان", r => platform(r.platform)],
      ["شناسه", r => ltr(r.canonical_chat_id)],
      ["وضعیت و رده بلوغ", r => maturityBadge(r.platform)],
      ["آلبوم بومی", r => (r.capabilities_json?.native_albums ? "دارد" : "—")],
      ["وضعیت", r => statusPill(r.is_enabled ? "ACTIVE" : "DISABLED")],
    ],
    dests,
  );

  const rightCol = el("div", null, "stack");
  rightCol.append(
    card("آزمون اتصال و ارسال آزمایشی", wrapField("مقصد انتخاب‌شده", selectedSel), testConnBtn, wrapField("متن تست واقعی", testText), testPubBtn),
    card("افزودن مقصد جدید", addForm),
  );
  const grid = el("div", null, "grid-2");
  grid.append(card("مقصدها و ماتریس قابلیت", tbl), rightCol);
  mount.replaceChildren(grid);
}

/* ================= VIEW 4: ROUTES & DRY-RUN ================= */
async function renderRoutes(mount, stamp) {
  const [routes, sources, dests] = await Promise.all([
    fetchList("/routes"),
    fetchList("/sources"),
    fetchList("/destinations"),
  ]);
  if (stamp !== generation) return;

  const tbl = table(
    [
      ["نام مسیر", "display_name"],
      ["حالت", r => el("span", r.route_mode, "pill purple")],
      ["خودکارسازی", r => state(r.automation_level)],
      ["وضعیت", r => statusPill(r.is_enabled ? "ACTIVE" : "DISABLED")],
      [
        "عملیات",
        r =>
          button(
            r.is_enabled ? "توقف" : "فعال‌سازی",
            safe(async () => {
              await request(scoped("/routes/" + r.id), "PATCH", { is_enabled: !r.is_enabled });
              await show("routes");
            }),
            "btn-outline btn-sm",
          ),
      ],
    ],
    routes,
  );

  // Dry-Run Card
  const drySel = el("select");
  setOptions(drySel, routes, itemLabel, false);
  const sampleIn = el("textarea");
  sampleIn.placeholder = "متن نمونه برای شبیه‌سازی مرحله‌به‌مرحله بدون ارسال واقعی...";
  const dryOut = el("div");
  const stepper = el("div", null, "stepper");
  for (const [i, step] of ["۱. انتخاب محتوا", "۲. اعمال قوانین", "۳. پیش‌نمایش خروجی", "۴. گزارش نتیجه"].entries()) {
    const box = el("div", null, "step-box");
    box.append(el("span", fa(i + 1), "pill"), document.createTextNode(step.slice(3)));
    stepper.append(box);
  }
  const dryBtn = button(
    "اجرای شبیه‌سازی Dry-Run",
    safe(async () => {
      if (!drySel.value || !sampleIn.value.trim()) return status("مسیر و متن نمونه را وارد کنید.");
      const res = await request(scoped("/routes/" + encodeURIComponent(drySel.value) + "/dry-run"), "POST", {
        sample_text: sampleIn.value,
      });
      dryOut.replaceChildren(
        el("p", "نتیجه شبیه‌سازی: " + state(res.decision || "COMPLETED"), "pill"),
        ...(res.destinations || []).map(d =>
          card(d.platform ? platform(d.platform) : "مقصد", el("div", d.formatting?.payload?.plain_text || d.filter_reason || "—", "bubble")),
        ),
      );
    }),
    "btn-violet",
  );

  // Create Route Card with 4 segmented mode buttons
  let mode = "HYBRID";
  const seg = el("div", null, "segmented");
  for (const m of ["COPY", "RULES", "AI", "HYBRID"]) {
    const b = button(m, () => {
      mode = m;
      seg.querySelectorAll("button").forEach(x => x.classList.toggle("active", x === b));
    }, m === mode ? "active" : "");
    seg.append(b);
  }
  const createForm = el("form", null, "form-stack");
  const slugIn = el("input");
  slugIn.name = "slug";
  slugIn.required = true;
  slugIn.dir = "ltr";
  slugIn.placeholder = "tech-news-route";
  const rName = el("input");
  rName.name = "display_name";
  rName.required = true;
  rName.placeholder = "مسیر بازنشر اخبار فناوری";
  const srcMulti = el("select");
  srcMulti.multiple = true;
  srcMulti.required = true;
  setOptions(srcMulti, sources, itemLabel, false);
  const dstMulti = el("select");
  dstMulti.multiple = true;
  dstMulti.required = true;
  setOptions(dstMulti, dests, itemLabel, false);
  const kwIn = el("input");
  kwIn.name = "keyword";
  kwIn.placeholder = "کلمه کلیدی فیلتر (اختیاری)";
  const rBtn = el("button", "ایجاد مسیر جدید", "btn-primary");
  rBtn.type = "submit";
  createForm.append(
    wrapField("نام مسیر", rName),
    wrapField("شناسه لاتین", slugIn),
    wrapField("حالت پردازش", seg),
    wrapField("منابع (چندانتخابی)", srcMulti),
    wrapField("مقصدها (چندانتخابی)", dstMulti),
    wrapField("فیلتر کلیدواژه", kwIn),
    rBtn,
  );
  createForm.onsubmit = safe(async e => {
    e.preventDefault();
    const sIds = Array.from(srcMulti.selectedOptions, o => o.value);
    const dIds = Array.from(dstMulti.selectedOptions, o => o.value);
    if (!sIds.length || !dIds.length) return status("حداقل یک منبع و یک مقصد انتخاب کنید.");
    await request(scoped("/routes"), "POST", {
      slug: slugIn.value,
      display_name: rName.value,
      source_ids: sIds,
      destination_ids: dIds,
      route_mode: mode,
      automation_level: "APPROVAL_REQUIRED",
      filter_ast: kwIn.value ? { op: "regex_match", value: kwIn.value } : {},
    });
    status("مسیر جدید ساخته شد.");
    await show("routes");
  });

  const leftCol = el("div", null, "stack");
  leftCol.append(
    card("مسیرهای موجود", tbl),
    card("شبیه‌ساز Dry-Run (بدون انتشار واقعی)", stepper, wrapField("انتخاب مسیر", drySel), wrapField("متن نمونه", sampleIn), dryBtn, dryOut),
  );
  const grid = el("div", null, "grid-2");
  grid.append(leftCol, card("ایجاد مسیر جدید", createForm));
  mount.replaceChildren(grid);
}

/* ================= VIEW 5: CONTENT STUDIO ================= */
async function renderContentStudio(mount, stamp) {
  const [styles, providers] = await Promise.all([fetchList("/styles"), fetchList("/ai/providers")]);
  if (stamp !== generation) return;

  let freedom = "BALANCED";
  const freedomDefs = [
    ["STRICT", "سخت‌گیرانه", "دقیق و نزدیک به متن مرجع"],
    ["BALANCED", "متعادل (پیشنهادی)", "بازنویسی روان با حفظ کامل حقایق"],
    ["CREATIVE", "خلاقانه", "خلاقانه و تحلیلی با کنترل اعداد"],
    ["UNRESTRICTED", "کاملاً آزاد", "آزاد (گارد صحت خاموش)"],
  ];
  const fGrid = el("div", null, "freedom-grid");
  for (const [code, label, desc] of freedomDefs) {
    const fc = el("button", null, "freedom-card" + (code === freedom ? " active" : ""));
    fc.type = "button";
    fc.append(el("strong", label), el("small", desc));
    fc.onclick = () => {
      freedom = code;
      fGrid.querySelectorAll(".freedom-card").forEach(x => x.classList.toggle("active", x === fc));
    };
    fGrid.append(fc);
  }

  const rawIn = el("textarea");
  rawIn.rows = 6;
  rawIn.placeholder = "متن ورودی (مرجع) خود را اینجا بنویسید یا پیست کنید...";
  const styleSel = el("select");
  setOptions(styleSel, styles, itemLabel);
  const provSel = el("select");
  setOptions(provSel, providers, itemLabel);
  const outHost = el("div", null, "stack");
  outHost.append(el("p", "پس از کلیک روی «تولید محتوا»، خروجی پیشنهادی و گزارش Fact Guard اینجا نمایش داده می‌شوند.", "empty"));

  const genBtn = button(
    "تولید محتوا",
    safe(async () => {
      if (!rawIn.value.trim()) return status("متن ورودی را وارد کنید.");
      const res = await request(scoped("/studio/generate"), "POST", {
        input_mode: "MANUAL_TEXT",
        raw_text: rawIn.value,
        task_type: "REWRITE",
        freedom_mode: freedom,
        routing_mode: "AUTO_ECONOMY",
        ...(styleSel.value ? { style_profile_id: styleSel.value } : {}),
        ...(provSel.value ? { manual_provider_id: provSel.value } : {}),
      });
      outHost.replaceChildren();
      for (const c of res.candidates || []) {
        const diff = c.fact_diff_json || {};
        const blocked = c.fact_guard_verdict === "FACT_MISMATCH_BLOCKED";
        const pills = el("div", null, "fact-pills");
        pills.append(
          el("span", blocked ? "مغایرت مسدود شد" : "تأییدشده", blocked ? "pill stopped" : "pill"),
          el("span", "حذف‌شده: " + fa(diff.missing?.length || 0), "pill warning"),
          el("span", "افزوده‌شده: " + fa(diff.introduced?.length || 0), "pill purple"),
        );
        const diffList = el("ul");
        for (const m of diff.missing || []) diffList.append(el("li", "حذف‌شده از مرجع: " + m));
        for (const a of diff.introduced || []) diffList.append(el("li", "افزوده‌شده در خروجی: " + a));
        outHost.append(
          card("خروجی پیشنهادی", el("div", c.text || "—", "bubble")),
          card("Fact Guard · بررسی صحت اعداد و ادعاها", pills, diffList.children.length ? diffList : el("p", "مغایرتی یافت نشد.", "view-subtitle")),
        );
      }
      status("تولید محتوا و بررسی Fact Guard انجام شد.");
    }),
    "btn-primary",
  );

  const mainCol = el("div", null, "grid-2-eq");
  mainCol.append(
    card("متن ورودی (مرجع)", rawIn, genBtn),
    el("div", null, "stack"),
  );
  mainCol.lastChild.append(outHost);

  const adv = card(
    "تنظیمات پیشرفته",
    wrapField("سبک نگارش", styleSel),
    wrapField("ارائه‌دهنده هوش مصنوعی", provSel),
  );
  const layout = el("div", null, "grid-2");
  layout.append(mainCol, adv);
  mount.replaceChildren(fGrid, layout);
}

/* ================= VIEW 6: CHANNEL INTELLIGENCE ================= */
async function renderIntelligence(mount, stamp) {
  const [sources, analyses] = await Promise.all([
    fetchList("/sources"),
    fetchList("/channel-intelligence/analyses").catch(() => []),
  ]);
  if (stamp !== generation) return;

  const srcSel = el("select");
  setOptions(srcSel, sources, itemLabel, false);
  const runBtn = button(
    "آغاز تحلیل کانال",
    safe(async () => {
      if (!srcSel.value) return status("یک منبع انتخاب کنید.");
      await request(scoped("/channel-intelligence/analyses"), "POST", {
        source_id: srcSel.value,
        requested_sample_count: 120,
      });
      status("تحلیل هوشمند کانال در صف قرار گرفت.");
      await show("intelligence");
    }),
    "btn-primary",
  );

  const donutCard = card(
    "تفکیک نمونه‌گیری ۸۰/۲۰",
    el("div", null, "donut"),
    el("p", "۸۰٪ استخراج الگو · ۲۰٪ ارزیابی مستقل", "view-subtitle"),
    el("div", "هویت کانال‌های ثالث به صورت پیش‌فرض حذف (STRIP_BY_DEFAULT) می‌شود.", "notice"),
  );

  const tbl = table(
    [
      ["کانال هدف", r => ltr(r.target_channel_ref)],
      ["نمونه‌های یکتا", r => fa(r.unique_sample_count)],
      ["وضعیت", r => statusPill(r.status)],
    ],
    analyses,
  );

  const grid = el("div", null, "grid-2");
  grid.append(
    card("گزارش تحلیل‌های کانال", wrapField("انتخاب منبع", srcSel), runBtn, tbl),
    donutCard,
  );
  mount.replaceChildren(grid);
}

/* ================= VIEW 7: STYLES ================= */
async function renderStyles(mount, stamp) {
  const styles = await fetchList("/styles");
  if (stamp !== generation) return;

  const cards = el("div", null, "grid-2-eq");
  for (const s of styles) {
    cards.append(
      card(
        itemLabel(s),
        el("span", "نسخه " + fa(s.pinned_revision_no), "pill"),
        el("p", "دسته‌بندی: " + (s.content_category || "عمومی"), "view-subtitle"),
      ),
    );
  }
  const f1 = el("select");
  const f2 = el("select");
  setOptions(f1, styles, itemLabel);
  setOptions(f2, styles, itemLabel);
  const range = el("input");
  range.type = "range";
  range.min = "10";
  range.max = "90";
  range.value = "60";
  const out = el("p", "۶۰٪ سبک اول / ۴۰٪ سبک دوم", "pill");
  range.oninput = () => {
    out.textContent = fa(range.value) + "٪ سبک اول / " + fa(100 - Number(range.value)) + "٪ سبک دوم";
  };
  const mixTitle = el("input");
  mixTitle.placeholder = "نام الگوی ترکیبی";
  const mixBtn = button(
    "ذخیره ترکیب وزنی",
    safe(async () => {
      if (!f1.value || !f2.value || !mixTitle.value.trim()) return status("دو سبک و نام ترکیب را وارد کنید.");
      await request(scoped("/styles/compose"), "POST", {
        slug: "mix-" + Date.now(),
        title: mixTitle.value,
        styles: [
          { style_id: f1.value, weight: Number(range.value) / 100 },
          { style_id: f2.value, weight: (100 - Number(range.value)) / 100 },
        ],
      });
      status("الگوی ترکیبی ذخیره شد.");
      await show("styles");
    }),
    "btn-primary",
  );

  const grid = el("div", null, "grid-2");
  grid.append(
    card("فهرست سبک‌های نوشتار", cards.children.length ? cards : el("p", "الگویی ثبت نشده است.", "empty")),
    card("ترکیب وزن‌دار سبک‌ها", wrapField("سبک اول", f1), wrapField("سبک دوم", f2), wrapField("نسبت ترکیب", range), out, wrapField("نام ترکیب", mixTitle), mixBtn),
  );
  mount.replaceChildren(grid);
}

/* ================= VIEW 8: MEDIA & PODCAST ================= */
async function renderMedia(mount, stamp) {
  const [assets, jobs] = await Promise.all([fetchList("/media/assets"), fetchList("/media/jobs").catch(() => [])]);
  if (stamp !== generation) return;

  const tabBar = el("div", null, "tabs");
  const tabHost = el("div");
  const drawTab = name => {
    tabBar.querySelectorAll("button").forEach(b => b.classList.toggle("active", b.textContent === name));
    if (name === "کتابخانه رسانه") {
      tabHost.replaceChildren(
        card(
          "کتابخانه رسانه",
          table(
            [
              ["نوع", "media_kind"],
              ["خاستگاه", "origin_kind"],
              ["حجم", r => bytes(r.byte_size)],
            ],
            assets,
          ),
        ),
      );
    } else if (name === "سازنده اینفوگرافیک") {
      const t = el("input");
      t.placeholder = "عنوان اینفوگرافیک فارسی";
      const k = el("input");
      k.placeholder = "برچسب سطر (مثلاً نرخ رشد)";
      const v = el("input");
      v.placeholder = "مقدار (مثلاً ۲۴٪)";
      const b = button(
        "تولید اینفوگرافیک",
        safe(async () => {
          await request(scoped("/media/jobs"), "POST", {
            job_kind: "INFOGRAPHIC_RENDER",
            title: t.value || "اینفوگرافیک",
            table_rows: [[k.value || "شاخص", v.value || "۱۰۰"]],
          });
          status("کار تولید اینفوگرافیک ثبت شد.");
        }),
        "btn-primary",
      );
      tabHost.replaceChildren(card("سازنده اینفوگرافیک فارسی", wrapField("عنوان", t), wrapField("برچسب", k), wrapField("مقدار", v), b));
    } else {
      tabHost.replaceChildren(
        card(
          "کارهای پردازش رسانه و پادکست",
          table(
            [
              ["نوع کار", "job_kind"],
              ["وضعیت", r => statusPill(r.status)],
              ["زمان ثبت", r => date(r.created_at)],
            ],
            jobs,
          ),
        ),
      );
    }
  };
  for (const name of ["کتابخانه رسانه", "سازنده اینفوگرافیک", "کارهای رسانه"]) {
    tabBar.append(button(name, () => drawTab(name)));
  }
  drawTab("کتابخانه رسانه");
  mount.replaceChildren(tabBar, tabHost);
}

/* ================= VIEW 9: AI SETTINGS ================= */
async function renderAI(mount, stamp) {
  const [providers, consents] = await Promise.all([
    fetchList("/ai/providers"),
    fetchList("/security/consents").catch(() => []),
  ]);
  if (stamp !== generation) return;

  let activeType = "OPENAI";
  const provList = el("div", null, "form-stack");
  for (const [code, label] of [
    ["OPENAI", "OpenAI"],
    ["GEMINI", "Gemini"],
    ["COMPATIBLE_OPENAI", "Compatible OpenAI"],
  ]) {
    const pc = el("div", null, "provider-card" + (code === activeType ? " active" : ""));
    pc.append(el("strong", label), el("span", code === activeType ? "انتخاب‌شده" : "انتخاب", "pill"));
    pc.onclick = () => {
      activeType = code;
      provList.querySelectorAll(".provider-card").forEach(x => x.classList.toggle("active", x === pc));
    };
    provList.append(pc);
  }
  const pName = el("input");
  pName.placeholder = "نام نمایشی (مثلاً OpenAI اصلی)";
  const pKey = el("input");
  pKey.type = "password";
  pKey.placeholder = "کلید API (ذخیره رمزنگاری‌شده در خزانه)";
  const saveProv = button(
    "ذخیره کلید ارائه‌دهنده",
    safe(async () => {
      if (!pName.value || !pKey.value) return status("نام و کلید API را وارد کنید.");
      await request(scoped("/ai/providers"), "PUT", {
        provider_type: activeType,
        display_name: pName.value,
        api_key: pKey.value,
      });
      pKey.value = "";
      status("ارائه‌دهنده هوش مصنوعی با موفقیت ذخیره شد.");
      await show("ai");
    }),
    "btn-primary",
  );

  const consentHost = el("div");
  const scopes = [
    ["OPENAI_TEXT", "استفاده از سرویس متن OpenAI"],
    ["GEMINI_TEXT", "استفاده از سرویس متن Gemini"],
    ["STOCK_MEDIA_SEARCH", "جستجوی رسانه استوک دارای مجوز"],
  ];
  for (const [sc, desc] of scopes) {
    const existing = consents.find(c => c.provider_scope === sc);
    const granted = Boolean(existing?.is_granted);
    const row = el("div", null, "consent-row");
    const info = el("div");
    info.append(el("strong", sc), el("p", desc, "view-subtitle"));
    const toggle = button(
      granted ? "فعال" : "غیرفعال",
      safe(async () => {
        await request(scoped("/security/consents"), "PUT", {
          provider_scope: sc,
          is_granted: !granted,
        });
        status("تنظیم رضایت خارجی به‌روزرسانی شد.");
        await show("ai");
      }),
      granted ? "btn-primary btn-sm" : "btn-outline btn-sm",
    );
    row.append(info, toggle);
    consentHost.append(row);
  }

  const grid = el("div", null, "grid-2-eq");
  grid.append(
    card("ارائه‌دهنده هوش مصنوعی (BYOK)", provList, wrapField("نام نمایشی", pName), wrapField("کلید API", pKey), saveProv),
    card("رضایت و دسترسی‌های خارجی", consentHost, el("p", "ارائه‌دهندگان ثبت‌شده: " + fa(providers.length), "pill")),
  );
  mount.replaceChildren(grid);
}

/* ================= VIEW 10: DRAFTS & PUBLISH ================= */
async function renderDrafts(mount, stamp) {
  const [drafts, dests] = await Promise.all([fetchList("/drafts"), fetchList("/destinations")]);
  if (stamp !== generation) return;

  let activeDraft = drafts[0] || null;
  const bodyIn = el("textarea");
  bodyIn.rows = 6;
  bodyIn.placeholder = "متن پیش‌نویس را وارد یا ویرایش کنید...";
  const dstSel = el("select");
  setOptions(dstSel, dests, itemLabel, false);

  const previewBubble = el("div", "متن پیش‌نمایش پیام‌رسان اینجا نمایش داده می‌شود.", "bubble");
  if (activeDraft) {
    query("/drafts/" + encodeURIComponent(activeDraft.id))
      .then(d => {
        bodyIn.value = d.body_text || "";
        previewBubble.textContent = d.body_text || "—";
      })
      .catch(() => {});
  }
  bodyIn.oninput = () => {
    previewBubble.textContent = bodyIn.value || "—";
  };

  const btnBar = el("div", null, "toolbar");
  btnBar.append(
    button(
      "ذخیره پیش‌نویس",
      safe(async () => {
        if (!bodyIn.value.trim()) return status("متن پیش‌نویس خالی است.");
        await request(scoped("/drafts"), "POST", { body_text: bodyIn.value });
        status("پیش‌نویس جدید ذخیره شد.");
        await show("drafts");
      }),
      "btn-outline",
    ),
    button(
      "ارسال آزمایشی واقعی",
      safe(async () => {
        if (!activeDraft || !dstSel.value) return status("پیش‌نویس و مقصد را انتخاب کنید.");
        if (!(await confirmAction("ارسال آزمایشی واقعی", "نسخه آزمایشی به مقصد انتخاب‌شده ارسال می‌شود."))) return;
        await request(scoped("/drafts/" + activeDraft.id + "/publish"), "POST", {
          destination_ids: [dstSel.value],
          album_fallback_policy: "SEQUENTIAL_POSTS",
          mode: "EXPLICIT_TEST_SEND",
          expected_draft_revision_no: activeDraft.current_revision_no || 1,
          explicit_user_confirmation: true,
        });
        status("ارسال آزمایشی در صف تحویل ثبت شد.");
      }),
      "btn-amber",
    ),
    button(
      "انتشار نهایی",
      safe(async () => {
        if (!activeDraft || !dstSel.value) return status("پیش‌نویس و مقصد را انتخاب کنید.");
        if (!(await confirmAction("تأیید انتشار نهایی", "نسخه انتشار منجمد شده و برای ارسال نهایی در صف قرار می‌گیرد."))) return;
        await request(scoped("/drafts/" + activeDraft.id + "/publish"), "POST", {
          destination_ids: [dstSel.value],
          album_fallback_policy: "SEQUENTIAL_POSTS",
          mode: "LIVE_PUBLISH",
          expected_draft_revision_no: activeDraft.current_revision_no || 1,
          explicit_user_confirmation: true,
        });
        status("انتشار نهایی با موفقیت ثبت و صف‌بندی شد.");
      }),
      "btn-primary",
    ),
  );

  const grid = el("div", null, "grid-2");
  grid.append(
    card("ویرایش پیش‌نویس و تنظیمات انتشار", wrapField("مقصد انتشار", dstSel), wrapField("متن محتوا", bodyIn), btnBar),
    card("پیش‌نمایش پیام‌رسان", previewBubble, el("div", "پیش‌نمایش وب جایگزین نمایش واقعی در برنامه پیام‌رسان نیست.", "notice")),
  );
  mount.replaceChildren(grid);
}

/* ================= VIEW 11: DELIVERY & UNCERTAIN ================= */
async function renderDelivery(mount, stamp) {
  let activeStatus = "";
  const filterBar = el("div", null, "tabs");
  const tblHost = el("div");
  const recHost = el("div");

  const loadDeliveries = async () => {
    const q = activeStatus ? "/deliveries?status=" + encodeURIComponent(activeStatus) : "/deliveries";
    const rows = await fetchList(q);
    if (stamp !== generation) return;
    tblHost.replaceChildren(
      table(
        [
          ["وضعیت", r => statusPill(r.status)],
          ["زمان برنامه‌ریزی", r => date(r.scheduled_for)],
          ["تلاش", r => fa(r.attempts)],
          ["جزئیات", r => r.last_error_safe || "—"],
          [
            "عملیات",
            r =>
              r.status === "UNCERTAIN"
                ? button("تعیین تکلیف", () => mountReconcile(r), "btn-amber btn-sm")
                : "—",
          ],
        ],
        rows,
      ),
    );
    const firstUnc = rows.find(r => r.status === "UNCERTAIN");
    if (firstUnc) mountReconcile(firstUnc);
  };

  function mountReconcile(row) {
    let decision = "DISCARD_DO_NOT_SEND";
    const radios = el("div", null, "form-stack");
    for (const [code, label] of [
      ["CONFIRM_SENT", "ارسال عادی تأیید شد (CONFIRM_SENT)"],
      ["CONFIRM_NOT_SENT_REQUEUE", "ارسال مجدد با بازبینی (RESEND)"],
      ["DISCARD_DO_NOT_SEND", "انصراف و عدم ارسال (DISCARD_DO_NOT_SEND)"],
    ]) {
      const r = el("div", null, "provider-card" + (code === decision ? " active" : ""));
      r.append(el("strong", label));
      r.onclick = () => {
        decision = code;
        radios.querySelectorAll(".provider-card").forEach(x => x.classList.toggle("active", x === r));
      };
      radios.append(r);
    }
    const reasonIn = el("input");
    reasonIn.placeholder = "دلیل تصمیم (حداقل ۳ کاراکتر)...";
    const ack = el("input");
    ack.type = "checkbox";
    const ackLabel = el("label", null, "check-row");
    ackLabel.append(ack, el("span", "ریسک ارسال تکراری را می‌پذیرم"));
    const execBtn = button(
      "اجرای تصمیم آشتی",
      safe(async () => {
        if (decision === "CONFIRM_NOT_SENT_REQUEUE" && !ack.checked) {
          return status("برای ارسال مجدد، تیک پذیرش ریسک ارسال تکراری الزامی است.");
        }
        await request(scoped("/deliveries/" + row.id + "/reconcile"), "POST", {
          expected_lease_epoch: row.lease_epoch || 1,
          resolution_action: decision,
          reason: reasonIn.value || "تعیین تکلیف توسط مدیر",
          acknowledge_duplicate_risk: ack.checked,
        });
        status("تعیین تکلیف وضعیت UNCERTAIN با موفقیت ثبت شد.");
        await loadDeliveries();
      }),
      "btn-primary btn-block",
    );
    recHost.replaceChildren(
      card(
        "میز آشتی پیام‌های نامطمئن (UNCERTAIN)",
        el("span", row.last_error_safe || "UNCERTAIN", "pill warning"),
        radios,
        wrapField("توضیح / دلیل", reasonIn),
        ackLabel,
        execBtn,
      ),
    );
  }

  for (const [code, cap] of [
    ["", "همه"],
    ["UNCERTAIN", "نامطمئن"],
    ["FAILED", "ناموفق"],
    ["SENT", "ارسال‌شده"],
    ["SCHEDULED", "زمان‌بندی‌شده"],
    ["READY", "آماده"],
  ]) {
    const b = button(cap, safe(async () => {
      activeStatus = code;
      filterBar.querySelectorAll("button").forEach(x => x.classList.toggle("active", x === b));
      await loadDeliveries();
    }));
    if (!code) b.classList.add("active");
    filterBar.append(b);
  }

  recHost.replaceChildren(card("میز آشتی UNCERTAIN", el("p", "روی یک ردیف نامطمئن کلیک کنید.", "empty")));
  const grid = el("div", null, "grid-2");
  grid.append(card("صف تحویل محتوا", filterBar, tblHost), recHost);
  mount.replaceChildren(grid);
  await loadDeliveries();
}

/* ================= VIEW 12: OPERATIONS & BACKUPS ================= */
async function renderOperations(mount, stamp) {
  const [audits, backups] = await Promise.all([
    fetchList("/audit-events").catch(() => []),
    fetchList("/backups").catch(() => []),
  ]);
  if (stamp !== generation) return;

  const csvBtn = button(
    "دانلود گزارش CSV ایمن",
    safe(async () => {
      const csv = await query("/reports/export?format=csv");
      download(csv, "relayforge-report.csv", "text/csv;charset=utf-8");
    }),
    "btn-outline",
  );
  const jsonBtn = button(
    "دانلود گزارش JSON",
    safe(async () => {
      const data = await query("/reports/export?format=json");
      download(JSON.stringify(data, null, 2), "relayforge-report.json", "application/json");
    }),
    "btn-outline",
  );
  const cleanBtn = button(
    "پاکسازی فایل‌های منقضی",
    safe(async () => {
      if (!(await confirmAction("پاکسازی فضای ذخیره‌سازی", "فایل‌های منقضی و بدون ارجاع پاکسازی می‌شوند."))) return;
      await request(scoped("/maintenance/cleanup"), "POST", {});
      status("پاکسازی با موفقیت انجام شد.");
    }),
    "btn-outline",
  );

  const bpPass = el("input");
  bpPass.type = "password";
  bpPass.placeholder = "گذرواژه رمزگذاری پشتیبان (حداقل ۱۲ کاراکتر)";
  const bpBtn = button(
    "تهیه پشتیبان رمزگذاری‌شده (.rfbak)",
    safe(async () => {
      if (bpPass.value.length < 12) return status("گذرواژه پشتیبان باید حداقل ۱۲ کاراکتر باشد.");
      const res = await request(scoped("/backups"), "POST", {
        backup_password: bpPass.value,
        backup_scope: "TENANT_FULL_LOGICAL",
        include_media_assets: false,
        includes_reencrypted_secrets: false,
      });
      bpPass.value = "";
      if (res.bundle_base64) {
        download(Uint8Array.from(atob(res.bundle_base64), c => c.charCodeAt(0)), "tenant.rfbak", "application/octet-stream");
      }
      status("پشتیبان رمزگذاری‌شده ایجاد شد.");
      await show("operations");
    }),
    "btn-primary",
  );

  const delSlug = el("input");
  delSlug.dir = "ltr";
  delSlug.placeholder = "نامک مستأجر برای تأیید حذف قطعی";
  const delBtn = button(
    "حذف قطعی حساب مالک",
    safe(async () => {
      if (!delSlug.value.trim()) return status("نامک مستأجر را وارد کنید.");
      if (!(await confirmAction("حذف قطعی حساب مالک", "این عملیات برگشت‌ناپذیر است و ماندگاری فیزیکی PITR تا ۱۴ روز خواهد بود."))) return;
      await request("/tenants/current/delete-account", "POST", {
        confirmation_slug: delSlug.value.trim(),
        acknowledge_irreversible: true,
      });
      location.assign("/");
    }),
    "btn-stop",
  );

  const leftCol = el("div", null, "stack");
  leftCol.append(
    card(
      "گزارش ممیزی تغییرناپذیر (Audit Log)",
      el("div", null, "toolbar"),
      table(
        [
          ["عملیات", "action_code"],
          ["نقش", "actor_role"],
          ["زمان", r => date(r.created_at)],
        ],
        audits,
      ),
    ),
    card(
      "پشتیبان‌های ثبت‌شده (.rfbak)",
      table(
        [
          ["دامنه", r => state(r.backup_scope)],
          ["حجم", r => bytes(r.byte_size)],
          ["زمان", r => date(r.created_at)],
        ],
        backups,
      ),
    ),
  );
  leftCol.firstChild.querySelector(".toolbar").append(csvBtn, jsonBtn, cleanBtn);

  const rightCol = el("div", null, "stack");
  rightCol.append(
    card("تهیه پشتیبان رمزگذاری‌شده", wrapField("گذرواژه پشتیبان", bpPass), bpBtn),
    card(
      "منطقه خطرناک · حذف حساب مالک",
      el("div", "حذف کامل حساب مالک و امحای کلید رمزنگاری مستأجر (Crypto-Shredding). دوره نگهداری فیزیکی PITR: ۱۴ روز.", "notice danger-zone"),
      wrapField("تأیید نامک مستأجر", delSlug),
      delBtn,
    ),
  );
  const grid = el("div", null, "grid-2");
  grid.append(leftCol, rightCol);
  mount.replaceChildren(grid);
}

function wrapField(labelText, control) {
  const wrap = el("label", null, "field");
  wrap.append(el("span", labelText), control);
  return wrap;
}

const RENDERERS = {
  dashboard: renderDashboard,
  sources: renderSources,
  destinations: renderDestinations,
  routes: renderRoutes,
  content: renderContentStudio,
  intelligence: renderIntelligence,
  styles: renderStyles,
  media: renderMedia,
  ai: renderAI,
  drafts: renderDrafts,
  delivery: renderDelivery,
  operations: renderOperations,
};

async function show(name) {
  const [key, heading] = views.find(item => item[0] === name) || views[0];
  const stamp = ++generation;
  current = key;
  $("title").textContent = heading;
  $("subtitle").textContent = SUBTITLES[key] || "";
  $("actions").replaceChildren();
  $("results").replaceChildren(el("p", "در حال بارگذاری…", "empty"));
  document.querySelectorAll(".sidebar nav a").forEach(link => {
    if (link.dataset.view === key) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  $("sidebar").classList.remove("open");
  $("menu").setAttribute("aria-expanded", "false");
  try {
    await (RENDERERS[key] || renderDashboard)($("results"), stamp);
  } catch (error) {
    if (stamp === generation) $("results").replaceChildren(el("div", error.message, "notice danger-zone"));
  }
}

/* ================= BOOTSTRAP ================= */
if (location.pathname.replace(/\/$/, "") === "/operator") {
  mountOperator(status);
} else {
  const nav = document.querySelector(".sidebar nav");
  for (const [index, [key, title]] of views.entries()) {
    const link = el("a");
    link.href = "/app/" + key;
    link.dataset.view = key;
    link.append(icon(index), document.createTextNode(title));
    link.onclick = event => {
      event.preventDefault();
      history.pushState({}, "", link.href);
      show(key);
    };
    nav.append(link);
  }
}

$("bell-icon").append(icon(12));
$("bell").onclick = safe(async () => {
  const drawer = $("notifications");
  drawer.hidden = !drawer.hidden;
  $("bell").setAttribute("aria-expanded", String(!drawer.hidden));
  if (!drawer.hidden) {
    const list = await fetchList("/notifications").catch(() => []);
    drawer.replaceChildren(
      el("h3", "مرکز اعلان‌ها"),
      renderAlertsList(list, async () => {
        await show(current);
        drawer.hidden = true;
      }),
    );
  }
});

$("stop").onclick = safe(async () => {
  if (!(await confirmAction("توقف اضطراری موتور", "تمام ارسال‌های در انتظار لغو شده و موتور این فضای کاری متوقف می‌شود."))) return;
  await request(scoped("/ops/emergency-stop"), "POST", { enabled: true, cancel_waiting: true });
  status("توقف اضطراری فعال شد.");
  await show(current);
});

$("menu").onclick = () => {
  const open = $("sidebar").classList.toggle("open");
  $("menu").setAttribute("aria-expanded", String(open));
};

$("eye").onclick = () => {
  const input = $("login").elements.password;
  const visible = input.type === "password";
  input.type = visible ? "text" : "password";
  $("eye").textContent = visible ? "پنهان" : "نمایش";
  $("eye").setAttribute("aria-pressed", String(visible));
};

window.onpopstate = () => {
  if (workspace) show(location.pathname.split("/").pop());
};

$("workspace").onchange = safe(async event => {
  workspace = event.target.value;
  eventCursor = "";
  $("notifications").hidden = true;
  await show(current);
});

$("logout").onclick = safe(async () => {
  await request("/auth/logout", "POST");
  csrf = "";
  workspace = "";
  location.assign("/");
});

$("login").onsubmit = safe(async event => {
  event.preventDefault();
  const form = event.target;
  const data = Object.fromEntries(new FormData(form));
  if (!data.tenant_slug) delete data.tenant_slug;
  form.elements.password.value = "";
  const session = await request("/auth/login", "POST", data);
  csrf = session.csrf_token;
  workspace = session.default_workspace_id;
  const wsRes = await request("/workspaces");
  setOptions($("workspace"), wsRes.items || [], itemLabel, false);
  $("workspace").value = workspace;
  $("entry").hidden = true;
  $("studio").hidden = false;
  $("topbar").hidden = false;
  history.replaceState({}, "", "/app/dashboard");
  await show("dashboard");
});

async function poll() {
  try {
    if (workspace && !document.hidden) {
      const active = workspace;
      const data = await query("/events/poll" + (eventCursor ? "?cursor=" + encodeURIComponent(eventCursor) : ""));
      if (active === workspace && data.items?.length) {
        eventCursor = data.items.at(-1).event_id;
        if (current === "dashboard") await show("dashboard");
      }
    }
  } catch {
    // quiet background poll
  } finally {
    setTimeout(poll, 12000);
  }
}
setTimeout(poll, 12000);
