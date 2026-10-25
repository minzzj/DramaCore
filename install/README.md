# install · 一键安装入口

真实逻辑在 `install_core.py`（跨平台）；本目录只是各平台薄包装。

## Windows

```powershell
# 先看计划（不写任何文件）
powershell -ExecutionPolicy Bypass -File .\install\install_windows.ps1 -Mode attach -ComfyUI "D:\ComfyUI" -DryRun
# 正式安装
powershell -ExecutionPolicy Bypass -File .\install\install_windows.ps1 -Mode attach -ComfyUI "D:\ComfyUI" -Yes
```

## Linux

```bash
chmod +x install/install_linux.sh
./install/install_linux.sh --mode attach --comfyui /opt/ComfyUI --dry-run
./install/install_linux.sh --mode attach --comfyui /opt/ComfyUI --yes
```

## macOS

```bash
chmod +x install/install_macos.sh
./install/install_macos.sh --mode attach --comfyui "$HOME/ComfyUI" --dry-run
./install/install_macos.sh --mode attach --comfyui "$HOME/ComfyUI" --yes
```

- `attach`：装进已存在的 ComfyUI；
- `bootstrap`：自动准备 ComfyUI 与 venv（不下载模型）；
- 默认 dry-run，必须显式 `-Yes` / `--yes` 才写入；
- 不下载任何模型权重，只打印模型目录与文件名要求；
- 完整说明见 `docs/安装指南_用户版.md`。
