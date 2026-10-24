/**
 * 剧核 / DramaCore —— 多语言（中文 / English）资源 + 节点 UI 增强
 *
 * ## 为什么是「一个 JS 文件」而不是 JSON
 *
 * ComfyUI 通过 `WEB_DIRECTORY = "./web"` 自动把 web/ 下的 .js 全部加载进网页。
 * 这里把中/英资源挂到 `globalThis.DramaCoreI18N`，面板（bettercomfyui_panel.js）
 * 在 setup() 里直接读取，**零网络请求、零竞态**，也不需要改 ComfyUI 的加载顺序；
 * 同时本文件自己注册一个前端扩展，负责：
 *   1. 节点标题按语言本地化（配置了 zh/en 时只显示对应语言；auto 保留服务端双语标题）；
 *   2. 节点参数 tooltip 中英双语（按控件名匹配，覆盖常见参数）；
 *   3. 长参数**分组 + 折叠**（素材 / 视频 / 审核 / 输出），不删除任何参数；
 *   4. 执行结束后在节点上画状态/审核结论徽标（通过 / 不通过 / 需返工 / 失败…）。
 *
 * ## 语言优先级（高 → 低）
 *
 *   localStorage["dramacore.uiLang"]（面板里选的，立即生效）
 *     > 服务端 config/user.yaml 的 ui.language（面板保存后跨浏览器生效）
 *     > 浏览器语言（navigator.language，zh* → 中文，其余 → English）
 *
 * 节点标题的语种切换需要刷新页面（ComfyUI 的节点注册表在页面加载时建立），
 * 面板自身文案是即时切换的 —— 见 docs/剧核_DramaCore_品牌与配置说明.md。
 */

import { app } from "../../../scripts/app.js";

// ---------------------------------------------------------------------------
// 品牌与语言
// ---------------------------------------------------------------------------

export const BRAND = { zh: "剧核", en: "DramaCore" };
export const BRAND_FULL = { zh: "剧核 / DramaCore", en: "DramaCore / 剧核" };
export const LANGS = ["auto", "zh", "en"];
export const LANG_STORAGE_KEY = "dramacore.uiLang";

// ---------------------------------------------------------------------------
// 面板文案（zh / en）
// ---------------------------------------------------------------------------

const ZH = {
  "brand.tagline": "编剧 → 分镜 → 素材 → 出片 → 审核 → 成片，一个面板全流程",
  "brand.version": "剧核 / DramaCore v{v}",
  "common.ok": "正常",
  "common.warn": "注意",
  "common.err": "异常",
  "common.unknown": "未知",
  "common.loading": "读取中…",
  "common.save": "保存",
  "common.saved": "已保存",
  "common.refresh": "刷新",
  "common.test": "测试连接",
  "common.testing": "测试中…",

  "group.connection": "① 连接配置（API Key / 模型 / 接口地址）",
  "group.connection.hint": "填写后点「保存」写入 config/user.yaml；密钥只显示遮罩，不回显明文。",
  "api.key": "API Key",
  "api.key.ph": "填写密钥（留空 = 不修改已保存的密钥）",
  "api.base": "API Base（接口地址）",
  "api.base.ph": "如 https://api.deepseek.com（留空 = 官方默认）",
  "api.model": "模型名",
  "api.model.ph": "如 deepseek-chat / qwen-plus",
  "api.status.reading": "读取中…",
  "api.status.configured": "已配置",
  "api.status.missing": "未配置",
  "api.status.source": "来源",
  "api.status.masked": "密钥",
  "api.status.hint.missing": "尚未配置密钥：可在本面板填写，或设置环境变量 BETTERCOMFYUI_API_KEY（优先级最高）。",
  "api.test.ok": "连接成功：{ms} ms，模型 {n} 个",
  "api.test.ok.sample": "可用模型示例：{list}",
  "api.test.fail": "连接失败：{error}",

  "group.defaults": "② 创作默认值（提示词 / 语言 / 默认模型）",
  "group.defaults.hint": "全局默认值；节点上的同名参数留空时回落到这里，避免每次重复填。",
  "defaults.system_prompt": "默认系统提示词",
  "defaults.system_prompt.ph": "留空 = 用插件内置的导演/编剧提示词。可写：你是一名短剧编剧兼分镜导演…",
  "defaults.output_language": "输出语言（剧本/提示词语言）",
  "defaults.ui_language": "界面语言",
  "defaults.lang.auto": "自动（跟随浏览器）",
  "defaults.lang.zh": "中文",
  "defaults.lang.en": "English",
  "defaults.video_model": "默认视频模型",
  "defaults.image_model": "默认图像模型",
  "defaults.model.ph": "选择或直接输入模型文件/名称",
  "defaults.saved": "默认值已保存",
  "defaults.need_reload": "节点标题的语言切换需刷新页面（Ctrl+F5）生效",

  "group.workspace": "③ 项目与运行",
  "project.name.ph": "新项目名字（如：我的短剧）",
  "project.open": "打开或创建",
  "project.refresh": "刷新项目",
  "project.none": "（还没有项目：填名字后点「打开或创建」）",
  "executor": "执行器",
  "executor.mock": "占位媒体（一键闭环唯一路径，非模型生成）",
  "executor.real": "真实 ComfyUI（未接入本闭环：请用导演台/总流程工作流出片）",
  "script": "剧本 JSON（留空 = 用内置演示剧本）",
  "run": "开始运行",
  "run.busy": "运行中…",
  "workflow.load": "载入最简工作流",
  "workflow.loaded": "已载入最简工作流；填好剧本后点 Queue",
  "workflow.unsupported": "当前 ComfyUI 前端不支持自动载入工作流，请把插件 workflows/minimal_workflow.json 拖入画布",

  "group.status": "④ 状态与审核结论",
  "doctor": "体检",
  "doctor.running": "体检中…",
  "doctor.node_count": "节点：{n} 个",
  "doctor.orchestrator.ok": "编排包：正常（{file}）",
  "doctor.orchestrator.fail": "编排包：不可用 —— {error}",
  "doctor.ffmpeg.ok": "ffmpeg：正常",
  "doctor.ffmpeg.fail": "ffmpeg：不可用 —— {error}",
  "doctor.comfyui": "ComfyUI 版本：{v}",
  "doctor.real_gen.yes": "真实生成：可用",
  "doctor.real_gen.no": "真实生成：不可用（走占位媒体路径）",
  "doctor.compute.yes": "计算设备：{device}（{vram} MiB）",
  "doctor.compute.no": "真实生成前置条件不满足：{reason}",
  "status.audit": "审核结论",
  "status.no_data": "暂无执行记录：跑一次工作流后这里会显示状态与审核结论。",
  "log.title": "运行日志",
  "log.hint": "先点「体检」确认环境；点「载入最简工作流」开始。",
  "log.api_ok": "配置已保存：{written} → {path}",
  "log.config_ok": "剧核配置已保存：{written} → {path}",
  "log.save_fail": "保存失败：{error}",
  "log.no_change": "没有要保存的内容：请先填写配置",

  "node.group.assets": "素材 Assets",
  "node.group.video": "视频 Video",
  "node.group.review": "审核 Review",
  "node.group.output": "输出 Output",
  "node.group.other": "其它 Others",
};

const EN = {
  "brand.tagline": "Script → storyboard → assets → generation → review → final cut, all in one panel",
  "brand.version": "DramaCore v{v} (剧核)",
  "common.ok": "OK",
  "common.warn": "Warning",
  "common.err": "Error",
  "common.unknown": "Unknown",
  "common.loading": "Loading…",
  "common.save": "Save",
  "common.saved": "Saved",
  "common.refresh": "Refresh",
  "common.test": "Test connection",
  "common.testing": "Testing…",

  "group.connection": "1) Connection (API Key / model / API base)",
  "group.connection.hint": "Click Save to write config/user.yaml; the key is masked and never echoed back.",
  "api.key": "API Key",
  "api.key.ph": "Enter key (empty = keep the saved one)",
  "api.base": "API Base",
  "api.base.ph": "e.g. https://api.deepseek.com (empty = default)",
  "api.model": "Model name",
  "api.model.ph": "e.g. deepseek-chat / qwen-plus",
  "api.status.reading": "Loading…",
  "api.status.configured": "Configured",
  "api.status.missing": "Not configured",
  "api.status.source": "Source",
  "api.status.masked": "Key",
  "api.status.hint.missing": "No API key yet: fill it here, or set env BETTERCOMFYUI_API_KEY (highest priority).",
  "api.test.ok": "Connection OK: {ms} ms, {n} models",
  "api.test.ok.sample": "Model samples: {list}",
  "api.test.fail": "Connection failed: {error}",

  "group.defaults": "2) Defaults (system prompt / language / models)",
  "group.defaults.hint": "Global defaults; node parameters fall back to these when left empty.",
  "defaults.system_prompt": "Default system prompt",
  "defaults.system_prompt.ph": "Empty = built-in director/screenwriter prompt. e.g. You are a short-drama writer & storyboard director…",
  "defaults.output_language": "Output language (script/prompts)",
  "defaults.ui_language": "UI language",
  "defaults.lang.auto": "Auto (follow browser)",
  "defaults.lang.zh": "中文",
  "defaults.lang.en": "English",
  "defaults.video_model": "Default video model",
  "defaults.image_model": "Default image model",
  "defaults.model.ph": "Pick or type a model file/name",
  "defaults.saved": "Defaults saved",
  "defaults.need_reload": "Node titles follow the language after a page reload (Ctrl+F5)",

  "group.workspace": "3) Project & run",
  "project.name.ph": "New project name (e.g. My Short Drama)",
  "project.open": "Open / create",
  "project.refresh": "Refresh projects",
  "project.none": "(No project yet: type a name and click Open / create)",
  "executor": "Executor",
  "executor.mock": "Placeholder media (only path of this one-click loop; NOT model-generated)",
  "executor.real": "Real ComfyUI (not wired into this loop: use Director / full workflows)",
  "script": "Script JSON (empty = built-in demo script)",
  "run": "Run",
  "run.busy": "Running…",
  "workflow.load": "Load minimal workflow",
  "workflow.loaded": "Minimal workflow loaded; fill the script and click Queue",
  "workflow.unsupported": "This ComfyUI frontend cannot auto-load workflows; drag workflows/minimal_workflow.json onto the canvas",

  "group.status": "4) Status & review verdicts",
  "doctor": "Diagnose",
  "doctor.running": "Diagnosing…",
  "doctor.node_count": "Nodes: {n}",
  "doctor.orchestrator.ok": "Orchestrator: OK ({file})",
  "doctor.orchestrator.fail": "Orchestrator: unavailable — {error}",
  "doctor.ffmpeg.ok": "ffmpeg: OK",
  "doctor.ffmpeg.fail": "ffmpeg: unavailable — {error}",
  "doctor.comfyui": "ComfyUI version: {v}",
  "doctor.real_gen.yes": "Real generation: available",
  "doctor.real_gen.no": "Real generation: unavailable (placeholder media path)",
  "doctor.compute.yes": "Compute device: {device} ({vram} MiB)",
  "doctor.compute.no": "Real generation preconditions not met: {reason}",
  "status.audit": "Review verdict",
  "status.no_data": "No run yet: status and review verdicts appear here after a workflow runs.",
  "log.title": "Run log",
  "log.hint": "Click “Diagnose” to check the environment; click “Load minimal workflow” to start.",
  "log.api_ok": "Settings saved: {written} → {path}",
  "log.config_ok": "DramaCore settings saved: {written} → {path}",
  "log.save_fail": "Save failed: {error}",
  "log.no_change": "Nothing to save: fill in some settings first",

  "node.group.assets": "Assets 素材",
  "node.group.video": "Video 视频",
  "node.group.review": "Review 审核",
  "node.group.output": "Output 输出",
  "node.group.other": "Others 其它",
};

// ---------------------------------------------------------------------------
// 节点显示名（与服务端 bettercomfyui_comfy/__init__.py 的双语标题保持同义）
// ---------------------------------------------------------------------------
//
// 节点「类型 ID」保持 BetterComfyUI* 不变（旧工作流兼容）；这里只改**显示名**。

export const NODE_NAMES = {
  BetterComfyUIOneStop: { zh: "剧核·单节点（剧本→成片）", en: "DramaCore · One-Stop (Script→Final Cut)" },
  BetterComfyUIEverything: { zh: "剧核·总控台（剧本→素材→导演组）", en: "DramaCore · Control Hub" },
  BetterComfyUIOneDirector: { zh: "剧核·一体化导演台", en: "DramaCore · Unified Director" },
  BetterComfyUIProject: { zh: "剧核·项目", en: "DramaCore · Project" },
  BetterComfyUIRun: { zh: "剧核·运行", en: "DramaCore · Run" },
  BetterComfyUIAssetReview: { zh: "剧核·素材审核", en: "DramaCore · Asset Review" },
  BetterComfyUIManualAsset: { zh: "剧核·手动素材", en: "DramaCore · Manual Asset" },
  BetterComfyUIResult: { zh: "剧核·结果", en: "DramaCore · Result" },
  BetterComfyUIDoctor: { zh: "剧核·体检", en: "DramaCore · Doctor" },
  BetterComfyUISettings: { zh: "剧核·API 配置", en: "DramaCore · API Settings" },
  BetterComfyUIStoryboard: { zh: "剧核·剧本→提示词", en: "DramaCore · Script→Prompts" },
  BetterComfyUIPromptBridge: { zh: "剧核·提示词桥接", en: "DramaCore · Prompt Bridge" },
  BetterComfyUIAssetGen: { zh: "剧核·素材生成（取任务）", en: "DramaCore · Asset Generation" },
  BetterComfyUIAssetIngest: { zh: "剧核·素材入库", en: "DramaCore · Asset Ingest" },
  BetterComfyUIVideoReview: { zh: "剧核·视频审核", en: "DramaCore · Video Review" },
  BetterComfyUIFinalize: { zh: "剧核·成片剪辑与自检", en: "DramaCore · Finalize & QA" },
  BetterComfyUIAssetImage: { zh: "剧核·素材｜取已锁定图", en: "DramaCore · Asset · Locked Image" },
  BetterComfyUIAssetSlots: { zh: "剧核·素材｜多槽输出(1-9)", en: "DramaCore · Asset · Multi-slot (1-9)" },
  BetterComfyUIAssetMedia: { zh: "剧核·素材｜取已锁定音/视频", en: "DramaCore · Asset · Locked A/V" },
  BetterComfyUITimelineText: { zh: "剧核·融合｜时间轴文本", en: "DramaCore · Fusion · Timeline Text" },
  BetterComfyUIFusionGroups: { zh: "剧核·融合｜段计划→导演组", en: "DramaCore · Fusion · Segments→Director Groups" },
  BetterComfyUIFusionPackGroup: { zh: "剧核·融合｜单组打包", en: "DramaCore · Fusion · Pack Group" },
  BetterComfyUIFusionCombine: { zh: "剧核·融合｜组合并", en: "DramaCore · Fusion · Combine" },
  BetterComfyUIFusionIngest: { zh: "剧核·融合｜回灌段版本", en: "DramaCore · Fusion · Ingest Take" },
  BetterComfyUIFusionDoctor: { zh: "剧核·融合｜体检", en: "DramaCore · Fusion · Doctor" },
};

// ---------------------------------------------------------------------------
// 状态 / 审核结论 → 徽标文案与颜色（节点可视化用）
// ---------------------------------------------------------------------------

export const STATUS = {
  passed: { zh: "通过", en: "Passed", color: "#2e7d32" },
  proceed: { zh: "通过", en: "Proceed", color: "#2e7d32" },
  ok: { zh: "正常", en: "OK", color: "#2e7d32" },
  blocked: { zh: "不通过", en: "Blocked", color: "#c62828" },
  rejected: { zh: "不通过", en: "Rejected", color: "#c62828" },
  failed: { zh: "失败", en: "Failed", color: "#c62828" },
  rework: { zh: "需返工", en: "Rework", color: "#ef6c00" },
  retry: { zh: "重试中", en: "Retrying", color: "#ef6c00" },
  degraded: { zh: "降级", en: "Degraded", color: "#ef6c00" },
  manual: { zh: "待人工确认", en: "Manual review", color: "#1565c0" },
  running: { zh: "运行中", en: "Running", color: "#1565c0" },
  pending: { zh: "等待中", en: "Pending", color: "#616161" },
  skipped: { zh: "已跳过", en: "Skipped", color: "#616161" },
};

// ---------------------------------------------------------------------------
// 参数 tooltip（按控件名匹配；节点里所有同名控件都会带上，中英双语）
// ---------------------------------------------------------------------------

export const WIDGET_TOOLTIPS = {
  api_key: { zh: "API 密钥；只写本地配置，不进工作流文件", en: "API key; stored locally only, never in workflow files" },
  api_key_env: { zh: "优先从该环境变量读密钥（比配置文件优先）", en: "Read the key from this env var first (overrides config)" },
  model_name: { zh: "调用的模型名，如 deepseek-chat", en: "Model name, e.g. deepseek-chat" },
  model: { zh: "模型名/模型文件", en: "Model name / model file" },
  endpoint: { zh: "接口地址（API Base）", en: "API base URL" },
  base_url: { zh: "接口地址（API Base）", en: "API base URL" },
  system_prompt: { zh: "系统提示词：约束编剧/导演风格与输出格式", en: "System prompt: constrains style and output format" },
  system_prompt_template: { zh: "全局默认系统提示词（留空=内置）", en: "Global default system prompt (empty = built-in)" },
  prompt: { zh: "正向提示词：主体+动作+镜头+光线+风格", en: "Positive prompt: subject, action, shot, lighting, style" },
  positive_prompt: { zh: "正向提示词", en: "Positive prompt" },
  negative_prompt: { zh: "反向提示词：要避开的内容", en: "Negative prompt: what to avoid" },
  seed: { zh: "随机种子；-1 = 每次随机（复现实验时固定）", en: "Seed; -1 = random each run (fix it to reproduce)" },
  steps: { zh: "采样步数；越高越慢越细", en: "Sampling steps; higher = slower & more detailed" },
  cfg: { zh: "CFG 强度；越高越贴合提示词（过高会僵）", en: "CFG scale; higher follows prompts more (too high = stiff)" },
  sampler: { zh: "采样器", en: "Sampler" },
  scheduler: { zh: "调度器", en: "Scheduler" },
  width: { zh: "画面宽度（像素）", en: "Frame width (px)" },
  height: { zh: "画面高度（像素）", en: "Frame height (px)" },
  resolution: { zh: "目标分辨率", en: "Target resolution" },
  fps: { zh: "帧率；与导演台 segment_seconds 匹配", en: "Frame rate; keep consistent with segment_seconds" },
  segment_seconds: { zh: "每段视频时长（秒）", en: "Seconds per segment" },
  duration: { zh: "时长（秒）", en: "Duration (seconds)" },
  batch_count: { zh: "每批生成数量", en: "Generation count per batch" },
  video_model: { zh: "视频生成模型", en: "Video generation model" },
  image_model: { zh: "图像生成模型", en: "Image generation model" },
  lora: { zh: "LoRA 权重/强度", en: "LoRA weights / strength" },
  language: { zh: "输出语言：zh=中文，en=English", en: "Output language: zh or en" },
  output_language: { zh: "输出语言：zh=中文，en=English", en: "Output language: zh or en" },
  review_mode: { zh: "审核模式：MODEL=模型审 / HUMAN=人工审", en: "Review mode: MODEL = model, HUMAN = human" },
  threshold: { zh: "通过阈值；低于则判不通过/需返工", en: "Pass threshold; below = fail / rework" },
  score_threshold: { zh: "评分通过阈值（默认 70）", en: "Score threshold (default 70)" },
  max_retries: { zh: "最大重试次数", en: "Max retries" },
  human_timeout_minutes: { zh: "人工审核超时（分钟），超时按配置切换", en: "Manual review timeout (min), then switch per config" },
  vram_tier: { zh: "显存档位：auto=自动按卡选择", en: "VRAM tier: auto = pick by GPU" },
  output_path: { zh: "输出路径", en: "Output path" },
  format: { zh: "封装格式（MP4 等）", en: "Container format (MP4 etc.)" },
  codec: { zh: "视频编码器", en: "Video codec" },
  crf: { zh: "编码质量（越小越清晰、文件越大）", en: "Encoding quality (lower = sharper, bigger)" },
  bgm_volume: { zh: "背景音乐音量（0~1）", en: "BGM volume (0–1)" },
  project_name: { zh: "项目名（同名会复用已有项目）", en: "Project name (same name reuses the project)" },
  script: { zh: "剧本/章节 JSON", en: "Script / chapter JSON" },
  executor: { zh: "执行器：mock=占位媒体，comfyui=真实生成", en: "Executor: mock = placeholder, comfyui = real" },
};

// ---------------------------------------------------------------------------
// 参数分组规则（顺序敏感：先匹配先分组）
// ---------------------------------------------------------------------------

export const GROUP_RULES = [
  { key: "assets", titleKey: "node.group.assets", re: /(asset|素材|ref_|reference|slot|lora|sdxl|image_prompt|character|角色)/i },
  { key: "review", titleKey: "node.group.review", re: /(review|audit|审核|threshold|score|cnt\d?|policy|verdict|human|rework|retry)/i },
  { key: "output", titleKey: "node.group.output", re: /(output|encode|finalize|成片|剪辑|format|codec|crf|subtitle|watermark|bgm|audio|save|path|cover)/i },
  { key: "video", titleKey: "node.group.video", re: /(video|segment|fps|frame|duration|resolution|width|height|vram|sampler|scheduler|steps|cfg|seed|motion|导演|director)/i },
];

// ---------------------------------------------------------------------------
// 语言检测与翻译
// ---------------------------------------------------------------------------

/** 浏览器/配置语言 → zh | en。localStorage 优先（面板切换后立即生效）。 */
export function detectLanguage() {
  try {
    const saved = localStorage.getItem(LANG_STORAGE_KEY);
    if (saved === "zh" || saved === "en") return saved;
  } catch (e) {
    /* localStorage 不可用时忽略 */
  }
  try {
    const nav = String(navigator.language || navigator.userLanguage || "").toLowerCase();
    return nav.startsWith("zh") ? "zh" : "en";
  } catch (e) {
    return "zh";
  }
}

export function resolveLanguage(preferred) {
  if (preferred === "zh" || preferred === "en") return preferred;
  return detectLanguage();
}

export function formatTemplate(text, vars) {
  if (typeof text !== "string") return String(text);
  return text.replace(/\{(\w+)\}/g, (_m, k) => (vars && k in vars ? String(vars[k]) : `{${k}}`));
}

/**
 * 创建翻译器。
 * @param {string} [preferred] "auto" | "zh" | "en"（来自服务端配置）
 */
export function createI18n(preferred) {
  let lang = resolveLanguage(preferred);
  const t = (key, vars) => {
    const dict = lang === "en" ? EN : ZH;
    return formatTemplate(dict[key] ?? ZH[key] ?? key, vars);
  };
  return {
    get lang() { return lang; },
    get preferred() { return preferred ?? "auto"; },
    setLang(next) {
      const v = next === "auto" || next === "en" || next === "zh" ? next : "zh";
      try { localStorage.setItem(LANG_STORAGE_KEY, v); } catch (e) { /* ignore */ }
      lang = resolveLanguage(v);
      return lang;
    },
    nodeName(id) {
      const entry = NODE_NAMES[id];
      if (!entry) return id;
      return (preferred === "zh" || preferred === "en") ? entry[lang] : entry.zh;
    },
    widgetTooltip(name) {
      const entry = WIDGET_TOOLTIPS[name];
      if (!entry) return null;
      return lang === "en" ? entry.en : entry.zh;
    },
    statusLabel(key) {
      const entry = STATUS[key];
      if (!entry) return String(key);
      return lang === "en" ? entry.en : entry.zh;
    },
    t,
    dict: lang === "en" ? EN : ZH,
  };
}

// ---------------------------------------------------------------------------
// 对外暴露（面板直接读 globalThis，不依赖模块加载顺序：ComfyUI 先加载完所有
// 扩展文件、再调用各扩展 setup()，所以面板 setup 时这里一定已执行）
// ---------------------------------------------------------------------------

globalThis.DramaCoreI18N = {
  BRAND,
  BRAND_FULL,
  LANGS,
  LANG_STORAGE_KEY,
  NODE_NAMES,
  STATUS,
  WIDGET_TOOLTIPS,
  GROUP_RULES,
  detectLanguage,
  resolveLanguage,
  createI18n,
};

// ---------------------------------------------------------------------------
// 节点 UI 增强扩展
// ---------------------------------------------------------------------------

/** 判定一个节点是否属于本插件（显示名表或类型名前缀）。 */
function isOurNode(nodeData) {
  const name = nodeData?.name;
  if (!name) return false;
  return Boolean(NODE_NAMES[name]) || String(name).startsWith("BetterComfyUI");
}

/** 给节点控件挂 tooltip（按当前界面语言）。 */
function applyWidgetTooltips(node) {
  const lang = resolveLanguage();
  for (const w of node.widgets || []) {
    try {
      const entry = WIDGET_TOOLTIPS[w.name];
      if (!entry) continue;
      w.tooltip = lang === "en" ? entry.en : entry.zh;
    } catch (e) {
      /* 单个控件失败不影响其它 */
    }
  }
}

/**
 * 长参数分组 + 折叠。
 *
 * 做法：在每组第一个控件前插入一个「分组标题按钮」（button 类型控件不参与
 * 工作流序列化，所以**不改变存档格式**）；点标题按钮折叠/展开该组控件。
 * 折叠用通用做法：把控件的 computeSize 换成 [0,-4] 并重算节点尺寸；
 * **不删除任何控件**，展开后恢复原 computeSize。
 */
function applyWidgetGroups(node, i18n) {
  const widgets = node.widgets || [];
  if (widgets.length < 6) return; // 参数少的节点不折腾

  const groups = new Map(); // key -> { titleKey, widgets: [] }
  for (const w of widgets) {
    const name = String(w.name || "");
    if (!name || w.type === "button") continue;
    for (const rule of GROUP_RULES) {
      if (rule.re.test(name)) {
        if (!groups.has(rule.key)) groups.set(rule.key, { titleKey: rule.titleKey, widgets: [] });
        groups.get(rule.key).widgets.push(w);
        break;
      }
    }
  }
  // 只有「至少两组、且每组 ≥2 个参数」才值得分组，否则保持原样
  const usable = [...groups.entries()].filter(([, g]) => g.widgets.length >= 2);
  if (usable.length < 2) return;

  for (const [, group] of usable) {
    try {
      const memberSet = new Set(group.widgets);
      const state = { collapsed: false }; // 默认展开（用户要求参数"全可见"，折叠只是可选收纳）
      const button = node.addWidget("button", `${i18n.t(group.titleKey)} ▾`, null, () => {
        state.collapsed = !state.collapsed;
        button.name = `${i18n.t(group.titleKey)} ${state.collapsed ? "▸" : "▾"}`;
        for (const w of memberSet) {
          if (!w) continue;
          if (state.collapsed) {
            if (!w.__dcOrigComputeSize) w.__dcOrigComputeSize = w.computeSize;
            w.computeSize = () => [0, -4];
          } else if (w.__dcOrigComputeSize) {
            w.computeSize = w.__dcOrigComputeSize;
          }
        }
        try {
          const size = node.computeSize();
          node.setSize([Math.max(node.size[0], size[0]), size[1]]);
          node.setDirtyCanvas(true, true);
        } catch (e) {
          /* 尺寸重算失败不影响参数值 */
        }
      });
      const idx = node.widgets.indexOf(button);
      if (idx >= 0) node.widgets.splice(idx, 1);
      const firstIdx = node.widgets.findIndex((w) => memberSet.has(w));
      node.widgets.splice(firstIdx >= 0 ? firstIdx : node.widgets.length, 0, button);
    } catch (e) {
      console.warn("[剧核/DramaCore] 参数分组失败（不影响节点使用）：", e);
    }
  }
  try { node.setDirtyCanvas(true, true); } catch (e) { /* ignore */ }
}

/** 从执行消息里提取状态/审核结论（字段因节点而异，尽量宽松匹配）。 */
function extractStatus(message) {
  if (!message || typeof message !== "object") return null;
  const candidates = [];
  const walk = (obj, depth) => {
    if (!obj || depth > 3) return;
    if (Array.isArray(obj)) {
      for (const item of obj) walk(item, depth + 1);
      return;
    }
    if (typeof obj === "object") {
      for (const [k, v] of Object.entries(obj)) {
        const key = k.toLowerCase();
        if (["passed", "ok", "success", "blocked", "verdict", "conclusion", "policy", "result"].includes(key)
            && (typeof v === "boolean" || typeof v === "string" || typeof v === "number")) {
          candidates.push([key, v]);
        }
        if (v && typeof v === "object") walk(v, depth + 1);
      }
    }
  };
  walk(message, 0);
  if (!candidates.length) return null;
  for (const [key, value] of candidates) {
    if (key === "passed" || key === "ok" || key === "success") {
      return value === true || value === "true" || value === 1 ? "passed" : "failed";
    }
    if (key === "blocked") {
      if (value === true || value === "true" || value === 1) return "blocked";
    }
    if (key === "verdict" || key === "conclusion" || key === "policy" || key === "result") {
      const text = String(value).toLowerCase();
      if (text.includes("pass") || text.includes("proceed") || text.includes("通过")) return "passed";
      if (text.includes("block") || text.includes("reject") || text.includes("fail") || text.includes("不通过") || text.includes("失败")) return "blocked";
      if (text.includes("rework") || text.includes("返工")) return "rework";
      if (text.includes("manual") || text.includes("人工")) return "manual";
    }
  }
  return "pending";
}

/** 在节点右上角画状态徽标。 */
function drawStatusBadge(node, ctx) {
  const statusKey = node.__dcStatus;
  if (!statusKey || !ctx) return;
  const entry = STATUS[statusKey];
  if (!entry) return;
  const label = node.__dcStatusLang === "en" ? entry.en : entry.zh;
  try {
    ctx.save();
    ctx.font = "bold 11px sans-serif";
    const padding = 4;
    const w = ctx.measureText(label).width + padding * 2;
    const h = 16;
    const x = Math.max(4, (node.size?.[0] || 120) - w - 6);
    const y = 4;
    ctx.fillStyle = entry.color;
    ctx.globalAlpha = 0.92;
    ctx.fillRect(x, y, w, h);
    ctx.globalAlpha = 1;
    ctx.fillStyle = "#ffffff";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(label, x + padding, y + h / 2 + 0.5);
    ctx.restore();
  } catch (e) {
    /* 画布异常不影响节点 */
  }
}

function registerNodeUIExtension() {
  try {
    app.registerExtension({
      name: "DramaCore.NodeUI",
      async beforeRegisterNodeDef(nodeType, nodeData) {
        try {
          if (!isOurNode(nodeData)) return;
          // 1) 节点标题：配置了 zh/en 时只用该语言（auto 保留服务端双语标题）
          try {
            const mode = localStorage.getItem(LANG_STORAGE_KEY) || "auto";
            if ((mode === "zh" || mode === "en") && nodeData?.name && NODE_NAMES[nodeData.name]) {
              nodeData.display_name = NODE_NAMES[nodeData.name][mode];
            }
          } catch (e) { /* localStorage 不可用则保留双语标题 */ }

          const origCreated = nodeType.prototype.onNodeCreated;
          nodeType.prototype.onNodeCreated = function () {
            let result;
            try {
              result = origCreated?.apply(this, arguments);
            } finally {
              try {
                const i18n = globalThis.DramaCoreI18N?.createI18n?.() || null;
                if (i18n) {
                  applyWidgetTooltips(this);
                  applyWidgetGroups(this, i18n);
                }
              } catch (e) {
                console.warn("[剧核/DramaCore] 节点 UI 增强失败（不影响使用）：", e);
              }
            }
            return result;
          };

          // 2) 执行结果 → 状态/审核结论徽标
          const origExecuted = nodeType.prototype.onExecuted;
          nodeType.prototype.onExecuted = function (message) {
            let result;
            try {
              result = origExecuted?.apply(this, arguments);
            } catch (e) {
              console.warn("[剧核/DramaCore] onExecuted 原实现报错：", e);
            }
            try {
              const st = extractStatus(message);
              if (st) {
                this.__dcStatus = st;
                const i18n = globalThis.DramaCoreI18N?.createI18n?.() || null;
                this.__dcStatusLang = i18n?.lang || "zh";
                this.setDirtyCanvas(true, true);
              }
            } catch (e) {
              console.warn("[剧核/DramaCore] 状态提取失败：", e);
            }
            return result;
          };

          // 3) 画布上叠加状态徽标（不覆盖原绘制）
          const origDraw = nodeType.prototype.onDrawForeground;
          nodeType.prototype.onDrawForeground = function (ctx) {
            try {
              if (origDraw) origDraw.apply(this, arguments);
            } catch (e) {
              /* 原绘制失败不阻断徽标 */
            }
            try { drawStatusBadge(this, ctx); } catch (e) { /* ignore */ }
          };
        } catch (e) {
          console.warn("[剧核/DramaCore] beforeRegisterNodeDef 增强失败：", e);
        }
      },
    });
    return true;
  } catch (e) {
    console.warn("[剧核/DramaCore] 节点 UI 扩展注册失败（面板不受影响）：", e);
    return false;
  }
}

registerNodeUIExtension();

export default globalThis.DramaCoreI18N;
