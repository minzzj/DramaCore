# Dual Version: Stable vs Beta

> One plugin folder, two workflow sets — the workflow you open defines the channel.
> Stable = delivery-ready; Beta = faster + better-looking public beta.

**Language** · [简体中文](../双版本_正式版与内测版.md) | **English**

## Channels

| Channel | Version | Status | Download |
|---|---|---|---|
| 🟢 Stable | `v0.2.0-internal` | Default; validated | [Stable ZIP](https://github.com/minzzj/DramaCore/releases/download/v0.2.0-internal/DramaCore-v0.2.0-internal.zip) |
| 🟡 Beta (public beta) | `v0.3.0-beta.1` | Prerelease; may be unstable | [Beta ZIP](https://github.com/minzzj/DramaCore/releases/download/v0.3.0-beta.1/DramaCore-v0.3.0-beta.1.zip) |

## Stable

- Purpose: default delivery/output; marked as latest release;
- Workflows (`bettercomfyui_comfy/workflows/`):
  - `【新版】剧核_单节点_剧本到成片_真实参数版_20261024.json`
  - `【新版】剧核_单节点_剧本到成片_快速4步版_20261024.json`
- Pipeline: r2v + asset review/LOCK; Real-parameter 1344×768 / 8 steps; Fast 1152×640 / 4 steps.

## Beta (public beta)

- Purpose: validate a faster + better-looking pipeline; **do not use for final delivery yet**;
- Workflows (`beta/workflows/`, also visible as 【内测版】 after installing the beta ZIP):
  | File | Spec | Use |
  |---|---|---|
  | `内测版_高质量_2x15s.json` | 2×15s ≈ 30s | Quality first |
  | `内测版_极速_2x10s.json` | 2×10s ≈ 20s | Speed first |
- Shared parameters: t2v / no asset-image generation / 1376×768 / 8 steps / 24fps / no transitions / no subtitles / auto-rework off;
- Borrowed from a high-quality external H3 Director workflow: pure t2v, no asset chain, long segments, single-pass encoding.

## Install & switch

1. Stable: install the Stable ZIP, open the 【新版】 workflows;
2. Beta: install the Beta ZIP (may overwrite the same `custom_nodes/bettercomfyui_comfy`), restart, Ctrl+F5, open 【内测版】 workflows;
3. **Switching = opening the other workflow set**; no uninstall/reinstall;
4. Version markers: Stable `__version__=0.2.0-internal`; Beta `__version__=0.3.0-beta.1`, UI badge shows "内测 0.3.0-beta.1".

## Rollback (important)

- Beta fails / quality regresses → reopen a Stable workflow; Stable files are untouched;
- ComfyUI fails to start after installing Beta → move/delete the `beta/` folder and restart;
- Do not make Beta your default delivery version; wait for GPU A/B validation before merging into Stable;
- Code changes keep `.bak_r83_*` / `.bak_r84_*` backups; compile/check failures roll back automatically.

## Key differences

| Item | Stable | Beta |
|---|---|---|
| Task mode | r2v + asset LOCK | t2v pure text-to-video |
| Asset images | On (SDXL) | Off |
| Segments/length | 1×5.167s (real-param) | 2×15s / 2×10s |
| Resolution | 1344×768 | 1376×768 |
| Transitions/subtitles | Auto / hard subtitles | None / none (single encode) |
| Auto-rework | On | Off (speed first) |
| Status | Stable delivery | Public beta |
