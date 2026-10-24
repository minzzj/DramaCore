"""导演台层：段计划、分段生成、段级审核、返工与级联（契约 §7）。

依赖方向：`director` -> `store` + `review` + `executors` + `models`；**不得 import `pipeline`**。
"""

from __future__ import annotations

from bettercomfyui.director.cascade import (
    CASCADE_POLICIES,
    CONTENT_DEPENDENCY_KINDS,
    CascadeController,
)
from bettercomfyui.director.deps import (
    AssetLockResolver,
    CounterLike,
    IdFactory,
    default_counters,
    default_ids,
)
from bettercomfyui.director.errors import (
    CascadeDependencyMissing,
    CascadePolicyError,
    DirectorConfigError,
    DirectorDependencyError,
    DirectorError,
    EdgeVerificationError,
    LockGateError,
    ReviewMaterialMissing,
    ReviewScopeViolation,
    SegmentPlanError,
    StaleSegmentVersion,
)
from bettercomfyui.director.events import EventRecorder
from bettercomfyui.director.generate import SegmentGenerator, TechRetryCounter, classify_error
from bettercomfyui.director.plan import (
    DEPENDENCY_KINDS,
    FACET_KINDS,
    dependency_edge,
    encode_dependency_ref,
    facet_value,
    parse_dependency_ref,
    plan_segments,
)
from bettercomfyui.director.review import R1A_DIMENSIONS, SegmentReviewer
from bettercomfyui.director.rework import ReworkController, select_highest_score_candidate
from bettercomfyui.director.settings import cfg_get, cfg_section

__all__ = [
    "CASCADE_POLICIES",
    "CONTENT_DEPENDENCY_KINDS",
    "DEPENDENCY_KINDS",
    "FACET_KINDS",
    "R1A_DIMENSIONS",
    "AssetLockResolver",
    "CascadeController",
    "CascadeDependencyMissing",
    "CascadePolicyError",
    "CounterLike",
    "DirectorConfigError",
    "DirectorDependencyError",
    "DirectorError",
    "EdgeVerificationError",
    "EventRecorder",
    "IdFactory",
    "LockGateError",
    "ReviewMaterialMissing",
    "ReviewScopeViolation",
    "ReworkController",
    "SegmentGenerator",
    "SegmentPlanError",
    "SegmentReviewer",
    "StaleSegmentVersion",
    "TechRetryCounter",
    "cfg_get",
    "cfg_section",
    "classify_error",
    "default_counters",
    "default_ids",
    "dependency_edge",
    "encode_dependency_ref",
    "facet_value",
    "parse_dependency_ref",
    "plan_segments",
    "select_highest_score_candidate",
]
