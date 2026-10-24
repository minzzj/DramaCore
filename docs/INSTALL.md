# Better ComfyUI · 安装与使用说明（INSTALL）

> 目标：**从零到「能在 ComfyUI 里点一下就跑完整流程」**，每一步都可复制粘贴。
> 想先搞清楚「这是什么、哪里不能信」，看 [HANDOFF.md](HANDOFF.md)（3 分钟）；
> 接口真源看 [CONTRACTS.md](CONTRACTS.md)；验收结论看 [VERIFY.md](VERIFY.md)。

---

## ⚠️ 三条必须先读的事实（不是免责声明，是实测结论）

| # | 事实 | 意味着什么 |
|---|---|---|
| **1** | **本机（开发机）没有模型权重**，`doctor` 实测 `models/` 下权重文件数 = **0**，且 ComfyUI 版本 **0.22.3 < 0.30** | 跑起来走的是 **占位媒体（mock）路径**：视频由 ffmpeg 合成纯色画面 + 正弦音轨，**产出不是模型输出**。它只能证明「编排、版本、审核、LOCK、落盘」这套逻辑通了 |
| **2** | **真实生成需要三样东西同时具备**：ComfyUI **≥ 0.30**（建议 0.41）+ 真实模型权重 + **目标机显存 ≥ 24GB** | 本机 GPU 是 GTX 1050 Ti 4GB，**只够开发**。CPU 版 torch 能跑通流程但真实出片慢到不可用 |
| **3** | 成片元数据里带 **`generation_provenance = {is_mock, executor, reason, unverified[]}`** | **不要**把它改成 `is_mock: false` 来「让演示好看一点」。成片文件在磁盘上无法区分「模型生成」与「占位媒体」，来源只存在于这份元数据里；有回归测试断言元数据文本必须出现「占位/未验证」字样 |

---

## 0. 先理解：要装的是**两个**东西

```
<ComfyUI 根目录>/
  main.py
  custom_nodes/
    bettercomfyui_comfy/          <-- ① 插件：节点界面 + 前端面板（本仓库的 bettercomfyui_comfy/）
      __init__.py  _bridge.py  nodes.py  phase2.py  routes.py
      web/js/bettercomfyui_panel.js
  models/                         <-- 权重放这里（真实生成才需要）

<任意目录，例如本仓库根目录>/
  bettercomfyui/                  <-- ② 编排包：真正的业务逻辑（store/review/director/pipeline）
  config/default.yaml
```

* ComfyUI 启动时会 `import` `custom_nodes/` 下每个目录，并读取它暴露的
  `NODE_CLASS_MAPPINGS`（节点注册表）、`NODE_DISPLAY_NAME_MAPPINGS`（显示名）、
  `WEB_DIRECTORY`（前端扩展）。见 ComfyUI 源码 `nodes.py` 的自定义节点加载段。
* 插件**自己不含业务逻辑**：它通过 `_bridge.py` 去找编排包 `bettercomfyui`。
  **找不到就明确报错，绝不假装可用**（这是本项目的纪律：禁止「节点在但点了没反应」的假成功）。

**今天没有 `pyproject.toml` / `setup.py` / `requirements.txt`**（已确认仓库根目录下均不存在），
所以 `pip install -e .` 现在会失败。请按下面「复制目录 / 设环境变量」的方式装。
也**不要**依赖 `pip install bettercomfyui` —— 仓库里没有发布配置，我们无法确认 PyPI 上那个名字对应的是本项目。

---

## 1. 方案 A：装进**已有**的 ComfyUI（推荐）

假设你的 ComfyUI 根目录是 `D:\ComfyUI`，本仓库在 `[PROJECT-ROOT]`。
请按自己的实际路径替换。

四步：**A1 把插件目录复制进 `ComfyUI/custom_nodes/` → A2 装 `bettercomfyui` 包 → A3 装依赖 → A4 重启**。

### A1. 复制插件目录

```powershell
$COMFY = 'D:\ComfyUI'
$REPO  = '[PROJECT-ROOT]'

# 目录名必须是 bettercomfyui_comfy（ComfyUI 用目录名当模块名）
Copy-Item -Recurse -Force "$REPO\bettercomfyui_comfy" "$COMFY\custom_nodes\bettercomfyui_comfy"
```

复制完检查一下**层级没有多套一层**：

```powershell
Test-Path "$COMFY\custom_nodes\bettercomfyui_comfy\__init__.py"   # 必须是 True
# 下面是常见的错误结果（多套了一层同名目录），出现就删掉外层重来
Test-Path "$COMFY\custom_nodes\bettercomfyui_comfy\bettercomfyui_comfy\__init__.py"  # 必须是 False
```

### A2. 装 `bettercomfyui` 包（让 ComfyUI 的 Python 能 `import bettercomfyui`，**任选一种**）

编排包必须位于 ComfyUI 那个 Python 环境能导入到的位置。三种方式，按省事程度排序：

**方式 1：整包塞进插件目录内部**（最省事，适合只想跑通）

```powershell
Copy-Item -Recurse -Force "$REPO\bettercomfyui" "$COMFY\custom_nodes\bettercomfyui_comfy\bettercomfyui"
# 顺带把配置目录也带上（没有它 config 会加载失败）
Copy-Item -Recurse -Force "$REPO\config" "$COMFY\custom_nodes\bettercomfyui_comfy\config"
```

**方式 2：设环境变量指向仓库根目录**（适合本机开发，改动立即生效）

```powershell
# 当前会话临时生效
$env:BETTERCOMFYUI_HOME = $REPO
# 永久生效（开新窗口才看得到）
[Environment]::SetEnvironmentVariable('BETTERCOMFYUI_HOME', $REPO, 'User')
```

> `BETTERCOMFYUI_HOME` 指向**包含 `bettercomfyui/` 目录的那一级**，不是 `bettercomfyui` 本身。

**方式 3：装进 site-packages**（有打包清单之后才可用；今天没有，故略）

### A3. 装依赖

在 **ComfyUI 所用的那个 Python 环境**里执行（不是随便一个 python）：

```powershell
cd $COMFY
.\.venv\Scripts\Activate.ps1          # 便携版/桌面版请用它自带的 python
python -m pip install pyyaml imageio-ffmpeg
```

| 依赖 | 谁需要 | 缺了会怎样 |
|---|---|---|
| `pyyaml` | 编排包（`config.py`、`store/projects.py` 都 `import yaml`） | 配置加载直接失败，节点点了报错 |
| `imageio-ffmpeg` | 拼接/封装阶段（`pipeline/core.py` 用它取 ffmpeg 可执行文件） | PATH 里也没有 `ffmpeg` 时，拼接阶段失败；**mock 路径同样需要 ffmpeg**（占位视频是 ffmpeg 合成的） |
| `torch` | ComfyUI 本体 | 缺了 ComfyUI 根本起不来 |

### A4. 重启 ComfyUI

```powershell
cd $COMFY
python main.py
```

然后跳到第 3 节「怎么确认装好了」。

---

## 2. 方案 B：从零装一套 ComfyUI

> **CPU 版很慢。** CPU 版只适合「跑通安装与流程」，真实出片请用 NVIDIA GPU 版。
> 本机开发用的 `.comfyenv` 里就是 `torch 2.14.1+cpu`。

```powershell
# 1) 取 ComfyUI 源码
cd D:\
git clone https://github.com/comfyanonymous/ComfyUI.git
cd D:\ComfyUI

# 2) 建独立 venv（Python 3.11 / 3.12）
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip

# 3) 先装 torch。CPU 版：
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
#    有 NVIDIA 显卡要真出图/出片时，改装 CUDA 版（索引地址与版本以 ComfyUI 官方 README 为准）：
#    pip install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu130

# 4) 装 ComfyUI 其余依赖
pip install -r requirements.txt

# 5) 装 Better ComfyUI 的依赖
pip install pyyaml imageio-ffmpeg

# 6) 装插件（同方案 A1/A2）
Copy-Item -Recurse -Force '<本仓库>\bettercomfyui_comfy' 'D:\ComfyUI\custom_nodes\bettercomfyui_comfy'
Copy-Item -Recurse -Force '<本仓库>\bettercomfyui'      'D:\ComfyUI\custom_nodes\bettercomfyui_comfy\bettercomfyui'
Copy-Item -Recurse -Force '<本仓库>\config'             'D:\ComfyUI\custom_nodes\bettercomfyui_comfy\config'

# 7) 启动（默认端口 8188）
python main.py
```

**要真实生成视频，还差三样**（缺一样就仍然只走占位媒体路径）：

1. ComfyUI 升级到 **≥ 0.30**（本机实测 0.22.3 低于 MiniMaxH3 导演台系的下限；upstream 已到 0.41）；
2. 把权重放进 `models/`（`doctor` 只认 `.safetensors/.ckpt/.pt/.pth/.bin/.gguf/.onnx/.sft`，
   `configs/*.yaml` 与 `put_*_here` 占位文件**不算**权重）；
3. 目标机显存 **≥ 24GB**（本机 4GB 只够开发）。

---

## 3. 怎么确认装好了（三层验证，缺一层都不算装好）

### 第一层：启动日志

ComfyUI 启动时会打印自定义节点导入耗时块：

```
Import times for custom nodes:
   0.1 seconds: D:\ComfyUI\custom_nodes\bettercomfyui_comfy
```

* 若出现 `(IMPORT FAILED)` → 该插件导入失败，往上翻日志看 traceback；
* 若出现 `Skip ... module for custom nodes due to the lack of NODE_CLASS_MAPPINGS` →
  插件没有被识别为节点包（多半是目录层级多套了一层，或 `__init__.py` 缺失）。

插件自己还会打一行（`logging`，取决于日志级别）：

```
Better ComfyUI：插件 v0.1.0 加载完成，6 个节点；编排包=<...>\bettercomfyui\__init__.py
```

**编排包没找到时**会额外打印一段可操作的指引（这是有意设计，不是崩溃）：

```
[Better ComfyUI] 编排包不可用：
Better ComfyUI 插件已加载，但找不到业务编排包 `bettercomfyui`，节点无法工作。
已尝试的候选位置：...
修复方法（任选其一）：...
```

### 第二层：网页节点搜索框

打开 `http://127.0.0.1:8188`，双击画布空白处（或右键 → Add Node），在搜索框输入
**`Better ComfyUI`**，应当看到 **6 个**节点：

| 节点显示名 | 用途 |
|---|---|
| **Better ComfyUI 项目** | 打开/创建/列出/删除项目（**R5 项目隔离**的入口） |
| **Better ComfyUI 运行** | 一键跑：剧本 → 素材 → 视频段 → 审核 → 拼接 → 成片 |
| Better ComfyUI 素材审核 | 人工审核素材版本（通过/拒绝/切模型模式） |
| Better ComfyUI 手动素材 | 手动把文件加进素材库（不审核，但留标记） |
| **Better ComfyUI 结果** | 只读展示运行状态与成片路径 |
| Better ComfyUI 体检 | 环境体检：缺什么、怎么修，点一下就知道 |

只看到 1 个「体检」节点、其余都没有，说明**编排包没装好** —— 点体检节点会告诉你原因。

### 第三层：命令行复核（可选，但在本机开发时最好用）

```powershell
cd '<本仓库>'
python -m bettercomfyui.cli doctor       # 环境体检：模块就绪度 / ComfyUI / GPU / ffmpeg
python -m bettercomfyui.cli run          # 端到端最小闭环（占位媒体路径）
```

> 注意：`doctor` 需要 `pyyaml`。本仓库的 `.comfyenv` 里有 torch 但**没有 pyyaml**，
> 用那个 venv 跑 CLI 前先 `pip install pyyaml imageio-ffmpeg`。

---

## 4. 怎么用最简工作流

工作流文件：`bettercomfyui_comfy/workflows/minimal_workflow.json`（**只有 3 个节点**，不依赖任何第三方节点包）。

### 4.1 加载

* 把 `minimal_workflow.json` **直接拖进** ComfyUI 画布；或菜单 **Workflow → Open**（Ctrl+O）选它。

加载后应当看到三个节点和两条连线：

```
Better ComfyUI 项目 ──project_id──> Better ComfyUI 运行 ──report_json──> Better ComfyUI 结果
   1. 项目（选择或创建）                2. 一键运行                          3. 结果（状态 + 成片路径）
```

### 4.2 三个节点的参数（就是文件里预先填好的值）

| 节点 | 参数 | 文件里的值 | 说明 |
|---|---|---|---|
| 项目 | `action` | 打开或创建 | 想删项目时选「删除项目」，并把 `confirm_delete` 打开（二次确认是硬要求） |
| 项目 | `project_name` | 我的短剧 | 同名项目已存在就打开它，不存在就新建 |
| 项目 | `project_id` | （空） | 填了就按 id 直接打开；留空则按名字查找/新建 |
| 运行 | `project_id` | ← 连线上游 | 由「项目」节点喂进来 |
| 运行 | `script_text` | 一段可用的演示剧本 JSON | 换成你自己的剧本；**必须是合法 JSON**，否则节点直接报错（不会静默当成空剧本） |
| 运行 | `review_mode` | MODEL | 审核模式（MODEL / HUMAN） |
| 运行 | `asset_review_mode` | MODEL | 素材审核模式（MODEL / HUMAN） |
| 运行 | `executor` | **占位媒体(mock)** | 本机没权重，**保持默认**。有权重+≥0.30 时才改「真实 ComfyUI」 |
| 运行 | `max_segments` | 3 | 段数上限 |
| 运行 | `enable_cascade` | true | 是否启用级联返工 |
| 结果 | `report_json` | ← 连线上游 | 由「运行」节点喂进来 |
| 结果 | `open_output_folder` | false | 打开为 true，跑完自动弹出成片所在文件夹（Windows） |

### 4.3 跑

1. 先点一次 **Better ComfyUI 体检** 节点（可选，但强烈建议）确认「编排包可用」；
2. 在「运行」节点里把 `script_text` 换成你的剧本（或先原样跑一遍）；
3. 点 **Queue / 运行**；
4. 看「结果」节点的输出文本：状态、成片路径、元数据路径、段数、`R_full` 调用次数（**应恰为 1**）。

成片落在项目目录里（**R5：任何路径都含 `project_id`**）：

```
<ComfyUI 或仓库根>/projects/<project_id>/output/     # 成片 + 封面 + 元数据
<ComfyUI 或仓库根>/projects/<project_id>/logs/events.jsonl   # 追加式事件日志
```

### 4.4 离线自检这个工作流（改过 JSON 之后务必跑）

```powershell
cd '<本仓库>'
python bettercomfyui_comfy\workflows\validate_workflow.py
```

它会**真 import** `bettercomfyui_comfy` 取节点注册表，逐条检查：节点 type 是否逐字命中、
连线两端是否存在、**每个必填输入是否都有值**（连线或控件值）、`widgets_values` 是否与控件逐位对齐、
输出名/类型是否与 `RETURN_TYPES`/`RETURN_NAMES` 一致。**退出码 0 = PASS，非 0 = FAIL**。

---

## 5. 怎么卸载

```powershell
$COMFY = 'D:\ComfyUI'

# 1) 停掉 ComfyUI（Ctrl+C，或关掉窗口）

# 2) 删插件目录（前端面板 js 在它内部，一并消失）
Remove-Item -Recurse -Force "$COMFY\custom_nodes\bettercomfyui_comfy"

# 3) 如果设过环境变量，清掉
[Environment]::SetEnvironmentVariable('BETTERCOMFYUI_HOME', $null, 'User')
Remove-Item Env:\BETTERCOMFYUI_HOME -ErrorAction SilentlyContinue

# 4) 如果当初 pip 装过依赖（pyyaml / imageio-ffmpeg），可选卸载
python -m pip uninstall pyyaml imageio-ffmpeg
```

**项目数据不会自动删除**（这是有意设计：素材库只追加、可追溯）。确认不再需要时手工删：

```powershell
# 先确认路径！R5 要求所有项目数据都在 projects/<project_id>/ 下
Get-ChildItem "$COMFY\projects"        # 或 <本仓库>\projects
Remove-Item -Recurse -Force "$COMFY\projects\<project_id>"
```

重启 ComfyUI，确认启动日志里不再出现 `bettercomfyui_comfy` 的导入行、搜索框搜不到
「Better ComfyUI」即为卸载干净。浏览器记得 **Ctrl+F5** 强刷（前端 js 有缓存）。

---

## 6. 常见问题

### Q1. 搜索框里搜不到节点 / 只有 4~5 个节点

按顺序排查：

1. **看启动日志**有没有 `(IMPORT FAILED)` 或 `Cannot import ... custom nodes: ...` —— 有就读 traceback；
2. **目录层级**：`custom_nodes\bettercomfyui_comfy\__init__.py` 必须直接存在（不要多套一层同名目录）；
3. **文件完整性**：`__init__.py` / `_bridge.py` / `nodes.py` / `phase2.py` / `routes.py` / `web\` 都要在；
4. **浏览器缓存**：Ctrl+F5；
5. **翻页**：搜索框里输入 `Better ComfyUI`（含空格）全称。

### Q2. 节点在，但一点就报「找不到业务编排包 `bettercomfyui`」

插件与编排包是分开装的。按报错里列出的「已尝试的候选位置」对照修：

* 用 **A2 方式 1**：把 `bettercomfyui/`（和 `config/`）复制进
  `custom_nodes/bettercomfyui_comfy/` 内部；
* 用 **A2 方式 2**：设 `BETTERCOMFYUI_HOME` 指向**包含** `bettercomfyui/` 的那一级目录，然后重启 ComfyUI；
* 确认 ComfyUI 用的 Python 环境里 `python -c "import bettercomfyui"` 能成功。

> 插件的设计是：**找不到编排包就明确报错并给出修法**，绝不会「假装能用」。

### Q3. 端口被占用（`Address already in use` / 起不来）

ComfyUI 默认端口 **8188**。

```powershell
netstat -ano | findstr :8188          # 看是谁占了，最后一列是 PID
python main.py --port 8189            # 换端口启动
# 或杀掉占用进程（先确认 PID 确实是旧实例）
taskkill /PID <PID> /F
```

**换端口后记得改配置**：本仓库 `config/user.yaml` 里

```yaml
global:
  comfyui_base_path: 'C:\...\ComfyUI'
  comfyui_endpoint: 'http://127.0.0.1:8000'
```

`comfyui_endpoint` 必须与实际监听的端口一致（默认值写的是 8000，而 ComfyUI 默认是 8188）——
不一致时 `doctor` 与「真实 ComfyUI」执行器都会连不上。**这些机器相关路径只能写在 `config/user.yaml`**，
不许硬编码进代码（CONTRACTS 第 1 节）。

### Q4. `ModuleNotFoundError: No module named 'torch'` / `No module named 'yaml'`

* `torch`：ComfyUI 本体需要。`pip install torch --index-url https://download.pytorch.org/whl/cpu`
  （CPU 版，慢）；有显卡时按官方 README 装 CUDA 版。
* `yaml`：是**编排包**需要（不是你装错地方了）。`pip install pyyaml`。
* 关键：**装到 ComfyUI 用的那个 Python 环境里**。便携版/桌面版自带 Python，
  要用它自带的终端或 `python_embeded\python.exe -m pip install ...`。

### Q5. 报「拼接/封装失败」或 ffmpeg 相关错误

`pipeline/core.py` 先找 PATH 里的 `ffmpeg`，找不到就用 `imageio-ffmpeg` 自带的：

```powershell
pip install imageio-ffmpeg
python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
```

**占位媒体路径也需要 ffmpeg**（占位视频就是 ffmpeg 合成的合法 mp4），所以缺 ffmpeg 时
连 mock 都跑不出成片。

### Q6. 跑出来画面是纯色 + 有「嘟——」的声音，是不是坏了？

不是坏，那是**占位媒体**：ffmpeg 合成的纯色画面 + 440Hz 正弦音轨。
本机没有模型权重、ComfyUI 版本也不够，**真实生成未验证**（见开头第 1、2 条）。
要看它到底走了哪条路，看成片元数据里的 `generation_provenance`。

---

## 7. 已知限制（截至本次交付，如实列出）

1. **未验证真实生成**：本机 `models/` 权重数 0、ComfyUI 0.22.3 < 0.30、`custom_nodes` 无第三方节点、
   `127.0.0.1:8000` 实测不可达。所以「真实生成可用」这句话在本机**不成立**。
   放行口只有一个：目标机跑通后由 Lead 显式调用 `assert_real_capabilities()`。
2. **「运行」节点的 4 个控件当前不改变行为**：`review_mode`、`asset_review_mode`、
   `max_segments`、`enable_cascade` 只出现在 `INPUT_TYPES` 与函数签名里，`run()` 内部尚未消费它们
   （实测：只有 `executor` 会被用来决定走 mock 还是真实路径）。界面能改、工作流能存，
   但改了**暂时没有效果** —— 这是待接线项，不是「已生效」。
3. **素材库只追加不覆盖**（R2）：版本只增不改，磁盘会持续增长，属设计取舍。
4. **LOCK 绑定 `asset_id + version + content_hash`**（R3）：任何新版本都必须重新审核，
   不要在外部手工改 `meta.json` 来「让它通过」。
5. **删除项目需要二次确认**（`confirm_delete` 打开）；有运行任务或未保存状态时删除会被拒绝。

---

_本文件与 `bettercomfyui_comfy/workflows/minimal_workflow.json`、`validate_workflow.py` 同批交付。_
_节点字段名以 `bettercomfyui_comfy/nodes.py` 的 `NODE_CLASS_MAPPINGS` / `INPUT_TYPES` 为唯一真源。_
