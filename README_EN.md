# DramaCore (Better ComfyUI)

> **Script in, film out.** One ComfyUI mega-node covers: script → prompts → assets → H3 Director → review → edit & final cut.
> License **CC BY-NC-SA 4.0**: download, modify and non-commercial redistribution allowed; **commercial use prohibited**; derivatives must be released under the same license.

**Language** · **简体中文** | **English**

**Release channels**

| Channel | Version | Purpose | Download |
|---|---|---|---|
| 🟢 **Stable** | `v0.2.0-internal` | Default; validated stable pipeline | [Stable ZIP](https://github.com/minzzj/DramaCore/releases/download/v0.2.0-internal/DramaCore-v0.2.0-internal.zip) |
| 🟡 **Beta (public beta)** | `v0.3.0-beta.1` | Faster + better-looking new pipeline; **may be unstable** | [Beta ZIP](https://github.com/minzzj/DramaCore/releases/download/v0.3.0-beta.1/DramaCore-v0.3.0-beta.1.zip) |

> Recommendation: use Stable for delivery; try Beta for speed/quality experiments.
> Both channels share one plugin folder — **the workflow you open decides the version**.
> If Beta fails, reopen a Stable workflow to roll back. See [Dual-version guide](docs/en/DUAL_VERSION.md).

---

## In 30 seconds

- **One node**: script in, film out; assets, director, review and editing all inside;
- **H3-native format**: system prompt V3.0 "contract alignment" (`h3_v3_contract`), three fields, `[Shot]`, camera type/amplitude/speed, dialogue format;
- **Segment continuity**: 22-frame context overlap between segments;
- **Review & LOCK**: asset/segment/full-film review + LOCK reuse, rework on failure;
- **Open and editable**: full workflows included; default style is only an example — **replace it**.

## Install (3 minutes)

> Full guide (models, API key, start, troubleshooting): **[Installation guide (EN)](docs/en/INSTALL.md)**.

**1. Get the code**

```bash
git clone https://github.com/minzzj/DramaCore.git
cd DramaCore
```

No Git: download [main.zip](https://github.com/minzzj/DramaCore/archive/refs/heads/main.zip) and unzip.

**2. Preview the install plan (dry-run, writes nothing)**

```powershell
# Windows: replace D:\ComfyUI with your ComfyUI path
powershell -ExecutionPolicy Bypass -File .\install\install_windows.ps1 -Mode attach -ComfyUI "D:\ComfyUI" -DryRun
```

```bash
# Linux / macOS
chmod +x install/install_linux.sh
./install/install_linux.sh --mode attach --comfyui /opt/ComfyUI --dry-run
```

**3. Install**

```powershell
powershell -ExecutionPolicy Bypass -File .\install\install_windows.ps1 -Mode attach -ComfyUI "D:\ComfyUI" -Yes
```

```bash
./install/install_linux.sh --mode attach --comfyui /opt/ComfyUI --yes
```

The installer detects GPU/backend/VRAM → installs dependencies → places the plugin and orchestrator → creates launchers (`start_dramacore.bat` / `.sh`).
**It does not download models**; see the guide for models and API key.

> No ComfyUI yet? Use `-Mode bootstrap` / `--mode bootstrap` to prepare ComfyUI (models still excluded).

## Workflows

### Stable (default)

| File (`bettercomfyui_comfy/workflows/`) | Purpose |
|---|---|
| `【新版】剧核_单节点_剧本到成片_快速4步版_20261024.json` | Fast test: 1152×640 / 4 steps / ref2v 4-step LoRA |
| `【新版】剧核_单节点_剧本到成片_真实参数版_20261024.json` | Final output: 1344×768 / 8 steps / ref2v 8-step LoRA |
| `00_【单节点】剧本→成片（无外部依赖）.json` | Full one-node pipeline (editable) |
| Other `W*/S*/P*/00_*` | Sub-flows for assets/review/editing |

### Beta (public beta, optional)

| File (`beta/workflows/` or the 【内测版】 menu) | Spec |
|---|---|
| `内测版_高质量_2x15s.json` | 2×15s ≈ 30s; quality first |
| `内测版_极速_2x10s.json` | 2×10s ≈ 20s; speed first |

Beta = pure t2v / no asset-image chain / long segments / no transitions or subtitles / single-pass encoding.
**Unstable; if it fails, switch back to Stable.** See [beta/README_EN.md](beta/README_EN.md).

## Highlights

- **System prompt V3.0 "contract alignment"**: single `h3_v3_contract` (37,457 chars), no legacy rule blocks appended;
- **Automatic colorfix removed**: keep natural lighting; bright days and naturally darker nights;
- **"Alive human" guidance**: realistic skin texture, natural lighting, breathing/blinking micro-motion;
- **H3-native format**: three fields + `[Shot]` + camera type/amplitude/speed + `(S1)(S2)` + `<d>[Chinese]</d>`;
- **Front-end auto-compat**: old workflow parameters auto-healed, no blocking banner;
- **Stable presets**: Real-parameter 1344×768 / 8 steps; Fast 1152×640 / 4 steps.

## Custom style (must change)

The default "unified style" and C1 style lock are **examples**. Replace them for your project:

1. Node "统一风格" (unified style) parameter;
2. System prompt V3.0 `PART C · C1 style lock`;
3. Director panel → Prompt → view/replace the active system prompt.

## Screenshots & promo

![ComfyUI workflow](docs/images/comfyui-workflow.png)

![Director / V3 prompt panel](docs/images/director-panel-v3.png)

![GitHub repository verification](docs/images/github-api-verify.png)

🎬 **Promo (film-frame cut, 48s)**: [BGM](https://github.com/minzzj/DramaCore/releases/download/v0.2.0-internal/DramaCore-promo-fast2-48s-bgm.mp4) · [Silent](https://github.com/minzzj/DramaCore/releases/download/v0.2.0-internal/DramaCore-promo-fast2-48s-silent.mp4) · [Poster](https://github.com/minzzj/DramaCore/releases/download/v0.2.0-internal/DramaCore-promo-fast2-poster.jpg)

## Configuration & key security

- Default config `config/default.yaml`; example `config/user.yaml.example`;
- **No token/API key is stored in the repo**; `config/user.yaml` is gitignored;
- Never put keys in workflow JSON, screenshots or issues; rotate immediately if leaked.

## Documentation

- [Installation guide (EN)](docs/en/INSTALL.md) · [中文安装指南](docs/安装指南_用户版.md)
- [Dual version: Stable vs Beta (EN)](docs/en/DUAL_VERSION.md) · [中文双版本说明](docs/双版本_正式版与内测版.md)
- [Documentation index (EN)](docs/en/INDEX.md) · [中文文档索引](docs/文档索引.md)
- [Chinese README](README.md)
- [Release notes v0.2.0](docs/发布说明_v0.2.0-internal.md)
- [Changelog](CHANGELOG.md)

## License

**CC BY-NC-SA 4.0**
- ✅ Download, view, modify, non-commercial redistribution;
- ✅ Derivatives must use the same license (ShareAlike) and keep attribution;
- ❌ Commercial use is prohibited (NonCommercial).

Because of the NonCommercial clause it is **not an OSI open-source license**; it is "source-available + non-commercial sharing". Full text: [LICENSE](LICENSE).
