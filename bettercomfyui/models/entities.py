"""Better ComfyUI 核心数据模型（唯一真源 / single source of truth）。

所有跨模块传递与持久化的数据结构都定义在这里。字段语义严格来自
《BetterComfyUI完整方案.docx》第七部分与《BetterComfyUI_AI_Agent开发任务书.md》第 4 节。

设计约束：
- 枚举字段持久化为字符串值（枚举真源见 models/enums.py）。
- 时间戳一律 ISO8601 带时区字符串，由 `now_iso()` 生成（统一后端时钟）。
- 金额/评分等数值不做隐式四舍五入。
- ID 里不含可变状态；状态一律是独立字段。
- 反序列化遇到未知枚举值必须抛错，不静默回退（防止把未识别状态当正常状态）。

数据完整性守卫（__post_init__）刻意校验「语义上不可能」的组合，例如：
已 LOCK 但没有 lock_source、审核结论没有 review_cycle_id 等。
让错误在构造时暴露，而不是在流水线跑到一半时才发现。
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field, asdict, fields as dc_fields
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any

from .enums import (
    AssetSource,
    AssetStatus,
    AssetType,
    AuditScope,
    DegradeFlag,
    ErrorClass,
    FullReviewPolicy,
    LibraryScope,
    LockSource,
    LockStatus,
    ReviewMode,
    ReviewState,
    SegmentSource,
    SegmentStatus,
)

#: 项目统一时区（用户环境为 Asia/Shanghai，+08:00，无夏令时）
LOCAL_TZ = timezone(timedelta(hours=8))

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def now_iso() -> str:
    """统一后端时间戳。所有审核/仲裁事件必须使用本函数，不信客户端时钟。"""
    return datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


def new_token() -> str:
    """32 位小写 hex 随机令牌（用于 finalization_token 等）。"""
    return uuid.uuid4().hex


def _coerce(enum_cls, value, field_name: str):
    if value is None:
        return None
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(str(value))
    except ValueError as exc:
        allowed = ", ".join(m.value for m in enum_cls)
        raise ValueError(
            f"{field_name} 取值 {value!r} 不是合法的 {enum_cls.__name__}；允许值：{allowed}"
        ) from exc


def _enum_to_value(obj: Any) -> Any:
    """把 dataclass / 枚举 / 列表递归转换成可 JSON 与 YAML 序列化的原生结构。

    ⚠️ 注意（实测踩过的坑）：本项目的 `_StrEnum` 同时继承 `str` 与 `Enum`，
    因此**不能**用 `isinstance(val, str)` 判定「已是原生值」——
    必须**先判 `isinstance(obj, Enum)`**，否则枚举会漏转，
    YAML 导出时报 `RepresenterError: cannot represent an object`。

    这个函数被覆盖过一次（丢失了 Enum 判断），导致 `dump_config` 直接报错。
    守卫：`tests/test_phase1_gaps.py` 与 `tests/test_foundation.py` 里的 Config 序列化测试。
    """
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (list, tuple)):
        return [_enum_to_value(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _enum_to_value(v) for k, v in obj.items()}
    if hasattr(obj, "__dataclass_fields__"):
        return {f.name: _enum_to_value(getattr(obj, f.name)) for f in dc_fields(obj)}
    return obj


def _from_dict(cls, data: dict):
    """宽容度有限的反序列化：只接受已知字段，未知字段忽略但记录在 _extra。

    之所以忽略未知字段而不是报错：向前兼容（旧文件可能缺新字段）。
    但**枚举取值非法必须报错**，因为这代表数据损坏或版本不匹配。
    """
    if data is None:
        return None
    known = {f.name for f in dc_fields(cls)}
    kwargs = {k: v for k, v in data.items() if k in known}
    obj = cls(**kwargs)
    extra = {k: v for k, v in data.items() if k not in known}
    if extra:
        object.__setattr__(obj, "_extra", extra)
    return obj


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass
class PromptParams:
    """剧本拆解与提示词参数（方案参数表一）。"""

    model_provider: str = "external_api"          # external_api / local_qwen / custom
    output_language: str = "zh"                   # zh / en
    output_format: str = "json"                   # json / delimited
    story_granularity: str = "shot"               # shot / scene
    system_prompt_template: str = ""              # 空 -> 使用内置默认模板
    inject_existing_assets: bool = True           # 多章节必须开启
    normalize_names: bool = True                  # 固定建议开启
    endpoint: str | None = None                   # 外部模型接口（开发者自配）
    api_key: str | None = None                    # 绝不写入真实密钥到仓库
    model_name: str | None = None
    timeout_s: float = 120.0
    # ---- gateway 需要的其余键（prompt/_conf.py 按 prompt.<key> 读取）----
    api_key_env: str | None = None                # 只写环境变量名，避免明文密钥
    max_retries: int = 2                          # 传输层错误重试；4xx 不重试
    retry_backoff_s: float = 0.5
    max_tokens: int | None = 8192
    extra_headers: dict = field(default_factory=dict)
    custom_request_template: dict = field(default_factory=lambda: {
        "model": "{model}",
        "messages": [
            {"role": "system", "content": "{system}"},
            {"role": "user", "content": "{user}"},
        ],
        "stream": False,
    })
    #: custom provider **必填**：从响应取文本的路径，如 "choices.0.message.content"
    custom_response_path: str | None = "choices.0.message.content"
    custom_response_template: str | None = None


@dataclass
class AssetGenParams:
    """素材生成参数（方案参数表二）。"""

    resolution: int = 1024
    steps: int = 25
    sampler: str = "DPM++ 2M Karras"
    cfg: float = 7.0
    seed: int = -1            # -1 表示随机
    batch_count: int = 4
    per_class_limit: int = 9  # 每类上限 9 张


@dataclass
class AssetReviewParams:
    """素材审核参数（方案参数表三）。"""

    default_mode: ReviewMode = ReviewMode.MODEL
    human_timeout_minutes: int = 10
    timeout_pause_on_active_op: bool = True
    model_candidate_limit: int = 3
    score_threshold: float = 70.0
    score_dimensions: list[str] = field(
        default_factory=lambda: ["画质", "一致性", "合规", "构图"]
    )
    on_timeout: str = "switch_to_model"   # switch_to_model / suspend / skip
    new_version_requires_review: bool = True
    fallback: str = "highest_of_candidates"
    notify_channels: list[str] = field(default_factory=lambda: ["popup", "sound"])
    retry_interval: str = "immediate"


@dataclass
class DirectorParams:
    """导演台参数（方案参数表四）。"""

    segment_seconds: int = 10
    resolution: str = "1080P"
    fps: int = 24
    tail_frames: int = 22
    first_frame: str = "first"   # first / manual
    last_frame: str = "last"     # last / manual
    vram_tier: str = "auto"      # auto / low / mid / high
    pruned_model: bool = True
    reference_mode: str = "auto"
    save_vram_all: bool = True
    #: 显存预算（GB）。阶段化调度用它做门禁：单阶段需求超过预算直接拒绝加载。
    #: 0 = 不设上限。5090 32G 建议填 30（留 2G 给系统与驱动）。
    vram_budget_gb: float = 0.0


@dataclass
class VideoReviewParams:
    """视频审核参数（方案参数表六）。"""

    audit_scope: AuditScope = AuditScope.BOTH
    threshold_segment: float = 70.0
    threshold_full: float = 70.0
    r1a_dimensions: list[str] = field(
        default_factory=lambda: ["画面连贯", "角色一致", "动作合理", "无闪烁"]
    )
    r1b_dimensions: list[str] = field(
        default_factory=lambda: ["叙事流畅", "音画同步", "节奏合理", "无明显跳变"]
    )
    cnt2_limit: int = 3
    cnt3_limit: int = 3
    regenerate_granularity: str = "segment"
    #: 返工时提示词加料模式（图一 SG5 X）：disabled / dimensions / assets / both
    rework_prompt_augment: str = "both"   # segment / full
    cascade_mark_enabled: bool = True
    max_cascade_depth: int = 8                 # 硬性终止条件，绝不允许无限级联
    #: 整片审核失败策略。None 表示未显式配置 —— 使用时必须报错，不得隐式决定。
    full_review_policy: FullReviewPolicy | None = None


@dataclass
class EncodeParams:
    """成片封装参数（方案参数表五）。"""

    audio_join: str = "auto"
    transition: str = "none"
    subtitle: bool = False
    watermark: bool = False
    format: str = "MP4"
    codec: str = "H.264"
    crf: int = 18
    cover: str = "first_frame"
    # ---- 背景音乐（图一 SG4 `O7`「背景音乐，后续需提进日程」）----
    #: BGM 音量（0.0-1.0）。与前景（段原声 / 指定音轨）`amix` 时的相对音量。
    bgm_volume: float = 0.35
    #: BGM 比成片短时是否**循环补齐**（`-stream_loop -1` + `-shortest`）。
    bgm_loop: bool = True
    #: BGM 淡入 / 淡出秒数（0 = 不淡）。
    bgm_fade: float = 1.0
    #: 原声 ducking：有原声时自动压低 BGM（`sidechaincompress`）。
    #: 默认关 —— 会把 BGM 压得很狠，按需开。
    bgm_ducking: bool = False


@dataclass
class GlobalParams:
    """全局配置参数（方案参数表七）与并发/恢复底线（任务书第 6 节）。"""

    log_level: str = "INFO"
    log_all_manual_and_auto_switch: bool = True
    tech_retry_limit: int = 3
    #: 单个段任务（生成/审核）的默认超时秒数。
    #: 任务书第 4 节把 `timeout` 列为**视频段**字段；这里的全局值是默认，
    #: 单段可用 `Segment.overrides.timeout_s` 显式覆盖。
    task_timeout_s: float = 900.0
    checkpoint_every_stage: bool = True
    config_import_export: bool = True
    project_root: str = "projects"
    max_notify_retries: int = 2

    #: ComfyUI 实例根目录（含 comfyui_version.py 与 models/ 的那一层）。
    #: 必须可配置：开发机与目标机位置不同，硬编码绝对路径违反 CONTRACTS 第 1 节。
    comfyui_base_path: str = ""
    #: ComfyUI HTTP 服务地址（执行器提交作业用）。留空表示未配置。
    comfyui_endpoint: str = ""


@dataclass
class Config:
    """完整配置。优先级：项目 config.yaml > 用户 user.yaml > 默认 default.yaml。"""

    prompt: PromptParams = field(default_factory=PromptParams)
    assets: AssetGenParams = field(default_factory=AssetGenParams)
    asset_review: AssetReviewParams = field(default_factory=AssetReviewParams)
    director: DirectorParams = field(default_factory=DirectorParams)
    video_review: VideoReviewParams = field(default_factory=VideoReviewParams)
    encode: EncodeParams = field(default_factory=EncodeParams)
    global_: GlobalParams = field(default_factory=GlobalParams)

    def to_dict(self) -> dict:
        d = _enum_to_value(self)
        d["global"] = d.pop("global_", {})
        return d


# ---------------------------------------------------------------------------
# 素材层
# ---------------------------------------------------------------------------


@dataclass
class ReviewAttempt:
    """一次审核尝试（模型候选 / 人工一次操作 / 一次重试）。"""

    attempt_id: str
    review_cycle_id: str
    mode: ReviewMode
    score: float | None = None
    passed: bool | None = None
    reason: str = ""
    backend_ts: str = field(default_factory=now_iso)   # 统一后端时间戳
    operator: str | None = None                        # 人工审核时的操作者
    candidate_version: str | None = None               # 本次尝试对应的候选版本
    error_class: ErrorClass | None = None              # 技术失败时填写
    superseded: bool = False                           # 迟到结果 -> 归档，不生效

    def __post_init__(self) -> None:
        self.mode = _coerce(ReviewMode, self.mode, "ReviewAttempt.mode")
        if self.error_class is not None:
            self.error_class = _coerce(ErrorClass, self.error_class, "ReviewAttempt.error_class")

    def to_dict(self) -> dict:
        return _enum_to_value(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewAttempt":
        return _from_dict(cls, data)


@dataclass
class ReviewRecord:
    """一个对象版本对应的正式审核周期记录。

    语义要点：
    - 同一版本只允许一个最终有效结论（由 finalization_token 条件更新保证）。
    - lock_source 只在结论为 ALLOWED / FALLBACK 时存在。
    - 人工与模型结果都保留，未胜出者标记 superseded，仅归档。
    """

    review_cycle_id: str
    mod_id: str
    review_state: ReviewState
    mode: ReviewMode
    target_kind: str                       # "asset" | "segment" | "movie"
    target_id: str
    version: str
    content_hash: str | None = None
    score: float | None = None
    reason: str = ""
    lock_source: LockSource | None = None
    attempts: list[ReviewAttempt] = field(default_factory=list)
    human_attempt: ReviewAttempt | None = None
    model_attempt: ReviewAttempt | None = None
    arb_id: str | None = None
    abort_id: str | None = None
    arb_winner: str | None = None          # "HUMAN" | "MODEL"
    finalization_token: str | None = None
    finalized_at: str | None = None
    cnt_id: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        self.review_state = _coerce(ReviewState, self.review_state, "ReviewRecord.review_state")
        self.mode = _coerce(ReviewMode, self.mode, "ReviewRecord.mode")
        if self.lock_source is not None:
            self.lock_source = _coerce(LockSource, self.lock_source, "ReviewRecord.lock_source")

        # 语义守卫：有 LOCK 来源必须是已通过或兜底状态
        if self.lock_source is not None and self.review_state not in (
            ReviewState.ALLOWED,
            ReviewState.FALLBACK,
        ):
            raise ValueError(
                f"ReviewRecord 不一致：lock_source={self.lock_source.value} "
                f"但 review_state={self.review_state.value}；"
                "LOCK 只允许在 ALLOWED 或 FALLBACK 状态下存在"
            )
        # 语义守卫：ALLOWED 必须有来源（否则就是「不知道谁批的」）
        if self.review_state is ReviewState.ALLOWED and self.lock_source is None:
            raise ValueError("ReviewRecord 不一致：review_state=ALLOWED 必须携带 lock_source")
        # 语义守卫：内容指纹格式
        if self.content_hash is not None and not _SHA256_RE.match(self.content_hash):
            raise ValueError(
                f"ReviewRecord.content_hash 格式非法：{self.content_hash!r}；"
                "要求 sha256:<64位小写hex>"
            )

    def to_dict(self) -> dict:
        return _enum_to_value(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewRecord":
        data = dict(data or {})
        data["attempts"] = [ReviewAttempt.from_dict(a) for a in data.get("attempts", [])]
        if data.get("human_attempt"):
            data["human_attempt"] = ReviewAttempt.from_dict(data["human_attempt"])
        if data.get("model_attempt"):
            data["model_attempt"] = ReviewAttempt.from_dict(data["model_attempt"])
        return _from_dict(cls, data)


@dataclass
class AssetVersion:
    """素材条目的一个具体版本。**审核与 LOCK 的最小单位**。

    核心原则：新版本必须重新审核；旧版本 LOCK 绝不转移到新版本。
    """

    version: str                       # "v1", "v2", ...
    content_hash: str                  # sha256:<hex>
    file_path: str                     # 相对项目目录的路径
    status: AssetStatus = AssetStatus.DRAFT
    lock_status: LockStatus = LockStatus.UNLOCKED
    lock_source: LockSource | None = None
    review_cycle_id: str | None = None
    review_history: list[str] = field(default_factory=list)  # review_cycle_id 列表
    source: AssetSource = AssetSource.AI
    created_at: str = field(default_factory=now_iso)
    superseded_by: str | None = None   # 被哪个新版本取代（旧版本仍保留）
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.status = _coerce(AssetStatus, self.status, "AssetVersion.status")
        self.lock_status = _coerce(LockStatus, self.lock_status, "AssetVersion.lock_status")
        self.source = _coerce(AssetSource, self.source, "AssetVersion.source")
        if self.lock_source is not None:
            self.lock_source = _coerce(LockSource, self.lock_source, "AssetVersion.lock_source")

        if not _SHA256_RE.match(self.content_hash or ""):
            raise ValueError(
                f"AssetVersion.content_hash 格式非法：{self.content_hash!r}；"
                "要求 sha256:<64位小写hex>"
            )
        # LOCK 三要素必须同时成立（R3）
        if self.lock_status is LockStatus.LOCKED:
            if self.lock_source is None:
                raise ValueError("AssetVersion 不一致：lock_status=LOCKED 必须携带 lock_source")
            if not self.review_cycle_id:
                raise ValueError(
                    "AssetVersion 不一致：lock_status=LOCKED 必须携带 review_cycle_id"
                )
            if self.status not in (AssetStatus.ALLOWED, AssetStatus.FALLBACK):
                raise ValueError(
                    f"AssetVersion 不一致：LOCKED 版本的 status={self.status.value}，"
                    "只允许 ALLOWED 或 FALLBACK"
                )
        if self.lock_status is LockStatus.UNLOCKED and self.lock_source is not None:
            raise ValueError("AssetVersion 不一致：未 LOCK 的版本不得携带 lock_source")

    @property
    def lock_id(self) -> str | None:
        """LOCK_{素材全局ID}_{版本号} 由 store.ids.lock_id() 生成，此处不含 asset_id。"""
        return None

    def to_dict(self) -> dict:
        return _enum_to_value(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AssetVersion":
        return _from_dict(cls, data)


@dataclass
class AssetEntry:
    """素材条目（逻辑身份）。**名称不是唯一身份**，asset_id 才是。"""

    asset_id: str
    asset_type: AssetType
    name: str
    normalized_name: str
    library_scope: LibraryScope = LibraryScope.PROJECT
    source: AssetSource = AssetSource.AI
    current_version: str | None = None
    versions: list[AssetVersion] = field(default_factory=list)
    references: list[dict] = field(default_factory=list)   # 被哪些镜头/视频段引用
    created_at: str = field(default_factory=now_iso)
    note: str = ""

    def __post_init__(self) -> None:
        self.asset_type = _coerce(AssetType, self.asset_type, "AssetEntry.asset_type")
        self.library_scope = _coerce(
            LibraryScope, self.library_scope, "AssetEntry.library_scope"
        )
        self.source = _coerce(AssetSource, self.source, "AssetEntry.source")

    def get_version(self, version: str) -> AssetVersion | None:
        for v in self.versions:
            if v.version == version:
                return v
        return None

    def latest_version(self) -> AssetVersion | None:
        if not self.versions:
            return None
        return max(self.versions, key=lambda v: int(v.version.lstrip("v")))

    def usable_version(self) -> AssetVersion | None:
        """返回可用于后续生成的版本：必须已 LOCK（通过审核或按策略接受）。

        手动添加未审核的素材（MANUAL_UNREVIEWED）**不可**用于生成，
        这是「素材审核前置」原则的落地。
        """
        for v in self.versions:
            if v.lock_status is LockStatus.LOCKED:
                return v
        return None

    def to_dict(self) -> dict:
        return _enum_to_value(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AssetEntry":
        data = dict(data or {})
        data["versions"] = [AssetVersion.from_dict(v) for v in data.get("versions", [])]
        return _from_dict(cls, data)


@dataclass
class SuspectRecord:
    """疑似重复记录。**禁止自动覆盖，禁止静默丢弃**。"""

    suspect_id: str
    normalized_name: str
    candidate_name: str
    candidate_type: AssetType
    candidates: list[str] = field(default_factory=list)   # 与之歧义的既有 asset_id
    reason: str = ""
    resolved: bool = False
    resolution: str | None = None    # "reuse" | "create_new" | "discarded"
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        self.candidate_type = _coerce(AssetType, self.candidate_type, "SuspectRecord.candidate_type")

    def to_dict(self) -> dict:
        return _enum_to_value(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SuspectRecord":
        return _from_dict(cls, data)


# ---------------------------------------------------------------------------
# 剧本 / 分镜
# ---------------------------------------------------------------------------


@dataclass
class Shot:
    """分镜/镜头。E1-E5 的落点。"""

    shot_id: str
    index: int
    prompt_image: str = ""        # 分镜图提示词
    prompt_video: str = ""        # 最终视频提示词（E5，**不进素材库，直接进导演台**）
    duration_s: float | None = None
    refs: list[dict] = field(default_factory=list)      # [{"asset_id","version","type"}]
    dialogue: str = ""

    def to_dict(self) -> dict:
        return _enum_to_value(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Shot":
        return _from_dict(cls, data)


@dataclass
class Scene:
    """场景。"""

    scene_id: str
    index: int
    name: str = ""
    prompt: str = ""
    shots: list[Shot] = field(default_factory=list)

    def to_dict(self) -> dict:
        return _enum_to_value(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Scene":
        return _from_dict(cls, data)


@dataclass
class Chapter:
    """章节。程序按章节边界落盘检查点。"""

    chapter_id: str
    index: int
    title: str = ""
    raw_script: str = ""
    scenes: list[Scene] = field(default_factory=list)
    new_asset_candidates: list[dict] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return _enum_to_value(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Chapter":
        data = dict(data or {})
        data["scenes"] = [
            Scene.from_dict({**s, "shots": [Shot.from_dict(x) for x in s.get("shots", [])]})
            for s in data.get("scenes", [])
        ]
        return _from_dict(cls, data)


# ---------------------------------------------------------------------------
# 视频段
# ---------------------------------------------------------------------------


@dataclass
class SegmentAttempt:
    """一次候选版本生成尝试，用于追踪候选、评分与重生成因果关系。"""

    attempt_id: str
    segment_id: str
    version: str
    index: int
    job_id: str | None = None
    executor: str | None = None
    video_path: str | None = None
    first_frame_path: str | None = None
    last_frame_path: str | None = None
    content_hash: str | None = None
    score: float | None = None
    passed: bool | None = None
    error_class: ErrorClass | None = None
    error: str | None = None
    #: 本次候选实际使用的种子 —— 复现某一版的关键。必须逐候选记录。
    used_seed: int | None = None
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if self.error_class is not None:
            self.error_class = _coerce(ErrorClass, self.error_class, "SegmentAttempt.error_class")

    def to_dict(self) -> dict:
        return _enum_to_value(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SegmentAttempt":
        return _from_dict(cls, data)


@dataclass
class SegmentOverrides:
    """单段对全局参数的**显式覆盖**。

    任务书第 3 节 E 段原文：

    > 全局参数统一注入；**单段覆盖必须显式记录。**

    以及第 4 节把 `audit_scope` / `threshold` / `retry_limit` / `timeout`
    列在**视频段**的字段里，而不是只放全局。

    ## 为什么必须"显式"

    隐含覆盖是排查灾难：某个段审核松了、重试次数多了，
    事后只能靠人回忆"当时是不是我改过这一段"。所以：

    * 全部字段默认 `None` = **不覆盖**，语义明确；
    * `resolve()` 返回生效值的同时，返回**哪些字段被覆盖了**；
    * `describe()` 产出人类可读的覆盖说明，写进段计划与事件日志。

    校验放在 `__post_init__`：非法值当场报错，而不是等到执行时才炸。
    """

    #: 审核范围：segment / full / both（None = 用全局）
    audit_scope: AuditScope | None = None
    #: 该段的通过阈值（None = 用全局的 threshold_segment）
    threshold: float | None = None
    #: 该段允许的质量重做上限（CNT2），None = 用全局
    retry_limit: int | None = None
    #: 该段生成/审核的超时秒数，None = 用全局
    timeout_s: float | None = None
    #: 该段的**生成参数**覆盖（采样步数/CFG/分辨率/时长…）。
    #: 空 dict = 不覆盖。键对应生成参数名，值即覆盖值。
    generation_params: dict = field(default_factory=dict)
    #: 覆盖原因。**覆盖了就必须说明为什么** —— 否则事后无法判断是否合理。
    reason: str = ""
    #: 谁做的覆盖（human / model / system）
    by: str = "human"

    _OVERRIDABLE = ("audit_scope", "threshold", "retry_limit", "timeout_s")

    def __post_init__(self) -> None:
        if self.audit_scope is not None:
            self.audit_scope = _coerce(
                AuditScope, self.audit_scope, "SegmentOverrides.audit_scope"
            )
        if self.threshold is not None:
            t = float(self.threshold)
            if not 0.0 <= t <= 100.0:
                raise ValueError(f"SegmentOverrides.threshold 必须在 0..100，收到 {t}")
            self.threshold = t
        if self.retry_limit is not None:
            n = int(self.retry_limit)
            if n < 0:
                raise ValueError(f"SegmentOverrides.retry_limit 不能为负，收到 {n}")
            self.retry_limit = n
        if self.timeout_s is not None:
            s = float(self.timeout_s)
            if s <= 0:
                raise ValueError(f"SegmentOverrides.timeout_s 必须为正，收到 {s}")
            self.timeout_s = s
        if not isinstance(self.generation_params, dict):
            raise TypeError(
                f"SegmentOverrides.generation_params 必须是 dict，"
                f"收到 {type(self.generation_params).__name__}"
            )
        # 审核参数覆盖必须写明原因：它们直接决定「这一段算不算通过」，
        # 是不可追溯风险最高的一类覆盖。
        # 生成参数覆盖只要求被标记（`params_source=segment_override`），不强制原因
        # —— 否则脚本里每个微调都要写小作文，反而会让人绕过机制。
        review_fields = self.review_overridden_fields()
        if review_fields and not str(self.reason).strip():
            raise ValueError(
                f"覆盖了审核参数 {review_fields} 却没有填 reason。"
                "任务书要求「单段覆盖必须显式记录」—— 审核参数直接决定该段是否通过，"
                "必须写明为什么这一段要不一样。"
            )

    def review_overridden_fields(self) -> list[str]:
        """被覆盖的**审核类**参数（不含生成参数）。"""
        return [f for f in self._OVERRIDABLE if getattr(self, f) is not None]

    def overridden_fields(self) -> list[str]:
        """哪些字段被真正覆盖了（用于审计与事件日志）。"""
        fields = self.review_overridden_fields()
        if self.generation_params:
            fields.append("generation_params")
        return fields

    def is_empty(self) -> bool:
        return not self.overridden_fields()

    def resolve(self, cfg: Any) -> tuple[dict, list[str]]:
        """与全局配置合并，返回 `(生效值, 被覆盖的字段名)`。

        生效值键名统一：`audit_scope` / `threshold` / `retry_limit` / `timeout_s`。
        生成参数的合并走 `resolve_generation()`（因为它依赖 `director` 段）。
        """
        vr = cfg.video_review
        gl = getattr(cfg, "global_", None)
        return (
            {
                "audit_scope": self.audit_scope or vr.audit_scope,
                "threshold": (self.threshold if self.threshold is not None
                              else vr.threshold_segment),
                "retry_limit": (self.retry_limit if self.retry_limit is not None
                                else vr.cnt2_limit),
                "timeout_s": (self.timeout_s if self.timeout_s is not None
                              else getattr(gl, "task_timeout_s", None)),
            },
            self.overridden_fields(),
        )

    def resolve_generation(self, director_cfg: Any) -> dict:
        """把全局导演参数与本段覆盖合并，返回生效的生成参数。

        `director_cfg` 是 `cfg.director`（dataclass 或映射均可）。
        覆盖值**逐个键**生效，未覆盖的键沿用全局 —— 不做整体替换，
        否则"只改步数"会把分辨率/fps 一起丢掉。
        """
        def g(key: str, default: Any) -> Any:
            if isinstance(director_cfg, dict):
                return director_cfg.get(key, default)
            return getattr(director_cfg, key, default)

        base = {
            "segment_seconds": g("segment_seconds", 10),
            "resolution": g("resolution", "1080P"),
            "fps": g("fps", 24),
            "tail_frames": g("tail_frames", 22),
            "vram_tier": g("vram_tier", "auto"),
            "pruned_model": g("pruned_model", True),
            "reference_mode": g("reference_mode", "auto"),
        }
        base.update(self.generation_params or {})
        return base

    def describe(self) -> str:
        """人类可读的覆盖说明（写进段计划/事件日志）。

        **同时给出字段名与中文说明**：字段名用于和配置/日志对照
        （只说"生成参数"就grep 不到），中文用于人直接读。
        """
        fields = self.overridden_fields()
        if not fields:
            return "无覆盖（全部沿用全局参数）"
        zh = {
            "audit_scope": "审核范围",
            "threshold": "通过阈值",
            "retry_limit": "质量重做上限",
            "timeout_s": "超时秒数",
            "generation_params": "生成参数",
        }
        parts = []
        for f in fields:
            v = getattr(self, f)
            if f == "generation_params":
                shown = json.dumps(v, ensure_ascii=False, sort_keys=True)
            else:
                shown = str(getattr(v, "value", v))
            label = f"{f}（{zh.get(f, f)}）"
            parts.append(f"{label}={shown}")
        s = "覆盖 " + "、".join(parts)
        if self.reason:
            s += f"（原因：{self.reason}；by={self.by}）"
        return s

    def to_dict(self) -> dict:
        return _enum_to_value(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SegmentOverrides":
        return _from_dict(cls, data)


@dataclass
class Segment:
    """一个逻辑视频段。**重生成不改变逻辑段 ID**，只递增段版本号。"""

    segment_id: str
    segment_index: int
    chapter_id: str
    prompt: str = ""
    asset_refs: list[dict] = field(default_factory=list)  # 必须含具体版本号
    task_mode: str = ""
    segment_duration: float | None = None
    resolution: str | None = None
    fps: int | None = None
    first_frame: str | None = None
    last_frame: str | None = None
    generation_params: dict = field(default_factory=dict)
    #: 生成种子。**可复现性必须落盘**：方案要求「种子 随机/固定（可复现）」。
    #: None = 由执行层随机；一旦实际生成用了某个种子，回灌时必须写回这里，
    #: 否则「这一版很好看」就永远复现不出来。
    seed: int | None = None
    #: 实际生效的种子（执行层回灌）。与 seed 分开存：
    #: seed 是"要求"，resolved_seed 是"实际用了哪个"（随机时二者不同）。
    resolved_seed: int | None = None
    current_version: str = "v1"
    candidate_version: str | None = None
    review_status: ReviewState | None = None
    review_result: dict | None = None
    status: SegmentStatus = SegmentStatus.PLANNED
    source: SegmentSource = SegmentSource.NORMAL
    degrade_flags: list[DegradeFlag] = field(default_factory=list)
    #: 依赖引用：本段依赖哪些段的首尾帧/动作/画面
    dependency_refs: list[str] = field(default_factory=list)
    #: 段 ID -> 是否因本段变化而待验证（Y2 标记），状态独立存字段，不写进 ID
    cascade_marked: bool = False
    cascade_id: str | None = None
    audit_scope: AuditScope | None = None
    #: 单段对全局参数的显式覆盖（任务书第 3 节 E：「单段覆盖必须显式记录」）。
    #: 空 SegmentOverrides() 表示全部沿用全局。
    overrides: SegmentOverrides = field(default_factory=SegmentOverrides)
    #: 自由备注。用于记录**来源说明**，例如「由导演台回灌自动补建（未经剧本计划阶段）」
    #: —— 让"这段不是剧本计划出来的"这件事留在数据里，而不是只存在于当次日志。
    note: str = ""
    attempts: list[SegmentAttempt] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        self.status = _coerce(SegmentStatus, self.status, "Segment.status")
        self.source = _coerce(SegmentSource, self.source, "Segment.source")
        if self.review_status is not None:
            self.review_status = _coerce(ReviewState, self.review_status, "Segment.review_status")
        if self.audit_scope is not None:
            self.audit_scope = _coerce(AuditScope, self.audit_scope, "Segment.audit_scope")
        if isinstance(self.overrides, dict):
            self.overrides = SegmentOverrides.from_dict(self.overrides)
        elif not isinstance(self.overrides, SegmentOverrides):
            raise TypeError(
                f"Segment.overrides 必须是 SegmentOverrides 或 dict，"
                f"收到 {type(self.overrides).__name__}"
            )
        self.degrade_flags = [
            _coerce(DegradeFlag, d, "Segment.degrade_flags") for d in self.degrade_flags
        ]
        # 语义守卫：asset_refs 必须带版本号（R3 前置）
        for ref in self.asset_refs:
            if not isinstance(ref, dict) or "asset_id" not in ref or "version" not in ref:
                raise ValueError(
                    f"Segment.asset_refs 元素必须同时含 asset_id 与 version，实际：{ref!r}"
                )

    def current_attempt(self) -> SegmentAttempt | None:
        for a in reversed(self.attempts):
            if a.version == self.current_version:
                return a
        return None

    def to_dict(self) -> dict:
        return _enum_to_value(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Segment":
        data = dict(data or {})
        data["attempts"] = [SegmentAttempt.from_dict(a) for a in data.get("attempts", [])]
        # `overrides` 必须是 SegmentOverrides 而不是裸 dict ——
        # `_from_dict` 只做 dataclass 字段赋值，不会递归构造嵌套 dataclass。
        # 漏了这一步会让 `seg.overrides.resolve(cfg)` 在反序列化后崩掉，
        # 而**内存里新建的段又是好的** —— 于是"重启后恢复"这条路径才会暴露，
        # 是最难查的一类不一致。
        ov = data.get("overrides")
        if isinstance(ov, dict):
            data["overrides"] = SegmentOverrides.from_dict(ov)
        return _from_dict(cls, data)


# ---------------------------------------------------------------------------
# 成片
# ---------------------------------------------------------------------------


@dataclass
class SegmentManifestItem:
    """成片元数据里的段清单项（方案第七部分 · 九）。"""

    segment_id: str
    segment_version: str
    asset_refs: list[dict] = field(default_factory=list)
    prompt_id: str | None = None
    video_path: str | None = None
    content_hash: str | None = None
    audit_id: str | None = None
    final_score: float | None = None
    review_source: str | None = None
    cnt2: int = 0
    cnt3: int = 0
    cascade_status: str | None = None
    degrade_flags: list[DegradeFlag] = field(default_factory=list)
    source: SegmentSource = SegmentSource.NORMAL

    def __post_init__(self) -> None:
        self.source = _coerce(SegmentSource, self.source, "SegmentManifestItem.source")
        self.degrade_flags = [
            _coerce(DegradeFlag, d, "SegmentManifestItem.degrade_flags")
            for d in self.degrade_flags
        ]

    def to_dict(self) -> dict:
        return _enum_to_value(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SegmentManifestItem":
        return _from_dict(cls, data)


@dataclass
class MovieMetadata:
    """成片元数据（方案第七部分 · 九「推荐成片元数据字段」）。"""

    project_id: str
    project_display_name: str = ""
    script_id: str | None = None
    chapters: list[str] = field(default_factory=list)
    final_output_path: str | None = None
    movie_content_hash: str | None = None
    output_time: str | None = None
    workflow_version: str = "0.1.0"
    segments: list[SegmentManifestItem] = field(default_factory=list)
    #: 整片审核
    r_full_audit_id: str | None = None
    r1b_scores: dict = field(default_factory=dict)
    r1b_threshold: float | None = None
    full_review_state: str | None = None
    full_review_failure_reason: str = ""
    full_review_policy: FullReviewPolicy | None = None
    delivered_with_degradation: bool = False
    #: 兜底来源明细
    fallback_details: list[dict] = field(default_factory=list)
    degrade_flags: list[DegradeFlag] = field(default_factory=list)
    #: 生成来源声明（任务书第 9 节末句：必须明确区分真实集成与 mock 测试）。
    #: 取值 {"is_mock": bool, "executor": str, "reason": str, "unverified": [str]}。
    #: **必须随成片一起落盘** —— 成片文件在磁盘上无法区分「模型生成」与「占位媒体」，
    #: 来源只存在于当次 stdout 就无法长期守住该纪律。
    generation_provenance: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    @property
    def is_mock_output(self) -> bool:
        return bool(self.generation_provenance.get("is_mock", False))

    def __post_init__(self) -> None:
        if self.full_review_policy is not None:
            self.full_review_policy = _coerce(
                FullReviewPolicy, self.full_review_policy, "MovieMetadata.full_review_policy"
            )
        self.degrade_flags = [
            _coerce(DegradeFlag, d, "MovieMetadata.degrade_flags") for d in self.degrade_flags
        ]
        if not isinstance(self.generation_provenance, dict):
            raise ValueError("MovieMetadata.generation_provenance 必须是 dict")
        if self.movie_content_hash is not None and not _SHA256_RE.match(self.movie_content_hash):
            raise ValueError(
                f"MovieMetadata.movie_content_hash 格式非法：{self.movie_content_hash!r}"
            )

    def to_dict(self) -> dict:
        return _enum_to_value(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MovieMetadata":
        data = dict(data or {})
        data["segments"] = [
            SegmentManifestItem.from_dict(s) for s in data.get("segments", [])
        ]
        return _from_dict(cls, data)


__all__ = [
    "LOCAL_TZ",
    "now_iso",
    "new_token",
    "PromptParams",
    "AssetGenParams",
    "AssetReviewParams",
    "DirectorParams",
    "VideoReviewParams",
    "EncodeParams",
    "GlobalParams",
    "Config",
    "ReviewAttempt",
    "ReviewRecord",
    "AssetVersion",
    "AssetEntry",
    "SuspectRecord",
    "Shot",
    "Scene",
    "Chapter",
    "SegmentAttempt",
    "Segment",
    "SegmentManifestItem",
    "MovieMetadata",
]
