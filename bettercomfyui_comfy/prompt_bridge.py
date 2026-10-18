"""提示词桥接节点 —— 把剧本巨人的输出接到一体化导演台。

## 为什么需要它（图一 SG1 的界面接缝）

`SG1`「剧本 → 提示词」这段逻辑**库里早就实现了**（`prompt/gateway.py` 的
`ExternalApiGateway` / `LocalQwenGateway` / `CustomGateway`，加 `validate_script`），
但**界面上没有任何节点调用它** —— 用户在工作流里点不出"给剧本出提示词"。

按"站在巨人肩膀上"的路线，正确做法不是再写一套提示词生成器，而是接现成巨人：

    StoryboardNode              我的桥接节点              一体化导演台
    （DeepSeek 拆剧本）   ->   （转成导演台格式）   ->   BetterComfyUIOneDirector
    out: image_prompts           out: 剧本分镜            out: 导演组
         video_prompts              提示词JSON
         dialogues

## 巨人输出长什么样（实测其源码的第 339-353 行）

```
[镜头1] 主角推开木门走进房间，镜头缓慢推进

[镜头2] 主角在桌前坐下，抬头看向窗外
```

也就是：**每个镜头一行标签 + 提示词，镜头之间用空行分隔**。
导演台要的是 `---` 独占一行分隔的多段文本。所以桥接节点做的是：

1. 按 `[镜头N]` 标签或空行切分；
2. 去掉标签、清理空白；
3. 用 `\n---\n` 重新拼成 `剧本分镜`；
4. 同时输出结构化 JSON（便于审计与下游消费）。

## 容错

* 没有 `[镜头N]` 标签（模型没按格式写）-> 退回按空行切分；
* 只有空行没有标签 -> 按空行切分；
* 全都没有 -> 把整段当成一个镜头（**不丢内容**，并在摘要里说明）；
* `video_prompts` 为空 -> 退回用 `image_prompts`（至少别让下游戏空转）。
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = [
    "BetterComfyUIPromptBridge",
    "parse_shot_prompts",
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
]

#: `[镜头N]` / `[镜头 1]` / `[shot 3]` / `[镜头3｜...]` 之类的行首标签。
_SHOT_LABEL = re.compile(
    r"^\s*[\[【]\s*(?:镜头|鏡頭|shot|Scene|scene)\s*(\d+)\s*[\]】]\s*[|｜:：\-—]?\s*",
    re.MULTILINE,
)


def parse_shot_prompts(text: Any) -> list[str]:
    """把巨人的 `video_prompts` 文本切成**镜头提示词列表**。

    切分策略（从强到弱，逐级降级，**任何一级都不丢内容**）：

    1. 有 `[镜头N]` 标签 -> 按标签切；
    2. 没有标签但有空行 -> 按空行切；
    3. 都没有 -> 整段算一个镜头。
    """
    raw = str(text or "").strip()
    if not raw:
        return []

    if _SHOT_LABEL.search(raw):
        # 用标签位置切分（标签本身去掉）
        parts: list[str] = []
        matches = list(_SHOT_LABEL.finditer(raw))
        for index, match in enumerate(matches):
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
            piece = raw[start:end].strip()
            if piece:
                parts.append(piece)
        if parts:
            return parts

    # 退路：按空行切分
    chunks = [c.strip() for c in re.split(r"\n\s*\n", raw) if c.strip()]
    if len(chunks) > 1:
        return chunks

    return [raw]


class BetterComfyUIPromptBridge:
    """把剧本巨人的提示词输出转成一体化导演台能吃的 `剧本分镜` 与 JSON。"""

    CATEGORY = "BetterComfyUI/提示词"
    FUNCTION = "bridge"
    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("剧本分镜", "提示词JSON", "摘要")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                # 接 StoryboardNode 的第 2 个输出（video_prompts）
                "video_prompts": ("STRING", {
                    "forceInput": True,
                    "tooltip": (
                        "接「Storyboard Generator (DeepSeek)」的 video_prompts 输出。\n"
                        "本节点会把它转成一体化导演台要的 `剧本分镜`（--- 分隔）。"
                    ),
                }),
            },
            "optional": {
                # 接 StoryboardNode 的第 1 个输出（image_prompts），作为兜底
                "image_prompts": ("STRING", {
                    "forceInput": True,
                    "tooltip": "接 image_prompts。video_prompts 为空时用它兜底。",
                }),
                "参考图描述": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "tooltip": (
                        "可选：把 image_prompts 里的角色/场景描述也写进来，"
                        "帮助导演台把素材锚定得更准。留空则忽略。"
                    ),
                }),
                "统一风格": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "tooltip": (
                        "可选的整片统一风格（支持 @标签名 展开）。\n"
                        "⚠️ 只写进「提示词JSON」，**不拼进每段文本**。\n"
                        "统一风格请在一体化导演台的「统一风格」里设置，避免重复注入。"
                    ),
                }),
                "段时长_秒": ("FLOAT", {
                    "default": 10.0, "min": 0.2, "max": 120.0, "step": 0.1,
                    "tooltip": "每一段的时长，会写进输出 JSON，供下游参考。",
                }),
            },
        }

    def bridge(
        self,
        video_prompts: str,
        image_prompts: str | None = None,
       参考图描述: str = "",
        统一风格: str = "",
        段时长_秒: float = 10.0,
    ):
        shots = parse_shot_prompts(video_prompts)
        source = "video_prompts"
        if not shots and image_prompts:
            shots = parse_shot_prompts(image_prompts)
            source = "image_prompts（video_prompts 为空，已兜底）"

        notes: list[str] = []
        if not shots:
            notes.append("两个输入都为空：没有可用的镜头提示词")
        if 参考图描述 and 参考图描述.strip():
            notes.append("已附带参考图描述（供人工/下游参考，不自动改提示词）")

        # 风格标签展开（图一 C6），并注入到每一段结尾
        style = ""
        if 统一风格 and 统一风格.strip():
            try:
                from bettercomfyui.prompt.styles import expand_style_tokens

                style = expand_style_tokens(统一风格.strip())
            except Exception:  # noqa: BLE001 - 风格库不可用不该让桥接失败
                style = 统一风格.strip()

        # ⚠️ **不把风格拼进每段文本**。
        # 实测踩坑：桥接拼一次、下游一体化导演台又拼一次，
        # 提示词里出现「全片视觉风格：冷蓝…。全片视觉风格：写实电影感」——
        # 重复注入既浪费 token，也可能让模型抓错重点。
        # 风格是**导演台的职责**（它有「统一风格」输入），桥接只把风格透传到 JSON。
        prompts: list[str] = [shot for shot in shots]

        # 导演台要的 `---` 分隔格式
        script_text = "\n---\n".join(prompts)

        payload = {
            "schema": "bettercomfyui.script/v1",
            "source": source,
            "generator": "ComfyUI-Storyboard-LLM (DeepSeek)",
            "segment_count": len(prompts),
            "segment_seconds": float(段时长_秒),
            "global_style": style,
            "reference_descriptions": (参考图描述 or "").strip(),
            "segments": [
                {"segment_index": i, "prompt": p, "asset_refs": []}
                for i, p in enumerate(prompts, start=1)
            ],
            "notes": notes,
        }
        payload_json = json.dumps(payload, ensure_ascii=False, indent=2)

        summary_lines = [
            f"镜头数：{len(prompts)}（来源：{source}）",
            f"段时长：{float(段时长_秒)}s",
        ]
        if style:
            summary_lines.append(f"统一风格：{style[:80]}")
        for note in notes:
            summary_lines.append(f"注意：{note}")
        if not prompts:
            summary_lines.append("警告：输出为空，下游会拒绝空计划")

        return (script_text, payload_json, "\n".join(summary_lines))


NODE_CLASS_MAPPINGS = {"BetterComfyUIPromptBridge": BetterComfyUIPromptBridge}
NODE_DISPLAY_NAME_MAPPINGS = {
    "BetterComfyUIPromptBridge": "提示词桥接（DeepSeek → 导演台）",
}
