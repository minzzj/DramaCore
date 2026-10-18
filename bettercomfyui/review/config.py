"""review 层配置解析。

**不另造配置类型。** CONTRACTS v1.1 澄清：`Config` / `AssetReviewParams` 唯一定义在
`bettercomfyui/models/entities.py`，加载层在 `bettercomfyui/config.py`。
本模块只做「取值 + 校验」，把 `AssetReviewParams` 的正常性检查集中在一处：

- `human_timeout_minutes` ∈ [1, 60]（方案参数表三）
- `model_candidate_limit` ∈ [1, 10]
- `score_threshold` ∈ [0, 100]
- `on_timeout` 只支持 `switch_to_model`：`suspend` / `skip` 在 `REVIEW_TRANSITIONS` 里
  **没有任何对应边**，无法在不伪造状态转移的前提下实现 —— 因此配置这两种值时**立即报错**，
  绝不静默按「切模型」处理。
- `fallback` 只支持 `highest_of_candidates`（方案参数表三「固定」）
- `new_version_requires_review` 必须为真（方案第七部分·一·5「新版本必重审」）
- `default_mode` 只能是 HUMAN / MODEL；AUTO_SWITCHED 只能由「人工超时」产生，不可配置
"""

from __future__ import annotations

import logging
from dataclasses import fields as dc_fields, replace
from typing import Any

from ..models.entities import AssetReviewParams, Config
from ..models.enums import ReviewMode
from .errors import ReviewConfigError

_LOG = logging.getLogger(__name__)

SUPPORTED_ON_TIMEOUT = ("switch_to_model",)
SUPPORTED_FALLBACK = ("highest_of_candidates",)
MIN_TIMEOUT_MINUTES = 1
MAX_TIMEOUT_MINUTES = 60
MIN_CANDIDATE_LIMIT = 1
MAX_CANDIDATE_LIMIT = 10


def _section_from_mapping(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ReviewConfigError(f"review 配置必须是 dict，实际：{type(data).__name__}")
    if "asset_review" in data and isinstance(data["asset_review"], dict):
        return dict(data["asset_review"])
    known = {f.name for f in dc_fields(AssetReviewParams)}
    if known & set(data):
        return dict(data)
    raise ReviewConfigError(
        "无法从 dict 解析 asset_review 配置：既没有 'asset_review' 子字典，"
        f"也不含任何已知字段（{', '.join(sorted(known))}）"
    )


def resolve_review_params(config: Any = None) -> AssetReviewParams:
    """把 Config / AssetReviewParams / dict / None 归一化为**校验过的** AssetReviewParams。"""
    params: AssetReviewParams
    if config is None:
        params = AssetReviewParams()
    elif isinstance(config, AssetReviewParams):
        params = replace(config)
    elif isinstance(config, Config):
        params = replace(config.asset_review)
    elif isinstance(config, dict):
        section = _section_from_mapping(config)
        known = {f.name for f in dc_fields(AssetReviewParams)}
        unknown = sorted(set(section) - known)
        if unknown:
            raise ReviewConfigError(
                f"asset_review 配置包含未知字段：{', '.join(unknown)}；"
                f"已知字段：{', '.join(sorted(known))}（拼写错误不得被静默忽略）"
            )
        section["default_mode"] = _parse_mode(section.get("default_mode", ReviewMode.MODEL), "default_mode")
        try:
            params = AssetReviewParams(**section)
        except TypeError as exc:
            raise ReviewConfigError(f"asset_review 配置无法构造：{exc}") from exc
    else:
        asset_review = getattr(config, "asset_review", None)
        if asset_review is None:
            raise ReviewConfigError(
                f"config 类型不受支持：{type(config).__name__}；"
                "需要 Config / AssetReviewParams / dict / None"
            )
        params = replace(asset_review)
    return validate_review_params(params)


def _parse_mode(value: Any, field: str) -> ReviewMode:
    try:
        mode = ReviewMode.parse(value)
    except ValueError as exc:
        raise ReviewConfigError(f"{field} 取值非法：{exc}") from exc
    if mode is None:
        raise ReviewConfigError(f"{field} 不能为 None")
    return mode


def _check_range(value: Any, name: str, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ReviewConfigError(f"{name} 必须是数字，实际：{value!r}") from exc
    if not (low <= number <= high):
        raise ReviewConfigError(f"{name}={value!r} 超出允许范围 [{low}, {high}]（方案参数表）")
    return number


def validate_review_params(params: AssetReviewParams) -> AssetReviewParams:
    if not isinstance(params, AssetReviewParams):
        raise ReviewConfigError(f"需要 AssetReviewParams，实际：{type(params).__name__}")

    _check_range(params.human_timeout_minutes, "human_timeout_minutes",
                 MIN_TIMEOUT_MINUTES, MAX_TIMEOUT_MINUTES)
    _check_range(params.model_candidate_limit, "model_candidate_limit",
                 MIN_CANDIDATE_LIMIT, MAX_CANDIDATE_LIMIT)
    _check_range(params.score_threshold, "score_threshold", 0, 100)

    on_timeout = str(params.on_timeout or "").strip()
    if on_timeout not in SUPPORTED_ON_TIMEOUT:
        raise ReviewConfigError(
            f"on_timeout={params.on_timeout!r} 不受支持。REVIEW_TRANSITIONS 未定义 "
            "suspend/skip 的任何边，无法在不伪造状态转移的前提下实现；"
            f"当前只支持 {', '.join(SUPPORTED_ON_TIMEOUT)}（方案参数表三默认值）。"
        )

    fallback = str(params.fallback or "").strip()
    if fallback not in SUPPORTED_FALLBACK:
        raise ReviewConfigError(
            f"fallback={params.fallback!r} 不受支持；方案参数表三「兜底规则 = 三轮最高分（固定）」，"
            f"当前只支持 {', '.join(SUPPORTED_FALLBACK)}。"
        )

    if not params.new_version_requires_review:
        raise ReviewConfigError(
            "new_version_requires_review=false 被拒绝：方案第七部分·一·5 与参数表三规定"
            "「新版本必重审」为固定规则，旧 LOCK 不得授权新版本。"
        )

    mode = _parse_mode(params.default_mode, "default_mode")
    if mode is ReviewMode.AUTO_SWITCHED:
        raise ReviewConfigError(
            "default_mode 不能是 AUTO_SWITCHED：该模式只能由「人工审核超时」自动产生"
            "（方案第七部分·一·3），不可配置。"
        )

    dims = params.score_dimensions
    if not isinstance(dims, (list, tuple)) or not dims or not all(str(d).strip() for d in dims):
        raise ReviewConfigError(f"score_dimensions 必须是非空字符串列表，实际：{dims!r}")

    channels = params.notify_channels
    if channels is None:
        channels = []
    if not isinstance(channels, (list, tuple)):
        raise ReviewConfigError(f"notify_channels 必须是列表，实际：{channels!r}")

    params.default_mode = mode
    return params


def timeout_seconds(params: AssetReviewParams) -> float:
    return float(params.human_timeout_minutes) * 60.0


__all__ = [
    "resolve_review_params",
    "validate_review_params",
    "timeout_seconds",
    "SUPPORTED_ON_TIMEOUT",
    "SUPPORTED_FALLBACK",
]
