# ROUND58 发布预检清单（v0.1.3-internal → minzzj/DramaCore）

> **执行者**：ROUND58 专项子智能体（上下文隔离）
> **时间**：2026-10-20
> **边界**：本清单只做**只读盘点 + 文档 + 打包/校验脚本（生成在 `%TEMP%`）**。
> **未执行**：`git push` / `git tag` / GitHub Release / 任何网络写操作；**未修改**任何源码功能文件；**未重启**服务。
> **结论**：当前 `_release/DramaCore` **不能直接发布**。存在 **4 个 P0 阻塞项**（§8），必须先同步源码并重新生成工作流，才能进入打包。

---

## 0. 结论速览

| 项目 | 状态 | 说明 |
|---|---|---|
| staging 仓库可读 | ✅ | `main` 分支，HEAD `97d0409`；原为干净工作区，本次预检新增 1 个**故意未提交**的 Release notes 草稿（见附录 A.3） |
| 现有 tag | ✅ | `v0.1.0-internal` / `v0.1.1-internal` / `v0.1.2-internal`（3 个） |
| staging 内容与源码一致 | ❌ **严重滞后** | HEAD 停留在 **ROUND54**；ROUND55/56/57 全部缺失 |
| 单节点工作流与节点 schema 对齐 | ❌ **P0-A** | staging 工作流 112 个 widget 槽位，源码 **119** → 参数整体错位 |
| `config/default.yaml` | ❌ **P0-B** | staging 完全没有该文件，但 6 处代码在运行时读取它 |
| 敏感信息（Key/Token/密码/邮箱/公网 IP） | ✅ 干净 | 全量扫描无命中（详见 §3） |
| 项目数据/模型/成片混入 | ✅ 干净 | 无 `projects/` `output/` `models/` `config/user.yaml` |
| 版本号常量 | ⚠️ 待改 | `bettercomfyui_comfy/__init__.py` 仍为 `0.1.2-internal` |
| 发布凭据通道 | ⚠️ 可用但不卫生 | 方法可复用；存在明文密码残留与失效私钥残留（§4） |
| 许可证 | ✅ | AGPL-3.0 + Commons Clause，`LICENSE` 已随包 |

---

## 1. `_release/DramaCore` 盘点

### 1.1 git 状态（remote 已脱敏）

```
remote origin (fetch/push) : noreply@users.noreply.github.com:minzzj/DramaCore.git     ← SSH 形式，URL 内不含 token
branch                     : main（另有 remotes/origin/main）
HEAD                       : 97d0409ef89de19192a95e5c70118e000fa0089c
author                     : DramaCore <noreply@users.noreply.github.com>（专用发布身份，非个人邮箱）
commit time                : Mon Oct 19 19:26:11 2026 +0800
subject                    : feat(round54): multi-script sequential generation, asset limit 64, logic-audit fixes
worktree                   : clean（git status --porcelain 为空）
tags                       : v0.1.0-internal / v0.1.1-internal / v0.1.2-internal
```

提交历史（4 条，全在 main）：

```
97d0409  feat(round54): multi-script sequential generation, asset limit 64, logic-audit fixes
051f56c  fix(round53): align defaults, real human-review wait, 4-dim asset scoring, wire 4 config fields, stage checkpoints
49aad2f  release: add requirements.txt, unify version v0.1.0-internal, add ROUND50-B audit/eval
8dae92f  DramaCore v0.1.0-internal: single-node script-to-movie ComfyUI plugin (internal test build)
```

> `main` 只有 4 个 commit，说明 ROUND55/56/57 的成果**一次都没有进过 git**，全部只存在于工作区源码树。

### 1.2 内容构成

| 指标 | 值 |
|---|---|
| 受版本控制文件数 | 363 |
| 磁盘文件数（不含 `.git`） | 363（无未跟踪/未忽略文件） |
| 总体积 | **5.19 MB** |
| `.git` 体积 | 2.03 MB |
| 最大单文件 | `bettercomfyui_comfy/one_stop_node.py` 169 KB |
| 目录 | `bettercomfyui/`、`bettercomfyui_comfy/`、`scripts/`、`tests/`、`config/`、`docs/` |
| 工作流 | 仅 1 个：`00_【单节点】剧本→成片（无外部依赖）.json` |
| 文档 | 12 个（ROUND45/46/47/49/50-A/50-B/53/54/54-A/54-B/54-C + 发布说明） |
| 顶层文件 | `README.md`、`LICENSE`、`requirements.txt`、`.gitignore` |

### 1.3 是否含 secrets / 用户配置 / 项目数据 / 模型 / 成片

| 检查项 | 结果 |
|---|---|
| `config/user.yaml`（含 API Key） | ✅ **不含**，只有 `config/user.yaml.example`（229 B 模板） |
| `config/user.yml` / `.env` / `secrets.*` | ✅ 不含 |
| `projects/`（用户项目与素材） | ✅ 不含 |
| `output/`（成片） | ✅ 不含 |
| `models/`、`*.safetensors` `*.pt` `*.pth` `*.gguf` | ✅ 不含 |
| 视频/图片/音频（`*.mp4` `*.mov` `*.png` `*.jpg` `*.wav`） | ✅ 不含 |
| 日志（`*.log`）、`__pycache__`、`.pytest_cache` | ✅ 不含 |
| `dist/`（历史 bundle） | ✅ 不含 |

> 结论：**包体干净**，这是历次发布脚本 `bcu_stage_release.py` 的「白名单复制 + 精简 workflows」策略起了作用。

---

## 2. 与源码的差异（发布质量的核心）

### 2.1 逐文件哈希比对（staging 的 363 个受控文件 vs 当前源码树）

| 类别 | 数量 |
|---|---|
| 与源码**完全一致** | 339 |
| 与源码**内容不同** | **21** |
| staging 独有（源码无此文件） | 3（`README.md` / `LICENSE` / `requirements.txt` —— 这三个是发布侧生成的，**属正常**） |

**内容不同的 21 个**（均为「staging 旧、源码新」方向）：

```
.gitignore
bettercomfyui/config.py
bettercomfyui/prompt/inject.py
bettercomfyui_comfy/director_edit.py
bettercomfyui_comfy/director_runtime.py
bettercomfyui_comfy/everything_node.py
bettercomfyui_comfy/finalize_nodes.py
bettercomfyui_comfy/one_stop_node.py            ← ROUND55/56 主要改动落点
bettercomfyui_comfy/routes.py                   ← ROUND55 回滚路由
bettercomfyui_comfy/vendor/h3_director/director/core_sampling.py
bettercomfyui_comfy/vendor/h3_director/director/executor_core.py
bettercomfyui_comfy/vendor/h3_director/director/spatial_tiled_sampling.py
bettercomfyui_comfy/vendor/h3_director/nodes/director.py
bettercomfyui_comfy/web/js/dramacore_director_ui.js    ← ROUND56 多段剧本/提示词面板
bettercomfyui_comfy/workflows/00_【单节点】剧本→成片（无外部依赖）.json   ← 见 P0-A
scripts/collect_onestop_evidence.py
tests/test_round53_fixes.py
tests/test_store_boundaries.py
docs/ROUND45_导演台UI移植.md
docs/ROUND47_真机全链与UI移植_逻辑对照.md
docs/ROUND49_新手可见UI与项目管理.md
```

> 注意最后 3 个 docs：**源码侧在 ROUND54 之后又改过这几份历史文档**，同步时应以源码为准覆盖。

### 2.2 源码有、staging 完全缺（46 个文件）

**必须补进去（发布必需）18 个：**

| 文件 | 大小 | 所属 | 作用 |
|---|---|---|---|
| `bettercomfyui/pipeline/batch_continuity.py` | 18.6 KB | R55 | 跨批尾帧连续性 |
| `bettercomfyui/pipeline/hard_subtitles.py` | 35.8 KB | R55 | 硬字幕烧录 |
| `bettercomfyui/pipeline/intro_outro.py` | 28.6 KB | R55 | 片头片尾 |
| `bettercomfyui/pipeline/transition_planner.py` | 24.9 KB | R55 | 规则引擎转场规划 |
| `bettercomfyui/store/rollback.py` | 29.4 KB | R55 | 素材版本回滚后端 |
| `bettercomfyui/prompt/h3_manual.py` | 20.5 KB | R55 | H3 手册注入 |
| `bettercomfyui/prompt/h3_handbook.py` | 3.7 KB | R56 | H3 手册全文入口 |
| `bettercomfyui/prompt/data/h3_handbook_full.txt` | 68.3 KB | R56 | 手册全文（系统提示词素材） |
| `bettercomfyui/hardware.py` | 22.4 KB | R57 | 硬件检测/适配矩阵/自检 CLI |
| `config/default.yaml` | 2.2 KB | — | **P0-B**，见 §2.4 |
| `tests/test_round55_batch_continuity.py` | 11.6 KB | R55 | 单测 |
| `tests/test_round55_intro_outro.py` | 11.0 KB | R55 | 单测 |
| `tests/test_round55_prompts.py` | 8.3 KB | R55 | 单测 |
| `tests/test_round55_rollback.py` | 11.3 KB | R55 | 单测 |
| `tests/test_round55_subtitles.py` | 12.4 KB | R55 | 单测 |
| `tests/test_round55_tiled_wiring.py` | 12.5 KB | R55 | 单测 |
| `tests/test_round55_transitions.py` | 23.1 KB | R55 | 单测 |
| `tests/test_round55_workflow_alignment.py` | 3.4 KB | R55/56 | **工作流对齐守卫**（P0-A 的长期防线） |

**必须排除（不得进包）3 个：**

| 文件 | 原因 |
|---|---|
| `config/user.yaml` | **含用户本机配置（含 API Key 字段）**，`.gitignore` 已忽略，绝不能提交 |
| `tests/verify/_last_run.json` | 测试运行产物（52 KB），源码 `.gitignore` 已忽略 |
| `tests/verify/_contracts_report.json` | 测试运行产物（19 KB），源码 `.gitignore` 已忽略 |

**可选（建议排除）25 个：**

- `bettercomfyui_comfy/vendor/h3_director/docs/screenshot.png`（125.6 KB 二进制，第三方 README 截图）——**建议排除**，纯体积。
- `bettercomfyui_comfy/workflows/` 下 21 个历史/调试工作流（`W1_`…`W4_`、`S1_`…`S4_`、`P1_`、`W0_`、`W0L_`、`fusion_A/B/C`、`one_director_demo`、`minimal_workflow`、`build_fusion_workflow.py`、`submit_check.py`、`validate_workflow.py`、`validate_fusion_workflow.py`）——历次发布脚本 `bcu_stage_release.py` **有意只保留 `00_` 前缀**，建议延续该策略。
- 但注意：源码有 **4 个** `00_` 工作流，staging 只有 1 个。是否全放需决策（见 §6.2）。

### 2.3 P0-A：单节点工作流 widget 错位（112 vs 119）——**最高危**

用 Python `json` 实测（非 PowerShell，避免中文编码干扰）：

| 文件 | 字节 | 节点数 | `BetterComfyUIOneStop.widgets_values` 长度 |
|---|---|---|---|
| 源码同路径工作流 | 53,393 | 1 | **119** |
| staging 同路径工作流 | 53,287 | 1 | **112** |

`widgets_values` 是**位置数组**，长度差 7 就意味着其后所有参数整体错位。实测差异（节选）：

| 槽位 | 源码 | staging |
|---|---|---|
| `[3]` | `''` | `MiniMax_H3_Ref2VA_pruned_int8_convrot.safetensors` |
| `[4]` | `'第一场 ...'`（剧本） | `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` |
| `[6]` | `minimax_h3_ref2va_pruned_int8_convrot.safetensors` | `minimax_h3_audio_vae_fp32.safetensors` |
| `[14]` | `True` | `12` |
| `[17]` | `'模型审核（本地启发式）'` | `1024` |
| `[21]` | `20` | `'r2v — 参考主体生视频(Reference to Video)'` |

共 **99 个槽位**取值不同（含位移），且源码尾部多 7 项。

> 这正是 ROUND55 审计里的 **P0-1** 同一类事故（当时 118 vs 112）。ROUND56 已把节点参数扩到 119 并新增 `tests/test_round55_workflow_alignment.py` 守卫，**但 staging 里的工作流还是 ROUND54 的 112 版本**。
> **若按现状发布，用户打开「单节点」主工作流即触发参数整体错位**：转场/片头片尾/审核阈值/失败策略/CNT/API/出片等一大片参数被相邻值覆盖，安全默认值失效。**这是必须阻断发布的理由。**

### 2.4 P0-B：缺 `config/default.yaml`

源码 `config/` 有 3 个文件，staging 只有 1 个：

```
源码   : default.yaml (2207 B) / user.yaml (229 B, 禁入包) / user.yaml.example (229 B)
staging: user.yaml.example (229 B)
```

但有 **6 处代码在运行时读取 `config/default.yaml`**（`repo_root/config/default.yaml`）：

```
bettercomfyui/config.py:294          default_path = repo_root / "config" / "default.yaml"
bettercomfyui_comfy/nodes.py:51      (path / "config" / "default.yaml").is_file()
bettercomfyui_comfy/settings_node.py:58
bettercomfyui_comfy/api_settings.py:77
bettercomfyui_comfy/director_edit.py:873   （读 default.yaml + user.yaml）
bettercomfyui_comfy/everything_node.py:521 （报错文案里点名该文件）
```

且 `api_settings.py` 注释明确：`default.yaml` 是**自动生成的默认值文件**，有测试守护「必须等于 `Config()` 的序列化」。

> 也就是说：v0.1.0 / v0.1.1 / v0.1.2 三次内测包**都缺这个文件**（历史遗留缺陷）。ROUND55 审计的 P2-1 也已记录 `default.yaml` 未同步 ROUND55 新开关（`h3_manual_enabled` / `no_subtitle`）。v0.1.3 应一并修掉。

### 2.5 版本号

| 位置 | 当前值 | v0.1.3 应为 |
|---|---|---|
| `bettercomfyui_comfy/__init__.py` → `__version__` | `0.1.2-internal` | `0.1.3-internal` |
| `README.md` 抬头/正文 3 处 | `v0.1.2-internal` | `v0.1.3-internal` |
| 已发布 tag | 最高 `v0.1.2-internal` | 新增 `v0.1.3-internal` |
| `docs/剧核_DramaCore_发布说明_v0.1.md` | 标题 `v0.1` | 更新为 v0.1.3 或新增 ROUND58 说明 |

---

## 3. 安全审计（已完成，结论：干净）

### 3.1 扫描项与结果

| 模式 | 命中 | 判定 |
|---|---|---|
| OpenAI 风格 `sk-…` | **0** | ✅ |
| GitHub Token `ghp_REDACTED/gho_/ghu_/ghs_/ghr_` / `github_pat_` | **0** | ✅ |
| AWS `AKIA…` | **0** | ✅ |
| `[REDACTED:PRIVATE-KEY]` | **0** | ✅ |
| 邮箱 `x@y.z` | **0** | ✅（不含个人邮箱） |
| 明文 `password/passwd/pwd: "…"` | **0** | ✅ |
| 中国大陆手机号 | 1 处误报 | ✅ 实际是工作流里的 `"id":2` / `last_node_id` 等节点编号，非手机号 |
| IPv4 | 52 个文件命中 | ✅ 全部为 `127.0.0.1`（本地 ComfyUI 端点） |
| 非环回/非内网 IPv4 | 1 处 | ✅ `scripts/ssh_verify.py` 里的 `your-gpu-host` 为文档占位示例 |
| `api_key` / `secret` / `token` 赋值 | 3 个测试文件 | ✅ 全为假 Key：`"sk-secret-should-not-leak"`、`"sk-acceptance-31-fake"`，以及「导出必须脱敏」断言文本 |

> 结论：**staging 无真实凭据泄露**。历次发布脚本 `bcu_publish_r54.py` 里的脱敏 REPLACEMENT 列表（公网 IP → `YOUR-GPU-HOST`、真实 Key 前缀 → `REDACTED`、QQ 邮箱 → `noreply@users.noreply.github.com`）已生效。

### 3.2 第三方 vendored 代码的许可证

`bettercomfyui_comfy/vendor/h3_director/` 保留其原始 `LICENSE`（MIT），随包一起分发 —— 已确认在 staged 文件列表中（`bettercomfyui_comfy/vendor/h3_director/LICENSE`）。发布说明中已声明。✅

### 3.3 `.gitignore` 评估

staging 的 `.gitignore` 与源码**不同**（是发布脚本 `bcu_stage_release.py` 重新生成的），两者各有取舍：

| staging 有 | 源码有但 staging 无 |
|---|---|
| `config/user.yaml`、`config/user.yml`、`.env`、`*.key`、`secrets.*` | `!projects/.gitkeep` |
| `projects/`、`dist/`、`logs/`、`*.log` | `_comfyui_tmp/`、`_comfyui_download.zip` |
| 模型后缀 `*.safetensors` `*.ckpt` `*.pt` `*.pth` `*.gguf` | `*.py[cod]`、`*.egg-info/`、`.venv/`、`venv/` |
| 媒体后缀 `*.mp4` `*.mov` `*.wav` `*.mp3` `*.png` `*.jpg` `*.jpeg` `*.webp` | `tests/verify/_last_run.json`、`tests/verify/_contracts_report.json`、`.coverage`、`htmlcov/` |
| `docs/remote_evidence_4090/`、`remote_reports/remote_pulled/` | `*.tmp`、`.*.lock`、`*.lock`、`.vscode/`、`.idea/`、`Thumbs.db`、`desktop.ini`、`dist/*.zip` |

> 建议：v0.1.3 采用**两者并集**（staging 的秘密/媒体规则 + 源码的 Python 缓存与测试产物规则），避免 `tests/verify/_last_run.json` 之类被误提交。注意 staging 的 `.gitignore` 忽略了 `*.png`，因此**不能**把 `docs/ROUND55_workflow_loaded.png` 之类截图放进包（也不需要）。

---

## 4. 凭据与发布通道（脱敏：只记方法，不含任何密钥）

### 4.1 现有可用方式（`%TEMP%` 下的历史发布脚本）

| 脚本 | 作用 | 可复用性 |
|---|---|---|
| `bcu_github_ssh.py` | 登录 → 建仓库 → 加临时 SSH key → push main+tag → 建内测 Release | 首次发布用，**建仓库部分不要复用** |
| `bcu_github_release.py` | 建内测 Release → 验证仓库首页 → 删临时 SSH key | 可复用（Release 部分） |
| `bcu_publish_r54.py` | **脱敏 → commit → tag → 临时 SSH key → push → 删 key** | ⭐ **v0.1.3 主流程最贴近的模板** |
| `bcu_release_r54.py` | 清理残留 key → 建 Release（含 bundle 附件 + 预发布）→ 验证 | ⭐ **Release 阶段模板**（含附件上传、预发布勾选、验证） |
| `bcu_finish_release.py` | requirements.txt / 版本统一 / 文档补充 → commit + tag + `push -f` tag | 参考（注意它用了 `push -f` tag） |
| `bcu_stage_release.py` | 白名单复制源码 → 生成 README/LICENSE/.gitignore → 敏感信息扫描 | ⭐ **同步源码的模板**（需扩充文件与文档清单） |
| `bcu_github_automation.py` / `_v3` / `_v4` | 早期全流程尝试 | 已被 r54 系列取代，不建议用 |
| `bcu_github_cleanup_release.py` | 清理 Release | 应急撤销时参考 |

**通道结论（本机实测）**

```
git                        : OK  C:\Program Files\Git\cmd\git.exe
ssh-keygen                 : OK  C:\WINDOWS\System32\OpenSSH\ssh-keygen.exe
python (3.11) + playwright : OK  （headless chromium 用 channel="msedge"）
gh CLI (GitHub CLI)        : ❌ 未安装 → 不能用 `gh release create`，只能走 Web UI 自动化或 PAT+curl
ffprobe                    : ❌ 未安装 → 打包/预检阶段不能做成片时长校验
```

远程 `origin` 是 **SSH 形式**（`noreply@users.noreply.github.com:minzzj/DramaCore.git`），URL 里**不含 token**，这是正确的做法。

**发布凭据实际形态（三重，全部可撤销）**

1. GitHub 账号登录：`EMAIL` + `PASSWORD` 两个变量（历史脚本里**硬编码为明文常量**）。
2. 临时 SSH 私钥：`%TEMP%\dramacore_ed25519`（ed25519，无口令，注释 `dramacore-release-temp`），公钥经 `https://github.com/settings/ssh/new` 粘贴到账号上。
3. push 时通过环境变量 `GIT_SSH_COMMAND` 指向上面的私钥（`-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null`），**不落 git config、不落 remote URL**。

> 本清单**不记录**任何密码/私钥/token 内容。

### 4.2 凭据卫生问题（发布前必须处理）

| # | 问题 | 实测证据 | 处置 |
|---|---|---|---|
| C-1 | **GitHub 账号密码以明文硬编码**在 `%TEMP%\bcu_*.py` 多个脚本中 | `bcu_github_ssh.py` / `bcu_github_release.py` / `bcu_publish_r54.py` / `bcu_release_r54.py` 均有 `PASSWORD = "…"` 明文常量 | 用完**立即改密码**；此后改用环境变量注入，不再硬编码 |
| C-2 | 上一轮的临时 SSH **私钥仍留在磁盘**：`%TEMP%\dramacore_ed25519` + `.pub` | 两个文件均存在（2026-10-19 生成） | 直接删除两个文件 |
| C-3 | 该私钥**已失效**（good news） | 实测 `ssh -T noreply@users.noreply.github.com` → `Permission denied (publickey)`；`git ls-remote origin` → exit 128 | 说明 r54 的「用后删 key」确实生效了；仅剩本地文件清理 |
| C-4 | `%TEMP%\ssh_pass.txt`（12 B）等历史凭据文件 | 存在 | 删除 |
| C-5 | `%TEMP%` 下大量 `bcu_*.log` 可能含运行时 URL/Key 片段 | 多个 `.err` 达 150–315 KB | 发布后清理，勿打包 |

### 4.3 撤销方式（凭据）

| 凭据 | 撤销动作 |
|---|---|
| 临时 SSH key | GitHub → `Settings → SSH and GPG keys` → 删除 `dramacore-release-temp`；再删本地 `%TEMP%\dramacore_ed25519{,.pub}`。发布后**逐条核对**列表里没有该 key |
| 若改用 PAT | GitHub → `Settings → Developer settings → Personal access tokens` → **Revoke**；最小权限只用 `Contents: Read and write`，**绝不给 `admin`/`delete_repo`**；有效期设 7 天 |
| 账号密码 | 若曾在脚本/对话中出现过：**立即改密码** + 检查 `Settings → Sessions` 踢掉不认识的会话 + 检查 `Settings → Applications` 有无可疑 OAuth 授权 |
| git 凭据缓存 | Windows 凭据管理器里删除 `git:https://github.com` 条目（若用过 HTTPS+PAT 方式） |

---

## 5. 完整发布步骤（v0.1.3-internal）

**角色定义**：**L** = Lead / 发布执行者；**H** = 人类用户（提供账号、点最终确认）；**S** = 本预检子智能体（已完成，不再参与写操作）。

| # | 步骤 | 具体动作 | 执行者 | 需要的凭据 | 失败/撤销方式 |
|---|---|---|---|---|---|
| 1 | **备份 staging** | `Copy-Item _release\DramaCore _release\DramaCore.bak_r58 -Recurse` | L | 无 | 删备份目录 |
| 2 | **同步源码** | 按 §2.2 白名单从工作区复制到 `_release/DramaCore`（含 18 个必补文件 + 21 个更新文件 + docs），**排除** `config/user.yaml`、`tests/verify/_last_run.json`、`_contracts_report.json` | L | 无 | 从备份恢复 |
| 3 | **重新生成单节点工作流（P0-A）** | 用 ROUND56 的生成方式按当前 119 参数 schema 重建 `00_【单节点】…json`，然后跑 `python -m pytest tests/test_round55_workflow_alignment.py -q` 必须 0 failed | L | 无 | 同步骤 1 |
| 4 | **补 `config/default.yaml`（P0-B）** | 从源码复制；若内容落后于 `Config()`，用 `api_settings` 的自动生成逻辑重生成 | L | 无 | 同步骤 1 |
| 5 | **版本号统一** | `bettercomfyui_comfy/__init__.py` → `0.1.3-internal`；`README.md` 3 处抬头改 `v0.1.3-internal`；限制表更新 | L | 无 | `git checkout -- .` |
| 6 | **写 Release notes** | 用配套草稿 `_release/DramaCore/docs/ROUND58_RELEASE_v0.1.3-internal.md` | L | 无 | 删文件 |
| 7 | **安全审计（门禁）** | 跑 `%TEMP%\bcu_r58_preflight.py`：敏感信息扫描 + 禁入文件检查 + 工作流对齐 + `default.yaml` 存在性 + 版本一致性 + 文档齐全。**必须 exit 0** | L | 无 | 修完重跑 |
| 8 | **跑测试（门禁）** | `python -m pytest tests -q`（至少 `tests/test_round55_*.py tests/test_round56_features.py tests/test_round55_workflow_alignment.py`）。**记录真实通过数，不得把历史结论当本次结果** | L | 无 | 回滚改动 |
| 9 | **打包** | `python -B %TEMP%\bcu_r58_pack.py --repo "_release\DramaCore" --version 0.1.3-internal` → 产出 `dist/dramacore_v0.1.3-internal_source.zip` + `.sha256` + `manifest.json` | L | 无 | 删 dist 产物 |
| 10 | **本地 commit** | `git add -A` → `git commit -m "release: v0.1.3-internal (ROUND55/56/57 land + workflow alignment + default.yaml)"` | L | 无（git 身份已配 `DramaCore <noreply@users.noreply.github.com>`） | `git reset --soft HEAD~1` |
| 11 | **打 tag** | `git tag -a v0.1.3-internal -m "DramaCore v0.1.3-internal (internal build, GPU regression pending)"` | L | 无 | `git tag -d v0.1.3-internal` |
| 12 | **生成临时 SSH key** | `ssh-keygen -t ed25519 -N "" -C "dramacore-release-temp" -f %TEMP%\dramacore_ed25519` | L | 本机 `ssh-keygen` | 删文件 |
| 13 | **上载公钥** | Playwright(headless, msedge) 登录 GitHub → `https://github.com/settings/ssh/new` → title `dramacore-release-temp` + 粘贴 `.pub` → Add | H（账号密码）→ L 执行 | **GitHub 账号密码**（C-1，建议改为环境变量注入） | 在 `settings/ssh` 删除该 key |
| 14 | **push main + tag** | `GIT_SSH_COMMAND='ssh -i %TEMP%\dramacore_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null' git push origin main`；再 `git push origin v0.1.3-internal` | L | 上一步的 SSH key | `git push --force-with-lease origin 97d0409:main` + `git push origin :refs/tags/v0.1.3-internal` |
| 15 | **删临时 SSH key（立即）** | Playwright → `settings/ssh` → 删 `dramacore-release-temp` → 确认弹窗 → 断言页面不再出现该标题；再删 `%TEMP%\dramacore_ed25519{,.pub}` | L | 同步骤 13 | 手动在 GitHub UI 删除 |
| 16 | **建 GitHub Release（预发布）** | `/releases/new?tag=v0.1.3-internal`：标题 `v0.1.3-internal · 内测版（…）| DramaCore / 剧核`；正文 = 步骤 6 草稿；勾 **pre-release**；上传步骤 9 的 zip + `.sha256`（+ 可选 `manifest.json`） | L | 同步骤 13 | Release 页 `Delete`；tag 用步骤 11 方式删 |
| 17 | **验证下载与首页** | ① 首页 `https://github.com/minzzj/DramaCore`：README 渲染正常、显示 `v0.1.3-internal`；② Release 页：`Pre-release` 标记存在、附件存在、正文完整；③ **匿名（隐身/登出）** 打开 Release 页确认公开可见；④ 下载 zip → 与本地 `Get-FileHash -Algorithm SHA256` 对比一致；⑤ 解压后抽检 `bettercomfyui_comfy/__init__.py` 版本号 = `0.1.3-internal` | L + H | 无 | Release 转草稿或删除 |
| 18 | **收尾** | 删 `%TEMP%\dramacore_ed25519*`、`ssh_pass.txt`；改 GitHub 密码（C-1）；清理含明文的 `%TEMP%\bcu_*.py`（先归档到安全位置再删）；清理 `dist/` 历史 bundle | H | — | — |

> **关键不可逆点**：步骤 14（push）、16（Release 公开）。1–11 全在本地，随时可回退；HEAD 回退锚点 = `97d0409`；tag 回退点 = 无 v0.1.3。

---

## 6. 打包方案

### 6.1 为什么不产 `.whl`

| 维度 | zip 源码包 | `.whl` |
|---|---|---|
| 消费方式 | ComfyUI 把 `bettercomfyui_comfy/` 拷进 `custom_nodes/` | 需 `pip install`，但插件必须在 `custom_nodes/` 下并依赖 ComfyUI 进程内 API |
| 依赖 | 仅 ComfyUI 自带 + `requirements.txt` | 需在 wheel 里声明 torch 等，与 ComfyUI 环境冲突 |
| vendored 目录 | 原样带着走 | 需打进 wheel，且 h3_director 自带 `pyproject.toml` 会干扰打包 |
| 兼容现有用户 | ✅ 与 v0.1.0–v0.1.2 一致（`bettercomfyui_bundle_<时间戳>.zip`） | ❌ 全部用户要换安装方式 |

**结论：不产出 whl。** 沿用 zip，但把命名从无意义时间戳改成含版本号的可追溯命名。

### 6.2 建议打包内容

**根目录结构**（zip 内顶层 = `DramaCore-v0.1.3-internal/`，避免解压污染）：

```
DramaCore-v0.1.3-internal/
├── README.md                     ← v0.1.3 更新（含内测声明 + 已知限制）
├── LICENSE                       ← AGPL-3.0 + Commons Clause 头
├── requirements.txt
├── .gitignore                    ← staging ∪ 源码 并集
├── bettercomfyui/                ← 含 hardware.py + pipeline 4 新模块 + store/rollback.py + prompt/h3_*
├── bettercomfyui_comfy/          ← 含 119 槽位单节点工作流 + routes.py(回滚路由) + web/js
│   └── vendor/h3_director/       ← 保留 MIT LICENSE，排除 docs/screenshot.png
├── config/
│   ├── default.yaml              ← ✅ 本次新增（P0-B）
│   └── user.yaml.example
├── scripts/                      ← 保留
├── tests/                        ← 含 8 个 round55 + 1 个 round56 + 对齐守卫
│   └── verify/                   ← **排除** _last_run.json / _contracts_report.json
└── docs/                         ← 见下
```

**docs/ 建议收录**（12 → 21 份，宁缺勿滥）：

- 保留现有 12 份（ROUND45/46/47/49/50-A/50-B/53/54/54-A/54-B/54-C + `剧核_DramaCore_发布说明_v0.1.md`）
- 新增 9 份：`ROUND55_实现记录.md`、`ROUND55_最终审计.md`、`ROUND55_发布前检查清单.md`、`ROUND55_版本回滚接口契约.md`、`ROUND56_需求落地记录.md`、`ROUND56_专项配置审计.md`、`ROUND56_专项UI审计.md`、`ROUND58_发布预检清单.md`（本文件）、`ROUND58_RELEASE_v0.1.3-internal.md`
- **排除**：所有 `*.png` 截图（ROUND55/56 的 UI 证据图，共 8 张约 2.5 MB）、`ROUND55_H3提示词工程手册_纯文本.txt`（69.9 KB，与包内 `prompt/data/h3_handbook_full.txt` 重复）、`ROUND20/52/RESUME` 等历史过程文档

> 工作流决策：建议**只放 `00_【单节点】…json` 1 个**（延续既有策略，README 也只指向它）。另 3 个 `00_【总流程】*` 与 21 个历史工作流不入包，避免"用户不知道点哪个"。

### 6.3 命名与校验和

```
dist/dramacore_v0.1.3-internal_source.zip
dist/dramacore_v0.1.3-internal_source.zip.sha256      ← "<hash>  <filename>"（sha256sum 兼容格式）
dist/dramacore_v0.1.3-internal_manifest.json          ← 文件清单 + 每个文件 sha256 + 生成时间 + git HEAD
```

Release 附件至少放 **zip + .sha256**（轻量、可校验）；`manifest.json` 建议一并放（约 100 KB，便于逐文件核验）。

---

## 7. 现有脚本可复用性评估

### 7.1 `scripts/publish.py`（138 行）——**不可复用，且名字有误导性**

| 项 | 结论 |
|---|---|
| 实际作用 | 把 `%TEMP%\bcui_stage` 暂存区文件**复制回工作区**并跑 3 个 pytest 校验。**与 GitHub 发布完全无关** |
| 可复用部分 | 仅是「复制→语法检查→契约测试→明确报告」这套**门禁思路**值得借鉴 |
| 缺口 | ① 无 Git 操作 ② 无打包 ③ 无 Release ④ 硬编码 `TRACKED` 17 个文件（严重过时，ROUND55/56 的文件一个都没列） ⑤ 硬编码 `[USER-HOME]\AppData\Local\Temp\bcui_stage` |

> 建议：**不要改它**（它是"暂存区回填"的工具，职责不同）；另建 `scripts/release_preflight.py` 承担发布门禁，或直接复用本清单配套的 `%TEMP%\bcu_r58_preflight.py` 后固化为正式脚本。

### 7.2 `scripts/prepare_remote_bundle.py`（76 行）——**部分可复用**

| 项 | 结论 |
|---|---|
| 可复用 | 白名单目录（`bettercomfyui`/`bettercomfyui_comfy`/`scripts`/`tests`/`config`/`docs`/`文档`）、排除后缀、`EXCLUDE_NAMES = {user.yaml, user.yml, secrets.yaml}` |
| **缺口 1（P0）** | **白名单里没有 `config/default.yaml` 的显式保障**，且 `docs` 会整体打包 → 会带进 8 张 PNG（约 2.5 MB）与 69.9 KB 重复 txt |
| **缺口 2** | **无 `manifest.json`、无 SHA256 校验和**（无法满足"附件/校验和"要求） |
| **缺口 3** | **无版本号注入**：输出名 `bettercomfyui_bundle_<时间戳>.zip`，不可追溯；不校验 `__version__` 与 tag 是否一致 |
| **缺口 4** | 无 `ROUND55/56/58` 文档白名单，无法按版本精确收敛 docs |
| **缺口 5** | 不排除 `tests/verify/_last_run.json` / `_contracts_report.json`（源码 `.gitignore` 里有，但打包脚本里没有） |
| **缺口 6** | 不排除 `vendor/h3_director/docs/screenshot.png`（125.6 KB 二进制） |
| **缺口 7** | 无"禁入文件存在即失败"的硬门禁（只静默跳过，不报告） |

> 建议：**保留它**（远程部署包仍需要），但发布用打包**另写** `scripts/release_pack.py`（本次先在 `%TEMP%` 出可用版本 `bcu_r58_pack.py`），补齐 manifest + sha256 + 版本注入 + docs 白名单 + 禁入硬门禁。

### 7.3 `%TEMP%` 历史发布脚本

见 §4.1。核心结论：**`bcu_stage_release.py`（同步）+ `bcu_publish_r54.py`（脱敏/commit/tag/SSH key/push/删key）+ `bcu_release_r54.py`（Release+附件+验证）三段式可直接沿用**，只需替换版本号、文件清单、Release 正文。

**必须修的点**：
1. 明文密码 → 改环境变量（如 `BCU_GH_EMAIL` / `BCU_GH_PASSWORD`）。
2. `docs` 白名单按版本收敛（当前是硬编码 7 个文件名）。
3. 增加 P0-A 工作流对齐门禁与 P0-B `default.yaml` 存在性门禁。
4. `push -f` tag（`bcu_finish_release.py` 用过）→ **改为先删远端 tag 再普通 push**，或先确认无人依赖旧 tag。

---

## 8. 发布阻塞项（放行判据）

### P0（不修不得发布）

| ID | 阻塞项 | 判据（如何证明已解决） |
|---|---|---|
| **P0-A** | staging 单节点工作流 112 槽位 ≠ 节点 schema 119 槽位，参数整体错位 | `python -m pytest tests/test_round55_workflow_alignment.py -q` → 0 failed；且用 `json` 读 staging 工作流 `widgets_values` 长度 == 119 |
| **P0-B** | staging 缺 `config/default.yaml`，6 处代码运行时读取它 | `Test-Path _release\DramaCore\config\default.yaml` → True；且 `python -m pytest tests/test_config_wiring_ratchet.py tests/test_consistency.py -q` 通过 |
| **P0-C** | ROUND55/56/57 代码未进 staging（`one_stop_node.py` `routes.py` `dramacore_director_ui.js` 等 21 个文件过期，46 个文件缺失） | §2.1/§2.2 的比对脚本重跑 → 「differs」仅剩无关项、「源码有 staging 缺」= 0（除禁入项） |
| **P0-D** | 版本号未更新（仍 `0.1.2-internal`） | `Select-String "__version__" _release\DramaCore\bettercomfyui_comfy\__init__.py` → `0.1.3-internal` |

### P1（发布前应处理）

| ID | 项 | 建议 |
|---|---|---|
| P1-A | 全量 pytest 未在本轮实跑（引用的是 ROUND55 历史结论「298 passed / 0 failed / 1 未验证」） | 发布前**真跑**一次并记录真实数字；**不得**把历史结论当本次结果 |
| P1-B | ROUND57 `hardware.py`（498 行）**无任何单测**（`tests/` 下无 `test_hardware*` / `test_round57*`），且**未接入** plugin UI / HTTP 路由 / CLI（唯一入口是 `python -m bettercomfyui.hardware doctor`） | 发布说明按「代码级交付、未接线」如实写；建议补 1 个 `detect_hardware(probe=...)` 注入式单测 |
| P1-C | ROUND57 无文档（`docs/` 下无 `ROUND57_*`；`hardware.py` 里的 Tier A/B/C/D 矩阵是唯一说明） | 至少把 Tier 矩阵与「未真机验证昇腾/AMD/Intel/Apple」写进 Release notes |
| P1-D | 明文 GitHub 密码残留 `%TEMP%`（C-1）、失效私钥残留（C-2）、`ssh_pass.txt`（C-4） | 见 §4.2/§4.3 |
| P1-E | `gh` CLI 缺失，Release 只能靠 Playwright 硬点按钮（脆弱） | 失败时降级为纯手工 UI 操作；不要为此装 gh（会引入新凭据面） |
| P1-F | `ffprobe` 缺失 → 无法在预检阶段做成片时长/编码校验 | 真机验收阶段在 GPU 机器上做 |

### P2（可发布后跟进）

`ROUND56_专项UI审计` 记录的 **UI-P1-1（蒙版可被 Tab 绕过）**、**UI-P1-2（"显示/隐藏原生参数表单"按钮无效）**、**UI-P1-3（>64 段 UI 无上限提示、后端静默截断）** 仍属未修项，应在 Release notes「已知限制」中如实列出。
（其中 **UI-P0-1 版本回滚按显示名查询必 500** 已修：`routes.py:_resolve_rollback_project` 已实现显示名→32 位 UUID 解析。）

---

## 9. 下一步命令（交给 Lead / 发布执行者）

> 以下命令**均未执行**，仅供下一步使用。全部假设在仓库根 `[PROJECT-ROOT]` 下。

```powershell
# 0) 先备份 staging（可回退锚点）
Copy-Item '_release\DramaCore' '_release\DramaCore.bak_r58' -Recurse -Force

# 1) 跑预检门禁（会列出 P0-A/P0-B 等全部阻塞项，当前必然失败＝预期行为）
python -B "$env:TEMP\bcu_r58_preflight.py" --repo '_release\DramaCore'

# 2) 同步源码（补齐 §2.2 的 18 个必补 + 覆盖 21 个过期文件；排除 3 个禁入项）
#    建议把"同步"固化进脚本：bcu_stage_release.py 的 3 个清单需扩充
#    - INCLUDE_DIRS 增加 bettercomfyui/pipeline, bettercomfyui/prompt/data, bettercomfyui/hardware.py
#    - config 增加 default.yaml
#    - docs 白名单增加 ROUND55*/ROUND56*/ROUND58*

# 3) 修 P0-A：按当前 119 参数 schema 重新生成单节点工作流（ROUND56 的生成方式）
python -B "$env:TEMP\bcu_regen_workflow_r56.py"        # 需按 R56 方式重建，勿用 R54/R55 旧脚本

# 4) 验证工作流对齐 + 配置门禁
python -m pytest tests\test_round55_workflow_alignment.py tests\test_config_wiring_ratchet.py -q

# 5) 版本号统一（P0-D）
#    改 bettercomfyui_comfy/__init__.py: __version__ = "0.1.3-internal"
#    改 README.md 3 处 v0.1.2-internal → v0.1.3-internal

# 6) 再跑预检门禁，必须 exit 0 才继续
python -B "$env:TEMP\bcu_r58_preflight.py" --repo '_release\DramaCore'

# 7) 打包 + 校验和
python -B "$env:TEMP\bcu_r58_pack.py" --repo '_release\DramaCore' --version 0.1.3-internal
Get-FileHash dist\dramacore_v0.1.3-internal_source.zip -Algorithm SHA256
Get-Content dist\dramacore_v0.1.3-internal_source.zip.sha256

# 8) 本地 commit + tag（此时仍未 push）
git -C '_release\DramaCore' add -A
git -C '_release\DramaCore' commit -m "release: v0.1.3-internal (ROUND55/56/57 land + workflow alignment + default.yaml)"
git -C '_release\DramaCore' tag -a v0.1.3-internal -m "DramaCore v0.1.3-internal (internal build, GPU regression pending)"
git -C '_release\DramaCore' log --oneline -3

# 9) ==== 以下为真实写操作，需人工确认后才可执行 ====
#    临时 SSH key → 上载公钥 → push main + tag → 立刻删 key
ssh-keygen -t ed25519 -N '""' -C "dramacore-release-temp" -f "$env:TEMP\dramacore_ed25519"
# （用 Playwright 脚本 bcu_github_ssh_add_r58.py 上载公钥，之后：）
$env:GIT_SSH_COMMAND = "ssh -i `"$env:TEMP\dramacore_ed25519`" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
git -C '_release\DramaCore' push origin main
git -C '_release\DramaCore' push origin v0.1.3-internal
# （用 bcu_github_keydel_r58.py 立即删除 GitHub 上的临时 key，并删本地私钥）

# 10) ==== 建 Release（预发布 + 附件）→ 验证下载与首页 ====
```

**回退命令（若第 9 步 push 后发现问题）**

```powershell
git -C '_release\DramaCore' push --force-with-lease origin 97d0409:main
git -C '_release\DramaCore' push origin :refs/tags/v0.1.3-internal
# GitHub Release 页 → Delete release（不要只删 tag，否则会留孤儿 Release）
```

---

## 10. 附：本次预检的资产清单

| 产物 | 位置 | 说明 |
|---|---|---|
| 本清单 | `docs/ROUND58_发布预检清单.md` | 只读盘点 + 步骤 + 阻塞项（本文件） |
| Release notes 草稿 | `_release/DramaCore/docs/ROUND58_RELEASE_v0.1.3-internal.md` | **未 commit**，仅供步骤 6/16 使用 |
| 预检门禁脚本 | `%TEMP%\bcu_r58_preflight.py` | 6 道门禁，exit 0 才可发布 |
| 打包脚本 | `%TEMP%\bcu_r58_pack.py` | zip + `.sha256` + `manifest.json` |

**本子智能体未做任何写网络操作、未改源码功能文件、未重启服务。**

---

## 附录 A：两个脚本的实测自检结果（ROUND58 已跑通）

> 本节记录脚本**实际运行**的结果，不是设计意图。执行环境：本机 Python 3.11，无 GPU、无网络写操作。

### A.1 `bcu_r58_preflight.py` 对当前 staging 的结果

```
$ python -B bcu_r58_preflight.py --repo "_release\DramaCore" --version 0.1.3-internal --expect-widgets 119
合计: 12 OK / 3 WARN / 7 BLOCK        → exit code 1（阻断，符合预期）
```

7 个 BLOCK 与 §8 的 P0 完全对应：

| # | 门禁 | 判据实测 |
|---|---|---|
| 1 | G2 版本一致性 | `__version__ = '0.1.2-internal'` ≠ `0.1.3-internal` → **P0-D** |
| 2 | G3 必补文件 | 缺 **18/18** 个 ROUND55/56/57 文件 → **P0-C** |
| 3 | G3 发布文档 | 缺 8/10 份（ROUND55×4、ROUND56×3、ROUND58 清单） |
| 4 | G4 default.yaml | 缺 `config/default.yaml` → **P0-B** |
| 5 | G5 工作流对齐 | `widgets_values=112` ≠ 期望 119（差 −7，第 3 槽位后全部错位）→ **P0-A** |
| 6 | G5 对齐守卫 | 缺 `tests/test_round55_workflow_alignment.py` |
| 7 | G7 测试 | ROUND55 定向测试 **0** 个（应 ≥8） |

3 个 WARN：README 仍是 `v0.1.2-internal`、缺 `tests/test_round56_features.py`、`hardware.py` 无单测。

### A.2 `bcu_r58_pack.py` 对源码树的实际打包结果（输出到 `%TEMP%`，未污染工作区）

```
$ python -B bcu_r58_pack.py --repo "." --version 0.1.3-internal --out-dir %TEMP%\bcu_r58_packtest
收录文件: 392    原始总量: 5.86 MB
压缩后 : 1.92 MB（压缩率 67.3%）
sha256 : 979e9c23b610281c3f9a6ae383340626af9de59693813b0ef6a8a43d8f450e8f
自检   : 条目数=392  testzip=OK    sha256 独立重算一致 ✅
```

产物三件套实测生成：`dramacore_v0.1.3-internal_source.zip`（1.92 MB）、`.zip.sha256`、`dramacore_v0.1.3-internal_manifest.json`（70 KB，含逐文件 sha256 + git 状态）。

**白名单实测排除效果**（这是打包正确性的关键证据）：

| 排除原因 | 数量 | 说明 |
|---|---|---|
| 排除目录 | 200 | `.git`、`__pycache__`、`projects`、`output`、`dist`、`_release` 等 |
| docs 不在白名单 / 子目录 | 139 | 含 `docs/remote_evidence_4090/`（4090 真机证据 JSON）、`docs/试用工作流/`、`docs/远程适配工作流/`、8 张 PNG 截图、69.9 KB 重复手册 txt |
| 排除后缀 | 63 | `*.png` `*.mp4` `*.log` `*.pyc` `*.zip` 等 |
| workflows 非 `00_*` 或生成器 | 20 | 21 个历史/调试工作流与 `workflows/*.py` 生成器 |
| 精确排除 | 3 | `config/user.yaml`、`tests/verify/_last_run.json`、`_contracts_report.json` |

**禁入项验证**（全部 confirmed excluded）：

```
config/user.yaml                                               -> excluded OK
tests/verify/_last_run.json                                    -> excluded OK
tests/verify/_contracts_report.json                            -> excluded OK
bettercomfyui_comfy/vendor/h3_director/docs/screenshot.png     -> excluded OK
```

**秘密扫描**：对收录的 392 个文件做 GitHub token / PAT / OpenAI Key / AWS Key / 私钥块扫描 → **clean，0 命中**。

**收录构成**：`.py` 335 / `.json` 54 / `.md` 30 / `.txt` 5 / 其他 9；`docs/` 精确 20 份白名单 md（无子目录）；`workflows/` 仅 4 个 `00_*`。

### A.3 对 staging 试跑（`--dry-run`）确认 git 元数据与默认输出路径

```
收录文件: 364    原始总量: 5.21 MB
git: branch=main  head=97d0409ef89d  describe=v0.1.2-internal
     remote=noreply@users.noreply.github.com:minzzj/DramaCore.git（SSH，URL 不含 token）
默认输出目录 -> H:\...\Better comfyUI\dist\        ✅ 与 §6.3 命名一致
[WARN] 工作区有未提交改动
```

> `dirty=True` 是**预期**的：本次预检按任务要求把 Release notes 草稿写进了
> `_release/DramaCore/docs/ROUND58_RELEASE_v0.1.3-internal.md` 且**故意不提交**，
> 因此 staging 相对 HEAD 恰好多了这 1 个未跟踪文件。发布执行者在步骤 10 commit 之后，
> 该告警会自动消失。

### A.4 两个脚本的使用边界

| 脚本 | 会写什么 | 绝不会做什么 |
|---|---|---|
| `bcu_r58_preflight.py` | **什么都不写**（纯只读） | 不写仓库、不调 git 写命令、不联网、不打印凭据原文 |
| `bcu_r58_pack.py` | 只在 `--out-dir`（默认 `dist/`）写 zip + `.sha256` + `manifest.json` | 不改源目录、不 commit、不 tag、不 push、不联网 |

### A.5 已知的脚本局限（照实写）

1. `preflight.py` 的 `G5` 用 `--expect-widgets`（默认 119）作为**外部期望值**，不自己推导节点 schema。权威判据仍是 `tests/test_round55_workflow_alignment.py`；两者要一起跑。
2. `pack.py` 的 `docs` 白名单是**版本硬编码**的 21 项，ROUND59 需同步维护，否则新文档进不了包。
3. `pack.py` 的 git 元数据取自 `--repo` 目录；若打包的是源码树（非 git 仓库）则 `head`/`branch` 为空，manifest 里会留空字段。
4. 两个脚本都**不校验** `README.md` 的「已知限制」章节是否与 `docs/` 审计结论一致 —— 这属于人工评审项。

---

## 附录 B：⚠️ 并发变更与口径修正（重要，请 Lead 先读这一节）

**本清单 §1–§2 的盘点数字是 2026-10-20 19:29–19:33 的快照。**
在本子智能体工作期间（19:35–19:49），**其他 ROUND58 子智能体正在并行修改源码树**，源码已明显不同于我的盘点时点。

### B.1 并发变更实测（按 mtime）

| 时间 | 文件 | 变化 |
|---|---|---|
| 19:35:00 | `docs/ROUND58_任务拆分与验收计划.md` | 🆕 新文档 |
| 19:35:16 | `bettercomfyui/prompt/data/h3_concise_v1.txt` | 🆕 20.6 KB |
| 19:35:50 | `bettercomfyui/prompt/h3_handbook.py` | 3.7 KB → **8.7 KB** |
| 19:37:56 | `bettercomfyui_comfy/hardware_adapt.py` | 🆕 **26 KB**（插件侧硬件适配） |
| 19:38:56 | `bettercomfyui_comfy/__init__.py` | 16.7 KB（版本/注册表改动） |
| 19:38:58 | `bettercomfyui_comfy/one_stop_node.py` | 169 KB → **198 KB** |
| 19:39:08 | `bettercomfyui_comfy/routes.py` | **61.7 KB**（原 52 KB） |
| 19:40:24 | `bettercomfyui_comfy/web/js/dramacore_director_ui.js` | **164.6 KB**（原 124 KB） |
| 19:40:34 | `bettercomfyui/hardware.py` | 22.4 KB → **28.5 KB**（ROUND57 模块在扩展） |
| 19:42:16 | `bettercomfyui_comfy/everything_node.py` | **49.1 KB**（原 40 KB） |
| 19:43:14 | `install/install_core.py` | 🆕 **40.5 KB** |
| 19:44:20 | `tests/test_round58_concise_prompt.py` | 🆕 11.1 KB |
| 19:45:18 | `install/{__init__.py,install_windows.ps1,install_linux.sh,install_macos.sh}` | 🆕 跨平台安装器 |
| 19:45:42 | `bettercomfyui/prompt/validate.py` | **78.6 KB**（原 50 KB） |
| 19:46:18 | `docs/ROUND58_H3提示词精简版集成.md` | 🆕 新文档 |

同时观察到 `.pytest_cache/` 与 `tests/verify/_last_run.json` 被写入 —— 说明**有另外的子智能体正在跑 pytest**。

### B.2 口径修正（我原先的说法作废）

| # | 原说法（§2.2 / §5 / §6） | 修正后（19:49 实测） |
|---|---|---|
| 1 | 「`install/` **不存在**」 | ❌ 作废。`install/` **现在存在**，含 5 个文件：`install_core.py`(40 KB)、`__init__.py`、`install_windows.ps1`、`install_linux.sh`、`install_macos.sh`。这是 ROUND58 的跨平台安装器，**必须进包** |
| 2 | 「`hardware.py` 22.4 KB」 | 现为 **28.5 KB**（仍在增长） |
| 3 | 「必补 18 个文件」 | 现为 **26 个**（新增 `hardware_adapt.py`、`h3_concise_v1.txt`、`install/*` ×5、`test_round58_concise_prompt.py`） |
| 4 | 「源码有 staging 缺 46 个」 | 现为 **54 个**，其中 3 个是禁入项（`config/user.yaml`、`tests/verify/_last_run.json`、`_contracts_report.json`），**实际有效缺口 51 个** |
| 5 | 「`docs/` 无 ROUND57/58 文档」 | ROUND57 仍无文档；但 ROUND58 已有 3 份（任务拆分、发布预检清单、H3提示词精简版集成） |

> **§2.3 的 P0-A（工作流 112 vs 119）与 §2.4 的 P0-B（缺 `default.yaml`）不受影响** —— 这两项在 19:33 与 19:49 两次实测结论一致，仍是硬阻塞。

### B.3 因此产生的流程结论（最重要）

1. **本清单任何数字都不能当作发布时的最终事实**。发布执行者必须在**所有 ROUND58 子智能体全部结束之后**，重新跑一次：

   ```powershell
   python -B "$env:TEMP\bcu_r58_preflight.py" --repo '_release\DramaCore'
   ```
   只有它 **exit 0**（或只剩可接受 WARN）时，才能进入 §5 的步骤 9（打包）。

2. **不要在我这份快照的基础上直接同步源码**。若在 19:49 之后就同步，很可能把「另一个子智能体写到一半的文件」打进包。
   正确顺序：**等 ROUND58 全部子任务收口 → 用户/Lead 确认源码树冻结 → 再同步 → 再预检 → 再打包**。

3. 建议 Lead 在同步前显式记录一个**冻结基线**（便于事后追溯打了哪一版）：

   ```powershell
   # 记录冻结时点的源码指纹（不写入仓库，只作为发布记录）
   Get-ChildItem -Recurse -File bettercomfyui,bettercomfyui_comfy,install,scripts,tests,config |
     Where-Object { $_.FullName -notmatch '__pycache__|\.pytest_cache' } |
     ForEach-Object { "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash, $_.FullName } |
     Sort-Object | Set-Content "$env:TEMP\bcu_r58_freeze_baseline.txt" -Encoding UTF8
   ```

### B.4 已同步调整的两个脚本（本附录对应的代码变更）

| 脚本 | 调整 | 实测结果 |
|---|---|---|
| `bcu_r58_pack.py` | ① `INCLUDE_DIRS` 增加 **`install`**；② `docs/` 从「精确文件名白名单」改为**前缀白名单**（`ROUND45_`…`ROUND58_` + 显式 deny 表），避免每轮忘记维护；③ `docs/` 只允许 `.md`/`.txt`，挡掉 PNG/CSV/零散 JSON | 402 文件 / 6.28 MB / secrets clean / 4 个 `00_*` 工作流 / `install/` 5 文件全收 / 禁入项 7 项全部 excluded |
| `bcu_r58_preflight.py` | `REQUIRED_FILES` 从 18 项扩到 **26 项**（含 `install/*`、`hardware_adapt.py`、`h3_concise_v1.txt`、`test_round58_concise_prompt.py`） | 对 staging 仍 **exit 1**，7 个 BLOCK（其中「缺 26/26 必需文件」） |

两个脚本均已 `python -m py_compile` 通过（exit 0）。

### B.5 给 Lead 的判断建议

- 如果 ROUND58 还要继续迭代（新增/重命名源码文件），**现在不是打 tag 的时机**；
- 如果 ROUND58 的工作已全部收口，那么 §5 的 18 步可以直接执行，但**第 7 步（预检门禁）必须用更新后的 26 项清单重跑**；
- 无论哪种情况，**P0-A 与 P0-B 必须先修**——这两项与并发变更无关，是确定的历史欠账。
