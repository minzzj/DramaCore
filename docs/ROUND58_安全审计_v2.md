# ROUND58 发布前安全审计（v2 · 独立复核）

- **审计对象**：工作区源码 `[PROJECT-ROOT]`；发布暂存 `_release/DramaCore`；候选发布包 `%TEMP%\bcu_r58_packtest\dramacore_v0.1.3-internal_source.zip`（392 文件 / 2,009,454 B / 生成于 2026-10-20T19:43:14+0800 / sha256 `979e9c23…`）；`dist/` 历史 bundle（32 个）。
- **审计时点**：2026-10-20 20:22–20:44（Asia/Shanghai）。
- **方式**：只读源码/配置/文档/工作流/测试；`git status/log/ls-files`；掩码式正则扫描（不打印明文）；对**本机正在运行的 ComfyUI** 做只读 GET 探测（未 POST、未改配置、未重启服务、未联网安装、未操作 GitHub）。
- **脱敏约定**：本文不写任何完整密钥、口令、私钥、JWT、邮箱或完整公网 IP；只写位置、类型、长度/前缀（如 `sk-acc…(len=39)`、`your-gpu-host`）。

> ### ⚠️ 审计期间源码被并发修改（重要）
> 本报告以 **20:44 快照**为准，并在文末标注快照指纹。审计过程中另一个修复流程已落地部分安全改动：
> - 20:33 `bettercomfyui_comfy/__init__.py` 版本升到 `0.1.3-internal`；
> - 20:39 `api_settings.py` 调整；
> - 20:40 `routes.py` 新增 `_panel_sensitive_guard`（本机/同源）、`_panel_endpoint_blocked`（云元数据）、自定义 endpoint 不发送已保存 Key；
> - 20:41 `dramacore_director_ui.js` 更新；
> - 20:24 `_release/DramaCore` 同步过一次（routes.py 64,530 B），**落后于 20:40 的安全修复**。
>
> 因此：**P0-1/P0-3 已按“修复后仍存在的缺口”重新定级**；发布前必须以最终源码重新打包并重跑本报告检查项。

---

## 0. 结论

> ## **NO-GO（不建议发布）**
>
> 仍存在 **4 个 P0**：
> 1. `POST /bettercomfyui/settings` **完全没有**本机/同源保护，可远程持久投毒 `endpoint`（下次生成把用户 Key 发到攻击者）、替换 `api_key`、指定任意 `api_key_env`；
> 2. 保存密钥时仍把**明文 Key 同步写入 ComfyUI 自己的设置文件**，而 ComfyUI 的 `/userdata/comfy.settings.json`、`/settings` **无鉴权即可读取**；
> 3. **没有可发布的构建物**：`_release/DramaCore` 落后于 20:40 安全修复，候选 zip 更旧（19:43）且缺 `install/`、根 `LICENSE`/`README`/`requirements.txt`；
> 4. 发布机 `%TEMP%` 仍有**明文 GitHub 密码与 OpenSSH 私钥**。
>
> 另有 **8 个 P1**（节点 `api_key` 进工作流/`/history`、残余无鉴权写路由、`/panel/test` 保护可被反代/开关绕过、远程安装脚本 root+`0.0.0.0`、发布包缺 AGPL 文件、vendor 许可误标 MIT、文档真实 IP/本机路径、成片文件名穿越）。

| 级别 | 数量 | 主题 |
|---|---|---|
| **P0** | 4 | `/settings` 无保护写配置；密钥写入无鉴权可读文件；构建物过期/缺件；发布机明文凭据 |
| **P1** | 8 | 节点 Key 进工作流/history；残余无鉴权写路由；`panel/test` 保护绕过；远程脚本 root+0.0.0.0；包缺许可证；vendor 许可误标；真实 IP/本机路径；输出名穿越 |
| **P2** | 12+ | 信息暴露、`refresh=1` 资源滥用、遮罩 3+4 位、依赖未锁、BAT 转义、ssh/publish 脚本、vendor 潜在路由、`.gitignore` 缺口、内部审计文档进包等 |

> 与旧审计的差异：`docs/ROUND58_发布预检清单.md` §3 结论为“安全审计干净”，但其规则只覆盖 token/PAT/OpenAI Key/AWS Key/私钥块，**未覆盖“无鉴权路由”“密钥落到无鉴权可读文件”“许可证缺失”“文档服务器 IP”**；本报告推翻该结论并补充新功能面。

---

## 1. P0（发布阻断，必须修）

### P0-1　`POST /bettercomfyui/settings` 无任何保护：远程持久投毒 endpoint / api_key / api_key_env

**位置**
- `bettercomfyui_comfy/api_settings.py:358-385`（`handle_save`）——**无** loopback/Origin/token 校验（20:39 改版后仍未加）
- 注册：`bettercomfyui_comfy/routes.py:1519-1525`（`/settings` GET+POST，双前缀 `/bettercomfyui` 与 `/api/bettercomfyui`）
- 可写字段：`api_settings.py:89-92` `ALLOWED_FIELDS = (model_provider, model_name, endpoint, api_key, api_key_env, output_language, output_format)`
- 落盘：`api_settings.py:184-215`（直接重写 `config/user.yaml`）

**证据**
- 20:40 新增的保护只加在 `/panel/config`（`routes.py:674`）与 `/panel/test`（`routes.py:910`），**`/settings` 未加**（`_panel_sensitive_guard` 全仓仅两处调用）。
- `/settings` 与 `/panel/config` 写的是同一份 `config/user.yaml`；后者还额外限制了 `api_key_env` 只能取两个白名单值（`routes.py:691-697`），前者**没有该限制**。

**可利用条件**：能访问 ComfyUI HTTP 端口（默认 `127.0.0.1:8188`；但 `scripts/远程安装巨人.sh:96,104` 与常见部署为 `--listen 0.0.0.0`，同网段/公网即可）；无需凭据；普通 HTTP 客户端即可，无需浏览器。

**影响**
1. **持久凭据外带**：`{"endpoint":"https://attacker.tld"}` → 此后 `gateway.py` 把用户真实请求（含 `Authorization: Bearer <用户Key>`）发往攻击者；
2. **替换/接管 Key**：`{"api_key":"<攻击者Key>"}`，可劫持用户调用、污染产出；
3. **任意环境变量名**：`{"api_key_env":"AWS_SECRET_ACCESS_KEY"}` → `GET /settings` 返回该变量的**遮罩值**（前 3+后 4 位，`api_settings.py:101-108,161-174`）→ 无需 `/panel/test` 即可远程泄露任意环境变量的 8 个字符；若再满足 P1-3 的绕过条件，则整值外带；
4. **提示词投毒**：通过 `/panel/config` 可写 200k `system_prompt`（该端现已限本机，但同机恶意进程/反代仍可），影响所有生成。

**修复建议**
1. 给 `/settings` POST 套用 `_panel_sensitive_guard`（与 `/panel/config` 一致），并同样白名单化 `api_key_env`；
2. 全组 `/bettercomfyui/*` 增加本地 token（启动生成随机值，前端从设置读取），无 token 401；
3. `endpoint` 写操作要求二次确认 + 记录旧值；禁止写私网/环回 host（本地 LLM 走显式开关）；
4. 修改 `api_key`/`endpoint` 时写审计日志（时间、来源 IP、旧值主机）。

---

### P0-2　保存密钥时明文写入 ComfyUI 自身“无鉴权可读”的设置文件

**位置**
- `bettercomfyui_comfy/api_settings.py:236-262`（`_sync_native_comfy_setting` 写 `StoryBoard.api_key`、`StoryBoard.base_url`）
- `api_settings.py:268`（`COMFY_SETTINGS_FILE = <repo>/.comfyui/user/default/comfy.settings.json`）
- 调用链：`api_settings.py:227-231`（每次 `save_user_settings` 都同步）

**证据（只读实测，未回显值）**
- `GET http://127.0.0.1:8188/userdata/comfy.settings.json` → **200**，567 B，含 `api_key` 字段；
- `GET http://127.0.0.1:8188/settings` → **200**，含 `StoryBoard.api_key` / `storyboard.api_key`（本次实测值为空，长度 0）；
- 两个 ComfyUI 原生端点均无鉴权（`server.py` 无 auth 中间件；`app/user_manager.py:334` 的 `/userdata/{file}` 在 `--multi-user` 关闭时用户恒为 `default`）。

**可利用条件**：用户在面板保存真实 Key（正常使用即触发）→ 任何能访问端口的客户端 GET 上述 URL 即可读走明文 Key。

**影响**：API Key 明文泄露（LAN/公网暴露时等同凭据失窃）；`settings.json` 也可能被其他插件读取。

**修复建议**
1. **不要**把密钥同步到 `comfy.settings.json` / 巨人 `settings.json`；只留在 `config/user.yaml`（优先环境变量）；
2. 若必须同步，只同步“已配置/来源”，不同步密钥；
3. `/bettercomfyui/*` 加 token，安装器/文档默认 `--listen 127.0.0.1`；
4. 已保存过的用户：文档提示轮换 Key，并提供清理 `comfy.settings.json` 旧值的方法。

---

### P0-3　没有可发布的一致构建物：暂存落后于安全修复，候选 zip 更旧且缺件

**对象**
- 发布暂存：`_release/DramaCore`（git HEAD `97d0409` = ROUND54；工作区大量未提交改动；`routes.py` 64,530 B / mtime 20:24:42，**低于**源码 66,850 B / 20:40:12）
- 候选包：`%TEMP%\bcu_r58_packtest\dramacore_v0.1.3-internal_source.zip`（392 文件，19:43:14）

**证据（SHA / 字节比对）**

| 文件 | 候选 zip | 发布暂存 | 当前源码 | 结论 |
|---|---|---|---|---|
| `routes.py` | 61,764 B（`8292060d…`） | 64,530 B（`4ef739bf…`） | 66,850 B（`a16efdb7…`，20:40） | **三者都不同**；暂存/ zip 均**不含** 20:40 的 loopback/同源修复 |
| `hardware_adapt.py` | 26,662 B | 27,276 B | 27,276 B | zip 旧；暂存=源码 |
| `dramacore_director_ui.js` | 旧 | 168,849 B（20:01） | 169,074 B（20:41） | 暂存/zip 均旧 |
| `install/`（5 文件） | **0 项** | 有 | 有 | zip **缺安装器** |
| 根 `LICENSE`/`README.md`/`requirements.txt` | **0 项** | 有 | 根目录本身没有 | zip **缺 AGPL/说明/依赖** |
| `docs/ROUND58_RELEASE_v0.1.3-internal.md` | 不在包内 | 未跟踪，但有 | 在暂存 docs | zip 无发布说明 |
| `__init__.py` 版本 | `0.1.2-internal` | `0.1.3-internal`（20:33） | `0.1.3-internal` | zip 版本过期 |

- zip 的 `manifest.json` 中 `git.head`/`git.branch`/`git.remote` 全为空 → 无来源可追溯。
- 结论：**当前没有任何构建物能代表 20:44 的源码**；直接发布 = 把 20:40 前的无鉴权写漏洞一起发出去。

**修复建议**：安全修复完成后，从当前工作区重建：`INCLUDE_DIRS` 加 `install`，`INCLUDE_FILES` 加 `LICENSE`/`README.md`/`requirements.txt`/`THIRD_PARTY_NOTICES.md`，`docs` 用前缀白名单并排除内部审计/含 IP 文档；生成带源码指纹的 manifest；版本统一 `0.1.3-internal`；重建后再跑本报告 §13 的比对命令。

---

### P0-4　发布机 `%TEMP%` 仍有明文 GitHub 密码与 SSH 私钥（不在仓库，但在发布流程上）

**位置 / 类型（不写值）**
- `%TEMP%\dramacore_ed25519`：419 B，**OpenSSH 私钥**（`[REDACTED:OPENSSH-PRIVATE-KEY]`）；同目录有 `.pub`
- `%TEMP%\bcu_github_automation.py`、`%TEMP%\bcu_github_ssh.py`：各含 1 处 `password = "<11 位>"` 字面量（前缀 `12`，非 `YOUR/xxx/***` 占位符）
- `%TEMP%\bcu_cfg_backup\user.yaml`：含 `sk-acc…(len=39)`；与 `docs/ROUND25_逻辑对照问题清单.md:16` 记录的“验收测试假密钥（len=39，`sk-acc` 前缀）”一致，但**未验证真伪前按真实凭据处理**
- `%TEMP%\bcu_ssh_env.txt`：100 B，含远端主机/用户/端口
- 同目录还有大量 `bcu_*.log`/`bcu_*.json`（历史运行路径/命令），不宜随机器交接

**证据**：`docs/ROUND58_发布预检清单.md:340` 已将“删 `%TEMP%\dramacore_ed25519*`、`ssh_pass.txt`”列为第 18 步；本轮实测这些文件**仍存在**。

**修复建议（发布前必须完成）**
1. 需要的值转存密码管理器后，安全删除上述 TEMP 文件；
2. **立即更换 GitHub 密码**、启用 2FA、改用短期 PAT（最小 `Contents: Read and write`）；
3. GitHub → Settings → SSH keys 确认对应公钥已删除/吊销，再删本地私钥；
4. 若 `bcu_cfg_backup/user.yaml` 的 `sk-` 值可能为真，轮换 DeepSeek Key；
5. 清理 `dist/` 历史 bundle 与旧 `bcu_r58_packtest` 包（发布只用最终重建包）。

---

## 2. P1（发布前应修，或明确风险接受）

### P1-1　节点 `api_key` 为普通 STRING widget → 进工作流 JSON 与 `/history`

- **位置**：`bettercomfyui_comfy/one_stop_node.py:837`（`optional` 中 `"api_key": ("STRING", {"default": ""})`，非 `forceInput`）；使用处 `one_stop_node.py:980,1101`；`settings_node.py:15-28` 自己承认 `/history` 会记录输入输出。
- **证据**：只读 `GET /history`（无鉴权）返回 21 条历史，其中 **2 条**的 `prompt[2].3.inputs.api_key` 非空（长度 8）——证明用户填过的值会进 history；随包 workflow 未含 Key（扫描 0 命中）。
- **影响**：用户在节点里填 Key（tooltip/文档允许）→ 保存/分享工作流、或他人读 `/history` 即泄露。
- **修复**：该输入改 `forceInput`（只能从「API 配置」节点连线）；UI 去掉可直接输入的 Key 框；发布说明提示清理 `/history`。

### P1-2　仍有未加保护的写/执行路由：`/rollback/apply`、`/project/open`、`/run`

- **位置**：`routes.py:1533`（rollback/apply）、`routes.py:1540`（project/open）、`routes.py:1541`（run）——均**未**调用 `_panel_sensitive_guard`。
- **影响（能访问端口即可）**：
  - `/rollback/apply`：把素材“当前 LOCK”改回旧版本、写 `_work/rollback_log.jsonl`（可伪造 `operator=panel`），破坏审核完整性；
  - `/project/open`：创建项目目录（名称/ID 受 `ProjectStore` 校验，**无路径穿越**，但有磁盘写入）；
  - `/run`：`executor` 被限为 `mock`（`routes.py:354-367`），不会真实出片，但会建项目、写占位媒体。
- **修复**：与 P0-1 同一 token 中间件；或至少对写接口套 `_panel_sensitive_guard`。

### P1-3　`/panel/test` 的本机保护可被部署方式绕过，且 SSRF 仅拦云元数据

- **现状（20:40 后）**：`routes.py:643-669` 只允许 `request.remote ∈ {127.0.0.1, ::1, localhost}`（可用 `BETTERCOMFYUI_ALLOW_REMOTE_PANEL=1` 关闭保护）；`routes.py:930-948` 对“请求体新 endpoint”要求显式自带 Key，**不再把已保存 Key 发往新地址**；`routes.py:802-813` 仅拦 `169.254.169.254`/`metadata.google.internal` 等 4 个元数据主机。
- **残余风险**：
  1. **反向代理 / SSH 隧道**：仓库自带 `scripts/bcu_tunnel.py` / `bcu_tunnel2.py`，文档也常以隧道访问；一旦服务在本机反代后面，`request.remote` 恒为 `127.0.0.1`，保护**完全失效**；
  2. `BETTERCOMFYUI_ALLOW_REMOTE_PANEL=1` 一旦设置（排障时常见），远程可再次 SSRF；
  3. 未拦私网/环回地址（本地 LLM 场景有意放行）→ 可探测 `127.0.0.1:*`、`192.168.*`、`10.*` 的其他服务；
  4. `/settings`（P0-1）可先把 `endpoint` 改成攻击者地址，使“已配置 endpoint”判断通过。
- **修复**：不要把“是否本机”当认证；改用 token；SSRF 目标做私网/环回黑名单（本地 LLM 用显式 allowlist）；反代场景要求反代注入认证头。

### P1-4　远程安装脚本以 root 启动/重启 ComfyUI 且绑 `0.0.0.0`，并 `bash -c` 执行进程命令行

- **位置**：`scripts/远程安装巨人.sh:96`、`:104`（默认 `main.py --listen 0.0.0.0 --port 8188`）、`:108`（`nohup bash -c "$args"`，`args` 来自 `ps -o args=`）、`:99-101`（强杀旧进程）。
- **影响**：把无鉴权面暴露到网络；以 root 跑 ComfyUI；把“当前进程命令行”当命令执行（本机其他用户若能影响该命令行，等价 root 命令执行）。
- **修复**：默认 `--listen 127.0.0.1`；非 root 专用用户；不要 `bash -c "$args"`，改固定 argv；脚本不进公开包或标注“仅维护者内部使用”。

### P1-5　候选发布包缺根 `LICENSE`（AGPL）/`README.md`/`requirements.txt`

- **证据**：候选 zip 392 项中根级只有 `.gitignore` 与目录；`LICENSE` 仅命中 `vendor/h3_director/LICENSE`；`README`/`requirements.txt` 仅 vendor。发布暂存 20:24 已补上这三个文件，但**尚未进 zip**。
- **影响**：AGPL-3.0 分发要求随附许可证与源码获取方式；Commons Clause 通知未随包；用户无法按发布说明执行 `pip install -r requirements.txt`。
- **修复**：打包白名单显式包含；发布说明写明 AGPL+Commons Clause 与商用限制。

### P1-6　vendor 许可证误标：文档写 MIT，实际 Apache-2.0，且无 NOTICE/归属说明

- **证据**：`_release/DramaCore/docs/ROUND58_RELEASE_v0.1.3-internal.md:167`（及 `:221`）写“保留其原始 MIT LICENSE”；`bettercomfyui_comfy/vendor/h3_director/LICENSE` 首行为 **Apache License 2.0**；`vendor/h3_director/README.md:242` 写 `Apache-2.0`；`pyproject.toml:5` `license = {file = "LICENSE"}`；全仓无 `NOTICE`/`THIRD_PARTY`。
- **修复**：发布说明改 Apache-2.0；补 `THIRD_PARTY_NOTICES.md`（名称、版本、许可、来源 URL、修改说明）；核对上游 NOTICE 并保留。

### P1-7　发布内容泄露真实服务器公网 IP 与本机路径/用户名

- **证据**：
  - 源码/staging `docs/` 中 **13 篇**文档含 `your-gpu-host`（`ROUND36/37/41/42/43/44/45/47/49`、`RESUME.md`、`ROUND20`、`单节点_剧本到成片_使用说明.md`、`剧核_发布评估.md`），部分带 `user@`/SSH 端口上下文；
  - 候选 zip 的 21 篇 docs 中 3 篇含该 IP（ROUND45/47/49）；
  - `config/user.yaml.example` 含本机绝对路径 `H:\…\Better comfyUI\.comfyui`（含用户名 `minzzj`）；
  - 根 `.gitignore` 未覆盖 `remote_reports/`、`docs/remote_evidence_4090/`、`保留备用的节点/`、`文档/`、`*.docx`、`comfyui_*.log`、`_release/`（`dist/` 只忽略 `dist/*.zip`）。
- **修复**：打包前脱敏（IP→`<GPU_HOST>`、端口/用户名→占位符）；`user.yaml.example` 清空 `comfyui_base_path`；扩充 `.gitignore`；只从干净导出目录打包。

### P1-8　成片输出文件名未消毒 → 路径穿越/任意文件覆盖

- **位置**：`bettercomfyui_comfy/finalize_nodes.py:256-259`：`final_name = (输出文件名 or "").strip() or f"成片_{ts}.mp4"`；仅补 `.mp4` 后缀（`:257-258`）；`final_path = Path(paths.output_dir) / final_name`（无 `..`/分隔符校验）。
- **影响**：该节点可由 `/prompt`（ComfyUI 默认无鉴权）提交；`输出文件名="../../config/user.yaml"` 等可让 ffmpeg 覆盖项目外文件（内容为视频字节 → 配置/代码损坏、DoS）。
- **修复**：`final_name = Path(final_name).name`；拒绝 `/`、`\`、`..`；强制后缀白名单；`resolve()` 后校验父目录等于 `output_dir`。

---

## 3. P2（加固项，建议本轮一并处理）

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| P2-1 | `routes.py:147-263`（`/doctor`）、`routes.py:1351-1369` + `hardware_adapt.py:566-588`（`/hardware`） | 返回绝对路径（orchestrator_root、ffmpeg、project_root）、版本、设备名/显存、`env`（分配器配置）、`evidence`（smi 原始行）、comfy-kitchen 扫描路径 | 面板只返回必要字段；路径相对化/脱敏；`evidence` 仅在本地调试开关返回 |
| P2-2 | `routes.py:1357-1361` + `hardware_adapt.py:482-496` | `GET /hardware?refresh=1` 无鉴权即可触发 5–6 次子进程探测 + 最多 200 个 py 文件扫描 + 运行时 monkeypatch → 资源放大/DoS | 加 token；`refresh` 限流（如 30s 一次）；探测加缓存 |
| P2-3 | `routes.py:1326-1343` | `/prompt/handbook` 无鉴权返回 70KB 全文，可被批量抓取/耗带宽 | 随 P0-1 加 token；预设白名单**已通过**（无文件路径注入） |
| P2-4 | `api_settings.py:101-108` | `mask_secret` 回显前 3+后 4 位（35 位 Key 泄露 8 位），`/settings` 无鉴权可读；叠加 P0-1 可远程读任意环境变量的 8 字符 | 只回 `sk-***` 或“已配置/来源”；不要回尾 4 位 |
| P2-5 | `install/install_core.py:903-908`；`requirements.txt`（暂存） | `--torch-index` 可指向任意 pip 源；依赖仅 `>=` 无上限/无 hash；本机无 `pip-audit` | 锁定版本+hash；`--torch-index` 仅允许官方域名；发布前离线/联网漏洞扫描 |
| P2-6 | `install/install_core.py:477-482` | Windows 启动器 `set "{key}={value}"`：env 值/仓库路径含 `"` 或 `&` 时 BAT 被截断/注入（本地执行） | 转义或改用配置文件读取 |
| P2-7 | `scripts/ssh_verify.py:34-47,69,74,100` | 远端命令 f-string 拼 `mkdir/cd`（`--remote-dir` 可注入远程 shell）；`host` 以 `-` 开头可注入 ssh 选项；`StrictHostKeyChecking=accept-new` 首次连接可 MITM | `shlex.quote`；host 校验 `^[A-Za-z0-9.-]+$`；知道指纹后改 `yes` |
| P2-8 | `scripts/publish.py:36,74-90` | 固定从 `%TEMP%\bcui_stage` 复制进仓库（可预测路径，本机其他进程可投放文件替换源码） | 改为仓库内/带校验暂存；或删除该脚本 |
| P2-9 | `vendor/h3_director/director/http_routes.py:127-190,630-680` | vendor 自带未鉴权上传/下载/导入路由（chunk 上传写 `input/`、`import_pack` 解包） | 目前 vendor `__init__.py` **未调用** `register_routes()`（未注册，潜在）；若用户把 vendor 当独立节点安装则暴露，建议 README 标注 |
| P2-10 | `config/user.yaml`、`comfy.settings.json`、巨人 `settings.json` | 密钥明文落盘，未见文件权限收紧（Windows ACL 继承） | 默认走环境变量；写盘后 `chmod 600`/ACL 限制 |
| P2-11 | 候选 zip `docs/`（21 篇） | 含 `ROUND50-A_愿景与流程图_全量代码审查.md`、`ROUND54-A/B`、`ROUND58_发布预检清单.md` 等内部审计/已知缺陷清单 | 公开包只放用户文档+发布说明+许可证；内部审计移出 |
| P2-12 | `.comfyui/server.py:281-286` + `routes.py` | 默认 `origin_only_middleware` 可挡跨源；但一旦 `--enable-cors-header`，任意网页可跨源读写无鉴权接口 | 文档不要建议 CORS；token 落地后即使 CORS 也不可跨源利用 |
| P2-13 | `routes.py:636-669` | 本机判定用 `request.remote`，无 token；反代/隧道部署即失效（见 P1-3） | 与 P0-1 的统一 token 方案合并，不要依赖网络位置 |

---

## 4. 全部 HTTP 路由与鉴权矩阵

注册方式：`routes.py:1503-1508` 的 `_both` 对每个路径同时注册 `/bettercomfyui+path` 与 `/api/bettercomfyui+path`（18 个逻辑端点 / 36 条路由）。

| # | 方法 | 路径（双前缀） | 处理器 | 读/写/执行/网络 | 20:44 现状 |
|---|---|---|---|---|---|
| 1 | GET | `/doctor` | `routes.py:147` | 版本/节点/路径/模型/设备 | ❌ 无保护（P2-1） |
| 2 | GET | `/hardware` | `routes.py:1351` | 硬件/补丁/环境/路径；`refresh=1` 触发探测 | ❌ 无保护（P2-1/P2-2） |
| 3 | GET | `/projects` | `routes.py:266` | 项目列表（编排包） | ❌ 无保护 |
| 4 | GET | `/settings` | `api_settings.py:334` | 读配置（遮罩 Key、绝对路径） | ❌ 无保护（P2-4） |
| 5 | **POST** | `/settings` | `api_settings.py:358` | **写** `user.yaml` + 同步原生/巨人设置 | ❌ **无保护（P0-1）** |
| 6 | GET | `/projects/list` | `routes.py:1138` | 项目摘要 + `root` 绝对路径 | ❌ 无保护（P1 枚举） |
| 7 | GET | `/project/context` | `routes.py:1162` | 资产/提示词/成片清单 | ❌ 无保护（P1） |
| 8 | GET | `/asset/preview` | `routes.py:1176` | **读**项目内图片 | ❌ 无保护；路径包含校验**通过** |
| 9 | GET | `/prompt/handbook` | `routes.py:1326` | 70KB 预设全文 | ❌ 无保护；白名单**通过** |
| 10 | GET | `/rollback/list` | `routes.py:1266` | 版本历史 | ❌ 无保护 |
| 11 | **POST** | `/rollback/apply` | `routes.py:1285` | **改**素材当前版本 + 审计 | ❌ 无保护（P1-2） |
| 12 | GET | `/panel/config` | `routes.py:627` | 读配置（遮罩） | ❌ 无保护（仅读） |
| 13 | **POST** | `/panel/config` | `routes.py:672` | **写** API/提示词/UI（200k） | ✅ 本机+同源保护（`routes.py:674`）；`api_key_env` 白名单 |
| 14 | **POST** | `/panel/test` | `routes.py:904` | **网络**：向指定 endpoint 发 Bearer | ⚠️ 本机+同源保护（`:910`）；自定义 endpoint 需自带 Key；**反代/开关可绕过**（P1-3） |
| 15 | GET | `/workflow/minimal` | `routes.py:421` | 读插件内 JSON | ❌ 无保护（固定文件，低危） |
| 16 | GET | `/phase2/status` | `routes.py:438` | 阶段状态 | ❌ 无保护（低危） |
| 17 | **POST** | `/project/open` | `routes.py:292` | **创建/打开**项目（写目录） | ❌ 无保护（P1-2） |
| 18 | **POST** | `/run` | `routes.py:343` | **执行** `demo.run_demo`（仅 mock） | ❌ 无保护（P1-2）；executor 限制**通过** |

> ComfyUI 上游自身路由（`/prompt`、`/history`、`/settings`、`/userdata/{file}`、`/upload/*`）默认无鉴权，是上述问题的放大器；本轮不改上游，但部署必须默认 `127.0.0.1`。

---

## 5. 密钥/凭据扫描结果（脱敏：只写位置/类型）

范围：源码、`docs/`、`workflows/`、`tests/`、`scripts/`、`install/`、`config/`、`_release/DramaCore`（含 `git log -p` 4 个 commit，约 5.2 MB diff）；共 965 个文本文件。规则：OpenAI 风格 Key、GitHub PAT、AWS AKID、私钥块、JWT、`user:pass@`、邮箱、IPv4、`password/token/secret` 赋值、`jygpu`。

| 类型 | 结果 | 处置 |
|---|---|---|
| 真实 LLM/云 Key | **0 处**（19 处 `sk-` 全为测试/文档假值：`sk-sec…(len=25)`、`sk-REA…(len=26)`、`sk-acc…(len=39/21)`） | 无需轮换；发布包不应带测试文件 |
| GitHub token/PAT | **0 处**；`_release` 远程为 `noreply@users.noreply.github.com:minzzj/DramaCore.git`（SSH，URL 无 token） | 保持 |
| SSH/私钥块 | 仓库/暂存 **0 处**；**`%TEMP%\dramacore_ed25519` 有 1 个 OpenSSH 私钥**（P0-4） | 删除并确认吊销 |
| 明文口令 | 仓库/暂存 **0 处**；**`%TEMP%\bcu_github_automation.py`、`bcu_github_ssh.py` 各 1 处 11 位口令字面量**（P0-4） | 改密+删除脚本 |
| `jygpu` token | 仓库 9 处均为平台名/URL（`jygpu.com`），**无 token**；会话文件在 TEMP，未进仓库 | 无需处理仓库；清理 TEMP |
| 邮箱 | 6 处：`noreply@users.noreply.github.com`（专用身份）、`noreply@users.noreply.github.com`、`@example` 占位 | 可接受 |
| 公网 IP | **13 篇 docs** 含 `your-gpu-host`（P1-7）；候选 zip 含 3 篇；`scripts/ssh_verify.py`/预检清单为占位 `your-gpu-host` | 脱敏后再打包 |
| 发布包禁入项 | 候选 zip：`config/user.yaml` 0、`.env` 0、`settings.json` 0、`.git/` 0、`__pycache__`/`.pyc` 0、`projects/` 0、`output/` 0、模型权重 0、日志 0、`_last_run` 0 | **通过** |
| 暂存 git 历史 | 4 个 commit；`config/user.yaml` 从未提交；`sk-` 命中 6 处均为假 Key | **通过** |

> 结论：**仓库文本内没有可用真实密钥**；真正的凭据风险在 `%TEMP%`（P0-4）与运行时写入的 `comfy.settings.json`（P0-2）。

---

## 6. 命令注入 / 路径穿越 / 任意文件读写

| 区域 | 结论 | 证据 |
|---|---|---|
| `install/install_core.py` | **未发现命令注入**：全部 `subprocess.run(argv)`、无 `shell=True`；git URL 为常量 `DEFAULT_COMFYUI_REPO`（`:48,546-549`）；pip 参数固定；`--dry-run` 不写盘/不联网（`:506-587,925-928`）；`--force` 先备份可回滚（`:725-753,778-813`）；`bootstrap` 拒非空目录（`:539-545`） | 通过 |
| `install_core.py` 启动器 | POSIX 用 `_shell_quote`（`:483-495`）；Windows BAT 的 env 值未转义（`:477-482`）→ P2-6 | 部分 |
| `--allow-experimental` | **不绕过安全限制**：仅放行 Windows+ROCm/Ascend 拒绝分支（`:778-790`），仍需 `--yes` | 通过 |
| `hardware_adapt.py` | 只读检测 + 幂等包裹 `is_nvidia/supports_fp8_compute`；子进程为固定探测命令 | 通过（`refresh=1` 可被无鉴权触发，P2-2） |
| `routes.py` 文件接口 | `/asset/preview` 三重校验（32 位 hex、`resolve()` 后必须在项目目录、图片后缀白名单）→ **穿越已挡**；`/rollback` 的 project/asset 走 `ProjectPaths`/索引；`/workflow/minimal` 固定文件 | 通过 |
| `finalize_nodes.py` | `输出文件名` 直接拼 `output_dir`，仅补 `.mp4` → **穿越/覆盖**（P1-8） | 不通过 |
| `scripts/ssh_verify.py` | 远端命令 f-string 拼接 + host 未校验（P2-7，运维脚本） | 部分 |
| `scripts/远程安装巨人.sh` | `bash -c "$args"`（来自 `ps`）+ root + `0.0.0.0`（P1-4） | 不通过 |
| 全仓 `shell=True`/`os.system`/`eval`/`pickle.loads` | **0 处**；`ast.literal_eval` 仅解析自有文本 | 通过 |

---

## 7. 新功能安全（ROUND58）

| 功能 | 结论 | 说明 |
|---|---|---|
| `GET /bettercomfyui/hardware` | ⚠️ 超出“机器声明” | 返回 `report`（`evidence` smi 原始行、`env`、`launch_args`、`source` 绝对路径）、`patches`、`preflight`、中文全文；无鉴权（P2-1）；`refresh=1` 可滥用（P2-2）。未发现密钥/用户名。 |
| `GET /bettercomfyui/prompt/handbook` | ✅ 基本安全 | `mode` 经 `h3_handbook.canonical_preset` 白名单（`h3_handbook.py:174-210`），只读固定 3 个数据文件，**无路径拼接**；仅内容抓取/带宽（P2-3）。 |
| 面板保存 200k 提示词 | ⚠️ 有风险 | 上限只防溢出；`/panel/config` 现已本机+同源保护，但**同机进程/反代/`ALLOW_REMOTE_PANEL=1`** 仍可写入，等于“可改所有生成行为”；`yaml.safe_dump` 本身无注入。 |
| 安装器 `--allow-experimental` | ✅ 通过 | 仅放行实验平台拒绝分支，仍需 `--yes`，不绕过文件/凭据/命令校验。 |
| JSON 契约（LLM→JSON） | ✅ 未发现注入 | `json.loads` + 字段校验（`prompt/validate.py`、`prompt/inject.py`），无 `eval`。 |
| 配置节点输出 Key | ⚠️ 设计权衡 | `settings_node.py:79-103` 从服务器读 Key 作下游连线值；连线在 `/history` 存引用而非明文；`_mask` 已遮罩（`:106-111`）。 |

---

## 8. 许可证 / 发布合规

| 项 | 状态 | 说明 |
|---|---|---|
| 主许可证 | ⚠️ 文件存在但**未随 zip** | `_release/DramaCore/LICENSE` 为 AGPL-3.0 全文 + Commons Clause v1.0（36 KB，含 `GNU AFFERO GENERAL PUBLIC LICENSE Version 3` 与 3 处 Commons Clause）；候选 zip **不含**根 LICENSE（P1-5）。 |
| Commons Clause 商用限制 | ✅ 已在 LICENSE/发布说明声明 | 但**不是 OSI 开源**，需 README/发布页显著说明“禁止商用、商业授权另谈”。 |
| vendored `h3_director` | ❌ 文档误标 | 实为 **Apache-2.0**（`vendor/h3_director/LICENSE`、`README.md:242`），发布说明写 MIT（P1-6）；无 NOTICE/THIRD_PARTY。 |
| 第三方依赖许可 | ⚠️ 未逐项核对 | 主依赖 PyYAML/Pillow/numpy/imageio-ffmpeg/aiohttp 均宽松许可；vendor 依赖未逐项审；模型权重不在包内（正确）。 |
| 依赖锁定/漏洞 | ⚠️ 未覆盖 | 仅 `>=` 下限、无 hash；本机无 `pip-audit`，未联网做 CVE 比对。 |
| 禁止商用提示 | ⚠️ 仅在发布说明（未随 zip） | 需 README + 包内 LICENSE + 发布页三处一致。 |

---

## 9. 发布包/暂存区核查

| 对象 | 状态 | 关键事实 |
|---|---|---|
| `_release/DramaCore` | ⚠️ 已同步但**落后安全修复** | `routes.py` 64,530 B / 20:24；源码 66,850 B / 20:40（缺 loopback/同源修复）；UI 20:01 vs 源码 20:41；`install/`、`LICENSE`、`README`、`requirements.txt`、`config/default.yaml` 已补；版本 0.1.3；git 大量未提交 |
| `%TEMP%\bcu_r58_packtest\*.zip` | ❌ 半成品 | 392 文件/2.0 MB，19:43 构建；`routes.py` 61,764 B（更旧）、**无 install/**、**无根 LICENSE/README/requirements**、**无发布说明**、版本 0.1.2、`git.head` 为空 |
| `dist/*.zip`（32 个历史 bundle） | ⚠️ 不应发布 | 由 `scripts/prepare_remote_bundle.py` 生成，含 `tests/verify/_last_run.json`（53 KB，本机路径/运行证据）、`config/user.yaml.example`（本机路径） |
| 包内禁入项 | ✅ | 候选 zip 无 user.yaml/`.env`/`settings.json`/`.git`/`__pycache__`/`projects`/`output`/模型/日志/`_last_run` |

---

## 10. 发布前必须处理清单（按顺序）

1. **修 P0-1**：`/settings` POST 加 `_panel_sensitive_guard` + `api_key_env` 白名单；全组 `/bettercomfyui/*` 统一本地 token。
2. **修 P0-2**：停止把密钥同步到 `comfy.settings.json`/巨人 `settings.json`；给出清理旧值方法；文档提示轮换。
3. **修 P1-1**：`one_stop_node.py:837` 的 `api_key` 改 `forceInput`；发布说明提示“不要在节点里填 Key”。
4. **修 P1-8**：`finalize_nodes.py` 文件名消毒（`Path(name).name` + 父目录校验）。
5. **修 P1-2/P1-3/P1-4**：剩余写路由加保护；不要把 `request.remote` 当认证；`远程安装巨人.sh` 默认 `127.0.0.1`、非 root、去掉 `bash -c "$ps"`。
6. **合规**：根目录补 `LICENSE`/`README.md`/`requirements.txt`/`THIRD_PARTY_NOTICES.md`；发布说明 MIT→Apache-2.0；包内带许可证与商用限制。
7. **脱敏**：13 篇含真实 IP 的 docs、`user.yaml.example` 本机路径；扩充 `.gitignore`；`docs` 打包改前缀白名单并排除内部审计。
8. **版本/构建**：确认 `__version__ = 0.1.3-internal`；从当前源码重建包（含 `install/`、许可证、说明、requirements），生成带源码指纹的 manifest。
9. **清理发布机**：删除 `%TEMP%\dramacore_ed25519*`、`bcu_ssh_env.txt`、`bcu_github_*.py`、`bcu_cfg_backup*`、旧 `bcu_r58_packtest`/`dist` 历史包；更换 GitHub 密码、吊销 SSH key。
10. **回归**：跑 `tests/` 与安装器 `--dry-run`；用本报告 §13 命令复核包内容/版本/差异。
11. **门禁化**：把 P0/P1 做成自动检查（`/settings` 有 guard、包内必须有 LICENSE/README/requirements、版本一致、包与源码 SHA 一致、包内无公网 IP/本机路径、无 `%TEMP%` 凭据）。

> 以上 11 项全部完成前，不应 push / 打 tag / 上传 Release / 对外分发。

---

## 11. 已通过项（本轮验证）

- 仓库文本内**未发现真实密钥/口令/私钥**（19 处 `sk-` 均为测试/文档假值；`git log -p` 4 个 commit 仅 6 处假 Key）；`config/user.yaml` 的 `api_key` 当前为空。
- `git` 远程为 SSH 形式，URL 内无 token；`config/user.yaml` 从未进入暂存 git 历史。
- 候选 zip 未带入 `user.yaml`/`.env`/`settings.json`/`.git`/`projects`/`output`/模型/日志/`__pycache__`。
- `install_core.py` 无 `shell=True`/`os.system`；git 源为常量、pip 参数为列表、`--dry-run` 无副作用、`--force` 备份+回滚、`bootstrap` 拒绝非空目录；`--allow-experimental` 仅放行显式实验路径。
- 全仓无 `eval`/`exec`/`pickle.loads`/`shell=True`；`ast.literal_eval` 使用安全。
- `/asset/preview` 路径穿越防护正确（32 位 project_id + `resolve()` + parents + 后缀白名单）。
- `/rollback` 的 project_id/asset 走 `ProjectPaths` 与索引解析，无路径拼接注入；有 hash 门禁与审计日志。
- `/run` 拒绝真实执行器（`executor=comfyui` 直接 400），只允许 mock。
- `/prompt/handbook` 预设白名单，无任意文件读取。
- `/panel/test` 已加本机/同源保护、拒绝把已保存 Key 发往新 endpoint、拦云元数据地址（20:40 修复，方向正确；残余见 P1-3）。
- `/panel/config` 已加本机/同源保护 + `api_key_env` 白名单（20:40 修复；但 `/settings` 未同步，见 P0-1）。
- 版本号已升 `0.1.3-internal`（20:33）。
- 日志/导出脱敏存在：`logging_setup.py:50,135-158`（`api_key`→`***`）、`config.py:307-326`（dump 清空 key）、`projects.py:115-117`（快照清 key）、`cli.py:582`（导出脱敏）、`gateway.py:306-315`（describe 不含 key）。
- 遮罩不回传完整 Key（`api_settings.py:101-108`），仅尾 4 位略多（P2-4）。

---

## 12. 未覆盖项 / 限制

1. **未实际利用**：没有远程 POST `/settings`、没有向外部地址发 `/panel/test`、没有外带密钥（避免副作用）；P0-1 结论来自代码路径 + 只读端点探测。
2. **并发修改**：审计期间 `routes.py`/`api_settings.py`/UI 被改动，本报告以 20:44 快照为准；发布前需对最终源码重跑。
3. **无网络**：未查 CVE/公告，未安装 `pip-audit`；依赖漏洞面未评估。
4. **无 GPU/NPU**：`hardware_adapt.ensure_runtime_patches` 的 monkeypatch、`refresh=1` 实际耗时、昇腾/ROCm 路径未真机验证。
5. **vendor 全量未逐行审**：`vendor/h3_director/`（约 90 个 py）只审路由注册、许可证、文件读写入口；`http_routes.py` 因未注册判为潜在风险。
6. **前端 XSS 未确认**：`dramacore_director_ui.js:402` 有 `el.innerHTML = v` 通用 helper，是否用于项目名/服务端字符串未完全追踪；建议单独做 DOM 注入审计。
7. **ComfyUI 上游路由**（`/prompt`、`/history`、`/settings`、`/userdata`）不属本仓库，未加固；它们是 P0-2/P1-1 的放大器。
8. **第三方节点**（`ComfyUI-Storyboard-LLM` 等）只做密钥落点检查，未审其路由/文件读写。
9. **发布机非仓库文件**（TEMP 下大量 `bcu_*`）只抽样检查凭据相关文件，未逐个审计。

---

## 13. 附录：可复现的只读检查命令

```powershell
# 1) 路由与保护覆盖（应看到 18 个 _both；_panel_sensitive_guard 目前只有 2 处调用）
Select-String -Path bettercomfyui_comfy\routes.py -Pattern '_both\(|_panel_sensitive_guard'
# 2) /settings 是否受保护（应输出 False，即仍是无保护写接口）
Select-String -Path bettercomfyui_comfy\api_settings.py -Pattern '_panel_sensitive_guard|request.remote|Origin'
# 3) P0-2：密钥落点（仍会把 Key 写入 comfy.settings.json）
Select-String -Path bettercomfyui_comfy\api_settings.py -Pattern '_sync_native_comfy_setting|COMFY_SETTINGS_FILE'
# 4) 只读确认 ComfyUI 无鉴权可读设置（200 即风险成立；不要打印内容）
python -B -c "import urllib.request as u; [print(p, u.urlopen('http://127.0.0.1:8188'+p, timeout=5).status) for p in ['/settings','/userdata/comfy.settings.json','/history']]"
# 5) 发布包与源码差异（应全部 SAME 才可发布）
python -B -c "import zipfile,hashlib,pathlib; z=zipfile.ZipFile(r'%TEMP%\bcu_r58_packtest\dramacore_v0.1.3-internal_source.zip'); [print(n, hashlib.sha256(z.read(n)).hexdigest()[:12]) for n in z.namelist() if n.endswith(('routes.py','hardware_adapt.py','install_core.py'))]"
# 6) 包内缺件/禁入项（LICENSE/README/requirements/install 应为 1+，禁入项应为 0）
python -B -c "import zipfile; z=zipfile.ZipFile(r'%TEMP%\bcu_r58_packtest\dramacore_v0.1.3-internal_source.zip'); names=z.namelist(); print({k:sum(k in n for n in names) for k in ['LICENSE','README','requirements.txt','install/','user.yaml','.env','settings.json','.git/','__pycache__','projects/','output/','.log','_last_run']})"
# 7) 掩码密钥扫描（只输出文件，不输出值）
python -B -c "import re,pathlib; pat=re.compile(r'sk-[A-Za-z0-9_-]{16,}|ghp_REDACTED[A-Za-z0-9]{30,}|BEGIN OPENSSH PRIVATE KEY'); [print(p) for p in pathlib.Path('.').rglob('*') if p.is_file() and p.suffix in {'.py','.md','.json','.yaml','.sh'} and pat.search(p.read_text('utf-8','ignore'))]"
```

---

**审计人**：ROUND58 独立专项安全审计子智能体（只读审计；未改源码/配置、未发布、未 push、未重启服务）。
**快照指纹**：`routes.py a16efdb76a`（66,850 B / 20:40:12）、`api_settings.py 16,280 B / 20:39:48`、`__init__.py 0.1.3-internal / 20:33:30`、`dramacore_director_ui.js 169,074 B / 20:41:02`；暂存 `routes.py 4ef739bf`（64,530 B / 20:24:42）；候选 zip sha256 `979e9c23…`（19:43:14）。
**结论**：**NO-GO**。P0=4、P1=8、P2=13。
