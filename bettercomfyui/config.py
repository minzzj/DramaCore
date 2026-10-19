"""配置加载与分层覆盖。

优先级（高 -> 低）：项目 config.yaml > 用户 config/user.yaml > 内置默认值。
方案「全局配置参数」表明确要求：用户配置覆盖默认配置。

设计要点：
- **不在配置里存储真实 API 密钥。** 代码接受 api_key 字段，但示例配置一律留空；
  真实密钥建议通过环境变量注入（见 `resolve_secret`）。
- 未知配置键 -> 报错。静默忽略拼错的键会导致「改了配置但不生效」的假象。
- 数值范围校验：例如 cnt2_limit 必须 >= 1，否则重做逻辑会立即进入兜底，
  这不是配置错误而是逻辑陷阱，必须在加载期挡住。
"""

from __future__ import annotations

import copy
import os
from dataclasses import fields as dc_fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from .models.entities import (
    AssetGenParams,
    AssetReviewParams,
    Config,
    DirectorParams,
    EncodeParams,
    GlobalParams,
    PromptParams,
    VideoReviewParams,
)
from .models.enums import AuditScope, FullReviewPolicy, ReviewMode

#: 配置段名 -> dataclass 类型。`global` 段映射到 GlobalParams（Python 关键字规避）。
_SECTION_TYPES: dict[str, type] = {
    "prompt": PromptParams,
    "assets": AssetGenParams,
    "asset_review": AssetReviewParams,
    "director": DirectorParams,
    "video_review": VideoReviewParams,
    "encode": EncodeParams,
    "global": GlobalParams,
}

#: 需要从字符串转成枚举的字段： (段名, 字段名) -> 枚举类
_ENUM_FIELDS: dict[tuple[str, str], type] = {
    ("asset_review", "default_mode"): ReviewMode,
    ("video_review", "audit_scope"): AuditScope,
    ("video_review", "full_review_policy"): FullReviewPolicy,
}

#: 数值范围约束。超出范围在加载期报错，避免运行到一半才发现配置无意义。
_NUMERIC_BOUNDS: dict[tuple[str, str], tuple[float, float]] = {
    ("assets", "resolution"): (64, 8192),
    ("assets", "steps"): (1, 1000),
    ("assets", "cfg"): (0.0, 100.0),
    ("assets", "batch_count"): (1, 64),
    ("assets", "per_class_limit"): (1, 64),
    ("asset_review", "human_timeout_minutes"): (1, 1440),
    ("asset_review", "model_candidate_limit"): (1, 100),
    ("asset_review", "score_threshold"): (0.0, 100.0),
    ("director", "segment_seconds"): (1, 600),
    ("director", "fps"): (1, 240),
    ("director", "tail_frames"): (0, 1000),
    ("video_review", "threshold_segment"): (0.0, 100.0),
    ("video_review", "threshold_full"): (0.0, 100.0),
    ("video_review", "cnt2_limit"): (1, 100),
    ("video_review", "cnt3_limit"): (1, 100),
    ("video_review", "max_cascade_depth"): (1, 1000),
    ("encode", "crf"): (0, 51),
    ("global", "tech_retry_limit"): (1, 100),
}


class ConfigError(Exception):
    """配置错误。必须在启动期抛出，绝不延迟到运行期。"""


def _deep_merge(base: dict, override: dict, *, path: str = "") -> dict:
    """递归合并。override 中的 None 视为「显式清空」，会真实覆盖 base。

    注意：不能把 None 当作「未设置」跳过 —— `full_review_policy: null`
    的语义就是「未显式配置」，必须能覆盖掉默认值。
    """
    out = dict(base)
    for key, val in override.items():
        here = f"{path}.{key}" if path else key
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val, path=here)
        else:
            out[key] = val
    return out


def _load_yaml(path: Path, *, required: bool) -> dict:
    if not path.exists():
        if required:
            raise ConfigError(f"配置文件不存在：{path}")
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 YAML 解析失败：{path}：{exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件根节点必须是映射（dict）：{path}，实际是 {type(raw).__name__}")
    return raw


def _build_section(section: str, data: dict):
    """把一段 dict 变成对应 dataclass 实例，做未知键与范围校验。"""
    cls = _SECTION_TYPES[section]
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"配置段 [{section}] 必须是映射，实际是 {type(data).__name__}")

    known = {f.name for f in dc_fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(
            f"配置段 [{section}] 存在未知键：{unknown}；"
            f"合法键：{sorted(known)}。未知键一律报错，避免「改了配置但不生效」。"
        )

    kwargs: dict[str, Any] = {}
    for key, val in data.items():
        enum_cls = _ENUM_FIELDS.get((section, key))
        if enum_cls is not None:
            if val is None:
                kwargs[key] = None
            elif isinstance(val, enum_cls):
                kwargs[key] = val
            else:
                try:
                    kwargs[key] = enum_cls(str(val))
                except ValueError as exc:
                    allowed = ", ".join(m.value for m in enum_cls)
                    raise ConfigError(
                        f"配置 [{section}].{key} 取值 {val!r} 非法；允许值：{allowed}"
                    ) from exc
        else:
            kwargs[key] = val

    bounds = _NUMERIC_BOUNDS.get((section, key)) if False else None  # 占位，见下方循环
    for (sec, key), (lo, hi) in _NUMERIC_BOUNDS.items():
        if sec != section or key not in kwargs:
            continue
        val = kwargs[key]
        if val is None:
            continue
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            raise ConfigError(f"配置 [{section}].{key} 必须是数值，实际 {type(val).__name__}")
        if not (lo <= val <= hi):
            raise ConfigError(
                f"配置 [{section}].{key}={val} 超出允许范围 [{lo}, {hi}]"
            )

    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"构造配置段 [{section}] 失败：{exc}") from exc


def config_from_dict(data: dict | None) -> Config:
    """从已合并的 dict 构造 Config，做未知段与范围校验。"""
    data = data or {}
    unknown = sorted(set(data) - set(_SECTION_TYPES))
    if unknown:
        raise ConfigError(
            f"配置存在未知段：{unknown}；合法段：{sorted(_SECTION_TYPES)}"
        )
    kwargs = {}
    for section in _SECTION_TYPES:
        kwargs["global_" if section == "global" else section] = _build_section(
            section, data.get(section, {})
        )
    cfg = Config(**kwargs)
    _cross_validate(cfg)
    # ROUND53：prompt.normalize_names 接线（此前该配置字段未被任何代码读取）
    try:
        from .prompt.normalize import set_normalize_enabled
        set_normalize_enabled(bool(getattr(cfg.prompt, "normalize_names", True)))
    except Exception:  # noqa: BLE001 - 接线失败不影响主流程
        pass
    return cfg


def _cross_validate(cfg: Config) -> None:
    """跨字段一致性校验。这些错误组合会让流程行为不可预期，必须启动期挡住。"""
    if cfg.director.first_frame not in ("first", "manual"):
        raise ConfigError("director.first_frame 只能是 first 或 manual")
    if cfg.director.last_frame not in ("last", "manual"):
        raise ConfigError("director.last_frame 只能是 last 或 manual")
    if cfg.asset_review.on_timeout not in ("switch_to_model", "suspend", "skip"):
        raise ConfigError("asset_review.on_timeout 只能是 switch_to_model/suspend/skip")
    if cfg.asset_review.fallback != "highest_of_candidates":
        raise ConfigError("asset_review.fallback 目前只支持 highest_of_candidates")
    if cfg.video_review.regenerate_granularity not in ("segment", "full"):
        raise ConfigError("video_review.regenerate_granularity 只能是 segment 或 full")
    if cfg.encode.format not in ("MP4", "MOV", "WebM"):
        raise ConfigError("encode.format 只能是 MP4/MOV/WebM")
    if cfg.encode.codec not in ("H.264", "H.265"):
        raise ConfigError("encode.codec 只能是 H.264 或 H.265")
    if cfg.encode.transition not in ("none", "fade"):
        raise ConfigError("encode.transition 只能是 none 或 fade")
    if cfg.encode.cover not in ("first_frame", "custom"):
        raise ConfigError("encode.cover 只能是 first_frame 或 custom")
    # 图一 ENC1「音频自动拼接可手动调」：
    #   auto   保留各段音轨拼接（默认）
    #   mute   成片静音
    #   manual 用调用方指定的整条音轨替换（必须给 audio_source，否则 encode() 报错）
    if cfg.encode.audio_join not in ("auto", "mute", "manual"):
        raise ConfigError("encode.audio_join 只能是 auto / mute / manual")
    # ---- 背景音乐（图一 O7）----
    _bgm_vol = cfg.encode.bgm_volume
    if not isinstance(_bgm_vol, (int, float)) or isinstance(_bgm_vol, bool):
        raise ConfigError("encode.bgm_volume 必须是数字")
    if not (0.0 <= float(_bgm_vol) <= 1.0):
        raise ConfigError("encode.bgm_volume 必须在 0.0-1.0 之间")
    _bgm_fade = cfg.encode.bgm_fade
    if not isinstance(_bgm_fade, (int, float)) or isinstance(_bgm_fade, bool):
        raise ConfigError("encode.bgm_fade 必须是数字")
    if float(_bgm_fade) < 0:
        raise ConfigError("encode.bgm_fade 不能为负数")
    _budget = cfg.director.vram_budget_gb
    if not isinstance(_budget, (int, float)) or isinstance(_budget, bool):
        raise ConfigError("director.vram_budget_gb 必须是数字")
    if float(_budget) < 0:
        raise ConfigError("director.vram_budget_gb 不能为负数（0 = 不设上限）")
    if cfg.video_review.rework_prompt_augment not in (
        "disabled", "dimensions", "assets", "both"):
        raise ConfigError(
            "video_review.rework_prompt_augment 只能是 disabled/dimensions/assets/both")
    if cfg.global_.log_level not in ("DEBUG", "INFO", "WARN", "ERROR"):
        raise ConfigError("global.log_level 只能是 DEBUG/INFO/WARN/ERROR")
    if cfg.prompt.output_format not in ("json", "delimited"):
        raise ConfigError("prompt.output_format 只能是 json 或 delimited")
    if cfg.prompt.story_granularity not in ("shot", "scene"):
        raise ConfigError("prompt.story_granularity 只能是 shot 或 scene")
    # 手动审核模式却把超时策略设为切模型是允许的（人工超时切模型是核心特性），
    # 但「暂停超时」关闭 + on_timeout=skip 会导致人工永不超时也永不切换，属于陷阱。
    if not cfg.asset_review.timeout_pause_on_active_op and cfg.asset_review.on_timeout == "skip":
        raise ConfigError(
            "配置陷阱：timeout_pause_on_active_op=false 且 on_timeout=skip "
            "会导致人工审核永久挂起。请改为 switch_to_model。"
        )


def load_config(
    *,
    repo_root: Path,
    project_dir: Path | None = None,
    user_override: Path | None = None,
) -> Config:
    """按优先级加载并合并配置。

    参数：
        repo_root: 仓库根目录，用于定位 config/default.yaml 与 config/user.yaml
        project_dir: 项目目录，若含 config.yaml 则作为最高优先级覆盖
        user_override: 显式指定用户配置路径，默认 repo_root/config/user.yaml
    """
    repo_root = Path(repo_root)
    merged: dict = {}

    default_path = repo_root / "config" / "default.yaml"
    merged = _deep_merge(merged, _load_yaml(default_path, required=False))

    user_path = Path(user_override) if user_override else repo_root / "config" / "user.yaml"
    merged = _deep_merge(merged, _load_yaml(user_path, required=False))

    if project_dir is not None:
        proj_cfg = Path(project_dir) / "config.yaml"
        merged = _deep_merge(merged, _load_yaml(proj_cfg, required=False))

    return config_from_dict(merged)


def resolve_secret(cfg: Config, *, env_var: str = "BETTERCOMFYUI_API_KEY") -> str | None:
    """解析 API 密钥：环境变量优先，其次配置字段。

    **绝不把真实密钥写入仓库。** 示例配置一律留空。
    """
    env_val = os.environ.get(env_var)
    if env_val:
        return env_val.strip()
    if cfg.prompt.api_key:
        return cfg.prompt.api_key.strip()
    return None


def dump_config(cfg: Config, path: Path) -> None:
    """导出配置（配置导入导出功能）。**导出前清除密钥字段。**"""
    data = cfg.to_dict()
    if isinstance(data.get("prompt"), dict):
        data["prompt"]["api_key"] = None
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


__all__ = [
    "ConfigError",
    "load_config",
    "config_from_dict",
    "resolve_secret",
    "dump_config",
]
