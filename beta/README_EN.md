# Beta Channel (Public Beta) · DramaCore

> ⚠️ **This is a beta, not the stable release.** It may be unstable; do not use it for final delivery.
> Stable: see the repository root / Release [v0.2.0-internal](https://github.com/minzzj/DramaCore/releases/tag/v0.2.0-internal).

**Language** · [简体中文](README.md) | **English**

## Version info

| Item | Value |
|---|---|
| Channel | Beta (public beta) |
| Version | `0.3.0-beta.1` |
| Purpose | Faster + better-looking new pipeline |
| Base | Stable `v0.2.0-internal` + ROUND83/84 security & stability fixes |
| Download | [Beta ZIP](https://github.com/minzzj/DramaCore/releases/download/v0.3.0-beta.1/DramaCore-v0.3.0-beta.1.zip) |

## Beta workflows

| File | Segments × length | Use |
|---|---|---|
| `workflows/内测版_高质量_2x15s.json` | 2×15s ≈ 30s | Quality first |
| `workflows/内测版_极速_2x10s.json` | 2×10s ≈ 20s | Speed first |

Shared: t2v / asset generation off / 1376×768 / 8 steps / 24fps / no transitions / no subtitles / auto-rework off / Refine off.
After installing the Beta ZIP, the 【内测版】 workflows also appear in the ComfyUI workflow menu.

## Install

1. Unzip the Beta ZIP;
2. Put `bettercomfyui_comfy` into ComfyUI `custom_nodes/` (can overwrite/merge with Stable);
3. Restart ComfyUI → Ctrl+F5;
4. Open a 【内测版】 workflow.

## Roll back to Stable (important)

- Beta fails/looks worse → reopen a Stable workflow:
  - `【新版】剧核_单节点_剧本到成片_真实参数版_20261024.json`
  - or `…快速4步版…json`
- Stable files are untouched; no reinstall needed;
- If the Beta package prevents startup: remove the `beta/` folder and restart.

## Test checklist

| Item | Record |
|---|---|
| Time | total / per-segment |
| VRAM | peak GB |
| Visuals | seams, character consistency, lip-sync |
| Errors | full log/screenshot |

## Known risks

- 2×15s/2×10s long segments need more VRAM; OOM may auto-degrade (continuity off, batch=1);
- t2v has no reference images → weaker identity consistency than Stable r2v + LOCK assets;
- No formal GPU A/B yet; public beta. Merge into Stable only after validation.

License: CC BY-NC-SA 4.0 (commercial use prohibited; derivatives same license).
