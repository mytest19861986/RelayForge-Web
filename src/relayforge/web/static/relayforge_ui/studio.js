import {views, actions} from "./views.js";
import {mountOperator} from "./operator.js";
import {$, el, fa, ltr, date, platform, state, icon, card, button, table, setOptions, confirmAction, download} from "./components.js";

let csrf="", workspace="", current="", eventCursor="", toast, generation=0, refreshPane, mediaTab="";
function status(text) {
  $("status").textContent=text; $("status").hidden=!text;
  clearTimeout(toast); toast=setTimeout(()=>$("status").hidden=true,4500);
}
async function request(path,method="GET",body) {
  const headers={};
  if (method!=="GET") {
    if (!(body instanceof FormData)) headers["Content-Type"]="application/json";
    headers["Idempotency-Key"]=crypto.randomUUID();
    if(csrf) headers["X-CSRFToken"]=csrf;
  }
  const response=await fetch("/api/v1"+path,{method,headers,credentials:"same-origin",
    body:body instanceof FormData || body===undefined ? body : JSON.stringify(body)});
  const value=response.headers.get("content-type")?.includes("json") ? await response.json() : await response.text();
  if(!response.ok) throw Error(value.error?.message_fa || "درخواست انجام نشد.");
  return value;
}
const scoped = path => "/workspaces/"+encodeURIComponent(workspace)+path;
const query = path => request(scoped(path));
const attempt = fn => async (...args) => {try{await fn(...args);}catch(error){status(error.message);}};
async function items(path) {
  const url=scoped(path), records=[];
  let cursor="";
  do {
    const data=await request(url+(url.includes("?")?"&":"?")+new URLSearchParams({limit:100,...(cursor?{cursor}:{})}));
    records.push(...(data.items||[]));
    cursor=data.page_info?.next_cursor || data.next_cursor;
  } while(cursor);
  return records;
}
const label = (item,index) => item.display_name || item.display_title || item.title || item.title_text ||
  item.name || item.label || item.target_channel_ref || item.canonical_chat_id ||
  (item.model_id ? item.model_id : [state(item.job_kind||item.media_kind||item.backup_scope||item.speaker_mode||item.status),
    item.created_at ? date(item.created_at) : fa(index+1)].filter(Boolean).join(" · "));
const collections={
  source_ids:"/sources",source_id:"/sources",destination_ids:"/destinations",secret_id:"/security/secrets",
  manual_provider_id:"/ai/providers",provider_id:"/ai/providers",style_profile_id:"/styles",first:"/styles",
  second:"/styles",asset_ids:"/media/assets",assets:"/media/assets",podcast_script_id:"/podcast-scripts",
  brand_identity_id:"/brands",manifest_id:"/backups",
};
function pickerPath(name,path) {
  if(name!=="id") return collections[name];
  for(const endpoint of ["/channel-intelligence/analyses","/security/secrets","/media/jobs","/destinations","/routes","/styles","/drafts","/deliveries"]) {
    if(path.startsWith(endpoint+"/")) return endpoint;
  }
}
const formatStatus = row => el("span",state(row.status||row.approval_state||row.is_enabled?.toString().toUpperCase()),"pill");
const yesno = value => value ? "فعال" : "غیرفعال";
const bytes = value => value == null ? "—" : fa(Math.round(value/1048576*10)/10)+" مگابایت";
function meter(value,max) {
  const bar=el("progress"); bar.max=Math.max(1,max||0); bar.value=value||0;
  bar.setAttribute("aria-label","پیشرفت"); return bar;
}
function notices(list) {
  if(!list?.length) return el("p","اعلان جدیدی وجود ندارد.","empty");
  const group=el("div");
  for(const item of list) group.append(card(item.title_fa,el("p",item.body_fa),
    el("span",fa(item.occurrence_count)+" بار","pill"),
    ...(item.is_read?[]:[button("خواندم",attempt(async()=>{
      await request(scoped("/notifications/"+item.id+"/read"),"POST",{});
      await refreshDashboard();
      if(!$("notifications").hidden) await openNotifications();
    }))])));
  return group;
}
async function openNotifications() {
  const pane=$("notifications"), activeWorkspace=workspace, notificationItems=await items("/notifications");
  if(activeWorkspace!==workspace) return;
  pane.replaceChildren(el("h2","اعلان‌ها"),notices(notificationItems));
}
function syncDashboard(value) {
  $("engine").textContent=value.emergency_stop?"موتور · متوقف‌شده":"موتور · در حال اجرا";
  $("engine").classList.toggle("stopped",value.emergency_stop);
  $("unread").textContent=fa(value.unread);
  $("stop").disabled=value.emergency_stop;
}
async function refreshDashboard() {
  const key=workspace,value=await query("/dashboard");
  if(workspace!==key) return;
  syncDashboard(value);
  if(current==="dashboard") render(value,"/dashboard");
}
function dashboard(value) {
  syncDashboard(value);
  const metrics=el("div",null,"metrics");
  Object.entries({sources:"منابع فعال",destinations:"مقصدها",routes:"مسیرهای فعال",
    unread:"اعلان‌های خوانده‌نشده",active_media_jobs:"کارهای رسانه"}).forEach(([key,name],index)=>{
    const tile=card(null,icon(index),el("strong",fa(value[key])),el("span",name));
    tile.classList.add("metric"); metrics.append(tile);
  });
  const plan=value.budgets?.policies?.find(item=>item.policy_origin==="OPERATOR_PLAN"&&item.period_kind==="MONTHLY");
  const spend=value.budgets?.settled_spend_micros||0;
  const columns=el("div",null,"cards");
  columns.append(card("بودجه مستأجر",
    el("p",plan ? "سقف ماهانه طرح: "+fa(plan.max_cost_micros)+" میکرو":"سقف طرح در دسترس نیست."),
    el("p","مجموع هزینه ثبت‌شده: "+fa(spend)+" میکرو"),
    ...(plan&&value.budgets.monthly_settled_spend_micros!=null?[meter(value.budgets.monthly_settled_spend_micros,plan.max_cost_micros)]:[])),
    card("اعلان‌های مهم",notices(value.recent_notifications)),
    card("وضعیت دیسک",meter(value.disk_used_percent,100),
      el("p",fa(value.disk_used_percent)+"٪ استفاده · "+bytes(value.disk_free_bytes)+" آزاد"),
      el("p",bytes(value.disk_used_bytes)+" از "+bytes(value.disk_total_bytes)),
      el("span","نگهداری فیزیکی PITR · "+fa(value.pitr_retention_days)+" روز","pill")));
  const node=el("div");node.append(metrics,columns);return node;
}
const listColumns={
  "/sources":[["منبع","display_title"],["کانال",row=>ltr(row.canonical_channel_id)],["نوع",row=>ltr(row.source_kind)],["وضعیت",row=>yesno(row.is_active)]],
  "/destinations":[["مقصد","display_name"],["کانال",row=>ltr(row.canonical_chat_id)],["پیام‌رسان",row=>platform(row.platform)],
    ["رده",row=>el("span",row.platform==="EITAA"?"فقط ارسال خروجی":row.platform.endsWith("_EXPERIMENTAL")?"آزمایشی — نیازمند تست زنده":"فعال GA","pill "+(row.platform.endsWith("_EXPERIMENTAL")?"warning":""))],
    ["آلبوم بومی",row=>row.capabilities_json?.native_albums?"دارد":"—"],["وضعیت",row=>yesno(row.is_enabled)]],
  "/routes":[["مسیر","display_name"],["حالت",row=>ltr(row.route_mode)],["سطح",row=>state(row.automation_level)],
    ["عملیات",row=>button(row.is_enabled?"غیرفعال کردن":"فعال کردن",attempt(async()=>{
      await request(scoped("/routes/"+row.id),"PATCH",{is_enabled:!row.is_enabled}); await refreshPane();
    }))]],
  "/styles":[["الگو",row=>label(row,0)],["نسخه",row=>fa(row.pinned_revision_no)],["دسته","content_category"]],
  "/drafts":[["پیش‌نویس",row=>label(row,0)],["بازبینی",row=>fa(row.current_revision_no)],["وضعیت",formatStatus]],
  "/deliveries":[["وضعیت",formatStatus],["زمان",row=>date(row.scheduled_for)],["تلاش",row=>fa(row.attempts)],
    ["نتیجه",row=>row.last_error_safe||"—"],["عملیات",row=>row.status==="UNCERTAIN"?button("تعیین تکلیف",()=>{
      const form=$("actions").querySelector('[data-reconcile]');
      form.elements.id.value=row.id;form.scrollIntoView({block:"start"});
    }):"—"]],
  "/media/assets":[["نوع","media_kind"],["خاستگاه","origin_kind"],["حجم",row=>bytes(row.byte_size)],["وضعیت",formatStatus]],
  "/media/jobs":[["نوع","job_kind"],["وضعیت",formatStatus],["زمان",row=>date(row.created_at)]],
  "/ai/providers":[["ارائه‌دهنده","display_name"],["نوع",row=>ltr(row.provider_type)],["وضعیت",row=>yesno(row.is_enabled)]],
  "/security/consents":[["دامنه",row=>ltr(row.provider_scope)],["رضایت",row=>row.is_granted?"مجاز":"لغوشده"],["زمان",row=>date(row.updated_at)]],
  "/security/secrets":[["عنوان","label"],["نوع","secret_kind"],["نمای کلید",row=>ltr(row.masked_hint)]],
  "/backups":[["دامنه",row=>state(row.backup_scope)],["زمان",row=>date(row.created_at)],["حجم",row=>bytes(row.byte_size)],["فایل",row=>backupLink(row.id)]],
  "/audit-events":[["عملیات","action_code"],["نقش","actor_role"],["زمان",row=>date(row.created_at)]],
  "/reports/ai-usage":[["مدل",row=>ltr(row.model_id)],["قابلیت","task_modality"],["هزینه",row=>fa(row.cost_micros)],["اجرا",row=>fa(row.executions)]],
  "/channel-intelligence/analyses":[["کانال",row=>ltr(row.target_channel_ref)],["نمونه",row=>fa(row.unique_sample_count)],["وضعیت",formatStatus]],
};
function collection(endpoint,records) {
  const node=el("div");
  const columns=listColumns[endpoint]||[["رسانه",row=>row.photographer||row.author||row.title||label(row,0)],["وضعیت",formatStatus]];
  const search=el("input"); search.type="search"; search.placeholder="جست‌وجو در فهرست";
  search.setAttribute("aria-label",search.placeholder);
  const target=el("div");
  function show(query=""){
    const found=records.filter(row=>Object.values(row).some(value=>typeof value==="string"&&value.toLowerCase().includes(query.toLowerCase())));
    target.replaceChildren(table(columns,found));
  }
  search.oninput=()=>show(search.value); node.append(search,target); show();return node;
}
function facts(items) {
  const list=el("ul");
  for(const entry of items||[]) list.append(el("li",Array.isArray(entry)?entry.join(" · "):entry));
  return list;
}
function contentComparison(data,input) {
  const grid=el("div",null,"comparison");
  grid.append(card("متن ورودی",el("p",input||"—","bubble")));
  for(const result of data.candidates) {
    const diff=result.fact_diff_json||{}, blocked=result.fact_guard_verdict==="FACT_MISMATCH_BLOCKED";
    grid.append(card("خروجی پیشنهادی",el("p",result.text,"bubble"),el("h3","گارد صحت حقایق"),
      el("p",blocked?"مغایرت مسدود شد":result.fact_guard_verdict==="NOT_APPLICABLE_UNRESTRICTED"?"بررسی غیرفعال است":"مغایرت مسدودکننده یافت نشد",blocked?"notice":"pill"),
      el("p","حذف‌شده · "+fa(diff.missing?.length||0)),facts(diff.missing),
      el("p","افزوده‌شده · "+fa(diff.introduced?.length||0)),facts(diff.introduced),
      ...(diff.sensitive_claim_flagged?[el("p","ادعای حساس نیازمند بازبینی است.","notice")]:[])));
  }
  return grid;
}
function previews(rows, actionsData) {
  const grid=el("div",null,"cards");
  for(const row of rows) {
    const target=actionsData?.find(item=>item.id===row.destination_id);
    const payload=row.rendered_payload||row.payload||row.formatting?.payload||{};
    const warnings=row.degradations||row.formatting?.degradations||[];
    grid.append(card(target?platform(target.platform)+" · "+target.display_name:(row.platform?platform(row.platform):"مقصد"),
      el("p",payload.plain_text||payload.text||row.filter_reason||"پیش‌نمایش متنی موجود نیست.","bubble"),
      ...warnings.map(message=>el("p",state(message),"notice"))));
  }
  return grid;
}
function reportAnalysis(data) {
  const stats=data.statistical_summary_json||{}, panel=el("div",null,"cards");
  const split=stats.split||{};
  const train=data.partition_counts?.EXTRACTION ?? stats.extraction_count ?? split.extraction_count;
  const evaluate=data.partition_counts?.EVALUATION ?? stats.evaluation_count ?? split.evaluation_count;
  const counts=table([["شاخص","title"],["تعداد",row=>fa(row.value)]],[
    {title:"درخواستی",value:data.requested_sample_count??stats.requested_sample_count},
    {title:"یکتا",value:data.unique_sample_count??stats.unique_sample_count},
    {title:"استخراج",value:train},{title:"ارزیابی",value:evaluate}]);
  panel.append(card("نمونه‌گیری و تفکیک ۸۰/۲۰",el("div",null,"donut"),counts));
  const identity=data.extracted_identity_json||{};
  panel.append(card("هویت استخراج‌شده",facts(Object.values(identity).flat().filter(value=>typeof value==="string")),
    el("p","هویت کانال ثالث به‌طور پیش‌فرض حذف می‌شود.","notice")));
  const topics=Object.entries(stats.clusters||{});
  if(topics.length){
    panel.append(card("موضوعات نمونه",...topics.map(([title,count])=>card(title,
      meter(count, data.unique_sample_count||100),el("span",fa(count)+" نمونه")))));
  }
  return panel;
}
function render(value,path="",input="",targets=[],mount=$("results")) {
  const base=path.split("?")[0];
  let node;
  if(base==="/dashboard") node=dashboard(value);
  else if(value?.candidates) node=contentComparison(value,input);
  else if(value?.statistical_summary_json) node=reportAnalysis(value);
  else if(value?.destinations) {
    node=el("div");
    if(path.endsWith("/dry-run")) node.append(el("p","۱. انتخاب محتوا ← ۲. اعمال قوانین ← ۳. پیش‌نمایش ← ۴. نتیجه","pill"),el("p",state(value.decision||"")));
    else node.append(el("p","پیش‌نمایش وب جایگزین نمایش در برنامه پیام‌رسان نیست.","notice"));
    node.append(previews(value.destinations,targets));
  } else if(value?.items) node=collection(base,value.items);
  else if(value?.table_counts) node=card("اثر بازیابی در حالت امن",
    table([["بخش","name"],["تعداد",row=>fa(row.count)]],[
      ...Object.entries(value.table_counts).map(([name,count])=>({name:tableNames[name]||name,count})),
      {name:"رسانه",count:value.media_count},{name:"کلیدهای رمزگذاری‌شده",count:value.secret_rekey_count},
    ]),el("p","موتور، مقصدها، مسیرها و دسترسی‌های خارجی در بازیابی متوقف می‌شوند.","notice"));
  else if(value?.policies) node=card("بودجه و سقف طرح",table([["نوع","policy_origin"],["دوره","period_kind"],["سقف (میکرو)",row=>fa(row.max_cost_micros)]],value.policies),
    el("p","هزینه ثبت‌شده: "+fa(value.settled_spend_micros)));
  else if(base.includes("/reports/export")) node=card("دریافت گزارش",button("دانلود",()=>download(
    typeof value==="string"?value:JSON.stringify(value),typeof value==="string"?"report.csv":"report.json","application/octet-stream")));
  else if(base.endsWith("/export")) node=card("بسته الگو",button("دانلود الگو",()=>download(JSON.stringify(value),"style.json","application/json")));
  else if(value?.bundle_base64 || value?.manifest_id) node=card("پشتیبان رمزگذاری‌شده",
    value.bundle_base64?button("دانلود .rfbak",()=>download(Uint8Array.from(atob(value.bundle_base64),char=>char.charCodeAt(0)),"tenant.rfbak","application/octet-stream")):
      backupLink(value.manifest_id));
  else if(value?.latency_ms!=null) node=card("نتیجه آزمون مدل",el("p",fa(value.latency_ms)+" میلی‌ثانیه"),el("span",state(value.status),"pill"));
  else if(value?.webhook_token) node=card("اتصال وب‌هوک",el("p","کلید ثبت وب‌هوک را همین حالا در تنظیمات ربات وارد کنید."),ltr(value.webhook_token));
  else node=card("نتیجه",el("p",value?.status?state(value.status):"درخواست انجام شد."));
  mount.replaceChildren(node);
}
function backupLink(identifier){
  const link=el("a","دریافت پشتیبان ثبت‌شده","button secondary");
  link.href="/api/v1"+scoped("/backups/"+encodeURIComponent(identifier)+"/download");
  return link;
}
const tableNames={drafts:"پیش‌نویس‌ها",draft_revisions:"بازبینی‌ها",brand_identities:"برندها",routes:"مسیرها",sources:"منابع",destinations:"مقصدها",media_assets:"دارایی‌های رسانه"};
function archiveWizard() {
  const form = document.createElement("form");
  const heading = document.createElement("strong");
  heading.textContent = "بارگذاری خروجی JSON از Telegram Desktop";
  const notice = document.createElement("p");
  notice.textContent = "Bot API آرشیو پیام‌های قبلی کانال را دریافت نمی‌کند.";
  const source = el("select");
  source.setAttribute("aria-label","منبع آرشیو");
  setOptions(source, [], label);
  items("/sources").then(rows=>setOptions(source,rows.filter(item=>item.source_kind==="TELEGRAM_DESKTOP_EXPORT"),label)).catch(error=>status(error.message));
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
      render(await request(path + "/" + job.export_job_id + "/complete", "POST", {}),"/sources");
    } catch (error) { status(error.message); }
    finally { button.disabled = false; }
  };
  return form;
}


function operation([heading,path,method,fields,serialize,needsConfirm], load) {
  const form=el("form"), output=el("div"), advanced=el("details");
  advanced.append(el("summary","تنظیمات پیشرفته"));
  form.append(el("h3",heading));
  if(path.endsWith("/reconcile")) form.dataset.reconcile="";
  const controls=new Map(), records=new Map();
  const action=el("button",heading,heading.includes("آزمایشی")?"amber":heading.includes("فنی")?"violet":heading.includes("حذف حساب")?"danger":"");
  for(const [name,caption,kind,optional] of fields){
    if(kind==="rows"){
      const grid=el("fieldset");
      grid.append(el("legend",caption));
      function addRow(){
        const row=el("div",null,"infographic-row");
        for(const [name,title] of [["row_title","عنوان"],["row_value","مقدار"]]){
          const wrap=el("label",title), input=el("input");
          input.name=name;input.maxLength=140;input.required=true;
          wrap.append(input);row.append(wrap);
        }
        row.append(button("حذف سطر",()=>{if(grid.querySelectorAll(".infographic-row").length>1)row.remove();}));
        grid.insertBefore(row,add);
      }
      const add=button("افزودن سطر",()=>{if(grid.querySelectorAll(".infographic-row").length<12)addRow();});
      grid.append(add);addRow();form.append(grid);continue;
    }
    if(kind==="derived") continue;
    const wrapper=el("label",caption);
    if(kind==="freedom" || name==="resolution_action") {
      const fieldset=el("fieldset",null,kind==="freedom"?"freedoms":"");
      fieldset.append(el("legend",caption));
      const values=kind==="freedom"?["STRICT","BALANCED","CREATIVE","UNRESTRICTED"]:kind;
      values.forEach((value,index)=>{
        const row=el("label"), input=el("input");
        input.type="radio";input.name=name;input.value=value;
        input.checked=kind==="freedom"?value==="BALANCED":index===0;
        row.append(input,el("span",state(value)));fieldset.append(row);
      });
      form.append(fieldset);continue;
    }
    const isPicker=["pick","multi"].includes(kind);
    const input=el(isPicker||Array.isArray(kind)?"select":kind==="textarea"?"textarea":"input");
    input.name=name;input.required=!optional&&kind!=="checkbox";
    controls.set(name,input);
    wrapper.append(input);
    if(kind==="multi") input.multiple=true;
    if(isPicker){
      setOptions(input,[],label);
      const endpoint=pickerPath(name,path);
      if(endpoint){
        const filter=rows=>rows.filter(row=>{
          if(path.endsWith("/reconcile")) return row.status==="UNCERTAIN";
          if(name==="podcast_script_id") return ["APPROVED","READY"].includes(row.status);
          if(path.endsWith("/test-publish")) return !row.is_archived;
          return true;
        });
        load(endpoint).then(rows=>{
          records.set(name,filter(rows));
          setOptions(input,records.get(name),label);
          input.dispatchEvent(new Event("change"));
        }).catch(error=>status(error.message));
      }
    }else if(Array.isArray(kind)){
      kind.forEach(value=>input.add(new Option(platform(state(value)),value)));
    }else if(input.tagName==="INPUT"){
      input.type=kind.endsWith("-file")?"file":kind;
      if(input.type==="password") input.autocomplete="off";
      if(kind==="upload-file") input.accept="image/*,video/*,audio/*";
      if(kind==="json-file") input.accept=".json,application/json";
      if(kind==="backup-file") input.accept=".rfbak,application/octet-stream";
      if(kind==="number"){input.min="0";input.step="1";}
      if(kind==="range"){
        input.min="0";input.max="100";input.value="70";
        const complementary=el("output");
        const sync=()=>{complementary.textContent=fa(input.value)+"٪ / "+fa(100-Number(input.value))+"٪";};
        input.oninput=sync;sync();wrapper.append(complementary);
      }
    }
    (optional==="advanced"?advanced:form).append(wrapper);
  }
  if(advanced.children.length>1) form.append(advanced);
  form.append(action,output);
  const provider=controls.get("provider_id")||controls.get("manual_provider_id");
  const model=controls.get("model_id")||controls.get("manual_model_id");
  const modality=controls.get("modality");
  if(provider&&model?.tagName==="SELECT"){
    async function models(refresh=false) {
      const selected=provider.value, type=modality?.value||(path==="/media/jobs"?"TTS":"TEXT");
      setOptions(model,[],label);
      if(!selected) return;
      const result=await query("/ai/providers/"+encodeURIComponent(selected)+"/models?"+
        new URLSearchParams({modality:type,...(refresh?{refresh:"true"}:{})}));
      if(provider.value===selected && (!modality||modality.value===type))
        setOptions(model,result.items.map(item=>({...item,id:item.model_id})),item=>item.model_id);
    }
    provider.addEventListener("change",attempt(()=>models()));
    modality?.addEventListener("change",attempt(()=>models()));
    (advanced.parentElement ? advanced : form).insertBefore(
      button("به‌روزرسانی مدل‌ها",attempt(()=>models(true))),advanced.parentElement ? null : action);
  }
  const draft=controls.get("id");
  if(path.startsWith("/drafts/")&&method==="PATCH") draft.addEventListener("change",attempt(async()=>{
    const id=draft.value;if(!id) return;
    const record=await query("/drafts/"+encodeURIComponent(id));
    if(draft.value!==id) return;
    controls.get("body_text").value=record.body_text;
    controls.get("title_text").value=record.title_text||"";
    controls.get("brand_identity_id").value=record.brand_identity_id||"";
    for(const option of controls.get("assets").options) option.selected=(record.media_manifest_json||[]).includes(option.value);
  }));
  if(path.endsWith("/reconcile")){
    const ack=controls.get("acknowledge_duplicate_risk");
    const sync=()=>{action.disabled=!draft.value || (form.elements.resolution_action.value==="CONFIRM_NOT_SENT_REQUEUE"&&!ack.checked);};
    form.addEventListener("change",sync);sync();
  }
  let restorePreview;
  form.onsubmit=async event=>{
    event.preventDefault();
    const raw=new FormData(form), values=Object.fromEntries(raw);
    const owner=workspace, activeView=current;
    for(const [name,control] of controls) if(control.multiple) values[name]=raw.getAll(name);
    if(raw.has("row_title"))values.table_rows=raw.getAll("row_title").map((title,index)=>[title,raw.getAll("row_value")[index]]);
    const selected=(records.get("id")||[]).find(row=>row.id===values.id);
    if(selected){
      values.revision=selected.current_revision_no;
      values.expected_lease_epoch=selected.lease_epoch;
    }
    if(needsConfirm && !(path.endsWith("/restore")&&values.mode==="PREVIEW") &&
      !await confirmAction(heading,path.includes("delete-account")?"حذف حساب برگشت‌ناپذیر است. داده‌ها تا ۱۴ روز در نسخه‌های فیزیکی PITR می‌مانند.":undefined)) return;
    if(owner!==workspace || activeView!==current) return;
    action.disabled=true;
    form.querySelectorAll('input[type="password"]').forEach(input=>{input.value="";});
    try{
      for(const [name,,kind] of fields){
        const file=values[name];
        if(!kind.endsWith?.("-file")) continue;
        if(!file?.size){delete values[name];continue;}
        if(kind==="upload-file") continue;
        if(file.size>524288) throw Error("فایل برای ورود مستقیم بزرگ است. پشتیبان‌های بزرگ را از فهرست انتخاب کنید.");
        values[name]=kind==="json-file"?JSON.parse(await file.text()):btoa(Array.from(new Uint8Array(await file.arrayBuffer()),byte=>String.fromCharCode(byte)).join(""));
      }
      if(path.endsWith("/restore")){
        const file=controls.get("bundle_base64").files[0];
        const choice=values.manifest_id || file;
        if(values.mode==="APPLY" && (!choice || choice!==restorePreview)) throw Error("ابتدا اثر همین پشتیبان را پیش‌نمایش کنید.");
      }
      if(path.endsWith("/reconcile")&&values.resolution_action==="CONFIRM_NOT_SENT_REQUEUE"&&values.acknowledge_duplicate_risk!=="on")
        throw Error("ریسک ارسال تکراری را تأیید کنید.");
      const resolved=path.replace(/\{([a-z_]+)\}/g,(_,key)=>encodeURIComponent(values[key]||""));
      const endpoint=resolved.startsWith("!")?resolved.slice(1):scoped(resolved);
      const value=await request(endpoint,method,method==="GET"?undefined:serialize(values));
      if(owner!==workspace || activeView!==current) return;
      const destinations=value?.destinations?await load("/destinations"):undefined;
      render(value,resolved,values.raw_text||values.media_kind,destinations,output);
      if(path.endsWith("/restore")&&values.mode==="PREVIEW")
        restorePreview=values.manifest_id||controls.get("bundle_base64").files[0];
      if(path.includes("delete-account")) { location.assign("/"); return; }
      if(method!=="GET"&&path!=="/backups/restore") await refreshPane();
      status("عملیات انجام شد.");
    }catch(error){status(error instanceof SyntaxError?"فایل الگو معتبر نیست.":error.message);}
    finally{action.disabled=false; if(path.endsWith("/reconcile")) form.dispatchEvent(new Event("change"));}
  };
  form.refreshPickers=async()=>{
    await Promise.all([...records.keys()].map(async name=>{
      const rows=await load(pickerPath(name,path));
      const filtered=rows.filter(row=>path.endsWith("/reconcile")?row.status==="UNCERTAIN":
        name==="podcast_script_id"?["APPROVED","READY"].includes(row.status):true);
      records.set(name,filtered);setOptions(controls.get(name),filtered,label);
    }));
  };
  return form;
}

async function show(name) {
  const [key,heading,endpoint]=views.find(item=>item[0]===name)||views[0], stamp=++generation;
  current=key;
  mediaTab="کتابخانه رسانه";
  $("title").textContent=heading;
  $("delivery-filters")?.remove();
  const cache=new Map();
  const load=path=>{
    if(!cache.has(path)) cache.set(path,items(path).catch(error=>{cache.delete(path);throw error;}));
    return cache.get(path);
  };
  const forms=(actions[key]||[]).filter(action=>key!=="dashboard"||action[1]!=="/ops/emergency-stop").map(action=>operation(action,load));
  $("actions").replaceChildren(...forms);
  $("results").replaceChildren(el("p","در حال بارگذاری…","empty"));
  document.querySelectorAll("nav a").forEach(link=>{
    if(link.dataset.view===key) link.setAttribute("aria-current","page");
    else link.removeAttribute("aria-current");
  });
  $("sidebar").classList.remove("open");$("menu").setAttribute("aria-expanded","false");
  if(key==="sources") $("actions").append(archiveWizard());
  if(key==="drafts") $("actions").prepend(el("p","پیش‌نمایش وب جایگزین نمایش در برنامه پیام‌رسان نیست.","notice"));
  if(key==="operations") $("actions").prepend(el("p","نسخه‌های فیزیکی PITR تا ۱۴ روز نگهداری می‌شوند.","notice"));
  if(key==="media"){
    const tabs=el("div",null,"toolbar");
    for(const [tab,indexes] of [["کتابخانه رسانه",[8]],["جست‌وجوی مجوزدار",[0]],["اینفوگرافیک",[1]],
      ["استیکر و کلیپ",[2,3]],["پادکست",[4,5]],["کارها",[6,7]]]){
      tabs.append(button(tab,attempt(async()=>{
        mediaTab=tab;
        forms.forEach((form,index)=>form.hidden=!indexes.includes(index));
        if(tab==="کارها"){
          const result=await query("/media/jobs");
          if(stamp===generation) render(result,"/media/jobs");
        }else if(tab==="کتابخانه رسانه") await refreshPane();
      })));
    }
    $("actions").before(tabs);
    tabs.id="media-tabs";forms.forEach((form,index)=>form.hidden=index!==8);
  }
  document.querySelectorAll("#media-tabs").forEach((node,index,array)=>{if(key!=="media"||index<array.length-1)node.remove();});
  const effective=endpoint||(key==="intelligence"?"/channel-intelligence/analyses":null);
  let deliveryStatus="";
  if(key==="delivery"){
    const filters=el("div",null,"toolbar");filters.id="delivery-filters";
    for(const [name,caption] of [["","همه"],["UNCERTAIN","نامطمئن"],["FAILED","ناموفق"],["SENT","ارسال‌شده"],["SCHEDULED","زمان‌بندی‌شده"],["READY","آماده"]]){
      const filter=button(caption,attempt(async()=>{
        deliveryStatus=name;
        filters.querySelectorAll("button").forEach(item=>item.setAttribute("aria-pressed",String(item===filter)));
        await refreshPane();
      }));
      filter.setAttribute("aria-pressed",String(!name));filters.append(filter);
    }
    $("results").before(filters);
  }
  refreshPane=async ()=>{
    cache.clear();
    if(effective){
      const path=key==="media"&&mediaTab==="کارها"?"/media/jobs":effective;
      const listing=path==="/deliveries"&&deliveryStatus?path+"?status="+encodeURIComponent(deliveryStatus):path;
      const value=path==="/dashboard"?await query(path):{items:await load(listing)};
      if(stamp===generation) render(value,path);
    }
    if(stamp===generation) await Promise.allSettled(forms.map(form=>form.refreshPickers()));
  };
  try{await refreshPane();}catch(error){if(stamp===generation)$("results").replaceChildren(el("p",error.message,"notice"));}
}
if(location.pathname.replace(/\/$/,"")==="/operator") mountOperator(status);
const nav=document.querySelector("nav");
for(const [index,[key,title]] of views.entries()){
  const link=el("a");link.href="/app/"+key;link.dataset.view=key;
  link.append(icon(index),document.createTextNode(title));
  link.onclick=event=>{event.preventDefault();history.pushState({},"",link.href);show(key);};
  nav.append(link);
}
$("bell-icon").append(icon(12));
$("bell").onclick=attempt(async()=>{
  const drawer=$("notifications");
  drawer.hidden=!drawer.hidden;$("bell").setAttribute("aria-expanded",String(!drawer.hidden));
  if(!drawer.hidden) await openNotifications();
});
$("stop").onclick=attempt(async()=>{
  if(!await confirmAction("توقف اضطراری","ارسال‌های در انتظار لغو می‌شوند و موتور این فضا متوقف می‌شود."))return;
  await request(scoped("/ops/emergency-stop"),"POST",{enabled:true,cancel_waiting:true});
  await refreshDashboard();
});
$("menu").onclick=()=>{$("menu").setAttribute("aria-expanded",String($("sidebar").classList.toggle("open")));};
$("eye").onclick=()=>{
  const input=$("login").elements.password, visible=input.type==="password";
  input.type=visible?"text":"password";$("eye").textContent=visible?"پنهان کردن گذرواژه":"نمایش گذرواژه";
  $("eye").setAttribute("aria-pressed",String(visible));
};
window.onpopstate=()=>{if(workspace)show(location.pathname.split("/").pop());};
$("workspace").onchange=attempt(async event=>{
  workspace=event.target.value;eventCursor="";$("notifications").hidden=true;
  await show(current);await refreshDashboard();
});
$("logout").onclick=attempt(async()=>{
  await request("/auth/logout","POST");
  csrf="";workspace="";location.assign("/");
});
$("login").onsubmit=attempt(async event=>{
  event.preventDefault();
  const form=event.target,data=Object.fromEntries(new FormData(form));
  if(!data.tenant_slug)delete data.tenant_slug;
  form.elements.password.value="";
  const session=await request("/auth/login","POST",data);
  csrf=session.csrf_token;workspace=session.default_workspace_id;
  const response=await request("/workspaces");
  setOptions($("workspace"),response.items,label,false);
  $("workspace").value=workspace;
  $("entry").hidden=true;$("studio").hidden=false;$("topbar").hidden=false;
  history.replaceState({},"","/app/dashboard");await show("dashboard");
});
async function poll(){
  try{
    if(workspace&&!document.hidden){
      const active=workspace,data=await query("/events/poll"+(eventCursor?"?cursor="+encodeURIComponent(eventCursor):""));
      if(active===workspace){
        if(data.items?.length){
          eventCursor=data.items.at(-1).event_id;await refreshDashboard();
          if(current==="delivery" || (current==="media"&&mediaTab==="کارها")) await refreshPane();
        }
      }
    }
  }catch(error){status(error.message);}
  finally{setTimeout(poll,12000);}
}
setTimeout(poll,12000);
