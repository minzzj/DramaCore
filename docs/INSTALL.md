# 安装入口（INSTALL）

> 本页为安装入口；完整、可复制粘贴的步骤已合并到 **[安装指南（用户版）](安装指南_用户版.md)**。
> 本文件保留给旧链接/旧文档跳转，不再维护旧版内容（旧版 INSTALL 已归档）。

## 正式版（推荐）

1. 下载 [正式版 ZIP](https://github.com/minzzj/DramaCore/releases/download/v0.2.0-internal/DramaCore-v0.2.0-internal.zip) 或 `git clone` 仓库；
2. 运行一键安装器（Windows / Linux / macOS）：
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install\install_windows.ps1 -Mode attach -ComfyUI "D:\ComfyUI" -DryRun
   powershell -ExecutionPolicy Bypass -File .\install\install_windows.ps1 -Mode attach -ComfyUI "D:\ComfyUI" -Yes
   ```
   ```bash
   ./install/install_linux.sh --mode attach --comfyui /opt/ComfyUI --dry-run
   ./install/install_linux.sh --mode attach --comfyui /opt/ComfyUI --yes
   ```
3. 模型 / API Key / 启动 / 工作流：见 **[安装指南（用户版）](安装指南_用户版.md)**。

## 内测版（公测，可选）

下载 [内测版 ZIP](https://github.com/minzzj/DramaCore/releases/download/v0.3.0-beta.1/DramaCore-v0.3.0-beta.1.zip)，解压覆盖同一 `custom_nodes/bettercomfyui_comfy`，重启后打开【内测版】工作流。
内测不稳定；报错切回正式版工作流即可，详见 [双版本说明](双版本_正式版与内测版.md)。

## 其它

- 安装器说明：[../install/README.md](../install/README.md)
- 文档索引：[文档索引.md](文档索引.md)
- 历史安装/部署长文：`ROUND57_安装部署指南.md`（开发存档，普通用户不用看）
