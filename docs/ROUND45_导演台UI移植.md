# ROUND45 导演台 UI 移植 —— 单节点 `BetterComfyUIOneStop` 素材库 / 时间轴 / 22 帧连续可视化

> 时间：2026-10-18（Asia/Shanghai）｜执行：品牌/UI/多语言智能体（ui-agent）
> 用户升级要求：*「单节点 UI 过于生硬、没有原先导演节点 UI 好、功能不全，22 帧连续没了」* →
> **移植原 MiniMaxH3 Director 的 UI 体验，不是 LiteGraph 长表单**；素材库、时间轴、22 帧段间连续必须可视化且可交互。
> 结论先说：**已在本地 8188 用真实浏览器（Playwright + Edge headless）验证 30/30 + 旧工作流兼容 7/7 通过，截图见 `docs/remote_evidence_4090/ui_port/`；节点类型 ID 不变，旧工作流照常加载。远端 4090 由 Lead 统一部署（本文第六节给步骤），本智能体未重启远端/未提交 GPU 队列。**

---

## 一、改了哪些文件

| 文件 | 类型 | 说明 |
|---|---|---|
| `bettercomfyui_comfy/web/js/dramacore_director_ui.js` | **新增 ~1.1k 行** | 单节点导演台 UI：DOM widget 全自绘（素材卡 / canvas 时间轴 / 22 帧连续性专区 / 分段编辑 / 状态徽标），直接读写节点已有 widget，**无执行逻辑改动** |
| `bettercomfyui_comfy/web/js/bettercomfyui_panel.js` | 改 2 行 | setup 里加 `await import("./dramacore_director_ui.js")` 兜底（即使 ComfyUI `/extensions` 列表被缓存，新 UI 也能加载；失败只 console.info，不影响面板） |
| `docs/remote_evidence_4090/ui_port/*.png` | 新增 9 张截图 | 真实浏览器截图（见第四节） |
| `docs/remote_evidence_4090/ui_port/验证结果.json` | 新增 | 30 项 UI 验证结果明细 |
| `docs/remote_evidence_4090/ui_port/旧工作流兼容验证.json` | 新增 | 7 项旧工作流兼容结果 |
| `docs/ROUND45_导演台UI移植.md` | 新增 | 本报告 |
| `.comfyui/custom_nodes/bettercomfyui_comfy/web/js/*`（本地运行实例） | 同步 2 个 JS | 仅复制静态 JS 供本地浏览器验证，**未重启 ComfyUI**、未改远端任何东西；远端由 Lead 部署 |

**未改**：`vendor/`、`bettercomfyui/`、`one_stop_node.py`（UI 全靠前端，节点 91 个原生参数原样保留）、`config/*.yaml`。

> 说明：原导演台 UI 的源码（`ComfyUI_MiniMaxH3_Director/web/js/minimax_timeline.js` 647KB 等）**没有直接拷贝**（其 DOM 结构/timeline 编辑器与单节点 schema 不同，且 647KB 强耦合原节点的 5 个输出 widget 与 MMX_DIR_GROUP 组）。这里按同一套交互范式重写，但保留原导演台的 UI 概念：DOM widget 编辑器、canvas 时间轴、`CONT_JOINT`（段间连续性关节）、缩略图卡、连续性 frame choices `[5,22,39,56]`。

---

## 二、移植清单（逐项对照）

| # | 原导演台 UI 能力（来源） | 单节点移植后 | 状态 |
|---|---|---|---|
| 1 | **DOM widget 编辑器**（`minimax_timeline.js:13524 addDOMWidget`） | `addDOMWidget("dc_director_ui")`，节点上渲染整块自绘 UI（1180px 宽，约 1.9k px 高） | ✅ 移植 |
| 2 | **canvas 时间轴**（RULER / TRACK / 缩略图 / 分段块） | canvas 自绘：总时长/总帧、S1..Sn 分段块（时长+帧数+提示词摘要+参考图徽标）、音频轨（含波形示意） | ✅ 移植（简化：未做拖拽改时长） |
| 3 | **段间连续性关节 `CONT_JOINT`（22f）** | 每个关节画 `22f ⇄` 彩色徽标 + 连接线；关闭主开关或「每段引用上段」= 硬切（红色虚线样式）；顶部「22 帧段间连续」专区常驻 | ✅ 移植 + 强化（用户点名，做成最显眼区块） |
| 4 | **连续性档位 5/22/39/56 + 默认 22**（`CONTINUITY_FRAME_CHOICES`） | 4 个档位 chip，点击即写回 `连续帧数` widget；22 高亮并联动音频上下文 24 | ✅ 移植 |
| 5 | **音频上下文 24 帧**（原 `audio=24`） | 「音频上下文 24 帧」chip + 「音频同步 24」按钮，实时显示；非 24 时变黄告警 | ✅ 移植 |
| 6 | **每段 pin / 首段不 pin / continuityFromPrev** | pin 条：S1 标「首段：不 pin」，S2.. 标「pin 上段 ✓」；分段卡显示 `pin 上段 22f` | ✅ 移植 |
| 7 | **素材库 / 参考图槽位 UI**（原导演台素材卡） | ①首帧图 ②尾帧图 ③参考视频 ④参考音频 四张素材卡：图片预览（走 ComfyUI `/view`）、路径直接编辑、已指定/LOCK 徽标；下接素材生成/审核参数卡（生成个数、审核轮数/模式、复用 LOCKED、图像模型/分辨率/步数/CFG） | ✅ 移植（预览限 input/output/temp 可服务路径，见第五节） |
| 8 | **多段卡片 + 每段提示词/时长编辑** | 分段卡列表：S#、时长输入、提示词 textarea、参考图/视频/音频数量、pin 状态、上移/下移/删除、「+ 添加分段」 | ✅ 移植 |
| 9 | **timeline JSON 双向同步** | JSON→UI：自动解析时间轴JSON 渲染；UI→JSON：显式「写入时间轴JSON（高级覆盖）」；「清空」回自动构造。**不自动写 JSON**，避免把节点的自动 timeline 构造变成用户覆盖、丢素材自动注入 | ✅ 移植（有意做成显式） |
| 10 | **任务类型 / 编辑模式** | 下拉/任务类型显示在头部 chip 与时间轴参数行 | ✅ 移植 |
| 11 | **全局提示 / 系统提示词** | ⑤ 提示词区：系统提示词选择、自定义系统提示词、统一风格（全局提示）、输出语言、分镜粒度 | ✅ 移植 |
| 12 | **Refine / SelfLift / FaceRefine / SemanticBridge 面板** | ⑤ 增强区：4 个开关+全部子参数（步数/比例/次数/像素/放大方式/检测器/alpha…） | ✅ 移植 |
| 13 | **显存策略 / 缓存 / 音频模式** | ④ 生成区：段间/二采/修脸清理开关、缓存编码/目录、音频模式（generate/source/mute，静音时音频轨变红） | ✅ 移植 |
| 14 | **输出/剪辑配置** | ④ 生成区：Finalize、CRF、转场/字幕/水印/封面/格式（未实现项在节点内如实记录） | ✅ 移植 |
| 15 | **运行状态 / 审核结论徽标** | ⑥ 状态区 + 头部 chip：从 `onExecuted` 输出里提取「素材结论 / 段间连续实证 / 审核 / 成片 / failed」等 → 彩色徽标 + 日志；顶部另有 22f 连续 ON/OFF 实时 chip | ✅ 移植（比原导演台更贴合本节点输出格式） |
| 16 | **原生长表单** | 默认全部收起（不删除）：⑦「高级」可一键显示/隐藏原生 91 个参数表单 | ✅ 保留（隐藏不删参数，序列化不变） |
| 17 | 原导演台：缩略图懒加载 / 拖动裁剪 / 播放器 / 分组输入端口 | 未移植 | ⚠️ 见第五节原因 |

---

## 三、22 帧连续：移植后的可视化长什么样（用户点名项）

顶部第一个区块就是「① 22 帧段间连续」（绿框高亮，不是长表单里的一个 checkbox）：

- 主开关 + 档位 chips `5 / 22 / 39 / 56`（22 高亮为官方基线）+ `音频上下文 24 帧` + `音频同步 24`；
- 连续模式（guide / continue）+ 重绘幅度（continue 才激活）+ 「每段 pin 上段」+「保完整尾部」；
- **pin 可视化条**：S1 ⇄ S2 ⇄ S3 ⇄ S4，关节徽标 `22f ⇄ / audio 24f`；关闭主开关 → 关节变「硬切 / 无重叠」；首段标「首段：不 pin」；
- 摘要行：`段间连续：ON ｜ 帧档位 22（官方基线）｜ 模式 guide ｜ 音频上下文 24 帧 ｜ 保完整尾部 开 ｜ pin 规则 每段引用上段（首段除外）`；
- canvas 时间轴里每个关节同样画 `22f⇄` 徽标；分段卡显示 `pin 上段 22f`；
- 头部实时 chip：`22f 连续 ON · 22f` / `段间连续 OFF`。

对应截图：`02_22帧段间连续可视化.png`、`04_canvas时间轴与分段.png`、`01_单节点导演台UI全貌_默认折叠.png`。

---

## 四、截图（真实浏览器、本地 8188、1:1 像素）

目录：`docs/remote_evidence_4090/ui_port/`

| 文件 | 内容 |
|---|---|
| `01_单节点导演台UI全貌_默认折叠.png` | 节点 UI 全貌（1176×1910）：①22帧连续 + ②素材库 + ③时间轴/分段 + ④⑤⑥⑦折叠区 |
| `02_22帧段间连续可视化.png` | 22 帧专区特写：档位 chips / 音频 24 / S1-S4 pin 条 |
| `03_素材库UI.png` | 素材库：首帧真实图片预览（已指定徽标）、4 张素材卡、生成/审核参数 |
| `04_canvas时间轴与分段.png` | canvas 时间轴（S1-S3 + 22f 关节 + 音频轨）与可编辑分段卡 |
| `05_全功能UI_全部展开.png` | 4 个折叠区全部展开后的完整参数（证明"参数一个不少"） |
| `06_生成采样输出参数.png` | 生成/采样/输出参数特写（③ 折叠区展开） |
| `08_旧工作流加载后的单节点UI.png` | 加载插件自带旧工作流后，节点仍挂载新 UI |
| `00_画布中的单节点.png` | 画布全景 |
| `验证结果.json` | 30 项 UI 验证明细（控件值断言、预览图片加载、JSON 双向、无 console error） |
| `旧工作流兼容验证.json` | 7 项兼容明细（加载/UI挂载/graphToPrompt/入参无污染） |

---

## 五、验证（真实执行，非声明）

环境：本地 ComfyUI 8188（`--cpu --listen 127.0.0.1 --port 8188`，**未重启**）、Python 3.11 + Playwright + Edge headless。

| # | 验证 | 结果 |
|---|---|---|
| 1 | `node --check bettercomfyui_comfy/web/js/dramacore_director_ui.js` | ✅ 语法通过 |
| 2 | `node --check bettercomfyui_comfy/web/js/bettercomfyui_panel.js` | ✅ 语法通过 |
| 3 | `node tests/js/panel_dom_test.mjs`（项目自带面板 DOM 测试） | ✅ 9/9（面板动态 import 失败被 catch，无 console.error） |
| 4 | Playwright UI 验证（新建节点 + 真实点击 + 断言） | ✅ **30/30**（见 `验证结果.json`） |
| 5 | Playwright 旧工作流兼容（`workflows/00_【单节点】剧本→成片（无外部依赖）.json`） | ✅ **7/7**（见 `旧工作流兼容验证.json`） |

30/30 关键断言（摘要）：

- 节点挂载 `dc_director_ui` DOM widget、UI 7 个分区、DOM widget 布局宽 1176px；
- 22 帧档位 chips = `[5,22,39,56]`；点「39帧」→ `连续帧数` widget=39、关节徽标同步 `39f ⇄`、高亮切换；点回 22 → 恢复；
- 段数=4（通过 UI 输入框操作）→ 4 张分段卡、3 个 22f 关节、pin 条 4 个分段盒；
- 关闭「段间连续」→ 3 个关节全部变硬切；再打开恢复；
- 素材库设真实 output 图片路径 → `<img>` 出现且 `naturalWidth>0`（预览真实加载）、卡片显示「已指定」；
- 「写入时间轴JSON」→ 4 段合法 JSON、`output.continuityOverlapFrames=22`、`totalFrames=496`；「清空」→ 控件为空回自动构造；
- 91 个原生参数默认全部隐藏但保留（`widgets_values` 91 项不变），DOM widget `serialize:false`，执行入参无 DOM widget / 无多余字段；
- 浏览器控制台无 error（环境性 404：`user.css`/`userdata`/模板列表已单独核对，与本改动无关）。

旧工作流兼容 7/7：加载 → 找到节点 → 挂载新 UI → 7 分区 → 22 帧档位在 → `graphToPrompt()` 成功（该节点 inputs 90 项）→ inputs 中无 `dc_director_ui`/`dcdu` 污染 → 无 pageerror。

---

## 六、部署 / 验证步骤

**本地（已做，无需重启）**：新 UI 是纯静态 JS（`web/js/dramacore_director_ui.js`）+ 面板一行动态 import；ComfyUI `/extensions` 会按目录实时列出新文件，本地已复制到 `.comfyui/custom_nodes/bettercomfyui_comfy/web/js/` 并直接有效（浏览器 Ctrl+F5 即可）。

**远端 4090（由 Lead 统一执行，本智能体未操作远端）**：

```powershell
# 1) 本地打包（沿用 ROUND36/ROUND42 的源码包流程）
cd '[PROJECT-ROOT]'
python -B scripts/prepare_remote_bundle.py
scp -P 8188 dist\bettercomfyui_bundle_*.zip user@your-gpu-host:/data/
```

```bash
# 2) 远端：只替换插件源码（含 web/js 新文件），不碰模型/工作流/用户配置
ssh -p PORT user@your-gpu-host
cd /data && unzip -o bettercomfyui_bundle_*.zip -d BetterComfyUI
cp -a /model/ComfyUI/custom_nodes/bettercomfyui_comfy/web/js/dramacore_director_ui.js \
      /model/ComfyUI/custom_nodes/bettercomfyui_comfy/web/js/
cp -a /data/BetterComfyUI/bettercomfyui_comfy/web/js/bettercomfyui_panel.js \
      /model/ComfyUI/custom_nodes/bettercomfyui_comfy/web/js/
# 纯前端改动：无需重启 ComfyUI；浏览器 Ctrl+F5 刷新即可（若该机 ComfyUI 缓存了扩展列表，重启一次更稳）
```

**部署后验证（浏览器，Ctrl+F5）**：

1. 新建/打开 `BetterComfyUIOneStop` 节点 → 出现剧核导演台 UI（标题「剧核 · DramaCore」，7 个分区），无报错；
2. 顶部「① 22 帧段间连续」：档位 22 高亮、音频上下文 24、pin 条显示 `22f ⇄`；点 39/5/56 会改 `连续帧数`；关主开关关节变硬切；
3. 段数改 4 → 时间轴/分段卡变 4 段、3 个关节；「写入时间轴JSON」后高级区 JSON 非空，点「清空」回自动；
4. 素材库给首帧填 `/model/ComfyUI/output/xxx.png`（output 目录图片）→ 卡片出现预览图；
5. 打开一个旧工作流（含该节点）→ 节点正常加载且 UI 在；点 Queue 前 `graphToPrompt` 正常（本地已用 Playwright 验证）。

---

## 七、未移植项与原因（如实）

| 未移植 | 原因 |
|---|---|
| 原导演台的时间轴**拖拽改时长/拖动裁剪/逐帧缩略图懒加载/播放器** | 原 647KB 编辑器围绕其自身 timeline 编辑器与真实素材目录设计，移植需要按本节点 schema 重写数据层，工作量 > 本轮 60 分钟预算；当前改为「数字输入改时长 + 显式写 JSON」，功能等价但交互简化 |
| 原导演台的 **DB/批次/分组/多节点协同编辑** | 单节点是「一个节点搞定全流程」定位，没有独立的分组节点；这部分能力在单节点上等价为"段数/每段参数"，已在时间轴区体现 |
| 素材库真实**项目资产缩略图**（运行时生成的角色/场景/物品图） | 预览依赖服务端静态服务：ComfyUI `/view` 只服务 `input/output/temp`，项目素材目录不在此列；本轮未改后端路由/不重启服务，故非 output 路径显示占位。**运行时 LOCK 状态**会在点 Queue 跑完后由执行输出回填到⑥状态区（已验证解析逻辑）。若需真图预览，下一步可加一个只读 `/bettercomfyui/asset_preview` 路由（需重启一次） |
| 原导演台的 **i18n 文案（英文完整版）** | 本 UI 文案以中文为主（含关键英文标签）；完整中英双语留给现有 `dramacore_i18n.js` 体系下一步扩展 |

## 八、风险与注意

1. **纯前端改动，不改执行逻辑**：所有 UI 只是读写节点原生 widget；`widgets_values` 数量/顺序不变（91 项），旧工作流加载与 `graphToPrompt` 均已实测通过。
2. DOM widget 以 `serialize:false` 添加，不进执行入参（已断言 inputs 无污染）。
3. 「时间轴JSON」**不会**被 UI 自动写入（只有点按钮才写）——防止用户只是看看 UI 就把节点的「自动构造 timeline + 素材自动注入」变成用户 JSON 覆盖（这是节点已有行为，UI 刻意尊重）。
4. 隐藏原生控件只改 `computeSize`，不改值/顺序；⑦高级区可一键恢复原生表单。
5. 本地 `.comfyui/output/dc_ui_demo.png` 是验证用示例图（复制自 docs/宣传片封面），不属于产品代码；远端无需该文件。
6. 本智能体**未重启**本地/远端 ComfyUI，**未提交 GPU 队列**；远端部署与实机复验由 Lead 执行。
