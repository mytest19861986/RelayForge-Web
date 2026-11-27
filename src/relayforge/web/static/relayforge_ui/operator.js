import {$, el, button, card, table, fa, ltr, statusPill, date, confirmAction} from "./components.js";

export function mountOperator(status) {
  $("entry").hidden = true;
  const root = $("operator");
  root.hidden = false;
  let token = "";
  let renderVersion = 0;
  const banner = el("header",null,"operator-banner");
  const home = el("a","بازگشت به ورود استودیو");
  home.href="/";
  banner.append(el("h1","پنل اپراتور پلتفرم"),home);
  root.append(banner);

  async function api(path, method="GET", data) {
    const headers = {Authorization:"Bearer " + token};
    if (method!=="GET") {
      headers["Content-Type"]="application/json";
      headers["Idempotency-Key"]=crypto.randomUUID();
    }
    const result = await fetch("/api/v1/operator" + path,{method,headers,credentials:"omit",
      body:data === undefined ? undefined : JSON.stringify(data)});
    if (!result.ok) throw Error((await result.json()).error?.message_fa || "درخواست اپراتور انجام نشد.");
    return result.json();
  }
  function form(fields,label,submit) {
    const box=el("form");
    for (const [name,title,type,value] of fields) {
      const wrap=el("label",title), input=el("input");
      input.name=name; input.type=type; input.required=true;
      input.value=value ?? "";
      if(type==="password") input.autocomplete="off";
      if(type==="number"){input.min="0"; input.step="1";}
      wrap.append(input); box.append(wrap);
    }
    const action=el("button",label);
    box.append(action);
    box.onsubmit=async event=>{
      event.preventDefault();
      action.disabled=true;
      const value=Object.fromEntries(new FormData(box));
      box.querySelectorAll('input[type="password"]').forEach(item=>{item.value="";});
      try{ await submit(value); }catch(error){status(error.message);}
      finally{action.disabled=false;}
    };
    return box;
  }
  const auth=form([["token","کلید دسترسی اپراتور","password"]],"ورود به پنل",async values=>{
    token=values.token;
    try{await refresh();}catch(error){token="";throw error;}
    login.hidden=true;
    consolePane.hidden=false;
  });
  const login=card("ورود به پنل اپراتور",auth);
  const consolePane=el("div");
  consolePane.hidden=true;
  root.append(login,consolePane);
  const toolbar=el("div",null,"toolbar"), search=el("input"), list=el("div"), editor=el("div");
  search.type="search"; search.placeholder="جست‌وجو با نام یا نامک مستأجر";
  search.setAttribute("aria-label",search.placeholder);
  search.onchange=()=>refresh().catch(error=>status(error.message));
  const creationDialog=el("dialog");
  const create=form([["display_name","نام سازمان","text"],["slug","نامک سازمان","text"],
    ["owner_email","ایمیل مالک","email"],["owner_display_name","نام مالک","text"],
    ["owner_password","گذرواژه اولیه مالک","password"]],"ایجاد مستأجر",async item=>{
    await api("/tenants","POST",item);
    creationDialog.close();
    status("مستأجر و فضای کاری پیش‌فرض ایجاد شدند.");
    await refresh();
  });
  creationDialog.append(el("h2","ایجاد مستأجر جدید"),create,button("انصراف",()=>creationDialog.close()));
  toolbar.append(search,button("ایجاد مستأجر جدید",()=>creationDialog.showModal(),""),
    button("خروج",()=>{token=""; renderVersion++; consolePane.hidden=true; list.replaceChildren(); editor.replaceChildren(); login.hidden=false;}));
  consolePane.append(toolbar,el("p","ماندگاری نسخه‌های فیزیکی PITR تا ۱۴ روز است.","notice"),list,editor,creationDialog);

  function quota(item) {
    editor.replaceChildren(card("سقف طرح · " + item.display_name, form([
      ["daily_max_cost_micros","سقف روزانه (میکرو)","number",item.daily_max_cost_micros],
      ["monthly_max_cost_micros","سقف ماهانه (میکرو)","number",item.monthly_max_cost_micros],
    ],"ذخیره سقف طرح",async value=>{
      if (!await confirmAction("به‌روزرسانی سقف طرح",item.display_name)) return;
      await api("/tenants/"+encodeURIComponent(item.id)+"/plan-quotas","PUT",{
        daily_max_cost_micros:Number(value.daily_max_cost_micros),
        monthly_max_cost_micros:Number(value.monthly_max_cost_micros),
      });
      status("سقف طرح به‌روز شد.");
      await refresh();
    })));
    editor.scrollIntoView({block:"nearest"});
  }
  async function refresh(cursor="") {
    const generation=++renderVersion;
    const response=await api("/tenants?"+new URLSearchParams({q:search.value,...(cursor?{cursor}:{})}));
    if(generation!==renderVersion) return;
    const rows=response.items;
    list.replaceChildren(table([["نام مستأجر","display_name"],["نامک",item=>ltr(item.slug)],
      ["وضعیت",item=>statusPill(item.status)],["تاریخ ساخت",item=>date(item.created_at)],
      ["سقف روزانه",item=>fa(item.daily_max_cost_micros)],["سقف ماهانه",item=>fa(item.monthly_max_cost_micros)],
      ["عملیات",item=>button("ویرایش سقف",()=>quota(item))]],rows));
    if(response.page_info?.next_cursor) list.append(button("صفحه بعد",()=>refresh(response.page_info.next_cursor).catch(error=>status(error.message))));
    if(cursor) list.append(button("صفحه اول",()=>refresh().catch(error=>status(error.message))));
  }
}
