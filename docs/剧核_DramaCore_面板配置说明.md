# 剧核 / DramaCore 面板配置说明

> 更新：ROUND43（2026-10-18）｜适用：ComfyUI 网页右侧「剧核 / DramaCore」面板 + 原生设置
> 一句话：**API、模型、接口地址、默认系统提示词、语言、默认视频/图像模型，全部在面板里填，保存即持久化，可一键测试连接。**

## 一、面板在哪

- ComfyUI 网页右侧侧边栏 → 「剧核 / DramaCore」面板（找不到就 Ctrl+F5 刷新；仍没有说明该前端版本没有侧边栏）。
- 没有侧边栏时：ComfyUI 齿轮 → 设置 → 搜「剧核」/「DramaCore」，有 API 密钥 / 模型名 / 接口地址三项原生设置。
- 面板**不会**再创建浮动窗口（用户明确要求），不会遮挡画布。

## 二、面板四个分组与字段清单

### ① 连接配置（API Key / 模型 / 接口地址）

| 字段 | 控件 id | 说明 |
|---|---|---|
| API Key | `#dc-api-key` | 密码框；留空 = 不修改已保存密钥；保存后立即清空输入框，不回显明文 |
| API Base | `#dc-api-base` | 如 `https://api.deepseek.com`；留空 = 用官方默认/已保存值 |
| 模型名 | `#dc-api-model` | 如 `deepseek-chat`、`qwen-plus` |
| 测试连接 | `#dc-api-test` | 由 **ComfyUI 服务端**代发请求（不是浏览器直连，避开 CORS 误判） |
| 保存 | `#dc-api-save` | 写入 `config/user.yaml`，并同步给剧本巨人（`ComfyUI-Storyboard-LLM/settings.json`）与 ComfyUI 原生设置 |

- 状态条：显示「已配置 / 未配置 + 遮罩后的密钥 + 来源（环境变量 / config/user.yaml / 原生设置 / 巨人）」，**永不显示完整密钥**。
- 测试连接返回：HTTP 状态、延迟 ms、模型数量与示例；`401/403` 明确报"密钥无效或无权限"；网络错误报具体原因并提示"API Base 要能被服务器访问，别写 localhost（除非同机）"。
- 密钥优先级（高→低）：环境变量 `BETTERCOMFYUI_API_KEY` → `config/user.yaml` 的 `prompt.api_key` → 面板保存值（写进 user.yaml）→ 原生设置/巨人 settings.json。

### ② 创作默认值（提示词 / 语言 / 默认模型）

| 字段 | 控件 id | 落盘位置 | 说明 |
|---|---|---|---|
| 默认系统提示词 | `#dc-system-prompt` | `prompt.system_prompt_template` | 留空 = 用内置导演/编剧提示词；上限 8000 字符 |
| 界面语言 | `#dc-ui-language` | `ui.language`（auto/zh/en） | 面板文案立即切换；节点标题刷新页面后切换 |
| 输出语言 | `#dc-output-language` | `prompt.output_language`（zh/en） | 剧本/提示词的输出语言 |
| 默认视频模型 | `#dc-video-model` | `ui.default_video_model` | 下拉候选来自 ComfyUI models 目录扫描 + 推荐值；也可直接输入 |
| 默认图像模型 | `#dc-image-model` | `ui.default_image_model` | 同上 |
| 保存 | `#dc-defaults-save` | 写入 `config/user.yaml` | 只写白名单字段，保留 yaml 其它内容 |

### ③ 项目与运行

项目名输入 + 「打开或创建」、项目下拉 + 「刷新项目」、执行器选择（mock=占位媒体 / 真实 ComfyUI 未接入本闭环）、剧本 JSON、开始运行、载入最简工作流。

### ④ 状态与审核结论

「体检」按钮 → 彩色徽标逐项显示：节点数、编排包是否可用、ffmpeg、ComfyUI 版本、计算设备与显存、真实生成是否可用；下方运行日志（可复制给运维）。

## 三、怎么填（DeepSeek 示例）

1. 打开面板 → ① 连接配置：
   - API Key：粘贴 `sk-...`（只写一次，保存后输入框自动清空）；
   - API Base：`https://api.deepseek.com`；
   - 模型名：`deepseek-chat`。
2. 点「测试连接」→ 显示「连接成功：xxx ms，模型 N 个」即通过。
3. 点「保存」→ 提示「配置已保存：prompt.api_key, prompt.model_name, prompt.endpoint → config/user.yaml」。
4. ② 创作默认值：填默认系统提示词（如"你是短剧编剧兼分镜导演，输出 JSON…"），语言选中文/English，默认视频/图像模型下拉选择 → 保存。
5. 刷新页面（Ctrl+F5）→ 节点标题语言与 HTML 语言同步；到画布用「剧核·单节点」「剧核·总导演台」等工作流出片。

## 四、数据落在哪

`<仓库根>/config/user.yaml`（面板保存会保留该文件其它字段）：

```yaml
prompt:
  api_key: sk-****（不回显）
  model_name: deepseek-chat
  endpoint: https://api.deepseek.com
  output_language: zh
  system_prompt_template: 你是短剧编剧兼分镜导演…
ui:
  language: auto          # auto / zh / en
  default_video_model: minimax_h3_t2v_turbo.json
  default_image_model: RealVisXL_V5.0_fp16.safetensors
```

- `config/default.yaml` 是**自动生成的默认值文件**（有测试守着），面板**不写它**。
- 密钥也可以用环境变量 `BETTERCOMFYUI_API_KEY`（优先级最高，适合 CI/生产）。

## 五、接口（前端调用，服务端实现）

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/bettercomfyui/panel/config`（同时注册 `/api/bettercomfyui/panel/config`） | 读面板全部配置（密钥遮罩） |
| POST | `/bettercomfyui/panel/config` | 保存：api_key/model_name/endpoint + system_prompt/output_language/ui_language/video_model/image_model |
| POST | `/bettercomfyui/panel/test` | 服务端测试连接（GET /models，404 时退化 POST /chat/completions） |
| GET/POST | `/bettercomfyui/settings` | 旧接口，保留兼容（只处理 prompt 白名单字段） |
| GET | `/bettercomfyui/doctor` | 体检（面板④的数据源） |

## 六、常见问题

| 现象 | 原因/处理 |
|---|---|
| 面板不出现 | 该 ComfyUI 前端无侧边栏 → 用齿轮设置里的「剧核」分组；或 Ctrl+F5 |
| 提示"未配置 API Key" | ① 填 Key 并保存；或设置环境变量 `BETTERCOMFYUI_API_KEY` |
| 测试连接失败：401/403 | 密钥错/无权限/不是 OpenAPI 兼容接口 |
| 测试连接失败：网络错误 | API Base 从**服务器**访问不通（远端 4090 上写 localhost 无效） |
| 节点标题还是英文/中文混杂 | 节点标题语种在页面加载时确定，Ctrl+F5；或把界面语言设为 auto |
| 保存后 `user.yaml` 没变化 | 检查是否有写权限；接口返回的 `saved.path` 与 `written` 字段会写明 |
