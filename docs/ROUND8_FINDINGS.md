# 第八轮进展记录（2026-10-17 凌晨）

> 目标轮次 6/60。本轮把「网页面板在浏览器里的渲染未验证」从**只能看不能验**
> 推到**DOM 级真实执行验证**，并在过程中确认了两件事：
> 一条真实的 harness 缺陷，以及面板本身是干净的。

---

## 一、原来的"验证"其实什么都没证明

此前关于面板我只做了三条检查：

| 检查 | 实际证明了什么 |
|---|---|
| `node --check` 能解析 | 只证明**语法**对，不证明逻辑能跑 |
| `/bettercomfyui/*` 路由返回 200 | 只证明**后端**在，不证明前端会调用 |
| 节点已注册 | **与面板无关** |

真正会出的问题是：模块在 `registerExtension` 之前抛错、
引用了不存在的 DOM API、或**初始化时报错但被自己的 try/catch 吞掉**
（只剩一行 `console.error`）。这些只有**真正执行一遍**才会暴露。

本机没有任何 headless 浏览器（playwright / selenium / pyppeteer 都没装），
但面板用到的 DOM API 很少（已用 grep 逐个数过），所以可以自己写替身。

---

## 二、做法：最小 DOM 替身 + Node ESM 真实执行

`tests/js/panel_dom_test.mjs`：

1. 把面板复制到临时目录，使它的相对导入
   （`../../scripts/app.js`、`../../scripts/api.js`）指向我们写的 stub；
2. 用最小替身提供 `document` / `window` / `localStorage`，以及
   `createElement / getElementById / querySelector / querySelectorAll /
    appendChild / addEventListener / insertAdjacentHTML / style /
    textContent / innerHTML / scrollHeight / scrollTop`；
3. **真的 `import()` 面板模块**，然后执行 `setup()`；
4. 断言 9 项，包括 **「面板没有打印任何错误」**。

### 实测输出

```
[OK]   模块可加载（顶层代码不抛错）
[OK]   导出了具名符号
[OK]   调用了 app.registerExtension
[OK]   扩展有 name
[OK]   扩展有 setup()
[OK]   setup() 能跑完（不抛错）
[OK]   setup() 后 document.body 里有节点
[OK]   面板挂载了带 id 的元素
[OK]   面板没有打印任何错误
结果：9/9 通过
```

---

## 三、过程中踩到两个坑，都值得记

### 坑 1：`new URL(...).pathname` 是**百分号编码**的

第一版用 `new URL(import.meta.url).pathname` 求仓库路径，
结果中文与空格被编码成 `%E6%96%87%E4%BB%B6`，文件当然找不到。

**修法**：用 `fileURLToPath(import.meta.url)`。
这个坑在 Windows + 中文路径下必踩。

### 坑 2（更重要）：替身的 `innerHTML` 不解析，会掩盖真 bug

面板的真实写法是：

```js
root.innerHTML = `<button id="bcu-doctor">体检</button> ...`;
root.querySelector("#bcu-doctor").addEventListener("click", ...);
```

我的替身最初 `set innerHTML(v) { this._html = v; }` —— **存下字符串但不解析**，
于是 `querySelector` 永远返回 null，面板在绑定事件时就崩。

危险的是：**这时我分不清"面板有 bug"和"替身不够真"**。
第一轮跑出来 6/8，我一度以为是面板的问题。

**修法**：替身真的解析 `innerHTML` 里的 `id="..."`（一个 20 行的最小解析器）。
修好之后 9/9 —— 说明面板本身是干净的，之前的失败是替身的问题。

### 顺带发现：原来那 8 项断言**不足以**发现"面板其实报错了"

修好解析后 8/8 通过，但 stderr 里仍有一行：

```
[Better ComfyUI] 面板初始化失败（不影响 ComfyUI 其他功能）:
TypeError: this.logEl.insertAdjacentHTML is not a function
```

也就是说：**根节点挂上了、8 项全绿，但面板后续功能全挂**。
因为面板对初始化错误是"捕获后只打印"，我的断言看不见。

**修法**：补 `insertAdjacentHTML` 到替身，并**单独加一条断言**
「面板没有打印任何错误」（把 `console.error` 抓下来检查）。

这条是整轮最有价值的修正：**"部分成功"最容易被误判为"成功"**。

---

## 四、接进 pytest + 执行器自检

`tests/test_panel_dom.py`（4 条）：

* 脚本存在；
* 面板在替身 DOM 里跑起来且**不打印任何错误**；
* **执行器自检**：断言脚本至少跑了 8 项检查、且包含三个关键断言 ——
  防止脚本退化成"什么都不做也返回 0"（那样测试就成了摆设）；
* 静态兜底：面板用到的 DOM API 都在、且**没有内联 `onclick`**
  （会被 ComfyUI 的 CSP 拦掉）。

无 Node 时 **skip 并标注「未验证」**，不假装跑过。

---

## 五、与线上服务器交叉确认

```
GET /extensions/bettercomfy_comfy/js/bettercomfyui_panel.js
  -> HTTP 200, 14381 bytes, 含 registerExtension
```

14381 字节与 DOM 测试读取的文件**大小完全一致** ——
即「被执行的」和「被服务的」是同一份文件，不是两个副本。

---

## 六、这条验证的**边界**（不夸大）

✅ 已证明：面板在真实 DOM 语义下能加载、注册扩展、执行 `setup()`、
绑定事件、把节点挂进 `body`，且**不报任何错误**；服务器确实在提供这份文件。

❌ **仍未证明**：像素级渲染、CSS 生效、真实浏览器的事件冒泡与布局。
那需要真实浏览器（本机没有）。所以文档里把这一项从
"完全未验证"改为"**DOM 级已验证 / 浏览器渲染级未验证**"，不含糊。

---

## 七、验证汇总

| 项 | 结果 |
|---|---|
| 面板 DOM 级执行 | ✅ **9/9**，零错误 |
| 面板 pytest 封装 | ✅ 4/4（含执行器自检） |
| 全量测试（系统 Python） | **EXIT=0** |
| 全量测试（ComfyUI venv） | **EXIT=0** |
| 独立验收 | 92 通过 / 0 失败 / 0 未验证 |
| 面板 JS 被服务 | ✅ HTTP 200，14381 字节（与执行的是同一份） |
| 权重下载 | 🔄 **30.01 / 59.38 GB** |

---

## 八、仍未验证

1. **真实 H3 视频出片** —— 需 ≥24GB 显存（本机 4GB）。
2. **浏览器渲染级（像素/CSS/布局）** —— 本机无 headless 浏览器；
   DOM 级已过，但两者不等价。
3. 参考音色链路与导演台高级模式（二采/Guide/SelfLift）接入编排层。

---

_本轮所有 ✅ 均来自实际执行的命令输出；两个替身缺陷是我自己写的，如实记录。_
