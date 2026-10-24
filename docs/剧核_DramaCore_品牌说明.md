# 剧核 / DramaCore 品牌说明

> 更新：ROUND43（2026-10-18）｜负责：品牌/UI/多语言智能体｜状态：本地已验证，4090 部署待 Lead 统一执行

## 一、品牌定名

| 项 | 值 |
|---|---|
| 中文名 | **剧核** |
| 英文名 | **DramaCore** |
| 完整写法 | 剧核 / DramaCore（UI 标题）、剧核 DramaCore（节点分类） |
| 内部包名 | `bettercomfyui_comfy`（**不变**，保证已安装目录/旧工作流兼容） |
| 节点类型 ID | `BetterComfyUI*`（**一律不变**，旧工作流可直接打开） |
| 面板路由前缀 | `/bettercomfyui/*`（**不变**，前端/脚本兼容） |

## 二、改名改在哪（显示面）

1. **节点显示名**：`bettercomfyui_comfy/__init__.py` 的「品牌层」在注册完成后统一覆盖
   `NODE_DISPLAY_NAME_MAPPINGS`，格式：
   `剧核·单节点（剧本→成片）｜DramaCore One-Stop (Script→Final Cut)`
   —— 中英双语同显，节点搜索栏搜「剧核」或「DramaCore」都能找到。
2. **节点分类**：每个节点类的 `CATEGORY` 统一到 `剧核 DramaCore` 及其子目录：
   `融合 Fusion` / `单节点 One-Stop` / `总控台 Control Hub` / `素材 Assets` /
   `审核 Review` / `提示词 Prompts` / `配置 Settings` / `成片 Output`
   （左侧「添加节点」菜单里不再出现 Better ComfyUI）。
3. **右侧面板**：标题「剧核 / DramaCore」，渐变品牌色，四个折叠分组
   （连接配置 / 创作默认值 / 项目与运行 / 状态与审核结论）。
4. **原生设置**：ComfyUI 齿轮 → 设置里新增「剧核 / DramaCore · API 密钥 / 模型名 / 接口地址」
   三项，设置分组名改为剧核。
5. **启动日志**：`[剧核 DramaCore] ...`（措辞由 `Better ComfyUI：` 改为 `剧核 DramaCore：`）。

> 说明：内部 logger 名 `bettercomfyui.comfy`、包名、节点 ID、路由前缀**故意保留旧名**——
> 它们是"机器接口"，改名会造成旧工作流/旧脚本失效；用户可见的都是「剧核 / DramaCore」。

## 三、多语言（i18n）结构

- 资源文件：`bettercomfyui_comfy/web/js/dramacore_i18n.js`（新增，ComfyUI 自动加载）。
  文件内 `ZH` / `EN` 两个文案字典 + `NODE_NAMES`（节点名）+ `WIDGET_TOOLTIPS`（参数 tooltip）
  + `STATUS`（审核/运行状态词）+ `GROUP_RULES`（参数分组规则）。
- 运行时挂到 `globalThis.DramaCoreI18N`，面板**零网络请求**读取，避免 JSON fetch 竞态。
- 语言优先级：`localStorage["dramacore.uiLang"]`（本浏览器）→ `config/user.yaml` 的
  `ui.language`（服务端，跨浏览器）→ 浏览器语言（`navigator.language`，zh* → 中文，其余英文）。
- 切换效果：
  - 面板文案、状态徽标：**立即生效**（保存后立刻重刷）；
  - 节点标题：刷新页面（Ctrl+F5）后生效（ComfyUI 节点注册表在页面加载时建立）；
  - 节点分类：服务端双语（`剧核 DramaCore/素材 Assets`），无需切换。
- 覆盖范围（逐项）：

| 范围 | 覆盖 | 位置 |
|---|---|---|
| 节点显示名（25 个类型） | 中/英 | `__init__.py` + `dramacore_i18n.js` NODE_NAMES |
| 节点分类（9 类） | 中/英 | `__init__.py` `_BRAND_CATEGORIES` |
| 参数 tooltip（约 45 个常见控件名） | 中/英 | `dramacore_i18n.js` WIDGET_TOOLTIPS |
| 面板全部文案（4 分组 + 日志 + 报错） | 中/英 | `dramacore_i18n.js` ZH/EN |
| 运行/审核状态词（13 个：通过/不通过/需返工/待人工确认/失败…） | 中/英 | `dramacore_i18n.js` STATUS |

## 四、节点参数分组折叠与状态可视化

- **分组折叠**（`dramacore_i18n.js`，只对参数 ≥6 个的本插件节点生效）：
  按控件名匹配「素材 Assets / 视频 Video / 审核 Review / 输出 Output」四组，
  在每组第一个参数前插入折叠标题按钮（button 控件**不参与工作流序列化**，
  不改变存档格式），点击折叠/展开；**不删除任何参数**，默认展开。
- **状态/审核结论可视化**：节点执行结束后，从前端 `onExecuted` 消息里提取
  `passed / blocked / verdict / policy / conclusion / result` 等字段，
  在节点右上角画彩色徽标（通过=绿、不通过=红、需返工=橙、待人工=蓝）。
- **面板状态可视化**：体检结果按「节点数 / 编排包 / ffmpeg / ComfyUI 版本 / 计算设备 /
  真实生成可用」逐项彩色徽标显示，不再是糊成一段文字。

## 五、旧工作流兼容清单

| 兼容项 | 结论 |
|---|---|
| 节点类型 ID `BetterComfyUI*` | 不变 ✅ |
| 节点控件名/顺序/默认值 | 不变（只加 button 折叠标题，不序列化）✅ |
| `/bettercomfyui/*` 老接口 | 保留（`/settings`、`/doctor`、`/projects` 等）✅ |
| `config/user.yaml` 已有字段 | 保留（只追加 `ui.*` 与 `prompt.system_prompt_template`）✅ |
| 面板文件名 `bettercomfyui_panel.js` | 保留（ComfyUI 扩展入口路径不变）✅ |
