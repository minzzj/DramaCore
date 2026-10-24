/**
 * 剧核 / DramaCore —— 网页面板（ComfyUI 前端扩展）
 *
 * 目标：让用户在 ComfyUI 网页里**不记节点名、不翻文档**就能完成：
 *   填 API（Key / 模型 / 接口地址）→ 测试连接 → 配创作默认值（系统提示词 / 语言 /
 *   默认视频/图像模型）→ 选项目 → 填剧本 → 跑流程 → 看状态与审核结论。
 *
 * 设计取舍（都是实测踩坑后定下的）：
 * - **只用 ComfyUI 自带的 app/api**，不依赖第三方前端库，避免版本升级就崩。
 * - **中文 / English 双语**：文案来自 `dramacore_i18n.js`（挂在 globalThis.DramaCoreI18N，
 *   零网络请求）。本文件在它缺失时也能加载（降级为显示 key），保证"面板坏了不白屏"。
 * - **多语言优先级**：本浏览器选择（localStorage）> 服务端 config/user.yaml 的 ui.language
 *   > 浏览器语言。面板文案即时切换；节点标题需刷新页面（ComfyUI 节点表在加载时建立）。
 * - **创建失败不能拖垮网页**：整个 setup 包在 try/catch，失败只在控制台告警。
 * - **不创建浮动窗口**：找不到侧边栏就不挂载，改用 ComfyUI 原生入口（设置 + 工作流）。
 * - 面板路由全部在 `/bettercomfyui/` 下（api.fetchApi 会自动加 /api 前缀）。
 *
 * 安装：本文件位于 <插件>/web/js/ 下，由 `WEB_DIRECTORY = "./web"` 自动加载。
 */

import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

const EXT_NAME = "DramaCore.Panel";
const PANEL_ID = "dramacore-panel";
const STYLE_ID = "dramacore-panel-style";

// ⛔ 用户要求：第二阶段入口保留但不展示。改成 true 即可启用（无需改其他代码）。
const ENABLE_PHASE2_UI = false;

// ---------------------------------------------------------------------------
// 样式（剧核品牌色：深底 + 青金；尽量复用 ComfyUI 的 CSS 变量，兼容浅色主题）
// ---------------------------------------------------------------------------

const CSS = `
#${PANEL_ID} {
  display: flex; flex-direction: column; gap: 8px;
  padding: 10px 10px 14px; font-size: 12px; color: var(--fg-color, #ddd);
  background: var(--comfy-menu-bg, #202020);
  border-radius: 8px; margin: 6px;
  border: 1px solid rgba(64, 196, 255, .18);
}
#${PANEL_ID} .dc-title {
  font-weight: 800; font-size: 14px; display: flex; align-items: center; gap: 6px;
  background: linear-gradient(90deg, #35c5ff, #7ee787);
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
#${PANEL_ID} .dc-ver { margin-left: auto; font-weight: 500; font-size: 11px; opacity: .65; color: var(--fg-color, #ddd); }
#${PANEL_ID} .dc-tagline { opacity: .72; font-size: 11px; line-height: 1.5; margin-top: -2px; }
#${PANEL_ID} .dc-group {
  border: 1px solid var(--border-color, #3a3a3a); border-radius: 6px;
  padding: 6px 8px 8px; background: rgba(255, 255, 255, .015);
}
#${PANEL_ID} .dc-group > summary {
  cursor: pointer; font-weight: 700; font-size: 12px; padding: 3px 0; outline: none;
  color: #7ee787;
}
#${PANEL_ID} .dc-group[open] > summary { margin-bottom: 5px; }
#${PANEL_ID} .dc-row { display: flex; gap: 6px; align-items: center; margin: 5px 0; }
#${PANEL_ID} .dc-row > * { flex: 1 1 auto; min-width: 0; }
#${PANEL_ID} .dc-grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin: 5px 0; }
#${PANEL_ID} .dc-grid2 > div { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
#${PANEL_ID} label { font-size: 11px; opacity: .82; margin-top: 3px; display: block; }
#${PANEL_ID} button {
  flex: 0 0 auto; padding: 5px 9px; cursor: pointer; border-radius: 4px;
  border: 1px solid var(--border-color, #444);
  background: var(--comfy-input-bg, #2b2b2b); color: inherit; font-size: 12px;
}
#${PANEL_ID} button:hover { filter: brightness(1.25); }
#${PANEL_ID} button.dc-primary { background: #1668c8; border-color: #1668c8; color: #fff; }
#${PANEL_ID} button:disabled { opacity: .5; cursor: not-allowed; }
#${PANEL_ID} input, #${PANEL_ID} select, #${PANEL_ID} textarea {
  width: 100%; box-sizing: border-box; padding: 4px 6px; border-radius: 4px;
  border: 1px solid var(--border-color, #444);
  background: var(--comfy-input-bg, #2b2b2b); color: inherit; font-size: 12px;
}
#${PANEL_ID} textarea { min-height: 64px; resize: vertical; font-family: monospace; }
#${PANEL_ID} input[type="password"] { font-family: monospace; }
#${PANEL_ID} .dc-muted { opacity: .7; font-size: 11px; line-height: 1.45; }
#${PANEL_ID} .dc-status-line { padding: 4px 6px; border-radius: 4px; background: rgba(126, 231, 135, .06); }
#${PANEL_ID} .dc-badges { display: flex; flex-direction: column; gap: 3px; margin: 4px 0; }
#${PANEL_ID} .dc-badge { padding: 3px 6px; border-radius: 4px; font-size: 11px; line-height: 1.4; }
#${PANEL_ID} .dc-badge.dc-ok { background: rgba(76, 175, 80, .16); color: #7ee787; }
#${PANEL_ID} .dc-badge.dc-warn { background: rgba(240, 180, 41, .14); color: #f0b429; }
#${PANEL_ID} .dc-badge.dc-err { background: rgba(239, 83, 80, .16); color: #ef9a9a; }
#${PANEL_ID} .dc-log {
  max-height: 150px; overflow: auto; white-space: pre-wrap; word-break: break-all;
  font-family: monospace; font-size: 11px; padding: 6px;
  background: var(--comfy-input-bg, #1b1b1b); border-radius: 4px;
  border: 1px solid var(--border-color, #3a3a3a);
}
#${PANEL_ID} .dc-warn { color: #f0b429; }
#${PANEL_ID} .dc-ok { color: #4caf50; }
#${PANEL_ID} .dc-err { color: #ef5350; }
#${PANEL_ID} .dc-hidden { display: none !important; }
`;

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const el = document.createElement("style");
  el.id = STYLE_ID;
  el.textContent = CSS;
  document.head.appendChild(el);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

/** 读取多语言资源；缺失时降级（返回 key），保证面板不崩。 */
function makeI18n(preferred) {
  const i18n = globalThis.DramaCoreI18N;
  if (i18n && typeof i18n.createI18n === "function") {
    try {
      return i18n.createI18n(preferred);
    } catch (err) {
      console.warn("[剧核/DramaCore] i18n 初始化失败，降级为原始 key：", err);
    }
  }
  // 降级：显示 key（仅在 dramacore_i18n.js 缺失时发生，不会白屏）
  return {
    lang: "zh",
    setLang() { return "zh"; },
    t: (key) => key,
    nodeName: (id) => id,
    statusLabel: (k) => k,
  };
}

// ---------------------------------------------------------------------------
// 面板
// ---------------------------------------------------------------------------

export class BetterComfyUIPanel {
  constructor() {
    this.root = null;
    this.logEl = null;
    this.projects = [];
    this.lastReport = null;
    this.config = null;
    this.i18n = makeI18n();
    this.host = null;
  }

  t(key, vars) {
    return this.i18n.t(key, vars);
  }

  log(msg, kind = "") {
    if (!this.logEl) return;
    const cls = kind ? ` class="dc-${kind}"` : "";
    const ts = new Date().toLocaleTimeString();
    this.logEl.insertAdjacentHTML("beforeend",
      `<div${cls}>[${ts}] ${escapeHtml(String(msg))}</div>`);
    this.logEl.scrollTop = this.logEl.scrollHeight;
  }

  setBusy(busy) {
    const buttons = this.root?.querySelectorAll("button[data-busy-guard]") ?? [];
    for (const b of buttons) b.disabled = busy;
  }

  async callApi(path, options) {
    const res = await api.fetchApi(path, options);
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = { raw: text };
    }
    if (!res.ok) {
      const detail = (data && (data.error || data.detail || data.raw)) || res.statusText;
      throw new Error(`${res.status} ${detail}`);
    }
    return data || {};
  }

  // -------------------------------------------------------------------
  // 多语言：把 data-i18n / data-i18n-ph 的静态文案按当前语言重刷
  // -------------------------------------------------------------------
  applyI18n() {
    try {
      for (const el of this.root?.querySelectorAll("[data-i18n]") ?? []) {
        el.textContent = this.t(el.dataset.i18n);
      }
      for (const el of this.root?.querySelectorAll("[data-i18n-ph]") ?? []) {
        el.placeholder = this.t(el.dataset.i18nPh);
      }
    } catch (err) {
      console.warn("[剧核/DramaCore] 文案刷新失败：", err);
    }
  }

  // -------------------------------------------------------------------
  // 配置读取 / 保存 / 测试连接（一条龙：这是用户"信息要全"的落点）
  // -------------------------------------------------------------------
  async loadPanelConfig() {
    const apiStatusEl = this.root?.querySelector("#dc-api-status");
    let data;
    try {
      data = await this.callApi("/bettercomfyui/panel/config");
    } catch (err) {
      if (apiStatusEl) {
        apiStatusEl.textContent = `读取配置失败：${err.message}`;
        apiStatusEl.className = "dc-muted dc-status-line dc-err";
      }
      return null;
    }
    if (data.ok === false) {
      if (apiStatusEl) apiStatusEl.textContent = `读取配置失败：${data.error ?? "未知错误"}`;
      return null;
    }
    this.config = data;

    // 版本 / 品牌
    const verEl = this.root?.querySelector("#dc-ver");
    if (verEl) verEl.textContent = data.version ? this.t("brand.version", { v: data.version }) : "";

    // 连接配置
    const apiInfo = data.api || {};
    const modelEl = this.root?.querySelector("#dc-api-model");
    if (modelEl && apiInfo.model) modelEl.value = apiInfo.model;
    const baseEl = this.root?.querySelector("#dc-api-base");
    if (baseEl && apiInfo.endpoint) baseEl.value = apiInfo.endpoint;
    if (apiStatusEl) {
      if (apiInfo.configured) {
        apiStatusEl.innerHTML = `<span class="dc-ok">✔ ${escapeHtml(this.t("api.status.configured"))}</span>`
          + ` — ${escapeHtml(this.t("api.status.masked"))}: ${escapeHtml(apiInfo.masked || "")}`
          + ` ｜ ${escapeHtml(this.t("api.status.source"))}: ${escapeHtml(apiInfo.source || "?")}`;
        apiStatusEl.className = "dc-muted dc-status-line";
      } else {
        apiStatusEl.textContent = `✘ ${this.t("api.status.missing")} — ${this.t("api.status.hint.missing")}`;
        apiStatusEl.className = "dc-muted dc-status-line dc-warn";
      }
    }

    // 创作默认值
    const defaults = data.defaults || {};
    const sysEl = this.root?.querySelector("#dc-system-prompt");
    if (sysEl) sysEl.value = defaults.system_prompt || "";
    const outLangEl = this.root?.querySelector("#dc-output-language");
    if (outLangEl) outLangEl.value = defaults.output_language || "zh";
    const uiLangEl = this.root?.querySelector("#dc-ui-language");
    if (uiLangEl) uiLangEl.value = defaults.ui_language || "auto";
    const vModelEl = this.root?.querySelector("#dc-video-model");
    if (vModelEl) vModelEl.value = defaults.video_model || "";
    const iModelEl = this.root?.querySelector("#dc-image-model");
    if (iModelEl) iModelEl.value = defaults.image_model || "";

    // 模型候选（datalist）：服务端扫描 ComfyUI models 目录，扫不到就给推荐值
    this.fillDatalist("#dc-video-models", data.options?.video_models);
    this.fillDatalist("#dc-image-models", data.options?.image_models);

    // 服务端配置的语言：若本浏览器没有手动选择，则按服务端配置刷新面板文案
    try {
      const local = localStorage.getItem("dramacore.uiLang");
      if ((!local || local === "auto") && defaults.ui_language) {
        this.i18n = makeI18n(defaults.ui_language);
        this.applyI18n();
      }
    } catch (e) { /* localStorage 不可用则忽略 */ }
    return data;
  }

  fillDatalist(selector, values) {
    const list = this.root?.querySelector(selector);
    if (!list) return;
    try {
      list.innerHTML = "";
      for (const v of values || []) {
        const opt = document.createElement("option");
        opt.value = String(v);
        list.appendChild(opt);
      }
    } catch (err) {
      console.warn("[剧核/DramaCore] 模型候选填充失败：", err);
    }
  }

  collectConnectionPayload() {
    const payload = {};
    const key = (this.root?.querySelector("#dc-api-key")?.value ?? "").trim();
    if (key) payload.api_key = key;                       // 空 = 不覆盖已保存密钥
    const model = (this.root?.querySelector("#dc-api-model")?.value ?? "").trim();
    if (model) payload.model_name = model;
    const base = (this.root?.querySelector("#dc-api-base")?.value ?? "").trim();
    if (base) payload.endpoint = base;
    return payload;
  }

  async saveConnection() {
    const payload = this.collectConnectionPayload();
    if (!Object.keys(payload).length) {
      this.log(this.t("log.no_change"), "warn");
      return;
    }
    this.setBusy(true);
    try {
      const d = await this.callApi("/bettercomfyui/panel/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (d.ok === false) {
        this.log(this.t("log.save_fail", { error: d.error ?? "unknown" }), "err");
        return;
      }
      // 保存成功后立刻清空密钥输入框，避免明文停留在 DOM
      const keyEl = this.root?.querySelector("#dc-api-key");
      if (keyEl) keyEl.value = "";
      const written = (d.saved?.written ?? []).join(", ");
      this.log(this.t("log.api_ok", { written: written || "-", path: d.saved?.path ?? "config/user.yaml" }), "ok");
      await this.loadPanelConfig();
    } catch (err) {
      this.log(this.t("log.save_fail", { error: err.message }), "err");
    } finally {
      this.setBusy(false);
    }
  }

  async saveDefaults() {
    const payload = {
      system_prompt: (this.root?.querySelector("#dc-system-prompt")?.value ?? ""),
      output_language: this.root?.querySelector("#dc-output-language")?.value || "zh",
      ui_language: this.root?.querySelector("#dc-ui-language")?.value || "auto",
      video_model: (this.root?.querySelector("#dc-video-model")?.value ?? "").trim(),
      image_model: (this.root?.querySelector("#dc-image-model")?.value ?? "").trim(),
    };
    this.setBusy(true);
    try {
      const d = await this.callApi("/bettercomfyui/panel/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (d.ok === false) {
        this.log(this.t("log.save_fail", { error: d.error ?? "unknown" }), "err");
        return;
      }
      const written = (d.saved?.written ?? []).join(", ");
      this.log(this.t("log.config_ok", { written: written || "-", path: d.saved?.path ?? "config/user.yaml" }), "ok");
      // 界面语言即时生效（面板文案立刻切换；节点标题需刷新页面）
      const newLang = this.i18n.setLang(payload.ui_language);
      this.applyI18n();
      this.log(`${this.t("defaults.saved")}（UI: ${newLang}）`, "ok");
      this.log(this.t("defaults.need_reload"), "warn");
      await this.loadPanelConfig();
    } catch (err) {
      this.log(this.t("log.save_fail", { error: err.message }), "err");
    } finally {
      this.setBusy(false);
    }
  }

  async testConnection() {
    const resultEl = this.root?.querySelector("#dc-test-result");
    const payload = this.collectConnectionPayload();
    if (resultEl) resultEl.textContent = this.t("common.testing");
    this.setBusy(true);
    try {
      const d = await this.callApi("/bettercomfyui/panel/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (d.ok) {
        const msg = this.t("api.test.ok", { ms: d.latency_ms ?? "?", n: d.models_count ?? 0 });
        const sample = (d.models_sample || []).slice(0, 5).join(", ");
        if (resultEl) {
          resultEl.textContent = sample
            ? `${msg} ｜ ${this.t("api.test.ok.sample", { list: sample })}`
            : msg;
          resultEl.className = "dc-muted dc-ok";
        }
        this.log(msg, "ok");
      } else {
        const msg = this.t("api.test.fail", { error: d.error ?? d.hint ?? "unknown" });
        if (resultEl) { resultEl.textContent = msg; resultEl.className = "dc-muted dc-err"; }
        this.log(msg, "err");
      }
    } catch (err) {
      const msg = this.t("api.test.fail", { error: err.message });
      if (resultEl) { resultEl.textContent = msg; resultEl.className = "dc-muted dc-err"; }
      this.log(msg, "err");
    } finally {
      this.setBusy(false);
    }
  }

  // -------------------------------------------------------------------
  // 体检（状态可视化：每项一个彩色徽标，不糊成一段文字）
  // -------------------------------------------------------------------
  async refreshDoctor() {
    this.setBusy(true);
    const badges = this.root?.querySelector("#dc-badges");
    try {
      const d = await this.callApi("/bettercomfyui/doctor");
      const lines = [];
      const add = (kind, text) => lines.push(
        `<div class="dc-badge dc-${kind}">${escapeHtml(text)}</div>`);
      add("ok", this.t("doctor.node_count", { n: d.node_count ?? "?" }));
      if (d.orchestrator_available) {
        add("ok", this.t("doctor.orchestrator.ok", { file: d.orchestrator_file || "?" }));
      } else {
        add("err", this.t("doctor.orchestrator.fail", { error: d.orchestrator_error || "?" }));
      }
      add(d.ffmpeg_available ? "ok" : "warn",
        d.ffmpeg_available ? this.t("doctor.ffmpeg.ok")
          : this.t("doctor.ffmpeg.fail", { error: d.ffmpeg_error || "?" }));
      if (d.comfyui_version) add("warn", this.t("doctor.comfyui", { v: d.comfyui_version }));
      const compute = d.compute || {};
      if (compute.available === true) {
        add("ok", this.t("doctor.compute.yes", { device: compute.device || "?", vram: compute.vram_mib ?? "?" }));
      } else if (compute.available === false) {
        add("warn", this.t("doctor.compute.no", { reason: compute.reason || "?" }));
      }
      add(d.real_generation_available ? "ok" : "warn",
        d.real_generation_available ? this.t("doctor.real_gen.yes") : this.t("doctor.real_gen.no"));
      if (badges) badges.innerHTML = lines.join("");
      // 日志里也留一份纯文本（便于复制给运维）
      this.log(`插件 v${d.plugin_version ?? "?"}，节点 ${d.node_count ?? "?"} 个`,
        d.orchestrator_available ? "ok" : "warn");
      if (d.legacy_comfyui_warning) this.log(d.legacy_comfyui_warning, "warn");
      const verEl = this.root?.querySelector("#dc-ver");
      if (verEl && d.plugin_version) verEl.textContent = this.t("brand.version", { v: d.plugin_version });
    } catch (err) {
      if (badges) badges.innerHTML = `<div class="dc-badge dc-err">${escapeHtml(err.message)}</div>`;
      this.log(`体检失败：${err.message}`, "err");
    } finally {
      this.setBusy(false);
    }
  }

  // -------------------------------------------------------------------
  // 项目 / 运行
  // -------------------------------------------------------------------
  async refreshProjects() {
    try {
      const d = await this.callApi("/bettercomfyui/projects");
      this.projects = d.projects || [];
      const sel = this.root?.querySelector("#dc-project");
      if (!sel) return;
      sel.innerHTML = "";
      if (!this.projects.length) {
        sel.innerHTML = `<option value="">${escapeHtml(this.t("project.none"))}</option>`;
      } else {
        for (const p of this.projects) {
          const o = document.createElement("option");
          o.value = p.project_id;
          o.textContent = `${p.display_name} · ${String(p.project_id).slice(0, 8)}`;
          sel.appendChild(o);
        }
      }
      this.log(`项目列表：${this.projects.length} 个`);
    } catch (err) {
      this.log(`读取项目列表失败：${err.message}`, "err");
    }
  }

  async openOrCreateProject() {
    const nameEl = this.root?.querySelector("#dc-project-name");
    const sel = this.root?.querySelector("#dc-project");
    const name = (nameEl?.value || "").trim();
    const pid = sel?.value || "";
    this.setBusy(true);
    try {
      const d = await this.callApi("/bettercomfyui/project/open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_name: name, project_id: pid }),
      });
      this.log(`已就绪项目 ${d.project_id}（${d.display_name}）`, "ok");
      await this.refreshProjects();
      const sel2 = this.root?.querySelector("#dc-project");
      if (sel2) sel2.value = d.project_id;
    } catch (err) {
      this.log(`打开/创建失败：${err.message}`, "err");
    } finally {
      this.setBusy(false);
    }
  }

  async startRun() {
    const sel = this.root?.querySelector("#dc-project");
    const scriptEl = this.root?.querySelector("#dc-script");
    const execEl = this.root?.querySelector("#dc-executor");
    const projectId = sel?.value || "";
    if (!projectId) {
      this.log("请先选择或创建一个项目", "err");
      return;
    }
    let script = null;
    const raw = (scriptEl?.value || "").trim();
    if (raw) {
      try {
        script = JSON.parse(raw);
      } catch (err) {
        this.log(`剧本不是合法 JSON：${err.message}`, "err");
        return;
      }
    }
    this.setBusy(true);
    this.log(`开始运行（执行器：${execEl?.value ?? "mock"}）…这可能需要几分钟`);
    try {
      const d = await this.callApi("/bettercomfyui/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId, script, executor: execEl?.value ?? "mock" }),
      });
      this.lastReport = d.report;
      this.renderReport(d.report);
    } catch (err) {
      this.log(`运行失败：${err.message}`, "err");
    } finally {
      this.setBusy(false);
    }
  }

  renderReport(rep) {
    if (!rep) return;
    if (rep.ok) this.log(`运行成功：成片 ${rep.output_path}`, "ok");
    else this.log(`运行失败：${rep.error}`, "err");
    if (rep.used_mock) {
      this.log("⚠️ 本次为占位媒体路径：产出不是模型生成结果（已写入元数据 generation_provenance）", "warn");
    }
    if (rep.unverified?.length) {
      this.log(`未验证项（${rep.unverified.length}）：${rep.unverified.join("；")}`, "warn");
    }
  }

  async insertMinimalWorkflow() {
    this.setBusy(true);
    try {
      const wf = await this.callApi("/bettercomfyui/workflow/minimal");
      if (!wf || !wf.nodes) throw new Error("服务端未返回工作流内容");
      if (typeof app.loadGraphData === "function") {
        await app.loadGraphData(wf);
        this.log(this.t("workflow.loaded"), "ok");
      } else {
        this.log(this.t("workflow.unsupported"), "warn");
      }
    } catch (err) {
      this.log(`载入工作流失败：${err.message}`, "err");
    } finally {
      this.setBusy(false);
    }
  }

  // -------------------------------------------------------------------
  // 构建 DOM
  // -------------------------------------------------------------------
  build() {
    const root = document.createElement("div");
    root.id = PANEL_ID;

    // ⛔ 第二阶段区块：保留接入点但不在界面展示（ENABLE_PHASE2_UI 打开即可）。
    const phase2Html = ENABLE_PHASE2_UI
      ? `<div class="dc-row"><button id="dc-phase2">第二阶段（自动抓取/发布）</button></div>`
      : ``;

    root.innerHTML = `
      <div class="dc-title">
        <span id="dc-brand">剧核 / DramaCore</span>
        <span class="dc-ver" id="dc-ver"></span>
      </div>
      <div class="dc-tagline" id="dc-tagline" data-i18n="brand.tagline"></div>

      <details class="dc-group" open>
        <summary id="dc-g-connection" data-i18n="group.connection">① 连接配置</summary>
        <div class="dc-muted" id="dc-connection-hint" data-i18n="group.connection.hint"></div>
        <div class="dc-muted dc-status-line" id="dc-api-status" data-i18n="api.status.reading">读取中…</div>
        <label for="dc-api-key" id="dc-l-api-key" data-i18n="api.key">API Key</label>
        <input id="dc-api-key" type="password" autocomplete="off" data-i18n-ph="api.key.ph" />
        <div class="dc-grid2">
          <div>
            <label for="dc-api-base" id="dc-l-api-base" data-i18n="api.base">API Base</label>
            <input id="dc-api-base" data-i18n-ph="api.base.ph" />
          </div>
          <div>
            <label for="dc-api-model" id="dc-l-api-model" data-i18n="api.model">模型名</label>
            <input id="dc-api-model" data-i18n-ph="api.model.ph" />
          </div>
        </div>
        <div class="dc-row">
          <button data-busy-guard id="dc-api-test" data-i18n="common.test">测试连接</button>
          <button data-busy-guard class="dc-primary" id="dc-api-save" data-i18n="common.save">保存</button>
        </div>
        <div class="dc-muted" id="dc-test-result"></div>
      </details>

      <details class="dc-group" open>
        <summary id="dc-g-defaults" data-i18n="group.defaults">② 创作默认值</summary>
        <div class="dc-muted" id="dc-defaults-hint" data-i18n="group.defaults.hint"></div>
        <label for="dc-system-prompt" id="dc-l-system-prompt" data-i18n="defaults.system_prompt">默认系统提示词</label>
        <textarea id="dc-system-prompt" data-i18n-ph="defaults.system_prompt.ph"></textarea>
        <div class="dc-grid2">
          <div>
            <label for="dc-ui-language" id="dc-l-ui-language" data-i18n="defaults.ui_language">界面语言</label>
            <select id="dc-ui-language">
              <option value="auto" data-i18n="defaults.lang.auto">自动</option>
              <option value="zh" data-i18n="defaults.lang.zh">中文</option>
              <option value="en" data-i18n="defaults.lang.en">English</option>
            </select>
          </div>
          <div>
            <label for="dc-output-language" id="dc-l-output-language" data-i18n="defaults.output_language">输出语言</label>
            <select id="dc-output-language">
              <option value="zh">中文</option>
              <option value="en">English</option>
            </select>
          </div>
        </div>
        <div class="dc-grid2">
          <div>
            <label for="dc-video-model" id="dc-l-video-model" data-i18n="defaults.video_model">默认视频模型</label>
            <input id="dc-video-model" list="dc-video-models" data-i18n-ph="defaults.model.ph" />
            <datalist id="dc-video-models"></datalist>
          </div>
          <div>
            <label for="dc-image-model" id="dc-l-image-model" data-i18n="defaults.image_model">默认图像模型</label>
            <input id="dc-image-model" list="dc-image-models" data-i18n-ph="defaults.model.ph" />
            <datalist id="dc-image-models"></datalist>
          </div>
        </div>
        <div class="dc-row">
          <button data-busy-guard class="dc-primary" id="dc-defaults-save" data-i18n="common.save">保存</button>
        </div>
        <div class="dc-muted" id="dc-defaults-result"></div>
      </details>

      <details class="dc-group" open>
        <summary id="dc-g-workspace" data-i18n="group.workspace">③ 项目与运行</summary>
        <div class="dc-row">
          <input id="dc-project-name" data-i18n-ph="project.name.ph" />
          <button data-busy-guard id="dc-open" data-i18n="project.open">打开或创建</button>
        </div>
        <div class="dc-row">
          <select id="dc-project"><option value="">（加载中…）</option></select>
          <button data-busy-guard id="dc-refresh" data-i18n="project.refresh">刷新项目</button>
        </div>
        <label for="dc-executor" id="dc-l-executor" data-i18n="executor">执行器</label>
        <select id="dc-executor">
          <option value="mock" data-i18n="executor.mock">占位媒体</option>
          <option value="comfyui" disabled data-i18n="executor.real">真实 ComfyUI</option>
        </select>
        <label for="dc-script" id="dc-l-script" data-i18n="script">剧本 JSON</label>
        <textarea id="dc-script" data-i18n-ph="script"></textarea>
        <div class="dc-row">
          <button data-busy-guard class="dc-primary" id="dc-run" data-i18n="run">开始运行</button>
          <button data-busy-guard id="dc-loadwf" data-i18n="workflow.load">载入最简工作流</button>
        </div>
        ${phase2Html}
      </details>

      <details class="dc-group" open>
        <summary id="dc-g-status" data-i18n="group.status">④ 状态与审核结论</summary>
        <div class="dc-row">
          <button data-busy-guard id="dc-doctor" data-i18n="doctor">体检</button>
        </div>
        <div class="dc-muted" id="dc-status-hint" data-i18n="status.no_data"></div>
        <div class="dc-badges" id="dc-badges"></div>
        <div class="dc-muted" id="dc-log-title" data-i18n="log.title">运行日志</div>
        <div class="dc-log" id="dc-log"></div>
      </details>
    `;

    // 事件绑定（addEventListener，避免内联 onclick 被 CSP 拦）
    root.querySelector("#dc-api-test")?.addEventListener("click", () => this.testConnection());
    root.querySelector("#dc-api-save")?.addEventListener("click", () => this.saveConnection());
    root.querySelector("#dc-defaults-save")?.addEventListener("click", () => this.saveDefaults());
    root.querySelector("#dc-refresh")?.addEventListener("click", () => this.refreshProjects());
    root.querySelector("#dc-open")?.addEventListener("click", () => this.openOrCreateProject());
    root.querySelector("#dc-run")?.addEventListener("click", () => this.startRun());
    root.querySelector("#dc-loadwf")?.addEventListener("click", () => this.insertMinimalWorkflow());
    root.querySelector("#dc-doctor")?.addEventListener("click", () => this.refreshDoctor());
    // 语言下拉：文案即时切换（保存后生效；节点标题刷新页面后生效）
    root.querySelector("#dc-ui-language")?.addEventListener("change", (ev) => {
      this.i18n.setLang(ev?.target?.value || "auto");
      this.applyI18n();
    });
    if (ENABLE_PHASE2_UI) {
      root.querySelector("#dc-phase2")?.addEventListener("click", async () => {
        try {
          this.log(JSON.stringify(await this.callApi("/bettercomfyui/phase2/status"), null, 2));
        } catch (err) {
          this.log(err.message, "err");
        }
      });
    }

    this.root = root;
    this.logEl = root.querySelector("#dc-log");
    this.applyI18n();
    return root;
  }

  /** 找到合适的挂载点。找不到返回 null —— 此时不创建任何窗口（用户明确要求）。 */
  findMountPoint() {
    const selectors = [
      "#comfyui-sidebar",
      ".comfyui-sidebar",
      "#sidebar",
      "[data-testid='sidebar']",
    ];
    for (const s of selectors) {
      const el = document.querySelector(s);
      if (el) return el;
    }
    return null;
  }

  mount() {
    if (document.getElementById(PANEL_ID)) return;
    const host = this.findMountPoint();
    if (!host) {
      console.info(
        "[剧核/DramaCore] 未找到侧边栏：不创建浮动面板。" +
        "请用 ComfyUI 原生「设置」配置 API，或用原生工作流（W0/W1/W3）运行。"
      );
      return;
    }
    this.host = host;
    host.appendChild(this.build());
  }
}

// ---------------------------------------------------------------------------
// 原生设置注册（用户要求：不要自定义窗口，用 ComfyUI 自带的「设置」）
// ---------------------------------------------------------------------------
//
// ComfyUI 的齿轮 → 设置里会多出「剧核 / DramaCore」分组：
//   * API 密钥  —— 用户操作后写入服务器 config/user.yaml **并同步给剧本巨人**
//   * 模型名 / 接口地址 / 默认系统提示词
// 这样即使侧边栏不存在、面板不挂载，用户也能在原生界面里完成配置。
function registerNativeSettings() {
  const ui = app?.ui?.settings;
  if (!ui?.addSetting) return false;

  // ⚠️ 只在「真实用户操作」之后才允许保存设置。
  // 原因（实测事故）：ComfyUI 在页面加载/恢复持久化设置时也会触发 onChange，
  // 之前的实现会直接 POST /bettercomfyui/settings —— 用户只是打开网页，
  // config/user.yaml 就被静默改写了。用「用户是否真的交互过」做门闩。
  let userActed = false;
  const markUserActed = () => { userActed = true; };
  for (const evt of ["pointerdown", "keydown", "touchstart", "wheel"]) {
    document.addEventListener(evt, markUserActed, { capture: true, passive: true });
  }

  const saveToServer = async (payload) => {
    if (!userActed) {
      console.info("[剧核/DramaCore] 跳过设置恢复触发的自动保存（只响应用户操作）");
      return;
    }
    try {
      const res = await fetch("/api/bettercomfyui/panel/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (data.ok === false) {
        console.warn("[剧核/DramaCore] 保存 API 配置失败：", data.error);
      } else {
        console.info("[剧核/DramaCore] API 配置已保存：", (data.saved?.written || []).join(", "));
      }
    } catch (err) {
      console.warn("[剧核/DramaCore] 保存 API 配置异常：", err);
    }
  };

  ui.addSetting({
    id: "DramaCore.ApiKey",
    name: "剧核 / DramaCore · API 密钥（DeepSeek 等）\n保存后写入 config/user.yaml 并同步给剧本巨人",
    type: "text",
    defaultValue: "",
    onChange: (value) => {
      const key = (value ?? "").trim();
      if (key) saveToServer({ api_key: key });
    },
  });
  ui.addSetting({
    id: "DramaCore.ApiModel",
    name: "剧核 / DramaCore · 模型名（如 deepseek-chat）",
    type: "text",
    defaultValue: "deepseek-chat",
    onChange: (value) => {
      const model = (value ?? "").trim();
      if (model) saveToServer({ model_name: model });
    },
  });
  ui.addSetting({
    id: "DramaCore.ApiBase",
    name: "剧核 / DramaCore · 接口地址（留空 = 官方默认）",
    type: "text",
    defaultValue: "https://api.deepseek.com",
    onChange: (value) => {
      const base = (value ?? "").trim();
      if (base) saveToServer({ endpoint: base });
    },
  });
  return true;
}

// ---------------------------------------------------------------------------
// 注册扩展
// ---------------------------------------------------------------------------

const panel = new BetterComfyUIPanel();

app.registerExtension({
  name: EXT_NAME,
  async setup() {
    try {
      injectStyle();
      // 懒加载单节点导演台 UI（新文件；即使 /extensions 列表被缓存也能挂上）
      try {
        await import("./dramacore_director_ui_v016.js?v=20261025v016");
      } catch (err) {
        console.info("[剧核/DramaCore] 导演台 UI 模块未加载（不影响面板）：", err?.message || err);
      }
      panel.mount();
      if (!panel.root) {
        // 没有侧边栏：不挂载面板，但原生设置仍要注册（用户仍能配 API）
        registerNativeSettings();
        return;
      }
      panel.log(panel.t("log.hint"));
      // 先读配置（API 状态 / 默认值），再拉项目与体检
      await panel.loadPanelConfig();
      panel.refreshProjects();
      panel.refreshDoctor();
      registerNativeSettings();
    } catch (err) {
      // 面板失败不能拖垮 ComfyUI 网页
      console.warn("[剧核/DramaCore] 面板初始化失败（不影响 ComfyUI 其他功能）：", err);
    }
  },
});

export default panel;
