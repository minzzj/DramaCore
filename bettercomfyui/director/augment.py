"""返工提示词加料（图一 SG5 `X`「提示词自动加相关提示词和 @素材」）。

## 对应流程图

    T["定位不合格段"] --> U["只重生成该段"]
    U --> REGEN["重新生成该段视频"]
    REGEN --> X["提示词自动加相关提示词和 @素材"]
    X --> V["以该段素材首尾帧作参考"]

## 修的是什么缺口

`X` 此前**零实现**（`augment` / `rework_prompt` 全工程命中 0 处）。
后果：返工时把**同一段提示词**再喂一遍，模型没有任何新信息 ——
不合格的维度大概率还是不合格，CNT2 三次很快烧完，最后只能取最高分兜底。

**加料**才是"重做"和"重复"的区别：告诉模型这次该改什么、必须用哪些素材。

## 两种加料维度（可配置，默认都加）

1. **按失败维度**（`dimensions`）—— 从审核结果里找出没达标的维度，
   翻译成一句正向约束。例如「角色一致性 62 分」-> 「保持角色面部与服装与参考图一致」。
2. **按素材标记**（`assets`）—— 把这一段引用的素材写成 `@素材` 标记
   （图一 `XF` 的格式：`@素材类型_序号_名称`），让重生成明确知道该锚定谁。

配置：`video_review.rework_prompt_augment`，取值
`disabled` / `dimensions` / `assets` / `both`（默认 `both`）。

## 为什么要保留原文并留痕

加料后的提示词与原始提示词**必须都能查到** ——
否则事后无法判断"这次重做到底比上次多说了什么"，
也就无法复盘"加料是否有效"。所以：
* 原始提示词存进 `segment.generation_params["prompt_original_before_augment"]`；
* 加料明细返回给调用方，由调用方发事件。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

__all__ = [
    "AUGMENT_MODES",
    "DEFAULT_AUGMENT_MODE",
    "DIMENSION_HINTS",
    "asset_markers",
    "build_rework_prompt",
    "failed_dimensions",
    "format_asset_marker",
]

#: 允许的加料模式。
AUGMENT_MODES = ("disabled", "dimensions", "assets", "both")

DEFAULT_AUGMENT_MODE = "both"

#: 失败维度 -> 一句**正向**约束。
#:
#: 为什么用正向表述而不是"不要模糊"：扩散/视频模型对否定词常常无效
#: （"不要模糊"里的"模糊"仍会进入注意力），正向描述更可靠。
DIMENSION_HINTS: dict[str, str] = {
    "角色一致性": "保持角色面部特征、发型与服装与参考图完全一致",
    "画面连贯": "保持动作与镜头衔接自然连续，避免跳变",
    "动作合理": "动作符合物理规律与人体结构，避免关节扭曲",
    "无闪烁": "画面稳定，避免帧间闪烁与纹理抖动",
    "画质": "提升清晰度与细节锐度，避免模糊与噪点",
    "合规": "内容与画面合规，不出现敏感元素",
    "构图": "构图稳定，主体位置与景别保持一致",
    "叙事连贯": "叙事推进连贯，与前一段的情节衔接自然",
    "音频同步": "音画同步，口型与动作对齐",
    "节奏合理": "节奏平稳，避免突兀的快慢切换",
}

#: 素材类型 -> 中文标记前缀（图一 `XF`「@素材类型_序号_名称」）。
_ASSET_TYPE_CN = {
    "CHARACTER": "角色",
    "SCENE": "场景",
    "PROP": "物品",
    "SHOT": "分镜",
    "AUDIO": "音频",
    "VIDEO": "视频",
}


def failed_dimensions(
    review: dict, *, threshold: float | None = None
) -> list[tuple[str, float | None]]:
    """从审核结果里找出**没达标的维度**，返回 `[(维度名, 分数)]`。

    审核结果的结构在不同路径下不完全一样，所以这里**按多个候选键名取**，
    取不到就返回空（绝不能因为结构不认识就崩掉整个返工流程）。

    识别顺序：`r1a_scores` -> `scores` -> `dimensions` -> `r1b_scores`。
    """
    if not isinstance(review, dict):
        return []
    scores: dict[str, Any] | None = None
    for key in ("r1a_scores", "scores", "dimensions", "r1b_scores"):
        value = review.get(key)
        if isinstance(value, dict) and value:
            scores = value
            break
    if not scores:
        return []

    limit = threshold
    if limit is None:
        try:
            limit = float(review.get("threshold"))
        except (TypeError, ValueError):
            limit = None

    out: list[tuple[str, float | None]] = []
    for name, raw in scores.items():
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if limit is None or value < limit:
            out.append((str(name), value))
    # 分数低的排前面：最该先改
    out.sort(key=lambda item: (item[1] if item[1] is not None else 0.0))
    return out


def format_asset_marker(
    *, ref: str | None = None, asset_id: str | None = None,
    name: str | None = None, asset_type: str | None = None,
) -> str:
    """生成 `@素材` 标记。

    优先用图一 `XF` 的完整格式 `@类型_序号_名称`（信息最全）；
    拿不到类型/序号时退回 `@名称` 或 `@asset_id` —— **绝不返回空标记**。
    """
    if asset_type and asset_id:
        type_cn = _ASSET_TYPE_CN.get(str(asset_type).upper(), str(asset_type))
        # asset_id 形如 `项目_character_1_abc123`：取 `character_1` 里的序号
        parts = str(asset_id).split("_")
        seq = None
        lowered = [p.lower() for p in parts]
        for idx, part in enumerate(lowered):
            if part in ("character", "scene", "prop", "shot", "audio", "video") and idx + 1 < len(parts):
                if parts[idx + 1].isdigit():
                    seq = parts[idx + 1]
                    break
        if seq and name:
            return f"@{type_cn}_{seq}_{name}"
        if name:
            return f"@{type_cn}_{name}"
    if name:
        return f"@{name}"
    if ref:
        return f"@{ref}"
    if asset_id:
        return f"@{asset_id}"
    return ""


def asset_markers(
    asset_refs: Iterable[Any] | None,
    *,
    resolver: Callable[[str], dict | None] | None = None,
) -> list[str]:
    """把段引用的素材列表转成 `@素材` 标记列表（去重、保序）。

    `resolver`：可选，`asset_id -> {"name","asset_type"}`。
    给了就能产出完整的 `@类型_序号_名称`；不给则退回 `@ref` / `@asset_id`。
    """
    markers: list[str] = []
    seen: set[str] = set()
    for ref in asset_refs or []:
        asset_id = known = None
        if isinstance(ref, dict):
            asset_id = ref.get("asset_id")
            known = ref.get("name") or ref.get("ref")
        else:
            known = str(ref)
        info = None
        if resolver is not None and asset_id:
            try:
                info = resolver(str(asset_id))
            except Exception:  # noqa: BLE001 - 解析失败退化为简单标记
                info = None
        marker = format_asset_marker(
            ref=str(known) if known else None,
            asset_id=str(asset_id) if asset_id else None,
            name=(info or {}).get("name") if info else (known if asset_id else None),
            asset_type=(info or {}).get("asset_type") if info else None,
        )
        if marker and marker not in seen:
            seen.add(marker)
            markers.append(marker)
    return markers


def build_rework_prompt(
    segment: Any,
    review: dict | None,
    *,
    mode: str = DEFAULT_AUGMENT_MODE,
    threshold: float | None = None,
    resolver: Callable[[str], dict | None] | None = None,
) -> dict:
    """构造返工用的加料提示词。返回明细，供调用方落库/发事件。

    返回::

        {
          "mode": "both",
          "original": "...",          # 原始提示词
          "augmented": "...",         # 加料后的提示词（mode=disabled 时等于 original）
          "added_dimensions": [("角色一致性", 62.0)],
          "added_hints": ["保持角色面部..."],
          "added_markers": ["@角色_1_林秀"],
          "changed": bool,
        }

    加料**追加在原文之后**，不改写原文 —— 这样即使加料不理想，原始意图仍在。
    """
    mode_text = str(mode or DEFAULT_AUGMENT_MODE).strip().lower()
    if mode_text not in AUGMENT_MODES:
        raise ValueError(
            f"rework_prompt_augment 只能是 {'/'.join(AUGMENT_MODES)}，收到 {mode!r}"
        )

    original = str(getattr(segment, "prompt", "") or "")
    result = {
        "mode": mode_text,
        "original": original,
        "augmented": original,
        "added_dimensions": [],
        "added_hints": [],
        "added_markers": [],
        "changed": False,
    }
    if mode_text == "disabled":
        return result

    suffix_parts: list[str] = []

    if mode_text in ("dimensions", "both"):
        dims = failed_dimensions(review or {}, threshold=threshold)
        hints: list[str] = []
        for name, _score in dims:
            hint = DIMENSION_HINTS.get(name)
            if hint and hint not in hints:
                hints.append(hint)
        result["added_dimensions"] = dims
        result["added_hints"] = hints
        if hints:
            suffix_parts.append("【本次重做必须修正】" + "；".join(hints) + "。")

    if mode_text in ("assets", "both"):
        markers = asset_markers(getattr(segment, "asset_refs", None), resolver=resolver)
        result["added_markers"] = markers
        if markers:
            suffix_parts.append(
                "【必须锚定的素材】" + " ".join(markers)
                + "（保持与这些素材的外观/场景一致）。"
            )

    if suffix_parts:
        joined = " ".join(suffix_parts)
        result["augmented"] = f"{original} {joined}".strip() if original else joined
        result["changed"] = True
    return result
