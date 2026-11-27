import {
  $,
  el,
  button,
  card,
  table,
  fa,
  ltr,
  statusPill,
  date,
  icon,
  confirmAction,
} from "./components.js";

export function mountOperator(status) {
  $("entry").hidden = true;
  const root = $("operator");
  root.hidden = false;
  let token = "";
  let renderVersion = 0;

  const banner = el("div", null, "operator-banner");
  const titleBox = el("div");
  titleBox.append(
    el("h1", "پنل سوپرادمین / اپراتور پلتفرم"),
    el("p", "کنترل سطح پلتفرم، مدیریت مستأجرها و تنظیم سقف پلن OPERATOR_PLAN"),
  );
  const home = el("a", "بازگشت به ورود استودیو");
  home.href = "/";
  banner.append(titleBox, home);
  root.replaceChildren(banner);

  async function api(path, method = "GET", data) {
    const headers = { Authorization: "Bearer " + token };
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      headers["Idempotency-Key"] = crypto.randomUUID();
    }
    const result = await fetch("/api/v1/operator" + path, {
      method,
      headers,
      credentials: "omit",
      body: data === undefined ? undefined : JSON.stringify(data),
    });
    if (!result.ok) {
      let msg = "درخواست اپراتور انجام نشد.";
      try {
        msg = (await result.json()).error?.message_fa || msg;
      } catch {
        // ignore parse errors
      }
      throw Error(msg);
    }
    return result.json();
  }

  function makeForm(fields, submitLabel, onSubmit) {
    const box = el("form", null, "form-stack");
    for (const [name, title, type, value, placeholder] of fields) {
      const wrap = el("label", null, "field");
      wrap.append(el("span", title));
      const input = el("input");
      input.name = name;
      input.type = type;
      input.required = true;
      input.value = value ?? "";
      if (placeholder) input.placeholder = placeholder;
      if (type === "password") input.autocomplete = "off";
      if (type === "number") {
        input.min = "0";
        input.step = "1";
      }
      if (type === "email" || type === "password" || name === "slug") input.dir = "ltr";
      wrap.append(input);
      box.append(wrap);
    }
    const action = el("button", submitLabel, "btn-primary");
    action.type = "submit";
    box.append(action);
    box.onsubmit = async event => {
      event.preventDefault();
      action.disabled = true;
      const value = Object.fromEntries(new FormData(box));
      box.querySelectorAll('input[type="password"]').forEach(item => {
        item.value = "";
      });
      try {
        await onSubmit(value);
      } catch (error) {
        status(error.message);
      } finally {
        action.disabled = false;
      }
    };
    return box;
  }

  const authCard = card(
    "ورود اپراتور پلتفرم",
    el("p", "کلید Bearer فقط در حافظه موقت صفحه نگهداری می‌شود و هرگز در مرورگر ذخیره نمی‌گردد.", "notice info"),
    makeForm([["token", "کلید دسترسی اپراتور (Bearer Token)", "password", "", "demo-operator-token"]], "ورود به پنل اپراتور", async values => {
      token = values.token;
      try {
        await refresh();
      } catch (error) {
        token = "";
        throw error;
      }
      authWrap.hidden = true;
      consoleGrid.hidden = false;
      status("ورود به پنل اپراتور با موفقیت انجام شد.");
    }),
  );
  const authWrap = el("div", null, "auth-wrap");
  authCard.style.width = "min(440px, 100%)";
  authWrap.append(authCard);

  const consoleGrid = el("div", null, "operator-grid");
  consoleGrid.hidden = true;

  const side = el("aside", null, "sidebar");
  side.append(el("div", "اپراتور پلتفرم", "sidebar-caption"));
  const sideNav = el("nav");
  const listLink = el("a", null);
  listLink.href = "#tenants";
  listLink.setAttribute("aria-current", "page");
  listLink.append(icon(0), document.createTextNode("مدیریت مستأجرها"));
  const createBtnNav = el("a", null);
  createBtnNav.href = "#create";
  createBtnNav.append(icon(1), document.createTextNode("ایجاد مستأجر جدید"));
  const exitLink = el("a", null);
  exitLink.href = "#exit";
  exitLink.append(icon(11), document.createTextNode("خروج از پنل"));
  sideNav.append(listLink, createBtnNav, exitLink);
  side.append(sideNav);

  const centerCol = el("div", null, "stack");
  const toolbar = el("div", null, "toolbar");
  const search = el("input");
  search.type = "search";
  search.placeholder = "جست‌وجو در مستأجرها (نام یا شناسه Slug)...";
  search.style.maxWidth = "320px";
  search.onchange = () => refresh().catch(error => status(error.message));

  const creationDialog = el("dialog", null, "modal-dialog");
  const creationBody = el("div", null, "modal-body");
  creationBody.append(
    el("h2", "ایجاد مستأجر جدید"),
    makeForm(
      [
        ["display_name", "نام سازمان / مستأجر", "text", "", "رسانه آفتاب"],
        ["slug", "شناسه لاتین (Slug)", "text", "", "org-aftab"],
        ["owner_email", "ایمیل مالک", "email", "", "owner@company.ir"],
        ["owner_display_name", "نام کامل مالک", "text", "", "مهدی رحمانی"],
        ["owner_password", "گذرواژه اولیه (حداقل ۱۲ کاراکتر)", "password", "", "••••••••••••"],
      ],
      "ایجاد مستأجر و فضای کاری",
      async item => {
        await api("/tenants", "POST", item);
        creationDialog.close();
        status("مستأجر جدید و فضای کاری پیش‌فرض با موفقیت ایجاد شدند.");
        await refresh();
      },
    ),
    button("بستن", () => creationDialog.close(), "btn-outline"),
  );
  creationDialog.append(creationBody);

  createBtnNav.onclick = e => {
    e.preventDefault();
    creationDialog.showModal();
  };
  listLink.onclick = e => {
    e.preventDefault();
    refresh().catch(err => status(err.message));
  };
  exitLink.onclick = e => {
    e.preventDefault();
    token = "";
    renderVersion++;
    consoleGrid.hidden = true;
    tableHost.replaceChildren();
    quotaHost.replaceChildren();
    authWrap.hidden = false;
  };

  toolbar.append(
    button("+ ایجاد مستأجر جدید", () => creationDialog.showModal(), "btn-primary"),
    search,
    button("بروزرسانی", () => refresh().catch(err => status(err.message)), "btn-outline"),
  );

  const tableHost = el("div");
  centerCol.append(card("مدیریت مستأجرها (Tenants)", toolbar, tableHost));

  const quotaHost = el("div", null, "stack");
  function renderDefaultQuota() {
    quotaHost.replaceChildren(
      card(
        "سقف پلن اپراتور (OPERATOR_PLAN)",
        el("span", "قفل‌شده در دیتابیس (RF007)", "pill"),
        el("p", "برای ویرایش سقف روزانه و ماهانه هر سازمان، روی دکمه «ویرایش سقف» در جدول کلیک کنید.", "empty"),
        el("div", "بازه نگهداری فیزیکی PITR: ۱۴ روز", "notice"),
      ),
    );
  }
  renderDefaultQuota();

  function openQuotaEditor(item) {
    quotaHost.replaceChildren(
      card(
        "ویرایش سقف پلن · " + item.display_name,
        el("span", "OPERATOR_PLAN", "pill"),
        makeForm(
          [
            ["daily_max_cost_micros", "سقف روزانه AI (میکرو-دلار)", "number", item.daily_max_cost_micros],
            ["monthly_max_cost_micros", "سقف ماهانه AI (میکرو-دلار)", "number", item.monthly_max_cost_micros],
          ],
          "ذخیره سقف پلن",
          async value => {
            if (!(await confirmAction("به‌روزرسانی سقف پلن", item.display_name))) return;
            await api("/tenants/" + encodeURIComponent(item.id) + "/plan-quotas", "PUT", {
              daily_max_cost_micros: Number(value.daily_max_cost_micros),
              monthly_max_cost_micros: Number(value.monthly_max_cost_micros),
            });
            status("سقف پلن اپراتور با موفقیت ذخیره شد.");
            await refresh();
          },
        ),
        el("div", "بازه نگهداری فیزیکی PITR: ۱۴ روز", "notice"),
      ),
    );
  }

  async function refresh(cursor = "") {
    const generation = ++renderVersion;
    const response = await api(
      "/tenants?" + new URLSearchParams({ q: search.value, ...(cursor ? { cursor } : {}) }),
    );
    if (generation !== renderVersion) return;
    const rows = response.items || [];
    tableHost.replaceChildren(
      table(
        [
          ["نام مستأجر", "display_name"],
          ["شناسه (Slug)", item => ltr(item.slug)],
          ["وضعیت", item => statusPill(item.status)],
          ["تاریخ ایجاد", item => date(item.created_at)],
          ["سقف روزانه", item => fa(item.daily_max_cost_micros)],
          ["سقف ماهانه", item => fa(item.monthly_max_cost_micros)],
          ["عملیات", item => button("ویرایش سقف", () => openQuotaEditor(item), "btn-outline btn-sm")],
        ],
        rows,
      ),
    );
    if (response.page_info?.next_cursor) {
      tableHost.append(
        button("صفحه بعد", () => refresh(response.page_info.next_cursor).catch(e => status(e.message)), "btn-outline btn-sm"),
      );
    }
  }

  consoleGrid.append(side, centerCol, quotaHost);
  root.append(authWrap, consoleGrid, creationDialog);
}
