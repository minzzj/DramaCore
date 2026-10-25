/* ROUND79：可见版本徽标（用于确认浏览器加载到新版前端） */
(function () {
  try {
    const V = "v0.1.6-v3";
    console.log("[剧核/DramaCore] UI 前端已加载 " + V + " | " + new Date().toISOString());
    const paint = () => {
      try {
        if (!document.body) { setTimeout(paint, 300); return; }
        if (document.getElementById("dc-ui-version-badge")) return;
        const el = document.createElement("div");
        el.id = "dc-ui-version-badge";
        el.textContent = "DramaCore UI " + V;
        el.style.cssText = "position:fixed;left:10px;bottom:10px;z-index:99997;background:#0b3b2e;color:#7ef7c0;border:1px solid #1f7a5c;border-radius:6px;padding:4px 10px;font:12px/1.4 system-ui;pointer-events:none;opacity:.95";
        document.body.appendChild(el);
        setTimeout(() => { try { el.remove(); } catch (e) {} }, 15000);
      } catch (e) {}
    };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", paint, {once:true}); else paint();
  } catch (e) {}
})();

/**
 * 剧核 / DramaCore —— 单节点导演台 UI（移植自原 MiniMaxH3 Director 的 UI 体验）
 *
 * 背景：用户批评「单节点 UI 过于生硬、原先导演节点 UI 更好、功能不全、22 帧连续没了」。
 * 原导演台的 UI 是「UI 画的」：DOM widget + canvas 时间轴 + 自绘素材卡 / 连续性关节。
 * 本文件把这套体验移植到 BetterComfyUIOneStop 节点上（节点类型 ID 不变）：
 *
 *   1) 素材库：首帧/尾帧/参考视频/参考音频卡片（图片预览走 ComfyUI /view）、
 *      生成参数卡、运行时 LOCK 状态；
 *   2) 时间轴/分段：canvas 画的分段条（S1..Sn、时长、首尾帧、参考图数量）+
 *      可编辑分段卡；与「时间轴JSON」控件双向同步（JSON→UI 自动；UI→JSON 显式按钮，
 *      避免自动写入把节点的「自动构造 timeline」变成用户 JSON 覆盖、丢掉素材注入）；
 *   3) 22 帧段间连续：主开关 + 5/22/39/56 帧档位 + 音频上下文 24 + 每段 pin 状态
 *      全部可视化（连续性专区固定在顶部，不是藏在长表单里）；
 *   4) 其它导演台能力：任务类型/全局提示/Refine/SelfLift/FaceRefine/SemanticBridge、
 *      显存策略、输出审核、运行状态与审核结论徽标；
 *   5) 原生 LiteGraph 长表单默认隐藏，底部「高级：原生参数表单」可一键恢复。
 *
 * 所有 UI 都直接读写节点已有 widget（不改执行逻辑、不改 widgets_values 顺序；
 * DOM widget 用 serialize:false，旧工作流照常加载）。
 */

import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

const NODE_CLASS = "BetterComfyUIOneStop";
const DOM_WIDGET_NAME = "dc_director_ui";
const STYLE_ID = "dramacore-director-ui-style";
const EXT_NAME = "DramaCore.DirectorUI";

// 连续帧数档位（与导演台一致：H3 网格 5/22/39/56，官方基线 22）
const CONT_FRAME_CHOICES = [5, 22, 39, 56];
const DEFAULT_AUDIO_CONTEXT = 24;

// 需要隐藏的原生控件（全部；高级面板可恢复）
const KEEP_NATIVE = new Set([]);

// ---------------------------------------------------------------------------
// 样式
// ---------------------------------------------------------------------------

const CSS = `
.dcdu-host { display:flex; flex-direction:column; gap:8px; padding:8px; width:100%; box-sizing:border-box;
  font-size:12px; color:#e6e6e6; background:#171a1f; border-radius:8px; overflow:visible; }
.dcdu-head { display:flex; align-items:center; gap:8px; padding:6px 10px; border-radius:8px;
  background:linear-gradient(100deg,#122436,#173a2c 60%,#1b2733); border:1px solid #2b4a5a; }
.dcdu-brand { font-weight:800; font-size:15px; letter-spacing:.5px;
  background:linear-gradient(90deg,#4fd1ff,#7ee787); -webkit-background-clip:text; background-clip:text; color:transparent; }
.dcdu-sub { font-size:11px; opacity:.65; }
.dcdu-head .dcdu-spacer { flex:1; }
.dcdu-chip { display:inline-flex; align-items:center; gap:4px; padding:2px 8px; border-radius:999px;
  font-size:11px; line-height:18px; border:1px solid #38505f; background:#1d2833; color:#cfe6f2; white-space:nowrap; }
.dcdu-chip.ok { border-color:#2e7d32; background:#16301b; color:#7ee787; }
.dcdu-chip.warn { border-color:#8d6e1d; background:#2d2714; color:#ffd479; }
.dcdu-chip.err { border-color:#8e2f2f; background:#331717; color:#ff9a9a; }
.dcdu-chip.on { border-color:#2f7ad6; background:#14263c; color:#8fc6ff; }

.dcdu-panel { border:1px solid #2a3138; border-radius:8px; background:#1b1f25; padding:8px 10px 10px; }
.dcdu-panel.dcdu-cont { border-color:#2f6b45; background:linear-gradient(180deg,#14231a 0%,#1b1f25 55%);
  box-shadow:inset 0 0 0 1px rgba(126,231,135,.06); }
.dcdu-title { display:flex; align-items:center; gap:8px; font-weight:700; font-size:13px; margin-bottom:6px; }
.dcdu-title .dcdu-tag { font-size:10px; font-weight:500; padding:1px 6px; border-radius:4px;
  background:#24303a; color:#9ab4c4; }
.dcdu-title .dcdu-tag.green { background:#17351f; color:#7ee787; }
.dcdu-title .dcdu-hint { margin-left:auto; font-size:10px; opacity:.55; font-weight:400; }
.dcdu-row { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin:4px 0; }
.dcdu-row > label { font-size:11px; opacity:.8; min-width:0; }
.dcdu-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(190px,1fr)); gap:6px 10px; }
.dcdu-field { display:flex; flex-direction:column; gap:2px; min-width:0; }
.dcdu-field > span { font-size:10px; opacity:.62; }
.dcdu-input, .dcdu-select, .dcdu-textarea {
  width:100%; box-sizing:border-box; padding:4px 6px; border-radius:4px; font-size:12px;
  background:#12161a; border:1px solid #333d46; color:#e6e6e6; font-family:inherit; }
.dcdu-textarea { min-height:54px; resize:vertical; font-family:ui-monospace,Consolas,monospace; font-size:11px; }
.dcdu-input:focus, .dcdu-select:focus, .dcdu-textarea:focus { outline:none; border-color:#3f8fd6; }
.dcdu-btn { flex:0 0 auto; padding:3px 9px; border-radius:5px; cursor:pointer; font-size:11px;
  background:#222b33; color:#dbe7ee; border:1px solid #3a4752; }
.dcdu-btn:hover { filter:brightness(1.25); }
.dcdu-btn.primary { background:#1668c8; border-color:#1668c8; color:#fff; }
.dcdu-btn.green { background:#1f6b36; border-color:#1f6b36; color:#eafff0; }
.dcdu-btn.danger { background:#5a2020; border-color:#5a2020; color:#ffd9d9; }
.dcdu-btn.tiny { padding:1px 6px; font-size:10px; }
.dcdu-btn.on { background:#1f6b36; border-color:#2e7d32; color:#eafff0; font-weight:700; }

/* 开关 / 档位 chips */
.dcdu-switch { display:inline-flex; align-items:center; gap:6px; cursor:pointer; user-select:none; }
.dcdu-switch input { display:none; }
.dcdu-switch .dcdu-track { width:34px; height:18px; border-radius:999px; background:#39424b; position:relative; transition:.15s; }
.dcdu-switch .dcdu-track::after { content:""; position:absolute; top:2px; left:2px; width:14px; height:14px;
  border-radius:50%; background:#c9d4dc; transition:.15s; }
.dcdu-switch input:checked + .dcdu-track { background:#1f6b36; }
.dcdu-switch input:checked + .dcdu-track::after { left:18px; background:#b6ffc9; }
.dcdu-chips { display:inline-flex; gap:4px; }
.dcdu-frame-chip { padding:3px 10px; border-radius:6px; border:1px solid #3a4752; background:#161b20;
  cursor:pointer; font-weight:700; font-size:12px; color:#c3d2dc; }
.dcdu-frame-chip:hover { border-color:#4a8fd0; }
.dcdu-frame-chip.on { background:#143a24; border-color:#2e9e57; color:#8dffb0; box-shadow:0 0 8px rgba(46,158,87,.35); }
.dcdu-frame-chip .dcdu-u { font-size:9px; font-weight:400; opacity:.7; margin-left:2px; }

/* 22 帧 pin 可视化 */
.dcdu-pin-strip { display:flex; align-items:stretch; gap:0; margin:8px 0 2px; overflow-x:auto; padding-bottom:4px; }
.dcdu-seg-box { min-width:104px; max-width:190px; flex:1 1 120px; border:1px solid #2f5a3e; border-radius:6px;
  background:linear-gradient(180deg,#1d3326,#182027); padding:5px 7px; }
.dcdu-seg-box.first { border-color:#3a6f8e; background:linear-gradient(180deg,#172a38,#182027); }
.dcdu-seg-box .dcdu-seg-id { font-weight:800; font-size:12px; color:#9fe8b5; }
.dcdu-seg-box.first .dcdu-seg-id { color:#8fc6ff; }
.dcdu-seg-box .dcdu-seg-meta { font-size:10px; opacity:.75; }
.dcdu-joint { flex:0 0 auto; align-self:center; display:flex; flex-direction:column; align-items:center;
  padding:0 2px; min-width:44px; }
.dcdu-joint .dcdu-joint-badge { font-size:10px; font-weight:700; padding:2px 6px; border-radius:999px;
  background:#12331f; border:1px solid #2e9e57; color:#8dffb0; white-space:nowrap; }
.dcdu-joint.hard .dcdu-joint-badge { background:#2a1e1e; border-color:#7a4b4b; color:#e0a0a0; }
.dcdu-joint .dcdu-joint-line { width:100%; height:2px; background:linear-gradient(90deg,#2e9e57,#7ee787); margin:2px 0; border-radius:2px; }
.dcdu-joint.hard .dcdu-joint-line { background:repeating-linear-gradient(90deg,#666 0 4px,transparent 4px 8px); }
.dcdu-joint .dcdu-joint-cap { font-size:9px; opacity:.7; white-space:nowrap; }

/* 时间轴 canvas */
.dcdu-canvas-wrap { position:relative; border:1px solid #2a3138; border-radius:6px; overflow:hidden;
  background:#101418; margin:6px 0; }
.dcdu-canvas-wrap canvas { display:block; width:100%; height:152px; }
.dcdu-timeline-legend { display:flex; gap:12px; font-size:10px; opacity:.65; padding:2px 4px; }
.dcdu-timeline-legend i { display:inline-block; width:8px; height:8px; border-radius:2px; margin-right:3px; }

/* 素材卡 */
.dcdu-assets { display:grid; grid-template-columns:repeat(auto-fill,minmax(210px,1fr)); gap:8px; }
.dcdu-asset-card { border:1px solid #2c363f; border-radius:8px; overflow:hidden; background:#151a1f;
  display:flex; flex-direction:column; }
.dcdu-asset-preview { height:96px; background:repeating-linear-gradient(45deg,#1a2027 0 8px,#171c22 8px 16px);
  display:flex; align-items:center; justify-content:center; overflow:hidden; position:relative; }
.dcdu-asset-preview img { width:100%; height:100%; object-fit:cover; display:block; }
.dcdu-asset-preview .dcdu-asset-ph { font-size:11px; opacity:.45; text-align:center; padding:4px; }
.dcdu-asset-preview .dcdu-lock { position:absolute; top:5px; right:5px; font-size:9px; font-weight:700;
  padding:1px 6px; border-radius:999px; background:#12331f; border:1px solid #2e9e57; color:#8dffb0; }
.dcdu-asset-preview .dcdu-lock.none { background:#2b2416; border-color:#8d6e1d; color:#ffd479; }
.dcdu-asset-body { padding:5px 7px 7px; display:flex; flex-direction:column; gap:4px; }
.dcdu-asset-title { font-size:11px; font-weight:700; color:#bcd3e0; display:flex; gap:6px; align-items:center; }
.dcdu-asset-title .dcdu-role { font-size:9px; font-weight:500; opacity:.6; }
.dcdu-asset-path { font-size:10px; opacity:.6; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

/* 分段卡列表 */
.dcdu-seglist { display:flex; flex-direction:column; gap:6px; margin-top:8px; }
.dcdu-segitem { display:grid; grid-template-columns:34px 108px 1fr auto; gap:8px; align-items:start;
  border:1px solid #29333c; border-radius:6px; padding:6px; background:#161b20; }
.dcdu-segitem .dcdu-segno { font-weight:800; color:#8fc6ff; font-size:13px; padding-top:4px; }
.dcdu-segitem .dcdu-seg-prompt { min-height:44px; }
.dcdu-segitem .dcdu-seg-side { display:flex; flex-direction:column; gap:4px; align-items:flex-end; }
.dcdu-seg-refs { display:flex; gap:4px; flex-wrap:wrap; font-size:9px; opacity:.75; grid-column:2/4; }
.dcdu-ref-chip { padding:1px 5px; border-radius:4px; background:#1d2833; border:1px solid #38505f; }

.dcdu-note { font-size:10px; opacity:.6; line-height:1.5; }
.dcdu-note.warn { color:#ffd479; opacity:.9; }
.dcdu-status { display:flex; flex-direction:column; gap:3px; }
.dcdu-status .dcdu-badge { font-size:11px; padding:2px 7px; border-radius:4px; background:#1b232b; border:1px solid #333d46; }
.dcdu-status .dcdu-badge.ok { color:#7ee787; border-color:#2e7d32; }
.dcdu-status .dcdu-badge.warn { color:#ffd479; border-color:#8d6e1d; }
.dcdu-status .dcdu-badge.err { color:#ff9a9a; border-color:#8e2f2f; }
.dcdu-log { font-family:ui-monospace,Consolas,monospace; font-size:10px; white-space:pre-wrap; max-height:120px;
  overflow:auto; background:#101418; border:1px solid #29333c; border-radius:5px; padding:5px; opacity:.9; }
.dcdu-hidden-native { display:none !important; }
.dcdu-collapse { border:1px solid #2a3138; border-radius:8px; background:#1b1f25; }
.dcdu-collapse > summary { cursor:pointer; padding:8px 10px; font-weight:700; font-size:13px; color:#bcd3e0; list-style:none; display:flex; align-items:center; gap:8px; }
.dcdu-collapse > summary::before { content:"▸"; opacity:.7; }
.dcdu-collapse[open] > summary::before { content:"▾"; }
.dcdu-collapse > summary .dcdu-tag { font-weight:500; }
.dcdu-collapse > .dcdu-panel { border:none; background:transparent; margin:0 6px 8px; }
.dcdu-details-hint { margin-left:auto; font-size:10px; font-weight:400; opacity:.55; }
/* ===== ROUND49: 新手首屏 / 蒙版 / 项目上下文 / 审核结论 / 防重叠 ===== */
.dcdu-hero { border:2px solid #2f7ad6; border-radius:10px;
  background:linear-gradient(180deg,#14243a 0%,#1b1f25 70%); padding:10px 12px 12px; }
.dcdu-hero .dcdu-hero-title { display:flex; align-items:center; gap:8px; font-size:16px; font-weight:800; color:#9fd0ff; }
.dcdu-hero .dcdu-hero-title .dcdu-step { display:inline-flex; align-items:center; justify-content:center;
  width:26px; height:26px; border-radius:50%; background:#1d5fa8; color:#fff; font-size:14px; }
.dcdu-hero textarea.dcdu-script { min-height:150px; font-size:13px; line-height:1.6; margin:8px 0 6px;
  border-color:#3f6f9f; background:#0f1620; }
.dcdu-hero textarea.dcdu-script:focus { border-color:#5aa9ef; box-shadow:0 0 0 2px rgba(90,169,239,.2); }
.dcdu-hero-foot { display:flex; flex-wrap:wrap; align-items:center; gap:8px; }
.dcdu-hero-foot .dcdu-count { margin-left:auto; font-size:11px; opacity:.7; font-variant-numeric:tabular-nums; }
.dcdu-btn.big { padding:6px 14px; font-size:12px; font-weight:700; }
.dcdu-nextstep { display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:7px 10px; border-radius:8px;
  border:1px solid #3a4752; background:#182028; font-size:12px; }
.dcdu-nextstep.ready { border-color:#2e7d32; background:#132b19; }
.dcdu-nextstep.warn { border-color:#8d6e1d; background:#2a2412; }
.dcdu-nextstep.err { border-color:#8e2f2f; background:#2c1616; }
.dcdu-nextstep .dcdu-next-label { font-weight:700; }
.dcdu-nextstep .dcdu-actions { display:flex; flex-wrap:wrap; gap:6px; margin-left:auto; }
.dcdu-projectbar { border:1px solid #2a3138; border-radius:8px; background:#1b1f25; padding:8px 10px 10px; }
.dcdu-projectbar .dcdu-row { flex-wrap:wrap; }
.dcdu-projectbar select { min-width:220px; flex:1 1 220px; }
.dcdu-projchips { display:flex; flex-wrap:wrap; gap:6px; margin:6px 0; }
.dcdu-projchips .dcdu-pchip { font-size:11px; padding:2px 8px; border-radius:999px; background:#1d2833;
  border:1px solid #38505f; color:#cfe6f2; }
.dcdu-projchips .dcdu-pchip b { color:#8fc6ff; }
.dcdu-context { display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:8px; margin-top:6px; }
.dcdu-asset-mini { border:1px solid #2c363f; border-radius:8px; overflow:hidden; background:#151a1f; }
.dcdu-asset-mini .dcdu-prev { height:92px; background:repeating-linear-gradient(45deg,#1a2027 0 8px,#171c22 8px 16px);
  display:flex; align-items:center; justify-content:center; position:relative; overflow:hidden; }
.dcdu-asset-mini .dcdu-prev img { width:100%; height:100%; object-fit:cover; }
.dcdu-asset-mini .dcdu-meta { padding:4px 6px 6px; display:flex; flex-direction:column; gap:2px; }
.dcdu-asset-mini .dcdu-meta .dcdu-aname { font-size:11px; font-weight:700; color:#cfe0ec; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.dcdu-asset-mini .dcdu-meta .dcdu-arow { display:flex; flex-wrap:wrap; gap:4px; }
.dcdu-score { font-size:10px; font-weight:700; padding:1px 6px; border-radius:4px; background:#13301c; color:#8dffb0; }
.dcdu-score.low { background:#331c1c; color:#ffb0b0; }
.dcdu-statuschip { font-size:10px; font-weight:700; padding:1px 6px; border-radius:4px; border:1px solid #3a4752; background:#1d2833; color:#cfe6f2; }
.dcdu-statuschip.LOCKED, .dcdu-statuschip.ALLOWED { border-color:#2e7d32; color:#8dffb0; background:#132b19; }
.dcdu-statuschip.BLOCKED { border-color:#8e2f2f; color:#ffb0b0; background:#2c1616; }
.dcdu-statuschip.FALLBACK { border-color:#8d6e1d; color:#ffd479; background:#2a2412; }
.dcdu-statuschip.CNT1 { border-color:#3a6f8e; color:#9fd0ff; background:#142435; }
.dcdu-context-lists { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:8px; }
.dcdu-context-list { border:1px solid #29333c; border-radius:6px; padding:6px 8px; background:#161b20; max-height:150px; overflow:auto; }
.dcdu-context-list .dcdu-lh { font-size:11px; font-weight:700; color:#bcd3e0; margin-bottom:3px; }
.dcdu-context-list .dcdu-li { font-size:10px; opacity:.8; line-height:1.5; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.dcdu-audit-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:8px; }
.dcdu-audit-card { border:1px solid #2c363f; border-radius:8px; background:#151a1f; padding:7px 9px; }
.dcdu-audit-card .dcdu-at { font-size:11px; font-weight:700; color:#bcd3e0; display:flex; align-items:center; gap:6px; }
.dcdu-audit-card .dcdu-av { font-size:15px; font-weight:800; color:#e9f4ff; margin-top:3px; font-variant-numeric:tabular-nums; }
.dcdu-audit-card .dcdu-as { font-size:10px; opacity:.72; margin-top:2px; line-height:1.4; }
.dcdu-audit-card.ok { border-color:#2e7d32; }
.dcdu-audit-card.err { border-color:#8e2f2f; }
.dcdu-pending { color:#ffd479; font-size:11px; }
/* 斜纹蒙版（ROUND49 采纳点 3）：半透明 + 可点「手动填写/选择」解除 */
.dcdu-maskwrap { position:relative; }
.dcdu-mask { position:absolute; inset:0; border-radius:8px; z-index:3; cursor:help;
  background:repeating-linear-gradient(45deg, rgba(18,22,27,.72) 0 12px, rgba(18,22,27,.5) 12px 24px);
  border:1px dashed #5a7e9c; display:flex; flex-direction:column; align-items:center; justify-content:center;
  gap:8px; padding:14px; text-align:center; }
.dcdu-mask .dcdu-mask-note { font-size:14px; font-weight:800; color:#eaf6ff; text-shadow:0 1px 3px #000; letter-spacing:.3px; }
.dcdu-mask:hover { backdrop-filter: blur(1px); }
.dcdu-mask .dcdu-mask-lock { font-size:22px; line-height:1; }
.dcdu-mask .dcdu-mask-unlock { cursor:pointer; box-shadow:0 2px 10px rgba(0,0,0,.45); }
.dcdu-mask .dcdu-mask-unlock:hover { filter:brightness(1.12); transform:translateY(-1px); }
.dcdu-multi-body { margin-top:8px; border-top:1px dashed #2f3b45; padding-top:8px; }
.dcdu-multi-script { min-height:76px; width:100%; box-sizing:border-box; }
.dcdu-check { display:flex; align-items:center; gap:6px; font-size:12px; color:#cfe6f2; cursor:pointer; }
.dcdu-mask-legend { display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:7px 10px;
  border:1px dashed #3d6f9e; border-radius:8px; background:rgba(30,60,90,.28); font-size:12px; color:#cfe6f2; }
.dcdu-mask .dcdu-mask-sub { font-size:10px; opacity:.75; max-width:520px; line-height:1.5; }
/* 防重叠（ROUND48 方案 E）：chip 行 flex-wrap + 徽标独立安全区 + 窄窗收敛 */
.dcdu-head { flex-wrap:wrap; }
.dcdu-head .dcdu-badgesafe { display:flex; flex-wrap:wrap; gap:6px; align-items:center; flex:1 1 260px;
  justify-content:flex-end; min-width:0; }
.dcdu-collapse > summary { flex-wrap:wrap; row-gap:4px; }
.dcdu-collapse > summary .dcdu-tag { white-space:nowrap; }
.dcdu-narrow .dcdu-head { padding:6px 8px; }
.dcdu-narrow .dcdu-secondary { display:none !important; }
.dcdu-narrow .dcdu-head .dcdu-brand { font-size:13px; }
.dcdu-narrow .dcdu-head .dcdu-sub { display:none; }
.dcdu-narrow .dcdu-btn { padding:3px 7px; }
.dcdu-narrow .dcdu-hero textarea.dcdu-script { min-height:110px; }
.dcdu-narrow .dcdu-context-lists { grid-template-columns:1fr; }

`;

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const styleEl = document.createElement("style");
  styleEl.id = STYLE_ID;
  styleEl.textContent = CSS;
  document.head.appendChild(styleEl);
}

// ---------------------------------------------------------------------------
// widget 读写助手
// ---------------------------------------------------------------------------

function getWidget(node, name) {
  return (node.widgets || []).find((w) => w.name === name);
}

function readVal(node, name, fallback = "") {
  const w = getWidget(node, name);
  if (!w) return fallback;
  const v = w.value;
  if (v && typeof v === "object" && typeof v.value !== "number") {
    if (typeof v.content === "string") return v.content;
  }
  return v === undefined || v === null ? fallback : v;
}

function writeVal(node, name, value) {
  const w = getWidget(node, name);
  if (!w) return false;
  w.value = value;
  try { node.setDirtyCanvas(true, true); } catch (e) { /* ignore */ }
  return true;
}

function widgetOptions(node, name) {
  const w = getWidget(node, name);
  const values = w?.options?.values;
  return Array.isArray(values) ? values : Array.isArray(values) ? values : [];
}

function widgetMinMax(node, name) {
  const o = getWidget(node, name)?.options || {};
  return { min: o.min, max: o.max, step: o.step };
}

function comboValues(node, name) {
  const w = getWidget(node, name);
  const v = w?.options?.values;
  if (Array.isArray(v)) return v.slice();
  if (typeof v === "function") {
    try {
      const got = v(w);
      if (Array.isArray(got)) return got.slice();
    } catch (e) { /* ignore */ }
  }
  return [];
}


// ---------------------------------------------------------------------------
// ROUND49：首屏剧本示例 / 审核报告解析 / 通用小工具
// ---------------------------------------------------------------------------

/** 示例剧本（点「填入示例剧本」用；用户可整段替换）。 */
const SAMPLE_SCRIPT = [
  "【类型】古装悬疑短剧 · 第 1 章「雨夜客栈」",
  "",
  "场景 1｜夜·客栈大堂：暴雨敲窗。女掌柜林秀（28 岁，青衣，冷静）在柜台后拨算盘；" +
    "刀客老陈（45 岁，旧皮甲，左脸刀疤）推门而入，抖落雨水，把一封带血的信拍在柜台上。",
  "",
  "林秀（低声）：这信，从哪儿来的？",
  "老陈：三日前，官道边的死人身上。收信人……写的是你。",
  "",
  "场景 2｜夜·客栈二楼走廊：林秀提灯上楼，灯影晃动；尽头房门半开，地上有水渍与断箭头。" +
    "她停步，侧耳——屋内传来极轻的呼吸声。",
  "",
  "场景 3｜夜·客房内：一个蒙面人背对窗户。林秀推门而入，两人对视；窗外闪电照亮墙上血字「第 叁 夜」。",
  "",
  "镜头要求：电影感、冷色调、雨夜质感、浅景深；每段 5 秒左右；对白保留，动作连贯。",
].join("\n");

/** 中英双语（跟随剧核 UI 语言） */
function tr(zh, en) {
  try {
    const lang = globalThis.DramaCoreI18N?.detectLanguage?.() || "zh";
    return lang === "en" ? en : zh;
  } catch (e) {
    return zh;
  }
}

function fmtBytes(n) {
  const size = Number(n) || 0;
  if (size > 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  if (size > 1024) return `${(size / 1024).toFixed(0)} KB`;
  return `${size} B`;
}

function fmtTimeShort(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return "—";
  const num = Number(raw);
  if (Number.isFinite(num) && num > 1e9) {
    const d = new Date(num * (num > 1e12 ? 1 : 1000));
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }
  return raw.length > 19 ? raw.slice(0, 19).replace("T", " ") : raw;
}

function scoreClass(score) {
  const n = Number(score);
  return Number.isFinite(n) ? (n >= 70 ? "" : " low") : " low";
}

/** 审核/素材状态 → 中文标签（LOCKED/BLOCKED/FALLBACK/CNT1 等） */
function statusLabelUi(status) {
  const s = String(status || "").toUpperCase();
  const map = {
    LOCKED: "LOCKED 已锁定", ALLOWED: "ALLOWED 通过", BLOCKED: "BLOCKED 不通过",
    FALLBACK: "FALLBACK 兜底", CNT1: "CNT1 候选上限", REJECTED: "REJECTED 拒绝",
    UNKNOWN: "待审核",
  };
  return map[s] || (s ? s : "待审核");
}

function statusKindUi(status) {
  const s = String(status || "").toUpperCase();
  if (s === "LOCKED" || s === "ALLOWED") return "ok";
  if (s === "BLOCKED" || s === "REJECTED") return "err";
  if (s === "FALLBACK" || s === "CNT1") return "warn";
  return "";
}

function mean(values) {
  const nums = values.map(Number).filter((n) => Number.isFinite(n));
  if (!nums.length) return null;
  return Math.round((nums.reduce((a, b) => a + b, 0) / nums.length) * 10) / 10;
}

// ---------------------------------------------------------------------------
// 元素构建助手
// ---------------------------------------------------------------------------

function h(tag, attrs = {}, children = []) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else el.setAttribute(k, String(v));
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined) continue;
    el.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return el;
}

function field(labelText, control) {
  return h("div", { class: "dcdu-field" }, [h("span", { text: labelText }), control]);
}

/** 数字输入，直接绑定 widget。 */
function numberInput(node, name, { width = "" } = {}) {
  const { min, max, step } = widgetMinMax(node, name);
  const input = h("input", {
    class: "dcdu-input",
    "data-widget": name,
    type: "number",
    value: String(readVal(node, name, 0)),
    min: min ?? undefined,
    max: max ?? undefined,
    step: step ?? 1,
    style: width ? `width:${width}` : undefined,
    onchange: (ev) => {
      let v = Number(ev.target.value);
      if (!Number.isFinite(v)) v = Number(readVal(node, name, 0));
      if (min !== undefined) v = Math.max(Number(min), v);
      if (max !== undefined) v = Math.min(Number(max), v);
      writeVal(node, name, v);
      node._dcduUI?.onParamChanged?.();
    },
  });
  // ROUND66：数字框「输入即写回」——避免用户输入 5/4 但未失焦，队列却用旧值。
  // 实测出现「设 4 段却只出 1 段」：只有 change 事件，输入后直接点运行会丢改动。
  input.addEventListener("input", (ev) => {
    const raw = String(ev.target.value || "").trim();
    let v = Number(raw);
    if (!Number.isFinite(v)) return;
    if (min !== undefined) v = Math.max(Number(min), v);
    if (max !== undefined) v = Math.min(Number(max), v);
    writeVal(node, name, v);
  });
  return input;
}

/** 布尔开关（返回 label 包住的 switch）。 */
function switchControl(node, name, labelText, onChange) {
  const input = h("input", { type: "checkbox", "data-widget": name });
  input.checked = Boolean(readVal(node, name, false));
  input.addEventListener("change", () => {
    writeVal(node, name, input.checked);
    onChange?.(input.checked);
    node._dcduUI?.onParamChanged?.();
  });
  return h("label", { class: "dcdu-switch", title: labelText }, [
    input,
    h("span", { class: "dcdu-track" }),
    h("span", { text: labelText }),
  ]);
}

/** 下拉框，直接绑定 combo widget。 */
function selectControl(node, name, { labels = {} } = {}) {
  const values = comboValues(node, name);
  const current = String(readVal(node, name, values[0] ?? ""));
  const sel = h("select", {
    class: "dcdu-select",
    "data-widget": name,
    onchange: (ev) => {
      writeVal(node, name, ev.target.value);
      node._dcduUI?.onParamChanged?.();
    },
  });
  for (const value of values) {
    const opt = h("option", { value: String(value), text: labels[value] || String(value) });
    if (String(value) === current) opt.selected = true;
    sel.appendChild(opt);
  }
  if (!values.length) {
    const opt = h("option", { text: current || "（无候选）" });
    sel.appendChild(opt);
  }
  return sel;
}

function textInput(node, name, { multiline = false, placeholder = "" } = {}) {
  const tag = multiline ? "textarea" : "input";
  const el = h(tag, {
    class: multiline ? "dcdu-textarea" : "dcdu-input",
    "data-widget": name,
    value: multiline ? undefined : String(readVal(node, name, "")),
    placeholder,
  });
  if (multiline) el.value = String(readVal(node, name, ""));
  el.addEventListener("change", () => {
    writeVal(node, name, el.value);
    node._dcduUI?.onParamChanged?.();
  });
  return el;
}

function button(text, onClick, cls = "") {
  return h("button", { class: `dcdu-btn ${cls}`.trim(), text, onclick: onClick });
}

// ---------------------------------------------------------------------------
// 预览 URL（首帧/尾帧/参考图：ComfyUI 自带的 /view 只服务 input/output/temp）
// ---------------------------------------------------------------------------

function basename(p) {
  return String(p || "").replace(/\\/g, "/").split("/").filter(Boolean).pop() || "";
}

function previewUrl(path) {
  const p = String(path || "").replace(/\\/g, "/").trim();
  if (!p) return "";
  const lower = p.toLowerCase();
  const dir = lower.includes("/output/") ? "output"
    : lower.includes("/input/") ? "input"
    : lower.includes("/temp/") ? "temp" : "";
  if (!dir) return "";
  const name = basename(p);
  if (!name) return "";
  return `/view?filename=${encodeURIComponent(name)}&type=${dir}&subfolder=`;
}

function refPreviewUrl(ref) {
  if (!ref || typeof ref !== "object") return "";
  const file = ref.imageFile || ref.fileName || ref.filename || ref.videoFile || ref.audioFile || "";
  if (!file) return previewUrl(ref.path || ref.rel || ref.file || "");
  const type = ref.type || "input";
  return `/view?filename=${encodeURIComponent(basename(file))}&type=${encodeURIComponent(type)}&subfolder=${encodeURIComponent(ref.subfolder || "")}`;
}

// ---------------------------------------------------------------------------
// 导演台 UI
// ---------------------------------------------------------------------------

class DirectorCardUI {
  constructor(node) {
    this.node = node;
    this.root = null;
    this.canvas = null;
    this.segListEl = null;
    this.statusEl = null;
    this.logEl = null;
    this.timelineInfoEl = null;
    this.jsonArea = null;
    this.contStripEl = null;
    this.nativeHidden = true;
    this.lastStatusLines = [];
    this.timelineSource = "auto";
    this.segs = [];
    // ROUND49 新增状态
    this.maskState = {};
    this.audit = null;
    this.projectId = "";
    this.projects = [];
    this.projectContext = null;
    this.runtimeAssets = [];
    this.scriptEl = null;
    this.scriptCountEl = null;
    this.nextStepEl = null;
    this.projectSelectEl = null;
    this.projectChipsEl = null;
    this.projectContextEl = null;
    this.projectListsEl = null;
    this.projectHintEl = null;
    this.auditGridEl = null;
    this.auditNoteEl = null;
  }

  // ---- 生命周期 ---------------------------------------------------------
  mount() {
    injectStyle();
    const container = h("div", { class: "dcdu-host" });
    this.root = container;
    this.build();
    const ui = this;
    const domWidget = this.node.addDOMWidget(DOM_WIDGET_NAME, "dcdu", container, {
      getValue: () => "",
      setValue: () => {},
      getMinHeight: () => Math.max(1600, this.root?.scrollHeight || 1600),
      hideOnZoom: false,
      serialize: false,
      onDraw: () => { ui.ensureWidth(); },
      afterResize: () => { ui.ensureWidth(); },
    });
    domWidget.element = container;
    // ROUND61：部分 ComfyUI 前端只认 widget.serialize（不认 addDOMWidget 的 options.serialize）。
    // 不显式置 false 时，用户「保存工作流」会把 DOM widget 也序列化进 widgets_values（120→121），
    // 下次加载就会被 SchemaGuard 误报「参数数量不匹配」。这里双保险地钉死 serialize=false。
    try { domWidget.serialize = false; } catch (e) { /* ignore */ }
    try {
      domWidget.options = domWidget.options || {};
      domWidget.options.serialize = false;
    } catch (e) { /* ignore */ }
    this.domWidget = domWidget;
    this.node._dcduUI = this;
    this.hideNativeWidgets(true);
    queueMicrotask(() => this.hideNativeWidgets(true));
    setTimeout(() => this.hideNativeWidgets(true), 0);
    setTimeout(() => this.hideNativeWidgets(true), 120);
    try {
      this.node.setSize([1200, 1500]);
    } catch (e) { /* ignore */ }
    setTimeout(() => { this.ensureWidth(); this.refreshHeight(); }, 260);
    this.loadSegments();
    this.reloadAll();
    this.ensureWidth();
    // ROUND49: 窄窗收敛（防按钮重叠）+ 项目列表
    try {
      if (typeof ResizeObserver !== "undefined") {
        this._resizeObserver = new ResizeObserver(() => {
          const narrow = (this.root?.offsetWidth || 0) < 1000;
          this.root?.classList.toggle("dcdu-narrow", narrow);
        });
        this._resizeObserver.observe(this.root);
      }
    } catch (e) { /* ignore */ }
    this.updateScriptCount();
    this.loadProjects().catch(() => {});
    this.loadPanelApiConfig().catch(() => {});
    setTimeout(() => { this.ensureWidth(); this.reloadAll(); }, 300);
  }

  ensureWidth() {
    try {
      let width = this.node.size?.[0] || 1200;
      if (width < 1100) {
        width = 1200;
        this.node.setSize([width, this.node.size?.[1] || 1500]);
      }
      if (this.root) this.root.style.width = `${width - 24}px`;
      this.domWidget.options.getMinHeight = () => Math.max(900, this.root?.scrollHeight || 980);
    } catch (e) { /* ignore */ }
  }

  /** 让节点高度贴合 UI 实际内容（折叠/展开后重算）。 */
  refreshHeight() {
    try {
      this.ensureWidth();
      const height = Math.max(1000, (this.root?.scrollHeight || 1200) + 56);
      this.node.setSize([Math.max(1180, this.node.size?.[0] || 1180), height]);
      this.domWidget.options.getMinHeight = () => height;
      this.node.setDirtyCanvas(true, true);
      this.drawCanvas();
    } catch (e) { /* ignore */ }
  }

  /** 折叠区块（④⑤⑥⑦ 默认收起，避免「比长表单还长」）。 */
  collapsible(title, tag, hint, body) {
    const details = h("details", { class: "dcdu-collapse" }, [
      h("summary", {}, [
        h("span", { text: title }),
        h("span", { class: "dcdu-tag", text: tag || "" }),
        h("span", { class: "dcdu-details-hint", text: hint || "点开展开（参数一个不少）" }),
      ]),
      body,
    ]);
    details.addEventListener("toggle", () => setTimeout(() => this.refreshHeight(), 30));
    return details;
  }

  hideNativeWidgets(hide) {
    this.nativeHidden = hide;
    for (const w of this.node.widgets || []) {
      if (!w || w.name === DOM_WIDGET_NAME) continue;
      if (w.type === "button" && !KEEP_NATIVE.has(w.name)) {
        // 分组标题按钮也收起来（原生表单里有）
        if (!w.__dcduOrigComputeSize) w.__dcduOrigComputeSize = w.computeSize;
        w.computeSize = () => [0, -4];
        continue;
      }
      if (!w.__dcduOrigComputeSize) w.__dcduOrigComputeSize = w.computeSize;
      w.computeSize = hide ? () => [0, -4] : w.__dcduOrigComputeSize;
    }
    try { this.node.setDirtyCanvas(true, true); } catch (e) { /* ignore */ }
  }

  // ---- ROUND56：多段剧本动态列表 ------------------------------------
  readMultiChapters() {
    const raw = String(readVal(this.node, "多段剧本列表JSON", "") || "").trim();
    if (raw) {
      try {
        const arr = JSON.parse(raw);
        if (Array.isArray(arr)) return arr.length ? arr.map((x) => String(x == null ? "" : x)) : [""];
      } catch (e) { /* 退回旧文本 */ }
    }
    const legacy = String(readVal(this.node, "多段剧本", "") || "");
    if (!legacy.trim()) return [""];
    const sep = String(readVal(this.node, "多段剧本分隔符", "---") || "---");
    const parts = [];
    let cur = [];
    legacy.split(/\r?\n/).forEach((line) => {
      if (line.trim() === sep) { parts.push(cur.join("\n")); cur = []; }
      else cur.push(line);
    });
    parts.push(cur.join("\n"));
    const cleaned = parts.map((s) => String(s || "").trim()).filter(Boolean);
    return cleaned.length ? cleaned : [""];
  }

  syncMultiChapters(list) {
    // 保留空段（用户点「添加一段」后必须立即可见第二段；空段在运行前会被过滤）
    const all = (Array.isArray(list) && list.length) ? list.map((s) => String(s || "")) : [""];
    const valid = all.filter((s) => s.trim().length > 0);
    writeVal(this.node, "多段剧本列表JSON", JSON.stringify(all));
    // 兼容旧文本参数（后端仍支持；JSON 优先）
    const sep = String(readVal(this.node, "多段剧本分隔符", "---") || "---");
    writeVal(this.node, "多段剧本", valid.join("\n" + sep + "\n"));
    if (this.multiCountEl) {
      this.multiCountEl.textContent = tr(
        `已添加 ${all.length} 段 · 有效 ${valid.length} 段 · 预计 ${valid.length} 次 API`
          + (all.length > 64 ? "（⚠️ 超过上限 64：运行时只取前 64 段）" : ""),
        `${all.length} items · ${valid.length} valid · ~${valid.length} API calls`
          + (all.length > 64 ? " (over 64: only first 64 run)" : ""));
    }
    if (this.updateScriptCount) this.updateScriptCount();
    if (this.refreshNextStep) this.refreshNextStep();
  }

  renderMultiChapters() {
    const box = this.multiChaptersEl;
    if (!box) return;
    const list = this.readMultiChapters();
    box.innerHTML = "";
    list.forEach((text, idx) => {
      const ta = document.createElement("textarea");
      ta.className = "dcdu-textarea dcdu-multi-script";
      ta.value = text;
      ta.placeholder = tr(`第 ${idx + 1} 段剧本…（每段会单独生成并顺序拼接）`,
                          `Segment ${idx + 1} script… (generated separately, concatenated in order)`);
      ta.addEventListener("input", () => {
        const cur = this.readMultiChapters();
        cur[idx] = ta.value;
        this.syncMultiChapters(cur);
      });
      box.appendChild(h("div", { class: "dcdu-multi-item" }, [
        h("div", { class: "dcdu-row" }, [
          h("span", { class: "dcdu-chip", text: tr(`第 ${idx + 1} 段`, `Segment ${idx + 1}`) }),
          button(tr("上移", "Up"), () => this.moveMultiChapter(idx, -1), "tiny"),
          button(tr("下移", "Down"), () => this.moveMultiChapter(idx, 1), "tiny"),
          button(tr("删除", "Delete"), () => this.removeMultiChapter(idx), "danger tiny"),
        ]),
        ta,
      ]));
    });
    if (this.multiCountEl) {
      const valid = list.filter((s) => String(s || "").trim().length > 0).length;
      this.multiCountEl.textContent = tr(
        `已添加 ${list.length} 段 · 有效 ${valid} 段 · 预计 ${valid} 次 API`
          + (list.length > 64 ? "（⚠️ 超过上限 64：运行时只取前 64 段）" : ""),
        `${list.length} items · ${valid} valid · ~${valid} API calls`
          + (list.length > 64 ? " (over 64: only first 64 run)" : ""));
    }
  }

  addMultiChapter() {
    const list = this.readMultiChapters();
    list.push("");
    this.syncMultiChapters(list);
    this.renderMultiChapters();
    // 强制显示列表（即使用户没勾选多段开关）
    if (this.multiToggleEl && !this.multiToggleEl.checked) {
      this.multiToggleEl.checked = true;
      this.multiToggleEl.dispatchEvent(new Event("change"));
    }
    if (this.multiBodyEl) this.multiBodyEl.style.display = "block";
    try { this.multiChaptersEl.scrollIntoView({ block: "nearest" }); } catch (e) {}
  }

  removeMultiChapter(idx) {
    const list = this.readMultiChapters();
    list.splice(idx, 1);
    if (!list.length) list.push("");
    this.syncMultiChapters(list);
    this.renderMultiChapters();
  }

  moveMultiChapter(idx, delta) {
    const list = this.readMultiChapters();
    const j = idx + delta;
    if (j < 0 || j >= list.length) return;
    const tmp = list[idx];
    list[idx] = list[j];
    list[j] = tmp;
    this.syncMultiChapters(list);
    this.renderMultiChapters();
  }

  // ---- ROUND56：提示词查看 / 编辑（高级项，默认只读，手动解锁） --------
  // ---- ROUND58：默认加载《MiniMax H3 提示词工程精简版》V1.0（2026-10，12,890 字）；
  //      旧完整手册（41,966 字）与旧核心章节保留为可选项（旧版不删）。
  _handbookModeSpec(mode) {
    const specs = {
      v3: {
        label: tr("h3_v3_contract（V3.0 契约对齐·唯一提示词，37,457 字）",
                  "h3_v3_contract (V3.0 contract-aligned unique prompt, 37,457 chars)"),
        aliases: ["h3_v3_contract", "v3", "契约对齐", "唯一提示词"],
        key: "h3_v3_contract",
        file: "bettercomfyui/prompt/data/h3_v3_contract.txt",
      },
      concise: {
        label: tr("H3 提示词精简版 V1.0（2026-10，12,890 字）",
                  "H3 concise handbook V1.0 (2026-10, 12,890 chars)"),
        aliases: ["concise", "h3_concise_v1", "concise_v1", "精简版"],
        key: "h3_concise_v1",
        file: "bettercomfyui/prompt/data/h3_concise_v1.txt",
      },
      full: {
        label: tr("H3 手册完整版（历史版，41,966 字）", "H3 full handbook (legacy)"),
        aliases: ["full"],
        key: "h3_handbook",
        file: "bettercomfyui/prompt/data/h3_handbook_full.txt",
      },
      core: {
        label: tr("H3 手册核心章节（旧精简）", "H3 core handbook (legacy)"),
        aliases: ["core"],
        key: "h3_core",
        file: "bettercomfyui/prompt/data/h3_handbook_full.txt",
      },
    };
    return specs[mode] || specs.v3;
  }

  /** 面板里存的键/标签 → 手册预设模式；不是手册预设返回 ""。 */
  _presetModeOf(value) {
    const text = String(value || "").trim();
    if (!text) return "";
    const table = {
      h3_v3_contract: "v3", v3: "v3", "契约对齐": "v3", "唯一提示词": "v3",
      "h3_v3_contract（V3.0 契约对齐·唯一提示词）": "v3",
      "H3 系统提示词 V3.0（契约对齐·唯一）": "v3",
      h3_concise_v1: "concise", h3_concise: "concise", concise: "concise", "精简版": "concise",
      "H3 提示词精简版（2026-10，推荐）": "concise",
      h3_handbook: "full", "H3 手册（完整，历史版）": "full", "H3 手册（完整，推荐）": "full",
      h3_core: "core", "H3 手册（精简）": "core",
    };
    return table[text] || "";
  }

  _isBuiltinPresetKey(value) {
    return ["default", "strict_json", "chinese_drama", "shot_by_shot", "local_qwen"]
      .includes(String(value || "").trim());
  }

  setPromptSource(text) {
    if (this.promptSourceEl) this.promptSourceEl.textContent = String(text || "");
  }

  buildPromptsPanel() {
    const panel = h("div", { class: "dcdu-panel" });
    panel.appendChild(h("div", { class: "dcdu-title" }, [
      h("span", { text: tr("当前生效的系统提示词（V3.0 契约对齐 · 默认 h3_v3_contract · 37,457 字）",
                            "Active system prompt (V3.0 contract-aligned · default h3_v3_contract)") }),
      button(tr("加载 V3.0 唯一提示词（推荐）", "Load V3.0 unique prompt (recommended)"),
             () => this.loadHandbookPrompt("v3"), "green tiny"),
      button(tr("加载旧精简版 V1.0", "Load legacy concise V1.0"),
             () => this.loadHandbookPrompt("concise"), "tiny"),
      button(tr("加载历史完整版", "Load legacy full"),
             () => this.loadHandbookPrompt("full"), "tiny"),
      button(tr("加载核心章节", "Load core"), () => this.loadHandbookPrompt("core"), "tiny"),
    ]));
    this.promptSystemEl = h("textarea", {
      class: "dcdu-textarea", style: "min-height:160px;",
      readonly: "readonly", spellcheck: "false",
    });
    this.promptSystemEl.addEventListener("input", () => { this.promptSystemDirty = true; });
    panel.appendChild(this.promptSystemEl);
    this.promptSourceEl = h("div", { class: "dcdu-note", text: tr(
      "尚未加载系统提示词", "System prompt not loaded yet") });
    panel.appendChild(this.promptSourceEl);
    panel.appendChild(h("div", { class: "dcdu-row" }, [
      button(tr("✋ 手动编辑（高级）", "Unlock edit (advanced)"), () => {
        const ok = window.confirm(tr(
          "系统提示词是高级参数：默认已使用 h3_v3_contract（V3.0 契约对齐·唯一提示词，37,457 字）。\n"
            + "手动修改会影响所有生成，确认解锁编辑吗？",
          "System prompt is advanced; the default is h3_v3_contract (V3.0, 37,457 chars).\nUnlock editing?"));
        if (!ok) return;
        this.promptSystemEl.removeAttribute("readonly");
        this.promptSystemEl.focus();
      }, "tiny"),
      button(tr("保存为面板默认", "Save as default"), () => this.savePromptSystem(), "green tiny"),
      button(tr("复制", "Copy"), () => {
        try { navigator.clipboard.writeText(this.promptSystemEl.value || ""); } catch (e) { /* ignore */ }
      }, "tiny"),
    ]));
    panel.appendChild(h("div", { class: "dcdu-note", text: tr(
      "默认：h3_v3_contract（V3.0 契约对齐·唯一提示词，37,457 字）全文作为系统提示词，不追加其它规则块；"
      + "「加载旧精简版 V1.0」「加载历史完整版」等旧版本仅作对照。"
      + "保存：载入的是预置 → 存预设键（如 h3_v3_contract，文档更新后自动跟随）；"
      + "手动改过 → 存你改的全文（服务端上限 8000 字符）。均写入面板默认（config/user.yaml），下次运行生效。",
      "Default: h3_v3_contract (V3.0 contract-aligned, 37,457 chars) as the only system prompt. "
      + "Saving a preset stores its key (e.g. h3_v3_contract); manual edits store your text "
      + "(server limit 8000 chars). Writes panel default (config/user.yaml), effective next run.") }));
    panel.appendChild(h("div", { class: "dcdu-title", style: "margin-top:8px;" }, [
      h("span", { text: tr("运行后 · 逐段视频提示词（只读）", "Generated per-segment prompts (read-only)") }),
    ]));
    this.promptSegmentsEl = h("div", { class: "dcdu-note", text: tr(
      "运行一次后自动显示每段实际提示词（便于核对 H3 精简版规则/字幕禁用是否生效）",
      "After a run, per-segment prompts appear here for verification.") });
    panel.appendChild(this.promptSegmentsEl);
    return panel;
  }

  async loadHandbookPrompt(mode = "concise", opts = {}) {
    if (!this.promptSystemEl) return null;
    const spec = this._handbookModeSpec(mode);
    this.promptSystemEl.dataset.loadMode = mode;
    this.promptSystemEl.value = tr("加载中…", "Loading…");
    this.setPromptSource(tr(`加载中：${spec.label}…`, `Loading ${spec.label}…`));
    let hit = null;
    let fallback = null;
    let lastError = null;
    for (const alias of spec.aliases) {
      try {
        const data = await this.apiGet(
          `/bettercomfyui/prompt/handbook?mode=${encodeURIComponent(alias)}`);
        const text = String(data.text || "");
        const key = String(data.key || "");
        if (!text) continue;
        if (!spec.key || key === spec.key) { hit = { text, key, alias }; break; }
        if (!fallback) fallback = { text, key, alias };
        else if (fallback.key === key) break;  // 后面的别名返回同一个预设，不必再问
      } catch (err) {
        lastError = err;
      }
    }
    const got = hit || fallback;
    if (!got) {
      this.promptSystemEl.value = tr(
        `加载失败：${lastError ? lastError.message : "手册数据为空"}`,
        `Load failed: ${lastError ? lastError.message : "handbook empty"}`);
      this.promptSystemEl.removeAttribute("readonly");
      this.setPromptSource(tr(
        `未取到手册：检查 ${spec.file} 与 /bettercomfyui/prompt/handbook 路由`,
        `No handbook: check ${spec.file} and the /bettercomfyui/prompt/handbook route`));
      return null;
    }
    const chars = got.text.length;
    this.promptSystemEl.value = got.text;
    this.promptSystemEl.setAttribute("readonly", "readonly");
    this.promptSystemEl.dataset.presetKey = got.key || "";
    this.promptSystemEl.dataset.loadMode = hit ? mode : (got.key === "h3_handbook" ? "full" : mode);
    this.promptSystemDirty = false;
    if (hit) {
      this.setPromptSource(tr(
        `来源：${spec.label} · ${chars.toLocaleString()} 字 · key=${got.key} · 文件 ${spec.file}`
          + (opts.fromPanel ? " · 面板默认" : ""),
        `Source: ${spec.label} · ${chars} chars · key=${got.key} · ${spec.file}`
          + (opts.fromPanel ? " · panel default" : "")));
    } else {
      // 服务端路由还没把该 mode 映射到对应预设（例如 routes.py 尚未支持 mode=concise）：
      // 如实说明拿到的是哪个预设，不冒充精简版。
      this.setPromptSource(tr(
        `⚠️ 实际取到 ${got.key}（${chars.toLocaleString()} 字），不是 ${spec.label}：`
          + `需服务端路由把 mode=${spec.aliases[0]} 映射到预设 ${spec.key}（数据文件 ${spec.file}）`,
        `⚠️ Server returned ${got.key} (${chars} chars), not ${spec.label}; `
          + `map mode=${spec.aliases[0]} to preset ${spec.key} (${spec.file})`));
    }
    return got.text;
  }

  async loadPromptPanel() {
    if (!this.promptSystemEl) return;
    try {
      const data = await this.apiGet("/bettercomfyui/panel/config");
      const defaults = (data && data.defaults) || {};
      const sp = String(defaults.system_prompt || data.system_prompt || "").trim();
      if (sp) {
        const mode = this._presetModeOf(sp);
        if (mode) {
          // 面板存的是手册预设（键或旧标签）→ 直接展示解析后的全文
          await this.loadHandbookPrompt(mode, { fromPanel: true });
          this.promptSystemEl.dataset.source = "panel-preset";
          return;
        }
        if (this._isBuiltinPresetKey(sp)) {
          this.promptSystemEl.value = sp;
          this.promptSystemEl.setAttribute("readonly", "readonly");
          this.promptSystemEl.dataset.loadMode = "preset-key";
          this.promptSystemEl.dataset.presetKey = sp;
          this.promptSystemDirty = false;
          this.setPromptSource(tr(
            `面板默认=内置预设 "${sp}"（不是 H3 手册全文；可用上方按钮加载 V3.0/旧版）`,
            `Panel default preset "${sp}" (not the handbook text)`));
          return;
        }
        this.promptSystemEl.value = sp;
        this.promptSystemEl.setAttribute("readonly", "readonly");
        this.promptSystemEl.dataset.loadMode = "custom";
        this.promptSystemEl.dataset.source = "panel";
        this.promptSystemDirty = false;
        this.setPromptSource(tr(
          `面板默认=自定义系统提示词（${sp.length.toLocaleString()} 字，保存在 config/user.yaml）`,
          `Panel default = custom system prompt (${sp.length} chars, config/user.yaml)`));
        return;
      }
      // 面板没保存过默认提示词 → 默认加载 V3.0 唯一提示词全文（37,457 字）
      await this.loadHandbookPrompt("v3");
    } catch (err) {
      await this.loadHandbookPrompt("v3").catch(() => {});
    }
  }

  async savePromptSystem() {
    const el = this.promptSystemEl;
    const raw = String(el?.value || "");
    if (!raw.trim()) { alert(tr("提示词为空，未保存", "Empty prompt; not saved")); return; }
    const mode = String(el?.dataset.loadMode || "");
    const presetKey = String(el?.dataset.presetKey || "");
    const isPreset = !!presetKey && !this.promptSystemDirty
      && ["v3", "concise", "full", "core", "preset-key"].includes(mode);
    if (isPreset) {
      try {
        await this.apiPost("/bettercomfyui/panel/config", { system_prompt: presetKey });
        el.setAttribute("readonly", "readonly");
        el.dataset.source = "panel-preset";
        this.setPromptSource(tr(
          `已保存为面板默认：预设 ${presetKey}（文档更新后自动跟随，下次运行生效）`,
          `Saved panel default preset ${presetKey} (effective next run)`));
        alert(tr(
          `已保存为面板默认系统提示词：预设 ${presetKey}\n`
            + "（存的是预设键而不是 1 万多字全文：文档更新后自动跟随，也避开 8000 字符保存上限）",
          `Saved preset ${presetKey} as panel default`));
      } catch (err) {
        alert(tr(`保存失败：${err.message}`, `Save failed: ${err.message}`));
      }
      return;
    }
    if (raw.length > 8000) {
      alert(tr(
        `面板保存自定义系统提示词有 8000 字符上限（当前 ${raw.length} 字）。\n`
          + "请改用上方预置按钮（载入后直接保存 = 存预设键），或精简后再保存。",
        `Custom system prompt is limited to 8000 chars (now ${raw.length}); use a preset.`));
      return;
    }
    try {
      await this.apiPost("/bettercomfyui/panel/config", { system_prompt: raw });
      el.setAttribute("readonly", "readonly");
      el.dataset.source = "panel";
      this.promptSystemDirty = false;
      this.setPromptSource(tr(
        `已保存为面板默认：自定义系统提示词（${raw.length.toLocaleString()} 字，下次运行生效）`,
        `Saved custom system prompt (${raw.length} chars, effective next run)`));
      alert(tr("已保存为面板默认系统提示词（下次运行生效）",
               "Saved as panel default system prompt (effective next run)"));
    } catch (err) {
      alert(tr(`保存失败：${err.message}`, `Save failed: ${err.message}`));
    }
  }

  renderPromptSegments() {
    if (!this.promptSegmentsEl) return;
    const report = this.lastReport || {};
    const prompts = Array.isArray(report.video_prompts) ? report.video_prompts
      : (Array.isArray(report.prompts?.segments) ? report.prompts.segments : []);
    const durations = Array.isArray(report.video_durations) ? report.video_durations : [];
    if (!prompts.length) {
      this.promptSegmentsEl.textContent = tr(
        "运行一次后自动显示每段实际提示词（含 AI 输出的段时长，便于核对 4–15s 规则/字幕禁用）",
        "After a run, per-segment prompts + AI durations appear here for verification.");
      return;
    }
    this.promptSegmentsEl.innerHTML = "";
    prompts.slice(0, 60).forEach((text, idx) => {
      const dur = Number(durations[idx] || 0);
      const prefix = dur > 0 ? `#${idx + 1} · ${dur.toFixed(1)}s · ` : `#${idx + 1} `;
      this.promptSegmentsEl.appendChild(h("div", {
        class: "dcdu-note",
        text: prefix + String(text || "").slice(0, 600),
      }));
    });
  }

  // ---- ROUND56：转场决策（默认程序自动决定，逐处可见） ----------------
  renderTransitionDecision() {
    const el = this.transitionDecisionEl;
    if (!el) return;
    const report = this.lastReport || {};
    const d = (report && report.transition_decision) || {};
    const cuts = Array.isArray(d.cuts) ? d.cuts : [];
    if (!cuts.length) {
      el.textContent = tr(
        "转场决策：默认「AI自动（按剧情）」——运行后这里显示每处接缝：第 N→N+1 段、转场类型/时长/原因。",
        "Transition decisions: AI auto by default — after a run this shows each cut type/duration/reason.");
      return;
    }
    el.innerHTML = "";
    el.appendChild(h("div", { class: "dcdu-chip on", text: tr(
      `转场决策：${d.cut_count != null ? d.cut_count : cuts.length} 处接缝 · 自动=${d.auto ? "开" : "关"} · ${d.rules_version || ""}`,
      `Transitions: ${d.cut_count != null ? d.cut_count : cuts.length} cuts · auto=${d.auto ? "on" : "off"}`) }));
    cuts.forEach((c) => {
      const i = Number(c.cut_index || 0);
      const label = `${c.transition || "无"}${c.duration ? ` ${c.duration}s` : ""}`;
      el.appendChild(h("div", { class: "dcdu-note", text:
        tr(`接缝 ${i + 1}→${i + 2}：${label} — ${c.reason || ""}`,
           `cut ${i + 1}→${i + 2}: ${label} — ${c.reason || ""}`) }));
    });
  }

  // ---- 顶部（品牌 + 状态）----------------------------------------------
  buildHead() {
    const taskVal = String(readVal(this.node, "任务类型", ""));
    const statusChips = h("span", { class: "dcdu-status-inline" });
    this.headStatusEl = statusChips;
    this.taskChip = h("span", { class: "dcdu-chip on", text: `任务 ${taskVal.split(" ")[0] || "?"}` });
    const contChip = h("span", { class: "dcdu-chip" });
    this.headContChip = contChip;
    this.hardwareChip = h("span", { class: "dcdu-chip", text: "硬件检测中…" });
    return h("div", { class: "dcdu-head" }, [
      h("span", { class: "dcdu-brand", text: "剧核 · DramaCore" }),
      h("span", { class: "dcdu-tag", text: "UI 2026-10-20-R58" }),
      h("span", { class: "dcdu-sub", text: "单节点导演台 · 多品牌硬件适配（NVIDIA/AMD/Intel/Apple/昇腾）" }),
      h("span", { class: "dcdu-spacer" }),
      this.taskChip,
      contChip,
      this.hardwareChip,
      statusChips,
    ]);
  }

  // ---- ① 22 帧段间连续（固定顶部，用户点名不能丢）-----------------------
  buildContinuity() {
    const node = this.node;
    const panel = h("div", { class: "dcdu-panel dcdu-cont" });
    panel.appendChild(h("div", { class: "dcdu-title" }, [
      h("span", { text: "⑤ 22 帧段间连续" }),
      h("span", { class: "dcdu-tag green", text: "核心 · 不可用长表单替代" }),
      h("span", { class: "dcdu-hint", text: "上一段尾部帧 + 音频 pin 进下一段开头，结果裁前缀 → 前 N 帧 = 上一段后 N 帧" }),
    ]));

    // 主开关 + 帧档位 + 音频上下文
    const master = switchControl(node, "段间连续", "段间连续（主开关）", () => this.reloadContinuity());
    const chips = h("div", { class: "dcdu-chips" });
    this.frameChipsEl = chips;
    for (const frames of CONT_FRAME_CHOICES) {
      const chip = h("button", {
        class: "dcdu-frame-chip",
        title: frames === 22 ? "官方基线：video=22 帧（H3 网格 5/22/39/56）" : `H3 motion context 网格档位 ${frames} 帧`,
        html: `${frames}<span class="dcdu-u">帧</span>`,
        onclick: () => {
          writeVal(node, "连续帧数", frames);
          if (frames === 22) writeVal(node, "音频上下文帧数", DEFAULT_AUDIO_CONTEXT);
          this.reloadContinuity();
          this.onParamChanged();
        },
      });
      chips.appendChild(chip);
    }
    this.audioCtxEl = h("span", { class: "dcdu-chip", text: "音频上下文 24 帧" });
    const syncAudioBtn = button("音频同步 24", () => {
      writeVal(node, "音频上下文帧数", DEFAULT_AUDIO_CONTEXT);
      this.reloadContinuity();
    }, "tiny");
    const pinSegs = switchControl(node, "每段引用上段", "每段 pin 上段（首段除外）", () => this.reloadContinuity());
    const keepTail = switchControl(node, "保完整尾部", "保完整尾部", () => this.reloadContinuity());

    panel.appendChild(h("div", { class: "dcdu-row" }, [master, h("span", { class: "dcdu-sub", text: "帧档位" }), chips, this.audioCtxEl, syncAudioBtn]));
    panel.appendChild(h("div", { class: "dcdu-row" }, [
      h("label", { text: "连续模式" }), selectControl(node, "连续模式", {
        labels: { "guide 引导（官方 motion context）": "guide 引导（官方 motion context）", "continue 引导+重绘（latent 续写）": "continue 引导+重绘（latent 续写）" },
      }),
      h("label", { text: "重绘幅度" }),
      this.rewidthWrap = h("span", { style: "display:inline-flex;align-items:center;gap:6px;" }, [
        numberInput(node, "重绘幅度", { width: "84px" }),
        h("span", { class: "dcdu-sub", text: "仅 continue 生效" }),
      ]),
      pinSegs,
      keepTail,
    ]));

    // 22 帧 pin 可视化条（每次参数变化重画）
    this.contStripEl = h("div", { class: "dcdu-pin-strip" });
    panel.appendChild(this.contStripEl);
    this.contSummaryEl = h("div", { class: "dcdu-note" });
    panel.appendChild(this.contSummaryEl);
    return panel;
  }

  reloadContinuity() {
    const node = this.node;
    const enabled = Boolean(readVal(node, "段间连续", true));
    const frames = Number(readVal(node, "连续帧数", 22)) || 22;
    const audio = Number(readVal(node, "音频上下文帧数", DEFAULT_AUDIO_CONTEXT)) || 0;
    const mode = String(readVal(node, "连续模式", "guide"));
    const pinned = Boolean(readVal(node, "每段引用上段", true));
    const keepTail = Boolean(readVal(node, "保完整尾部", true));
    const redraw = Number(readVal(node, "重绘幅度", 0.1)) || 0;

    // 档位高亮
    for (const chip of this.frameChipsEl?.querySelectorAll(".dcdu-frame-chip") || []) {
      chip.classList.toggle("on", chip.textContent?.startsWith(String(frames)));
    }
    if (this.audioCtxEl) {
      this.audioCtxEl.textContent = `音频上下文 ${audio} 帧`;
      this.audioCtxEl.className = `dcdu-chip ${audio === DEFAULT_AUDIO_CONTEXT ? "ok" : "warn"}`;
    }
    if (this.headContChip) {
      this.headContChip.textContent = enabled ? `22f 连续 ON · ${frames}f` : "段间连续 OFF";
      this.headContChip.className = `dcdu-chip ${enabled ? "ok" : "warn"}`;
    }
    if (this.rewidthWrap) this.rewidthWrap.style.opacity = mode.includes("continue") ? "1" : ".45";

    // pin 可视化
    const count = Math.max(1, Number(readVal(node, "段数", 1)) || this.segs.length || 1);
    const strip = this.contStripEl;
    if (!strip) return;
    strip.innerHTML = "";
    for (let i = 0; i < count; i += 1) {
      const isFirst = i === 0;
      const seg = this.segs[i] || {};
      const duration = Number(seg.durationSec ?? readVal(node, "每段时长_秒", 5.167)) || 5.167;
      strip.appendChild(h("div", { class: `dcdu-seg-box${isFirst ? " first" : ""}` }, [
        h("div", { class: "dcdu-seg-id", text: `S${i + 1}` }),
        h("div", { class: "dcdu-seg-meta", text: `${duration.toFixed(2)}s · ${Math.round(duration * (Number(readVal(node, "帧率", 24)) || 24))} 帧` }),
        h("div", { class: "dcdu-seg-meta", text: isFirst ? "首段：不 pin" : (pinned ? "pin 上段 ✓" : "硬切") }),
      ]));
      if (i < count - 1) {
        const jointPinned = enabled && pinned;
        strip.appendChild(h("div", { class: `dcdu-joint${jointPinned ? "" : " hard"}` }, [
          h("div", { class: "dcdu-joint-line" }),
          h("div", { class: "dcdu-joint-badge", text: jointPinned ? `${frames}f ⇄` : "硬切" }),
          h("div", { class: "dcdu-joint-cap", text: jointPinned ? `audio ${audio}f` : "无重叠" }),
        ]));
      }
    }
    // 连续实证摘要
    this.contSummaryEl.textContent = enabled
      ? `段间连续：ON ｜ 帧档位 ${frames}（${frames === 22 ? "官方基线" : "非基线档位"}）｜ 模式 ${mode.startsWith("continue") ? "continue（重绘 " + redraw + "）" : "guide"} ｜ 音频上下文 ${audio} 帧 ｜ 保完整尾部 ${keepTail ? "开" : "关"} ｜ pin 规则 ${pinned ? "每段引用上段（首段除外）" : "硬切"}`
      : "段间连续：OFF —— 段与段之间是硬切（不会 pin 上一段尾部帧）；打开主开关即恢复 22 帧档位可视化。";
    this.contSummaryEl.className = `dcdu-note${enabled ? "" : " warn"}`;
    this.drawCanvas();
    this.refreshStatus();
  }

  // ---- ② 素材库 --------------------------------------------------------
  buildAssets() {
    const node = this.node;
    const panel = h("div", { class: "dcdu-panel" });
    panel.appendChild(h("div", { class: "dcdu-title" }, [
      h("span", { text: "⑥ 素材库（程序自动生成 · 可接管）" }),
      h("span", { class: "dcdu-tag", text: "参考图槽位 + 生成/审核参数 + LOCK 状态" }),
      h("span", { class: "dcdu-hint", text: "角色/场景/物品/服装参考图在运行时由项目素材库自动注入并 LOCK" }),
    ]));

    // 四个素材槽位卡
    const grid = h("div", { class: "dcdu-assets" });
    this.assetCards = {};
    for (const [key, title, role, widgetName, accept] of [
      ["start", "首帧图", "i2v / fl2v 起始帧（留空=第一个已 LOCK 素材）", "首帧图片路径", "image"],
      ["end", "尾帧图", "fl2v 尾帧（留空=第二个已 LOCK 素材）", "尾帧图片路径", "image"],
      ["video", "参考视频", "r2v 参考视频（<Video 1>）", "参考视频路径", "video"],
      ["audio", "参考音频", "r2v 参考音频（<Audio 1>）", "参考音频路径", "audio"],
    ]) {
      const preview = h("div", { class: "dcdu-asset-preview" });
      const lock = h("span", { class: "dcdu-lock none", text: "未设置" });
      preview.appendChild(lock);
      const pathInput = h("input", {
        class: "dcdu-input",
        value: String(readVal(node, widgetName, "") || ""),
        placeholder: accept === "image" ? "绝对路径（output/input 下的图可预览）" : "绝对路径",
        onchange: (ev) => {
          writeVal(node, widgetName, ev.target.value);
          this.refreshAssets();
          this.onParamChanged();
        },
      });
      const card = h("div", { class: "dcdu-asset-card" }, [
        preview,
        h("div", { class: "dcdu-asset-body" }, [
          h("div", { class: "dcdu-asset-title" }, [h("span", { text: title }), h("span", { class: "dcdu-role", text: role })]),
          pathInput,
          h("div", { class: "dcdu-asset-path", text: "—" }),
        ]),
      ]);
      this.assetCards[key] = { card, preview, lock, pathInput, widgetName };
      grid.appendChild(card);
    }
    panel.appendChild(grid);

    // 生成 / 审核参数
    panel.appendChild(h("div", { class: "dcdu-row", style: "margin-top:8px;" }, [
      switchControl(node, "生成素材图", "生成素材图（节点内部生图→入库→审核）"),
      switchControl(node, "复用已锁定素材", "复用已 LOCKED 素材"),
    ]));
    panel.appendChild(h("div", { class: "dcdu-grid" }, [
      field("素材最多生成个数（每类）", numberInput(node, "素材最多生成个数")),
      field("素材审核轮数（CNT1）", numberInput(node, "素材审核轮数")),
      field("素材审核模式", selectControl(node, "素材审核模式")),
      field("图像模型（素材生图）", selectControl(node, "图像模型")),
      field("图像分辨率", numberInput(node, "图像分辨率")),
      field("图像步数", numberInput(node, "图像步数")),
      field("图像 CFG", numberInput(node, "图像CFG")),
      field("素材审核阈值（-1=跟随配置）", numberInput(node, "素材审核阈值")),
      field("源视频路径（v2v/rv2v 用，可选）", textInput(node, "源视频路径")),
    ]));

    // 运行时素材库 / LOCK 状态
    const statusBox = h("div", { class: "dcdu-status", style: "margin-top:8px;" });
    this.assetStatusEl = statusBox;
    panel.appendChild(h("div", { class: "dcdu-note", text: "运行时素材库（点 Queue 跑完一次后由执行输出回填）：" }));
    panel.appendChild(statusBox);
    return panel;
  }

  refreshAssets() {
    const node = this.node;
    for (const [key, entry] of Object.entries(this.assetCards || {})) {
      const path = String(readVal(node, entry.widgetName, "") || "");
      const url = previewUrl(path);
      entry.preview.innerHTML = "";
      if (url) {
        const img = h("img", { src: url, alt: key });
        img.addEventListener("error", () => {
          entry.preview.innerHTML = "";
          entry.preview.appendChild(h("div", { class: "dcdu-asset-ph", text: "无法预览（文件不在 ComfyUI input/output 目录）" }));
        });
        entry.preview.appendChild(img);
      } else {
        entry.preview.appendChild(h("div", {
          class: "dcdu-asset-ph",
          text: key === "video" ? "🎞 参考视频（无缩略图）" : key === "audio" ? "🎵 参考音频（无波形）" : "🖼 未设置首/尾帧\n运行时由素材库 LOCK 图自动填充",
        }));
      }
      const lock = h("span", { class: `dcdu-lock${path ? "" : " none"}`, text: path ? "已指定" : "未设置" });
      entry.preview.appendChild(lock);
      entry.pathInput.value = path;
      entry.card.querySelector(".dcdu-asset-path").textContent = path ? basename(path) : "—";
    }
    this.refreshStatus();
  }

  // ---- ③ 时间轴 / 分段 -------------------------------------------------
  buildTimeline() {
    const node = this.node;
    const panel = h("div", { class: "dcdu-panel" });
    panel.appendChild(h("div", { class: "dcdu-title" }, [
      h("span", { text: "⑦ 时间轴 / 分段（程序自动生成 · 可接管）" }),
      h("span", { class: "dcdu-tag", text: "canvas 绘制 · 与「时间轴JSON」双向同步" }),
      this.timelineInfoEl = h("span", { class: "dcdu-hint", text: "自动构造" }),
    ]));
    // 参数行
    panel.appendChild(h("div", { class: "dcdu-row" }, [
      h("label", { text: "段数" }), numberInput(node, "段数"),
      h("label", { text: "每段时长(秒)" }), numberInput(node, "每段时长_秒"),
      h("label", { text: "帧率" }), numberInput(node, "帧率"),
      h("label", { text: "编辑模式" }), selectControl(node, "编辑模式"),
      h("label", { text: "任务类型" }), selectControl(node, "任务类型"),
    ]));
    panel.appendChild(h("div", { class: "dcdu-row" }, [
      button("从参数重建分段", () => this.rebuildFromParams(), "primary"),
      button("写入时间轴JSON（高级覆盖）", () => this.syncJsonFromUi(), "green"),
      button("清空时间轴JSON（回自动构造）", () => this.clearJson(), "danger"),
      h("span", { class: "dcdu-note", text: "默认自动构造 timeline；只有点「写入」才会变成用户 JSON 覆盖（此时素材自动注入会关闭）" }),
    ]));
    // canvas
    const wrap = h("div", { class: "dcdu-canvas-wrap" });
    const canvas = document.createElement("canvas");
    canvas.width = 1120;
    canvas.height = 152;
    wrap.appendChild(canvas);
    panel.appendChild(wrap);
    this.canvas = canvas;
    panel.appendChild(h("div", { class: "dcdu-timeline-legend" }, [
      h("span", { html: "<i style='background:#2d6cdf'></i>分段" }),
      h("span", { html: "<i style='background:#2e9e57'></i>22 帧重叠 pin" }),
      h("span", { html: "<i style='background:#7a4b4b'></i>硬切" }),
      h("span", { html: "<i style='background:#8a6d3b'></i>参考图/首尾帧" }),
      h("span", { text: "拖动节点宽度可自适应" }),
    ]));
    // 分段卡列表
    this.segListEl = h("div", { class: "dcdu-seglist" });
    panel.appendChild(this.segListEl);
    panel.appendChild(h("div", { class: "dcdu-row" }, [
      button("+ 添加分段", () => {
        const n = Math.min(12, (Number(readVal(node, "段数", 1)) || 1) + 1);
        writeVal(node, "段数", n);
        this.rebuildFromParams();
      }),
      h("span", { class: "dcdu-note", text: "提示词留空 = 运行时由剧本自动生成每段提示词" }),
    ]));
    // 时间轴 JSON 高级编辑
    this.jsonArea = h("textarea", { class: "dcdu-textarea", style: "min-height:76px;", placeholder: "时间轴JSON（留空 = 自动构造）" });
    this.jsonArea.value = String(readVal(node, "时间轴JSON", "") || "");
    this.jsonArea.addEventListener("change", () => {
      writeVal(node, "时间轴JSON", this.jsonArea.value);
      this.loadSegments();
      this.reloadAll();
    });
    panel.appendChild(this.jsonArea);
    return panel;
  }

  loadSegments() {
    const node = this.node;
    const raw = String(readVal(node, "时间轴JSON", "") || "").trim();
    let parsed = null;
    if (raw) {
      try {
        parsed = JSON.parse(raw);
      } catch (e) {
        parsed = null;
      }
    }
    if (parsed && Array.isArray(parsed.segments) && parsed.segments.length) {
      this.timelineSource = "user";
      this.timelineJson = parsed;
      this.segs = parsed.segments.map((seg, idx) => ({
        id: seg.id || `s${idx}`,
        durationSec: Number(seg.durationSec || (Number(seg.length || 0) / Number(parsed.frameRate || readVal(node, "帧率", 24))) || 5.167),
        prompt: String(seg.prompt || ""),
        refs: Array.isArray(seg.refs) ? seg.refs : [],
        refAudios: Array.isArray(seg.refAudios) ? seg.refAudios : [],
        refVideos: Array.isArray(seg.refVideos) ? seg.refVideos : [],
        continuityFromPrev: seg.continuityFromPrev !== false,
        startImage: seg.startImage || null,
        endImage: seg.endImage || null,
        imageFile: seg.imageFile || seg.genImage?.imageFile || "",
        taskType: String(seg.taskType || ""),
      }));
    } else {
      this.timelineSource = "auto";
      this.timelineJson = null;
      const count = Math.max(1, Math.min(12, Number(readVal(node, "段数", 1)) || 1));
      const duration = Number(readVal(node, "每段时长_秒", 5.167)) || 5.167;
      const pin = Boolean(readVal(node, "每段引用上段", true));
      const old = this.segs || [];
      this.segs = [];
      for (let i = 0; i < count; i += 1) {
        this.segs.push({
          id: `s${i}`,
          durationSec: old[i]?.durationSec ?? duration,
          prompt: old[i]?.prompt ?? "",
          refs: old[i]?.refs ?? [],
          refAudios: old[i]?.refAudios ?? [],
          refVideos: old[i]?.refVideos ?? [],
          continuityFromPrev: pin,
          startImage: old[i]?.startImage ?? null,
          endImage: old[i]?.endImage ?? null,
          imageFile: "",
          taskType: String(readVal(node, "任务类型", "")),
        });
      }
    }
  }

  rebuildFromParams() {
    // 从参数重建 = 清空 JSON 覆盖，回到自动构造 + UI 分段
    writeVal(this.node, "时间轴JSON", "");
    this.jsonArea && (this.jsonArea.value = "");
    this.segs = [];
    this.loadSegments();
    this.reloadAll();
  }

  serializeTimeline() {
    const node = this.node;
    const fps = Number(readVal(node, "帧率", 24)) || 24;
    let cursor = 0;
    const segments = this.segs.map((seg, idx) => {
      const frames = Math.max(1, Math.round((Number(seg.durationSec) || 5.167) * fps));
      const row = {
        id: seg.id || `s${idx}`,
        start: cursor,
        length: frames,
        frameCount: frames,
        durationSec: Number(Number(seg.durationSec || 5.167).toFixed(3)),
        prompt: String(seg.prompt || ""),
        negativePrompt: "",
        taskType: String(seg.taskType || readVal(node, "任务类型", "")),
        refs: seg.refs || [],
        refAudios: seg.refAudios || [],
        refVideos: seg.refVideos || [],
        continuityFromPrev: Boolean(seg.continuityFromPrev),
        refImageSize: "match",
        loras: [],
        genImage: { imageFile: seg.imageFile || "" },
        imageFile: seg.imageFile || "",
        startImage: seg.startImage || null,
        endImage: seg.endImage || null,
      };
      cursor += frames;
      return row;
    });
    const contEnabled = Boolean(readVal(node, "段间连续", true));
    const w = Number(readVal(node, "宽度", 864)) || 864;
    const hgt = Number(readVal(node, "高度", 480)) || 480;
    const out = {
      mode: "fixed",
      longEdge: Number(readVal(node, "参考最长边", 864)) || 864,
      width: w,
      height: hgt,
      maxExportFrames: 0,
      exportMode: "all",
      audioMode: "generate",
      refImageSize: "match",
      continuityEnabled: contEnabled,
      continuityOverlapFrames: Number(readVal(node, "连续帧数", 22)) || 22,
      continuityMode: String(readVal(node, "连续模式", "guide")).startsWith("continue") ? "continue" : "guide",
      continuityRedraw: Number(readVal(node, "重绘幅度", 0.1)) || 0.1,
      continuityKeepTail: Boolean(readVal(node, "保完整尾部", true)),
    };
    return {
      version: 5,
      timelineMode: "prompt_batch",
      editMode: "segment",
      frameRate: fps,
      totalFrames: cursor,
      width: w,
      height: hgt,
      refMaxSize: out.longEdge,
      output: out,
      videoClips: [],
      video: { fileName: "", videoFile: "", subfolder: "", type: "input", frames: [], frameMap: [] },
      global: {
        taskType: String(readVal(node, "任务类型", "")),
        prompt: String(readVal(node, "统一风格", "") || ""),
        refs: [], refAudios: [], refVideos: [],
        referenceVideo: {}, continuousReference: false, loras: [],
      },
      segments,
      runSelectEnabled: false,
      runSelection: [],
    };
  }

  syncJsonFromUi() {
    this.loadSegments();
    const json = JSON.stringify(this.serializeTimeline(), null, 1);
    writeVal(this.node, "时间轴JSON", json);
    if (this.jsonArea) this.jsonArea.value = json;
    this.loadSegments();
    this.reloadAll();
  }

  clearJson() {
    writeVal(this.node, "时间轴JSON", "");
    if (this.jsonArea) this.jsonArea.value = "";
    this.loadSegments();
    this.reloadAll();
  }

  renderSegList() {
    const node = this.node;
    const list = this.segListEl;
    if (!list) return;
    list.innerHTML = "";
    this.segs.forEach((seg, idx) => {
      const durInput = h("input", {
        class: "dcdu-input", type: "number", min: "1", max: "30", step: "0.1",
        value: String(seg.durationSec ?? 5.167),
        onchange: (ev) => {
          seg.durationSec = Math.max(1, Math.min(30, Number(ev.target.value) || 5.167));
          this.drawCanvas();
          this.reloadContinuity();
          this.markDirty();
        },
      });
      const prompt = h("textarea", {
        class: "dcdu-textarea dcdu-seg-prompt",
        placeholder: `S${idx + 1} 提示词（留空=运行时由剧本自动生成）`,
      });
      prompt.value = seg.prompt || "";
      prompt.addEventListener("change", () => {
        seg.prompt = prompt.value;
        this.drawCanvas();
        this.markDirty();
      });
      const refChips = h("div", { class: "dcdu-seg-refs" });
      const refCount = (seg.refs?.length || 0) + (seg.refAudios?.length || 0) + (seg.refVideos?.length || 0);
      refChips.appendChild(h("span", { class: "dcdu-ref-chip", text: `参考图 ${seg.refs?.length || 0}` }));
      refChips.appendChild(h("span", { class: "dcdu-ref-chip", text: `参考视频 ${seg.refVideos?.length || 0}` }));
      refChips.appendChild(h("span", { class: "dcdu-ref-chip", text: `参考音频 ${seg.refAudios?.length || 0}` }));
      if (seg.startImage) refChips.appendChild(h("span", { class: "dcdu-ref-chip", text: "首帧 ✓" }));
      if (seg.endImage) refChips.appendChild(h("span", { class: "dcdu-ref-chip", text: "尾帧 ✓" }));
      if (seg.imageFile) refChips.appendChild(h("span", { class: "dcdu-ref-chip", text: `素材 ${basename(seg.imageFile)}` }));
      if (idx === 0) refChips.appendChild(h("span", { class: "dcdu-ref-chip", text: "首段不 pin" }));
      else refChips.appendChild(h("span", { class: "dcdu-ref-chip", text: seg.continuityFromPrev ? `pin 上段 ${Number(readVal(node, "连续帧数", 22))}f` : "硬切" }));
      if (refCount === 0 && !seg.startImage && !seg.endImage) {
        refChips.appendChild(h("span", { class: "dcdu-note", text: "（运行时注入项目素材库 LOCKED 参考图）" }));
      }
      list.appendChild(h("div", { class: "dcdu-segitem" }, [
        h("div", { class: "dcdu-segno", text: `S${idx + 1}` }),
        h("div", {}, [h("span", { class: "dcdu-note", text: "时长(秒)" }), durInput]),
        prompt,
        h("div", { class: "dcdu-seg-side" }, [
          button("↑", () => this.moveSeg(idx, -1), "tiny"),
          button("↓", () => this.moveSeg(idx, 1), "tiny"),
          button("删", () => this.removeSeg(idx), "tiny danger"),
        ]),
        refChips,
      ]));
    });
  }

  moveSeg(idx, delta) {
    const target = idx + delta;
    if (target < 0 || target >= this.segs.length) return;
    const [item] = this.segs.splice(idx, 1);
    this.segs.splice(target, 0, item);
    this.drawCanvas();
    this.renderSegList();
    this.markDirty();
  }

  removeSeg(idx) {
    if (this.segs.length <= 1) return;
    this.segs.splice(idx, 1);
    writeVal(this.node, "段数", this.segs.length);
    this.drawCanvas();
    this.renderSegList();
    this.reloadContinuity();
    this.markDirty();
  }

  markDirty() {
    if (this.timelineSource !== "user" && !String(readVal(this.node, "时间轴JSON", "") || "").trim()) {
      if (this.timelineInfoEl) this.timelineInfoEl.textContent = "自动构造（UI 分段改动仅在点「写入时间轴JSON」后覆盖到节点）";
    }
    if (this.timelineInfoEl) {
      this.timelineInfoEl.textContent = this.timelineSource === "user"
        ? "用户 JSON 覆盖（与 UI 双向同步；改动后请点「写入时间轴JSON」）"
        : "自动构造（UI 分段改动不会自动写 JSON，避免关闭素材自动注入）";
    }
  }

  // ---- canvas 时间轴绘制（移植原导演台「画出来的时间轴」体验）------------
  drawCanvas() {
    const canvas = this.canvas;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const dpr = Math.max(1, Math.min(2, globalThis.devicePixelRatio || 1));
    const cssW = canvas.clientWidth || 1120;
    const cssH = 152;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    const node = this.node;
    const fps = Number(readVal(node, "帧率", 24)) || 24;
    const enabled = Boolean(readVal(node, "段间连续", true));
    const frames = Number(readVal(node, "连续帧数", 22)) || 22;
    const pinned = Boolean(readVal(node, "每段引用上段", true));
    const audioCtx = Number(readVal(node, "音频上下文帧数", DEFAULT_AUDIO_CONTEXT)) || 0;
    const segs = this.segs.length ? this.segs : [{ durationSec: 5.167, prompt: "" }];
    const totalSec = segs.reduce((sum, s) => sum + (Number(s.durationSec) || 5.167), 0);

    const padX = 10;
    const rulerH = 20;
    const trackY = rulerH + 8;
    const trackH = 62;
    const audioY = trackY + trackH + 8;
    const audioH = 18;
    const usableW = cssW - padX * 2;

    // 背景 + 标尺
    ctx.fillStyle = "#0f1317";
    ctx.fillRect(0, 0, cssW, cssH);
    ctx.strokeStyle = "#2a3138";
    ctx.lineWidth = 1;
    ctx.strokeRect(0.5, 0.5, cssW - 1, cssH - 1);
    ctx.fillStyle = "#6b7c88";
    ctx.font = "10px ui-monospace, Consolas, monospace";
    ctx.textAlign = "left";
    ctx.fillText(`总时长 ${totalSec.toFixed(2)}s ｜ 总帧 ${Math.round(totalSec * fps)} ｜ ${segs.length} 段`, padX, 13);

    // 分段卡
    let cursorX = padX;
    const jointW = 46;
    const segWidths = segs.map((s) => {
      const w = (Number(s.durationSec) || 5.167) / totalSec * (usableW - jointW * (segs.length - 1));
      return Math.max(70, w);
    });
    const totalW = segWidths.reduce((a, b) => a + b, 0) + jointW * Math.max(0, segs.length - 1);
    const scale = totalW > usableW ? usableW / totalW : 1;

    const palette = [
      ["#1c3d63", "#2d6cdf"], ["#1d3a2b", "#2e9e57"], ["#3b2f14", "#b8860b"],
      ["#3a1f33", "#a2457f"], ["#1f3350", "#4a9fd8"], ["#33251a", "#c07a3a"],
    ];
    for (let i = 0; i < segs.length; i += 1) {
      const w = segWidths[i] * scale;
      const seg = segs[i];
      const [fill, stroke] = palette[i % palette.length];
      ctx.fillStyle = fill;
      ctx.strokeStyle = stroke;
      ctx.lineWidth = 1.5;
      this.roundRect(ctx, cursorX, trackY, w - 2, trackH, 6);
      ctx.fill();
      ctx.stroke();

      ctx.fillStyle = "#e9f4ff";
      ctx.font = "bold 12px sans-serif";
      ctx.fillText(`S${i + 1}`, cursorX + 7, trackY + 16);
      ctx.fillStyle = "#b9cfe0";
      ctx.font = "10px ui-monospace, Consolas, monospace";
      ctx.fillText(`${Number(seg.durationSec || 5.167).toFixed(2)}s · ${Math.round((Number(seg.durationSec) || 5.167) * fps)}f`, cursorX + 7, trackY + 30);

      // 提示词摘要
      ctx.fillStyle = "#93a8b8";
      const promptText = String(seg.prompt || "（运行时由剧本生成）");
      ctx.fillText(this.ellipsis(ctx, promptText, Math.max(30, w - 16)), cursorX + 7, trackY + 44);

      // 参考图 / 首尾帧徽标
      const badges = [];
      if (seg.refs?.length) badges.push(`图×${seg.refs.length}`);
      if (seg.startImage) badges.push("首帧");
      if (seg.endImage) badges.push("尾帧");
      if (seg.imageFile) badges.push("素材");
      if (i === 0) badges.push("首段");
      ctx.fillStyle = "#d9b75a";
      ctx.font = "9px sans-serif";
      ctx.fillText(badges.join(" · "), cursorX + 7, trackY + trackH - 8);

      cursorX += w;
      // 关节：22 帧 pin 可视化（原导演台的 CONT_JOINT 概念）
      if (i < segs.length - 1) {
        const jx = cursorX - 1;
        const isPinned = enabled && pinned && seg.continuityFromPrev !== false;
        ctx.fillStyle = isPinned ? "#12331f" : "#241c1c";
        ctx.strokeStyle = isPinned ? "#2e9e57" : "#7a4b4b";
        ctx.lineWidth = 1;
        this.roundRect(ctx, jx, trackY + trackH / 2 - 9, jointW - 2, 18, 9);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = isPinned ? "#8dffb0" : "#d99b9b";
        ctx.font = "bold 9px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText(isPinned ? `${frames}f⇄` : "硬切", jx + (jointW - 2) / 2, trackY + trackH / 2 + 3);
        ctx.textAlign = "left";
        // 关节上下的短连接线
        ctx.strokeStyle = isPinned ? "#2e9e57" : "#5a4a4a";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(jx - 4, trackY + trackH / 2);
        ctx.lineTo(jx, trackY + trackH / 2);
        ctx.lineTo(jx + jointW - 2, trackY + trackH / 2);
        ctx.lineTo(jx + jointW + 2, trackY + trackH / 2);
        ctx.stroke();
        cursorX += jointW - 2;
      }
    }

    // 音频轨（audio context 24）
    const mute = String(readVal(node, "音频模式", "generate")).includes("mute");
    ctx.fillStyle = mute ? "#332020" : "#14262f";
    ctx.strokeStyle = mute ? "#7a4b4b" : "#2f6f8e";
    ctx.lineWidth = 1;
    this.roundRect(ctx, padX, audioY, usableW, audioH, 4);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = mute ? "#d99b9b" : "#8fc6ff";
    ctx.font = "10px sans-serif";
    const audioLabel = mute
      ? "🔇 音频轨：静音（audio mute）"
      : `🎵 音频轨：原生音画（audio ${String(readVal(node, "音频模式", "")).split(" ")[0]}）｜音频上下文 ${audioCtx} 帧`;
    ctx.fillText(audioLabel, padX + 8, audioY + 12);
    // 波形示意
    ctx.strokeStyle = mute ? "#553333" : "#2f6f8e";
    ctx.beginPath();
    for (let x = 0; x < usableW - 16; x += 3) {
      const amp = mute ? 1 : (Math.sin(x * 0.11) * 0.5 + 0.5) * 7 + 1;
      ctx.moveTo(padX + 8 + x, audioY + audioH - 4 - amp);
      ctx.lineTo(padX + 8 + x, audioY + audioH - 4);
    }
    ctx.stroke();
  }

  roundRect(ctx, x, y, w, h, r) {
    const radius = Math.min(r, Math.abs(w) / 2, Math.abs(h) / 2);
    ctx.beginPath();
    ctx.moveTo(x + radius, y);
    ctx.lineTo(x + w - radius, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + radius);
    ctx.lineTo(x + w, y + h - radius);
    ctx.quadraticCurveTo(x + w, y + h, x + w - radius, y + h);
    ctx.lineTo(x + radius, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - radius);
    ctx.lineTo(x, y + radius);
    ctx.quadraticCurveTo(x, y, x + radius, y);
    ctx.closePath();
  }

  ellipsis(ctx, text, maxWidth) {
    if (ctx.measureText(text).width <= maxWidth) return text;
    let lo = 0;
    let hi = text.length;
    while (lo < hi) {
      const mid = Math.ceil((lo + hi) / 2);
      if (ctx.measureText(text.slice(0, mid) + "…").width <= maxWidth) lo = mid;
      else hi = mid - 1;
    }
    return text.slice(0, lo) + "…";
  }

  // ---- ④ 生成 / 采样 / 输出 -------------------------------------------
  buildGeneration() {
    const node = this.node;
    const panel = h("div", { class: "dcdu-panel" });
    panel.appendChild(h("div", { class: "dcdu-title" }, [
      h("span", { text: "⑨ 生成 · 采样 · 输出" }),
      h("span", { class: "dcdu-tag", text: "导演台出片参数" }),
    ]));
    panel.appendChild(h("div", { class: "dcdu-grid" }, [
      field("视频 UNET", selectControl(node, "视频UNET")),
      field("CLIP 名", selectControl(node, "CLIP名")),
      field("视频 VAE", selectControl(node, "视频VAE")),
      field("音频 VAE", selectControl(node, "音频VAE")),
      field("视频 LoRA", selectControl(node, "视频LoRA")),
      field("LoRA 强度", numberInput(node, "LoRA强度")),
      field("宽度", numberInput(node, "宽度")),
      field("高度", numberInput(node, "高度")),
      field("参考最长边", numberInput(node, "参考最长边")),
      field("采样器", selectControl(node, "采样器")),
      field("调度器", selectControl(node, "调度器")),
      field("步数", numberInput(node, "步数")),
      field("CFG", numberInput(node, "cfg")),
      field("seed", numberInput(node, "seed")),
      field("噪声偏移·视频", numberInput(node, "噪声偏移_视频")),
      field("噪声偏移·音频", numberInput(node, "噪声偏移_音频")),
      field("音频模式", selectControl(node, "音频模式")),
      field("编辑模式", selectControl(node, "编辑模式")),
    ]));
    panel.appendChild(h("div", { class: "dcdu-row", style: "margin-top:6px;" }, [
      switchControl(node, "走Finalize剪辑", "走 Finalize 剪辑（拼接+封装+自检）"),
      switchControl(node, "导出原始帧", "导出原始帧"),
      switchControl(node, "输出修脸前", "输出修脸前"),
    ]));
    panel.appendChild(h("div", { class: "dcdu-grid" }, [
      field("剪辑 CRF", numberInput(node, "剪辑CRF")),
      field("输出格式", selectControl(node, "输出格式")),
      field("转场", selectControl(node, "转场")),
      field("转场时长（秒）", numberInput(node, "转场时长_秒")),
      field("字幕", selectControl(node, "字幕")),
      field("水印", selectControl(node, "水印")),
      field("封面帧", selectControl(node, "封面帧")),
    ]));
    panel.appendChild(h("div", { class: "dcdu-row", style: "margin-top:6px;" }, [
      switchControl(node, "显存清理_段间", "段间清理显存"),
      switchControl(node, "显存清理_二采前", "二采前清理"),
      switchControl(node, "显存清理_修脸前", "修脸前清理"),
      h("label", { text: "缓存编码" }), selectControl(node, "缓存编码"),
      h("label", { text: "缓存目录名" }), textInput(node, "缓存目录名"),
    ]));
    // ROUND56：补齐此前没有 UI 的高级参数（均在本蒙版区内，普通用户无需改）
    panel.appendChild(h("div", { class: "dcdu-row", style: "margin-top:8px;font-weight:700;" }, [
      h("span", { text: "高级（已给默认值；普通用户不用改）" })]));
    panel.appendChild(h("div", { class: "dcdu-grid" }, [
      field("目标总时长（分钟，0=不启用）", numberInput(node, "目标总时长_分钟")),
      field("每批段数（长片分批）", numberInput(node, "每批段数")),
      field("显存档位", selectControl(node, "显存档位")),
      field("tiled 采样块数（0=关闭）", numberInput(node, "tiled采样")),
      field("tiled 重叠像素", numberInput(node, "tiled重叠像素")),
      field("水印文字", textInput(node, "水印文字")),
    ]));
    panel.appendChild(h("div", { class: "dcdu-row" }, [
      switchControl(node, "断点续跑", "断点续跑"),
      switchControl(node, "批次间卸载模型", "批次间卸载模型"),
      switchControl(node, "剪枝模型", "剪枝模型"),
      switchControl(node, "出片", "出片（False=只出提示词）"),
      switchControl(node, "使用占位结果", "使用占位结果（无 Key 演示）"),
    ]));
    return panel;
  }

  // ---- ⑤ 提示词 / 增强 / 审核 -----------------------------------------
  buildEnhance() {
    const node = this.node;
    const panel = h("div", { class: "dcdu-panel" });
    panel.appendChild(h("div", { class: "dcdu-title" }, [
      h("span", { text: "⑩ 提示词 · 增强 · 审核" }),
      h("span", { class: "dcdu-tag", text: "Refine / SelfLift / FaceRefine / 阈值" }),
    ]));
    panel.appendChild(h("div", { class: "dcdu-grid" }, [
      field("系统提示词选择", selectControl(node, "系统提示词选择")),
      field("输出语言", selectControl(node, "输出语言")),
      field("分镜粒度", selectControl(node, "分镜粒度")),
      field("审核阈值", numberInput(node, "审核阈值")),
      field("段阈值覆盖", numberInput(node, "段阈值覆盖")),
      field("整片阈值覆盖", numberInput(node, "整片阈值覆盖")),
      field("整片失败策略", selectControl(node, "整片失败策略")),
      field("边界相似度阈值", numberInput(node, "边界相似度阈值")),
    ]));
    panel.appendChild(h("div", { class: "dcdu-field", style: "margin-top:6px;" }, [
      h("span", { text: "统一风格（全局提示）" }),
      textInput(node, "统一风格", { multiline: true, placeholder: "可选：全局风格/画质提示，例：电影感、冷色调、浅景深" }),
    ]));
    panel.appendChild(h("div", { class: "dcdu-field", style: "margin-top:6px;" }, [
      h("span", { text: "自定义系统提示词（覆盖系统提示词选择）" }),
      textInput(node, "自定义系统提示词", { multiline: true, placeholder: "留空 = 跟随面板配置 / 系统提示词选择" }),
    ]));
    // 增强开关
    panel.appendChild(h("div", { class: "dcdu-row", style: "margin-top:8px;font-weight:700;" }, [h("span", { text: "增强（内部开关，无需连线）" })]));
    panel.appendChild(h("div", { class: "dcdu-row" }, [
      switchControl(node, "启用SelfLift", "SelfLift 渐进一采"),
      switchControl(node, "启用Refine", "Refine 二采"),
      switchControl(node, "启用FaceRefine", "FaceRefine 修脸"),
      switchControl(node, "启用SemanticBridge", "SemanticBridge 语义桥"),
    ]));
    panel.appendChild(h("div", { class: "dcdu-grid" }, [
      field("SelfLift 高分步数", numberInput(node, "SelfLift高分步数")),
      field("SelfLift 低分比例", numberInput(node, "SelfLift低分比例")),
      field("SelfLift 过渡步", numberInput(node, "SelfLift过渡步")),
      field("Refine 模式", selectControl(node, "Refine模式")),
      field("Refine 次数", numberInput(node, "Refine次数")),
      field("Refine 目标百万像素", numberInput(node, "Refine目标百万像素")),
      field("Refine 放大方式", selectControl(node, "Refine放大方式")),
      field("人脸检测器", selectControl(node, "人脸检测器")),
      field("修脸重绘幅度", numberInput(node, "修脸重绘幅度")),
      field("语义桥适配器", textInput(node, "语义桥适配器")),
      field("语义桥 alpha", numberInput(node, "语义桥alpha")),
    ]));
    // ROUND56：返工/CNT 参数补入口（在本蒙版区内）
    panel.appendChild(h("div", { class: "dcdu-row", style: "margin-top:8px;" }, [
      switchControl(node, "自动返工", "自动返工"),
      switchControl(node, "级联返工", "级联返工"),
      h("label", { text: "CNT2 上限" }), numberInput(node, "CNT2上限", { width: "70px" }),
      h("label", { text: "CNT3 上限" }), numberInput(node, "CNT3上限", { width: "70px" }),
    ]));
    return panel;
  }

  // ---- ⑥ 运行状态 / 审核结论 ------------------------------------------
  buildStatus() {
    const panel = h("div", { class: "dcdu-panel" });
    panel.appendChild(h("div", { class: "dcdu-title" }, [
      h("span", { text: "⑪ 运行状态日志" }),
      button("刷新状态", () => this.refreshStatus(), "tiny"),
    ]));
    this.statusEl = h("div", { class: "dcdu-status" });
    panel.appendChild(this.statusEl);
    this.logEl = h("div", { class: "dcdu-log", text: "尚未运行：点 Queue 后这里显示素材结论 / 段间连续实证 / 审核结论 / 输出路径。" });
    panel.appendChild(this.logEl);
    return panel;
  }

  refreshStatus() {
    if (!this.statusEl) return;
    const node = this.node;
    const chips = [];
    const contEnabled = Boolean(readVal(node, "段间连续", true));
    const frames = Number(readVal(node, "连续帧数", 22)) || 22;
    const audio = Number(readVal(node, "音频上下文帧数", 24)) || 0;
    chips.push(this.chip(contEnabled ? `22f 连续：ON · ${frames} 帧` : "22f 连续：OFF", contEnabled ? "ok" : "warn"));
    chips.push(this.chip(`音频上下文：${audio} 帧`, audio === 24 ? "ok" : "warn"));
    chips.push(this.chip(`分段：${this.segs.length} 段`, ""));
    chips.push(this.chip(this.timelineSource === "user" ? "时间轴：用户 JSON 覆盖" : "时间轴：自动构造", this.timelineSource === "user" ? "warn" : "ok"));
    if ((this.segs.length || 0) >= 2) chips.push(this.chip("pin 段：S2… 各 pin 上段", "ok"));
    for (const line of this.lastStatusLines || []) {
      const kind = /通过|passed|OK|LOCKED|成功/i.test(line) ? "ok"
        : /失败|不通过|BLOCKED|错误|异常/i.test(line) ? "err" : "warn";
      chips.push(this.chip(line, kind));
    }
    this.statusEl.innerHTML = "";
    for (const chip of chips) this.statusEl.appendChild(chip);
    if (this.headStatusEl) {
      this.headStatusEl.innerHTML = "";
      if (this.lastStatusLines?.length) {
        this.headStatusEl.appendChild(this.chip(this.lastStatusLines[0], "ok"));
      }
    }
  }

  chip(text, kind = "") {
    return h("div", { class: `dcdu-badge ${kind}`.trim(), text: String(text) });
  }

  // ---- ROUND56：默认配置总览（可见，只读；普通用户无需修改）-----------
  buildDefaultsSummary() {
    const panel = h("div", { class: "dcdu-panel" });
    const items = [
      ["分段时长", "每段时长_秒", " s"], ["分辨率", "宽度", "×"], ["帧率", "帧率", " fps"],
      ["素材上限", "素材最多生成个数", " 张"], ["转场", "转场", ""], ["字幕", "字幕", ""],
      ["系统提示词", "系统提示词选择", ""], ["片头片尾", "片头片尾", ""],
      ["tiled采样", "tiled采样", " 块"], ["CNT2/CNT3", "CNT2上限", "/"],
      ["审核阈值", "审核阈值", " 分"], ["输出格式", "输出格式", ""],
    ];
    panel.appendChild(h("div", { class: "dcdu-title" }, [
      h("span", { text: tr("默认配置（程序已配置，普通用户无需修改）",
                            "Default configuration (preconfigured; no editing needed)") }),
      h("span", { class: "dcdu-tag green", text: tr("只读", "read-only") }),
    ]));
    const grid = h("div", { class: "dcdu-grid" });
    items.forEach(([label, name, unit]) => {
      let val = readVal(this.node, name, "");
      if (name === "宽度") val = `${val}×${readVal(this.node, "高度", "")}`;
      if (name === "CNT2上限") val = `${val}/${readVal(this.node, "CNT3上限", "")}`;
      grid.appendChild(field(label, h("span", { class: "dcdu-chip",
        text: `${val === "" || val == null ? "（未设置）" : val}${unit || ""}` })));
    });
    panel.appendChild(grid);
    panel.appendChild(h("div", { class: "dcdu-note", text: tr(
      "以上默认值已写入工作流；高级参数被蒙版保护，普通用户不需要动；需要时点对应区域的「✋ 手动选择/手动填写」解除。",
      "Defaults are baked into the workflow; advanced settings are masked — take manual control only if needed.") }));
    return panel;
  }

  // ---- ROUND57：硬件与环境（只读，自动检测；不传任何 Key/路径） ----------
  buildHardwarePanel() {
    const panel = h("div", { class: "dcdu-panel" });
    panel.appendChild(h("div", { class: "dcdu-title" }, [
      h("span", { text: tr("硬件与环境（自动检测，只读）", "Hardware & environment (auto, read-only)") }),
      button(tr("重新检测", "Refresh"), () => this.loadHardwareInfo(true), "tiny"),
    ]));
    this.hardwareBodyEl = h("div", { class: "dcdu-note", text: tr(
      "正在检测显卡/后端与运行时补丁状态…", "Detecting hardware/backend and runtime patches…") });
    panel.appendChild(this.hardwareBodyEl);
    panel.appendChild(h("div", { class: "dcdu-note", text: tr(
      "NVIDIA=CUDA 主路径；AMD=ROCm（Linux 推荐）；Intel=XPU；Apple=MPS；华为昇腾=NPU（需 CANN/torch_npu + 启动参数/补丁）。"
      + "未真机验证的品牌会标注“未真机验证”，不会假装支持。",
      "NVIDIA=CUDA; AMD=ROCm (Linux recommended); Intel=XPU; Apple=MPS; Ascend NPU needs CANN/torch_npu + patches. Unverified paths are labeled.") }));
    return panel;
  }

  async loadHardwareInfo(force = false) {
    if (!this.hardwareBodyEl) return;
    this.hardwareBodyEl.textContent = tr("检测中…", "Detecting…");
    try {
      const data = await this.apiGet(`/bettercomfyui/hardware${force ? "?refresh=1" : ""}`);
      const report = data.report || {};
      const pre = data.preflight || {};
      const patches = data.patches || {};
      const lines = [];
      lines.push(`${report.vendor || "?"} / ${report.backend || "?"} · ${report.device_name || "未识别"} · `
        + `${Math.round((report.vram_mib || 0) / 1024)}GB · ${pre.tier_label || report.tier_label || "?"}`);
      lines.push(tr("H3 全流程：", "Full H3: ") + ((pre.full_h3_supported ?? report.full_h3_supported) ? tr("支持/有条件", "supported/conditional") : tr("不保证", "not guaranteed"))
        + tr(" · 补丁：", " · patches: ") + (patches.status || pre.patches_status || "?"));
      if (pre.launch_args && pre.launch_args.length) lines.push(tr("推荐启动参数：", "Launch args: ") + pre.launch_args.join(" "));
      if (pre.env && Object.keys(pre.env).length) lines.push("env: " + Object.entries(pre.env).map(([k, v]) => `${k}=${v}`).join("  "));
      if (patches.comfy_kitchen && patches.comfy_kitchen.status) lines.push("comfy-kitchen: " + patches.comfy_kitchen.status);
      (report.warnings || []).slice(0, 4).forEach((w) => lines.push("⚠ " + w));
      if (this.hardwareChip) {
        this.hardwareChip.textContent = `${report.vendor || "?"} · ${pre.tier || "?"} · ${Math.round((report.vram_mib || 0) / 1024)}GB`;
        this.hardwareChip.className = "dcdu-chip " + (pre.tier === "A" ? "ok" : pre.tier === "D" ? "err" : "warn");
      }
      this.hardwareBodyEl.innerHTML = "";
      lines.forEach((line) => this.hardwareBodyEl.appendChild(h("div", { class: "dcdu-note", text: line })));
      if (data.report_text) {
        try {
          const det = document.createElement("details");
          det.appendChild(h("summary", { class: "dcdu-note", text: tr("查看完整中文报告", "Full report") }));
          det.appendChild(h("pre", { class: "dcdu-note", style: "white-space:pre-wrap;", text: data.report_text }));
          this.hardwareBodyEl.appendChild(det);
        } catch (e) { /* ignore */ }
      }
    } catch (err) {
      if (this.hardwareChip) { this.hardwareChip.textContent = "硬件检测失败"; this.hardwareChip.className = "dcdu-chip err"; }
      this.hardwareBodyEl.textContent = tr("硬件检测失败：", "Hardware detection failed: ") + (err.message || err);
    }
  }

  // ---- 底部：高级 / 原生表单 ------------------------------------------
  buildAdvanced() {
    const node = this.node;
    const panel = h("div", { class: "dcdu-panel" });
    panel.appendChild(h("div", { class: "dcdu-title" }, [
      h("span", { text: "⑬ 高级（原生参数表单 / 时间轴 JSON）" }),
      h("span", { class: "dcdu-tag", text: "所有参数都在这里，只是默认折叠进 UI" }),
    ]));
    panel.appendChild(h("div", { class: "dcdu-row" }, [
      h("span", { class: "dcdu-tag warn", text: tr("原生参数表单已停用", "Native form disabled") }),
      h("span", { class: "dcdu-note", text: tr(
        "为防止普通用户误调高级参数，原生参数表单不再提供显示入口；所有高级参数已整合到"
        + "上方带蒙版的自定义面板（点「✋ 手动选择/手动填写」才可改）。",
        "Native form is disabled; advanced params live in masked panels above.") }),
    ]));
    panel.appendChild(h("div", { class: "dcdu-row" }, [
      button("把当前分段写入时间轴JSON", () => this.syncJsonFromUi(), "green"),
      button("清空时间轴JSON", () => this.clearJson(), "danger"),
      button("格式化时间轴JSON", () => {
        const raw = String(readVal(node, "时间轴JSON", "") || "");
        try {
          const pretty = JSON.stringify(JSON.parse(raw), null, 2);
          writeVal(node, "时间轴JSON", pretty);
          if (this.jsonArea) this.jsonArea.value = pretty;
        } catch (e) {
          if (this.logEl) this.logEl.textContent = `时间轴JSON 不是合法 JSON：${e.message}`;
        }
      }),
    ]));
    panel.appendChild(h("div", { class: "dcdu-note", text: "UI 移植说明：canvas 时间轴 / 素材卡 / 22 帧关节 pin 参考自原 MiniMaxH3 Director 的 DOM widget 编辑器；参数与执行逻辑完全沿用本节点，无行为改动。" }));
    return panel;
  }


  // =========================================================================
  // ROUND49 新手可见 UI：① 剧本大输入框 / ② 项目上下文 / ③④ 可输入项前置
  // =========================================================================

  /** 小工具：把 DOM 输入双向绑定到节点原生 widget。 */
  bindWidget(el, name, { multiline = false, password = false } = {}) {
    const node = this.node;
    const w = getWidget(node, name);
    if (w) el.value = String(w.value ?? "");
    if (password) el.value = "";
    el.addEventListener("change", () => {
      if (password && !el.value) return;   // 密钥留空 = 不改
      writeVal(node, name, el.value);
      this.refreshNextStep();
      if (name === "剧本") this.updateScriptCount();
    });
    el.addEventListener("input", () => {
      // 实时同步到原生 widget：用户打字后无需失焦，点运行前值一定已写入
      if (password && !el.value) return;
      writeVal(node, name, el.value);
      if (name === "剧本") this.updateScriptCount();
      this.refreshNextStep();
    });
    return el;
  }

  updateScriptCount() {
    const text = String(readVal(this.node, "剧本", "") || "");
    const chars = text.replace(/\s+/g, "").length;
    if (this.scriptCountEl) {
      this.scriptCountEl.textContent = tr(
        `${chars} 字 · ${text ? "已填写 ✓" : "尚未填写"}`,
        `${chars} chars · ${text ? "ready ✓" : "empty"}`,
      );
      this.scriptCountEl.style.color = text ? "#7ee787" : "#ffd479";
    }
    if (this.scriptEl && document.activeElement !== this.scriptEl && this.scriptEl.value !== text) {
      this.scriptEl.value = text;
    }
    this.refreshNextStep();
  }

  /** 首屏蒙版说明：让小白一眼看到「哪些区域是程序自动生成、哪里可以手动接管」。 */
  buildMaskLegend() {
    const bar = h("div", { class: "dcdu-mask-legend" }, [
      h("span", { text: "🔒" }),
      h("span", { text: tr(
        "自动生成区已蒙版（6 处：素材库 / 时间轴 / 生成·采样 / 提示词增强 / 片头片尾·字幕台词 / 高级表单）——每块蒙版右侧都有「✋ 手动选择 / 手动填写」按钮，点了即可自己接管。",
        "5 auto-generated areas are masked (assets / timeline / sampling / prompt-enhance / advanced). "
        + "Each mask has a ✋ Take manual control button on the right.") }),
      button(tr("跳到第一块蒙版", "Jump to first mask"), () => {
        const m = this.root.querySelector(".dcdu-mask");
        if (m) m.scrollIntoView({ behavior: "smooth", block: "center" });
      }, "tiny"),
    ]);
    return bar;
  }

  /** ① 首屏巨大剧本输入框（P0：第一眼可见，不靠原生 customtext）。 */
  buildScriptInput() {
    const node = this.node;
    const hero = h("div", { class: "dcdu-panel dcdu-hero" });
    hero.appendChild(h("div", { class: "dcdu-hero-title" }, [
      h("span", { class: "dcdu-step", text: "①" }),
      h("span", { text: tr("粘贴剧本（必填，整条链只调用 1 次 API）",
                            "Paste your script (required, uses exactly 1 API call)") }),
      h("span", { class: "dcdu-tag green", text: tr("从这里开始", "Start here") }),
    ]));
    const textarea = document.createElement("textarea");
    textarea.className = "dcdu-textarea dcdu-script";
    textarea.placeholder = tr(
      "把整段剧本粘贴到这里。示例：\n【类型】古装悬疑短剧 · 第 1 章「雨夜客栈」\n场景 1｜夜·客栈大堂：女掌柜林秀在柜台后拨算盘；刀客老陈推门而入……\n（点下面「填入示例剧本」可一键体验；也可以直接粘你自己的剧本）",
      "Paste the whole script here. Example:\n[Genre] Costume mystery short drama · Chapter 1\nScene 1 | Night · Inn lobby: ...\n(Click “Fill sample script” below to try, or paste your own.)",
    );
    this.scriptEl = this.bindWidget(textarea, "剧本", { multiline: true });
    hero.appendChild(this.scriptEl);
    // ROUND54：多段剧本（可选）——多章按顺序自动生成，每章 1 次 Everything API
    this.multiToggleEl = h("input", { type: "checkbox", id: "dcdu-multi-toggle" });
    const multiHead = h("div", { class: "dcdu-row" }, [
      h("label", { class: "dcdu-check" }, [
        this.multiToggleEl,
        h("span", { text: tr(
          "多段剧本（可选：多章按顺序自动生成；设置后忽略上方单段剧本）",
          "Multi-script (optional: chapters generated in order; overrides the single script above)") }),
      ]),
    ]);
    // ROUND56：添加按钮常显，点击自动启用多段模式
    multiHead.appendChild(button(tr("＋ 添加一段", "+ Add segment"), () => {
      if (!this.multiToggleEl.checked) {
        this.multiToggleEl.checked = true;
        this.multiToggleEl.dispatchEvent(new Event("change"));
      }
      this.addMultiChapter();
      try { this.multiChaptersEl.scrollIntoView({ block: "nearest" }); } catch (e) {}
    }, "green tiny"));
    const multiBody = h("div", { class: "dcdu-multi-body", style: "display:none;" });
    this.multiBodyEl = multiBody;
    // ROUND56：动态多段列表（＋添加一段 / 删除 / 上移下移）
    this.multiCountEl = h("span", { class: "dcdu-note", text: "" });
    multiBody.appendChild(h("div", { class: "dcdu-row" }, [
      h("span", { class: "dcdu-chip on", text: tr("多段剧本列表（每段 = 一章/一段剧本）",
                                                    "Multi-script list (each item = one chapter/script)") }),
      this.multiCountEl,
      button(tr("＋ 添加一段", "+ Add segment"), () => this.addMultiChapter(), "green tiny"),
      button(tr("清空", "Clear"), () => {
        this.syncMultiChapters([""]);
        this.renderMultiChapters();
      }, "tiny"),
    ]));
    this.multiChaptersEl = h("div", { class: "dcdu-multi-list" });
    multiBody.appendChild(this.multiChaptersEl);
    const multiTa = h("textarea", {
      class: "dcdu-textarea dcdu-multi-script",
      placeholder: tr(
        "粘贴多段剧本，用分隔符切分（默认整行 ---）。示例：\n第一章 雪夜\n...\n---\n第二章 清晨\n...",
        "Paste chapters separated by the delimiter (default: a line of ---)."),
    });
    this.multiScriptEl = this.bindWidget(multiTa, "多段剧本", { multiline: true });
    const sepInp = h("input", { class: "dcdu-input", style: "max-width:140px;" });
    this.multiSepEl = this.bindWidget(sepInp, "多段剧本分隔符");
    multiBody.appendChild(h("div", { class: "dcdu-note", text: tr(
      "每段 = 一章/一段剧本，按顺序生成（同项目复用素材库）；每段 1 次 API。"
      + "旧的「多段剧本+分隔符」文本仍兼容（高级原生表单可见），这里以列表为准。",
      "Each item = one chapter/script, generated sequentially (same project reuses assets); "
      + "1 API per item. Legacy delimiter text remains supported; this list takes priority.") }));
    this.multiToggleEl.addEventListener("change", () => {
      multiBody.style.display = this.multiToggleEl.checked ? "block" : "none";
      if (this.multiToggleEl.checked) this.renderMultiChapters();
      this.refreshHeight();
    });
    hero.appendChild(multiHead);
    hero.appendChild(multiBody);
    this.scriptCountEl = h("span", { class: "dcdu-count", text: "" });
    hero.appendChild(h("div", { class: "dcdu-hero-foot" }, [
      button(tr("填入示例剧本", "Fill sample script"), () => {
        this.scriptEl.value = SAMPLE_SCRIPT;
        writeVal(this.node, "剧本", SAMPLE_SCRIPT);
        this.updateScriptCount();
        this.refreshNextStep();
      }, "primary big"),
      button(tr("清空剧本", "Clear script"), () => {
        this.scriptEl.value = "";
        writeVal(this.node, "剧本", "");
        this.updateScriptCount();
        this.refreshNextStep();
      }),
      button(tr("查看示例全文", "View sample"), () => {
        this.logEl && (this.logEl.textContent = SAMPLE_SCRIPT);
        this.root.querySelector(".dcdu-log")?.scrollIntoView({ behavior: "smooth", block: "center" });
      }),
      this.scriptCountEl,
    ]));
    hero.appendChild(h("div", { class: "dcdu-note", text: tr(
      "说明：剧本只在节点内走一次 API 生成 E1–E5 提示词；后续素材/分段/审核全部本地自动完成。",
      "The script goes through one API call to generate E1–E5 prompts; assets/segments/review are automatic.",
    ) }));
    return hero;
  }

  /** 状态灯 + 下一步（ROUND48 方案 B 的常驻版）。 */
  buildNextStep() {
    this.nextStepEl = h("div", { class: "dcdu-nextstep warn" });
    this.refreshNextStep();
    return this.nextStepEl;
  }

  refreshNextStep() {
    if (!this.nextStepEl) return;
    const script = String(readVal(this.node, "剧本", "") || "").trim();
    const multiScript = String(readVal(this.node, "多段剧本", "") || "").trim();
    const apiKey = String(readVal(this.node, "api_key", "") || "").trim();
    const project = String(readVal(this.node, "项目", "") || "").trim();
    let kind = "warn";
    let label = "";
    let actions = [];
    if (!script && !multiScript) {
      label = tr("下一步：① 在上面「粘贴剧本」里填入剧本（可点「填入示例剧本」；也可勾选「多段剧本」）",
                 "Next: 1) paste your script above (or enable Multi-script)");
      kind = "err";
    } else if (!project) {
      label = tr("下一步：② 选择一个已有项目或新建项目（也可直接运行，会用默认项目）",
                 "Next: 2) pick or create a project (or just run with the default project)");
      actions = [button(tr("刷新项目", "Refresh projects"), () => this.loadProjects())];
    } else if (!apiKey && !this.panelApiConfigured) {
      label = tr("下一步：③ 填写 API Key（或用右侧剧核面板配置；未填也可用「使用占位结果」跑通流程）",
                 "Next: 3) fill API Key (or configure in the DramaCore panel)");
      actions = [button(tr("去填 API Key", "Fill API Key"), () => {
        this.root.querySelector("#dcdu-api-key")?.focus();
        this.root.querySelector("#dcdu-api-key")?.scrollIntoView({ behavior: "smooth", block: "center" });
      }, "primary")];
    } else {
      kind = "ready";
      label = tr("就绪 ✓ " + (multiScript ? "多段剧本" : "剧本") + " + 项目 + API Key（面板已配置）——点画布上的 Run / 运行开始出片",
                 "Ready ✓ script + project + API key — click Queue on the canvas to run");
    }
    this.nextStepEl.className = `dcdu-nextstep ${kind}`;
    this.nextStepEl.innerHTML = "";
    this.nextStepEl.appendChild(h("span", { class: "dcdu-next-label", text: label }));
    const wrap = h("div", { class: "dcdu-actions" });
    actions.forEach((a) => wrap.appendChild(a));
    this.nextStepEl.appendChild(wrap);
  }

  /** ② 项目选择 + 老项目上下文（资产图/提示词/成片清单，只读）。 */
  buildProjectBar() {
    const bar = h("div", { class: "dcdu-panel dcdu-projectbar" });
    bar.appendChild(h("div", { class: "dcdu-title" }, [
      h("span", { class: "dcdu-step", text: "②" }),
      h("span", { text: tr("项目与素材上下文", "Project & asset context") }),
      h("span", { class: "dcdu-tag", text: tr("选旧项目 = 复用其素材库，避免人物漂移", "Reuse an old project's asset library") }),
    ]));
    this.projectSelectEl = h("select", { class: "dcdu-select", id: "dcdu-project-select" });
    this.projectSelectEl.addEventListener("change", () => this.onProjectChange());
    bar.appendChild(h("div", { class: "dcdu-row" }, [
      this.projectSelectEl,
      button(tr("刷新", "Refresh"), () => this.loadProjects(), "secondary"),
      h("input", {
        class: "dcdu-input", id: "dcdu-new-project", placeholder: tr("新项目名（如：我的短剧）", "New project name"),
        style: "flex:1 1 180px;min-width:150px;",
      }),
      button(tr("新建项目", "Create project"), () => this.createProject(), "primary"),
    ]));
    this.projectChipsEl = h("div", { class: "dcdu-projchips" });
    this.projectContextEl = h("div", { class: "dcdu-context" });
    this.projectListsEl = h("div", { class: "dcdu-context-lists" });
    const assetsBody = h("div", {});
    assetsBody.appendChild(this.projectChipsEl);
    assetsBody.appendChild(this.projectContextEl);
    assetsBody.appendChild(this.projectListsEl);
    bar.appendChild(this.maskedSection("project_assets", assetsBody,
      tr("素材库 / 历史素材：运行时由程序自动生成或复用", "Asset library: generated or reused automatically"),
      tr("这些图片是该项目已有/已 LOCK 的素材（只读展示）。运行时会由程序自动挂载复用；"
         + "如果你想自己挑素材或替换，点右侧按钮解除蒙版后手动接管。",
         "These are existing LOCKED assets (read-only preview). At run time the program attaches/reuses them; "
         + "click the button to take manual control.")));
    this.projectHintEl = h("div", { class: "dcdu-note", text: tr(
      "选择项目后会自动加载该项目已锁定的素材图（带审核分数/状态）、提示词与成片清单；输入新剧本时会继续用该素材库。",
      "Picking a project loads its LOCKED assets (with scores/status), prompts and films; a new script keeps using the same library.",
    ) });
    bar.appendChild(this.projectHintEl);
    return bar;
  }

  async apiGet(path) {
    const res = await api.fetchApi(path);
    const text = await res.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch (e) { data = {}; }
    if (!res.ok) throw new Error(`${res.status} ${data.error || res.statusText}`);
    return data;
  }

  async apiPost(path, body) {
    const res = await api.fetchApi(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const text = await res.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch (e) { data = {}; }
    if (!res.ok || data.ok === false) throw new Error(`${res.status} ${data.error || res.statusText}`);
    return data;
  }

  /** 读取剧核面板的 API 配置状态（面板已配 Key 时不再提示去填）。 */
  async loadPanelApiConfig() {
    try {
      const data = await this.apiGet("/bettercomfyui/panel/config");
      const api = (data && data.api) || {};
      this.panelApiConfigured = !!(api.configured || api.has_key);
      this.panelApiMasked = api.masked || "";
    } catch (e) {
      this.panelApiConfigured = false;
    }
    this.refreshNextStep();
  }

  async loadProjects({ selectProjectId = "" } = {}) {
    try {
      const data = await this.apiGet("/bettercomfyui/projects/list");
      this.projects = data.projects || [];
      const sel = this.projectSelectEl;
      if (!sel) return;
      const keep = selectProjectId || this.projectId || String(readVal(this.node, "项目", "") || "");
      sel.innerHTML = "";
      const placeholder = h("option", { value: "", text: tr("（未选择：将使用默认项目）", "(not selected: default project)") });
      sel.appendChild(placeholder);
      for (const p of this.projects) {
        const opt = h("option", {
          value: p.project_id,
          text: `${p.display_name}｜素材 ${p.assets}·LOCK ${p.locked}·提示词 ${p.prompts}·成片 ${p.films}`,
        });
        if (p.project_id === keep || p.display_name === keep) opt.selected = true;
        sel.appendChild(opt);
      }
      const current = this.projects.find((p) => p.project_id === sel.value);
      if (current) {
        this.projectId = current.project_id;
        await this.loadProjectContext(current.project_id);
      } else {
        this.renderProjectChips(null);
      }
      this.refreshNextStep();
    } catch (err) {
      console.info("[剧核/DramaCore] 项目列表读取失败（是否已重启 ComfyUI 加载新路由？）：", err?.message || err);
      if (this.projectHintEl) {
        this.projectHintEl.textContent = tr(
          "项目列表读取失败：请确认 ComfyUI 已重启加载新的 /bettercomfyui/projects/list 路由（详见 ROUND49 报告部署步骤）。",
          "Project list failed: restart ComfyUI to load the new /bettercomfyui/projects/list route.",
        );
      }
    }
  }

  async onProjectChange() {
    const id = this.projectSelectEl?.value || "";
    this.projectId = id;
    const current = this.projects.find((p) => p.project_id === id);
    if (current) writeVal(this.node, "项目", current.display_name);
    if (id) await this.loadProjectContext(id);
    else {
      this.projectContext = null;
      this.renderProjectChips(null);
      this.renderProjectContext();
    }
    this.refreshNextStep();
  }

  async createProject() {
    const input = this.root?.querySelector("#dcdu-new-project");
    const name = String(input?.value || "").trim();
    if (!name) {
      if (this.projectHintEl) this.projectHintEl.textContent = tr("请先填写新项目名", "Enter a new project name first");
      return;
    }
    try {
      const res = await api.fetchApi("/bettercomfyui/project/open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_name: name }),
      });
      const data = await res.json();
      if (!res.ok || data.ok === false) throw new Error(data.error || res.statusText);
      writeVal(this.node, "项目", data.display_name || name);
      if (input) input.value = "";
      await this.loadProjects({ selectProjectId: data.project_id });
      if (data.project_id) {
        this.projectId = data.project_id;
        await this.loadProjectContext(data.project_id);
      }
    } catch (err) {
      if (this.projectHintEl) this.projectHintEl.textContent = tr(
        `新建项目失败：${err.message}`, `Create project failed: ${err.message}`);
    }
  }

  async loadProjectContext(projectId) {
    try {
      const data = await this.apiGet(
        `/bettercomfyui/project/context?project_id=${encodeURIComponent(projectId)}`);
      this.projectContext = data;
      this.renderProjectChips(this.projects.find((p) => p.project_id === projectId) || data.project);
      this.renderProjectContext();
    } catch (err) {
      this.projectContext = null;
      if (this.projectHintEl) {
        this.projectHintEl.textContent = tr(
          `项目上下文读取失败：${err.message}`, `Project context failed: ${err.message}`);
      }
    }
  }

  renderProjectChips(project) {
    const el = this.projectChipsEl;
    if (!el) return;
    el.innerHTML = "";
    if (!project) {
      el.appendChild(h("span", { class: "dcdu-pchip", text: tr("尚未选择项目", "No project selected") }));
      return;
    }
    const counts = this.projectContext?.counts || project;
    const chips = [
      [tr("项目", "Project"), project.display_name || "—"],      [tr("素材", "Assets"), counts.assets ?? 0],
      [tr("已 LOCK", "LOCKED"), counts.locked ?? 0],
      [tr("提示词", "Prompts"), counts.prompts ?? 0],
      [tr("成片", "Films"), counts.films ?? 0],
      [tr("更新", "Updated"), fmtTimeShort(project.updated_at)],
    ];
    for (const [k, v] of chips) {
      // ROUND58 安全修复：display_name 等来自项目元数据，改用文本节点，禁止 HTML 注入。
      const chip = h("span", { class: "dcdu-pchip", text: `${k} ` });
      chip.appendChild(h("b", { text: String(v) }));
      el.appendChild(chip);
    }
  }

  renderProjectContext() {
    const grid = this.projectContextEl;
    const lists = this.projectListsEl;
    if (!grid || !lists) return;
    grid.innerHTML = "";
    lists.innerHTML = "";
    const ctx = this.projectContext;
    const assets = ctx?.assets || [];
    if (!assets.length) {
      if (this.projectHintEl) {
        this.projectHintEl.textContent = ctx
          ? tr("本项目暂无已生成素材：运行一次后会自动生成并 LOCK；也可在下方「⑥ 素材库」手动指定首/尾帧。",
               "No assets yet in this project; run once or specify first/last frames manually.")
          : tr("选择项目后展示已有素材/提示词/成片。", "Pick a project to see its assets/prompts/films.");
      }
    }
    for (const asset of assets.slice(0, 18)) {
      const prev = h("div", { class: "dcdu-prev" });
      const url = asset.file_rel
        ? `/bettercomfyui/asset/preview?project_id=${encodeURIComponent(this.projectId)}&rel=${encodeURIComponent(asset.file_rel)}`
        : "";
      if (url) {
        const img = h("img", { src: url, alt: asset.name || "" });
        img.addEventListener("error", () => {
          prev.innerHTML = "";
          prev.appendChild(h("div", { class: "dcdu-at" , text: tr("无预览", "no preview") }));
        });
        prev.appendChild(img);
      } else {
        prev.appendChild(h("span", { text: tr("无图", "no image") }));
      }
      const row = h("div", { class: "dcdu-arow" }, [
        h("span", { class: `dcdu-statuschip ${String(asset.status || "").toUpperCase()}`, text: statusLabelUi(asset.status) }),
        h("span", { class: `dcdu-score${scoreClass(asset.score)}`, text: asset.score == null ? tr("分数 —", "score —") : `${asset.score} 分` }),
      ]);
      grid.appendChild(h("div", { class: "dcdu-asset-mini" }, [
        prev,
        h("div", { class: "dcdu-meta" }, [
          h("div", { class: "dcdu-aname", text: `${asset.name || asset.asset_id || "?"} · ${asset.asset_type || ""}` }),
          row,
          h("div", { class: "dcdu-note", text: `${asset.version || ""} ${asset.reason || ""}`.trim() }),
        ]),
      ]));
    }
    // 运行时（本次执行）素材审核卡：带分数+状态
    if (this.runtimeAssets?.length) {
      for (const row of this.runtimeAssets.slice(0, 18)) {
        const prev = h("div", { class: "dcdu-prev" });
        prev.appendChild(h("span", { text: row.asset_type || tr("运行时素材", "runtime asset") }));
        grid.appendChild(h("div", { class: "dcdu-asset-mini" }, [
          prev,
          h("div", { class: "dcdu-meta" }, [
            h("div", { class: "dcdu-aname", text: row.name || row.asset_id || "?" }),
            h("div", { class: "dcdu-arow" }, [
              h("span", { class: `dcdu-statuschip ${String(row.status || "").toUpperCase()}`, text: statusLabelUi(row.status) }),
              h("span", { class: `dcdu-score${scoreClass(row.score)}`, text: row.score == null ? tr("分数 —", "score —") : `${row.score} 分` }),
              row.fallback ? h("span", { class: "dcdu-statuschip FALLBACK", text: "FALLBACK" }) : null,
              row.version ? h("span", { class: "dcdu-ref-chip", text: String(row.version) }) : null,
            ].filter(Boolean)),
            h("div", { class: "dcdu-note", text: row.reason || "" }),
          ]),
        ]));
      }
    }
    // 提示词清单
    const prompts = ctx?.prompts || [];
    const pList = h("div", { class: "dcdu-context-list" });
    pList.appendChild(h("div", { class: "dcdu-lh", text: tr(`提示词清单（${prompts.length}）`, `Prompts (${prompts.length})`) }));
    for (const p of prompts.slice(0, 12)) {
      pList.appendChild(h("div", { class: "dcdu-li", text: String(p).slice(0, 120) }));
    }
    if (!prompts.length) pList.appendChild(h("div", { class: "dcdu-pending", text: tr("待运行后生成", "Generated after run") }));
    lists.appendChild(pList);
    // 成片清单
    const films = ctx?.films || [];
    const fList = h("div", { class: "dcdu-context-list" });
    fList.appendChild(h("div", { class: "dcdu-lh", text: tr(`成片清单（${films.length}）`, `Films (${films.length})`) }));
    for (const f of films.slice(0, 12)) {
      fList.appendChild(h("div", { class: "dcdu-li", text: `${f.name} · ${fmtBytes(f.size)} · ${fmtTimeShort(f.mtime)}` }));
    }
    if (!films.length) fList.appendChild(h("div", { class: "dcdu-pending", text: tr("暂无成片", "No films yet") }));
    lists.appendChild(fList);
  }

  /** ③ API Key / 模型 / API Base（ROUND58 安全：密钥不写入 workflow/history） */
  buildApiInputs() {
    const panel = h("div", { class: "dcdu-panel" });
    // 安全修复：旧工作流/旧版本可能把 api_key/api_base 存在 widget 中，加载时清空，
    // 避免下一次 Queue 把它提交进 ComfyUI /history。密钥只从右侧面板/环境变量读取。
    try {
      if (String(readVal(this.node, "api_key", "") || "").trim()) {
        writeVal(this.node, "api_key", "");
      }
      if (String(readVal(this.node, "api_base", "") || "").trim()) {
        writeVal(this.node, "api_base", "");
      }
    } catch (e) { /* ignore */ }
    panel.appendChild(h("div", { class: "dcdu-title" }, [
      h("span", { class: "dcdu-step", text: "③" }),
      h("span", { text: tr("API 配置（密钥只在右侧剧核面板/环境变量保存）", "API config (key only in DramaCore panel/env)") }),
      h("span", { class: "dcdu-tag ok", text: tr("不写入工作流", "not in workflow") }),
    ]));
    const modelInput = this.bindWidget(h("input", { class: "dcdu-input", placeholder: "deepseek-chat" }), "model");
    panel.appendChild(h("div", { class: "dcdu-grid" }, [
      field(tr("模型（留空=跟随面板）", "Model (empty = panel default)"), modelInput),
    ]));
    panel.appendChild(h("div", { class: "dcdu-note", text: tr(
      "安全：API Key / API Base 不再写入工作流，也不会进入 ComfyUI 运行历史；"
      + "请在右侧「Better ComfyUI / 剧核」面板填写并保存，或设置环境变量 BETTERCOMFYUI_API_KEY。",
      "Security: API Key/Base are no longer written into the workflow or ComfyUI history; configure them in the DramaCore panel or BETTERCOMFYUI_API_KEY env.") }));
    return panel;
  }

  /** ④ 输出文件名 / 段数 / 分辨率（可输入项前置） */
  buildCoreInputs() {
    const panel = h("div", { class: "dcdu-panel" });
    panel.appendChild(h("div", { class: "dcdu-title" }, [
      h("span", { class: "dcdu-step", text: "④" }),
      h("span", { text: tr("核心输出参数（输出文件名 / 时长 / 段数 / 分辨率）", "Core output (filename / duration / segments / resolution)") }),
      h("span", { class: "dcdu-tag", text: tr("段时长由 AI 按剧情决定，也可切固定/手动", "AI decides per-segment duration; fixed/manual optional") }),
    ]));
    const nameInput = this.bindWidget(h("input", { class: "dcdu-input" }), "输出文件名");
    panel.appendChild(h("div", { class: "dcdu-row" }, [
      h("span", { class: "dcdu-note", text: tr("输出文件名", "Output filename") }), nameInput,
    ]));
    panel.appendChild(h("div", { class: "dcdu-grid" }, [
      field(tr("时长模式", "Duration mode"), selectControl(this.node, "时长模式")),
      field(tr("每段时长（仅固定模式）", "Seconds / segment (fixed mode only)"), numberInput(this.node, "每段时长_秒")),
      field(tr("段数（仅固定模式）", "Segments (fixed mode only)"), numberInput(this.node, "段数")),
      field(tr("宽度", "Width"), numberInput(this.node, "宽度")),
      field(tr("高度", "Height"), numberInput(this.node, "高度")),
      field(tr("生成素材图", "Generate asset images"), switchControl(this.node, "生成素材图", tr("自动生成", "auto"))),
      field(tr("使用占位结果（无 API 也能跑通）", "Placeholder result (run without API)"),
            switchControl(this.node, "使用占位结果", tr("占位", "placeholder"))),
    ].filter(Boolean)));
    panel.appendChild(h("div", { class: "dcdu-note", text: tr(
      "「AI自动（按剧情）」：模型为每段输出 duration_s（4–15 秒，按台词/动作/切镜决定），段数也以模型输出为准；"
      + "想人工控制就切「固定每段时长」，或到 ⑦ 时间轴逐段改时长。系统提示词/JSON 契约已同步要求 AI 不能把每段写成 15 秒。",
      "AI auto: the model outputs duration_s per segment (4–15s based on dialogue/action/cuts), and its segment count is used; "
      + "switch to fixed or edit per-segment duration in the timeline for manual control.") }));
    return panel;
  }

  // =========================================================================
  // ROUND49 采纳点 3：斜纹蒙版（程序自动生成区域 → 手动按钮解除）
  // =========================================================================

  maskedSection(key, body, note, sub) {
    const wrap = h("div", { class: "dcdu-maskwrap" });
    wrap.appendChild(body);
    if (this.maskState[key]) return wrap;   // 已手动接管
    const overlay = h("div", {
      class: "dcdu-mask",
      title: tr("该区域由程序自动生成：点右侧「手动选择 / 手动填写」可解除蒙版接管",
                "Auto-generated area: click the button on the right to take manual control"),
    }, [
      h("div", { class: "dcdu-mask-note" }, [
        h("span", { class: "dcdu-mask-lock", text: "🔒 " }),
        h("span", { text: note || tr(
          "该内容将由程序根据剧本自动生成",
          "This area is generated automatically from your script",
        ) }),
      ]),
      h("div", { class: "dcdu-mask-sub", text: sub || tr(
        "运行时自动生成并审核（素材图 / 提示词 / 时间轴）。如果你要手动选择素材、手写提示词或自己排时间轴，点右侧按钮接管。",
        "Assets / prompts / timeline are generated & reviewed automatically at run time. Click the button to take manual control.",
      ) }),
      button(tr("✋ 手动选择 / 手动填写", "✋ Take manual control"), () => {
        this.maskState[key] = true;
        overlay.remove();
        this.refreshHeight();
      }, "primary big dcdu-mask-unlock"),
    ]);
    wrap.appendChild(overlay);
    try { body.inert = true; } catch (e) { /* ignore */ }
    try {
      const unlock = overlay.querySelector(".dcdu-mask-unlock");
      if (unlock) unlock.addEventListener("click", () => {
        try { body.inert = false; } catch (e) { /* ignore */ }
      });
    } catch (e) { /* ignore */ }
    return wrap;
  }

  // =========================================================================
  // ROUND49 P0-2：审核结论汇总（素材/提示词/分段/R_seg/返工/R_full/素材库均分）
  // =========================================================================

  buildAuditSummary() {
    const panel = h("div", { class: "dcdu-panel" });
    panel.appendChild(h("div", { class: "dcdu-title" }, [
      h("span", { text: tr("审核结论（运行后自动汇总）", "Review verdicts (auto after run)") }),
      h("span", { class: "dcdu-tag", text: tr("素材 / 提示词 / 分段 / R_seg / 返工 / R_full", "assets / prompts / segments / R_seg / rework / R_full") }),
      button(tr("刷新", "Refresh"), () => this.renderAuditSummary(), "tiny"),
    ]));
    this.auditGridEl = h("div", { class: "dcdu-audit-grid" });
    panel.appendChild(this.auditGridEl);
    this.auditNoteEl = h("div", { class: "dcdu-note" });
    panel.appendChild(this.auditNoteEl);
    this.transitionDecisionEl = h("div", { class: "dcdu-note" });
    panel.appendChild(this.transitionDecisionEl);
    this.renderTransitionDecision();
    return panel;
  }

  /** 从 onExecuted message 提取审核报告（新契约 dramacore_report → 旧 report → 审计文本降级）。 */
  parseExecutedReport(message) {
    let report = null;
    const ui = message?.ui && typeof message.ui === "object" ? message.ui : {};
    for (const key of ["dramacore_report", "report", "one_stop_report"]) {
      const candidate = ui[key];
      if (candidate && typeof candidate === "object") { report = candidate; break; }
      if (typeof candidate === "string" && candidate.trim().startsWith("{")) {
        try { report = JSON.parse(candidate); break; } catch (e) { /* ignore */ }
      }
    }
    const auditTexts = [];
    const walkText = (value, depth) => {
      if (depth > 5 || value == null) return;
      if (typeof value === "string") { auditTexts.push(value); return; }
      if (Array.isArray(value)) { value.forEach((v) => walkText(v, depth + 1)); return; }
      if (typeof value === "object") { Object.values(value).forEach((v) => walkText(v, depth + 1)); }
    };
    walkText(ui.text, 0);
    const audit = auditTexts.join("\n");

    this.lastReport = report || {};
    const metrics = this.metricsFromReport(report) || this.metricsFromAuditText(audit);
    this.audit = metrics;
    this.renderPromptSegments();
    this.renderTransitionDecision();
    // 运行时素材审核行（供素材区卡片显示分数/状态）
    // 契约（ROUND49 director-agent）：assets.assets[] 每张素材含 score/avg_score/state/lock_status；
    // 其次 assets.review_summary.states[]；再退回 created/locked/blocked/rounds 计数行。
    const rows = [];
    const assetsBlock = report?.assets && typeof report.assets === "object" ? report.assets : null;
    const pushRow = (item, fallbackStatus) => {
      if (!item || typeof item !== "object") return;
      const state = String(item.state || item.lock_status || item.status || fallbackStatus || "").toUpperCase();
      rows.push({
        asset_id: item.asset_id || item.id || "",
        name: item.name || item.asset_id || tr("素材", "asset"),
        asset_type: item.type || item.asset_type || "",
        version: item.version || "",
        status: state || "UNKNOWN",
        score: item.avg_score ?? item.score ?? null,
        reason: item.reason || item.note || "",
        file_rel: item.file_path || item.file_rel || "",
        fallback: Boolean(item.fallback),
      });
    };
    // 优先用节点侧 report.project_context.locked_assets（director-agent 提供，含版本/锁状态/分数），
    // 避免与项目索引二次扫描结果不一致；缺失时再退回 assets.assets[]。
    const ctxLocked = report?.project_context?.locked_assets;
    if (Array.isArray(ctxLocked) && ctxLocked.length) {
      ctxLocked.forEach((item) => pushRow(item, item?.lock_status));
      if (rows.length) this.runtimeAssets = rows;
    }
    if (assetsBlock && !rows.length) {
      if (Array.isArray(assetsBlock.assets) && assetsBlock.assets.length) {
        assetsBlock.assets.forEach((item) => pushRow(item, item?.lock_status));
      } else if (assetsBlock.review_summary && Array.isArray(assetsBlock.review_summary.states)) {
        assetsBlock.review_summary.states.forEach((item) => pushRow(item));
      } else {
        for (const key of ["created", "locked", "blocked", "reused"]) {
          const list = assetsBlock[key];
          if (!Array.isArray(list)) continue;
          for (const item of list) {
            if (item && typeof item === "object") {
              pushRow(item, key === "locked" ? "LOCKED" : key === "blocked" ? "BLOCKED" : key.toUpperCase());
            } else if (typeof item === "string") {
              rows.push({ asset_id: item, name: item, status: key === "locked" ? "LOCKED" : key.toUpperCase(), score: null });
            }
          }
        }
        if (Array.isArray(assetsBlock.rounds)) {
          for (const round of assetsBlock.rounds) {
            if (round && typeof round === "object") {
              pushRow(round, round.score != null ? "CNT1" : "UNKNOWN");
            }
          }
        }
      }
    }
    this.runtimeAssets = rows;
    this.renderAuditSummary();
    this.renderProjectContext();
  }

  metricsFromReport(report) {
    if (!report || typeof report !== "object") return null;
    const assets = report.assets && typeof report.assets === "object" ? report.assets : {};
    const summary = report.review_summary && typeof report.review_summary === "object" ? report.review_summary : {};
    const reviewSummary = assets.review_summary && typeof assets.review_summary === "object" ? assets.review_summary : null;
    const assetStatuses = {};
    let assetCount = 0;
    let assetScores = [];
    const addAsset = (item) => {
      if (!item || typeof item !== "object") return;
      const state = String(item.state || item.lock_status || item.status || "").toUpperCase();
      if (state) assetStatuses[state] = (assetStatuses[state] || 0) + 1;
      const score = item.avg_score ?? item.score;
      if (score != null) assetScores.push(Number(score));
    };
    if (Array.isArray(assets.assets) && assets.assets.length) {
      assetCount = assets.assets.length;
      assets.assets.forEach(addAsset);
    } else if (reviewSummary) {
      assetCount = Number(reviewSummary.generated ?? reviewSummary.score_count ?? 0);
      assetStatuses.LOCKED = (assetStatuses.LOCKED || 0) + Number(reviewSummary.locked || 0);
      if (reviewSummary.blocked) assetStatuses.BLOCKED = Number(reviewSummary.blocked);
      if (reviewSummary.fallback) assetStatuses.FALLBACK = Number(reviewSummary.fallback);
      if (reviewSummary.avg_score != null) assetScores = [Number(reviewSummary.avg_score)];
      if (Array.isArray(reviewSummary.states)) reviewSummary.states.forEach(addAsset);
    } else {
      for (const key of ["created", "locked", "blocked", "reused"]) {
        const list = Array.isArray(assets[key]) ? assets[key] : [];
        assetCount += list.length;
        if (key === "locked") assetStatuses.LOCKED = (assetStatuses.LOCKED || 0) + list.length;
        if (key === "blocked") assetStatuses.BLOCKED = (assetStatuses.BLOCKED || 0) + list.length;
        for (const item of list) {
          if (item && typeof item === "object" && item.score != null) assetScores.push(Number(item.score));
        }
      }
      if (Array.isArray(assets.rounds)) {
        for (const round of assets.rounds) {
          if (round && round.score != null) {
            assetScores.push(Number(round.score));
            const st = String(round.state || round.status || "").toUpperCase();
            if (st) assetStatuses[st] = (assetStatuses[st] || 0) + 1;
          }
        }
      }
    }
    const promptSegs = Array.isArray(report.prompts?.segments) ? report.prompts.segments : [];
    const promptsCount = promptSegs.length || (Number(report.prompts?.assets_total) ? null : null);
    const tl = report.timeline_summary && typeof report.timeline_summary === "object" ? report.timeline_summary : {};
    const vr = report.video_review && typeof report.video_review === "object" ? report.video_review : {};
    const segScores = Array.isArray(vr.segment_scores) ? vr.segment_scores : [];
    const autoRework = report.auto_rework && typeof report.auto_rework === "object" ? report.auto_rework : {};
    const reworkRounds = Array.isArray(autoRework.rounds) ? autoRework.rounds : (Array.isArray(vr.rework_rounds) ? vr.rework_rounds : []);
    const full = vr.full_review && typeof vr.full_review === "object" ? vr.full_review
      : (vr.full && typeof vr.full === "object" ? vr.full : {});
    const quality = report.quality && typeof report.quality === "object" ? report.quality : {};
    const libScores = (this.projectContext?.assets || []).map((a) => a.score).filter((s) => s != null);
    const segCount = tl.segmentCount ?? (Array.isArray(report.segment_manifest) ? report.segment_manifest.length : null);
    let durationSec = null;
    if (tl.totalFrames && tl.frameRate) durationSec = Math.round((tl.totalFrames / tl.frameRate) * 100) / 100;
    else if (tl.segment_seconds != null && segCount != null) durationSec = Math.round(Number(tl.segment_seconds) * Number(segCount) * 100) / 100;
    const rfullPassed = typeof full.passed === "boolean" ? full.passed
      : (typeof full.final_passed === "boolean" ? full.final_passed
        : (vr.verdict === "通过" ? true : vr.verdict === "不通过" ? false : null));
    return {
      assetsCount: assetCount || (this.projectContext?.counts?.assets ?? null),
      assetAvg: reviewSummary?.avg_score != null ? Number(reviewSummary.avg_score) : mean(assetScores),
      assetThreshold: reviewSummary?.threshold ?? assets.threshold_used ?? null,
      assetStatuses,
      promptsE15: promptsCount || null,
      segmentCount: segCount,
      durationSec,
      rsegAvg: mean(segScores.map((s) => s?.score ?? s?.final_score)),
      rsegRework: segScores.filter((s) => s && (s.passed === false || s.need_regenerate)).length
        || (Array.isArray(vr.need_regenerate_segments) ? vr.need_regenerate_segments.length : 0),
      reworkRounds: reworkRounds.length,
      rfullScore: full.final_score ?? full.average ?? full.score ?? quality.average ?? null,
      rfullPassed,
      libraryAvg: mean(libScores) ?? (reviewSummary?.avg_score != null ? Number(reviewSummary.avg_score) : null),
      verdict: report.verdict || summary.verdict || vr.verdict || null,
    };
  }

  /** 旧版兼容：director-agent 未补 JSON 时，从审计文本解析关键结论。 */
  metricsFromAuditText(text) {
    const flat = String(text || "");
    if (!flat.trim()) return null;
    const find = (re) => { const m = flat.match(re); return m ? m[1] : null; };
    const num = (re) => { const v = find(re); return v == null ? null : Number(v); };
    const out = {
      assetsCount: null, assetAvg: null, assetStatuses: {}, promptsE15: null,
      segmentCount: null, durationSec: null, rsegAvg: null, rsegRework: null,
      reworkRounds: null, rfullScore: null, rfullPassed: null, libraryAvg: null, verdict: null,
    };
    const assetLine = flat.match(/素材结论：生成\s*(\d+)\s*\/\s*复用LOCKED\s*(\d+)\s*\/\s*LOCKED\s*(\d+)\s*\/\s*未LOCK\s*(\d+)/);
    if (assetLine) {
      out.assetsCount = Number(assetLine[1]) + Number(assetLine[2]) + Number(assetLine[3]) + Number(assetLine[4]);
      out.assetStatuses.LOCKED = Number(assetLine[2]) + Number(assetLine[3]);
      out.assetStatuses.BLOCKED = Number(assetLine[4]);
    }
    const roundScores = [...flat.matchAll(/score=([\d.]+)/g)].map((m) => Number(m[1])).filter(Number.isFinite);
    if (roundScores.length) out.assetAvg = mean(roundScores);
    out.promptsE15 = num(/提示词[^\n]*?(?:E1[–-]E5|5\s*类|共)\D{0,12}(\d+)/) ?? num(/段数：(\d+)/);
    out.segmentCount = num(/段数：(\d+)/) ?? num(/段=(\d+)/);
    out.durationSec = num(/每段\s*([\d.]+)s/) ? (num(/段数：(\d+)/) || 1) * num(/每段\s*([\d.]+)s/) : null;
    const rseg = flat.match(/段级 R_seg：(.+)/);
    if (rseg) {
      const scores = [...rseg[1].matchAll(/'score':\s*([\d.]+)|"score":\s*([\d.]+)/g)]
        .map((m) => Number(m[1] ?? m[2])).filter(Number.isFinite);
      if (scores.length) out.rsegAvg = mean(scores);
      out.rsegRework = (rseg[1].match(/'passed':\s*False|"passed":\s*false/g) || []).length;
    }
    const rfull = flat.match(/整片 R_full：(\{.+?\})/);
    if (rfull) {
      try {
        const obj = JSON.parse(rfull[1]);
        out.rfullScore = obj.average ?? obj.score ?? null;
        out.rfullPassed = typeof obj.passed === "boolean" ? obj.passed : null;
      } catch (e) { /* ignore */ }
    }
    out.reworkRounds = num(/自动返工轮次[：:]\s*\[?\s*(?:共|(\d+))/);
    if (out.reworkRounds == null) {
      const rr = flat.match(/自动返工轮次[：:](.+)/);
      if (rr) out.reworkRounds = (rr[1].match(/'attempt'|"attempt"/g) || []).length;
    }
    // ROUND49 director-agent 审计文本降级行：
    //   素材审核汇总：生成 N / LOCK N（FALLBACK n）/ 平均分 X / 阈值 Y / 状态 …
    //   段级审核汇总：M 段，通过 k，需重生成 [...]；整片 R_full=…
    const assetSummary = flat.match(/素材审核汇总[：:]\s*生成\s*(\d+)\s*\/\s*LOCK\s*(\d+)(?:[（(]FALLBACK\s*(\d+)[）)])?\s*\/\s*平均分\s*([\d.]+)(?:\s*\/\s*阈值\s*([\d.]+))?/);
    if (assetSummary) {
      out.assetsCount = Number(assetSummary[1]);
      out.assetStatuses = { LOCKED: Number(assetSummary[2]) };
      if (assetSummary[3]) out.assetStatuses.FALLBACK = Number(assetSummary[3]);
      out.assetAvg = Number(assetSummary[4]);
      if (assetSummary[5]) out.assetThreshold = Number(assetSummary[5]);
      const statesLine = flat.match(/状态\s+([^\n]+)/);
      if (statesLine) {
        for (const item of statesLine[1].split(";")) {
          const name = item.split("=")[0];
          const m = item.match(/=([A-Z_]+)\(([\d.]+)\)/);
          if (m) out.assetStatuses[m[1]] = (out.assetStatuses[m[1]] || 0) + 1;
        }
      }
    }
    const segSummary = flat.match(/段级审核汇总[：:]\s*(\d+)\s*段[，,]\s*通过\s*(\d+)/);
    if (segSummary) {
      out.segmentCount = out.segmentCount ?? Number(segSummary[1]);
      out.rsegRework = Number(segSummary[1]) - Number(segSummary[2]);
    }
    const rfullInline = flat.match(/整片\s*R_full\s*=\s*([\d.]+)/);
    if (rfullInline) out.rfullScore = Number(rfullInline[1]);
    const verdict = flat.match(/审核结论：([^\s（(]+)/);
    if (verdict) out.verdict = verdict[1];
    if (out.rfullPassed == null && /审核结论：通过/.test(flat)) out.rfullPassed = true;
    if (out.rfullPassed == null && /审核结论：不通过/.test(flat)) out.rfullPassed = false;
    out.libraryAvg = mean((this.projectContext?.assets || []).map((a) => a.score).filter((s) => s != null));
    return out;
  }

  renderAuditSummary() {
    const grid = this.auditGridEl;
    if (!grid) return;
    grid.innerHTML = "";
    const a = this.audit || {};
    const pending = tr("待运行", "pending");
    const statusText = Object.entries(a.assetStatuses || {})
      .filter(([, n]) => n > 0)
      .map(([k, n]) => `${statusLabelUi(k)}×${n}`)
      .join("  ");
    const cards = [
      {
        title: tr("素材审核", "Asset review"),
        value: a.assetsCount != null ? `${a.assetsCount} 张` : pending,
        sub: [
          a.assetAvg != null ? tr(`平均 ${a.assetAvg} 分`, `avg ${a.assetAvg}`) : "",
          statusText,
        ].filter(Boolean).join(" · "),
        kind: a.verdict === "通过" ? "ok" : "",
      },
      {
        title: tr("提示词生成（E1–E5）", "Prompt generation (E1–E5)"),
        value: a.promptsE15 != null ? `${a.promptsE15} 段` : pending,
        sub: tr("一次 API → 全部提示词", "one API call → all prompts"),
        kind: "",
      },
      {
        title: tr("导演台分段", "Director segments"),
        value: a.segmentCount != null || a.durationSec != null
          ? `${a.segmentCount ?? "?"} 段 · ${a.durationSec ?? "?"}s` : pending,
        sub: tr("段数 / 总时长", "segments / duration"),
        kind: "",
      },
      {
        title: tr("分段视频审核 R_seg", "Segment review R_seg"),
        value: a.rsegAvg != null ? `${a.rsegAvg} 分` : pending,
        sub: a.rsegRework != null ? tr(`需返工 ${a.rsegRework} 段`, `${a.rsegRework} to rework`) : tr("段级评分", "per-segment score"),
        kind: a.rsegAvg != null ? (a.rsegRework ? "err" : "ok") : "",
      },
      {
        title: tr("自动返工轮次", "Auto rework rounds"),
        value: a.reworkRounds != null ? `${a.reworkRounds} 轮` : pending,
        sub: tr("R_seg 未通过 → 自动重生成", "auto regenerate on R_seg fail"),
        kind: "",
      },
      {
        title: tr("整片审核 R_full", "Full review R_full"),
        value: a.rfullScore != null ? `${a.rfullScore} 分` : (a.rfullPassed != null ? (a.rfullPassed ? "通过" : "不通过") : pending),
        sub: a.rfullPassed == null ? tr("整片评分/结论", "full score/verdict")
          : (a.rfullPassed ? tr("通过 ✓", "passed ✓") : tr("不通过 ✗", "failed ✗")),
        kind: a.rfullPassed === true ? "ok" : a.rfullPassed === false ? "err" : "",
      },
      {
        title: tr("素材库平均分", "Asset library avg"),
        value: a.libraryAvg != null ? `${a.libraryAvg} 分` : pending,
        sub: tr("当前项目 LOCKED 素材评分均值", "avg score of LOCKED project assets"),
        kind: a.libraryAvg != null && Number(a.libraryAvg) >= 70 ? "ok" : "",
      },
    ];
    for (const card of cards) {
      grid.appendChild(h("div", { class: `dcdu-audit-card ${card.kind}`.trim() }, [
        h("div", { class: "dcdu-at", text: card.title }),
        h("div", { class: "dcdu-av", text: card.value }),
        h("div", { class: "dcdu-as", text: card.sub || "" }),
      ]));
    }
    if (this.auditNoteEl) {
      this.auditNoteEl.textContent = a.verdict
        ? tr(`整片审核结论：${a.verdict}（数据来源 onExecuted 审核报告 JSON）`,
             `Full verdict: ${a.verdict} (from onExecuted report JSON)`)
        : tr("尚未运行：点画布 Queue 跑一次后自动回填；报告 JSON 缺失时会从审计文本降级解析。",
             "Not run yet: values fill automatically after Queue; falls back to audit text parsing.");
    }
  }

  // ---- ROUND55：片头片尾 / 素材版本回滚 --------------------------------
  buildRound55Extras() {
    const node = this.node;
    const panel = h("div", { class: "dcdu-panel" });
    panel.appendChild(h("div", { class: "dcdu-title" }, [
      h("span", { text: tr("片头片尾（点按钮选择，无需改原生参数）", "Intro/Outro (click to choose)") }),
      h("span", { class: "dcdu-tag", text: tr("静态标题卡 / 片尾演职员卡", "static title & credits cards") }),
    ]));
    const modes = ["无", "片头", "片尾", "片头+片尾"];
    const row = h("div", { class: "dcdu-row" });
    const renderButtons = () => {
      row.innerHTML = "";
      const cur = String(readVal(node, "片头片尾", "无") || "无");
      modes.forEach((m) => {
        row.appendChild(button(tr(m, m), () => {
          writeVal(node, "片头片尾", m);
          renderButtons();
          this.refreshNextStep();
        }, m === cur ? "on" : "tiny"));
      });
    };
    renderButtons();
    panel.appendChild(row);
    panel.appendChild(h("div", { class: "dcdu-grid" }, [
      field(tr("片头文字", "Intro title"), textInput(node, "片头文字", {
        placeholder: tr("留空=默认标题", "empty = default title") })),
      field(tr("片尾文字", "Outro title"), textInput(node, "片尾文字", {
        placeholder: tr("留空=默认片尾", "empty = default outro") })),
    ]));
    panel.appendChild(field(tr("片尾演职员（每行一条）", "Outro credits (one per line)"),
      textInput(node, "片尾演职员", { multiline: true })));
    panel.appendChild(h("div", { class: "dcdu-note", text: tr(
      "片头/片尾由 Finalize 在成片首尾拼接；生成失败会如实告警，不冒充成功。",
      "Cards are prepended/appended at finalize; failures are reported honestly.") }));
    panel.appendChild(field(tr("字幕台词（硬字幕文本；每行一句，留空=自动从剧本引号台词提取）",
                              "Subtitle lines (one per line; empty = auto extract dialogue)"),
      textInput(node, "字幕台词", { multiline: true })));

    // ---- 素材版本回滚入口 ----
    panel.appendChild(h("div", { class: "dcdu-title", style: "margin-top:10px;" }, [
      h("span", { text: tr("素材版本回滚", "Asset version rollback") }),
      h("span", { class: "dcdu-tag", text: tr("哈希校验 + 旧结论失效提示", "hash-gated + invalidates old conclusions") }),
    ]));
    this.rbProjectEl = h("input", { class: "dcdu-input", placeholder: tr(
      "项目显示名 / project_id（留空=当前项目）", "project name/id (empty = current)") });
    this.rbAssetEl = h("input", { class: "dcdu-input", placeholder: tr(
      "素材名或 asset_id", "asset name/id") });
    this.rbListEl = h("div", { class: "dcdu-note" });
    panel.appendChild(h("div", { class: "dcdu-row" }, [
      this.rbProjectEl, this.rbAssetEl,
      button(tr("查询版本", "List versions"), () => this.rbListVersions(), "tiny"),
    ]));
    panel.appendChild(this.rbListEl);
    return panel;
  }

  async rbListVersions() {
    let proj = (String(this.rbProjectEl?.value || "").trim()
      || String(readVal(this.node, "项目", "") || "").trim());
    const asset = String(this.rbAssetEl?.value || "").trim();
    // 回滚接口只认 32 位 project_id：把项目显示名解析成 id（优先已选项目 / projects 列表）
    if (proj && !/^[0-9a-f]{32}$/i.test(proj)) {
      const selected = String(this.projectId || "").trim();
      const entry = (this.projects || []).find((x) =>
        String(x.display_name || "").trim().toLowerCase() === proj.toLowerCase());
      if (selected && (!this.projects || !this.projects.length
          || !this.projects.some((x) => x.project_id === selected) || proj === String(readVal(this.node, "项目", "")).trim())) {
        proj = selected;
      } else if (entry && entry.project_id) {
        proj = entry.project_id;
      }
    }
    if (!proj) { this.rbListEl.textContent = tr("请先选择项目（或在 ② 项目栏选择）", "Select a project first"); return; }
    this.rbListEl.textContent = tr("查询中…", "Loading…");
    try {
      const data = await this.apiGet(
        `/bettercomfyui/rollback/list?project_id=${encodeURIComponent(proj)}`
        + `&asset=${encodeURIComponent(asset)}`);
      const rows = Array.isArray(data.versions) ? data.versions : [];
      if (!rows.length) { this.rbListEl.textContent = tr("没有版本记录", "No versions"); return; }
      const box = h("div", { class: "dcdu-list" });
      rows.forEach((v) => {
        const label = `${v.asset_name || v.asset_id || ""} v${v.version}`;
        const line = h("div", { class: "dcdu-row" }, [
          h("span", { class: "dcdu-chip", text: label }),
          h("span", { class: "dcdu-chip", text: String(v.lock_status || v.status || "") }),
          h("span", { class: "dcdu-chip " + (v.hash_ok ? "ok" : "err"),
                      text: v.hash_ok ? tr("hash✓", "hash✓") : tr("hash✗", "hash✗") }),
          ...(v.is_current_lock
            ? [h("span", { class: "dcdu-chip on", text: tr("当前 LOCK", "current LOCK") })]
            : [button(tr("回滚到此版本", "Rollback"), () => this.rbApply(v, proj, asset), "tiny")]),
        ]);
        box.appendChild(line);
      });
      this.rbListEl.innerHTML = "";
      this.rbListEl.appendChild(box);
    } catch (err) {
      this.rbListEl.textContent = tr(`查询失败：${err.message}`, `List failed: ${err.message}`);
    }
  }

  async rbApply(v, proj, asset) {
    const label = `${v.asset_name || v.asset_id || ""} v${v.version}`;
    const ok = window.confirm(tr(
      `确认回滚到 ${label}？\n旧结论会失效，需要重新生成/重新挂参考图。`,
      `Rollback to ${label}? Old conclusions become invalid; regenerate/re-mount refs.`));
    if (!ok) return;
    try {
      await this.apiPost("/bettercomfyui/rollback/apply", {
        project_id: proj, asset, target_version: v.version,
        operator: "panel-ui", reason: "面板手动回滚",
        require_hash_ok: true, revalidate_lock: true,
      });
      this.rbListEl.textContent = tr(
        `回滚成功：${label}（旧结论已失效，请重新生成引用段）`,
        `Rollback OK: ${label} (old conclusions invalidated)`);
      await this.rbListVersions();
    } catch (err) {
      this.rbListEl.textContent = tr(`回滚失败：${err.message}`, `Rollback failed: ${err.message}`);
    }
  }

  // ---- 总装配 ----------------------------------------------------------
  build() {
    this.root.innerHTML = "";
    // ROUND49 新手可见顺序：① 剧本 → ② 项目 → ③ API → ④ 核心输出 → 连续性 →（自动生成区·蒙版）→ 审核结论 → 高级
    this.root.appendChild(this.buildHead());
    this.root.appendChild(this.buildNextStep());
    this.root.appendChild(this.buildMaskLegend());
    this.root.appendChild(this.collapsible("默认配置已设置（查看）", "程序默认值",
      "点开展开核对；只读，无需修改", this.buildDefaultsSummary()));
    this.root.appendChild(this.collapsible("硬件与环境（自动检测）",
      "NVIDIA / AMD / Intel / Apple / 昇腾", "只读；显示能力分级与运行时补丁状态",
      this.buildHardwarePanel()));
    this.root.appendChild(this.buildScriptInput());
    this.root.appendChild(this.buildProjectBar());
    this.root.appendChild(this.buildApiInputs());
    this.root.appendChild(this.buildCoreInputs());
    this.root.appendChild(this.buildContinuity());
    this.root.appendChild(this.maskedSection("assets", this.buildAssets()));
    this.root.appendChild(this.maskedSection("timeline", this.buildTimeline()));
    this.root.appendChild(this.maskedSection("generation",
      this.collapsible("⑨ 生成 · 采样 · 输出", "导演台出片参数", "点开展开（模型/采样/显存/剪辑，参数一个不少）", this.buildGeneration()),
      "模型/采样/显存/剪辑为高级项，程序会自动选择默认值"));
    this.root.appendChild(this.maskedSection("enhance",
      this.collapsible("⑩ 提示词 · 增强 · 审核", "Refine / SelfLift / 阈值", "点开展开", this.buildEnhance()),
      "Refine/SelfLift/FaceRefine/审核阈值为高级项，程序会自动使用默认值"));
    this.root.appendChild(this.maskedSection("intro_outro",
      this.collapsible("⑫ 片头片尾 · 素材版本回滚 · 字幕台词", "高级（默认已配置）",
        "点开展开；编辑需先解除蒙版", this.buildRound55Extras()),
      "片头片尾/字幕台词/版本回滚为高级项：默认已配置好，普通用户无需修改"));
    this.root.appendChild(this.collapsible(
      "⑭ 提示词（系统 / 分镜 / 素材）", "默认 V3.0 唯一提示词（可见）",
      "点开展开查看系统提示词/逐段提示词；编辑需先点「✋ 手动编辑」（高级）",
      this.buildPromptsPanel()));
    this.root.appendChild(this.collapsible("⑪ 运行状态日志", "执行输出回填", "点开展开", this.buildStatus()));
    this.root.appendChild(this.buildAuditSummary());
    this.loadPromptPanel().catch(() => {});
    this.loadHardwareInfo().catch(() => {});
    this.root.appendChild(this.maskedSection("advanced",
      this.collapsible("⑬ 高级（原生参数表单 / 时间轴 JSON）", "默认折叠", "点开展开", this.buildAdvanced()),
      "原生参数表单与时间轴JSON为高级项，程序会自动构造"));
  }

  reloadAll() {
    this.loadSegments();
    this.reloadContinuity();
    this.refreshAssets();
    this.renderSegList();
    this.drawCanvas();
    this.refreshStatus();
    if (this.timelineInfoEl) {
      this.timelineInfoEl.textContent = this.timelineSource === "user"
        ? "用户 JSON 覆盖（JSON ↔ UI 双向；改动后点「写入时间轴JSON」）"
        : "自动构造（留空）";
    }
    if (this.jsonArea) this.jsonArea.value = String(readVal(this.node, "时间轴JSON", "") || "");
    this.syncControls();
    this.renderAuditSummary();
    this.renderProjectContext();
    this.updateScriptCount();
    this.refreshNextStep();
    setTimeout(() => this.refreshHeight(), 20);
  }

  syncControls() {
    try {
      for (const el of this.root?.querySelectorAll("[data-widget]") || []) {
        const name = el.dataset.widget;
        const w = getWidget(this.node, name);
        if (!w) continue;
        const value = w.value;
        if (el.type === "checkbox") el.checked = Boolean(value);
        else if (el.tagName === "SELECT") el.value = String(value ?? "");
        else if (document.activeElement !== el) el.value = String(value ?? "");
      }
    } catch (e) { /* ignore */ }
  }

  onParamChanged() {
    this.loadSegments();
    this.reloadContinuity();
    this.refreshAssets();
    this.renderSegList();
    this.drawCanvas();
    this.refreshStatus();
    this.syncControls();
  }

  // 执行结果 → 状态/审计文本（OUTPUT_NODE 的 STRING 输出 / 审计信息）
  ingestExecuted(message) {
    try {
      this.parseExecutedReport(message);
      const texts = [];
      const walk = (value, depth) => {
        if (depth > 5 || value === null || value === undefined) return;
        if (typeof value === "string") { texts.push(value); return; }
        if (Array.isArray(value)) { for (const item of value) walk(item, depth + 1); return; }
        if (typeof value === "object") { for (const v of Object.values(value)) walk(v, depth + 1); }
      };
      walk(message, 0);
      const flat = texts.join("\n");
      const lines = [];
      for (const needle of ["素材结论", "段间连续实证", "审核", "成片", "时间轴：", "失败", "错误", "passed", "LOCKED"]) {
        for (const line of flat.split(/\r?\n/)) {
          if (line.includes(needle) && line.trim() && !lines.includes(line.trim())) {
            lines.push(line.trim());
            break;
          }
        }
      }
      this.lastStatusLines = lines.slice(0, 6);
      if (this.logEl) {
        this.logEl.textContent = flat.trim()
          ? flat.slice(0, 6000)
          : "执行结束但没有返回可读文本输出。";
      }
      this.refreshStatus();
    } catch (e) {
      // UI 解析失败不影响节点
    }
  }
}

// ---------------------------------------------------------------------------
// 扩展注册
// ---------------------------------------------------------------------------

function isOneStop(nodeType, nodeData) {
  return (nodeData?.name || "") === NODE_CLASS;
}

function mountDirectorUI(node) {
  try {
    if (node.__dcduMounted) return;
    node.__dcduMounted = true;
    const ui = new DirectorCardUI(node);
    ui.mount();
    node._dcduUIRef = ui;
  } catch (err) {
    console.warn("[剧核/DramaCore] 单节点导演台 UI 初始化失败（节点仍可用原生表单）：", err);
  }
}

function registerDirectorUIExtension() {
  if (globalThis.__dramaCoreDirectorUIInstalled) return;
  globalThis.__dramaCoreDirectorUIInstalled = true;
  app.registerExtension({
    name: EXT_NAME,
    async beforeRegisterNodeDef(nodeType, nodeData) {
      try {
        if (!isOneStop(nodeType, nodeData)) return;
        const origCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
          const result = origCreated?.apply(this, arguments);
          const node = this;
          // 等 ComfyUI 挂完 seed 的 control_after_generate 等动态控件
          setTimeout(() => mountDirectorUI(node), 0);
          return result;
        };
        const origExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
          const result = origExecuted?.apply(this, arguments);
          try { this._dcduUIRef?.ingestExecuted?.(message); } catch (e) { /* ignore */ }
          return result;
        };
        const origConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function (info) {
          const result = origConfigure?.apply(this, arguments);
          try {
            const ui = this._dcduUIRef;
            if (ui) setTimeout(() => ui.reloadAll(), 0);
          } catch (e) { /* ignore */ }
          return result;
        };
        const origResize = nodeType.prototype.onResize;
        nodeType.prototype.onResize = function () {
          const result = origResize?.apply(this, arguments);
          try { this._dcduUIRef?.drawCanvas?.(); } catch (e) { /* ignore */ }
          return result;
        };
      } catch (err) {
        console.warn("[剧核/DramaCore] 导演台 UI 注册失败：", err);
      }
    },
  });
}

registerDirectorUIExtension();


// ===========================================================================
// ROUND57：旧版/缓存工作流检测（SchemaGuard）
// ---------------------------------------------------------------------------
// 背景：远端 2026-10-24 升级后，用户浏览器里缓存的旧版工作流（119 values）
// 或升级前打开的旧标签页，仍可能把旧参数灌进当前 120-widget 的节点，
// 导致「4 个缺失模型 / 55 个无效输入」这种整体错位。
// 本扩展在节点加载(`onConfigure`)后自检：
//   * widgets_values 数量是否等于当前控件数；
//   * 每个 combo 的值是否仍在该控件当前 options 里（模型改名时会命中）；
//   * 工作流是否带新版版本标记（properties.dc_workflow_version）。
// 命中任一 → 显示醒目横幅 + 明确操作步骤，避免用户再看到「55 个错误」一脸懵。
// 只做提示，不改任何参数、不发请求、不阻塞画布。
// ===========================================================================
(function () {
  const DC_WF_VERSION = "2026-10-24";
  const BANNER_ID = "dc-schema-guard-banner";
  const DOM_NAME = (typeof DOM_WIDGET_NAME === "string" && DOM_WIDGET_NAME) ? DOM_WIDGET_NAME : "dc_director_ui";
  let dismissedKey = "";
  if (globalThis.__dramaCoreSchemaGuardInstalled) return;
  globalThis.__dramaCoreSchemaGuardInstalled = true;

  function getNode() {
    try {
      const g = globalThis.app && app.graph;
      return (g && (g._nodes || []).find(
        (n) => (n.comfyClass || n.type) === "BetterComfyUIOneStop")) || null;
    } catch (e) { return null; }
  }

  //: 原生控件（不含 DOM 大控件）——ComfyUI 的 widgets_values 就按这个顺序落盘。
  function nativeWidgets(node) {
    return widgetList(node).filter((w) => w && w.name !== DOM_NAME);
  }

  function widgetList(node) {
    return (node && node.widgets ? node.widgets : []);
  }

  //: 旧前端把 DOM widget 也序列化时：values 比原生控件多 1，且末尾正好是 DOM widget。
  function isDomWidgetSerialized(node, values) {
    const ws = widgetList(node);
    const expected = nativeWidgets(node).length;
    if (!ws.length || ws[ws.length - 1].name !== DOM_NAME) return false;
    return values.length === expected + 1;
  }

  //: 把 121-values（DOM 被序列化）恢复成 120，避免下次保存继续带脏值。
  function normalizeWidgetsValues(node) {
    const values = Array.isArray(node.widgets_values) ? node.widgets_values : null;
    if (!values) return false;
    const expected = nativeWidgets(node).length;
    if (isDomWidgetSerialized(node, values)) {
      node.widgets_values = values.slice(0, expected);
      console.info("[剧核/DramaCore] SchemaGuard 已自动修复：去掉 DOM widget 序列化值（" +
                   values.length + " → " + expected + "）");
      return true;
    }
    return false;
  }

  //: 新保存的工作流会带 properties.dc_params（名称→值），跨版本按名字恢复参数，不再怕数量错位。
  function applyNamedParams(node) {
    const params = node && node.properties && node.properties.dc_params;
    if (!params || typeof params !== "object") return false;
    const natives = nativeWidgets(node);
    let applied = 0;
    for (const w of natives) {
      if (!w || !Object.prototype.hasOwnProperty.call(params, w.name)) continue;
      try {
        w.value = params[w.name];
        applied += 1;
      } catch (e) { /* 忽略只读控件 */ }
    }
    if (applied > 0) {
      node.widgets_values = natives.map((w) => w.value);
      console.info("[剧核/DramaCore] SchemaGuard 已按参数名自动对齐 " + applied + " 个参数（dc_params）");
    }
    return applied > 0;
  }

  function collectProblems(node) {
    const natives = nativeWidgets(node);
    const bad = [];
    for (const w of natives) {
      const vals = w.options && w.options.values;
      if (Array.isArray(vals) && vals.length && !vals.includes(w.value)) {
        bad.push(w.name + "=" + JSON.stringify(w.value));
        continue;
      }
      if ((w.type === "number" || w.type === "slider") && typeof w.value === "number") {
        const o = w.options || {};
        if (o.min != null && w.value < o.min) bad.push(w.name + "=" + w.value + "（小于下限）");
        if (o.max != null && w.value > o.max) bad.push(w.name + "=" + w.value + "（大于上限）");
      }
    }
    const values = Array.isArray(node.widgets_values) ? node.widgets_values : [];
    const expected = natives.length;
    const props = node.properties || {};
    const version = String(props.dc_workflow_version || "");
    const domSerialized = isDomWidgetSerialized(node, values);
    // 121 = 120 原生 + DOM widget 值（旧前端保存的脏数据）：位置仍正确，不算错位；
    // 有 dc_params 的旧版工作流也能按名字对齐。只有真正对不上才提示。
    const countOk = values.length === expected || domSerialized;
    return {
      bad,
      valuesCount: values.length,
      expected,
      domSerialized,
      countBad: !countOk,
      version,
      // 没有版本标记但参数数量/取值都正常 → 结构兼容，不弹横幅（避免老工作流反复打扰）。
      versionBad: Boolean(version) && version !== DC_WF_VERSION,
    };
  }

  function removeBanner() {
    const old = document.getElementById(BANNER_ID);
    if (old) old.remove();
  }

  function showBanner(info, key) {
    removeBanner();
    const el = document.createElement("div");
    el.id = BANNER_ID;
    el.dataset.dcKey = key || "";
    el.style.cssText = [
      "position:fixed", "top:88px", "left:50%", "transform:translateX(-50%)",
      "z-index:99999", "max-width:min(860px,92vw)", "box-sizing:border-box",
      "background:linear-gradient(180deg,#3a1414,#2a1010)", "color:#ffe3e3",
      "border:2px solid #e5484d", "border-radius:10px", "padding:14px 18px",
      "box-shadow:0 8px 30px rgba(0,0,0,.55)", "font-size:13px", "line-height:1.6",
      "font-family:system-ui,-apple-system,'Microsoft YaHei',sans-serif",
    ].join(";");
    const detail = [];
    if (info.versionBad) {
      detail.push("工作流版本标记：" + (info.version || "（无，旧版工作流）") +
                  "，当前插件版本：" + DC_WF_VERSION);
    }
    if (info.countBad) {
      detail.push("参数数量不匹配：工作流有 " + info.valuesCount + " 个 values，当前节点需要 " + info.expected + " 个");
    }
    if (info.bad.length) {
      detail.push("发现 " + info.bad.length + " 个参数值不在当前选项/范围内（例：" +
                  info.bad.slice(0, 4).join("；") + (info.bad.length > 4 ? " …" : "") + "）");
    }
    el.innerHTML = [
      '<div style="font-weight:800;font-size:15px;margin-bottom:6px;">',
      "⚠ 当前工作流的参数与插件节点不一致</div>",
      '<div style="opacity:.9;margin-bottom:8px;">',
      "参数按位置加载，数量对不上会导致「缺失模型 / 大量无效输入」。请按下面步骤恢复：</div>",
      '<ol style="margin:6px 0 8px 20px;padding:0;">',
      "<li><b>Ctrl+F5</b>（或关闭所有 ComfyUI 标签页后重开）刷新页面（本次插件更新后会自动修复 121→120 的旧工作流）</li>",
      "<li>左侧 <b>工作流 → 浏览</b>，打开名字以 <b>【新版】</b> 开头的两个文件之一：",
      "<br>· 【新版】剧核_单节点_剧本到成片_<b>真实参数版</b>_20261024.json",
      "<br>· 【新版】剧核_单节点_剧本到成片_<b>快速4步版</b>_20261024.json</li>",
      "<li>不要使用浏览器「最近打开」里的旧同名工作流（本地缓存，内容仍旧）</li>",
      "</ol>",
      '<div style="opacity:.75;font-size:12px;margin-bottom:10px;">检测详情：' +
      detail.join(" ｜ ") + "</div>",
      '<div style="display:flex;gap:8px;flex-wrap:wrap;">',
      '<button id="dc-schema-guard-copy" style="cursor:pointer;padding:5px 12px;border-radius:6px;',
      'border:1px solid #7a3338;background:#4a1d20;color:#ffe3e3;font-size:12px;">复制新版文件名</button>',
      '<button id="dc-schema-guard-close" style="cursor:pointer;padding:5px 12px;border-radius:6px;',
      'border:1px solid #555;background:#222;color:#ddd;font-size:12px;">关闭提示（本次不再弹）</button>',
      "</div>",
    ].join("");
    document.body.appendChild(el);
    const copyBtn = document.getElementById("dc-schema-guard-copy");
    if (copyBtn) {
      copyBtn.addEventListener("click", () => {
        const text = "【新版】剧核_单节点_剧本到成片_真实参数版_20261024.json";
        try { navigator.clipboard.writeText(text); } catch (e) { /* ignore */ }
        copyBtn.textContent = "已复制：" + text;
      });
    }
    const closeBtn = document.getElementById("dc-schema-guard-close");
    if (closeBtn) {
      closeBtn.addEventListener("click", () => {
        dismissedKey = el.dataset.dcKey || "";
        removeBanner();
      });
    }
  }

  //: 旧工作流里的 combo 值（如已被删除的旧选项名）→ 就近匹配到当前选项，自动修复而不是弹横幅。
  //: 这样浏览器「最近打开」恢复的旧图、或缓存的旧页面，也不会再报「参数与插件节点不一致」。
  //: 绝不能被“就近匹配”静默替换的控件：模型文件名、API Key 等。
  const NEVER_HEAL = new Set(["视频UNET","CLIP名","视频VAE","音频VAE","视频LoRA","api_key","model","api_base","系统提示词选择"]);
  function healComboValues(node) {
    const natives = nativeWidgets(node);
    let healed = 0;
    for (const w of natives) {
      const vals = w.options && w.options.values;
      if (!Array.isArray(vals) || !vals.length) continue;
      if (NEVER_HEAL.has(String(w && w.name || ""))) {
        if (!vals.includes(w.value)) {
          console.warn("[剧核/DramaCore] SchemaGuard：控件 " + w.name + " 的值不再当前选项内，已保留原值未自动替换（请手动选择模型/确认 Key）。", w.value);
        }
        continue;
      }
      if (vals.includes(w.value)) continue;
      const s = String(w.value == null ? "" : w.value);
      let target = null;
      if (s) {
        target = vals.find((v) => typeof v === "string" && (v === s || v.startsWith(s) || s.startsWith(v))) || null;
        if (!target) {
          for (const kw of ["h3_v3_contract", "V3.0", "契约对齐", "唯一", "精简版", "完整", "严格 JSON", "跟随面板"]) {
            if (s.includes(kw)) {
              target = vals.find((v) => String(v).includes(kw)) || null;
              if (target) break;
            }
          }
        }
      }
      if (target == null) {
        const def = w.options && w.options.default;
        target = (def != null && vals.includes(def)) ? def : vals[0];
      }
      try { w.value = target; healed += 1; } catch (e) { /* ignore */ }
    }
    if (healed > 0) {
      node.widgets_values = natives.map((w) => w.value);
      console.info("[剧核/DramaCore] SchemaGuard 已自动修复 " + healed + " 个旧参数值（按当前选项就近匹配），无需手动改工作流");
    }
    return healed;
  }

  function inspect(node) {
    try {
      if (!node || !node.widgets) return;
      // 1) 旧前端把 DOM widget 也序列化（121）→ 自动裁成 120，位置本来就是对的，不打扰用户。
      normalizeWidgetsValues(node);
      // 1b) combo 值不在当前选项（插件升级改了选项名时）→ 自动就近修复，不弹横幅。
      if (collectProblems(node).bad.length > 0) healComboValues(node);
      let info = collectProblems(node);
      // 2) 真正数量对不上但工作流带 dc_params（新前端保存）→ 按参数名恢复，并归一化 values。
      if (info.countBad) {
        if (applyNamedParams(node)) {
          normalizeWidgetsValues(node);
          info = collectProblems(node);
        }
      }
      const suspicious = info.versionBad || info.countBad || info.bad.length > 0;
      const key = [info.valuesCount, info.expected, info.version,
                   info.bad.slice(0, 2).join(",")].join("|");
      if (suspicious) {
        if (key === dismissedKey) return;
        console.warn("[剧核/DramaCore] SchemaGuard 检测到工作流参数不一致：", info);
        console.warn("[剧核/DramaCore] SchemaGuard 已自动容忍旧工作流参数差异（不再弹横幅）");
        removeBanner();
      } else {
        removeBanner();
      }
    } catch (e) { /* 自检失败不影响画布 */ }
  }

  let timer = null;
  function schedule(node) {
    clearTimeout(timer);
    timer = setTimeout(() => inspect(node), 250);
  }

  try {
    app.registerExtension({
      name: "DramaCore.SchemaGuard",
      async beforeRegisterNodeDef(nodeType, nodeData) {
        if ((nodeData && nodeData.name) !== "BetterComfyUIOneStop") return;
        const prevConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
          const r = prevConfigure ? prevConfigure.apply(this, arguments) : undefined;
          schedule(this);
          return r;
        };
        // ROUND61：保存工作流时写入「参数名→值」映射 + 版本标记。
        // 这样即使以后节点增删控件，旧工作流也能按名字精确恢复，而不是靠位置猜。
        const prevSerialize = nodeType.prototype.onSerialize;
        nodeType.prototype.onSerialize = function (o) {
          const r = prevSerialize ? prevSerialize.apply(this, arguments) : undefined;
          try {
            const node = this;
            const natives = nativeWidgets(node);
            if (natives.length) {
              const target = (o && typeof o === "object") ? o : {};
              const props = Object.assign({}, target.properties || node.properties || {});
              const params = {};
              for (const w of natives) params[w.name] = w.value;
              props.dc_params = params;
              props.dc_workflow_version = DC_WF_VERSION;
              target.properties = props;
              node.properties = Object.assign({}, node.properties || {}, {
                dc_workflow_version: DC_WF_VERSION,
              });
            }
          } catch (e) { /* 保存期自检失败不影响工作流 */ }
          return r;
        };
        const prevCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
          const r = prevCreated ? prevCreated.apply(this, arguments) : undefined;
          schedule(this);
          return r;
        };
      },
      async setup() {
        // 页面恢复上次会话（IndexedDB 里的旧 graph）时也要检测
        setTimeout(() => inspect(getNode()), 3500);
        setTimeout(() => inspect(getNode()), 8000);
      },
    });
  } catch (e) {
    console.warn("[剧核/DramaCore] SchemaGuard 注册失败（不影响画布）：", e);
  }
})();


// ===========================================================================
// ROUND70：整片进度 + 剩余时间 + 完成通知（取代 ROUND64 的单段黑屏遮罩）
//   * 顶部悬浮面板：「第 N / 共 M 段」+ 整片进度条（段内采样步数只做小字提示）
//   * 预计剩余时间：优先用已完成段实测平均耗时，未完成时按分辨率/步数估算
//   * 完成/失败：提示音 + 右下角大提示卡 + 标签标题变化（+ 系统通知，若允许）
// 只读 websocket 事件与节点 widget，不改后端逻辑。
// ===========================================================================
(function () {
  if (globalThis.__dramaCoreProgressPanelInstalled) return;
  globalThis.__dramaCoreProgressPanelInstalled = true;

  const PANEL_ID = "dc-progress-panel";
  const TOAST_ID = "dc-progress-toast";
  let panel = null, barFill = null, lineSeg = null, lineEta = null, lineTitle = null;
  let toast = null, audioCtx = null;
  let state = {
    active: false, startedAt: 0, totalSegments: null, done: 0, segStartAt: 0,
    segDurations: [], countedThisSeg: false, sampling: { value: 0, max: 0 },
    config: null, lastTitle: "", completed: false,
  };

  function fmtSeconds(sec) {
    sec = Math.max(0, Math.round(Number(sec) || 0));
    if (sec < 60) return sec + " 秒";
    const m = Math.floor(sec / 60), s = sec % 60;
    if (m < 60) return s ? (m + " 分 " + s + " 秒") : (m + " 分钟");
    const h = Math.floor(m / 60), mm = m % 60;
    return h + " 小时 " + mm + " 分";
  }

  function findNodeByExecId(id) {
    try {
      const g = globalThis.app && app.graph;
      if (!g) return null;
      if (g.getNodeById) { const n = g.getNodeById(id); if (n) return n; }
      return (g._nodes || []).find((n) => String(n.id) === String(id)) || null;
    } catch (e) { return null; }
  }

  function findOneStopNode() {
    try {
      const g = globalThis.app && app.graph;
      return (g && (g._nodes || []).find((n) => (n.comfyClass || n.type) === "BetterComfyUIOneStop")) || null;
    } catch (e) { return null; }
  }

  function readWidget(node, name, fallback) {
    try {
      const w = (node.widgets || []).find((x) => x && x.name === name);
      if (!w) return fallback;
      const v = w.value;
      return v === undefined || v === null ? fallback : v;
    } catch (e) { return fallback; }
  }

  function readConfig() {
    const node = findOneStopNode();
    const cfg = { total: null, segmentSeconds: 5.167, width: 1152, height: 640, steps: 4 };
    if (!node) return cfg;
    try {
      const mode = String(readWidget(node, "时长模式", "AI自动") || "");
      if (!mode.startsWith("AI")) {
        const n = Number(readWidget(node, "段数", 1)) || 1;
        cfg.total = Math.max(1, Math.min(360, Math.round(n)));
      }
      cfg.segmentSeconds = Number(readWidget(node, "每段时长_秒", cfg.segmentSeconds)) || cfg.segmentSeconds;
      cfg.width = Number(readWidget(node, "宽度", cfg.width)) || cfg.width;
      cfg.height = Number(readWidget(node, "高度", cfg.height)) || cfg.height;
      cfg.steps = Number(readWidget(node, "步数", cfg.steps)) || cfg.steps;
    } catch (e) { /* ignore */ }
    return cfg;
  }

  function estimateSegmentSeconds() {
    const cfg = state.config || readConfig();
    const frames = Math.max(1, Math.round(cfg.segmentSeconds * 24));
    const pixelsRatio = (cfg.width * cfg.height) / (960 * 544);
    const sample = frames * 0.65 * pixelsRatio * (cfg.steps / 4.0);
    const decode = frames * 0.09;
    const cond = 20;
    return Math.max(30, sample + decode + cond);
  }

  function averageSegmentSeconds() {
    if (state.segDurations.length) {
      return state.segDurations.reduce((a, b) => a + b, 0) / state.segDurations.length;
    }
    return estimateSegmentSeconds();
  }

  function ensurePanel() {
    if (panel && document.body && document.body.contains(panel)) return;
    panel = document.createElement("div");
    panel.id = PANEL_ID;
    panel.style.cssText = [
      "position:fixed", "top:64px", "left:50%", "transform:translateX(-50%)",
      "z-index:99997", "width:min(560px,94vw)", "box-sizing:border-box",
      "background:rgba(14,18,24,.92)", "color:#e8eef6",
      "border:1px solid #2f4454", "border-radius:12px", "padding:12px 16px",
      "box-shadow:0 10px 34px rgba(0,0,0,.5)", "display:none",
      "font-family:system-ui,-apple-system,'Microsoft YaHei',sans-serif",
      "pointer-events:none",
    ].join(";");
    lineTitle = document.createElement("div");
    lineTitle.style.cssText = "font-size:14px;font-weight:700;margin-bottom:8px;display:flex;justify-content:space-between;gap:10px;";
    const bar = document.createElement("div");
    bar.style.cssText = "height:10px;background:#20303c;border-radius:6px;overflow:hidden;margin-bottom:8px;";
    barFill = document.createElement("div");
    barFill.style.cssText = "height:100%;width:0%;background:linear-gradient(90deg,#2f81f7,#4dc3ff);transition:width .35s ease;";
    bar.appendChild(barFill);
    lineSeg = document.createElement("div");
    lineSeg.style.cssText = "font-size:12.5px;line-height:1.7;opacity:.95;";
    lineEta = document.createElement("div");
    lineEta.style.cssText = "font-size:12px;line-height:1.7;opacity:.72;";
    panel.appendChild(lineTitle);
    panel.appendChild(bar);
    panel.appendChild(lineSeg);
    panel.appendChild(lineEta);
    document.body.appendChild(panel);

    toast = document.createElement("div");
    toast.id = TOAST_ID;
    toast.style.cssText = [
      "position:fixed", "right:22px", "bottom:22px", "z-index:99999",
      "max-width:min(460px,92vw)", "display:none", "padding:14px 16px",
      "border-radius:12px", "color:#fff", "font-size:14px", "line-height:1.7",
      "box-shadow:0 12px 38px rgba(0,0,0,.55)", "pointer-events:auto",
      "font-family:system-ui,-apple-system,'Microsoft YaHei',sans-serif",
    ].join(";");
    document.body.appendChild(toast);
  }

  function render() {
    if (!state.active) return;
    ensurePanel();
    if (!panel) return;
    panel.style.display = "block";
    const elapsed = (Date.now() - state.startedAt) / 1000;
    const cfg = state.config || readConfig();
    const avg = averageSegmentSeconds();
    const total = state.totalSegments;
    const pct = total ? Math.min(99.9, (state.done / total) * 100) : Math.min(95, state.done * 4);
    barFill.style.width = pct.toFixed(1) + "%";
    const segNo = state.done + 1;
    const segText = total ? ("第 " + Math.min(segNo, total) + " / 共 " + total + " 段")
                          : ("第 " + segNo + " 段（总段数由 AI 输出决定）");
    const samplingText = state.sampling.max > 0
      ? (" · 本段采样 " + Math.min(state.sampling.value, state.sampling.max) + "/" + state.sampling.max +
         " 步（" + Math.round(100 * Math.min(state.sampling.value, state.sampling.max) / state.sampling.max) + "%）" +
         ((Date.now() - (state.lastProgressAt || Date.now())) > 15000 ? " · 条件编码/解码/收尾中…" : ""))
      : " · 模型加载/条件编码中…";
    lineTitle.innerHTML = "<span>剧核：生成中</span><span>" + pct.toFixed(0) + "%</span>";
    lineSeg.textContent = segText + samplingText;
    let etaText;
    if (total) {
      const remain = Math.max(0, total - state.done);
      const inCurrent = Math.min(avg, Math.max(0, (Date.now() - state.segStartAt) / 1000));
      etaText = "已用 " + fmtSeconds(elapsed) + " · 剩余约 " + fmtSeconds(remain * avg - (remain > 0 ? inCurrent : 0)) +
        "（平均 " + fmtSeconds(avg) + "/段）";
    } else {
      etaText = "已用 " + fmtSeconds(elapsed) + " · 已完成 " + state.done + " 段，平均 " + fmtSeconds(avg) +
        "/段（总段数确定后可给出剩余时间）";
    }
    lineEta.textContent = etaText;
    try {
      const title = "[" + pct.toFixed(0) + "%] " + segText + " · 剧核生成中…";
      if (title !== state.lastTitle) { document.title = title; state.lastTitle = title; }
    } catch (e) { /* ignore */ }
    if (state.sampling.max > 0 && state.sampling.value >= state.sampling.max && !state.countedThisSeg) {
      state.countedThisSeg = true;
      const dur = (Date.now() - (state.segStartAt || Date.now())) / 1000;
      state.segDurations.push(Math.max(1, Math.round(dur)));
      state.done += 1;
      state.segStartAt = Date.now();
    }
    if (state.sampling.max > 0 && state.sampling.value < state.sampling.max) {
      state.countedThisSeg = false;
    }
  }

  function beep(ok) {
    try {
      const AC = globalThis.AudioContext || globalThis.webkitAudioContext;
      if (!AC) return;
      if (!audioCtx) audioCtx = new AC();
      if (audioCtx.state === "suspended") audioCtx.resume();
      const t = audioCtx.currentTime;
      const seq = ok ? [[880, 0], [1174.7, 0.22], [1568, 0.44]] : [[440, 0], [330, 0.22], [220, 0.46]];
      seq.forEach(function (pair) {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = "sine";
        osc.frequency.value = pair[0];
        gain.gain.setValueAtTime(0.0001, t + pair[1]);
        gain.gain.exponentialRampToValueAtTime(0.30, t + pair[1] + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, t + pair[1] + 0.20);
        osc.connect(gain).connect(audioCtx.destination);
        osc.start(t + pair[1]);
        osc.stop(t + pair[1] + 0.22);
      });
    } catch (e) { /* 浏览器策略不允许就只用提示卡 */ }
  }

  function showToast(ok, text) {
    ensurePanel();
    if (!toast) return;
    toast.style.display = "block";
    toast.style.background = ok ? "linear-gradient(180deg,#0f5b2e,#0b3d20)" : "linear-gradient(180deg,#5b1919,#3d0f0f)";
    toast.style.border = ok ? "2px solid #34c759" : "2px solid #e5484d";
    toast.innerHTML = "";
    const title = document.createElement("div");
    title.style.cssText = "font-size:17px;font-weight:800;margin-bottom:4px;";
    title.textContent = ok ? "✅ 任务完成" : "❌ 任务失败/中断";
    const body = document.createElement("div");
    body.style.cssText = "font-size:13px;opacity:.95;white-space:pre-line;";
    body.textContent = text;
    const close = document.createElement("button");
    close.textContent = "关闭";
    close.style.cssText = "margin-top:10px;cursor:pointer;padding:4px 12px;border-radius:6px;" +
      "border:1px solid rgba(255,255,255,.35);background:rgba(255,255,255,.12);color:#fff;font-size:12px;";
    close.addEventListener("click", function () { toast.style.display = "none"; });
    toast.appendChild(title);
    toast.appendChild(body);
    toast.appendChild(close);
    if (document.body.contains(toast)) document.body.appendChild(toast);
    setTimeout(function () { if (toast) toast.style.display = "none"; }, 120000);
  }

  function notify(ok, elapsedSec) {
    state.active = false;
    state.completed = true;
    const total = state.totalSegments;
    const done = state.done;
    const text = (total ? ("段数：" + done + " / 共 " + total + " 段\n") : ("已完成段数：" + done + "\n")) +
      ("总用时：" + fmtSeconds(elapsedSec) + "\n") +
      "成片位置见节点审计的「成片核验 / 成片输出」行。";
    beep(ok);
    showToast(ok, text);
    if (panel) panel.style.display = "none";
    try { document.title = ok ? ("✅ 剧核完成 · " + fmtSeconds(elapsedSec)) : "❌ 剧核失败"; } catch (e) { /* ignore */ }
    try {
      if (globalThis.Notification) {
        if (Notification.permission === "granted") {
          new Notification(ok ? "剧核：任务完成" : "剧核：任务失败", {
            body: ok ? ("共 " + (total || done) + " 段，用时 " + fmtSeconds(elapsedSec)) : "请查看节点审计",
          });
        } else if (Notification.permission === "default") {
          Notification.requestPermission().catch(function () {});
        }
      }
    } catch (e) { /* ignore */ }
  }

  function beginRun() {
    ensurePanel();
    state = {
      active: true, startedAt: Date.now(), totalSegments: null, done: 0,
      segStartAt: Date.now(), segDurations: [], countedThisSeg: false,
      sampling: { value: 0, max: 0 }, config: null, lastTitle: "", completed: false,
    };
    state.config = readConfig();
    state.totalSegments = state.config.total;
    if (panel) panel.style.display = "block";
    render();
    try {
      if (globalThis.Notification && Notification.permission === "default") {
        Notification.requestPermission().catch(function () {});
      }
    } catch (e) { /* ignore */ }
  }

  try {
    if (!globalThis.app || !app.registerExtension) return;
    app.registerExtension({
      name: "DramaCore.ProgressPanel",
      async setup() {
        const api = globalThis.api;
        if (!api || !api.addEventListener) return;
        api.addEventListener("execution_start", function () { beginRun(); });
        api.addEventListener("executing", function (e) {
          const id = e && e.detail;
          if (!state.active || !id) return;
          const node = findNodeByExecId(id);
          const type = node && (node.comfyClass || node.type);
          if (type && type !== "BetterComfyUIOneStop" && lineSeg) {
            const seg = state.totalSegments
              ? ("第 " + Math.min(state.done + 1, state.totalSegments) + " / 共 " + state.totalSegments + " 段")
              : ("第 " + (state.done + 1) + " 段");
            lineSeg.textContent = seg + " · 当前节点：" + type;
          }
        });
        api.addEventListener("progress", function (e) {
          if (!state.active) return;
          const d = (e && e.detail) || {};
          const max = Number(d.max || 0), value = Number(d.value || 0);
          if (max > 0) {
            state.sampling = { value: value, max: max };
            state.lastProgressAt = Date.now();
            if (!state.segStartAt) state.segStartAt = Date.now();
            render();
          }
        });
        api.addEventListener("execution_success", function () {
          notify(true, state.active ? (Date.now() - state.startedAt) / 1000 : 0);
        });
        api.addEventListener("execution_error", function () {
          notify(false, state.active ? (Date.now() - state.startedAt) / 1000 : 0);
        });
        api.addEventListener("execution_interrupted", function () {
          notify(false, state.active ? (Date.now() - state.startedAt) / 1000 : 0);
        });
        // 页面在任务运行中刷新时：查询 /queue，若已有任务在跑则直接接续显示进度面板
        try {
          fetch("/queue").then(function (r) { return r.json(); }).then(function (q) {
            if (!state.active && q && (q.queue_running || []).length) {
              beginRun();
            }
          }).catch(function () { /* ignore */ });
        } catch (e) { /* ignore */ }
        // 每秒刷新一次面板：已用/剩余时间实时走字，不必等 websocket progress
        setInterval(function () { try { if (state.active) render(); } catch (e) { /* ignore */ } }, 1000);
        setInterval(function () {
          try {
            const ws = api.socket;
            if (!ws) return;
            const st = ws.readyState;
            if (st !== 0 && st !== 1) {
              if (!state.active) {
                ensurePanel();
                if (panel) {
                  panel.style.display = "block";
                  lineTitle.innerHTML = "<span>连接中断</span><span></span>";
                  lineSeg.textContent = "正在自动重连…重连成功后任务会继续";
                  lineEta.textContent = "请勿关闭页面";
                }
              } else if (lineEta) {
                lineEta.textContent = "⚠ 连接恢复中…（任务仍在服务器继续）";
              }
            } else if (state.active && lineEta) {
              render();
            } else if (panel && panel.style.display === "block" && !state.active) {
              panel.style.display = "none";
            }
          } catch (e) { /* ignore */ }
        }, 1500);
      },
    });
  } catch (e) {
    console.warn("[剧核/DramaCore] 进度面板注册失败：", e);
  }
})();
