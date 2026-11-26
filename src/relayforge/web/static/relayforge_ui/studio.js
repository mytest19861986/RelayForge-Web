import {views, actions} from "./views.js";

const $ = id => document.getElementById(id);
let csrf = "", workspace = "", current = "", eventCursor = "";
const status = text => { $("status").textContent = text; };

async function request(path, method = "GET", body) {
  const headers = {};
  if (method !== "GET") {
    headers["Content-Type"] = "application/json";
    headers["Idempotency-Key"] = crypto.randomUUID();
    if (csrf) headers["X-CSRFToken"] = csrf;
  }
  const response = await fetch("/api/v1" + path, {
    method, headers, credentials: "same-origin", body: body === undefined ? undefined : JSON.stringify(body)
  });
  const value = response.headers.get("content-type")?.includes("json")
    ? await response.json() : await response.text();
  if (!response.ok) throw Error(value.error?.message_fa || "درخواست انجام نشد");
  return value;
}

const scoped = path => "/workspaces/" + encodeURIComponent(workspace) + path;
function render(value) {
  const pre = document.createElement("pre");
  pre.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  const results = $("results");
  results.replaceChildren(pre);
  if (value.bundle_base64 || typeof value === "string") {
    const button = document.createElement("button");
    button.textContent = value.bundle_base64 ? "دریافت فایل پشتیبان" : "دریافت گزارش";
    button.onclick = () => {
      const text = value.bundle_base64 ? atob(value.bundle_base64) : value;
      const data = value.bundle_base64 ? Uint8Array.from(text, char => char.charCodeAt(0)) : text;
      const url = URL.createObjectURL(new Blob([data]));
      const link = document.createElement("a");
      link.href = url;
      link.download = value.bundle_base64 ? "tenant.rfbak" : "report.csv";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    };
    results.prepend(button);
  }
  if (current === "dashboard" && "sources" in value) {
    const summary = document.createElement("div");
    summary.className = "metrics";
    for (const [key, title] of Object.entries({
      sources:"منبع فعال", destinations:"مقصد", routes:"مسیر فعال",
      unread:"هشدار خوانده‌نشده", active_media_jobs:"کار رسانه در صف"
    })) {
      const card = document.createElement("div");
      const number = document.createElement("strong");
      number.textContent = new Intl.NumberFormat("fa").format(value[key]);
      card.append(number, document.createTextNode(title));
      summary.append(card);
    }
    results.prepend(summary);
  }
}

function operation([label, path, method, fields, data, confirmAction]) {
  const box = document.createElement("form");
  for (const [name, caption, kind] of fields) {
    const wrapper = document.createElement("label");
    wrapper.textContent = caption;
    const input = document.createElement(Array.isArray(kind) ? "select" : kind === "textarea" ? kind : "input");
    input.name = name;
    if (Array.isArray(kind)) {
      for (const choice of kind) input.add(new Option(choice, choice));
    } else if (input.tagName === "INPUT") {
      input.type = kind;
      input.required = kind !== "checkbox";
      if (kind === "password") input.autocomplete = "new-password";
    }
    wrapper.append(input);
    box.append(wrapper);
  }
  const button = document.createElement("button");
  button.textContent = label;
  if (confirmAction) button.className = "danger";
  box.append(button);
  box.onsubmit = async event => {
    event.preventDefault();
    if (confirmAction && !window.confirm(label + "؟")) return;
    const values = Object.fromEntries(new FormData(box));
    box.querySelectorAll('input[type="password"]').forEach(input => {input.value = "";});
    button.disabled = true;
    try {
      const value = await request(scoped(path.replace("{id}", encodeURIComponent(values.id || ""))), method, method === "GET" ? undefined : data(values));
      render(value);
      status("انجام شد");
    } catch (error) { status(error.message); }
    finally { button.disabled = false; }
  };
  return box;
}

function archiveWizard() {
  const form = document.createElement("form");
  const heading = document.createElement("strong");
  heading.textContent = "بارگذاری خروجی JSON از Telegram Desktop";
  const notice = document.createElement("p");
  notice.textContent = "Bot API آرشیو پیام‌های قبلی کانال را دریافت نمی‌کند.";
  const source = document.createElement("input");
  source.placeholder = "شناسه منبع آرشیو";
  source.setAttribute("aria-label", source.placeholder);
  source.required = true;
  source.dir = "ltr";
  const file = document.createElement("input");
  file.type = "file";
  file.accept = ".json,application/json";
  file.required = true;
  file.setAttribute("aria-label", "فایل خروجی آرشیو");
  const progress = document.createElement("progress");
  progress.max = 1;
  progress.value = 0;
  progress.setAttribute("aria-label", "پیشرفت بارگذاری");
  const button = document.createElement("button");
  button.textContent = "بارگذاری و ثبت آرشیو";
  form.append(heading, notice, source, file, progress, button);
  form.onsubmit = async event => {
    event.preventDefault();
    button.disabled = true;
    progress.value = 0;
    const path = scoped("/sources/" + encodeURIComponent(source.value) + "/exports");
    try {
      const chosen = file.files[0];
      const job = await request(path + "/presign", "POST", {
        file_size_bytes: chosen.size, job_mode: "ARCHIVE_INGEST"
      });
      const body = new FormData();
      for (const [name, value] of Object.entries(job.form_fields)) body.append(name, value);
      body.append("file", chosen);
      await new Promise((resolve, reject) => {
        const upload = new XMLHttpRequest();
        upload.open("POST", job.upload_url);
        upload.upload.onprogress = event => { if (event.lengthComputable) progress.value = event.loaded / event.total; };
        upload.onerror = () => reject(Error("ارتباط با مخزن فایل برقرار نشد"));
        upload.onload = () => upload.status >= 200 && upload.status < 300
          ? resolve() : reject(Error("مخزن فایل بارگذاری را رد کرد"));
        upload.send(body);
      });
      progress.value = 1;
      render(await request(path + "/" + job.export_job_id + "/complete", "POST", {}));
    } catch (error) { status(error.message); }
    finally { button.disabled = false; }
  };
  return form;
}

async function show(name) {
  const [key, heading, endpoint] = views.find(view => view[0] === name) || views[0];
  current = key;
  $("title").textContent = heading;
  $("actions").replaceChildren(...(actions[key] || []).map(operation));
  $("results").replaceChildren();
  document.querySelectorAll("nav a").forEach(link => {
    link.removeAttribute("aria-current");
    if (link.dataset.view === key) link.setAttribute("aria-current", "page");
  });
  if (key === "sources") $("actions").append(archiveWizard());
  if (key === "drafts") {
    const note = document.createElement("p");
    note.textContent = "پیش‌نمایش وب جایگزین نمایش در برنامه پیام‌رسان نیست.";
    $("actions").prepend(note);
  }
  if (endpoint) {
    const activeWorkspace = workspace;
    try {
      const data = await request(scoped(endpoint));
      if (current === key && workspace === activeWorkspace) render(data);
    } catch (error) { status(error.message); }
  }
}

for (const [key, label] of views) {
  const link = document.createElement("a");
  link.href = "/app/" + key;
  link.textContent = label;
  link.dataset.view = key;
  link.onclick = event => {
    event.preventDefault();
    history.pushState({}, "", link.href);
    show(key);
  };
  document.querySelector("nav").append(link);
}
window.onpopstate = () => { if (workspace) show(location.pathname.split("/").pop()); };
$("workspace").onchange = async event => { workspace = event.target.value; eventCursor = ""; await show(current); };
$("login").onsubmit = async event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  if (!data.tenant_slug) delete data.tenant_slug;
  event.target.password.value = "";
  try {
    const session = await request("/auth/login", "POST", data);
    csrf = session.csrf_token;
    workspace = session.default_workspace_id;
    const {items} = await request("/workspaces");
    $("workspace").replaceChildren(...items.map(item => new Option(item.display_name, item.id)));
    $("workspace").value = workspace;
    $("entry").hidden = true;
    $("studio").hidden = false;
    $("workspace-control").hidden = false;
    history.replaceState({}, "", "/app/dashboard");
    show("dashboard");
  } catch (error) { status(error.message); }
};

async function poll() {
  if (workspace && !document.hidden) {
    const activeWorkspace = workspace;
    try {
      const data = await request(scoped("/events/poll" + (eventCursor ? "?cursor=" + eventCursor : "")));
      if (workspace === activeWorkspace && data.items?.length) {
        eventCursor = data.items.at(-1).event_id;
        if (current === "dashboard") render(await request(scoped("/dashboard")));
      }
    } catch (error) { status(error.message); }
  }
  setTimeout(poll, 12000);
}
setTimeout(poll, 12000);
