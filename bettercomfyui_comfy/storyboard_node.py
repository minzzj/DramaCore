"""剧本→提示词节点（DeepSeek / 千问 / 自配接口），**能接 API 密钥**。

## 为什么要自己写这个节点

剧本巨人 `ComfyUI-Storyboard-LLM` 的 `StoryboardNode` **没有 api_key 输入端口**，
它只读自己目录下的 `settings.json`。后果是：
* `BetterComfyUISettings` 的 `api_key` 输出**没地方接**（用户实测指出）；
* 工作流里"API 配置 → 剧本"这一环是**断的**，不能算完整工作流。

本节点补上这一环：直接吃 `api_key / model / api_base` 三个输入，
复用编排包里的 `build_gateway()`（外部 API / 本地千问 / 自配接口都支持）。

## 输出与桥接节点对齐

`video_prompts` 输出格式与剧本巨人一致（`[镜头N] 提示词`，空行分隔），
所以可以直接接 `BetterComfyUIPromptBridge`。同时给出规范 JSON，
方便下游消费与审计。

## 没有密钥也能跑（占位模式）

`使用占位结果=True`（或没有密钥时显式选择）会产出**确定性演示剧本**，
让整条工作流在没有 API 密钥时也能跑通、能看结构。
但输出摘要在**首行大字标注「占位」**，绝不冒充真实模型输出。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

__all__ = [
    "BetterComfyUIStoryboard",
    "build_placeholder_storyboard",
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
]

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 系统提示词：要求模型输出**可解析的镜头列表**。
SYSTEM_PROMPT = """你是专业影视分镜师。把用户给的剧本拆成若干镜头。

严格按下面的 JSON 结构输出，不要输出任何解释文字、不要用 markdown 代码块：
{
  "title": "标题",
  "shots": [
    {"index": 1, "prompt": "这个镜头的完整视频提示词（中文，包含人物动作、镜头运动、光线、氛围）"},
    {"index": 2, "prompt": "..."}
  ]
}

要求：
1. 每个镜头 5-12 秒可拍完，按叙事顺序排列；
2. 提示词要具体：谁、在做什么、镜头怎么动、什么光、什么氛围；
3. 人物外观与场景描述在各镜头之间保持**一致**（同一人物同一造型）；
4. 只输出 JSON。
"""


def build_placeholder_storyboard(chapter_text: str, segment_seconds: float = 10.0) -> dict:
    """没有 API 密钥时的**确定性占位剧本**（明确标注是占位）。"""
    text = (chapter_text or "").strip()
    paras = [p.strip() for p in re.split(r"[\n。！？!?]+", text) if p.strip()]
    if len(paras) < 2:
        paras = [
            "主角推门走进房间，镜头缓慢推进，冷蓝夜色，空气中有浮尘",
            "主角在桌前坐下，抬头看向窗外，特写手部动作",
            "镜头拉远，画面定格在窗外的雪夜",
        ]
    shots = []
    for index, para in enumerate(paras[:6], start=1):
        shots.append({
            "index": index,
            "prompt": f"{para}。镜头运动平稳，自然光与冷色补光，电影感构图，"
                      f"约 {int(segment_seconds)} 秒",
        })
    return {"title": "占位剧本（未调用真实模型）", "shots": shots}


def _parse_json_block(text: str) -> dict | None:
    """从模型输出里抠出 JSON（容忍 ```json 代码块与前后废话）。"""
    raw = (text or "").strip()
    if not raw:
        return None
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1).strip()
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except Exception:  # noqa: BLE001
                return None
    return None


class BetterComfyUIStoryboard:
    """剧本 → 镜头提示词（走真实大模型；无密钥可用占位模式）。"""

    CATEGORY = "BetterComfyUI/提示词"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("video_prompts", "script_json", "摘要")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "剧本": ("STRING", {
                    "multiline": True,
                    "default": (
                        "第一章 雪夜\n"
                        "主角林秀在雪夜推门走进客栈，老陈抬头看她，两人对视。\n"
                        "林秀在桌边坐下，窗外雪落无声。"
                    ),
                    "tooltip": "要拆成镜头的剧本原文。",
                }),
                "段时长_秒": ("FLOAT", {
                    "default": 10.0, "min": 1.0, "max": 120.0, "step": 0.5,
                    "tooltip": "期望每个镜头的时长，会写进提示词与 JSON。",
                }),
                "使用占位结果": ("BOOLEAN", {
                    "default": False,
                    "tooltip": (
                        "True = 不调用大模型，直接产出确定性占位剧本"
                        "（没有 API 密钥时用来跑通工作流结构）。\n"
                        "False = 必须调用真实模型（无密钥会明确报错）。"
                    ),
                }),
            },
            "optional": {
                # ↓ 这三根线来自 `BetterComfyUISettings`
                "api_key": ("STRING", {
                    "forceInput": True,
                    "tooltip": "接「API 配置」节点的 api_key 输出。",
                }),
                "model": ("STRING", {
                    "forceInput": True,
                    "tooltip": "接「API 配置」节点的 model 输出（如 deepseek-chat）。",
                }),
                "api_base": ("STRING", {
                    "forceInput": True,
                    "tooltip": "接「API 配置」节点的 api_base 输出。",
                }),
                "风格": ("STRING", {
                    "multiline": True, "default": "电影感，真实自然，自然光影，正常曝光，场景自适应",
                    "tooltip": "整片统一风格，会注入每个镜头提示词。",
                }),
            },
        }

    def run(
        self,
        剧本: str,
        段时长_秒: float = 10.0,
        使用占位结果: bool = False,
        api_key: str = "",
        model: str = "",
        api_base: str = "",
        风格: str = "",
    ):
        style = (风格 or "").strip()
        key = (api_key or "").strip()
        model_name = (model or "").strip() or "deepseek-chat"
        base = (api_base or "").strip()

        if 使用占位结果 or not key:
            if not key and not 使用占位结果:
                # 明确报错：不静默降级（否则用户以为调通了真实模型）
                raise RuntimeError(
                    "没有拿到 API 密钥：请把「API 配置」节点的 api_key 接进来，"
                    "或在 ComfyUI 原生「设置」里填 StoryBoard.api_key，"
                    "或把「使用占位结果」设为 True 仅测试工作流结构。"
                )
            payload = build_placeholder_storyboard(剧本, 段时长_秒)
            mode_note = "占位（未调用真实模型）"
        else:
            payload = self._call_model(
                chapter_text=剧本, api_key=key, model=model_name,
                api_base=base, style=style, segment_seconds=段时长_秒)
            mode_note = f"真实模型（{model_name}）"

        shots = payload.get("shots") or []
        prompts: list[str] = []
        for index, shot in enumerate(shots, start=1):
            text = str(shot.get("prompt") or "").strip()
            if not text:
                continue
            if style:
                text = f"{text}。全片视觉风格：{style}"
            prompts.append(f"[镜头{index}] {text}")

        video_prompts = "\n\n".join(prompts)
        script_json = json.dumps({
            "schema": "bettercomfyui.script/v1",
            "title": payload.get("title", ""),
            "mode": mode_note,
            "segment_seconds": float(段时长_秒),
            "global_style": style,
            "segment_count": len(prompts),
            "segments": [
                {"segment_index": i, "prompt": p, "asset_refs": []}
                for i, p in enumerate(prompts, start=1)
            ],
        }, ensure_ascii=False, indent=2)

        head = "⚠️ 占位结果（未调用真实模型）" if "占位" in mode_note else f"✅ {mode_note}"
        summary = (
            f"{head}\n"
            f"镜头数：{len(prompts)}\n"
            f"段时长：{float(段时长_秒)}s\n"
            f"模型：{model_name if '真实' in mode_note else '（未调用）'}"
        )
        return (video_prompts, script_json, summary)

    # -- 真实调用（复用编排包的 gateway）----------------------------------
    def _call_model(
        self, *, chapter_text: str, api_key: str, model: str,
        api_base: str, style: str, segment_seconds: float,
    ) -> dict:
        try:
            from bettercomfyui.config import Config
            from bettercomfyui.prompt.gateway import build_gateway
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"编排包不可用，无法调用真实模型：{type(exc).__name__}: {exc}\n"
                "可以先把「使用占位结果」设为 True 测试工作流结构。"
            ) from exc

        cfg = Config()
        cfg.prompt.model_provider = "external_api"
        cfg.prompt.api_key = api_key
        cfg.prompt.model_name = model
        if api_base:
            cfg.prompt.endpoint = api_base

        gateway = build_gateway(cfg)
        user_prompt = (
            f"剧本：\n{chapter_text}\n\n"
            f"要求：每个镜头约 {float(segment_seconds)} 秒；"
            + (f"整片视觉风格：{style}。" if style else "")
        )
        text = gateway.complete(system=SYSTEM_PROMPT, user=user_prompt)
        payload = _parse_json_block(text)
        if not payload or not isinstance(payload.get("shots"), list):
            raise RuntimeError(
                "大模型返回的内容解析不出镜头列表（要求 JSON）。\n"
                f"原始输出前 500 字：{str(text)[:500]}"
            )
        return payload


NODE_CLASS_MAPPINGS = {"BetterComfyUIStoryboard": BetterComfyUIStoryboard}
NODE_DISPLAY_NAME_MAPPINGS = {
    "BetterComfyUIStoryboard": "剧本→提示词（DeepSeek 可接密钥）",
}
