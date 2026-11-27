export const $ = id => document.getElementById(id);
export const fa = value => new Intl.NumberFormat("fa-IR").format(value ?? 0);
export const date = value => value ? new Intl.DateTimeFormat("fa-IR",{dateStyle:"medium",timeStyle:"short",timeZone:"Asia/Tehran"}).format(new Date(value)) : "—";
export const platform = value => ({TELEGRAM:"تلگرام",BALE:"بله",EITAA:"ایتا",RUBIKA_EXPERIMENTAL:"روبیکا",SOROUSH_EXPERIMENTAL:"سروش‌پلاس"})[value] || value;
export const state = value => ({
  ACTIVE:"فعال", DISABLED:"غیرفعال", STOPPED:"متوقف‌شده", RETRY_WAIT:"در انتظار تلاش دوباره", PENDING_DELETION:"در انتظار حذف", READY:"آماده",
  SCHEDULED:"زمان‌بندی‌شده", SENT:"ارسال‌شده", FAILED:"ناموفق", UNCERTAIN:"نامطمئن",
  COMPLETED:"تکمیل‌شده", PENDING:"در صف", RUNNING:"در حال اجرا", SUCCEEDED:"موفق",
  EDITING:"در حال ویرایش", APPROVED:"تأییدشده", NEEDS_REVIEW:"نیازمند بازبینی",
  CANCELLED:"لغوشده", TRUE:"بله", FALSE:"خیر",
  STRICT:"سخت‌گیرانه", BALANCED:"متعادل · پیشنهادی", CREATIVE:"خلاق", UNRESTRICTED:"آزاد",
  CONFIRM_SENT:"ارسال تأیید شد", CONFIRM_NOT_SENT_REQUEUE:"ارسال نشده · بازگردانی به صف",
  DISCARD_DO_NOT_SEND:"کنارگذاری", TENANT_FULL_LOGICAL:"پشتیبان کامل منطقی",
  TENANT_SETTINGS_EXPORT:"تنظیمات فضای کاری",
})[value] || value;
export function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  return node;
}
const warningStates = new Set(["UNCERTAIN", "NEEDS_REVIEW", "RETRY_WAIT", "PENDING",
  "RUNNING", "DISABLED", "FALSE"]);
const stoppedStates = new Set(["FAILED", "CANCELLED", "STOPPED", "PENDING_DELETION"]);

export function statusPill(status) {
  const tone = stoppedStates.has(status) ? " stopped" : warningStates.has(status) ? " warning" : "";
  return el("span", state(status) || "—", "pill" + tone);
}

export function bytes(value) {
  if (value == null) return "—";
  const [power, unit] = value >= 1024 ** 3 ? [3, "گیگابایت"] :
    value >= 1024 ** 2 ? [2, "مگابایت"] : [1, "کیلوبایت"];
  return fa(Math.round(value / 1024 ** power * 10) / 10) + " " + unit;
}

export function ltr(value) {
  const node = el("bdi",value);
  node.dir = "ltr";
  return node;
}
const svgNS = "http://www.w3.org/2000/svg";
const strokes = [
  "M3 10l9-7 9 7v10H3z M9 20v-7h6v7","M5 4h14v16H5z M8 8h8 M8 12h8 M8 16h5",
  "M3 11L21 3l-6 18-4-8z M11 13l10-10","M4 6h16 M4 12h16 M4 18h16 M8 4v4 M16 10v4 M10 16v4",
  "M4 20l4-1L20 7l-3-3L5 16z M14 7l3 3","M4 19h16 M7 16v-5 M12 16V5 M17 16V8",
  "M4 19V6h13v13z M8 3h12v13","M3 5h18v14H3z M3 17l6-6 4 4 3-2 5 5",
  "M12 2v4 M12 18v4 M2 12h4 M18 12h4 M7 7h10v10H7z",
  "M5 3h10l4 4v14H5z M8 12h8 M8 16h5","M5 7h14 M5 12h14 M5 17h14 M3 7h.01 M3 12h.01 M3 17h.01",
  "M12 3l8 3v6c0 5-4 8-8 9-4-1-8-4-8-9V6z M8 12l3 3 5-6",
  "M18 8a6 6 0 0 0-12 0v5l-2 3h16l-2-3z M10 20h4",
];
export function icon(index) {
  const svg = document.createElementNS(svgNS,"svg");
  svg.setAttribute("viewBox","0 0 24 24"); svg.setAttribute("class","icon");
  svg.setAttribute("aria-hidden","true");
  const path = document.createElementNS(svgNS,"path");
  for (const [key,value] of Object.entries({d:strokes[index % strokes.length],fill:"none",stroke:"currentColor","stroke-width":"1.65","stroke-linecap":"round","stroke-linejoin":"round"})) path.setAttribute(key,value);
  svg.append(path);
  return svg;
}
export function button(label, click, className="secondary") {
  const node = el("button",label,className);
  node.type = "button";
  node.onclick = click;
  return node;
}
export function card(title, ...children) {
  const node = el("article",null,"card");
  if (title) node.append(el("h3",title));
  node.append(...children);
  return node;
}
export function table(columns, items) {
  if (!items.length) return el("p","رکوردی برای نمایش وجود ندارد.","empty");
  const grid = el("table");
  const head = grid.createTHead().insertRow();
  for (const [label] of columns) {
    const heading = el("th",label);
    heading.scope = "col";
    head.append(heading);
  }
  const body = grid.createTBody();
  for (const row of items) {
    const line = body.insertRow();
    for (const [,read] of columns) {
      const value = typeof read === "function" ? read(row) : row[read];
      line.insertCell().append(value instanceof Node ? value : document.createTextNode(value ?? "—"));
    }
  }
  const wrap = el("div",null,"table-wrap");
  wrap.append(grid);
  return wrap;
}
export function confirmAction(title, copy="این عملیات با تأیید شما انجام می‌شود.") {
  const dialog = $("confirmation");
  $("confirmation-title").textContent = title;
  $("confirmation-copy").textContent = copy;
  dialog.returnValue = "cancel";
  dialog.showModal();
  return new Promise(resolve => dialog.addEventListener("close",()=>resolve(dialog.returnValue==="confirm"),{once:true}));
}
export function download(data,name,type) {
  const href = URL.createObjectURL(new Blob([data],{type}));
  const link = el("a");
  link.href = href; link.download = name; link.click();
  setTimeout(()=>URL.revokeObjectURL(href),1000);
}
export function setOptions(input, items, label, placeholder=true) {
  const selected = new Set(Array.from(input.selectedOptions,option=>option.value));
  input.replaceChildren(...(placeholder && !input.multiple ? [new Option("انتخاب کنید","")] : []),
    ...items.map((item,index)=>new Option(label(item,index),item.id,false,selected.has(item.id))));
}
