"""Better ComfyUI 状态、来源与标记枚举（唯一真源 / single source of truth）。

所有枚举值严格来自《BetterComfyUI完整方案.docx》第七部分「版本、来源与降级枚举」
与「审核流程标识体系与状态码」。任何模块都不得自行定义同义枚举；
如需扩展，必须先修改本文件并同步 docs/SPEC.md 与 docs/CONTRACTS.md。

设计约束（来自任务书与方案第七部分）：
- ID 保持稳定；"待验证/已通过/已降级"等可变状态必须作为独立字段保存，
  不允许写进 ID 的一部分。
- FAILED（技术失败）与 BLOCKED（质量不合格）语义不同，绝不混用。
- CNT1 / CNT2 / CNT3 作用于不同逻辑任务，互不累加，且不因版本号递增而清零。
"""

from __future__ import annotations

from enum import Enum


class _StrEnum(str, Enum):
    """同时是 str 的枚举，便于 JSON 序列化与持久化。"""

    def __str__(self) -> str:  # pragma: no cover - 便利方法
        return str(self.value)

    @classmethod
    def parse(cls, value):
        """宽松解析：接受枚举实例、字符串或 None。

        对未知值抛出 ValueError，绝不静默回退默认值——静默回退会造成
        「把未识别状态当成正常状态」的假成功。
        """
        if value is None or isinstance(value, cls):
            return value
        try:
            return cls(str(value))
        except ValueError as exc:
            allowed = ", ".join(m.value for m in cls)
            raise ValueError(f"{cls.__name__} 不接受取值 {value!r}；允许值：{allowed}") from exc


# ---------------------------------------------------------------------------
# 审核状态码（方案第七部分 · 五）
# ---------------------------------------------------------------------------


class ReviewState(_StrEnum):
    """审核周期状态机。每个「状态 + 事件」组合只允许一个目标状态。"""

    RECEIVED = "RECEIVED"                    # 已接收，等待审核
    VALIDATING = "VALIDATING"                # 确定性校验中
    SCANNING = "SCANNING"                    # 模型扫描中
    CLASSIFYING = "CLASSIFYING"              # 分类/评分中
    AWAITING_REVIEW = "AWAITING_REVIEW"      # 等待人工审核
    TIMEOUT = "TIMEOUT"                      # 人工审核超时
    ARBITRATING = "ARBITRATING"              # 仲裁中
    ALLOWED = "ALLOWED"                      # 正常审核通过
    BLOCKED = "BLOCKED"                      # 审核未通过，等待优化/新候选
    FALLBACK = "FALLBACK"                    # 达到候选上限后选最高分
    FAILED = "FAILED"                        # 技术失败
    ABORTED = "ABORTED"                      # 任务已取消或结果已失效

    @property
    def is_terminal(self) -> bool:
        """终态：ALLOWED / FALLBACK / FAILED / ABORTED。

        BLOCKED 不是终态——它必须走向优化并产生新版本。
        AWAITING_REVIEW / TIMEOUT / ARBITRATING 是等待态，也不是终态。
        """
        return self in _REVIEW_TERMINAL_STATES


_REVIEW_TERMINAL_STATES = frozenset(
    {ReviewState.ALLOWED, ReviewState.FALLBACK, ReviewState.FAILED, ReviewState.ABORTED}
)

#: 允许的状态转移表，直接照搬方案第七部分「六、状态转移规则」。
#: key = (当前状态, 事件名)，value = 目标状态。未列出的组合一律拒绝。
REVIEW_TRANSITIONS: dict[tuple[ReviewState, str], ReviewState] = {
    (ReviewState.RECEIVED, "start_review"): ReviewState.VALIDATING,
    (ReviewState.VALIDATING, "validation_passed"): ReviewState.SCANNING,
    (ReviewState.VALIDATING, "validation_failed"): ReviewState.FAILED,
    (ReviewState.SCANNING, "scan_completed"): ReviewState.CLASSIFYING,
    (ReviewState.SCANNING, "scan_technical_failure"): ReviewState.FAILED,
    (ReviewState.CLASSIFYING, "score_above_threshold"): ReviewState.ALLOWED,
    (ReviewState.CLASSIFYING, "score_below_threshold_retryable"): ReviewState.BLOCKED,
    (ReviewState.CLASSIFYING, "score_below_threshold_at_candidate_limit"): ReviewState.FALLBACK,
    (ReviewState.AWAITING_REVIEW, "human_pass"): ReviewState.ALLOWED,
    (ReviewState.AWAITING_REVIEW, "human_reject"): ReviewState.BLOCKED,
    (ReviewState.AWAITING_REVIEW, "timeout_reached"): ReviewState.TIMEOUT,
    (ReviewState.TIMEOUT, "auto_switch_model_started"): ReviewState.SCANNING,
    (ReviewState.TIMEOUT, "late_human_result_needs_arbitration"): ReviewState.ARBITRATING,
    (ReviewState.SCANNING, "late_human_result_arrived"): ReviewState.ARBITRATING,
    (ReviewState.CLASSIFYING, "model_result_ready_with_pending_human"): ReviewState.ARBITRATING,
    (ReviewState.ARBITRATING, "human_earlier_and_passed"): ReviewState.ALLOWED,
    (ReviewState.ARBITRATING, "human_earlier_and_rejected"): ReviewState.BLOCKED,
    (ReviewState.ARBITRATING, "model_earlier_or_equal_and_passed"): ReviewState.ALLOWED,
    (ReviewState.ARBITRATING, "model_earlier_or_equal_not_passed_retryable"): ReviewState.BLOCKED,
    (ReviewState.ARBITRATING, "model_earlier_or_equal_not_passed_at_limit"): ReviewState.FALLBACK,
    (ReviewState.BLOCKED, "optimized_new_version_created"): ReviewState.VALIDATING,
    (ReviewState.FAILED, "technical_recovery_resubmit"): ReviewState.RECEIVED,
}

#: 「任意可运行状态 + 用户主动取消 -> ABORTED」
REVIEW_ABORTABLE_STATES = frozenset(
    {
        ReviewState.RECEIVED,
        ReviewState.VALIDATING,
        ReviewState.SCANNING,
        ReviewState.CLASSIFYING,
        ReviewState.AWAITING_REVIEW,
        ReviewState.TIMEOUT,
        ReviewState.ARBITRATING,
        ReviewState.BLOCKED,
    }
)


# ---------------------------------------------------------------------------
# LOCK 来源、段来源、审核模式、降级标记（方案第七部分 · 八）
# ---------------------------------------------------------------------------


class LockSource(_StrEnum):
    """LOCK 来源。一个 LOCK 只授权它所对应的素材版本，不跨版本继承。"""

    HUMAN_PASS = "HUMAN_PASS"        # 人工审核正常通过
    MODEL_PASS = "MODEL_PASS"        # 模型审核正常通过
    FALLBACK_PASS = "FALLBACK_PASS"  # 模型审核达到候选上限后选最高分


class SegmentSource(_StrEnum):
    """视频段来源。"""

    NORMAL = "NORMAL"                    # 正常生成并通过段级审核
    REGENERATED = "REGENERATED"          # 局部重生成后形成的新段版本
    REVIEW_FALLBACK = "REVIEW_FALLBACK"  # 段级审核达上限后选最高分候选
    CASCADE_FALLBACK = "CASCADE_FALLBACK"  # 级联段审核达上限后选最高分候选


class ReviewMode(_StrEnum):
    """审核模式。"""

    HUMAN = "HUMAN"                  # 人工审核模式
    MODEL = "MODEL"                  # 模型审核模式
    AUTO_SWITCHED = "AUTO_SWITCHED"  # 人工超时后自动切换为模型审核


class DegradeFlag(_StrEnum):
    """降级标记。NONE 之外的值必须写入成片元数据，不允许隐式降级。"""

    NONE = "NONE"
    CASCADE_DEGRADED = "CASCADE_DEGRADED"          # 级联段达上限仍未完全通过边界验证
    FALLBACK_DEGRADED = "FALLBACK_DEGRADED"        # 采用普通候选兜底策略
    FULL_REVIEW_DEGRADED = "FULL_REVIEW_DEGRADED"  # 整片审核未达阈值但按配置允许降级输出


class CounterKind(_StrEnum):
    """三个独立计数器的作用域。绝不互相累加，绝不因版本号变化清零。"""

    CNT1 = "CNT1"  # 素材模型审核候选轮次
    CNT2 = "CNT2"  # 当前问题视频段因质量不合格重做次数
    CNT3 = "CNT3"  # 因前段变化触发的级联衔接重做次数


# ---------------------------------------------------------------------------
# 素材层
# ---------------------------------------------------------------------------


class AssetType(_StrEnum):
    """素材类别。方案要求至少四类，允许按需求扩展。

    后两类（`AUDIO` / `VIDEO`）是为**融合**补的：三个导演台的 r2v 组支持
    `ref_audios`（音色 / 音画参考）与 `ref_videos`（参考视频），
    方案的「参考主体 / 音画参考」也要求这些能力。

    为什么不硬塞进 `PROP`：素材库就再也分不清"参考视频"与"道具图"，
    审核阈值、清单注入、节点槽位会跟着糊掉。
    """

    CHARACTER = "CHARACTER"  # 人物/角色
    SCENE = "SCENE"          # 场景
    PROP = "PROP"            # 物品
    SHOT = "SHOT"            # 分镜/镜头
    AUDIO = "AUDIO"          # 音频参考（音色 / 音画参考）
    VIDEO = "VIDEO"          # 视频参考（参考视频）


class LibraryScope(_StrEnum):
    """素材库作用域。查找优先级与冲突规则必须显式定义，防止跨项目串用。"""

    PROJECT = "PROJECT"  # 项目库
    PUBLIC = "PUBLIC"    # 公共库


class AssetSource(_StrEnum):
    """素材来源。手动添加允许直接入库，但必须记录来源与未审核状态。"""

    AI = "AI"                # 模型生成
    MANUAL = "MANUAL"        # 手动添加
    IMPORTED = "IMPORTED"    # 外部导入


class AssetStatus(_StrEnum):
    """素材版本生命周期状态。

    MANUAL_UNREVIEWED 是方案第四部分「手动添加素材不审核」的落地标记：
    手动添加直接入库，但绝不伪装成已审核。
    """

    DRAFT = "DRAFT"                          # 已生成/已入库，尚未进入审核
    PENDING_REVIEW = "PENDING_REVIEW"        # 审核中
    SUSPECT = "SUSPECT"                      # 疑似重复，待人工确认
    MANUAL_UNREVIEWED = "MANUAL_UNREVIEWED"  # 手动添加、未审核
    ALLOWED = "ALLOWED"                      # 已通过审核
    FALLBACK = "FALLBACK"                    # 兜底接受（明确标记）
    BLOCKED = "BLOCKED"                      # 审核未通过
    FAILED = "FAILED"                        # 技术失败
    ABORTED = "ABORTED"                      # 已取消
    SUPERSEDED = "SUPERSEDED"                # 已被更新版本取代（旧版本保留，不删除）


class LockStatus(_StrEnum):
    """素材版本锁定状态。LOCK 绑定 asset_id + version + content_hash。"""

    UNLOCKED = "UNLOCKED"
    LOCKED = "LOCKED"


class MatchOutcome(_StrEnum):
    """程序确定性匹配的三分支（方案第五部分 41/44 项修复）。"""

    EXISTS_REUSE = "EXISTS_REUSE"            # 明确匹配已有素材 -> 复用，不重复生成
    NEW_GENERATE = "NEW_GENERATE"            # 确定不存在 -> 进入生成队列
    SUSPECT_DUPLICATE = "SUSPECT_DUPLICATE"  # 疑似重复/歧义 -> 待确认，禁止自动覆盖


# ---------------------------------------------------------------------------
# 视频段层
# ---------------------------------------------------------------------------


class SegmentStatus(_StrEnum):
    """视频段任务状态。必须落盘，不能只存在于 UI 内存。"""

    PLANNED = "PLANNED"                          # 已建立段计划
    GENERATING = "GENERATING"                    # 生成中
    GENERATED = "GENERATED"                      # 已产出候选版本，待审核
    REVIEWING = "REVIEWING"                      # 段级审核中
    PASSED = "PASSED"                            # 本版本通过段级审核
    NEEDS_CASCADE_CHECK = "NEEDS_CASCADE_CHECK"  # 待衔接验证（Y2 标记）
    CASCADE_FAILED = "CASCADE_FAILED"            # 级联未通过边界验证
    FALLBACK_SELECTED = "FALLBACK_SELECTED"      # 达上限后取最高分
    FAILED = "FAILED"                            # 技术失败
    ABORTED = "ABORTED"                          # 已取消
    EXCLUDED = "EXCLUDED"                        # 不允许进入拼接


class AuditScope(_StrEnum):
    """审核执行范围。默认 both。"""

    SEGMENT = "segment"  # 仅段级
    FULL = "full"        # 仅整片
    BOTH = "both"        # 段级 + 整片


class JobStatus(_StrEnum):
    """执行器作业状态（ComfyUI 提交/轮询的统一抽象）。"""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"        # 技术失败，不消耗质量重做计数
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"      # 轮询异常/无法判定，必须显式处理，禁止当成功


class ErrorClass(_StrEnum):
    """错误分类（任务书第 6 节）。技术重试与质量返工必须分开计数。"""

    INPUT_VALIDATION = "INPUT_VALIDATION"  # 输入校验
    MODEL_API = "MODEL_API"                # 模型/API
    COMFY_WORKFLOW = "COMFY_WORKFLOW"      # ComfyUI 工作流
    FILE_ENCODING = "FILE_ENCODING"        # 文件/编码
    QUALITY_REVIEW = "QUALITY_REVIEW"      # 质量审核不合格（不是技术失败）
    USER_CANCELLED = "USER_CANCELLED"      # 用户取消


class ResumeTarget(_StrEnum):
    """断点续传恢复目标（方案第五部分第 31 项修复）。"""

    ASSET = "G"     # 素材阶段
    DIRECTOR = "P"  # 导演台阶段
    REVIEW = "R"    # 审核阶段


class FullReviewPolicy(_StrEnum):
    """整片审核失败策略。必须由项目配置显式选择，不得隐式决定。"""

    BLOCK_OUTPUT = "BLOCK_OUTPUT"      # 阻止输出，保留检查点
    DEGRADE_OUTPUT = "DEGRADE_OUTPUT"  # 降级输出，记录 FULL_REVIEW_DEGRADED


class Stage(_StrEnum):
    """关键阶段：检查点保存点与恢复锚点。"""

    PROJECT = "PROJECT"
    SCRIPT_PARSE = "SCRIPT_PARSE"
    ASSET_MATCH = "ASSET_MATCH"
    ASSET_GENERATE = "ASSET_GENERATE"
    ASSET_REVIEW = "ASSET_REVIEW"
    DIRECTOR_PLAN = "DIRECTOR_PLAN"
    SEGMENT_GENERATE = "SEGMENT_GENERATE"
    SEGMENT_REVIEW = "SEGMENT_REVIEW"
    CASCADE = "CASCADE"
    SPLICE = "SPLICE"
    FULL_REVIEW = "FULL_REVIEW"
    OUTPUT = "OUTPUT"


__all__ = [
    "ReviewState",
    "REVIEW_TRANSITIONS",
    "REVIEW_ABORTABLE_STATES",
    "LockSource",
    "SegmentSource",
    "ReviewMode",
    "DegradeFlag",
    "CounterKind",
    "AssetType",
    "LibraryScope",
    "AssetSource",
    "AssetStatus",
    "LockStatus",
    "MatchOutcome",
    "SegmentStatus",
    "AuditScope",
    "JobStatus",
    "ErrorClass",
    "ResumeTarget",
    "FullReviewPolicy",
    "Stage",
]
