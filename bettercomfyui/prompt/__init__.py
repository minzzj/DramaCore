"""``bettercomfyui.prompt`` —— 剧本拆解、提示词注入与素材的确定性匹配。

对外契约（CONTRACTS §5）：

* :mod:`bettercomfyui.prompt.gateway` —— 模型网关（external_api / local_qwen / custom）
* :mod:`bettercomfyui.prompt.normalize` —— 名称归一化与疑似重复判定
* :mod:`bettercomfyui.prompt.validate` —— 模型输出解析与严格结构校验
* :mod:`bettercomfyui.prompt.match` —— 三分支确定性匹配（R1：存在性只由程序判断）
* :mod:`bettercomfyui.prompt.inject` —— 系统/用户提示词组装（含既有素材注入）

依赖方向：``prompt`` 依赖 ``store``；**不得**依赖 ``review`` / ``director`` / ``pipeline``。
"""

from __future__ import annotations

from .errors import PromptDependencyError, PromptError, SuspectPersistenceError
from .gateway import (
    CustomGateway,
    ExternalApiGateway,
    GatewayError,
    HttpTransport,
    LocalQwenGateway,
    PromptModelGateway,
    TransportError,
    UrllibTransport,
    build_gateway,
)
from .inject import (
    SYSTEM_PROMPT_PRESETS,
    build_asset_manifest,
    build_prompt_messages,
    build_system_prompt,
    build_user_prompt,
)
from .match import MatchResult, build_suspect_queue, match_candidate, select_reusable_version
from .normalize import is_suspect, normalize_name, similarity, variant_key
from .validate import (
    ParseReport,
    ScriptValidationError,
    parse_script_output,
    parse_with_report,
    validate_script,
)

__all__ = [
    # errors
    "PromptError",
    "PromptDependencyError",
    "SuspectPersistenceError",
    # normalize
    "normalize_name",
    "is_suspect",
    "similarity",
    "variant_key",
    # validate
    "ScriptValidationError",
    "ParseReport",
    "parse_script_output",
    "parse_with_report",
    "validate_script",
    # match
    "MatchResult",
    "match_candidate",
    "build_suspect_queue",
    "select_reusable_version",
    # gateway
    "PromptModelGateway",
    "GatewayError",
    "HttpTransport",
    "TransportError",
    "UrllibTransport",
    "ExternalApiGateway",
    "LocalQwenGateway",
    "CustomGateway",
    "build_gateway",
    # inject
    "build_asset_manifest",
    "build_system_prompt",
    "build_user_prompt",
    "build_prompt_messages",
    "SYSTEM_PROMPT_PRESETS",
]
