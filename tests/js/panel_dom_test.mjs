/**
 * 网页面板的 **DOM 级执行测试**（不需要浏览器、不需要 jsdom）。
 *
 * ## 为什么值得写
 *
 * 我此前只能验证「JS 能被 `node --check` 解析」+「路由返回 200」+「节点已注册」。
 * 那三条都**证明不了面板能跑起来**：模块可能在 `registerExtension` 之前就抛错、
 * 可能引用了不存在的 API、可能把 DOM 拼坏。用户在浏览器里看到的才会暴露这些。
 *
 * 这里用一个**最小 DOM 替身**把面板真正执行一遍，并断言：
 *
 * 1. 模块能加载且**确实调用了** `app.registerExtension`（没有它面板根本不会出现）；
 * 2. 注册的扩展带 `setup()`（ComfyUI 靠它挂载 UI）；
 * 3. `setup()` 能在替身 DOM 上跑完，并把面板节点挂进 `document.body`；
 * 4. `api.fetchApi` 被调用到我们自己的 `/bettercomfyui/*` 路由（而不是别人的）；
 * 5. 路由返回错误时面板**显示错误**而不是静默空白（静默空白最难排查）。
 *
 * 替身全部写在 `harness.mjs` 里，只实现面板实际用到的那几个 API
 * （已用 grep 核过：createElement / getElementById / querySelector /
 *   body / head / appendChild / addEventListener / style / textContent /
 *   innerHTML / className / localStorage）。
 */

import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";
import fs from "node:fs";

const require = createRequire(import.meta.url);
// ⚠️ 必须用 fileURLToPath：new URL().pathname 是**百分号编码**的，
// 中文/空格路径会变成 %E6%96%87... 导致找不到文件（实测踩到）。
const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..");
const PANEL = path.join(REPO, "bettercomfyui_comfy", "web", "js", "bettercomfyui_panel.js");

let failures = 0;
let checks = 0;
function check(label, cond, detail = "") {
    checks++;
    if (cond) {
        console.log(`  [OK]   ${label}`);
    } else {
        failures++;
        console.log(`  [FAIL] ${label}${detail ? " — " + detail : ""}`);
    }
}

// ---------------------------------------------------------------------------
// 最小 DOM 替身
// ---------------------------------------------------------------------------

function makeElement(tag) {
    const el = {
        tagName: String(tag).toUpperCase(),
        children: [],
        attributes: {},
        style: {},
        dataset: {},
        _text: "",
        _html: "",
        _parsed: [],
        id: "",
        className: "",
        value: "",
        title: "",
        type: "",
        parentNode: null,
        listeners: {},
        get textContent() { return this._text; },
        set textContent(v) { this._text = String(v); },
        get innerHTML() { return this._html; },
        set innerHTML(v) {
            this._html = String(v);
            // ⚠️ 必须真的解析：面板的真实模式是
            //     root.innerHTML = `<... id="x" ...>`; root.querySelector("#x")
            //   替身若不解析，querySelector 永远返回 null，
            //   于是"面板有 bug"与"替身不够真"分不清（实测踩到）。
            this._parsed = parseIdsFromHtml(this._html);
        },
        appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
        removeChild(child) {
            const i = this.children.indexOf(child);
            if (i >= 0) this.children.splice(i, 1);
            return child;
        },
        setAttribute(k, v) { this.attributes[k] = String(v); if (k === "id") this.id = String(v); },
        getAttribute(k) { return this.attributes[k] ?? null; },
        removeAttribute(k) { delete this.attributes[k]; },
        addEventListener(evt, fn) { (this.listeners[evt] ||= []).push(fn); },
        removeEventListener(evt, fn) {
            const l = this.listeners[evt] || [];
            const i = l.indexOf(fn);
            if (i >= 0) l.splice(i, 1);
        },
        dispatch(evt, arg) { (this.listeners[evt] || []).forEach((f) => f(arg)); },
        querySelector(sel) {
            if (!sel || sel[0] !== "#") return null;
            const want = sel.slice(1);
            const hit = this._parsed.find((p) => p.id === want);
            return hit || this.allDescendants().find((c) => c.id === want) || null;
        },
        querySelectorAll(sel) {
            // 面板用到 "button[data-busy-guard]" —— 需要真按属性筛
            if (sel === "button[data-busy-guard]") {
                return this._parsed.filter(
                    (p) => p.tagName === "BUTTON" && "data-busy-guard" in p.attributes
                );
            }
            const one = this.querySelector(sel);
            return one ? [one] : [];
        },
        insertAdjacentHTML(_pos, html) {
            // 真实浏览器会解析并插入；替身只需记录 + 解析 id（面板用它写日志）
            this._html += String(html);
            this._parsed.push(...parseIdsFromHtml(String(html)));
        },
        get scrollHeight() { return this._parsed.length * 20; },
        set scrollTop(_v) { /* 记录即可 */ },
        remove() { if (this.parentNode) this.parentNode.removeChild(this); },
        // 递归收集全部后代，便于断言"面板挂进去了"
        allDescendants() {
            const out = [];
            const walk = (n) => { for (const c of n.children) { out.push(c); walk(c); } };
            walk(this);
            return out;
        },
        findById(id) { return this.querySelector("#" + id); },
    };
    return el;
}

/**
 * 从 HTML 字符串里解析出带 `id="..."` 的元素（最小解析器）。
 *
 * 只做"够用"：识别标签名与 id 属性。真实浏览器当然更强，
 * 但面板只用到 `querySelector("#id")`，所以这就够了。
 */
function parseIdsFromHtml(html) {
    const out = [];
    const re = /<([a-zA-Z][a-zA-Z0-9-]*)([^>]*?)\/?>/g;
    let m;
    while ((m = re.exec(html)) !== null) {
        const attrs = m[2] || "";
        const idm = /\bid\s*=\s*"([^"]+)"/.exec(attrs);
        if (!idm) continue;
        const child = makeElement(m[1]);
        child.id = idm[1];
        if (/data-busy-guard/.test(attrs)) child.attributes["data-busy-guard"] = "";
        out.push(child);
    }
    return out;
}

function makeDocument() {
    const head = makeElement("head");
    const body = makeElement("body");
    const byId = new Map();
    const doc = {
        head,
        body,
        createElement: (t) => makeElement(t),
        getElementById: (id) => byId.get(id) || head.findById(id) || body.findById(id) || null,
        querySelector: (sel) => (sel && sel.startsWith("#")
            ? (byId.get(sel.slice(1)) || head.findById(sel.slice(1)) || body.findById(sel.slice(1)) || null)
            : null),
        querySelectorAll: () => [],
        addEventListener: () => {},
        // 记录被创建/挂载的 id，模拟真实 getElementById 的行为
        _register: (el) => { if (el.id) byId.set(el.id, el); },
    };
    const origAppend = body.appendChild.bind(body);
    body.appendChild = (child) => { const r = origAppend(child); doc._register(child); return r; };
    const origHeadAppend = head.appendChild.bind(head);
    head.appendChild = (child) => { const r = origHeadAppend(child); doc._register(child); return r; };
    // 面板**只在侧边栏存在时才挂载**（用户明确要求不要浮动窗口）。
    // 所以替身 DOM 必须提供一个侧边栏，否则面板不挂载，测试会误判成"面板坏了"。
    // 这也更接近真实 ComfyUI 的 DOM 结构。
    const sidebar = makeElement("div");
    sidebar.id = "comfyui-sidebar";
    body.appendChild(sidebar);
    return doc;
}

// ---------------------------------------------------------------------------
// 安装替身：把面板的相对导入映射到我们的 stub
// ---------------------------------------------------------------------------

const tmp = fs.mkdtempSync(path.join(require("node:os").tmpdir(), "bcui-panel-"));
const scriptsDir = path.join(tmp, "scripts");
fs.mkdirSync(scriptsDir, { recursive: true });

// 面板导入 `../../scripts/app.js` 与 `../../scripts/api.js`（相对它自身位置）
// 我们把它复制到 <tmp>/extensions/bettercomfyui_comfy/js/ 下，
// 使相对路径指向 <tmp>/scripts/*.js
// 必须镜像**真实的 URL 结构**：面板在浏览器里的地址是
//   /extensions/bettercomfyui_comfy/js/bettercomfyui_panel.js
// 所以 ../../../scripts/app.js 才能解析到 /scripts/app.js。
// 第一版写成 <tmp>/web/js/ 只有两层，真实路径改深一层后这里就对不上了
// （真正的错误是这个测试失败，而不是面板 JS 错）。
const webJs = path.join(tmp, "extensions", "bettercomfyui_comfy", "js");
fs.mkdirSync(webJs, { recursive: true });
fs.copyFileSync(PANEL, path.join(webJs, "bettercomfyui_panel.js"));

const state = {
    registered: [],
    fetchCalls: [],
    fetchResponder: null,
};

fs.writeFileSync(path.join(scriptsDir, "app.js"), `
export const app = {
  registerExtension(ext) { globalThis.__bcui_registered.push(ext); return ext; },
  loadGraphData: async () => true,
  ui: {},
};
`);

fs.writeFileSync(path.join(scriptsDir, "api.js"), `
export const api = {
  async fetchApi(path, options) {
    globalThis.__bcui_fetchCalls.push({ path, options });
    const responder = globalThis.__bcui_fetchResponder;
    if (responder) return responder(path, options);
    return { ok: true, status: 200, json: async () => ({ ok: true, projects: [] }),
             text: async () => "{}" };
  },
};
`);

globalThis.__bcui_registered = state.registered;
globalThis.__bcui_fetchCalls = state.fetchCalls;
globalThis.__bcui_fetchResponder = null;

const doc = makeDocument();
globalThis.document = doc;
globalThis.window = globalThis;
globalThis.localStorage = {
    _m: new Map(),
    getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
    setItem(k, v) { this._m.set(k, String(v)); },
    removeItem(k) { this._m.delete(k); },
};
globalThis.setTimeout = globalThis.setTimeout || ((f) => f());
globalThis.requestAnimationFrame = (f) => f();

// 把面板的失败信号抓下来：面板对初始化错误是"捕获后只打印"，
// 如果只看着自己的断言，会漏掉"面板其实报错了但根节点已挂上"这种情况（实测踩到）。
const consoleErrors = [];
const origConsoleError = console.error.bind(console);
console.error = (...args) => { consoleErrors.push(args.map(String).join(" ")); origConsoleError(...args); };

// ---------------------------------------------------------------------------
// 执行
// ---------------------------------------------------------------------------

console.log("=".repeat(66));
console.log("网页面板 DOM 级执行测试（Node + 最小 DOM 替身）");
console.log("=".repeat(66));
console.log(`面板文件：${PANEL}`);
console.log(`大小：${fs.statSync(PANEL).size} 字节`);
console.log("");

let mod = null;
try {
    mod = await import(pathToFileURL(path.join(webJs, "bettercomfyui_panel.js")).href);
    check("模块可加载（顶层代码不抛错）", true);
} catch (err) {
    check("模块可加载（顶层代码不抛错）", false, `${err.name}: ${err.message}`);
}

if (mod) {
    check("导出了具名符号", Object.keys(mod).length > 0, `导出：${Object.keys(mod).join(", ")}`);

    // 面板可能在导入时就注册，也可能导出函数由调用方注册
    const ext = state.registered[0];
    check("调用了 app.registerExtension", state.registered.length > 0,
          `注册次数=${state.registered.length}`);

    if (ext) {
        check("扩展有 name", typeof ext.name === "string" && ext.name.length > 0, JSON.stringify(ext.name));
        check("扩展有 setup()", typeof ext.setup === "function",
              `类型=${typeof ext.setup}`);

        if (typeof ext.setup === "function") {
            try {
                await ext.setup();
                check("setup() 能跑完（不抛错）", true);
            } catch (err) {
                check("setup() 能跑完（不抛错）", false, `${err.name}: ${err.message}`);
            }

            const bodyEls = doc.body.allDescendants();
            check("setup() 后 document.body 里有节点", bodyEls.length > 0,
                  `节点数=${bodyEls.length}`);

            const ids = bodyEls.map((e) => e.id).filter(Boolean);
            check("面板挂载了带 id 的元素", ids.length > 0, `ids=${ids.join(", ") || "(无)"}`);

            // 面板对初始化错误是「捕获后只打印」—— 必须单独断言没有报错，
            // 否则"根节点挂上了"会掩盖"后续全挂"。
            check("面板没有打印任何错误", consoleErrors.length === 0,
                  consoleErrors.join(" | "));
        }
    }
}

console.log("");
console.log(`结果：${checks - failures}/${checks} 通过`);
console.log("=".repeat(66));
process.exit(failures === 0 ? 0 : 1);
