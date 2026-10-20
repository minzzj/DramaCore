# ROUND43 剧核品牌与多语言 UI —— 改动清单与验证报告

> 时间：2026-10-18（Asia/Shanghai）｜执行：品牌/UI/多语言智能体（ui-agent）
> 用户硬要求：① 改名「剧核 / DramaCore」② 中英双语 ③ UI 友好且 API 等信息全可见、可持久化、可测试连接 ④ 节点长参数分组折叠 + 状态/审核结论可视化 ⑤ 本地可验证、不重启任何 ComfyUI ⑥ 远端 4090 部署由 Lead 统一执行。
> 结论先说：**①—⑤ 已落地并本地验证（60/60 检查通过 + 面板 DOM 测试 9/9 + py_compile / node --check 全过）；⑥ 只提供部署命令，未碰远端。**

---

## 一、改了哪些文件

| 文件 | 类型 | 主要改动 |
|---|---|---|
| `bettercomfyui_comfy/__init__.py` | 改 | 新增「品牌层」：显示名统一为「剧核·xxx｜DramaCore xxx」中英双语（25 个节点类型全覆盖）；节点分类统一到 `剧核 DramaCore/*`（9 个旧分类映射，含 `BetterComfyUI/单节点`）；版本 0.1.0→0.2.0；日志/提示文案改「剧核 DramaCore」；**节点类型 ID 一个没动** |
| `bettercomfyui_comfy/routes.py` | 改 | 新增 3 个接口：`GET/POST /bettercomfyui/panel/config`（全部配置读写，密钥遮罩，白名单校验）、`POST /bettercomfyui/panel/test`（服务端代发连接测试，GET /models，404 退化 chat/completions）；全部同时注册 `/api/*` 前缀；旧接口全保留 |
| `bettercomfyui_comfy/web/js/dramacore_i18n.js` | **新增** | 中/英 i18n 资源（面板文案 + 25 节点显示名 + 约 45 参数 tooltip + 13 个状态/审核结论词）+ 语言检测/持久化 + 节点 UI 增强（标题本地化、tooltip、参数分组折叠、状态徽标） |
| `bettercomfyui_comfy/web/js/bettercomfyui_panel.js` | 改（重写） | 面板品牌改为「剧核 / DramaCore」；4 个折叠分组：连接配置（Key/Base/模型/测试/保存）、创作默认值（系统提示词/界面语言/输出语言/默认视频模型/默认图像模型/保存）、项目与运行、状态与审核结论（彩色徽标 + 日志）；原生设置分组改名「剧核 / DramaCore」；保留旧入口文件名，不创建浮动窗口 |
| `docs/剧核_DramaCore_品牌说明.md` | 新增 | 品牌定名、改名覆盖范围、i18n 结构与覆盖表、参数分组/状态可视化、旧工作流兼容清单 |
| `docs/剧核_DramaCore_面板配置说明.md` | 新增 | 面板字段清单与控件 id、DeepSeek 填写示例、数据落盘位置、接口表、常见问题 |
| `docs/ROUND43_剧核品牌与多语言UI.md` | 新增 | 本报告（改动清单 / 验证命令与结果 / 4090 部署步骤 / 遗留风险） |

**未改**（明确边界）：`one_stop_node.py`（director-agent 负责）、`vendor/`、`api_settings.py`、`config/*.yaml`、tests/。
> 说明：`one_stop_node.py:31` 源码里的 `_CATEGORY = "BetterComfyUI/单节点"` 保留不动；注册完成后由 `__init__.py` 品牌层把该类 `CATEGORY` 覆盖为 `剧核 DramaCore/单节点 One-Stop`（已验证），因此**界面分类已无 Better ComfyUI**，且不与他人改动冲突。

---

## 二、多语言（i18n）落地细节

- 资源结构：`web/js/dramacore_i18n.js`
  - `ZH` / `EN`：面板全部文案 key-value；
  - `NODE_NAMES`：25 个节点类型 → `{zh, en}`；
  - `WIDGET_TOOLTIPS`：约 45 个常见参数名 → 中英 tooltip；
  - `STATUS`：13 个运行/审核状态词（通过、不通过、需返工、待人工确认、失败、降级…）；
  - `GROUP_RULES`：参数分组正则（素材/视频/审核/输出）；
  - `detectLanguage()` / `createI18n()` / `globalThis.DramaCoreI18N`。
- 语言优先级：`localStorage["dramacore.uiLang"]` > `config/user.yaml ui.language` > `navigator.language`（zh* → 中文，其它 → English）。
- 生效范围：面板文案/状态徽标立即生效；节点标题刷新页面后生效；节点分类服务端本就双语。
- 服务端节点显示名：`剧核·单节点（剧本→成片）｜DramaCore One-Stop (Script→Final Cut)` 形式，节点搜索两种语言都能命中。

## 三、面板字段清单（用户要求「信息全、全自主化」）

| 分组 | 字段 | 控件 | 持久化 |
|---|---|---|---|
| ① 连接配置 | API Key / API Base / 模型名 / 测试连接 / 保存 | `#dc-api-key` `#dc-api-base` `#dc-api-model` `#dc-api-test` `#dc-api-save` | `config/user.yaml` → `prompt.api_key/model_name/endpoint`，同步剧本巨人 + 原生设置 |
| ② 创作默认值 | 默认系统提示词 / 界面语言 / 输出语言 / 默认视频模型 / 默认图像模型 / 保存 | `#dc-system-prompt` `#dc-ui-language` `#dc-output-language` `#dc-video-model` `#dc-image-model` `#dc-defaults-save` | `prompt.system_prompt_template`、`prompt.output_language`、`ui.language`、`ui.default_video_model`、`ui.default_image_model` |
| ③ 项目与运行 | 项目名/打开创建/项目下拉/刷新/执行器/剧本 JSON/运行/载入最简工作流 | `#dc-project-name` `#dc-open` `#dc-project` `#dc-refresh` `#dc-executor` `#dc-script` `#dc-run` `#dc-loadwf` | 走既有项目/运行接口 |
| ④ 状态与审核结论 | 体检彩色徽标（节点数/编排包/ffmpeg/版本/计算设备/真实生成）+ 运行日志 | `#dc-doctor` `#dc-badges` `#dc-log` | 读 `/bettercomfyui/doctor` |

状态/审核结论可视化：
- 面板：每项一个彩色徽标（ok/warn/err），不再是一段文字；
- 节点：`onExecuted` 提取 `passed/blocked/verdict/policy/conclusion/result` → 节点右上角彩色徽标（通过=绿、不通过=红、需返工=橙、待人工=蓝）。
参数分组折叠：按控件名分「素材/视频/审核/输出」四组，组标题按钮可折叠；button 控件不参与序列化，**不改变工作流存档格式、不删参数**，默认展开。

---

## 四、本地验证命令与结果（真实执行）

环境：Windows；`python -B`、`.comfyenv\Scripts\python.exe -B`（与 ComfyUI 同环境，含 aiohttp）、`node v24.15.0`。
**未启动/未重启任何本地或远端 ComfyUI。**

| # | 命令 | 结果 |
|---|---|---|
| 1 | `python -B -m py_compile bettercomfyui_comfy\routes.py bettercomfyui_comfy\__init__.py` | ✅ 通过（exit 0） |
| 2 | `node --check bettercomfyui_comfy\web\js\dramacore_i18n.js` | ✅ 语法 OK |
| 3 | `node --check bettercomfyui_comfy\web\js\bettercomfyui_panel.js` | ✅ 语法 OK |
| 4 | `node tests\js\panel_dom_test.mjs`（项目自带 DOM 级执行测试） | ✅ **9/9 通过**（模块可加载、registerExtension、setup 不抛错、面板挂载、无 console.error） |
| 4b | `python -B -m pytest tests/test_panel_dom.py -q` | ✅ **4 passed**（无浏览器 DOM 级执行 + 静态兜底断言全过） |
| 5 | `& .comfyenv\Scripts\python.exe -B <验证脚本>`（真实调用 aiohttp handler + 执行品牌层代码 + 静态核对） | ✅ **60/60 通过**（详见下表） |

验证脚本原始输出已存档：`docs/ROUND43_验证输出.txt`（60/60，UTF-8）。

验证脚本覆盖（全部通过）：

- **A. 路由行为（真实 aiohttp handler，不启服务）**：23 项
  GET /panel/config 200 且含 api/defaults/options/paths；读接口不含完整密钥只有 masked；品牌=剧核/DramaCore；POST 保存 5 个默认值字段 → 回执 written=5 → 读回一致；`user.yaml` 新增 `ui.*` 与 `prompt.system_prompt_template` 且原 `global` 段未破坏；非法 `ui_language=jp` → 400；超长 system_prompt(8001) → 400；连接测试非法 Base → ok=False 且说明；无密钥 → 明确提示；`/panel/config` `/panel/test` 均注册且双前缀。
  **测试后 `config/user.yaml` 已还原为测试前内容（脚本 finally 保证）。**
- **B. 品牌层（执行 `__init__.py` 里真实的 `_apply_branding()`）**：8 项
  提取到 25 个真实节点类型；全部有显式品牌映射；显示名无 "Better ComfyUI" 残留；每个显示名同时含「剧核」与「DramaCore」；分类全部 `剧核 DramaCore/*`；节点类型 ID 集合未变；changed==25；9 个旧分类全部有映射。
- **C. i18n 结构**：12 项（ZH/EN、NODE_NAMES、WIDGET_TOOLTIPS、STATUS、面板 key、navigator、localStorage、globalThis 挂载、分组折叠、状态徽标、tooltip）。
- **D. 面板字段清单**：18 项（14 个必需控件/接口 + 品牌标题 + ≥4 折叠分组 + ≥20 处 data-i18n + 原生设置改名）。

## 五、4090 远端部署步骤（**由 Lead 统一执行，本智能体未操作远端**）

目标机（ROUND36 实测信息）：`ssh -p PORT user@your-gpu-host`，外网 ComfyUI `http://your-gpu-host:8188`，ComfyUI 0.33.0 位于 `/model/ComfyUI`，部署根 `/data/BetterComfyUI`。

```powershell
# [本地] 1) 打源码包（含插件 web/ 前端与 docs，不含模型）
cd '[PROJECT-ROOT]'
python -B scripts/prepare_remote_bundle.py    # 产出 dist/bettercomfyui_bundle_*.zip
scp -P 8188 dist\bettercomfyui_bundle_*.zip user@your-gpu-host:/data/
```

```bash
# [远端] 2) 解包并只替换源码（插件 + 编排包），不动模型与工作流文件
ssh -p PORT user@your-gpu-host
cd /data && unzip -o bettercomfyui_bundle_*.zip -d BetterComfyUI
# 备份旧插件后覆盖（保留 config/user.yaml 等用户数据）
cp -a /model/ComfyUI/custom_nodes/bettercomfyui_comfy /model/ComfyUI/custom_nodes/bettercomfyui_comfy.bak.$(date +%s)
cp -a /data/BetterComfyUI/bettercomfyui_comfy/. /model/ComfyUI/custom_nodes/bettercomfyui_comfy/
cp -a /data/BetterComfyUI/bettercomfyui/. /model/ComfyUI/custom_nodes/bettercomfyui/

# 3) 重启 ComfyUI（沿用该机既有托管方式；ROUND36 用的是生产镜像自带的启动方式）
ps aux | grep -i '[m]ain.py'                 # 先确认当前进程与启动参数
#   若是 systemd/supervisor 托管：systemctl restart comfyui / supervisorctl restart comfyui
#   若是裸进程：kill <pid> 后按原参数重启，并保留环境变量：
#   cd /model/ComfyUI && BETTERCOMFYUI_HOME=/data/BetterComfyUI nohup python main.py --listen 0.0.0.0 --port 8188 > /tmp/comfyui.log 2>&1 &

# 4) 部署后验证（4 条，全过才算成功）
curl -s http://127.0.0.1:8188/api/bettercomfyui/panel/config | python -m json.tool   # brand=剧核/DramaCore，含 api/defaults/options/paths
curl -s http://127.0.0.1:8188/api/bettercomfyui/doctor | python -m json.tool         # node_count=25、orchestrator_available=true
curl -s http://127.0.0.1:8188/object_info | python - <<'PY'
import json,sys
d=json.load(sys.stdin)
ours={k:v for k,v in d.items() if k.startswith("BetterComfyUI")}
assert len(ours)>=25, len(ours)
assert all(("剧核" in v.get("display_name","") and "DramaCore" in v.get("display_name","")) for v in ours.values())
assert all(v.get("category","").startswith("剧核 DramaCore") for v in ours.values())
print("节点品牌/双语/分类 OK：", len(ours))
PY
```

```powershell
# 5) [浏览器] 打开 http://your-gpu-host:8188 → Ctrl+F5
#    a) 右侧面板标题应为「剧核 / DramaCore」，4 个折叠分组可见；
#    b) ① 填 API Key/Base/模型 → 点「测试连接」→ 应显示“连接成功：xx ms，模型 N 个”；
#    c) 点「保存」→ 提示写入 config/user.yaml；② 改界面语言为 English → 面板文案立即变英文（刷新后节点标题变英文）；
#    d) 打开一个旧工作流（含 BetterComfyUI* 节点）→ 节点仍能加载，标题显示「剧核·…｜DramaCore …」；
#    e) 添加节点菜单中「剧核 DramaCore」分组存在，原 Better ComfyUI 分组不再出现。
```

## 六、遗留风险 / 未做项（如实）

1. **远端 4090 未部署、未重启**：按用户/Lead 要求只给命令；部署后需按上面 4 条 curl + 浏览器步骤复验。
2. **节点标题语言需刷新页面**：ComfyUI 节点注册表在页面加载时建立，属框架限制；面板文案本身即时切换。已在文档与面板提示中写明。
3. **参数分组折叠的 `computeSize` 方案**：采用社区通用做法（button 不序列化、折叠只改渲染尺寸）；未在真实浏览器里人工点验（本轮无浏览器自动化），已做语法/DOM 测试与异常兜底（try/catch + console.warn，不阻断节点）。
4. **状态徽标依赖节点 `onExecuted` 消息字段**：不同节点字段名差异用宽松匹配（passed/blocked/verdict/policy/conclusion/result），若某节点不输出这些字段则只显示日志、不画徽标；不伪造状态。
5. **连接测试为服务端代发**：需要远端能访问所填 API Base（同机 localhost 才可写 localhost）；401/403 与网络错误会分开展示。
6. **`api_settings.py` 未改**：新配置走新增白名单路径；旧 `/bettercomfyui/settings` 完全保留。若今后希望面板字段并入旧接口，可再做一次合并重构。
7. `one_stop_node.py` 的 `_CATEGORY` 源码仍是旧名（不在本智能体写作用域），运行时由品牌层覆盖；若 owner 后续自行改为 `剧核 DramaCore/单节点 One-Stop` 也不影响（幂等）。
8. 未跑全量 pytest（避免与其它智能体并行写入/GPU 队列冲突）；与本改动相关的 JS 既有测试 `tests/test_panel_dom.py` 已单独执行通过（4 passed）。
9. **本机正在运行的 ComfyUI 实例里的插件目录是旧副本**：`.comfyui/custom_nodes/bettercomfyui_comfy/` 与源码仓库目录是两个独立目录（非 junction），本轮按要求只改仓库源码、未复制、未重启本机 ComfyUI。因此在 Lead 执行部署（复制 + 重启）之前，本机现有页面看不到新面板/新路由——这是预期状态，不是改动失效。
