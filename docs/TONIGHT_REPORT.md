# 今晚成果报告（TONIGHT REPORT）

> 生成时间：2026-10-17 凌晨（Asia/Shanghai）。
> 本报告只写**实际验证过**的结论；未验证的一律标注「未验证」。
> 环境：[INSTALL.md](INSTALL.md)｜契约：[CONTRACTS.md](CONTRACTS.md)｜总状态：[STATUS.md](STATUS.md)

---

## 一、一句话结论

**ComfyUI 0.41.0 已装好并正在运行，插件已装进 custom_nodes 并实测加载成功（6 个节点 + 网页面板 + 5 个后端路由），一套 3 节点最简工作流已就绪。**

```
网页地址： http://127.0.0.1:8188
节点搜索： 在节点搜索框输入 "Better ComfyUI"
```

**但真实视频生成仍未验证** —— 本机 GPU 是 GTX 1050 Ti，且**没有任何模型权重**，
因此 `real_generation=False` 是如实结论，不是还没做。

---

## 二、环境做了什么（从「旧到不能跑」到「可运行」）

你的机器原来装的 ComfyUI 相关组件很旧，且**根本起不来**。实测原因：

| 原来的问题 | 实测证据 |
|---|---|
| 没有 Python 环境 | `.comfyenv` 不存在；Desktop 版 app 装到了别处 |
| 缺全部核心依赖 | `torch` / `aiohttp` / `sqlalchemy` / `frontend-package` 全部 `ModuleNotFoundError` |
| Desktop 版配置指向不存在的盘 | `basePath = G:\...\客户\短剧\comfyui`，**G: 盘不存在** |
| 版本过旧 | 旧副本是 **0.22.3**，低于 MiniMaxH3 导演台要求的 `>=0.30` |
| `git clone` 超时 | `Failed to connect to github.com port 443`（但 HTTPS 下载 zip 正常） |

**今晚的处理**（全部可重复执行，见 `scripts/setup_comfyui.py`）：

| 项 | 结果 |
|---|---|
| 虚拟环境 | `<仓库>/.comfyenv`（Python 3.11.9） |
| torch | **2.14.1+cpu**（CPU 版；CUDA 版需要 NVIDIA 卡，本机 1050 Ti 4GB 不适合） |
| ComfyUI | **0.41.0**（官方 master zip，不是旧副本） |
| 依赖 | 73 个包，`requirements.txt` 全部装好 |
| 前端 | `comfyui-frontend-package 1.55.17`（**网页界面本体，确认可用**） |
| 额外 | `pyyaml`、`imageio-ffmpeg`（编排层拼接/封装依赖）、`pytest` |

> 说明：**为什么是 CPU 版**：本机 GTX 1050 Ti 只有 4GB 显存且无权重，
> 装 CUDA 版 torch（约 2.5GB+）对「让网页和插件跑起来」没有帮助，反而更慢。
> 目标机（≥24GB 显存）请用 `python scripts/setup_comfyui.py --cuda`。

---

## 三、插件做了什么

### 3.1 目录结构

```
bettercomfyui_comfy/                 ← ComfyUI 自定义节点包（复制进 custom_nodes 即可）
  __init__.py                         ComfyUI 入口：NODE_CLASS_MAPPINGS / WEB_DIRECTORY
  _bridge.py                          定位并加载 bettercomfyui 编排包（带可操作报错）
  nodes.py                            6 个节点
  routes.py                           网页面板用的 5 个 HTTP 路由
  phase2.py                           第二阶段接入点（**保留但不在 UI 展示**）
  web/js/bettercomfyui_panel.js       网页面板（ComfyUI 前端扩展）
  workflows/minimal_workflow.json     最简工作流（3 节点）
  workflows/validate_workflow.py      工作流离线校验器
```

### 3.2 六个节点（显示在 ComfyUI 网页里）

| 节点 | 显示名 | 作用 |
|---|---|---|
| `BetterComfyUIProject` | Better ComfyUI 项目 | 打开/创建/列出/删除项目（删除需二次确认） |
| `BetterComfyUIRun` | Better ComfyUI 运行 | **一键跑完整流程**：剧本→素材→视频段→审核→拼接→成片 |
| `BetterComfyUIAssetReview` | Better ComfyUI 素材审核 | 人工通过/拒绝/切模型模式（LOCK 绑定具体版本） |
| `BetterComfyUIManualAsset` | Better ComfyUI 手动素材 | **手动新增（不审核，标记 MANUAL_UNREVIEWED）** / **手动替换（走审核，旧版本保留）** |
| `BetterComfyUIResult` | Better ComfyUI 结果 | 展示运行结果（只读） |
| `BetterComfyUIDoctor` | Better ComfyUI 体检 | 环境体检，装好后第一件事就跑它 |

### 3.3 网页面板

`web/js/bettercomfyui_panel.js` 会在 ComfyUI 网页里挂一个面板（优先挂侧边栏，找不到就退化为右上角浮动面板），提供：
**体检 / 刷新项目 / 载入最简工作流 / 打开或创建项目 / 选执行器 / 填剧本 / 开始运行 / 日志区**。

面板只依赖 ComfyUI 自带 `app`/`api`，不引第三方库；初始化失败只打 console 错误，**不会让 ComfyUI 白屏**。

### 3.4 最简工作流（3 个节点）

```
BetterComfyUI 项目  ──project_id──▶  BetterComfyUI 运行  ──report_json──▶  BetterComfyUI 结果
```

两种用法：面板点「载入最简工作流」，或把 `workflows/minimal_workflow.json` 直接拖进画布。

### 3.5 第二阶段接入点（按要求：保留但不上屏）

`phase2.py` 提供：
- `next_chapter_for_project()` —— **OUT6：从项目状态继续，不从成片输出继续**（方案第五部分第 35 项的明确裁定）
- `fetch_next_chapter()` / `publish_movie()` —— 剧本来源与发布平台两个**接缝**，未接入时**抛错**，绝不返回假成功
- `publish_movie()` 会**拒绝发布 `is_mock=True` 的成片**（对外发布占位产出等于声称虚假结果）

**UI 里完全看不到它**：`NODE_CLASS_MAPPINGS` 没有它们，`PHASE2_NODES_EXPOSED = False`，
面板里对应区块被 `ENABLE_PHASE2_UI = false` 关闭。想启用只需改这一个常量 + 注册两行节点。
后端 `/bettercomfyui/phase2/status` 路由保留（界面不调用）。

---

## 四、实测验证结果（这一节是重点）

### 4.1 实测通过的证据

| 验证项 | 方法 | 结果 |
|---|---|---|
| ComfyUI 能启动 | 真启动进程 | ✅ 0.41.0，`Device: cpu`，`Starting server` |
| 网页可访问 | `GET /` | ✅ 200，20089 字节（前端页面本体） |
| **插件被加载** | 启动日志 | ✅ `Better ComfyUI：插件 v0.1.0 加载完成，6 个节点` |
| **6 个节点真的注册** | `GET /object_info`（994 个节点类型） | ✅ 6/6 全部在列 |
| 面板路由可用 | 逐个请求 5 个路由 | ✅ 全部 200 |
| `/doctor` 真实内容 | 读 JSON | ✅ plugin=0.1.0 nodes=6 ffmpeg=True comfy=0.41.0 realgen=False |
| 最简工作流 | `GET /bettercomfyui/workflow/minimal` | ✅ Project → Run → Result |
| **节点真的能跑** | 在 ComfyUI venv 里真调 `BetterComfyUIRun().run()` | ✅ `ok=True`，2.6s，成片产出 |
| 全量测试 | `pytest tests/ -q` | ✅ **EXIT=0，0 失败** |
| 插件专项测试 | 4 个文件 | ✅ 全部通过（含 aiohttp 真调处理器） |
| 工作流校验 | `validate_workflow.py` | ✅ OK=35 WARN=0 FAIL=0 |

### 4.2 测试方法（你要求我告诉你「怎么测的」）

真实模型生成无法测试，**替代方法**如下 —— 每一项都说明它替代了什么、以及它的边界：

| # | 替代方法 | 替代了什么 | 边界（它证明不了什么） |
|---|---|---|---|
| **S1** | **占位媒体 + mock 执行器**：用 ffmpeg 合成纯色视频 + 正弦音轨，跑完整流水线 | 真实模型出图/出片 | 证明**编排逻辑**正确；**证明不了**画质、模型兼容性、显存占用 |
| **S2** | **产物用 ffmpeg 实测校验**（时长/编码/音轨），而不是「文件存在就算过」 | 人工目测成片 | 证明成片是**合法媒体**、时长与段数吻合；证明不了内容是否符合剧本 |
| **S3** | **确定性打分器**替代本地评分模型 | 真实质量审核 | 证明审核/计数/兜底流程正确；证明不了评分是否合理 |
| **S4** | **aiohttp 直接调用处理器**（不需要起服务器） | 手工点页面 | 证明每个路由返回结构与状态码正确；证明不了浏览器端渲染 |
| **S5** | **`node --check` 语法检查 + 复制成 `.mjs`** | 浏览器加载 JS | 证明前端文件能被 ESM 解析；**证明不了**它在真实 DOM 里的行为 |
| **S6** | **按 ComfyUI 的方式加载插件**（临时 `custom_nodes/` 目录 + 子进程） | 装进 ComfyUI | 证明导入契约正确；实测中它抓到了真 bug（见 4.4） |
| **S7** | **变异测试**：故意改坏实现，确认测试变红 | 「测试是不是白写的」 | 证明测试真的会失败 |

**未验证项（如实列出，不在本机条件下无法验证）**：
1. 真实视频/图像生成质量与速度（需模型权重 + 目标机）
2. MiniMaxH3 导演台系节点加载（需装节点 + ComfyUI ≥0.30，现在版本够了但**节点未装**）
3. 网页面板在浏览器里的实际渲染与交互（本机无法用浏览器验证，只能验证到 JS 可解析 + 路由可用）
4. 目标机（≥24GB 显存）的显存与性能表现
5. 24 小时长视频、多章节连续运行

### 4.3 测试期间**真实抓到并修复的 4 个 bug**

这几个都是「静态看没问题、真跑才暴露」的类型，值得记录：

| # | 缺陷 | 怎么发现的 | 影响 |
|---|---|---|---|
| 1 | `_pkgn()` 算错包名（`__package__` 再 rsplit 一级 → `custom_nodes`） | 按 ComfyUI 方式加载的测试（S6） | 装进 ComfyUI 后所有面板路由都会 `ModuleNotFoundError` |
| 2 | `h_doctor` 里裸 `__version__` → `NameError` | **真实 HTTP 请求**（起服务器后 `/doctor` 返回 500） | 用户打开面板第一个请求就报错 |
| 3 | `max_segments` 对内置剧本静默失效 | 真实调节点（传 2 却跑 3 段） | 用户「只想跑 2 段省时间」被静默忽略 |
| 4 | 开发态与运行态代码漂移（修好的 `routes.py` 没进 `custom_nodes`） | 新增 `scripts/sync_to_comfyui.py --check` | 「我明明修好了」却仍是旧行为 |

> 教训已写进代码注释与测试：**「能导入」不等于「能调用」**，
> 因此现在有专门的 `tests/test_comfy_routes.py` 真实调用每个处理器。

### 4.4 关键数字

| 指标 | 数值 |
|---|---|
| 全量测试 | **EXIT=0，0 失败**（909+ 用例） |
| 独立验收（verify） | 15/15 通过，92 通过 / 0 失败 / 0 未验证 |
| 插件专项测试 | 4 文件全绿（契约 / 加载 / 路由 / 控件） |
| 插件节点数 | 6 |
| ComfyUI 节点类型总数 | 994（插件加载前 988） |
| 最简工作流 | 3 节点，校验 OK=35 FAIL=0 |
| 端到端运行耗时 | **2.6s**（2 段）/ 3.4s（3 段） |

---

## 五、怎么用（最短路径）

```powershell
# 1) 启动（服务已在运行，如需重启）
cd "[PROJECT-ROOT]\.comfyui"
"..\.comfyenv\Scripts\python.exe" main.py --cpu --listen 127.0.0.1 --port 8188 --disable-auto-launch
```

2. 浏览器打开 **http://127.0.0.1:8188**
3. 右侧（或右上角）找到 **Better ComfyUI** 面板 → 点「**体检**」确认环境
4. 点「**载入最简工作流**」→ 画布出现 3 个节点
5. 在面板里填项目名 → 「打开或创建」→ 「开始运行」
6. 运行完在日志区/「结果」节点看成片路径

**从零重装**（换机器时）：`python scripts/setup_comfyui.py`（加 `--cuda` 用显卡）
**装进别的 ComfyUI**：把 `bettercomfyui_comfy/` 复制到它的 `custom_nodes/`，并确保能 `import bettercomfyui`
**改了代码后同步**：`python scripts/sync_to_comfyui.py`（加 `--check` 只比对）

---

## 六、需要你决定的事

1. **`git clone` 被墙**：`github.com:443` 连不上，但 **HTTPS 下载 zip 正常**。
   所以脚本用 zip 下载。如果你有代理，可以用 `--cuda` 那类流程装节点。
2. **模型权重**：本机 0 个权重。要有真实产出，需要放权重到 `.comfyui/models/`，
   并注意本机 4GB 显存基本跑不了视频模型 → 建议在目标机（≥24GB）部署。
3. **MiniMaxH3 导演台系节点**：ComfyUI 版本已够（0.41.0 ≥ 0.30），但**节点尚未安装**。
   按 `docs/AUDIT_NODES.md` 的 `RECOMMEND` 清单装（`ComfyUI-DNNodes`、`ComfyUI-JobNotify`）。

---

## 七、诚实声明

- 本报告所有 ✅ 都来自**本次实际执行的命令输出**，未验证项已单独列出（§4.2 末）。
- **成片是占位媒体，不是模型生成结果**。该事实写进了成片元数据 `generation_provenance`
  （`is_mock / executor / reason / unverified`），并有回归测试断言元数据文本必须出现「占位/未验证」字样。
  **请不要**为了界面好看把它改成 `is_mock: false` —— 那是任务书第 9 节末句明令禁止的行为。
- 网页面板**只在代码层面验证过可用**（JS 可解析 + 路由 200 + 节点注册），
  **浏览器里的实际渲染本机无法验证**，请你打开网页确认一眼。
