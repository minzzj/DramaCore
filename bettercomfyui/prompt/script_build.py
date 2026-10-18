"""一次 API 调用 → 全链路提示词 → 按素材类型拆分 → 标准 JSON（图一 SG1 A–E / SG2 输入）。

## 为什么需要这个模块

用户基准（2026-10-17 报障）：

1. 输入为**一整段完整剧本**，全程**仅发起一次 API 调用**，一次性输出全链路所需的
   **全部提示词**（所有素材类提示词 + 视频生成提示词）；
2. 系统按预设分隔符自动拆出「人物图 / 场景图 / 物品图 / 分镜图」等不同素材提示词，
   并以**标准 JSON** 自动填充到对应工作流节点。

对照 `文档/BetterComfyUI_完整总流程图.dot`：

* `A` 剧本输入 → `D` AI 理解拆解 → `E` 输出提示词（`E1` 角色 / `E2` 场景 / `E3` 物品 /
  `E4` 分镜 / `E5` 视频）——**一次调用必须同时产出 E1–E5**；
* `E6` 之后的存在性匹配由程序做（`:mod:`bettercomfyui.prompt.match`），本模块不碰；
* `F` 特殊分隔符自动分段 —— 由 :func:`bettercomfyui.prompt.validate.parse_with_report`
  的 `auto` 路径承担（JSON 优先，失败再按预设分隔符解析，两条路径都显式记录）。

## 为什么放在编排层而不是节点里

界面节点只是壳；「怎么组装提示词、怎么解析、怎么校验、怎么拆分」属于编排逻辑，
必须有单测守着。节点（`bettercomfyui_comfy/script_prompts.py`）只负责把配置喂进来、
把结果摆到输出端口上。

## 一次调用的硬约束

:func:`generate_script_prompts` 内部**只调用一次** ``gateway.complete(...)``，
并把 ``api_calls`` 写进结果元数据。任何重试（`gateway` 内部的网络重试）不算"第二次
业务调用"——它仍是同一个请求逻辑，且 `max_retries` 由配置控制。

## 绝不静默

* API 失败 → :class:`ScriptBuildError`，消息含可操作原因（401/超时/缺 key）；
* 模型输出解析不了 / 结构不合法 → :class:`ScriptBuildError`，附解析报告与原文前 500 字；
* **不返回部分结果**：要么完整成功，要么抛错。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..models.enums import AssetType
from .errors import PromptError
from .gateway import GatewayError, build_gateway
from .inject import build_prompt_messages
from .validate import (
    ScriptValidationError,
    canonicalize_script,
    parse_with_report,
    validate_script,
)

__all__ = [
    "ASSET_TYPE_ORDER",
    "ASSET_TYPE_LABELS",
    "ScriptBuildError",
    "ScriptPromptResult",
    "generate_script_prompts",
    "build_placeholder_prompts",
    "split_assets_by_type",
    "collect_video_prompts",
]


class ScriptBuildError(PromptError):
    """一次调用构建提示词失败（API / 解析 / 结构校验）。**绝不静默降级**。"""


#: 图一 E1–E4 的顺序（角色 > 场景 > 物品 > 分镜，与 G5 优先级一致）
ASSET_TYPE_ORDER: tuple[AssetType, ...] = (
    AssetType.CHARACTER,
    AssetType.SCENE,
    AssetType.PROP,
    AssetType.SHOT,
)

ASSET_TYPE_LABELS: dict[str, str] = {
    AssetType.CHARACTER.value: "角色图提示词",
    AssetType.SCENE.value: "场景图提示词",
    AssetType.PROP.value: "物品图提示词",
    AssetType.SHOT.value: "分镜图提示词",
}


# ---------------------------------------------------------------------------
# 纯函数：拆分 / 汇总（可单测，不依赖网络）
# ---------------------------------------------------------------------------


def _type_value(raw: Any) -> str:
    """把 asset_type 统一成字符串值（枚举与字符串都接受）。"""
    if isinstance(raw, AssetType):
        return raw.value
    return str(raw or "").strip().upper()


def split_assets_by_type(assets: Iterable[Mapping[str, Any]]) -> dict[str, list[dict]]:
    """把校验后的 assets 按 E1–E4 四类拆开（保持原顺序；未知类型进 ``OTHER``）。

    返回值为 **可直接 json.dumps 的标准结构**：每个元素只保留
    ``id / name / prompt / asset_type / negative_prompt（若有）``，
    方便下游节点与人工核对。空类别也返回空数组（前端/预览能显示出"0 个"）。
    """
    grouped: dict[str, list[dict]] = {t.value: [] for t in ASSET_TYPE_ORDER}
    grouped["OTHER"] = []
    for item in assets or []:
        if not isinstance(item, Mapping):
            continue
        type_value = _type_value(item.get("asset_type"))
        record: dict[str, Any] = {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or ""),
            "asset_type": type_value,
            "prompt": str(item.get("prompt") or ""),
        }
        negative = item.get("negative_prompt")
        if isinstance(negative, str) and negative.strip():
            record["negative_prompt"] = negative.strip()
        bucket = grouped.get(type_value)
        if bucket is None:
            grouped["OTHER"].append(record)
        else:
            bucket.append(record)
    return grouped


def collect_video_prompts(script: Mapping[str, Any]) -> list[str]:
    """从 chapters[].segments[] 收集视频段提示词（E5），按章节/段顺序。"""
    prompts: list[str] = []
    for chapter in script.get("chapters") or []:
        if not isinstance(chapter, Mapping):
            continue
        for segment in chapter.get("segments") or []:
            if not isinstance(segment, Mapping):
                continue
            text = str(segment.get("prompt") or "").strip()
            if text:
                prompts.append(text)
    return prompts


def _format_errors(exc: ScriptValidationError) -> str:
    errors = getattr(exc, "errors", None)
    if not errors:
        return str(exc)
    lines = []
    for err in errors:
        if isinstance(err, Mapping):
            lines.append(
                f"  - {err.get('path', '$')}: {err.get('code', '')} {err.get('message', '')}".rstrip()
            )
        else:  # pragma: no cover - 防御
            lines.append(f"  - {err}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------


@dataclass
class ScriptPromptResult:
    """一次 API 调用的完整产物（可直接映射为节点输出端口）。"""

    raw_text: str
    script: dict
    assets_by_type: dict[str, list[dict]]
    video_prompts: list[str]
    meta: dict = field(default_factory=dict)

    # -- 便捷视图 ------------------------------------------------------
    @property
    def api_calls(self) -> int:
        return int(self.meta.get("api_calls") or 0)

    @property
    def mode(self) -> str:
        return str(self.meta.get("mode") or "真实模型")

    @property
    def asset_prompts_json(self) -> str:
        """四类素材提示词的标准 JSON（E1–E4）。"""
        payload = {
            "schema": "bettercomfyui.asset-prompts/v1",
            "source": self.meta.get("mode", ""),
            "counts": {key: len(value) for key, value in self.assets_by_type.items()},
            "assets": self.assets_by_type,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def typed_asset_prompts_json(self, type_name: str) -> str:
        """单类素材提示词的 JSON（如 CHARACTER / SCENE / PROP / SHOT）。"""
        type_value = _type_value(type_name)
        items = self.assets_by_type.get(type_value, [])
        payload = {
            "schema": "bettercomfyui.asset-prompts/v1",
            "asset_type": type_value,
            "label": ASSET_TYPE_LABELS.get(type_value, type_value),
            "count": len(items),
            "assets": items,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @property
    def video_prompts_text(self) -> str:
        """视频段提示词文本：独占一行的 ``---`` 分隔，可直接接一体化导演台。"""
        return "\n---\n".join(self.video_prompts)

    @property
    def video_prompts_json(self) -> str:
        payload = {
            "schema": "bettercomfyui.video-prompts/v1",
            "count": len(self.video_prompts),
            "separator": "---",
            "segments": [
                {"segment_index": index, "prompt": prompt}
                for index, prompt in enumerate(self.video_prompts, start=1)
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @property
    def full_json(self) -> str:
        """标准 JSON：素材 + 章节分镜 + 拆分结果 + 元数据（一份文件可追溯）。"""
        payload = {
            "schema": "bettercomfyui.prompts/v1",
            "meta": dict(self.meta),
            "title": self.script.get("title", ""),
            "assets": self.script.get("assets", []),
            "chapters": self.script.get("chapters", []),
            "asset_prompts_by_type": self.assets_by_type,
            "video_prompts": self.video_prompts,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @property
    def summary(self) -> str:
        counts = {key: len(value) for key, value in self.assets_by_type.items()}
        head = "✅" if self.api_calls >= 1 else "⚠️"
        mode_text = self.mode.replace("⚠️", "").strip() or self.mode
        model = str(self.meta.get("model") or "")
        model_suffix = f"（{model}）" if model and not model.startswith("（") else ""
        lines = [
            f"{head} {mode_text}｜API 调用 {self.api_calls} 次{model_suffix}",
            f"素材：角色 {counts.get('CHARACTER', 0)} / 场景 {counts.get('SCENE', 0)}"
            f" / 物品 {counts.get('PROP', 0)} / 分镜 {counts.get('SHOT', 0)}"
            + (f" / 其他 {counts['OTHER']}" if counts.get("OTHER") else ""),
            f"视频段：{len(self.video_prompts)}（--- 分隔，可直接接一体化导演台）",
        ]
        detected = self.meta.get("output_format_detected") or self.meta.get("output_format")
        if detected:
            lines.append(f"输出格式：{detected}")
        if self.meta.get("elapsed_s") is not None:
            lines.append(f"耗时：{float(self.meta['elapsed_s']):.2f}s")
        if self.meta.get("notes"):
            lines.extend(f"注意：{note}" for note in self.meta["notes"])  # type: ignore[index]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 一次调用主入口
# ---------------------------------------------------------------------------


def _normalize_output_format(value: Any) -> str:
    """把界面/配置里的输出格式统一成 validate 认识的 json / delimited / auto。"""
    if value is None:
        return "auto"
    text = str(value).strip().lower()
    if text in ("json", "结构化json", "结构化 json"):
        return "json"
    if text in ("delimited", "delimiter", "text", "plain", "分隔符", "分隔符文本", "纯文本"):
        return "delimited"
    return "auto"


def generate_script_prompts(
    *,
    cfg: Any,
    script_text: str,
    manifest: str = "",
    chapter_hint: str | None = None,
    output_format: Any = "auto",
    style_hint: str = "",
    gateway: Any = None,
    transport: Any = None,
    extra_meta: Mapping[str, Any] | None = None,
) -> ScriptPromptResult:
    """**一次** API 调用 → 解析 → 校验 → 按类型拆分。

    :param cfg: 配置对象（``bettercomfyui.config.Config`` 或任何支持 prompt.* 取值的东西）
    :param script_text: 一整段剧本正文（不能为空）
    :param manifest: 既有素材清单原文（图一 A2/A3 注入；空字符串 = 空库分支）
    :param output_format: ``auto``（默认；JSON 优先，失败按预设分隔符）/ ``json`` / ``delimited``
    :param style_hint: 整片统一风格（会追加到用户提示词；素材图提示词与视频段都由它约束）
    :param gateway: 可注入的网关（测试用替身；**不传则按配置构造真实网关**）
    :param transport: 传给 :func:`build_gateway` 的 HTTP 传输层（测试注入，避免真网络）
    :raises ScriptBuildError: API 失败、解析失败或结构校验失败
    """
    if not isinstance(script_text, str) or not script_text.strip():
        raise ScriptBuildError("剧本正文为空：请给一段完整剧本文本（不能是空串/纯空白）")

    messages = build_prompt_messages(
        script_text=script_text,
        cfg=cfg,
        manifest=manifest or "",
        chapter_hint=chapter_hint,
    )
    style_text = (style_hint or "").strip()
    if style_text:
        # 风格约束写在用户提示词末尾：一次调用里同时约束 E1–E5，不额外再调一次模型。
        messages["user"] = (
            f"{messages['user']}\n\n【整片视觉风格（所有素材与镜头必须一致）】{style_text}"
        )

    started = time.monotonic()
    api_calls = 0
    try:
        model_gateway = gateway if gateway is not None else build_gateway(cfg, transport=transport)
    except GatewayError as exc:
        raise ScriptBuildError(
            "API 配置不完整，无法发起调用：\n"
            f"{exc}\n"
            "请在 ComfyUI 原生「设置 → Better ComfyUI」或右侧面板填写 API 密钥/模型/地址，"
            "或把本节点的「使用占位结果」设为 True 只测试工作流结构。"
        ) from exc

    try:
        # 一次业务调用：先计数再发（失败时也如实报「已发起几次」，不把失败说成零次）
        api_calls += 1
        raw_text = model_gateway.complete(system=messages["system"], user=messages["user"])
    except GatewayError as exc:
        raise ScriptBuildError(
            f"模型调用失败（已发起 API 调用 {api_calls} 次）：\n{exc}"
        ) from exc

    elapsed = time.monotonic() - started
    fmt = _normalize_output_format(output_format)

    try:
        parsed, report = parse_with_report(raw_text, output_format=fmt)
    except ScriptValidationError as exc:
        raise ScriptBuildError(
            "模型返回解析失败（已按 JSON 与预设分隔符两条路径尝试）：\n"
            f"{_format_errors(exc)}\n\n"
            f"原始输出前 500 字：\n{raw_text[:500]}"
        ) from exc

    try:
        canonical = canonicalize_script(parsed)
        dropped_refs: list[str] = []
        script = validate_script(canonical, allow_drop_invalid_refs=True,
                                 dropped_refs=dropped_refs)
    except ScriptValidationError as exc:
        raise ScriptBuildError(
            "模型返回的结构不合法（不会进入素材生成）：\n"
            f"{_format_errors(exc)}\n\n"
            f"原始输出前 500 字：\n{raw_text[:500]}"
        ) from exc

    grouped = split_assets_by_type(script.get("assets") or [])
    video_prompts = collect_video_prompts(script)

    describe = getattr(model_gateway, "describe", None)
    gateway_info = describe() if callable(describe) else {}
    #: ParseReport.as_dict(): {requested_format, method, extracted_from, warnings}
    #: method = "json" | "delimited" —— 这就是实际生效的解析路径（不是猜的）。
    output_format_detected = getattr(report, "method", None)

    meta: dict[str, Any] = {
        "mode": "真实模型（一次调用）",
        "api_calls": api_calls,
        "elapsed_s": round(elapsed, 3),
        "provider": gateway_info.get("provider", ""),
        "model": gateway_info.get("model", ""),
        "url": gateway_info.get("url", ""),
        "output_format_requested": fmt,
        "output_format_detected": output_format_detected,
        "manifest_injected": bool(messages.get("manifest")),
        "asset_count": len(grouped.get("CHARACTER", []))
        + len(grouped.get("SCENE", []))
        + len(grouped.get("PROP", []))
        + len(grouped.get("SHOT", [])),
        "video_segment_count": len(video_prompts),
        "notes": [],
    }
    warnings = getattr(report, "warnings", None)
    if warnings:
        meta["notes"].extend(str(w) for w in warnings)
    if extra_meta:
        meta.update(dict(extra_meta))

    if dropped_refs:
        meta["dropped_invalid_refs"] = list(dropped_refs)
    return ScriptPromptResult(
        raw_text=raw_text,
        script=script,
        assets_by_type=grouped,
        video_prompts=video_prompts,
        meta=meta,
    )


# ---------------------------------------------------------------------------
# 占位模式（没有密钥时用它跑通结构；**明确标注占位，绝不冒充真实调用**）
# ---------------------------------------------------------------------------


def build_placeholder_prompts(
    script_text: str,
    *,
    segment_seconds: float = 10.0,
    style: str = "",
) -> ScriptPromptResult:
    """确定性占位剧本：四类素材 + 视频段，结构合法、可被下游消费。

    用途：没有 API 密钥/本机无网络时验证工作流结构与可视化。
    元数据 ``mode`` 明确写「占位（未调用真实模型）」，``api_calls=0``。
    """
    if not isinstance(script_text, str) or not script_text.strip():
        raise ScriptBuildError("剧本正文为空：占位模式也需要至少一句剧本描述")

    text = script_text.strip()
    paragraphs = [
        part.strip()
        for part in __import__("re").split(r"[\n。！？!?]+", text)
        if part.strip()
    ] or ["一个未命名场景"]
    # 只有一句话时补成 3 段：占位模式的意义是"让人看到分段结构与 --- 分隔"，
    # 单段输出会让用户误以为分段功能没生效。
    if len(paragraphs) < 3:
        base = paragraphs[0]
        paragraphs = [
            base,
            f"{base}，镜头推近，人物特写",
            f"镜头拉远，{base} 的环境全景",
        ]
    style_suffix = f"。整体风格：{style}" if style.strip() else ""

    assets: list[dict[str, Any]] = [
        {
            "id": "A1",
            "asset_type": "CHARACTER",
            "name": "主角",
            "prompt": f"{paragraphs[0][:60]} 的主角，正面清晰，电影级光影，细节丰富{style_suffix}",
        },
        {
            "id": "A2",
            "asset_type": "CHARACTER",
            "name": "配角",
            "prompt": f"{paragraphs[-1][:60]} 的配角，服装与造型清晰，中性光{style_suffix}",
        },
        {
            "id": "A3",
            "asset_type": "SCENE",
            "name": "主场景",
            "prompt": f"{text[:80]} 的环境全景，空间结构清晰，氛围光{style_suffix}",
        },
        {
            "id": "A4",
            "asset_type": "PROP",
            "name": "关键道具",
            "prompt": f"与「{paragraphs[0][:40]}」相关的关键道具，特写，材质清晰{style_suffix}",
        },
        {
            "id": "A5",
            "asset_type": "SHOT",
            "name": "分镜参考画面",
            "prompt": "中景构图，人物位于画面三分线，交代场景关系，电影感构图" + style_suffix,
        },
    ]
    shots = []
    for index, paragraph in enumerate(paragraphs[:6], start=1):
        shots.append(
            {
                "segment_index": index,
                "prompt": f"{paragraph}。镜头运动平稳，自然光与冷色补光，约 {int(segment_seconds)} 秒",
                "asset_refs": ["A1", "A3"],
                "duration_s": float(segment_seconds),
            }
        )
    if not shots:  # pragma: no cover - paragraphs 至少有 1 条
        shots.append({"segment_index": 1, "prompt": text, "asset_refs": [], "duration_s": 10.0})

    placeholder_script = {
        "title": "占位剧本（未调用真实模型）",
        "assets": assets,
        "chapters": [
            {
                "chapter_id": "CH001",
                "title": "占位章节",
                "summary": "占位模式生成的结构化章节",
                "segments": shots,
            }
        ],
    }
    script = validate_script(canonicalize_script(placeholder_script))
    grouped = split_assets_by_type(script.get("assets") or [])
    video_prompts = collect_video_prompts(script)
    meta = {
        "mode": "⚠️ 占位（未调用真实模型）",
        "api_calls": 0,
        "elapsed_s": 0.0,
        "provider": "placeholder",
        "model": "（未调用）",
        "url": "",
        "output_format_requested": "placeholder",
        "output_format_detected": "placeholder",
        "manifest_injected": False,
        "asset_count": sum(len(v) for k, v in grouped.items() if k != "OTHER"),
        "video_segment_count": len(video_prompts),
        "notes": ["占位结果仅用于验证工作流结构；真实生成请配置 API 密钥并关闭「使用占位结果」"],
    }
    return ScriptPromptResult(
        raw_text=json.dumps(placeholder_script, ensure_ascii=False),
        script=script,
        assets_by_type=grouped,
        video_prompts=video_prompts,
        meta=meta,
    )
