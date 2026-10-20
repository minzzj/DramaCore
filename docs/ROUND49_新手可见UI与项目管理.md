# ROUND49 新手可见 UI 与项目管理（web UI + 面板路由）

> 时间：2026-10-18（Asia/Shanghai）｜执行：ui-agent（品牌/UI/多语言/面板路由）
> 用户点名 P0：① 看不到明显的基本输入窗口 → 首屏要有巨大「粘贴剧本」输入框；② 审核结果展示不完善。
> 用户采纳点：③ 程序自动生成区加斜纹蒙版 + 手动接手按钮；④ 可输入项前置（①剧本→②项目→③API→④输出/段数/分辨率）；⑤ 项目选择 + 旧项目资产/提示词/成片上下文；⑥ 按钮重叠修复（ROUND48 方案 E）；⑦ 中英双语/旧工作流兼容/节点 ID 不变/DOM widget 不进 graphToPrompt。
> **结果：34/34 UI 验证 + 22/22 项目路由单测 + 面板 DOM 9/9 全过；本地 8188 真实浏览器截图见 `docs/remote_evidence_4090/ui_round49/`。没有改 core 执行逻辑；项目/审核数据只读展示。**

---

## 一、改了哪些文件

| 文件 | 类型 | 说明 |
|---|---|---|
| `bettercomfyui_comfy/routes.py` | 改（+3 只读路由） | `GET /bettercomfyui/projects/list`（扫描 `projects/*/project.json`：id/显示名/素材数/已LOCK数/提示词数/成片数/更新时间）；`GET /bettercomfyui/project/context?project_id=`（项目已有资产图+审核分数/状态、提示词清单、成片清单）；`GET /bettercomfyui/asset/preview?project_id=&rel=`（只读项目资产图，目录越界 403/非图片 415）。全部同时注册 `/api` 前缀；**只读，不写项目、不触发执行**。 |
| `bettercomfyui_comfy/web/js/dramacore_director_ui.js` | 改（ROUND45 UI 上扩展约 700 行） | 首屏剧本大输入框、下一步状态条、项目选择+上下文、API/核心参数前置、自动生成区斜纹蒙版+手动接手、审核结论汇总卡、窄窗防重叠、中英双语、按 director-agent 新契约解析 `ui.dramacore_report` |
| `docs/remote_evidence_4090/ui_round49/*.png` + `验证结果.json` + `项目路由单测输出.txt` + `补充验证_项目上下文回退.json` | 新增 | 真实浏览器截图与原始验证数据 |
| `docs/ROUND49_新手可见UI与项目管理.md` | 新增 | 本报告 |

前端数据契约（director-agent 已在 `one_stop_node.py` 提供，本智能体**只消费不修改**）：`ui["dramacore_report"] = report_obj`；解析优先级 `ui.dramacore_report` → `ui.report` → 审计文本降级。字段：`assets.assets[]`（score/avg_score/state/lock_status/fallback）、`assets.review_summary`（generated/locked/blocked/fallback/avg_score/threshold）、`video_review.segment_scores[]`/`full_review{final_score,passed}`、`auto_rework.rounds[]`、`prompts.segments[]`、`timeline_summary{segmentCount,totalFrames,frameRate,segment_seconds}`。

---

## 二、P0 修复

### P0-1 首屏「① 粘贴剧本」巨大输入框（已实现，截图 `01/02`）

- 节点**最顶部第一屏**（在状态条之后、项目/API 之前）放 2px 蓝色高亮大卡：
  - 标题「① 粘贴剧本（必填，整条链只调用 1 次 API）」+「从这里开始」标签；
  - 大 textarea（**布局高度 150px**，浏览器实测可点可输入），占位文案自带格式示例；
  - 按钮：**「填入示例剧本」**（一键填入 315 字古装悬疑示例）、「清空剧本」、「查看示例全文」；
  - 右下角**实时字数统计**「291 字 · 已填写 ✓」；
  - 直接双向绑定节点原生 `剧本` widget（不新增执行参数，旧工作流不变）。
- 顶部常驻「下一步」状态条（ROUND48 方案 B）：剧本为空 → 红条「下一步：① 粘贴剧本」；有剧本无项目 → 黄条提示选/建项目；无 API Key → 黄条 +「去填 API Key」按钮；全齐 → 绿条「就绪 ✓ 点 Queue 运行」。**不读文档也知道下一步点哪**。

### P0-2 审核结果展示完善（已实现，截图 `04`）

- **素材卡分数 + 状态**：项目上下文资产卡显示真实审核分数（如 `88 分`）+ 状态徽标 `LOCKED 已锁定 / BLOCKED 不通过 / FALLBACK 兜底 / CNT1 候选上限 / ALLOWED`，悬停可看审核理由；运行时（本次执行）素材卡同格式，并显示 `FALLBACK`/版本号（来自 `assets.assets[]`，兼容 `review_summary.states[]`）。
- **节点底部「审核结论」汇总区**（7 张指标卡，`待运行` → 跑完自动回填）：
  1. 素材审核：张数 + 平均分 + 各状态计数；
  2. 提示词生成 E1–E5：段数；
  3. 导演台分段：段数 · 总时长（`totalFrames/frameRate` 或 `segment_seconds×段数`）；
  4. 分段视频审核 R_seg：平均分 + 需返工段数；
  5. 自动返工轮次：轮数；
  6. 整片 R_full：分数 + 通过/不通过（`full_review.final_score/passed`，兼容旧 `full`/`quality`）；
  7. 素材库平均分：当前项目 LOCKED 素材评分均值。
- 数据来源优先级如上；字段缺失显示「待运行」，**不编造**；审计文本降级解析支持 director-agent 新增的「素材审核汇总 / 段级审核汇总 / 整片 R_full=」。
- 运行时素材卡数据优先级（按 director-agent 建议）：`report.project_context.locked_assets` → `assets.assets[]` → `assets.review_summary.states[]` → created/locked/blocked/rounds 计数行；补充验证见 `docs/remote_evidence_4090/ui_round49/补充验证_项目上下文回退.json`（locked_assets 2 张 → 卡片显示 91 分/LOCKED、76.5 分/LOCKED，审核结论同步渲染，4/4 通过）。

---

## 三、用户采纳点实现情况

| 采纳点 | 实现 | 证据 |
|---|---|---|
| ③ 蒙版（程序自动生成区） | 素材区 / 时间轴 / 生成采样 / 提示词增强审核 / 高级项 共 **5 处**斜纹半透明蒙版，蒙版文案「该内容将由程序根据剧本自动生成」+ 说明 + **「手动选择 / 手动填写」** 大按钮；点击即解除该区域蒙版、开放手动操作（本次会话生效） | `05_自动生成区斜纹蒙版.png`、`06_全功能UI_展开全部.png`；验证「点按钮后 mask 数 5→4」通过 |
| ④ 可输入项前置 | 顺序：**① 粘贴剧本 → ② 项目与素材上下文 → ③ API 配置（Key/模型/Base）→ ④ 核心输出（输出文件名/段数/每段时长/宽高/生成素材图/占位结果）**；22 帧连续（⑤）紧随其后；自动生成/高级项（⑥⑨⑩⑪⑫⑬）在后并加蒙版/折叠；原生参数仍保留在⑬ | `01` 全貌截图 |
| ⑤ 项目管理/上下文 | ② 区：项目下拉（40 个项目，含「素材 3·LOCK 3·提示词 3·成片 1」摘要）+ 刷新 + 新建项目（调既有 `POST /project/open`）；选中后自动加载：资产图（真实缩略图 `/asset/preview` + 分数/状态）、提示词清单、成片清单（只读）；选中项目会写入节点 `项目` widget → 该次运行继续用该项目素材库 | `03_②项目选择与旧资产上下文.png`；验证：40 个选项、3 资产卡、3 真实预览图、3 提示词、1 成片 |
| ⑥ 按钮重叠修复（方案 E） | 头部 chip/按钮行 `flex-wrap`；状态徽标独立安全区 `.dcdu-badgesafe`（`flex:1 1 260px` 右对齐）；窄窗 `ResizeObserver` 给 host 加 `.dcdu-narrow`（隐藏次要文字/收紧 padding）；按钮统一高度/圆角；**实测头部+状态条+项目栏可见控件两两重叠数 = 0** | `验证结果.json` overlap=0 |
| ⑦ 保持项 | 中英双语（`tr()` 跟随 localStorage/浏览器语言；实测 English 下标题/按钮/项目区为英文）；旧工作流兼容（DOM widget `serialize:false`，`graphToPrompt` 成功且 inputs 无 dcdu 污染）；节点类型 ID 未变；不改执行参数 | 34/34 验证含双语/兼容断言 |

---

## 四、验证（真实执行）

环境：本地 ComfyUI 8188（CPU，`--listen 127.0.0.1 --port 8188`，**本轮为加载新路由重启过一次本地实例**，未重启远端、未跑 GPU）、Playwright + Edge headless。

| # | 验证 | 结果 |
|---|---|---|
| 1 | `python -B -m py_compile bettercomfyui_comfy/routes.py` | ✅ |
| 2 | `node --check bettercomfyui_comfy/web/js/dramacore_director_ui.js` / `bettercomfyui_panel.js` | ✅ |
| 3 | `node tests/js/panel_dom_test.mjs`（项目自带面板 DOM 测试） | ✅ 9/9 |
| 4 | 项目路由单测（真实 aiohttp handler，写后不污染数据） | ✅ **22/22**（列表 39 项倒序、含 7 字段；上下文 assets=3/locked=3/prompts=3/films=1；非法/不存在 404；预览 200 image/png；路径越界 403；非图片 415；三路由均已注册）→ 原始输出 `项目路由单测输出.txt` |
| 5 | Playwright 真实浏览器 UI 验证（新建节点 + 真实点击 + 真实路由数据） | ✅ **34/34** → `验证结果.json` |
| 6 | 真实 HTTP 抽查（重启后） | `/bettercomfyui/projects/list` 200 count=39；`/project/context?project_id=e3ab…` assets=3 locked=3 prompts=3 films=1；`/asset/preview` 200 image/png 34553B；`..%2F..%2Fproject.json` → 403 |

**截图清单（`docs/remote_evidence_4090/ui_round49/`，1:1 像素）：**

| 文件 | 内容 |
|---|---|
| `00_画布中的新手首屏.png` | 画布全景（首屏剧本框 + 项目/API/核心参数 + 22帧连续 + 蒙版 + 审核结论） |
| `01_首屏剧本大输入框与下一步.png` | 节点 UI 全貌（中文，含示例剧本/项目已选/审核结论已回填） |
| `02_①剧本大输入框特写.png` | ① 剧本大输入框 + 字数 + 按钮 + 下一步条 |
| `03_②项目选择与旧资产上下文.png` | ② 项目下拉 + 汇总 chips + 旧项目资产卡（88 分/LOCKED/FALLBACK）+ 提示词/成片清单 |
| `04_审核结论汇总.png` | 7 张审核指标卡（3 张/5 段/3 段·15.5s/80.3 分/2 轮/84.6 分/88 分） |
| `05_自动生成区斜纹蒙版.png` | 素材区斜纹半透明蒙版 +「该内容将由程序根据剧本自动生成」+「手动选择/手动填写」按钮 |
| `06_全功能UI_展开全部.png` | 解除全部蒙版 + 展开全部折叠区后的完整参数 |

34/34 关键断言（摘要）：
- 首屏：hero 在第一屏（index 2，项目栏之前）、textarea 布局高 150px、占位示例文案在、下一步条在；清空剧本 → 下一步提示「剧本」；点「填入示例剧本」→ `剧本` widget=315 字、字数统计更新；
- 项目：下拉 40 项；选中 → 节点 `项目` widget=「端到端演示」；上下文 assets=3/prompts=3/films=1；3 张资产卡带 `88 分`+`LOCKED 已锁定`；3 张真实预览图加载；
- 蒙版：5 处；文案含「自动生成」；点「手动选择/手动填写」→ 5→4；
- 审核结论：运行前 7 卡全「待运行」；注入 director-agent 契约 JSON 后：3 张 / 5 段 / 3 段·15.5s / 80.3 分·需返工 1 段 / 2 轮 / 84.6 分·通过 / 88 分；运行时素材卡出现 `FALLBACK` 状态；
- 防重叠：头部/状态条/项目栏可见控件两两重叠数 0；
- 双语：English 下「Paste your script / Fill sample script / Project & asset context」；
- 兼容：`graphToPrompt` 成功，109 个 widget 中 DOM widget 未进 inputs。

---

## 五、部署步骤（**远端由 Lead 统一执行**；本智能体未操作远端）

本次改动包含 **前端静态 JS + 后端路由**，另外依赖 director-agent 的 `one_stop_node.py`（`ui.dramacore_report`）与 review 数据输出：

```powershell
# 1) 本地打包（沿用既有流程）
cd '[PROJECT-ROOT]'
python -B scripts/prepare_remote_bundle.py
scp -P 8188 dist\bettercomfyui_bundle_*.zip user@your-gpu-host:/data/
```

```bash
# 2) 远端覆盖源码（插件 3 个文件 + director-agent 的节点文件；不碰模型/项目数据）
cd /data && unzip -o bettercomfyui_bundle_*.zip -d BetterComfyUI
dst=/model/ComfyUI/custom_nodes/bettercomfyui_comfy
cp -a /data/BetterComfyUI/bettercomfyui_comfy/routes.py $dst/
cp -a /data/BetterComfyUI/bettercomfyui_comfy/one_stop_node.py $dst/          # director-agent 的契约（ui.dramacore_report）
cp -a /data/BetterComfyUI/bettercomfyui_comfy/review_nodes.py $dst/           # ROUND49 P1：review 节点 JSON 新增 index/scores 别名（评分逻辑零改动）
cp -a /data/BetterComfyUI/bettercomfyui_comfy/web/js/dramacore_director_ui.js $dst/web/js/
cp -a /data/BetterComfyUI/bettercomfyui_comfy/web/js/bettercomfyui_panel.js $dst/web/js/

# 3) 重启 ComfyUI（新增了后端路由，必须重启；沿用该机既有托管方式）
ps aux | grep -i '[m]ain.py'
# systemctl restart comfyui / supervisorctl restart comfyui / kill + 原参数重启
# 启动环境保留 BETTERCOMFYUI_HOME=/data/BetterComfyUI
```

```bash
# 4) 部署后验证
curl -s http://127.0.0.1:8188/bettercomfyui/projects/list | python -m json.tool | head -30
PID=$(curl -s http://127.0.0.1:8188/bettercomfyui/projects/list | python -c "import json,sys;d=json.load(sys.stdin);print(d['projects'][0]['project_id'])")
curl -s "http://127.0.0.1:8188/bettercomfyui/project/context?project_id=$PID" | python -m json.tool | head -30
# 浏览器 Ctrl+F5：
#  a) 新建 BetterComfyUIOneStop：第一屏应看到蓝色「① 粘贴剧本」大输入框 + 顶部下一步条；
#  b) ② 项目下拉应列出历史项目，选一个 → 资产图/提示词/成片清单出现（带分数+LOCKED）；
#  c) 点「填入示例剧本」→ 字数变化；下方 5 处斜纹蒙版点「手动选择/手动填写」可解除；
#  d) 跑完一次后底部「审核结论」7 张卡应显示数字（不是待运行）；若仍待运行，检查节点是否已带 ui.dramacore_report。
```

> 注意：远端如果还未重启，`/bettercomfyui/projects/list` 会 404，前端会在项目区显示明确提示（不会白屏）。

---

## 六、未做项 / 边界（如实）

1. **未做项目删除/重命名 UI**：列表+新建+选择已够当前流程；删除属写操作，避免误删项目数据，留给后续。
2. **成片清单只读展示**（文件名/大小/时间），未做网页播放器；`/asset/preview` 只服务图片（mp4 不代理），播放仍走 ComfyUI 原生输出预览。
3. **蒙版解除状态仅本次页面会话生效**（不写工作流、不落 localStorage）；刷新后自动生成区重新盖上蒙版。若要持久化，需加 localStorage 键，属后续小改。
4. **项目资产分数取自项目索引的 `versions[].extra.score`**；老项目（ROUND41 之前）无该字段时显示「分数 —」，属历史数据缺失，不是解析失败。
5. 本轮为加载新路由**重启了本地 8188 ComfyUI 一次**（CPU 实例，6 秒就绪；未重启远端、未跑 GPU）。当前机器上存在两个 `main.py` 进程（历史上就有），8188 由其中一个监听，未做清理以免影响其他智能体。
6. 部分验收截图里右侧任务队列为 ComfyUI 自带侧栏，已尽量隐藏；与 UI 功能无关。


---

## 远端 4090 真机验收与修复（2026-10-18 18:15 追加）

### 修复的 bug
1. **工作流控件错位（用户报错的根因）**：交付目录里是旧 95 控件工作流，当前节点 110 控件 → 导入后整体错位。
   已用当前前端重新生成（110 控件）并覆盖：UI 目录 + 交付目录 + 本地仓库；校验 ileWidgets=110 / frontendWidgets=110 / promptInputs=107 / bad=[]。
2. **剧本输入框不实时同步**：原只在 change 写回原生 widget；已改为 input 实时写回（打字即同步，点运行前一定生效）。
3. **面板已配 API Key 仍提示去填**：新增 /bettercomfyui/panel/config 读取，已配置时下一步直接显示「就绪」。
4. **续写时旧素材没挂成参考图**：生成素材图=False 时只做清单注入、挂参考图 0。
   已加「项目库复用」：按 E1–E5 名称匹配 LOCKED 素材，名称不匹配时用项目库其它 LOCKED 素材补足（上限 素材最多生成个数），审计逐条标注。

### 真机验证（新 UI + 真实 Key + 项目续写）
证据：docs/remote_evidence_4090/ui_round49_remote/（20/21/22/23/24 截图 + round49_ui.json + ui49e.log）
- UI 检查：首屏大剧本框可见（高 135px）、5 处蒙版、项目下拉 13 项（带素材/LOCK/成片数）、7 张审核指标卡；
- 用真实用户方式：点进大框输入剧本 → 实时同步到节点（第二章 52 字）→ 项目选「剧核ROUND49验证」→ 点 Run；
- **success 165.3s（真实 Key，API 1 次）**；素材结论：生成 0 / 复用LOCKED 3 / LOCKED 3 / 挂参考图 3；
  素材审核汇总：生成 0 / LOCK 3 / 状态 主角=LOCKED；配角=LOCKED；主场景=LOCKED；
  R_seg 87.7 通过（index=1、四维 scores 有值）、R_full 98.62 通过、完整性 True、显存 44576MB；
- 项目目录追加记录、旧文件保留；项目下拉数量/成片数增加。

### 仍存小问题
- 复用历史素材时素材卡分数显示「—」（旧版本数据没有 score 字段；新跑生成的素材有真实分数）；
- 窄窗按钮重叠计数仍有 1 处（轻微，下一轮修）；
- 旧项目「提示词清单」为空（历史数据未写 prompts，非解析失败）。
