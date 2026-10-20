# ROUND58 安全审计（发布前专项）

- **审计对象**：工作区源码（`H:\<path>\Better comfyUI`）、发布暂存 git 仓库 `_release/DramaCore`（HEAD `97d0409`）、`dist/` 历史包、`%TEMP%` 发布脚本与日志。
- **审计时点**：2026-10-20 20:30（Asia/Shanghai）。
- **方式**：只读源码/配置、`git` 历史与索引检查、ripgrep/正则扫描、Python 掩码扫描、本机已运行 ComfyUI 的只读 HTTP 探测；未修改源码/配置，未重启服务，未联网安装，未操作 GitHub。
- **脱敏约定**：本文不写任何完整密钥、口令、私钥、JWT、邮箱或公网 IP；只写位置、类型、长度/前缀（例如 `sk-1b6***`、`your-gpu-host`）。

## 0. 结论

> **不建议发布（BLOCK）。**
>
> 当前仍有 **2 个开放 P0**：发布机明文凭据残留（P0-4）、staging 未提交的混合树且已带入 13 篇含公网 IP 的文档（P0-5）。另有 **5 个开放 P1**（P1-1/P1-4/P1-6/P1-7/P1-8）和一组 P2 加固项。
>
> 审计过程中代码已合入 ROUND58 安全修复：原先的 **P0-1（节点 Key 进 workflow/history）、P0-2（Key 同步到无鉴权 settings）、P0-3（/panel/test 任意环境变量外带）已复核为“已修复/已缓解”**，不再按开放 P0 计；P0-2 的旧安装迁移与 P0-1/P0-3 的残留分别转入 P1/P2（见 §1）。
>
> **P0-4 属于凭据泄露，发布前必须处理**；P0-5 属于发布完整性/信息泄漏阻断，未清理并提交前不能发布。

### 0.1 P0/P1/P2 总表

| 级别 | 数量 | 主题 |
|---|---|---|
| **P0（开放）** | 2 | 发布机明文凭据（GitHub/SSH/jygpu/DeepSeek）；staging 未提交的混合树（0.1.3 工作树 + 旧 HEAD + 13 篇含公网 IP 文档 + `.gitignore` 退化） |
| **P0（已修复/缓解）** | 3 | 节点 Key 进 workflow/history（已修复，残留 P2）；Key 同步到无鉴权 settings（已停止写入/保存时 scrub，旧安装迁移 P1）；`/panel/test` 任意环境变量外带（已加本机+白名单+显式 key，残留 P2） |
| **P1（开放）** | 5 | 无鉴权敏感路由 + `0.0.0.0` 绑定；staging docs 已含公网 IP/SSH 端口（13 个文件）；vendor 许可误标 MIT（实为 Apache-2.0）；AGPL+Commons Clause 与 GPL/AGPL 兼容性；发布包本机路径/用户名泄漏 |
| **P1（已修复）** | 3 | 项目名存储型 XSS（已改 text 节点）；工作流 `api_base` 劫持 Key（Everything 已加 key_explicit 门禁）；`finalize_nodes` 输出文件名路径穿越（已取 basename） |
| **P2** | 12+ | 遮罩前 3+后 4 位泄漏；明文落盘/权限；`/doctor`/`/hardware`/项目路径信息暴露；vendor 未注册路由的路径穿越/SSRF（潜在）；`torch.load(weights_only=False)`；依赖/模型无校验；远程安装脚本 0.0.0.0；任意本地路径读取；`.gitignore` 缺口；内部 ops 文档进包；UI 蒙版可键盘绕过；无“不可信工作流”警告等 |

---

## 1. P0（发布阻断）

### P0-1 / P0-2 / P0-3 —— 审计中已复核修复/缓解（保留记录，不按开放 P0 计）

> 这三项在审计开始时确认为 P0；20:45–20:50 复核时，代码中已出现 ROUND58 安全修复。以下记录“原问题 + 修复后状态 + 残留”，避免把已修项继续列为开放 P0。

**P0-1 节点 Key 进 workflow/history（已修复；残留 P2）**
- 原问题：`dramacore_director_ui.js` 的 `bindWidget()` 会把 `api_key` 输入写入 node widget；`one_stop_node.py:837` 又是普通 `STRING` 输入，Key 会进入 workflow/`/prompt`/`/history`。
- 修复后：`dramacore_director_ui.js:2543-2569` 已移除 Key 输入，并在加载时清空旧 workflow 的 `api_key`/`api_base` widget；文案改为“密钥只在右侧面板/环境变量保存，不写入工作流”。正常 UI 路径不再把 Key 写进 workflow。
- 残留 P2：`one_stop_node.py:837` 仍保留 `api_key` optional 输入（兼容旧工作流/原生“参数”面板），`everything_node.py:515`、`storyboard_node.py`、`settings_node` 也有 `api_key` 输入；高级用户仍可在原生面板填入，进入 history。建议后续移除或提交前 strip，并清理旧 `/history` 条目（本机旧 history 仍有 1 条 `inputs/api_key`，长度 8，仅说明历史数据未清）。

**P0-2 Key 同步到无鉴权 `/settings`/`/storyboard/settings`（已停止写入；旧安装迁移 P1）**
- 原问题：`api_settings.py:224-227` 调用 `_sync_native_comfy_setting`，`:250` 写 `StoryBoard.api_key`，`:293-309` 写巨人 `settings.json`；这两个文件分别被无鉴权的 ComfyUI `/settings` 与 `/storyboard/settings` 返回。
- 修复后：`api_settings.py:220-230` 不再把 Key 传给同步函数；`:236-264`/`:299-326` 只同步 `base_url`，并主动 **scrub** 旧 `StoryBoard.api_key`/`api_key`；`:397-401` 限制 `api_key_env` 只能为两个固定变量。
- 残留 P1：scrub 只在**下一次保存配置**时执行；已升级但未再保存的安装，旧 `StoryBoard.api_key` 仍可能留在 `.comfyui/user/default/comfy.settings.json`，ComfyUI `GET /settings` 仍会返回完整旧 Key；`_read_native_comfy_setting`/`_read_giant_settings_key`（`:271-296`）仍读旧字段。建议在启动或 `/settings` GET 时主动 scrub 并迁移。
- 复核探测：当前 `/settings`、`/storyboard/settings` 的 key 为空（本机已清）；旧安装路径风险仍需迁移脚本。

**P0-3 `/panel/test` 任意环境变量外带/SSRF（已缓解；残留 P2）**
- 原问题：任意 `endpoint` + `Authorization: Bearer <已保存 key>`；`api_key_env` 可指向任意环境变量；无鉴权/无 Origin 检查。
- 修复后：`routes.py:636-669` 增加 loopback + Origin/Host 保护；`:930-948` 自定义 endpoint 必须显式提供 Key，服务端不再把已保存 Key 发往自定义地址；`api_settings.py:397-401` 的 `api_key_env` 仅允许 `BETTERCOMFYUI_API_KEY`/`DEEPSEEK_API_KEY`；`:968-969` 禁用重定向；`:802-813` 拦截云元数据 host。
- 残留 P2：`_panel_endpoint_blocked` 只拦 4 个元数据 host，仍允许 `127.0.0.1`/私网（为本地 LLM 保留）；本机进程/无 Origin 请求仍可做带**显式 Key** 的 SSRF；`BETTERCOMFYUI_ALLOW_REMOTE_PANEL=1` 可绕过保护。建议加私网/IP 解析拦截、鉴权 token，默认不开放远程。


### P0-4 发布机明文凭据残留（仓库外，但发布前必须处理）

> **明确：发布前必须处理。** 以下文件在 `%TEMP%`（不在 git 索引），但同机发布脚本/打包流程会用到它们；任何一次误复制、日志上传、打包目录扩大都会变成公开泄露。审计时它们仍然存在。

| 位置（`%TEMP%`） | 类型 | 现状/风险 | 处置 |
|---|---|---|---|
| `bcu_publish_r54.py`、`bcu_github_*.py`、`bcu_release_r54.py`、`bcu_round53_publish.py`、`bcu_stage_clean_r53.py` 等 | GitHub 账号邮箱 + **11 字符明文密码**（同一口令也出现在 `jygpu_login*.py`） | 明文常量，且 GitHub 与 jygpu 复用同一口令；脚本可直接登录账号 | 立即改 GitHub 密码 + 开启 2FA；改 jygpu 密码；删除/重写脚本为环境变量注入 |
| `dramacore_ed25519`、`dramacore_ed25519.pub` | OpenSSH 私钥（419 B，无口令） | 预检清单称已失效，但文件仍在；仍应视为敏感材料 | 删除两个文件；到 GitHub `Settings → SSH keys` 确认/删除对应 key |
| `bcu_ssh_env.txt` | SSH 主机 `your-gpu-host`、端口 22、用户 `root`、`BCU_SSH_PASS` 明文 | 服务器 root 口令明文落盘 | 改服务器 root 口令；禁用密码登录，改密钥 + 防火墙；删除文件 |
| `ssh_pass.txt` | 12 字节口令文件 | 明文口令（未在本文回显） | 确认归属并改密；删除 |
| `jygpu_tokens_found.json` | 2 个 jygpu JWT（227/393 字符；其一有效期至 2026-10-26） | 很可能是可用的登录态；`edge_shadow2` 浏览器 profile 仍可能含 localStorage | 在 jygpu 退出所有会话/改密；删除 token 文件与相关 Edge profile（或清 localStorage） |
| `bcu_local_comfyui4.log.err`、`bcu_start_new_container.sh` | 真实 DeepSeek Key（`sk-1b6***`，35 字符） | 日志/部署脚本明文包含真实 Key；日志常被贴到 issue | 立即轮换 DeepSeek Key；清理日志/脚本；在仓库与 issue 模板加“禁止贴日志原文” |
| `dist/*.zip`（32 个历史 bundle） | 早期包含 `config/user.yaml`（本机路径；采样时 Key 为空），多数包含旧版 `build_4090_workflow.py` 的公网 IP | 不属于 git 跟踪，但很容易被当作 Release 附件上传 | 全部不要上传；删除或移到离线归档；新的发布包重新生成并复扫 |
| 工作区与 staging `docs/` | 公网 IP/SSH 端口（`your-gpu-host`） | staging 同步后已有 13 个文件含 IP（P1-4），一旦 commit 即公开 | 先还原 staging 脱敏版；发布前全树扫描；只保留 docs 白名单 |

> 预检清单 `docs/ROUND58_发布预检清单.md` §4 已记录 C-1/C-2，但审计时**未清理**；本次还额外发现真实 DeepSeek Key 与未过期 jygpu JWT。请不要把“清单里写了”当作“已经处理”。

### P0-5 发布暂存区处于未提交的混合状态，且已引入公网 IP/内部文档（发布完整性阻断）

**独立复核（两个快照）**
- **20:30**：`_release/DramaCore` HEAD = `97d0409`（ROUND54），工作树干净（仅 `?? docs/ROUND58_RELEASE_v0.1.3-internal.md`）；缺 ROUND55–58 代码、`config/default.yaml`，工作流 112 槽位，版本 `0.1.2-internal`。
- **20:35**：检测到并发同步已把工作树改成 0.1.3：`__version__ = "0.1.3-internal"`、`config/default.yaml`、`bettercomfyui/hardware.py`、`bettercomfyui/store/rollback.py`、`bettercomfyui/prompt/h3_handbook.py`、**119 槽位**工作流均已出现；但 **HEAD 仍是 `97d0409`**，`git status` = **26 modified + 156 untracked**（20:40 已变为 **27 + 157**，同步仍在进行），未 commit/tag；README 仍写 `v0.1.2-internal`。
- 同步后的 `docs/` 已有 **13 个文件**含公网 IP `your-gpu-host` 与 SSH 端口/`user@`（ROUND20/36/37/41/42/43/44/45/47/49、RESUME、剧核_发布评估、单节点使用说明等），原 staging 脱敏版被覆盖（P1-4）。
- `.gitignore` 被源码版覆盖：不再忽略 `*.safetensors/*.pt/*.mp4/*.png`、`docs/remote_evidence_4090/`、`remote_reports/remote_pulled/` 等；`bettercomfyui_comfy/vendor/h3_director/docs/screenshot.png` 现在 unignored（约 125 KB），`git add -A` 会把它加入。
- 156 个 untracked 中约 99 个是 `docs/`（大量内部审计/运维报告），另有 16 个 tests、10 个 `bettercomfyui` 文件等；其中不少不应进入公开包。`docs/ROUND58_RELEASE...` 仍是未跟踪草稿。

**影响**
- 若现在 `git add -A && commit && push`：会把公网 IP/SSH 端口、内部运维报告、截图一起公开；若只 `git add` 部分文件，又可能漏掉 0.1.3 必需文件，发布包与 tag 不一致。
- 若直接按当前工作树打包：版本/工作流虽已对齐，但 docs/`.gitignore`/未跟踪清单未清理，仍不是可发布树。

**修复**
- 先清理：把这 13 个 docs 还原为脱敏版（`your-gpu-host`/8188）或只保留白名单 docs；恢复 `.gitignore` 并集（秘密/模型/媒体/远程证据/测试产物）；删除/忽略 `screenshot.png` 与内部运维 docs。
- 只做**选择性 `git add`**：`config/default.yaml`、R55–58 必需源码/测试、新工作流、脱敏 docs、README/版本；不要 `git add -A`。
- 复核 `git diff --cached` 后再 commit；README 升 `0.1.3-internal`；重跑测试/工作流守卫/secret+IP 扫描；再审计。

---

## 2. P1（发布前强烈建议修复）

### P1-1 仍有敏感 HTTP 路由无鉴权（部分已加保护）+ 文档/脚本鼓励 `--listen 0.0.0.0`

- **已加保护**：`/settings` 写入（`api_settings.py:358-401`）、`/panel/config`、`/panel/test` 已加 loopback + Origin/Host 校验；`api_key_env` 仅允许两个固定变量。
- **仍开放**：`/rollback/apply`（`routes.py:1382`）、`/project/open`（`:292`）、`/run`（`:343`）、`/hardware`（`:1448`）、`/projects/list`/`/project/context`/`/asset/preview`；无认证/CSRF/Origin/速率限制。
- **放大条件**：`scripts/远程安装巨人.sh:96,104` 用 `--listen 0.0.0.0` 重启 ComfyUI；根仓库 `docs/ROUND37...` 也记录该启动方式；ComfyUI 本身无鉴权。
- **可利用性**：同网段/云主机相邻租户可读取项目素材/提示词/成片列表、回滚素材 LOCK、创建项目、触发 ffmpeg 任务（DoS）、读取硬件/路径信息。API endpoint 重写已由 P0-2/P0-3 修复保护，但回滚/运行等写入接口仍可被滥用。
- **修复**：默认只绑 `127.0.0.1`；必须远程时走带认证的反向代理/VPN；为 `/rollback/apply`、`/project/open`、`/run` 等写接口加 CSRF token/Origin 白名单/一次性 token/限流；发布脚本不要默认 0.0.0.0。

### P1-2 项目显示名存储型 XSS（已修复）

- **原问题**：`projects.py:124-164` 接受任意 `display_name`；`/project/open` 可匿名创建；`dramacore_director_ui.js` 原先把 `display_name` 用 `html:` 注入。
- **复核修复**：`dramacore_director_ui.js:2451-2455` 已改为 `text:` + `h("b", { text: ... })`，不再走 `innerHTML`；项目名即使含 HTML 也只显示文本。
- **残留 P2**：服务端仍未限制 `display_name` 的 HTML/控制字符/长度；`/project/open` 仍无鉴权/限流（P1-1）；建议服务端清洗 + CSP + CSRF。

### P1-3 不可信工作流的 `api_base` 可把已保存 Key 发往任意 host（已修复）

- **原问题**：`one_stop_node`/`everything_node` 曾直接采用工作流 `api_base`；恶意 JSON 可把面板 Key 发往攻击者地址。
- **复核修复**：`everything_node.py:575-588` 已加门禁：只有节点**显式提供 `api_key`** 时才采用与面板不同的 `node_base`，否则忽略并告警；`storyboard_node` 要求显式 Key，`settings_node` 同理。正常“留空 Key、用面板 Key”的路径不会再接受工作流自定义 Base。
- **残留 P2**：`one_stop_node`/其它节点仍暴露 `api_base` 输入；建议补测试（恶意 workflow 不得改变 panel key 的目的 host），并在文档提示“不要加载不可信工作流”。

### P1-4 源码 docs 同步已把公网 IP/SSH 端口重新引入 staging（13 个文件，确认发生）

- **现状（20:35）**：staging `docs/` 已有 **13 个文件**含 `your-gpu-host`：ROUND36（2）、ROUND37（3）、ROUND43（5）、剧核_发布评估（6）、ROUND45（2）、ROUND47（3）、ROUND49（1）、ROUND20/41/42/44/RESUME/单节点使用说明（各 1）；含 `user@your-gpu-host`、`ssh -p PORT/22` 和外网 ComfyUI 端口。
- **原因**：同步时按 `docs/ROUND58_发布预检清单.md` §2.1“以源码为准覆盖”复制了源码 `docs/`；源码版本含真实 IP，而原 staging 版本是 `your-gpu-host`/8188。
- **影响**：一旦 commit/push 或打包，服务器 IP、SSH 端口、`root` 登录目标直接公开，给爆破/扫描提供目标；这是**已发生的发布泄漏**，不是假设。
- **修复**：把这 13 个 docs 还原为 staging 脱敏版，或只保留 `docs` 白名单（ROUND45/46/47/49/50-A/50-B/53/54/54-A/B/C/发布说明）；打包脚本对最终树/zip 加“非环回 IPv4 + `user@` + `ssh -p`”硬门禁；在 `git add` 前跑 IP 扫描。

### P1-5 `finalize_nodes.py` 输出文件名路径穿越（已修复）

- **原问题**：`finalize_nodes.py` 曾直接把 `输出文件名` 拼到 `paths.output_dir`，`../` 可逃逸。
- **复核修复**：`finalize_nodes.py:256-264` 已改为 `Path(raw_name.replace("\\","/")).name`，过滤不可打印字符，`.`/`..`/空名回退，再拼 `output_dir`；路径穿越已封堵。
- **残留 P2**：建议补回归测试；其它路径 sink（`review_nodes.py:507-514` 的任意绝对视频路径、字体路径等）仍按下文 P2-8 处理。

### P1-6 vendor 许可被误标为 MIT，实际是 Apache-2.0

- **事实**：`bettercomfyui_comfy/vendor/h3_director/LICENSE` 是 **Apache License 2.0**；`vendor/h3_director/README.md:242` 也写 `Apache-2.0`。
- **错误声明**：`docs/ROUND58_RELEASE_v0.1.3-internal.md:167`、`docs/剧核_DramaCore_发布说明_v0.1.md:61`、`docs/ROUND58_发布预检清单.md:243`、`docs/ROUND50-B...` 均写“MIT LICENSE”。
- **影响**：许可证/归属声明错误；Apache-2.0 要求保留许可与声明、标注修改；用户/合规扫描会得到错误结论。若上游有 NOTICE，还需保留（当前 vendor 目录无 NOTICE 文件）。
- **修复**：所有文档改为 Apache-2.0；新增 `THIRD_PARTY_NOTICES`（上游仓库 URL、版本/commit、修改说明、Apache-2.0 全文位置）；保留 vendor `LICENSE`；不要删除或改写版权头。

### P1-7 AGPL-3.0 + Commons Clause 的兼容性/可执行性风险

- **位置**：根 `LICENSE` 在 AGPL-3.0 上追加 Commons Clause（禁止 Sell）；README 也声明“不是 OSI 标准开源许可”。
- **风险**：AGPL-3.0 §7 / GPLv3 §7 禁止附加额外限制；ComfyUI 本身是 GPL-3.0，插件导入其模块；vendor `requirements.txt` 又含 `ultralytics`（AGPL-3.0）。把 Commons Clause 叠加到这些 GPL/AGPL 组件上，法律上存在冲突/不可执行风险；对商业用户也容易产生误解（“禁止商用”比 Commons Clause 的“Sell”更宽）。
- **修复**：让法务确认许可证组合；更稳妥的是 AGPL-3.0-only + 单独商业授权，不要把 Commons Clause 作为 AGPL 的附加条款；把 `ultralytics` 标为可选依赖并保留其 AGPL 声明；README 用精确措辞（禁止“Sell”，不是笼统“禁止商用”）。
- **等级**：P1（合规/发布风险，不是可执行漏洞）。

### P1-8 发布包内的本机绝对路径/用户名泄漏

- `_release/DramaCore/config/user.yaml.example:2`：`comfyui_base_path` 是本机绝对路径（形如 `H:\<path>\Better comfyUI\.comfyui`，已脱敏），既泄漏目录结构，也让其他用户复制后路径错误。
- `_release/DramaCore/scripts/publish.py:36`：`STAGE = Path(r"C:\Users\<user>\AppData\Local\Temp\bcui_stage")`，泄漏 Windows 用户名/临时目录布局。
- **修复**：示例配置改 `./.comfyui` 或留空；`publish.py` 改用 `%TEMP%`/环境变量（且该内部脚本是否应进公开包需再评估）。

---

## 3. P2（建议修复/加固）

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| P2-1 | `bettercomfyui_comfy/api_settings.py:101-108` | `mask_secret` 返回前 3 + 后 4 位；`/settings` 无鉴权，等于泄漏 7 个字符 | 只返回是否配置/来源；最多后 4 位 |
| P2-2 | `api_settings.py:211-233`、`:271-296` | API Key 仍明文写入 `config/user.yaml`（未设 600/ACL）；旧安装的 `StoryBoard.api_key`/巨人 `settings.json` key 只在下次保存时 scrub | 改用 keyring/env；至少 `chmod 600`/Windows ACL；启动或 GET 时主动迁移 scrub |
| P2-3 | `routes.py:147-263`（`/doctor`）、`:1448`（`/hardware`）、`:1244` 附近（`/projects/list`）、`api_settings.py:173`（`/settings` 的 `user_config_path`） | 无鉴权返回绝对路径、Python/设备/平台信息、项目根路径 | 远程访问时裁剪/鉴权；只返回相对路径 |
| P2-4 | `vendor/h3_director/lib/video_io.py:32-51`；`director/prompt_enhance_media.py:576-590`；`director/prompt_enhance_routes.py:40-52,68-127`；`director/http_routes.py:127-188` | vendor 的 `subfolder`/`videoFile` 未做 containment，存在任意读/路径穿越；`llm_url` 任意 URL（SSRF）；`upload_id` 可用 `C:foo` 做盘符相对路径 | 这些路由目前**未被注册**（`register_routes` 只有定义，无调用），属潜在风险；若将来接线，先加 `_is_under_dir`/allowlist/URL 校验 |
| P2-5 | `vendor/.../segment_cache.py:787,823,856,972,1166,1183`；`h3_latent_upscale.py:311`；`semantic_bridge.py:136-138` | `torch.load(..., weights_only=False)`，恶意 `.pt` 缓存/模型可执行 pickle | 统一 `weights_only=True`；优先 safetensors；对模型/缓存做哈希校验 |
| P2-6 | `scripts/setup_comfyui.py:36,86`；`scripts/download_models.py`（无 sha256）；`requirements.txt` 仅下限；vendor `requirements.txt` 含 `ultralytics` | 供应链：下载 ComfyUI master zip/60GB 模型无校验；依赖无锁/无 hash；`ultralytics` 为 AGPL | 固定 commit/版本 + SHA256；生成 lock/hashes；模型下载校验；把 ultralytics 标为可选并保留许可 |
| P2-7 | `scripts/远程安装巨人.sh:96,104` | 默认 `--listen 0.0.0.0`，且 `bash -c "$args"` 执行从 `ps` 读到的命令行 | 默认 127.0.0.1；需要远程时加认证/防火墙；不要用 `bash -c` 拼接 |
| P2-8 | `bettercomfyui_comfy/review_nodes.py:507-514`；`finalize_nodes.py` 的 `字幕字体文件`；`one_stop_node` 的首/尾帧/参考音视频路径 | 接受任意绝对路径，可读取本机任意媒体/字体并写入成片/报告 | 在节点边界校验路径属于 input/output/project；拒绝任意绝对路径 |
| P2-9 | `_release/DramaCore/.gitignore` | 被源码版覆盖后不再忽略 `*.safetensors/*.pt/*.mp4/*.png`、`docs/remote_evidence_4090/`、`remote_reports/remote_pulled/`、`screenshot.png` 等；`.netrc/.git-credentials/*.pem/id_rsa*` 仍缺 | 恢复 staging 与源码的并集：秘密文件、模型/媒体、远程证据、测试产物、内部 ops docs；发布前 `git status --ignored` + 全树扫描 |
| P2-10 | `scripts/publish.py`、`scripts/ssh_verify.py`、`scripts/远程安装巨人.sh`、`prepare_remote_bundle.py` | 内部运维脚本进入公开包；`ssh_verify.py:35,44` 使用 `StrictHostKeyChecking=accept-new` | 公开包移除内部 ops 脚本；保留最小安装脚本；文档说明主机密钥校验 |
| P2-11 | `_release/DramaCore/docs/ROUND58_发布预检清单.md`、`ROUND55_发布前检查清单.md` | 内部发布流程/凭据位置/账号余额等信息进入公开包（无完整 secret，但属内部信息） | 公开包只保留 release notes/用户文档；预检清单留在内部 |
| P2-12 | `bettercomfyui_comfy/web/js/dramacore_director_ui.js` 蒙版逻辑；release notes UI-P1-1 | 描述“折叠区蒙版只挡鼠标，Tab 可绕过”；无“不可信工作流”警告 | 安全默认值不要只靠视觉蒙版；在 README/首次运行提示“不要加载不可信工作流” |
| P2-13 | `routes.py:343-418`（`/run`）、`:292-340`（`/project/open`）、`:1448`（`/hardware?refresh=1`） | 无鉴权/无速率限制，可反复创建项目、跑 ffmpeg、触发硬件探测（进程/磁盘 DoS） | 加限流/并发锁/仅本机；对 project_name 长度与数量限制 |
| P2-14 | `api_settings.py:177-233`、`routes.py:501-526` | `user.yaml` 非原子读写，并发保存可能截断/覆盖 | 原子写 + 文件锁；保留备份 |
| P2-15 | `one_stop_node.py:837`、`everything_node.py:515`、`storyboard_node.py`、`settings_node.py` | 仍保留 `api_key` optional 输入；高级用户可在原生“参数”面板填入，Key 仍可能进入 workflow/history | 移除/隐藏该输入或提交前 strip；清理旧 `/history`；加回归测试 |
| P2-16 | `api_settings.py:271-296` | 仍从旧 `StoryBoard.api_key`/巨人 `settings.json` 读 Key；旧安装未保存过就不会 scrub | 启动/GET 时 scrub；迁移提示；不要把旧文件当合法来源 |
| P2-17 | `routes.py:802-813`、`:930-948` | `_panel_endpoint_blocked` 只拦 4 个元数据 host；本机/无 Origin 请求仍可用**显式 key** 做私网 SSRF；`BETTERCOMFYUI_ALLOW_REMOTE_PANEL=1` 可绕过 | 私网/IP 解析拦截；鉴权 token；默认关闭远程面板 |

---

## 4. 密钥/凭据泄漏专项（仓库内 vs 仓库外）

### 4.1 仓库内（源码 / docs / README / workflows / tests / staging / git 历史）

**结论：未发现真实 API Key / GitHub token / 私钥 / 真实邮箱。** 具体：

- `_release/DramaCore` 历史提交 363 个 tracked 文件，无 `config/user.yaml`、`projects/`、`output/`、模型/媒体/日志、`.env`、`*.pem`、`id_rsa*`、`.git-credentials`。**但当前工作树已被并发同步为 27 modified + 157 untracked（20:40 快照）**（见 P0-5），不能再用“工作区干净”描述；其中 `docs/` 已带入 13 个含公网 IP 的文件（P1-4）。
- `_release/DramaCore` 4 个 commit 的 `git log -p --all` / `git grep`：无 `ghp_REDACTED`/`github_pat_`/真实 `sk-`/`AKIA`/私钥块/明文 password 赋值；仅测试假 Key（`sk-secret-should-not-leak`、`sk-acceptance-31-fake` 等）。历史干净，但工作树未提交内容未审计完。
- 提交作者 `DramaCore <noreply@users.noreply.github.com>`；remote 为 SSH `noreply@users.noreply.github.com:minzzj/DramaCore.git`，URL 无 token；`.git/config` 无凭据。
- 根仓库 `config/user.yaml` 的 `prompt.api_key` 为空；根 `comfyui_err.log`/`comfyui_run.err` 未发现 `sk-` 或公网 IP。
- 注意：`bettercomfyui_comfy/tests` 里的 `sk-...` 是测试假 Key，**不要**因为这些命中而轮换/删除测试。

### 4.2 仓库外但同机（本次重点发现，发布前必须处理）

见 P0-4 表。简要：
- `%TEMP%` 的 GitHub/jygpu 登录脚本含明文口令（同一 11 字符口令）；`ssh_pass.txt` 12 字节口令；`bcu_ssh_env.txt` 含 `root` + SSH 口令；`dramacore_ed25519` 私钥；`jygpu_tokens_found.json` 2 个 JWT；`bcu_local_comfyui4.log.err`/`bcu_start_new_container.sh` 含真实 DeepSeek Key（`sk-1b6***`）。
- 这些不在 git 里，**不构成 GitHub 仓库内容泄露**；但发布操作在本机进行，属于“发布链路凭据卫生”P0，必须清理/轮换后再发布。

### 4.3 发布包（`_release/DramaCore`）

**历史提交不含**：`config/user.yaml`、`projects/`、`output/`、`models/`、`*.safetensors/*.pt/*.pth/*.gguf`、`*.mp4/*.png/*.wav`、`*.log`、`.env`、`.ssh`、`.git-credentials`、备份密钥。
**但当前工作树（20:35 快照）**：
- 已有 **27 modified + 157 untracked（20:40 快照）**；`git add -A` 会把 99 个 docs（含 13 个含公网 IP 的运维报告）、`screenshot.png`、内部预检清单等一起加入（P0-5/P1-4）。
- `config/default.yaml` 已出现（untracked，应选择性提交）；`config/user.yaml` 仍不存在（已确认）。
- `config/user.yaml.example` 含本机绝对路径（P1-8）；`scripts/publish.py` 含本机用户目录路径（P1-8）。
- `.gitignore` 已被源码版覆盖，失去模型/媒体/远程证据忽略规则（P2-9）。
- `dist/` 32 个历史 zip 不在 staging，但早期包含 `config/user.yaml`、多数含旧版 IP；不要作为 Release 附件（P0-4）。

### 4.4 `.gitignore` 评估

- **当前问题**：同步把 staging 原 `.gitignore` 换成了源码版；源码版不含 `*.safetensors`、`*.pt`、`*.mp4`、`*.png`、`docs/remote_evidence_4090/`、`remote_reports/remote_pulled/` 等规则，导致 `screenshot.png` 变为 unignored；`git add -A` 会带入模型/媒体/截图（P2-9）。
- **仍覆盖**：`config/user.yaml`、`config/user.yml`、`.env`、`*.key`、`projects/`、`.comfyui/`、`.comfyenv/`、`tests/verify/_last_run.json`、`tests/verify/_contracts_report.json`、`__pycache__/`。
- **缺口**：`.netrc`、`.git-credentials`、`*.pem`、`id_rsa*`、`*.ppk`、`*.bak`、`*.zip` 全量（当前只忽略 `dist/*.zip`）、内部 ops docs。
- **建议**：恢复 staging 原有秘密/媒体/远程证据规则并叠加源码 Python/测试规则；发布前跑 `git status --ignored` + 全树 secret/IP 扫描。

---

## 5. 发布包完整性检查（补充 P0-5 细节）

| 检查项 | 结果（20:35 快照） |
|---|---|
| git HEAD | `97d0409`（ROUND54）；工作树已是 0.1.3，但**未 commit/tag** |
| 版本常量 | `__version__ = 0.1.3-internal`；README 仍 `v0.1.2-internal`（不一致） |
| 关键文件 | `config/default.yaml`、`hardware.py`、`store/rollback.py`、`h3_handbook.py` 已出现（untracked，需选择性提交） |
| 工作流槽位 | 已从 112 更新为 **119**（与源码一致） |
| 工作树状态 | **27 modified + 157 untracked（20:40 快照）**；无 commit/tag |
| docs | **13 个文件含公网 IP/SSH 端口**（P1-4），原 staging 脱敏版被覆盖 |
| `.gitignore` | 被源码版覆盖，模型/媒体/远程证据规则丢失（P2-9） |
| 敏感文件 | 历史提交无 `config/user.yaml`/模型/项目/日志；当前 untracked 有 `screenshot.png` 等应排除项 |
| 历史 | 4 commits，无真实 secret；1 个 unreachable tag 对象（v0.1.0，无额外内容） |
| 发布说明 | `docs/ROUND58_RELEASE...` 仍是未跟踪草稿；内容与 README/代码状态需对齐 |

**建议删除/忽略清单（发布前）**
- 不提交：staging `docs/` 中 13 个含 IP 的运维报告、`ROUND58_发布预检清单` 等内部 ops 文档、`screenshot.png`、`dist`/日志/模型/媒体。
- 修正后纳入：`docs/ROUND58_RELEASE_v0.1.3-internal.md`（版本/SHA/MIT→Apache 修正后）、`config/default.yaml`、R55–58 必需源码/测试、新工作流。
- 恢复 `.gitignore` 并集：见 4.4。
- 应显式跟踪：`THIRD_PARTY_NOTICES`、脱敏 docs 白名单。

---

## 6. 许可证合规

| 项 | 现状 | 风险/建议 |
|---|---|---|
| 项目主许可证 | AGPL-3.0 + Commons Clause（`LICENSE`） | 非 OSI；Commons Clause 与 AGPL §7/GPLv3 §7 的“不得附加限制”冲突；ComfyUI 为 GPL-3.0，插件导入其模块；建议法务确认，优先 AGPL-3.0-only + 单独商业授权 |
| vendor `h3_director` | `LICENSE` 实为 **Apache-2.0**（README 也写 Apache-2.0） | 文档误写 MIT（P1-6）；必须更正并保留 Apache 许可/版权/修改说明 |
| NOTICE/第三方声明 | 无 `THIRD_PARTY_NOTICES`/`NOTICE` | Apache-2.0 及依赖许可未集中声明；新增 NOTICE，列 ComfyUI、MiniMax H3 Director（Apache-2.0，上游 URL/版本）、PyYAML/Pillow/numpy/imageio-ffmpeg/aiohttp/OpenCV/scenedetect 等 |
| `ultralytics` | vendor `requirements.txt` 含 `ultralytics>=8.0`（AGPL-3.0） | 与 Commons Clause 组合有冲突风险；标为可选、保留 AGPL 声明，或移除硬依赖 |
| 商业限制文案 | README 写“禁止商用” | Commons Clause 只限制 “Sell”，措辞过宽；按许可证原文精确表述 |
| 模型/权重 | README 称模型不在仓库、自行按许可下载 | 可接受；下载脚本需校验来源/哈希，避免供应链风险 |

**许可证结论**：不是“无 LICENSE”那种硬缺失，而是**声明错误 + 组合条款冲突**；在公开 GitHub 发布前应至少修正 vendor 许可声明并做法务确认（P1）。

---

## 7. 只读扫描记录（脱敏证据）

执行的只读检查（节选）：
- `git -C _release/DramaCore status --porcelain --untracked-files=all`：20:30 快照仅 `?? docs/ROUND58_RELEASE_v0.1.3-internal.md`；20:35 快照变为 **27 modified + 157 untracked（20:40 快照）**，20:40 为 **27 + 157**（并发同步仍在进行，见 P0-5）。
- `git -C _release/DramaCore log -p --all` + `git grep`（对 4 个 commit）→ 无真实 secret；仅测试假 Key；无 `.env`/`user.yaml`/私钥。
- `git ls-files` → 363 个 tracked；无模型/媒体/项目/日志/用户配置。
- `git fsck --unreachable` → 1 个 unreachable tag 对象（v0.1.0，无额外内容）。
- ripgrep 模式：`gh[pousr]_`、`github_pat_`、`sk-[A-Za-z0-9]{20,}`、`AKIA`、`BEGIN ... PRIVATE KEY`、`password=`、`jygpu`、`sshpass`、`user@`、非环回 IPv4、邮箱。
  - staging（20:30）：0 真实命中；`sk-` 仅测试假 Key。
  - staging（20:35 同步后）：**13 个 docs 含公网 IP/`user@`/`ssh -p`**；未发现真实 `sk-`/私钥；docs 之外未发现新的真实凭据。
  - 根 `docs/`：13 个文件含公网 IP（已掩码为 `your-gpu-host`），含 `user@`/`ssh -p` 运维命令；与 staging 同步后为同一批内容。
  - `%TEMP%`：命中真实凭据（P0-4），本文只记录位置/类型/长度，不回显值。
- 本机只读 HTTP：
  - `GET /settings` → 200，含 `StoryBoard.api_key`（空）；证明该无鉴权路由会返回该字段。
  - `GET /storyboard/settings` → 200，含 `api_key`（空）；证明第三方无鉴权路由。
  - `GET /history` → 200，9 条 prompt，其中 1 条含 `inputs/api_key`（长度 8，未回显）；证明 Key 会进 history。
- `dist` 32 个 zip 扫描：早期包含 `config/user.yaml`（采样 Key 为空）、旧版 `build_4090_workflow.py` 的公网 IP；未发现真实 `sk-`/私钥。
- `pip-audit`：环境未安装；按任务要求未联网安装，因此**未做依赖 CVE 审计**。

> 所有证据均只读；未修改源码/配置，未重启 ComfyUI，未执行 git push/tag/release，未登录 GitHub。

---

## 8. 发布前修复清单（按顺序）

1. **停止发布**：不要 `git push` 当前 `_release/DramaCore`，不要上传 `dist/*.zip`。
2. **轮换/清理凭据（P0-4）**：
   - 改 GitHub 密码并开 2FA；改 jygpu 密码并注销所有会话；改服务器 root 口令/禁用密码登录；轮换 DeepSeek Key。
   - 删除 `%TEMP%\dramacore_ed25519*`、`ssh_pass.txt`、`bcu_ssh_env.txt`、`jygpu_tokens_found.json`、含明文口令/Key 的 `%TEMP%\bcu_*.py` 与日志；清理 `edge_shadow2` 中 jygpu localStorage。
3. **修 P0 代码**：
   - 移除节点 `api_key` widget，提交前 strip；清理 workflow/history 中的 Key（P0-1）。
   - 删除 `_sync_native_comfy_setting`/`sync_giant_settings` 密钥同步，清除已有 `StoryBoard.api_key`（P0-2）。
   - 删除 HTTP 可写 `api_key_env`；`/panel/test` 加 endpoint allowlist/私网拦截/认证/CSRF（P0-3）。
4. **修 P1**：默认 127.0.0.1 + 鉴权/CSRF；XSS 转义；`api_base` 白名单；`finalize_nodes` 路径校验；docs 脱敏；vendor 许可声明；AGPL/Commons Clause 法务确认；示例路径占位化。
5. **整理 staging（P0-5）**：同步已发生但未提交；先按 P0-5/P1-4 清理：13 个 docs 还原脱敏版、恢复 `.gitignore` 并集、排除 `screenshot.png`/内部 ops docs；然后**选择性 `git add`**（`config/default.yaml`、R55–58 必需源码/测试、新工作流、脱敏 docs、README/版本），不要 `git add -A`。
6. **重跑验证**：工作流对齐守卫、全量 pytest/verify、`git status --ignored`、`git diff --cached` 人工复核、全树/zip secret+IP 扫描、`GET /settings`/`/history` 确认无 Key；README 升 `0.1.3-internal` 后再 commit/tag。
7. **再审计**：对最终 staging + 最终 zip 重跑本报告 P0/P1；确认 P0 清零后才允许 commit/tag/push/Release。

---

## 9. 未覆盖项与限制

- 未在“配置了真实 Key”的状态下做动态利用验证（只读原则；未写入 Key、未重启服务）；P0-1/P0-2/P0-3 的结论来自代码路径 + 本机空值路由探测 + 既有 history 字段，未实际外带 Key。
- 未做浏览器级 CSRF/PNA/CORS 实测；CSRF 结论基于 aiohttp `Request.json()` 不校验 Content-Type 的源码行为，以及路由无 Origin/CSRF 检查。不同浏览器对 localhost 的 Private Network Access 策略可能降低成功率，但直连/LAN 访问不受此限制。
- 未做依赖 CVE 扫描（`pip-audit` 未安装，按要求不联网安装）；未对 vendor 上游做逐行 diff/已知漏洞比对。
- 未审计 `%TEMP%`/`.comfyenv`/`.comfyui` 中全部第三方代码；仅审计与本插件路由/密钥/路径相关的部分。
- 未审计 `保留备用的节点/`（不属于发布包；其中含第三方源码与测试假 Key，若未来打包需逐包保留 LICENSE）。
- 许可证结论不是法律意见；AGPL+Commons Clause 的可执行性需法务确认。
- staging 在被审计期间发生了并发同步：本报告包含 **20:30**（旧树/干净）与 **20:35**（0.1.3 工作树/未提交、13 docs 含 IP）两个快照；结论以 20:35 为准。若同步仍在继续，发布前必须重跑第 8 节第 6–7 步。

---

**审计结论：BLOCK。P0 未清零、凭据未轮换、staging 未同步之前，不得发布到 GitHub。**
