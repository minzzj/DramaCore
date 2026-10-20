# ROUND25 修复报告：一次调用全量提示词 + 生成失败 + 结果可视化

> 用户本轮报障与基准（原文见 `docs/ROUND25_逻辑对照问题清单.md` 顶部）：
> ① API Key 已配置但工作流始终无法生成内容；② API 结果未在对应节点显示；
> ③ 一整段剧本、**全程仅一次 API 调用**、一次输出全部提示词（素材类 + 视频）；
> ④ 按预设分隔符自动拆出人物/场景/角色等素材提示词，以**标准 JSON** 填充到对应节点。
> 对照基准：`文档/BetterComfyUI_完整总流程图.dot` SG1（A–E）+ SG2（GEN1–GEN4 输入）+ SG7。

完成时间：2026-10-17 20:50。**所有结论可复现**，命令与证据见第四节。

---

## 一、问题清单 → 修复状态（逐条）

| # | 问题（实测根因） | 修复 | 状态 |
|---|---|---|---|
| 1 | 三处配置里的 API Key 都是验收测试假密钥 `sk-acceptance-test-FAKE-…`（len=39），真实调用必 401 → **这就是"Key 已配置却无法生成"的第一根因** | 清空三处假密钥；不再伪装"已配置"（待用户重新填真实 Key） | ✅ 已修 |
| 2 | 验收 30 直接写真实 `config/user.yaml`，且**只备份两处、漏了原生 `comfy.settings.json`**，假密钥残留在界面里 | 备份/恢复扩到三处；恢复后自检，不一致即 warn | ✅ 已修 |
| 3 | 面板把 `BetterComfyUI.ApiBase` 注册成原生设置，`onChange` 无条件 POST；**页面加载恢复设置就触发** → 打开网页即静默改写用户配置/巨人配置（实测复现：19:35 endpoint 被改 `/v1`） | JS 增加"只在真实用户交互后保存"门闩（pointerdown/keydown/wheel/touchstart） | ✅ 已修 + 回归 |
| 4 | W0/W0L 的剧本节点 `使用占位结果=True` → **即使有 Key 也永远不调模型** | W0/P1 = False（真调）；W0L = True（本机体验，明确标注占位） | ✅ 已修 |
| 5 | 界面路径只产出"镜头/视频提示词"，**没有 E1–E4（角色/场景/物品/分镜图提示词）** | 新增编排层 `prompt/script_build.py`：一次调用 → E1–E5 → 按类型拆分 → 标准 JSON | ✅ 新增 |
| 6 | 无法按预设分隔符拆分（界面只用 `[镜头N]`/空行） | 复用 `parse_with_report(auto)`：JSON 优先、`=== 角色 ===`/`=== 场景 ===`/`【物品】` 等预设分隔符自动解析，两条路径都记录 | ✅ 已修 |
| 7 | 结果不可视化：`script_json`/`摘要`/`提示词JSON` 全部悬空；无分类输出端口 | 新节点 8 个输出；W0/W0L/W2/P1 的 8 个输出**全部接 PreviewAny**，视频提示词同时接导演台「剧本分镜」 | ✅ 已修 |
| 8 | W0 含权重链 → 本机点队列必卡死（不是提示词失败，是"本机不可能出片"） | 新增 **P1（零权重、纯提示词、本机可跑）**；W0 仍留给 GPU 机器；红线不变 | ✅ 已修 |
| 9 | W2 连线非法（`target_slot=1` 指向不存在的输入槽）、模型名 `deepseek-v4-flash` 无效、无 API 配置节点 | 重建 W2：配置 → ScriptPrompts → 导演台 → 出片；每个连线槽位校验存在性 | ✅ 已修 |
| 10 | 无"只调用一次"的守门测试 | 验收 31：计数替身断言 `gateway.calls == 1`、`meta.api_calls == 1` | ✅ 新增 |
| 11 | 用户基准"填充到对应节点输入框"无法验证 | 工作流连线 + 真 ComfyUI 执行 + `/history` 逐输出断言（见第三节） | ✅ 已证 |

**仍未验证（如实标注，不写成通过）**：
- **真实 DeepSeek 密钥调用**：当前没有真实 Key（只有被清掉的测试假密钥），需用户填入后由用户实测；
- **真实视频出片**：环境墙（本机 15.75GB 内存 / 4GB 显存 vs 权重 34.14GB），与本轮无关；
- `BetterComfyUIEverything`（一体化总控台）仍用自写简化 prompt（只有镜头，无 E1–E4）——
  已登记为后续统一项，避免本轮范围失控（W0/P1/W2 已全部走新链路）。

---

## 二、改了什么（文件级）

### 新增
| 文件 | 作用 |
|---|---|
| `bettercomfyui/prompt/script_build.py` | **核心**：一次 `gateway.complete` → `parse_with_report(auto)` → `canonicalize_script` → `validate_script` → 按 CHARACTER/SCENE/PROP/SHOT 拆分 → 标准 JSON / `---` 视频提示词 / 统计；错误带可操作原因与原文前 500 字；占位模式 `api_calls=0` |
| `bettercomfyui_comfy/script_prompts.py` | 界面节点「剧本→全量提示词（一次调用·分类JSON）」：8 个输出（角色/场景/物品/分镜图提示词 JSON、视频提示词 `---` 文本、视频提示词JSON、全量JSON、摘要）；可接 API 配置节点；无 Key 明确报错 |
| `scripts/rebuild_prompt_workflows.py` | 用真实 `/object_info` 重建 4 套工作流并做 schema 校验（不通过不落盘） |
| `tests/verify/test_acceptance_31_script_prompts.py` | 15 条验收：单次调用/四类拆分/预设分隔符/标准 JSON/占位 0 调用/401 可读/解析失败可读/节点 8 输出/4 套工作流拓扑与可视化/占位开关/端口命名 |
| `docs/ROUND25_逻辑对照问题清单.md` | 对照 .dot 的问题清单（本轮基准） |

### 修改
| 文件 | 改动 |
|---|---|
| `bettercomfyui_comfy/__init__.py` | 注册 `script_prompts` 节点（节点总数 20 → **21**） |
| `bettercomfyui_comfy/web/js/bettercomfyui_panel.js` | 原生设置 onChange 加"仅用户交互后保存"门闩（修 A1） |
| `tests/verify/test_acceptance_30_*.py` | ①备份/恢复扩到原生设置并自检；②W0 覆盖清单改用新节点；③连线断言改为 `Settings→ScriptPrompts→OneDirector`；④新增 **P11 真浏览器回归**：打开页面不得改写 user.yaml / 巨人 settings.json |
| `tests/verify/test_acceptance_28_install_and_workflows.py` | W2 断言改为新链路 + 校验每个连线 `target_slot` 真实存在；愿景矩阵补 2 条（一次调用全量提示词、预设分隔符） |
| `tests/verify/test_acceptance_29_real_browser.py` | W2 真浏览器检查改为新节点 |
| `tests/verify/_harness.py` | 新增验收 31 条目 |
| `scripts/verify_install.py` | 期望节点加新节点（21）、界面工作流加 P1；节点数文案改为动态 |
| `config/user.yaml` / 巨人 `settings.json` / 原生 `comfy.settings.json` | **清空测试假密钥**（避免界面假"已配置"） |

### 工作流（源码 + `.comfyui/user/default/workflows` 双份，真实 schema 校验通过）
| 工作流 | 节点/连线 | 说明 |
|---|---|---|
| **P1_剧本→全量提示词（本机可跑）** | 14 / 15 | 配置 → ScriptPrompts(8 输出)→ 8 PreviewAny；视频提示词 → 导演组预览；**零权重** |
| W0_完整逻辑图全流程 | 22 / 28 | 配置 → ScriptPrompts → OneDirector → 真实导演台 → 合成 → 出片；8 输出全可视化；占位=False |
| W0L_本机可跑通体验版 | 14 / 17 | 同上但占位=True（本机体验结构，明确标注） |
| W2_DeepSeek全自动 | 19 / 22 | 修正旧版断链/非法槽位；配置节点 + ScriptPrompts + 出片链 |

---

## 三、证据（全部实测，可复现）

### 1) 单次调用 + 分隔符拆分 + 标准 JSON（单元级）
```
gateway.complete calls = 1        ← 计数替身
meta api_calls = 1
JSON 路径：format detected = json；四类拆分 角色2/场景1/物品1/分镜1
分隔符路径：format detected = delimited；=== 角色 === 等自动解析，同样是 1 次调用
视频提示词 = 林秀推门走进客栈 --- 老陈抬头看向门口
```
验收 31：**15/15 通过**。

### 2) 真实 ComfyUI 端到端（本地 mock OpenAI 兼容服务，不联网、不计费）
```
提交 P1 → status_str=success completed=True（14 节点）
mock 服务收到 API 请求 = 1 次（路径 /v1/chat/completions，Bearer mock key）
11 个文本预览节点显示：
  ✅ 角色图提示词 JSON（林秀/老陈）      ✅ 场景图提示词 JSON（客栈大堂）
  ✅ 物品图提示词 JSON（旧信）          ✅ 分镜图提示词 JSON（对视特写）
  ✅ 视频提示词（--- 分隔，3 段）        ✅ 视频提示词JSON
  ✅ 全量JSON（schema/meta.api_calls=1）✅ 摘要：API 调用 1 次 / delimited / 耗时
  ✅ 一体化导演台：3 段（视频提示词已自动填入「剧本分镜」）
断言 9/9 PASS（只调用一次 / 四类已显示 / --- 分隔 / 两种 JSON / 摘要）
```
> 这条同时证明：「一次调用」「预设分隔符拆分」「标准 JSON」「自动填充到下游节点」「可视化」。

### 3) 双解释器验收
```
系统 python -B -m pytest tests/verify -q → EXIT=0，通过=260 失败=0 未验证=1
.comfyenv  -B -m pytest tests/verify -q → EXIT=0，通过=249 失败=0 未验证=3
```
未验证均如实：`/free` 空闲时无显存下降（机器无模型驻留）、venv 未装 playwright（浏览器用例跳过）。
对比 ROUND24 基线 244（系统）/234（venv）→ **各 +16 / +15**（新增验收 31 与回归项）。

### 4) 安装与真浏览器
```
scripts/verify_install.py：插件逐文件哈希一致 / 巨人包在 / 工作流在 / 21 个节点可见 / 巨人节点可见 → 结论：安装完整
真浏览器（系统 Edge）打开 P1：画布 14/14，节点错误 无，未连线输入 无，pageerror 无，我方 console.error 无 → 通过
真浏览器回归（验收 30 P11）：打开页面前后 config/user.yaml 与巨人 settings.json **字节一致** → A1 不再复发
```

### 5) 复现命令
```powershell
cd '[PROJECT-ROOT]'
python -B -m pytest tests/verify/test_acceptance_31_script_prompts.py -q   # 15 条
python -B -m pytest tests/verify -q                                        # 260/0/1
& .comfyenv\Scripts\python.exe -B -m pytest tests/verify -q               # 249/0/3
python -B scripts/verify_install.py
python -B scripts/browser_sim.py --workflow "P1_剧本→全量提示词（本机可跑）.json"
# 需要 ComfyUI 在 8188 运行；重建工作流：
python -B scripts/rebuild_prompt_workflows.py
```
> 端到端 mock 验证脚本与 mock 服务为一次性工具，未提交进仓库（避免把"假 API"带进产品路径）；
> 其断言已固化进验收 31 的可重复用例。

---

## 四、现在怎么用（给用户）

1. 打开 ComfyUI（CPU 模式即可）：`cd .comfyui; & '..\.comfyenv\Scripts\python.exe' main.py --cpu --listen 127.0.0.1 --port 8188 --disable-auto-launch`
2. **右侧「Better ComfyUI」面板 → API 配置 → 填真实 DeepSeek 密钥 → 保存**（会把 `config/user.yaml`、巨人 `settings.json`、原生设置三处同步）。
3. 打开工作流 **`P1_剧本→全量提示词（本机可跑）`** → 在「剧本」里贴一整段剧本 → 点队列：
   - 只调用 **1 次** API；
   - 画布上 8 个预览框分别显示：角色/场景/物品/分镜图提示词（标准 JSON）、视频提示词（`---`）、
     视频提示词JSON、全量JSON、摘要；
   - 视频提示词已自动填入「一体化导演台。剧本分镜」。
4. 到 GPU 机器再开 **W0_完整逻辑图全流程** 出片（本机点 W0 会因加载权重而死锁，红线不变）。

> ⚠️ 密钥提醒：之前三处存的是验收测试假密钥（不是你的），本轮已清空。请重新填一次真实 Key；
> 以后跑 `tests/verify` 不会再覆盖它（验收 30 已扩到三处备份 + 面板不再自动保存）。


---

## 补充（2026-10-17 23:00，ROUND26）：本报告的产物形态已被用户否掉并重构

用户反馈：ROUND25 的「新节点 + 8 个输出 + 手工接线」是**生硬拼接 / 重新造东西**；
要求改成「**一个大节点**，插上模型、插上输出就能用；素材提示词自动进素材库；更自主自动化」。

因此：
- `BetterComfyUIScriptPrompts` 节点**已删除**（源码与安装副本）；
- 能力已收进 `BetterComfyUIEverything`（一体化总控台）内部：一次调用 E1–E5 → 确定性匹配登记素材库 →
  已 LOCK 素材全量挂每段 → 直接输出导演组；
- 工作流 W0（13 节点）/W2（12 节点，最简）/P1（5）/W0L（4），用户只需接「模型→导演台」「导演台→出片」。

本报告保留为**过程记录**（问题清单、根因分析、单次调用/占位/错误的测试仍有效，验收 31 已改为大节点形态）。
最新形态与证据见 `docs/ROUND26_大节点整合报告.md`。
