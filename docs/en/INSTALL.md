# Installation Guide (English)

> Goal: from zero to a running DramaCore on an existing ComfyUI installation, then load a workflow.
> Repo: <https://github.com/minzzj/DramaCore> · Current stable: `v0.2.0-internal`

**Language** · [简体中文](../安装指南_用户版.md) | **English**

## Contents

- [0. Requirements](#0-requirements)
- [1. Get the code](#1-get-the-code)
- [2. One-click install (recommended)](#2-one-click-install-recommended)
- [3. Manual install](#3-manual-install)
- [4. Model files](#4-model-files-must-be-provided-by-you)
- [5. Configure API key](#5-configure-api-key)
- [6. Start and run your first film](#6-start-and-run-your-first-film)
- [7. Beta channel (optional)](#7-beta-channel-optional)
- [8. Upgrade](#8-upgrade)
- [9. Troubleshooting](#9-troubleshooting)
- [10. License & security](#10-license--security)

## 0. Requirements

| Item | Requirement |
|---|---|
| ComfyUI | ≥ 0.30 (verified on 0.41; 0.33 works) |
| GPU | 24GB+ VRAM recommended for real generation (H3 48G is safer); CPU is for orchestration/mock only |
| Disk | ≥150GB free (H3 text encoder is ~34GB + diffusion weights + VAE) |
| Python | 3.9+ (use ComfyUI's bundled Python/venv) |
| Git | Optional; ZIP download is fine |
| Model weights | **Not included**; obtain them under their licenses (section 4) |
| LLM API | DeepSeek or compatible API key (one API call to split the script) |

## 1. Get the code

```bash
git clone https://github.com/minzzj/DramaCore.git
cd DramaCore
```

No Git: download <https://github.com/minzzj/DramaCore/archive/refs/heads/main.zip> and unzip.
Below, `<DramaCore>` means the repository root.

## 2. One-click install (recommended)

The installer detects GPU/backend/VRAM → installs ComfyUI requirements → places the plugin and orchestrator → writes launchers.
Default is dry-run (no writes); add `-Yes` / `--yes` to actually install. **Models are not downloaded.**

### Windows

```powershell
cd <DramaCore>
powershell -ExecutionPolicy Bypass -File .\install\install_windows.ps1 `
  -Mode attach -ComfyUI "D:\ComfyUI" -DryRun
powershell -ExecutionPolicy Bypass -File .\install\install_windows.ps1 `
  -Mode attach -ComfyUI "D:\ComfyUI" -Yes
```

### Linux

```bash
cd <DramaCore>
chmod +x install/install_linux.sh
./install/install_linux.sh --mode attach --comfyui /opt/ComfyUI --dry-run
./install/install_linux.sh --mode attach --comfyui /opt/ComfyUI --yes
```

### macOS

```bash
cd <DramaCore>
chmod +x install/install_macos.sh
./install/install_macos.sh --mode attach --comfyui "$HOME/ComfyUI" --dry-run
./install/install_macos.sh --mode attach --comfyui "$HOME/ComfyUI" --yes
```

### No ComfyUI yet?

```powershell
powershell -ExecutionPolicy Bypass -File .\install\install_windows.ps1 -Mode bootstrap -ComfyUI "D:\ComfyUI" -Backend cuda -Yes
```

```bash
./install/install_linux.sh --mode bootstrap --comfyui "$HOME/ComfyUI" --backend cuda --yes
```

On success a launcher is created (Windows `start_dramacore.bat`, Linux/macOS `start_dramacore.sh`).
**Always start via the launcher** (it sets `BETTERCOMFYUI_HOME`); do not run the vanilla `main.py` directly.

## 3. Manual install

1. Copy `<DramaCore>/bettercomfyui_comfy` to `<ComfyUI>/custom_nodes/bettercomfyui_comfy` (folder name must match);
2. Keep `<DramaCore>/bettercomfyui` in the repo; set `BETTERCOMFYUI_HOME=<DramaCore absolute path>` when starting;
3. Install dependencies with ComfyUI's Python:

```bash
<ComfyUI>/python -m pip install -r <DramaCore>/requirements.txt
```

4. Start:

```bash
set BETTERCOMFYUI_HOME=D:\DramaCore
python main.py
```

## 4. Model files (must be provided by you)

> ⚠️ No model weights are included. File names must match exactly, otherwise workflows show "missing model".

| Directory | File | Purpose |
|---|---|---|
| `ComfyUI/models/diffusion_models/` | `minimax_h3_ref2va_pruned_int8_convrot.safetensors` | H3 main diffusion model |
| `ComfyUI/models/text_encoders/` | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | H3 text encoder (~34GB) |
| `ComfyUI/models/vae/minimax/` | `minimax_h3_video_vae_fp16.safetensors` | Video VAE |
| `ComfyUI/models/vae/minimax/` | `minimax_h3_audio_vae_fp32.safetensors` | Audio VAE |
| `ComfyUI/models/loras/` | `minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors` | Real-parameter LoRA (8-step) |
| `ComfyUI/models/loras/` | `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors` | Fast 4-step LoRA |
| `ComfyUI/models/checkpoints/` | `RealVisXL_V5.0_fp16.safetensors` | Asset image generation (optional) |

Use official/licensed sources and comply with each model's license. The repo ships workflows only.

## 5. Configure API key

```bash
cp config/user.yaml.example config/user.yaml
```

```yaml
prompt:
  endpoint: https://api.deepseek.com
  model_name: deepseek-chat
  api_key: 'your-key'        # never commit
```

Alternatively use the "剧核" panel on the right side of ComfyUI.
`config/user.yaml` is gitignored; **never put real keys into workflows/screenshots/issues**.

Default system prompt: `h3_v3_contract` (V3.0 contract alignment, 37,457 chars).
The default unified style is only an example — replace it for your project.

## 6. Start and run your first film

1. Start via the launcher (or set `BETTERCOMFYUI_HOME` and run `python main.py`);
2. Open `http://127.0.0.1:8188` and press **Ctrl+F5**;
3. Load a workflow:
   - Test: `bettercomfyui_comfy/workflows/【新版】剧核_单节点_剧本到成片_快速4步版_20261024.json`
   - Final: `…真实参数版_20261024.json`
4. Paste a script into the node, select a project, click Run;
5. Output goes to `ComfyUI/output/剧核成片/` (and the project folder).

## 7. Beta channel (optional)

Download the [Beta ZIP](https://github.com/minzzj/DramaCore/releases/download/v0.3.0-beta.1/DramaCore-v0.3.0-beta.1.zip), unzip over the same plugin folder, restart and open a 【内测版】 workflow.

- Beta: pure t2v, no asset images, 2×15s/2×10s long segments, single-pass encoding;
- **Unstable**; if it fails or looks worse, reopen a Stable workflow;
- Startup failure: remove the `beta/` folder and restart.
See [Dual-version guide](DUAL_VERSION.md) / [beta/README_EN.md](../../beta/README_EN.md).

## 8. Upgrade

```bash
cd <DramaCore>
git pull
```

Re-run the installer (`-Yes` / `--yes`) or overwrite the plugin folder → restart → Ctrl+F5 → reopen the 【新版】 workflow.
`config/user.yaml` is preserved.

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| "Orchestrator unavailable" | `BETTERCOMFYUI_HOME` not set; start via `start_dramacore.bat/.sh` |
| "Missing model" | Check exact file names/directories in section 4 |
| "Parameters do not match node" | Ctrl+F5 and reopen the 【新版】 workflow |
| API 401 | Check `config/user.yaml` or the panel key |
| CUDA OOM | Lower resolution/reference count, batch segments=1, low VRAM mode; 48G is safer |
| Remote panel 403 | Access via `IP:port`; for domain access set `BETTERCOMFYUI_ALLOW_REMOTE_PANEL=1` |
| Port in use | Stop other ComfyUI processes or add `--port 8189` |

## 10. License & security

- CC BY-NC-SA 4.0: download/modify/non-commercial redistribution allowed; **commercial use prohibited**; derivatives same license + attribution;
- No token/API key is stored in the repo; rotate immediately if leaked;
- Model weights and third-party assets follow their own licenses.
