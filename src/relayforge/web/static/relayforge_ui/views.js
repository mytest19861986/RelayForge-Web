export const views = [
  ["dashboard", "نمای کلی", "/dashboard"],
  ["sources", "منابع و استخراج آرشیو", "/sources"],
  ["destinations", "مقصدها و ماتریس قابلیت", "/destinations"],
  ["routes", "قوانین، مسیرها و شبیه‌سازی", "/routes"],
  ["content", "استودیو تولید محتوا", "/drafts"],
  ["intelligence", "کانال‌شناس هوشمند", null],
  ["styles", "کتابخانه الگوها", "/styles"],
  ["media", "استودیو رسانه و پادکست", "/media/assets"],
  ["ai", "تنظیمات هوش مصنوعی و بودجه", "/ai/providers"],
  ["drafts", "پیش‌نویس، پیش‌نمایش مقصد و انتشار", "/drafts"],
  ["delivery", "صف تحویل و میز آشتی UNCERTAIN", "/deliveries"],
  ["operations", "عملیات، ممیزی و پشتیبان مستأجر", "/audit-events"]
];

const id = ["id", "شناسه", "text"];
const json = ["spec", "تنظیمات JSON", "textarea"];
export const actions = {
  sources: [
    ["ثبت منبع ربات","/sources","POST",[
      ["source_kind","نوع منبع",["WEBHOOK_INBOUND","TELEGRAM_DESKTOP_EXPORT"]],
      ["canonical_channel_id","شناسه کانال","text"],["display_title","عنوان","text"],
    ],v=>v.source_kind==="WEBHOOK_INBOUND"?{...v,webhook_platform:"telegram"}:v],
  ],
  dashboard: [["توقف اضطراری", "/ops/emergency-stop", "POST", [], () => ({enabled:true,cancel_waiting:true}), true]],
  destinations: [
    ["تست فنی اتصال", "/destinations/{id}/test-connection", "POST", [id], () => ({})],
    ["ارسال پست آزمایشی واقعی", "/destinations/{id}/test-publish", "POST", [id,json], v => ({...JSON.parse(v.spec),explicit_user_confirmation:true}), true]
  ],
  routes: [["شبیه‌سازی مسیر", "/routes/{id}/dry-run", "POST", [id,["sample_text","متن نمونه","textarea"]], v => ({sample_text:v.sample_text})]],
  content: [["تولید محتوا", "/studio/generate", "POST",
    [["raw_text","متن منبع","textarea"],["freedom_mode","آزادی بازنویسی",["STRICT","BALANCED","CREATIVE","UNRESTRICTED"]]],
    v => ({input_mode:"MANUAL_TEXT",raw_text:v.raw_text,task_type:"REWRITE",freedom_mode:v.freedom_mode,routing_mode:"AUTO_ECONOMY"})]],
  intelligence: [["آغاز تحلیل","/channel-intelligence/analyses","POST",[json],v=>JSON.parse(v.spec)],["گزارش تحلیل", "/channel-intelligence/analyses/{id}", "GET", [id]]],
  styles: [["ترکیب وزن‌دار","/styles/compose","POST",[json],v=>JSON.parse(v.spec)],["واردکردن الگو","/styles/import","POST",[json],v=>JSON.parse(v.spec)],["آزمایش الگو","/styles/{id}/try","POST",[id,json],v=>JSON.parse(v.spec)],["خروجی الگو","/styles/{id}/export","GET",[id]]],
  media: [["جستجوی تصاویر مجاز","/media/search","POST",[["text","موضوع","text"]],v=>({text:v.text})],
    ["ایجاد کار رسانه","/media/jobs","POST",[json],v=>JSON.parse(v.spec)],
    ["ذخیره متن پادکست","/podcast-scripts","POST",[json],v=>JSON.parse(v.spec)]],
  ai: [["ثبت امن کلید","/ai/providers","PUT",[
      ["provider_type","نوع ارائه‌دهنده",["OPENAI","GEMINI","COMPATIBLE_OPENAI"]],
      ["display_name","نام","text"],["api_key","کلید API","password"]
    ],v=>v],
    ["رضایت دسترسی خارجی","/security/consents","PUT",[
      ["provider_scope","دامنه رضایت",["OPENAI_TEXT","OPENAI_IMAGE","OPENAI_AUDIO","OPENAI_VISION","GEMINI_TEXT","GEMINI_IMAGE","GEMINI_AUDIO","GEMINI_VISION","GEMINI_VIDEO","STOCK_MEDIA_SEARCH"]],
      ["is_granted","اجازه می‌دهم","checkbox"]
    ],v=>({...v,is_granted:v.is_granted==="on"})],
    ["بودجه و سقف طرح","/ai/budgets","GET",[]],
    ["آزمون مصنوعی مدل","/ai/models/probe","POST",
      [["provider_id","شناسه ارائه‌دهنده","text"],["model_id","مدل","text"],["modality","قابلیت",["TEXT","VISION","IMAGE","TTS","VIDEO"]]],
      v=>({...v,explicit_token_spend_confirmation:true}),true]],
  drafts: [["ذخیره پیش‌نویس","/drafts","POST",[["body_text","متن","textarea"]], v=>v],
    ["پیش‌نمایش مقصد","/drafts/{id}/preview","POST",[id,json],v=>JSON.parse(v.spec)],
    ["انتشار نهایی","/drafts/{id}/publish","POST",[id,json],v=>({...JSON.parse(v.spec),mode:"LIVE_PUBLISH",explicit_user_confirmation:true}),true]],
  delivery:[["آشتی تحویل","/deliveries/{id}/reconcile","POST",[id,json,["duplicate","ریسک ارسال تکراری را می‌پذیرم","checkbox"]],v=>({...JSON.parse(v.spec),acknowledge_duplicate_risk:v.duplicate==="on"}),true]],
  operations:[
    ["پاکسازی فایل‌های منقضی و بدون مرجع","/maintenance/cleanup","POST",[],()=>({}),true],
    ["گزارش JSON","/reports/export?format=json","GET",[]],
    ["گزارش CSV","/reports/export?format=csv","GET",[]],
    ["پشتیبان رمزگذاری‌شده","/backups","POST",[["backup_password","گذرواژه پشتیبان","password"]],v=>v],
    ["بازنشانی امن","/ops/reset","POST",[["backup_password","گذرواژه پشتیبان","password"]],v=>v,true],
    ["بازیابی پشتیبان","/backups/restore","POST",
      [["backup_password","گذرواژه پشتیبان","password"],["bundle_base64","محتوای رمزگذاری‌شده","textarea"],["mode","عملیات",["PREVIEW","APPLY"]]],v=>v,true]
  ]
};
