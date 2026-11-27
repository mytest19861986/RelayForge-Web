export const views = [
  ["dashboard","نمای کلی","/dashboard"], ["sources","منابع و استخراج آرشیو","/sources"],
  ["destinations","مقصدها و ماتریس قابلیت","/destinations"],
  ["routes","قوانین، مسیرها و شبیه‌سازی","/routes"],
  ["content","استودیو تولید محتوا","/drafts"], ["intelligence","کانال‌شناس هوشمند",null],
  ["styles","کتابخانه الگوها","/styles"], ["media","استودیو رسانه و پادکست","/media/assets"],
  ["ai","تنظیمات هوش مصنوعی و بودجه","/ai/providers"],
  ["drafts","پیش‌نویس، پیش‌نمایش مقصد و انتشار","/drafts"],
  ["delivery","صف تحویل و میز آشتی UNCERTAIN","/deliveries"],
  ["operations","عملیات، ممیزی و پشتیبان مستأجر","/audit-events"],
];
const field = (name,label,kind="text",optional=false) => [name,label,kind,optional];
const id = field("id","انتخاب رکورد","pick");
const title = field("title","عنوان");
const slug = field("slug","شناسه لاتین");
const content = field("body_text","متن","textarea");
const platforms = ["TELEGRAM","BALE","EITAA","RUBIKA_EXPERIMENTAL","SOROUSH_EXPERIMENTAL"];
const scope = ["TENANT_SETTINGS_EXPORT","TENANT_FULL_LOGICAL"];
const ids = value => Array.isArray(value) ? value : value ? [value] : [];
const targets = [
  field("destination_ids","مقصدهای انتشار","multi"),
  field("album_fallback_policy","سیاست آلبوم",["SEQUENTIAL_POSTS","CANCEL_FOR_INCOMPATIBLE_DEST"]),
];
const preview = v => ({destination_ids:ids(v.destination_ids),album_fallback_policy:v.album_fallback_policy});
const publish = mode => v => ({
  ...preview(v), mode, expected_draft_revision_no:Number(v.revision), explicit_user_confirmation:true,
});
const backupFields = [
  field("backup_password","گذرواژه پشتیبان","password"),
  field("backup_scope","دامنه پشتیبان",scope),
  field("include_media_assets","فایل‌های رسانه را ضمیمه کن","checkbox"),
  field("includes_reencrypted_secrets","اسرار را با کلید پشتیبان دوباره رمزگذاری کن","checkbox"),
];
const backup = v => ({
  ...v, include_media_assets:v.include_media_assets==="on",
  includes_reencrypted_secrets:v.includes_reencrypted_secrets==="on",
});
const media = kind => v => ({
  job_kind:kind, asset_ids:ids(v.asset_ids || ""),
  ...(v.title ? {title:v.title} : {}), ...(v.caption ? {caption:v.caption} : {}),
});
const choices = () => [...targets,field("revision","بازبینی پیش‌نویس","derived")];
export const actions = {
  dashboard: [["توقف اضطراری","/ops/emergency-stop","POST",[],()=>({enabled:true,cancel_waiting:true}),true]],
  sources: [["ثبت منبع ربات","/sources","POST",[
    field("source_kind","نوع منبع",["WEBHOOK_INBOUND","TELEGRAM_DESKTOP_EXPORT"]),
    field("canonical_channel_id","شناسه کانال"),field("display_title","عنوان کانال"),
  ],v=>v.source_kind==="WEBHOOK_INBOUND"?{...v,webhook_platform:"telegram"}:v]],
  destinations: [
    ["ثبت مقصد","/destinations","POST",[
      field("platform","پیام‌رسان",platforms),field("display_name","نام مقصد"),
      field("canonical_chat_id","شناسه کانال مقصد"),field("secret_id","کلید خزانه","pick"),
    ],v=>v],
    ["تست فنی اتصال","/destinations/{id}/test-connection","POST",[id],()=>({})],
    ["ارسال پست آزمایشی واقعی","/destinations/{id}/test-publish","POST",[id,content],
      v=>({text:v.body_text,explicit_user_confirmation:true}),true],
  ],
  routes: [
    ["ساخت مسیر","/routes","POST",[
      slug,field("display_name","نام مسیر"),field("source_ids","منابع","multi"),
      field("destination_ids","مقصدها","multi"),field("route_mode","حالت",["COPY","RULES","AI","HYBRID"]),
      field("automation_level","سطح خودکارسازی",["DRAFT_ONLY","APPROVAL_REQUIRED","AUTO_PUBLISH_BY_POLICY"]),
      field("keyword","عبارت فیلتر","text",true),
      field("catch_up","سیاست تکمیل عقب‌ماندگی",["HOLD_FOR_REVIEW","GRADUAL_THROTTLED","SKIP_EXPIRED"],"advanced"),
      field("schedule","زمان شروع (ISO 8601 با اختلاف زمانی)","text","advanced"),
    ],v=>({slug:v.slug,display_name:v.display_name,source_ids:ids(v.source_ids),destination_ids:ids(v.destination_ids),
      route_mode:v.route_mode,automation_level:v.automation_level,schedule_policy:{catch_up:v.catch_up,...(v.schedule?{mode:"AT_TIMESTAMP",at_timestamp:v.schedule}:{})},filter_ast:v.keyword?{op:"regex_match",value:v.keyword}:{}})],
    ["شبیه‌سازی مسیر","/routes/{id}/dry-run","POST",[id,field("sample_text","متن نمونه","textarea")],v=>({sample_text:v.sample_text})],
  ],
  content: [["تولید محتوا","/studio/generate","POST",[
    field("raw_text","متن منبع","textarea"),field("freedom_mode","آزادی بازنویسی","freedom"),
    field("routing_mode","انتخاب مدل",["AUTO_ECONOMY","AUTO_QUALITY","MANUAL"],"advanced"),
    field("manual_provider_id","ارائه‌دهنده","pick","advanced"),
    field("manual_model_id","مدل","pick","advanced"),
    field("style_profile_id","الگوی نگارش","pick","advanced"),
  ],v=>({...Object.fromEntries(Object.entries(v).filter(([,value])=>value)),
    input_mode:"MANUAL_TEXT",task_type:"REWRITE",routing_mode:v.routing_mode||"AUTO_ECONOMY"})]],
  intelligence: [
    ["آغاز تحلیل","/channel-intelligence/analyses","POST",[field("source_id","منبع","pick"),
      field("requested_sample_count","تعداد نمونه","number",true)],
    v=>({source_id:v.source_id,requested_sample_count:Number(v.requested_sample_count||120)})],
    ["گزارش تحلیل","/channel-intelligence/analyses/{id}","GET",[id]],
  ],
  styles: [
    ["ساخت الگو","/styles","POST",[slug,title],v=>v],
    ["ترکیب وزن‌دار","/styles/compose","POST",[slug,title,field("first","الگوی اول","pick"),
      field("weight","وزن دو الگو","range"),field("second","الگوی دوم","pick")],
    v=>({slug:v.slug,title:v.title,styles:[{style_id:v.first,weight:Number(v.weight)/100},
      {style_id:v.second,weight:1-Number(v.weight)/100}]})],
    ["آزمایش الگو","/styles/{id}/try","POST",[id,content],v=>({text:v.body_text})],
    ["خروجی الگو","/styles/{id}/export","GET",[id]],
    ["واردکردن بسته الگو","/styles/import","POST",[field("package","فایل الگو","json-file")],v=>v.package],
  ],
  media: [
    ["جستجوی رسانه دارای مجوز","/media/search","POST",[field("text","موضوع"),field("media_kind","نوع",["IMAGE","VIDEO"])],v=>v],
    ["اینفوگرافیک","/media/jobs","POST",[title,field("labels","عنوان سطرها، هر سطر جدا","textarea"),
      field("values","مقدار سطرها، هر سطر جدا","textarea")],
    v=>({...media("INFOGRAPHIC_RENDER")(v),table_rows:v.labels.split("\n").map((item,i)=>[item,v.values.split("\n")[i]||""])})],
    ["استیکر بومی","/media/jobs","POST",[field("asset_ids","تصویر","pick")],media("NATIVE_STICKER")],
    ["کلیپ محلی FFmpeg","/media/jobs","POST",[field("asset_ids","تصاویر","multi"),field("caption","کپشن","text",true)],media("LOCAL_SLIDESHOW_CLIP")],
    ["ذخیره متن پادکست","/podcast-scripts","POST",[
      field("speaker_a","متن گوینده اول","textarea"),field("voice_a","صدای گوینده اول"),
      field("speaker_b","متن گوینده دوم اختیاری","textarea",true),field("voice_b","صدای گوینده دوم","text",true),
      field("approved","متن را برای تولید صوت تأیید می‌کنم","checkbox"),
    ],v=>({speaker_mode:v.speaker_b?"DUAL_SPEAKER":"SINGLE_SPEAKER",target_duration_sec:60,
      approved:v.approved==="on",segments:[{speaker:"A",text:v.speaker_a,voice_id:v.voice_a},
        ...(v.speaker_b?[{speaker:"B",text:v.speaker_b,voice_id:v.voice_b}]:[])]})],
    ["ساخت صدای پادکست","/media/jobs","POST",[field("podcast_script_id","سناریوی تأییدشده","pick"),
      field("provider_id","ارائه‌دهنده","pick"),field("model_id","مدل","pick"),field("output_format","خروجی",["VOICE_NOTE","AUDIO"])],
    v=>({...v,job_kind:"TTS_SYNTHESIS"}),true],
    ["لغو کار رسانه","/media/jobs/{id}","DELETE",[id],()=>undefined,true],
    ["وضعیت کار رسانه","/media/jobs/{id}","GET",[id]],
    ["افزودن فایل رسانه","/media/assets","POST",[field("file","فایل","upload-file")],v=>{
      const body=new FormData();body.append("file",v.file);return body;
    }],
  ],
  ai: [
    ["دسترسی‌های فضای کاری","/ai/settings","PUT",[
      field("allow_external_ai","اجازهٔ اتصال به هوش مصنوعی","checkbox"),
      field("allow_feedback_learning","یادگیری از بازخورد","checkbox"),
      field("allow_external_media_search","اجازهٔ جست‌وجوی رسانه","checkbox"),
    ],v=>Object.fromEntries(["allow_external_ai","allow_feedback_learning","allow_external_media_search"].map(key=>[key,v[key]==="on"]))],
    ["رضایت‌های ثبت‌شده","/security/consents","GET",[]],
    ["ثبت مدل در فهرست","/ai/providers/{provider_id}/models","POST",[
      field("provider_id","ارائه‌دهنده","pick"),field("model_id","نام مدل نزد ارائه‌دهنده"),
      field("modality","قابلیت",["TEXT","VISION","IMAGE","TTS","VIDEO"]),
    ],v=>({model_id:v.model_id,modalities:[v.modality]})],
    ["خزانه کلیدها","/security/secrets","GET",[]],
    ["افزودن کلید ربات","/security/secrets","POST",[field("label","عنوان کلید"),
      field("secret_kind","نوع کلید",["TELEGRAM_BOT_TOKEN","BALE_BOT_TOKEN","EITAA_API_TOKEN","RUBIKA_BOT_TOKEN","SOROUSH_BOT_TOKEN"]),
      field("raw_secret","کلید","password")],v=>v],
    ["لغو کلید","/security/secrets/{id}","DELETE",[id],()=>undefined,true],
    ["سقف فضای کاری","/ai/budgets","PUT",[field("period_kind","دوره",["DAILY","MONTHLY","TOTAL"]),
      field("max_cost_micros","سقف هزینه (میکرو)","number")],v=>({...v,max_cost_micros:Number(v.max_cost_micros)})],
    ["ثبت امن کلید","/ai/providers","PUT",[field("provider_type","نوع ارائه‌دهنده",["OPENAI","GEMINI","COMPATIBLE_OPENAI"]),
      field("display_name","نام"),field("api_key","کلید API","password"),field("base_url","نشانی ارائه‌دهنده سازگار","url",true)],v=>Object.fromEntries(Object.entries(v).filter(([,value])=>value))],
    ["رضایت دسترسی خارجی","/security/consents","PUT",[field("provider_scope","دامنه رضایت",
      ["OPENAI_TEXT","OPENAI_IMAGE","OPENAI_AUDIO","OPENAI_VISION","GEMINI_TEXT","GEMINI_IMAGE","GEMINI_AUDIO","GEMINI_VISION","GEMINI_VIDEO","STOCK_MEDIA_SEARCH"]),
      field("is_granted","اجازه می‌دهم","checkbox")],v=>({...v,is_granted:v.is_granted==="on"})],
    ["بودجه و سقف طرح","/ai/budgets","GET",[]],
    ["آزمون مصنوعی مدل","/ai/models/probe","POST",[field("provider_id","ارائه‌دهنده","pick"),
      field("model_id","مدل","pick"),field("modality","قابلیت",["TEXT","VISION","IMAGE","TTS","VIDEO"])],
    v=>({...v,explicit_token_spend_confirmation:true}),true],
  ],
  drafts: [
    ["ویرایش پیش‌نویس","/drafts/{id}","PATCH",[id,field("title_text","عنوان","text",true),content,
      field("brand_identity_id","هویت برند","pick",true),field("assets","پیوست‌های رسانه","multi",true)],
    v=>({title_text:v.title_text||null,body_text:v.body_text,brand_identity_id:v.brand_identity_id||null,
      media_asset_ids:ids(v.assets),expected_draft_revision_no:Number(v.revision)})],
    ["ذخیره پیش‌نویس","/drafts","POST",[content,field("assets","پیوست‌های رسانه","multi",true)],
      v=>({body_text:v.body_text,media_asset_ids:ids(v.assets)})],
    ["پیش‌نمایش مقصد","/drafts/{id}/preview","POST",[id,...targets],preview],
    ["ارسال آزمایشی واقعی","/drafts/{id}/publish","POST",[id,...choices()],publish("EXPLICIT_TEST_SEND"),true],
    ["انتشار نهایی","/drafts/{id}/publish","POST",[id,...choices()],publish("LIVE_PUBLISH"),true],
  ],
  delivery: [
    ["فیلتر وضعیت","/deliveries?status={status}","GET",[field("status","وضعیت",["UNCERTAIN","READY","SENT","FAILED","SCHEDULED"])]],
    ["آشتی تحویل","/deliveries/{id}/reconcile","POST",[id,field("expected_lease_epoch","اجاره تحویل","derived"),
      field("resolution_action","تصمیم",["CONFIRM_SENT","CONFIRM_NOT_SENT_REQUEUE","DISCARD_DO_NOT_SEND"]),
      field("reason","دلیل"),field("acknowledge_duplicate_risk","ریسک ارسال تکراری را می‌پذیرم","checkbox")],
    v=>({expected_lease_epoch:Number(v.expected_lease_epoch),resolution_action:v.resolution_action,
      reason:v.reason,acknowledge_duplicate_risk:v.acknowledge_duplicate_risk==="on"}),true],
  ],
  operations: [
    ["فهرست پشتیبان‌ها","/backups","GET",[]],
    ["خلاصه مصرف مدل‌ها","/reports/ai-usage","GET",[]],
    ["پاکسازی","/maintenance/cleanup","POST",[],()=>({}),true],
    ["گزارش JSON","/reports/export?format=json","GET",[]],
    ["گزارش CSV","/reports/export?format=csv","GET",[]],
    ["پشتیبان رمزگذاری‌شده","/backups","POST",backupFields,backup],
    ["بازنشانی امن","/ops/reset","POST",backupFields,backup,true],
    ["بازیابی پشتیبان","/backups/restore","POST",[field("backup_password","گذرواژه پشتیبان","password"),
      field("bundle_base64","فایل رمزگذاری‌شده .rfbak","backup-file",true),field("manifest_id","یا پشتیبان ثبت‌شده","pick",true),
      field("mode","گام بازیابی",["PREVIEW","APPLY"])],v=>Object.fromEntries(Object.entries(v).filter(([,value])=>value)),true],
    ["حذف حساب مالک (ماندگاری فیزیکی PITR تا ۱۴ روز)","!/tenants/current/delete-account","POST",
      [field("confirmation_slug","شناسه مستأجر برای تأیید حذف")],v=>v,true],
  ],
};
