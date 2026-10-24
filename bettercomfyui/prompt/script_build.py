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

## ROUND59：单次输出不可用时的自动恢复（不是静默降级）

真实故障：整集剧本（3 分钟 / 7 个场景）让 DeepSeek 一次输出 8000+ token 的 JSON，
服务端在 max_tokens 上限处截断（``finish_reason="length"``），解析器只能从截断文本里
取到 chapters 数组内部片段 → 报「顶层收到数组 / 无法识别的输出结构」，8/8 集全部失败。

现在的恢复策略（**首次调用仍然只有 1 次**；额外调用只在失败后发生，并如实写进
``meta.api_calls`` / ``meta.degraded`` / ``meta.notes``，节点审计里可见）：

* **结构不符**（模型给了单个素材对象 / 未知形状 / 非法 JSON 但没截断）→
  追加「精简 + 正确 schema」的纠正指令，**重试 1 次**；
* **输出被截断**（``finish_reason="length"`` 或解析器判定 ``truncated_json``）→
  **按场景拆分**（``场景1—2`` / ``第一场`` / ``SCENE 1`` 等标题切块）逐块生成，
  合并 assets（按归一化名去重）与 chapters，保住整集剧情；单块失败会再压缩重试 1 次，
  仍失败的块写进 ``meta.failed_chunks`` 并在审计里明确告警（不静默丢场景）；
* 两种恢复都失败 → 抛 :class:`ScriptBuildError`，消息里带 ``finish_reason``、
  原文长度、结尾 200 字与两次恢复的错误，便于判断是截断还是契约问题。

## 绝不静默

* API 失败 → :class:`ScriptBuildError`，消息含可操作原因（401/超时/缺 key）；
* 模型输出解析不了 / 结构不合法 → :class:`ScriptBuildError`，附解析报告与原文前 500 字；
* **不返回部分结果**：要么完整成功，要么抛错；降级恢复是显式的（meta + 审计可见）。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..models.enums import AssetType
from .errors import PromptError
from .gateway import GatewayError, build_gateway
from .inject import (
    build_prompt_messages,
    parse_manifest_asset_names,
    unique_system_prompt_enabled,
)
from .normalize import normalize_name
from .validate import (
    ScriptValidationError,
    adapt_script_output,
    canonicalize_script,
    parse_with_report,
    validate_script,
)

__all__ = [
    "ASSET_TYPE_ORDER",
    "ASSET_TYPE_LABELS",
    "MAX_RECOVERY_CALLS",
    "ScriptBuildError",
    "ScriptPromptResult",
    "generate_script_prompts",
    "build_placeholder_prompts",
    "split_assets_by_type",
    "split_script_into_chunks",
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


def collect_video_segments(script: Mapping[str, Any],
                           default_duration_s: float = 8.0) -> list[dict[str, Any]]:
    """收集视频段（prompt + 段时长）。

    ROUND58 新需求：段时长由模型按剧情输出（4–15s），不再每段固定 15s。
    这里做容错：缺失/非法时用 default；越界收敛到 [4, 15]；**不把 15 当默认**。
    """
    segments: list[dict[str, Any]] = []
    for chapter in script.get("chapters") or []:
        if not isinstance(chapter, Mapping):
            continue
        for segment in chapter.get("segments") or []:
            if not isinstance(segment, Mapping):
                continue
            text = str(segment.get("prompt") or "").strip()
            if not text:
                continue
            raw = segment.get("duration_s", segment.get("duration"))
            try:
                duration = float(raw)
            except (TypeError, ValueError):
                duration = float(default_duration_s)
            if duration <= 0:
                duration = float(default_duration_s)
            duration = max(4.0, min(15.0, round(duration, 1)))
            cast = [
                str(x).strip()
                for x in (segment.get("character_ids") or [])
                if str(x).strip()
            ]
            reused = [
                str(x).strip()
                for x in (segment.get("reused_asset_ids") or [])
                if str(x).strip()
            ]
            new_ids = [
                str((item or {}).get("id") or "").strip()
                for item in (segment.get("new_assets") or [])
                if isinstance(item, dict) and str((item or {}).get("id") or "").strip()
            ]
            segments.append({
                "segment_index": len(segments) + 1,
                "prompt": text,
                "duration_s": duration,
                "asset_refs": [str(x) for x in (segment.get("asset_refs") or []) if str(x).strip()],
                "character_ids": cast,
                "reused_asset_ids": reused,
                "new_asset_ids": new_ids,
            })
    return segments


def collect_video_prompts(script: Mapping[str, Any]) -> list[str]:
    """从 chapters[].segments[] 收集视频段提示词（E5），按章节/段顺序。"""
    return [s["prompt"] for s in collect_video_segments(script)]


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

    @property
    def video_segments(self) -> list[dict]:
        """每段 prompt + duration_s（AI 决定，4–15s）；未给时用 8s。"""
        return collect_video_segments(self.script)

    @property
    def video_durations(self) -> list[float]:
        return [float(s["duration_s"]) for s in self.video_segments]

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
                {"segment_index": index, "prompt": prompt, "duration_s": duration}
                for index, (prompt, duration) in enumerate(
                    zip(self.video_prompts, self.video_durations), start=1)
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
        if self.meta.get("degraded"):
            lines.append(
                f"降级恢复：{self.meta['degraded']}（{self.meta.get('fallback_reason') or '首次输出不可用'}）"
            )
        if self.meta.get("notes"):
            lines.extend(f"注意：{note}" for note in self.meta["notes"])  # type: ignore[index]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# ROUND59：单次输出不可用时的降级恢复（截断 → 按场景拆分；结构不符 → 纠正重试）
# ---------------------------------------------------------------------------

#: 降级恢复最多额外发起的 API 调用数（含分块重试），防止异常时成本失控。
MAX_RECOVERY_CALLS = 12

#: 单块剧本字符数超过它时，按行再切（避免长场景单块输出再次超 8192 token）。
_SPLIT_CHUNK_SOFT_CHARS = 2400

#: 场景标题（整集剧本按它分块）：场景1—2 / 【场景1】/ 第一场 / ## 场景 / SCENE 1 / INT. …
_SCENE_HEADER_RE = re.compile(
    r"^\s*(?:"
    r"场景\s*[0-9０-９]+(?:[—\-－~～至][0-9０-９]+)?"
    r"|【\s*场景[^】]{0,24}】"
    r"|第\s*[0-9０-９一二三四五六七八九十百]+\s*[场幕]"
    r"|#{1,6}\s*场景[^\n]{0,40}"
    r"|(?:INT|EXT)\.\s"
    r"|SCENE\s*[0-9]+\b"
    r")",
    re.IGNORECASE,
)

#: 跨块合并时，纯 id 形态（A1 / CH01 / SEG2）的引用若映射不到本块素材就丢弃，
#: 避免它意外命中另一块的全局 id（那会挂错参考图）。
_ID_LIKE_REF_RE = re.compile(r"^[A-Za-z]{0,4}\d{1,6}$")

_COMPACT_RETRY_RULES = """【上一次输出不可用，这是纠正重试（第 2 次尝试）】
上一次响应被截断或不是合法 JSON，本次必须遵守：
1. 只输出一个 JSON 对象，不要输出解释文字，不要输出 ``` 围栏。
2. 顶层必须是 assets（数组，可为空）与 chapters（非空数组）；每个 asset 必须有 id/asset_type/name/prompt；
   每个 segment 必须有 prompt/asset_refs/duration_s（4–15）。
3. 本次必须更精简：每个 segments[].prompt ≤ 500 个中文字符，每个 assets[].prompt ≤ 200 字；
   合并重复描述，优先保证结构完整；整份响应 ≤ 6000 tokens。"""

_SPLIT_CHUNK_RULES = """【降级：按场景拆分（第 {index}/{total} 块）】
上一次整份输出被截断/无法解析，本次只拆解上面这一段剧本（其余场景由后续调用分别拆解）：
1. 只输出本块出现的素材；其它场景/角色的素材不要输出。已给出的素材清单里的名称可以逐字引用，不要重复输出。
2. chapters 输出 1 个即可；segments 必须覆盖本块全部镜头；asset_refs 只能引用本块 assets 或清单中的名称。
3. 必须完整且精简：每个 segments[].prompt ≤ 500 个中文字符，assets[].prompt ≤ 200 字；
   只输出一个 JSON 对象，不要输出解释文字或 ``` 围栏。"""


def _is_scene_header(line: str) -> bool:
    return bool(_SCENE_HEADER_RE.match(line))


def _group_blocks(blocks: list[str], *, max_chunks: int) -> list[str]:
    """把相邻场景块合并成不超过 ``max_chunks`` 组（尽量等长；纯函数）。"""
    blocks = [block for block in blocks if block.strip()]
    n = len(blocks)
    if n <= max_chunks:
        return blocks
    total = sum(len(block) for block in blocks)
    groups_n = max(1, min(max_chunks, n))
    target = total / groups_n
    groups: list[str] = []
    current: list[str] = []
    current_len = 0
    remaining = groups_n
    for index, block in enumerate(blocks):
        current.append(block)
        current_len += len(block) + 2
        blocks_left = n - index - 1
        if remaining <= 1:
            continue
        must_close = blocks_left < remaining
        near_target = current_len >= target and blocks_left >= remaining - 1
        if must_close or near_target:
            groups.append("\n\n".join(current))
            current, current_len = [], 0
            remaining -= 1
    if current:
        groups.append("\n\n".join(current))
    return groups


def _split_by_lines(text: str, *, target_chars: int = 700) -> list[str]:
    lines = [line for line in text.split("\n") if line.strip()]
    blocks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        current.append(line)
        current_len += len(line) + 1
        if current_len >= target_chars:
            blocks.append("\n".join(current))
            current, current_len = [], 0
    if current:
        blocks.append("\n".join(current))
    return blocks


def split_script_into_chunks(script_text: str, *, max_chunks: int = 8) -> list[str]:
    """把整集剧本拆成多块（降级恢复用；纯函数，便于单测）。

    优先按场景标题（``场景1—2`` / ``【场景1】`` / ``第一场`` / ``SCENE 1`` / ``INT.`` 等）
    切块；没有场景标题时按空行段落切，再不行按行切。块数上限 ``max_chunks``，
    单块过长（> :data:`_SPLIT_CHUNK_SOFT_CHARS`）时按行再细分。
    """
    if not isinstance(script_text, str) or not script_text.strip():
        return []
    text = script_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if max_chunks < 2:
        return [text]
    blocks: list[str] = []
    current: list[str] = []
    for line in text.split("\n"):
        if _is_scene_header(line) and any(part.strip() for part in current):
            blocks.append("\n".join(current).strip())
            current = []
        current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    blocks = [block for block in blocks if block.strip()]
    # 集名/前情提要等「第一个场景标题之前」的内容并进第一块，避免白占一次调用。
    if len(blocks) >= 2 and not _is_scene_header(blocks[0].splitlines()[0]):
        blocks[0] = f"{blocks[0]}\n{blocks[1]}".strip()
        blocks.pop(1)
    if len(blocks) <= 1:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
        blocks = paragraphs if len(paragraphs) >= 2 else _split_by_lines(text)
    blocks = [block for block in blocks if block.strip()]
    if len(blocks) <= 1:
        return [text]
    grouped = _group_blocks(blocks, max_chunks=max_chunks)
    # 单块仍过长 → 按行再切（整集单场景极长时避免再次截断）。
    expanded: list[str] = []
    for group in grouped:
        if len(group) > _SPLIT_CHUNK_SOFT_CHARS and len(expanded) + 2 <= max_chunks * 2:
            expanded.extend(_split_by_lines(group, target_chars=_SPLIT_CHUNK_SOFT_CHARS // 2))
        else:
            expanded.append(group)
    return [chunk for chunk in expanded if chunk.strip()]


def _model_finish_reason(gateway: Any) -> str | None:
    """读网关最近一次响应的 ``finish_reason``（没有该接口的替身网关返回 None）。"""
    finish = getattr(gateway, "last_finish_reason", None)
    if isinstance(finish, str) and finish.strip():
        return finish.strip().lower()
    return None


def _call_model(gateway: Any, *, system: str, user: str) -> tuple[str, str | None]:
    """调用网关一次，返回 ``(正文, finish_reason)``。"""
    raw = gateway.complete(system=system, user=user)
    return str(raw or ""), _model_finish_reason(gateway)


def _looks_truncated(finish_reason: Any, exc: ScriptValidationError) -> bool:
    if str(finish_reason or "").strip().lower() == "length":
        return True
    try:
        return "truncated_json" in set(exc.codes)
    except Exception:  # noqa: BLE001 - 错误码读取失败按「非截断」处理
        return False


def _parse_adapt(
    raw_text: str, *, fmt: str, finish_reason: Any
) -> tuple[dict, list[str], str, dict]:
    """解析 + 形状适配（不做完整结构校验，供分块合并阶段用）。"""
    parsed, report = parse_with_report(raw_text, output_format=fmt, finish_reason=finish_reason)
    canonical, adapt_warnings, shape = adapt_script_output(parsed)
    return canonical, list(adapt_warnings), shape, report.as_dict()


def _parse_validate(
    raw_text: str,
    *,
    fmt: str,
    finish_reason: Any,
    existing_names: list[str],
) -> dict:
    """解析 + 适配 + 严格校验（一次完整输出）。返回结果包或抛 ScriptValidationError。"""
    canonical, adapt_warnings, shape, report = _parse_adapt(
        raw_text, fmt=fmt, finish_reason=finish_reason
    )
    dropped_refs: list[str] = []
    script = validate_script(
        canonical,
        existing_assets=existing_names or None,
        allow_drop_invalid_refs=True,
        dropped_refs=dropped_refs,
    )
    return {
        "script": script,
        "report": report,
        "notes": adapt_warnings,
        "shape": shape,
        "dropped_refs": dropped_refs,
    }


def _merge_scene_chunks(
    chunks: list[Mapping],
    *,
    dropped_refs: list[str] | None = None,
    notes: list[str] | None = None,
) -> dict:
    """把逐场景拆分的产物合并成一份剧本：assets 按归一化名去重、引用改写成全局 id。"""
    dropped = dropped_refs if dropped_refs is not None else []
    merge_notes = notes if notes is not None else []
    merged: dict[str, Any] = {"title": "", "assets": [], "chapters": []}
    name_to_global: dict[str, str] = {}
    for chunk_index, chunk in enumerate(chunks, 1):
        if not isinstance(chunk, Mapping):
            continue
        local_map: dict[str, str] = {}
        for asset in chunk.get("assets") or []:
            if not isinstance(asset, Mapping):
                continue
            name = str(asset.get("name") or "").strip()
            norm = normalize_name(name) if name else ""
            if norm and norm in name_to_global:
                global_id = name_to_global[norm]
                if asset.get("id"):
                    local_map[str(asset.get("id"))] = global_id
                if name:
                    local_map[name] = global_id
                merge_notes.append(f"第{chunk_index}块素材 {name!r} 与前面重复，已合并（不重复生成）")
                continue
            global_id = f"A{len(merged['assets']) + 1}"
            record = dict(asset)
            record["id"] = global_id
            merged["assets"].append(record)
            if norm:
                name_to_global[norm] = global_id
            local_map[global_id] = global_id
            if asset.get("id"):
                local_map[str(asset.get("id"))] = global_id
            if name:
                local_map[name] = global_id
        for chapter in chunk.get("chapters") or []:
            if not isinstance(chapter, Mapping):
                continue
            new_chapter = {key: value for key, value in chapter.items() if key != "assets"}
            new_chapter["chapter_id"] = f"CH{len(merged['chapters']) + 1:03d}"
            segments: list[dict] = []
            for segment in chapter.get("segments") or []:
                if not isinstance(segment, Mapping):
                    continue
                record = dict(segment)
                record["segment_index"] = len(segments) + 1
                refs: list[str] = []
                for ref in segment.get("asset_refs") or []:
                    text = str(ref)
                    if text in local_map:
                        refs.append(local_map[text])
                        continue
                    stripped = text.split(":", 1)[1].strip() if ":" in text else text
                    if stripped in local_map:
                        refs.append(local_map[stripped])
                        continue
                    if _ID_LIKE_REF_RE.match(stripped):
                        # 本块没有这个 id（可能来自被截断的上一块）：丢弃，绝不让它
                        # 意外命中另一块的全局 A1/A2（那会挂错参考图）。
                        dropped.append(
                            f"第{chunk_index}块 {text!r}: 跨块素材 id 无法映射（已丢弃）"
                        )
                        continue
                    refs.append(text)
                record["asset_refs"] = refs
                segments.append(record)
            new_chapter["segments"] = segments
            if segments:
                merged["chapters"].append(new_chapter)
    return merged


def _recover_by_corrective_retry(
    *,
    gateway: Any,
    messages: Mapping,
    fmt: str,
    existing_names: list[str],
    reason: str,
) -> dict:
    """结构不符但未截断：带「精简 + 正确 schema」指令重试 1 次。"""
    user = f"{messages.get('user') or ''}\n\n{_COMPACT_RETRY_RULES}"
    raw_text, finish_reason = _call_model(
        gateway, system=str(messages.get("system") or ""), user=user
    )
    result = _parse_validate(
        raw_text, fmt=fmt, finish_reason=finish_reason, existing_names=existing_names
    )
    result["degraded"] = "schema_retry"
    result["extra_calls"] = 1
    result["finish_reason"] = finish_reason
    result["fallback_reason"] = reason
    result["notes"] = list(result.get("notes") or []) + [
        f"首次输出不可用（{reason}）→ 已追加正确 schema/精简要求重试 1 次并成功"
    ]
    return result


def _recover_by_scene_split(
    *,
    gateway: Any,
    cfg: Any,
    messages: Mapping,
    script_text: str,
    manifest: str,
    fmt: str,
    existing_names: list[str],
    style_text: str,
    reason: str,
    max_calls: int,
) -> dict:
    """输出被截断：按场景拆分逐块生成，再合并 assets/chapters（保住整集剧情）。"""
    chunks = split_script_into_chunks(script_text)
    if len(chunks) < 2:
        raise ScriptValidationError(
            [
                {
                    "path": "$",
                    "code": "scene_split_unavailable",
                    "message": "剧本里找不到多个场景标记，无法按场景拆分降级；"
                    "请在剧本里用「场景1—1」这类标题分场，或压缩单次输出量后重试",
                }
            ]
        )
    system_prompt = str(messages.get("system") or "")
    parsed_chunks: list[tuple[int, dict]] = []
    failed: list[str] = []
    notes: list[str] = []
    dropped_refs: list[str] = []
    calls = 0
    collected_names: list[str] = []
    for index, chunk in enumerate(chunks, 1):
        if calls >= max_calls:
            failed.append(f"第{index}块：未调用（已达恢复调用上限 {max_calls}）")
            break
        extra_names = [
            name for name in dict.fromkeys(collected_names) if name and name not in existing_names
        ]
        chunk_manifest = manifest or ""
        if extra_names:
            prefix = f"{chunk_manifest}\n" if chunk_manifest else ""
            chunk_manifest = prefix + "已有素材（可逐字引用，不要重复输出）：" + "、".join(extra_names)
        chunk_messages = build_prompt_messages(
            script_text=chunk,
            cfg=cfg,
            manifest=chunk_manifest,
            chapter_hint=f"降级拆分 {index}/{len(chunks)}",
        )
        user = f"{chunk_messages['user']}\n\n{_SPLIT_CHUNK_RULES.format(index=index, total=len(chunks))}"
        if style_text:
            user += f"\n\n【整片视觉风格（所有素材与镜头必须一致）】{style_text}"
        raw_text, finish_reason = _call_model(gateway, system=system_prompt, user=user)
        calls += 1
        try:
            canonical, _warns, _shape, _report = _parse_adapt(
                raw_text, fmt=fmt, finish_reason=finish_reason
            )
        except ScriptValidationError as first_exc:
            # 单块再压缩重试一次（不同块互不影响，失败块记入 failed_chunks）。
            if calls >= max_calls:
                failed.append(f"第{index}块：{_format_errors(first_exc)[:240]}")
                continue
            retry_user = (
                f"{user}\n\n【本块仍不可用，再压缩一次】只输出 JSON，总量 ≤1500 tokens；"
                "segments 只保留关键镜头，prompt 更精简。"
            )
            raw_text, finish_reason = _call_model(
                gateway, system=system_prompt, user=retry_user
            )
            calls += 1
            try:
                canonical, _warns, _shape, _report = _parse_adapt(
                    raw_text, fmt=fmt, finish_reason=finish_reason
                )
            except ScriptValidationError as second_exc:
                failed.append(
                    f"第{index}块（场景拆分）：{_format_errors(second_exc)[:240]}"
                )
                continue
        parsed_chunks.append((index, canonical))
        for asset in canonical.get("assets") or []:
            if isinstance(asset, Mapping):
                name = str(asset.get("name") or "").strip()
                if name:
                    collected_names.append(name)
    if not parsed_chunks:
        raise ScriptValidationError(
            [
                {
                    "path": "$",
                    "code": "scene_split_failed",
                    "message": "按场景拆分后所有分块仍无法解析："
                    + ("；".join(failed)[:800] or "（无可用分块）"),
                }
            ]
        )
    all_names = list(dict.fromkeys([*existing_names, *collected_names]))
    validated: list[dict] = []
    for index, canonical in parsed_chunks:
        try:
            validated.append(
                validate_script(
                    canonical,
                    existing_assets=all_names or None,
                    allow_drop_invalid_refs=True,
                    dropped_refs=dropped_refs,
                )
            )
        except ScriptValidationError as exc:
            failed.append(f"第{index}块（结构校验）：{_format_errors(exc)[:240]}")
    if not validated:
        raise ScriptValidationError(
            [
                {
                    "path": "$",
                    "code": "scene_split_failed",
                    "message": "按场景拆分后所有分块结构校验都失败："
                    + ("；".join(failed)[:800] or "（无可用分块）"),
                }
            ]
        )
    merged = _merge_scene_chunks(validated, dropped_refs=dropped_refs, notes=notes)
    merged_script = validate_script(
        merged,
        existing_assets=existing_names or None,
        allow_drop_invalid_refs=True,
        dropped_refs=dropped_refs,
    )
    notes.insert(
        0,
        f"首次整份输出不可用（{reason}）→ 已降级为按场景拆分：{len(chunks)} 块，"
        f"成功 {len(validated)} 块，共 {calls} 次 API 调用（含分块重试）",
    )
    if failed:
        notes.append("⚠️ 以下分块仍失败（对应场景的分镜会缺失）：" + "；".join(failed))
    return {
        "script": merged_script,
        "report": {
            "requested_format": fmt,
            "method": "json",
            "extracted_from": "scene_split",
            "warnings": [],
        },
        "notes": notes,
        "shape": "script",
        "dropped_refs": dropped_refs,
        "degraded": "scene_split",
        "extra_calls": calls,
        "finish_reason": None,
        "fallback_reason": reason,
        "chunk_count": len(chunks),
        "success_chunks": len(validated),
        "failed_chunks": failed,
    }


def _recover_after_failure(
    *,
    gateway: Any,
    cfg: Any,
    messages: Mapping,
    script_text: str,
    manifest: str,
    fmt: str,
    finish_reason: Any,
    raw_text: str,
    first_exc: ScriptValidationError,
    existing_names: list[str],
    style_text: str,
    max_calls: int,
) -> dict:
    """首次输出不可用 → 纠正重试 / 按场景拆分。失败抛汇总后的 ScriptValidationError。"""
    if fmt == "delimited":
        # 纯分隔符模式：本模块的自动恢复（JSON 纠正指令 / 按场景分块）都基于 JSON 契约，
        # 强行套用会让「要 JSON、却按分隔符解析」互相打架；保持原行为直接报错。
        raise ScriptValidationError(
            [
                {
                    "path": "$",
                    "code": "recovery_unsupported",
                    "message": "output_format=delimited 模式不做自动降级恢复"
                    "（自动恢复只用于 JSON/auto）；请检查分隔符文本格式",
                }
            ]
        )
    truncated = _looks_truncated(finish_reason, first_exc)
    reason = (
        "输出被截断（finish_reason=length / max_tokens 上限）"
        if truncated
        else f"结构不符合契约（{first_exc.codes[0] if first_exc.codes else 'unknown'}）"
    )
    attempts: list[str] = []
    chunks = split_script_into_chunks(script_text)

    def _try_split() -> dict | None:
        if len(chunks) < 2:
            return None
        try:
            return _recover_by_scene_split(
                gateway=gateway,
                cfg=cfg,
                messages=messages,
                script_text=script_text,
                manifest=manifest,
                fmt=fmt,
                existing_names=existing_names,
                style_text=style_text,
                reason=reason,
                max_calls=max_calls,
            )
        except ScriptValidationError as exc:
            attempts.append("按场景拆分失败：" + _format_errors(exc))
            return None

    def _try_retry() -> dict | None:
        try:
            return _recover_by_corrective_retry(
                gateway=gateway, messages=messages, fmt=fmt,
                existing_names=existing_names, reason=reason,
            )
        except ScriptValidationError as exc:
            attempts.append("纠正重试失败：" + _format_errors(exc))
            return None

    # 截断优先拆分（拆分是确定性的，且能保住整集剧情）；结构不符先纠正重试。
    if truncated:
        recovered = _try_split() or _try_retry()
    else:
        recovered = _try_retry() or _try_split()
    if recovered is not None:
        return recovered
    raise ScriptValidationError(
        [
            {
                "path": "$",
                "code": "recovery_failed",
                "message": "首次输出不可用（"
                + reason
                + "），自动恢复（纠正重试 / 按场景拆分）也失败："
                + ("；".join(attempts)[:1200] or "（无可用恢复手段）"),
            }
        ]
    )


def _build_failure_message(
    first_exc: ScriptValidationError,
    raw_text: str,
    finish_reason: Any,
    recovery_error: ScriptValidationError | None = None,
) -> str:
    """构造可操作的失败消息：错误码 + finish_reason + 原文头尾（便于判断截断）。"""
    lines = [
        "模型返回不可用（解析/结构校验失败，已按 JSON 与预设分隔符两条路径尝试）：",
        _format_errors(first_exc),
        "",
        f"finish_reason={finish_reason or '未知'}；原始输出 {len(raw_text)} 字符",
    ]
    if recovery_error is not None:
        lines.extend(["", "自动恢复也失败（纠正重试 / 按场景拆分）：", _format_errors(recovery_error)])
    lines.extend(
        [
            "",
            f"原始输出前 500 字：\n{raw_text[:500]}",
            f"原始输出结尾 200 字（便于判断截断）：\n{raw_text[-200:]}",
        ]
    )
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
    allow_recovery: bool = True,
    max_recovery_calls: int = MAX_RECOVERY_CALLS,
) -> ScriptPromptResult:
    """（尽量）**一次** API 调用 → 解析 → 校验 → 按类型拆分。

    成功路径只有 1 次 ``gateway.complete()``。只有首次输出**被截断/结构不符**时才会
    自动恢复（纠正重试 1 次 / 按场景拆分），并把实际调用次数与降级方式如实写进
    ``meta.api_calls`` / ``meta.degraded`` / ``meta.notes``（节点审计可见）。

    :param cfg: 配置对象（``bettercomfyui.config.Config`` 或任何支持 prompt.* 取值的东西）
    :param script_text: 一整段剧本正文（不能为空）
    :param manifest: 既有素材清单原文（图一 A2/A3 注入；空字符串 = 空库分支）
    :param output_format: ``auto``（默认；JSON 优先，失败按预设分隔符）/ ``json`` / ``delimited``
    :param style_hint: 整片统一风格（会追加到用户提示词；素材图提示词与视频段都由它约束）
    :param gateway: 可注入的网关（测试用替身；**不传则按配置构造真实网关**）
    :param transport: 传给 :func:`build_gateway` 的 HTTP 传输层（测试注入，避免真网络）
    :param allow_recovery: 首次输出不可用时是否自动恢复（默认 True；测试可关掉）
    :param max_recovery_calls: 恢复阶段最多额外发起的调用数（含分块重试）
    :raises ScriptBuildError: API 失败、解析失败或结构校验失败（恢复也失败时）
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
    if style_text and unique_system_prompt_enabled(cfg):
        # V3.0 唯一提示词模式：系统提示词已包含全部风格/光影规则，这里只传用户配置的风格值
        messages["user"] = (
            f"{messages['user']}\n\n【整片视觉风格（用户配置）】{style_text}"
        )
    elif style_text:
        # 风格约束写在用户提示词末尾：一次调用里同时约束 E1–E5，不额外再调一次模型。
        # ROUND63：风格只统一质感/镜头语言，明暗与色温必须按剧本场景自动判断，禁止一刀切压暗。
        messages["user"] = (
            f"{messages['user']}\n\n【整片视觉风格（所有素材与镜头必须一致）】{style_text}"
            "\n【光线与明暗（必须按剧本场景自动判断，不能一刀切）】"
            "以上风格只统一质感与镜头语言，**不统一明暗**："
            "日戏/白天/明亮室内 → 明亮通透、自然光、正常饱和、暗部保留细节；"
            "夜景/夜戏 → 可以按剧情偏暗，但必须保留暗部层次与光源细节（不要死黑、不要满屏冷蓝）；"
            "**严禁**给所有镜头无条件添加「冷蓝、低饱和、暗调、暗角、night、low key、dark lighting、"
            "cool blue、low saturation」等压暗词；只有当剧本该场景明确需要时才写。"
            "\n【光影与场景（按剧本自然决定，不要一刀切）】白天/明亮室内→自然明亮、曝光正常；"
            "夜晚/夜戏→自然偏暗但保留真实光源与暗部细节；昏暗室内/密室→可以低照度但保留可读细节。"
            "不要统一提亮或压暗，不要给所有镜头加冷蓝/低饱和/暗调/暗角，也不要强制「明亮通透/高饱和」。"
            "\n【活人感】真实皮肤纹理与自然肤色、真实光影过渡与接触阴影、呼吸/眨眼/重心变化等自然微动作，"
            "动作有重量不僵硬，避免塑料/蜡像/CGI 平滑感。"
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
        raw_text, finish_reason = _call_model(
            model_gateway, system=messages["system"], user=messages["user"]
        )
    except GatewayError as exc:
        raise ScriptBuildError(
            f"模型调用失败（已发起 API 调用 {api_calls} 次）：\n{exc}"
        ) from exc

    elapsed = time.monotonic() - started
    fmt = _normalize_output_format(output_format)

    #: 模型实际注入的既有素材清单（用于允许 asset_refs 逐字引用清单里的名称）。
    injected_manifest = str(messages.get("manifest") or manifest or "")
    existing_names = parse_manifest_asset_names(injected_manifest)

    dropped_refs: list[str] = []
    adapt_warnings: list[str] = []
    report: dict[str, Any] = {}
    output_shape = ""
    degraded = ""
    fallback_reason = ""
    recovery_meta: dict[str, Any] = {}
    try:
        parsed, parsed_report = parse_with_report(
            raw_text, output_format=fmt, finish_reason=finish_reason
        )
        canonical, adapt_warnings, output_shape = adapt_script_output(parsed)
        script = validate_script(
            canonical,
            existing_assets=existing_names or None,
            allow_drop_invalid_refs=True,
            dropped_refs=dropped_refs,
        )
        report = parsed_report.as_dict()
    except ScriptValidationError as exc:
        recovered: dict | None = None
        recovery_error: ScriptValidationError | None = None
        if allow_recovery:
            try:
                recovered = _recover_after_failure(
                    gateway=model_gateway,
                    cfg=cfg,
                    messages=messages,
                    script_text=script_text,
                    manifest=injected_manifest,
                    fmt=fmt,
                    finish_reason=finish_reason,
                    raw_text=raw_text,
                    first_exc=exc,
                    existing_names=existing_names,
                    style_text=style_text,
                    max_calls=max(1, int(max_recovery_calls)),
                )
            except ScriptValidationError as recovery_exc:
                recovery_error = recovery_exc
        if recovered is None:
            raise ScriptBuildError(
                _build_failure_message(exc, raw_text, finish_reason, recovery_error)
            ) from exc
        api_calls += int(recovered.get("extra_calls") or 0)
        script = recovered["script"]
        report = recovered.get("report") or {}
        adapt_warnings = list(recovered.get("notes") or [])
        output_shape = str(recovered.get("shape") or "script")
        dropped_refs = list(recovered.get("dropped_refs") or [])
        degraded = str(recovered.get("degraded") or "")
        fallback_reason = str(recovered.get("fallback_reason") or "")
        recovery_meta = {
            key: recovered[key]
            for key in ("chunk_count", "success_chunks", "failed_chunks")
            if recovered.get(key) is not None
        }

    grouped = split_assets_by_type(script.get("assets") or [])
    video_segments = collect_video_segments(script)
    video_prompts = [s["prompt"] for s in video_segments]
    video_durations = [float(s["duration_s"]) for s in video_segments]

    describe = getattr(model_gateway, "describe", None)
    gateway_info = describe() if callable(describe) else {}
    #: ParseReport.as_dict(): {requested_format, method, extracted_from, warnings}
    #: method = "json" | "delimited" —— 这就是实际生效的解析路径（不是猜的）。
    output_format_detected = report.get("method") if isinstance(report, Mapping) else None

    notes = [str(w) for w in adapt_warnings]
    meta: dict[str, Any] = {
        "mode": "真实模型（一次调用）" if api_calls <= 1 else f"真实模型（{api_calls} 次调用，含降级恢复）",
        "api_calls": api_calls,
        "elapsed_s": round(elapsed, 3),
        "provider": gateway_info.get("provider", ""),
        "model": gateway_info.get("model", ""),
        "url": gateway_info.get("url", ""),
        "output_format_requested": fmt,
        "output_format_detected": output_format_detected,
        #: 模型输出的形状：script / legacy / handbook（handbook=已由适配层显式转换）。
        "output_shape": output_shape,
        #: ROUND59：输出被截断/结构不符时的显式降级标记（空串=一次调用成功）。
        "degraded": degraded,
        "fallback_reason": fallback_reason,
        "finish_reason": finish_reason,
        "raw_chars": len(raw_text),
        "manifest_injected": bool(messages.get("manifest")),
        "existing_asset_names": len(existing_names),
        "asset_count": len(grouped.get("CHARACTER", []))
        + len(grouped.get("SCENE", []))
        + len(grouped.get("PROP", []))
        + len(grouped.get("SHOT", [])),
        "video_segment_count": len(video_prompts),
        "video_durations": video_durations,
        "duration_source": "ai",
        "notes": notes,
        **recovery_meta,
    }
    report_warnings = report.get("warnings") if isinstance(report, Mapping) else None
    if report_warnings:
        meta["notes"].extend(str(w) for w in report_warnings)
    # ROUND58：如果模型仍然把每段写成同一个时长（尤其全 15s），显式提示用户，
    # 不静默接受；不擅自改模型创作时长，用户可切固定模式或手动改时间轴。
    if len(video_durations) >= 2 and len({round(float(d), 1) for d in video_durations}) == 1:
        meta["duration_uniform"] = True
        meta["notes"].append(
            f"⚠️ 模型输出的所有视频段时长都相同（{video_durations[0]:.1f}s）："
            "可能没有按剧情区分；请检查系统提示词，或在「时长模式=固定」/⑦ 时间轴人工调整。")
    else:
        meta["duration_uniform"] = False
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
    video_segments = collect_video_segments(script, default_duration_s=float(segment_seconds))
    video_prompts = [s["prompt"] for s in video_segments]
    video_durations = [float(s["duration_s"]) for s in video_segments]
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
        "video_durations": video_durations,
        "duration_source": "placeholder",
        "notes": ["占位结果仅用于验证工作流结构；真实生成请配置 API 密钥并关闭「使用占位结果」"],
    }
    return ScriptPromptResult(
        raw_text=json.dumps(placeholder_script, ensure_ascii=False),
        script=script,
        assets_by_type=grouped,
        video_prompts=video_prompts,
        meta=meta,
    )
