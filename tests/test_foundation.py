"""Lead 负责的地基模块测试：数据模型 / 配置 / 整片流水线。

这些测试覆盖三件「错了就全盘皆错」的事：
1. 数据模型的语义守卫真的会拒绝非法组合（不是装饰）。
2. 配置的未知键与超范围值在**加载期**就报错，不拖到运行期。
3. 整片阶段的三条硬规则：拼接门禁、R_full 只跑一次、失败策略必须显式配置。

配套要求：本文件里每个「守卫测试」都必须能因为实现被破坏而变红。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bettercomfyui.config import Config, ConfigError, config_from_dict, dump_config, load_config
from bettercomfyui.models.entities import (
    AssetVersion,
    MovieMetadata,
    ReviewAttempt,
    ReviewRecord,
    Segment,
    SegmentAttempt,
    now_iso,
)
from bettercomfyui.models.enums import (
    REVIEW_ABORTABLE_STATES,
    REVIEW_TRANSITIONS,
    AssetStatus,
    CounterKind,
    DegradeFlag,
    FullReviewPolicy,
    LockSource,
    LockStatus,
    ReviewMode,
    ReviewState,
    SegmentSource,
    SegmentStatus,
)
from bettercomfyui.pipeline.core import (
    PipelineError,
    apply_full_policy,
    full_review,
    select_final_versions,
)

FAKE_HASH = "sha256:" + "a" * 64


# ---------------------------------------------------------------------------
# 1. 枚举真源
# ---------------------------------------------------------------------------


def test_review_state_matches_spec_exactly():
    """审核状态码必须与方案第七部分·五 的 12 个状态逐字一致。"""
    expected = {
        "RECEIVED", "VALIDATING", "SCANNING", "CLASSIFYING", "AWAITING_REVIEW",
        "TIMEOUT", "ARBITRATING", "ALLOWED", "BLOCKED", "FALLBACK", "FAILED", "ABORTED",
    }
    assert {s.value for s in ReviewState} == expected


def test_terminal_states_are_exactly_four():
    """终态只有 4 个：ALLOWED / FALLBACK / FAILED / ABORTED。

    BLOCKED 不是终态 —— 它必须走向优化并产生新版本；
    如果这里把 BLOCKED 当终态，流程会停在「未通过」，永不重试。
    """
    terminal = {s.value for s in ReviewState if s.is_terminal}
    assert terminal == {"ALLOWED", "FALLBACK", "FAILED", "ABORTED"}
    assert not ReviewState.BLOCKED.is_terminal
    assert not ReviewState.AWAITING_REVIEW.is_terminal


def test_transition_table_only_targets_legal_states():
    for (src, event), dst in REVIEW_TRANSITIONS.items():
        assert isinstance(src, ReviewState), (src, event)
        assert isinstance(dst, ReviewState), (src, event)
        assert event, f"{src} 的事件名不能为空"


def test_abortable_states_exclude_terminal():
    """终态不允许再「被取消」；否则会产生第二个最终结论，违反唯一提交点。"""
    for s in REVIEW_ABORTABLE_STATES:
        assert not s.is_terminal, f"{s} 是终态，不应可被取消"


def test_counter_kinds_are_three_and_distinct():
    assert {c.value for c in CounterKind} == {"CNT1", "CNT2", "CNT3"}


def test_degrade_flags_match_spec():
    assert {d.value for d in DegradeFlag} == {
        "NONE", "CASCADE_DEGRADED", "FALLBACK_DEGRADED", "FULL_REVIEW_DEGRADED",
    }


# ---------------------------------------------------------------------------
# 2. 数据模型语义守卫
# ---------------------------------------------------------------------------


def test_asset_version_lock_requires_all_three_elements():
    """R3：LOCK 必须绑定 asset_id + version + content_hash，且结论必须是 ALLOWED/FALLBACK。"""
    # 缺 review_cycle_id
    with pytest.raises(ValueError, match="review_cycle_id"):
        AssetVersion(
            version="v1", content_hash=FAKE_HASH, file_path="a.png",
            status=AssetStatus.ALLOWED, lock_status=LockStatus.LOCKED,
            lock_source=LockSource.MODEL_PASS,
        )
    # 缺 lock_source
    with pytest.raises(ValueError, match="lock_source"):
        AssetVersion(
            version="v1", content_hash=FAKE_HASH, file_path="a.png",
            status=AssetStatus.ALLOWED, lock_status=LockStatus.LOCKED,
            review_cycle_id="asset:a1:v1",
        )
    # 状态不是 ALLOWED/FALLBACK
    with pytest.raises(ValueError, match="ALLOWED 或 FALLBACK"):
        AssetVersion(
            version="v1", content_hash=FAKE_HASH, file_path="a.png",
            status=AssetStatus.BLOCKED, lock_status=LockStatus.LOCKED,
            lock_source=LockSource.MODEL_PASS, review_cycle_id="asset:a1:v1",
        )


def test_asset_version_rejects_unlocked_with_lock_source():
    with pytest.raises(ValueError, match="不得携带 lock_source"):
        AssetVersion(
            version="v1", content_hash=FAKE_HASH, file_path="a.png",
            lock_status=LockStatus.UNLOCKED, lock_source=LockSource.HUMAN_PASS,
        )


def test_asset_version_rejects_bad_content_hash_format():
    with pytest.raises(ValueError, match="content_hash 格式非法"):
        AssetVersion(version="v1", content_hash="deadbeef", file_path="a.png")
    with pytest.raises(ValueError, match="content_hash 格式非法"):
        # 大写 hex 不接受（保证指纹可比较）
        AssetVersion(version="v1", content_hash="sha256:" + "A" * 64, file_path="a.png")


def test_review_record_allowed_must_carry_lock_source():
    with pytest.raises(ValueError, match="必须携带 lock_source"):
        ReviewRecord(
            review_cycle_id="asset:a1:v1", mod_id="MOD_asset_a1_1234abcd",
            review_state=ReviewState.ALLOWED, mode=ReviewMode.MODEL,
            target_kind="asset", target_id="a1", version="v1",
        )


def test_review_record_lock_source_only_when_allowed_or_fallback():
    with pytest.raises(ValueError, match="只允许在 ALLOWED 或 FALLBACK"):
        ReviewRecord(
            review_cycle_id="asset:a1:v1", mod_id="m", review_state=ReviewState.BLOCKED,
            mode=ReviewMode.MODEL, target_kind="asset", target_id="a1", version="v1",
            lock_source=LockSource.MODEL_PASS,
        )


def test_review_record_valid_fallback_is_accepted():
    rec = ReviewRecord(
        review_cycle_id="asset:a1:v1", mod_id="m", review_state=ReviewState.FALLBACK,
        mode=ReviewMode.MODEL, target_kind="asset", target_id="a1", version="v1",
        lock_source=LockSource.FALLBACK_PASS,
    )
    assert rec.to_dict()["review_state"] == "FALLBACK"
    assert rec.to_dict()["lock_source"] == "FALLBACK_PASS"


def test_segment_asset_refs_must_carry_version():
    """R3 的静态前置：段引用的素材必须指定具体版本，不能只给名称。"""
    with pytest.raises(ValueError, match="asset_id 与 version"):
        Segment(
            segment_id="s1", segment_index=1, chapter_id="CH001",
            asset_refs=[{"asset_id": "a1"}],
        )
    ok = Segment(
        segment_id="s1", segment_index=1, chapter_id="CH001",
        asset_refs=[{"asset_id": "a1", "version": "v2"}],
    )
    assert ok.asset_refs[0]["version"] == "v2"


def test_unknown_enum_value_raises_not_silently_defaults():
    """非法枚举必须报错。静默回退会把「未识别状态」当成正常状态。"""
    with pytest.raises(ValueError, match="不是合法的 SegmentStatus"):
        Segment(segment_id="s1", segment_index=1, chapter_id="CH001", status="WEIRD")


def test_enum_serialization_handles_str_enum_subclass():
    """回归：_StrEnum 同时继承 str 与 Enum，序列化时必须先判 Enum。"""
    seg = Segment(
        segment_id="s1", segment_index=1, chapter_id="CH001",
        status=SegmentStatus.GENERATED, source=SegmentSource.REVIEW_FALLBACK,
        degrade_flags=[DegradeFlag.CASCADE_DEGRADED],
    )
    d = seg.to_dict()
    assert d["status"] == "GENERATED"
    assert d["source"] == "REVIEW_FALLBACK"
    assert d["degrade_flags"] == ["CASCADE_DEGRADED"]
    assert json.dumps(d, ensure_ascii=False)  # 必须可 JSON 序列化


def test_movie_metadata_serializes_policy_and_flags():
    md = MovieMetadata(
        project_id="p", full_review_policy=FullReviewPolicy.DEGRADE_OUTPUT,
        degrade_flags=[DegradeFlag.FULL_REVIEW_DEGRADED],
    )
    d = md.to_dict()
    assert d["full_review_policy"] == "DEGRADE_OUTPUT"
    assert d["degrade_flags"] == ["FULL_REVIEW_DEGRADED"]


def test_now_iso_is_timezone_aware_local():
    ts = now_iso()
    assert ts.endswith("+08:00"), f"统一后端时间戳必须带时区：{ts}"


def test_roundtrip_segment_and_attempt():
    seg = Segment(
        segment_id="p_CH001_SEG001", segment_index=1, chapter_id="CH001",
        asset_refs=[{"asset_id": "a1", "version": "v2", "type": "CHARACTER"}],
        status=SegmentStatus.GENERATED, current_version="v3",
    )
    seg.attempts.append(
        SegmentAttempt(attempt_id="ATTEMPT_x_v3_1", segment_id=seg.segment_id, version="v3", index=1)
    )
    restored = Segment.from_dict(seg.to_dict())
    assert restored.segment_id == seg.segment_id
    assert restored.status is SegmentStatus.GENERATED
    assert restored.attempts[0].version == "v3"


# ---------------------------------------------------------------------------
# 3. 配置加载
# ---------------------------------------------------------------------------


def test_default_config_loads_from_repo(repo_root: Path):
    cfg = load_config(repo_root=repo_root)
    assert cfg.asset_review.human_timeout_minutes == 10
    assert cfg.asset_review.model_candidate_limit == 3
    assert cfg.video_review.cnt2_limit == 3
    assert cfg.video_review.cnt3_limit == 3
    assert cfg.video_review.audit_scope.value == "both"
    # 未显式配置：必须在「使用时」报错，而不是给个默认策略
    assert cfg.video_review.full_review_policy is None


def test_unknown_config_key_is_rejected():
    with pytest.raises(ConfigError, match="未知键"):
        config_from_dict({"prompt": {"nonexistent_key": 1}})


def test_unknown_config_section_is_rejected():
    with pytest.raises(ConfigError, match="未知段"):
        config_from_dict({"nonexistent_section": {}})


@pytest.mark.parametrize(
    "payload,needle",
    [
        ({"video_review": {"cnt2_limit": 0}}, "超出允许范围"),
        ({"video_review": {"cnt3_limit": 0}}, "超出允许范围"),
        ({"asset_review": {"human_timeout_minutes": 0}}, "超出允许范围"),
        ({"encode": {"crf": 99}}, "超出允许范围"),
        ({"assets": {"steps": 0}}, "超出允许范围"),
    ],
)
def test_out_of_range_values_rejected(payload, needle):
    with pytest.raises(ConfigError, match=needle):
        config_from_dict(payload)


def test_enum_config_value_validated():
    with pytest.raises(ConfigError, match="非法"):
        config_from_dict({"video_review": {"audit_scope": "sometimes"}})
    ok = config_from_dict({"video_review": {"audit_scope": "segment"}})
    assert ok.video_review.audit_scope.value == "segment"


def test_config_trap_timeout_pause_with_skip_is_rejected():
    """关闭超时暂停 + on_timeout=skip ⇒ 人工审核永久挂起。这是配置陷阱，必须挡住。"""
    with pytest.raises(ConfigError, match="配置陷阱"):
        config_from_dict({
            "asset_review": {"timeout_pause_on_active_op": False, "on_timeout": "skip"}
        })


def test_config_precedence_project_over_user_over_default(tmp_path: Path):
    """优先级：项目 config.yaml > 用户 user.yaml > 默认 default.yaml。"""
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "default.yaml").write_text(
        "director:\n  segment_seconds: 10\n  fps: 24\n", encoding="utf-8"
    )
    (repo / "config" / "user.yaml").write_text(
        "director:\n  segment_seconds: 15\n", encoding="utf-8"
    )
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "config.yaml").write_text("director:\n  fps: 30\n", encoding="utf-8")

    cfg = load_config(repo_root=repo, project_dir=proj)
    assert cfg.director.segment_seconds == 15, "用户配置应覆盖默认"
    assert cfg.director.fps == 30, "项目配置应覆盖用户与默认"


def test_dump_config_strips_api_key(tmp_path: Path):
    """导出配置绝不能带真实密钥。"""
    cfg = Config()
    cfg.prompt.api_key = "sk-secret-should-not-leak"
    out = tmp_path / "exported.yaml"
    dump_config(cfg, out)
    text = out.read_text(encoding="utf-8")
    assert "sk-secret-should-not-leak" not in text
    assert "api_key: null" in text


def test_dumped_default_config_reloads_identically(repo_root: Path):
    """导出->重新加载必须等价（防止 dump 漏字段导致配置漂移）。"""
    original = load_config(repo_root=repo_root)
    assert original.to_dict() == load_config(repo_root=repo_root).to_dict()


# ---------------------------------------------------------------------------
# 4. 整片流水线
# ---------------------------------------------------------------------------


def _seg(sid: str, idx: int, status: SegmentStatus, ver: str | None) -> Segment:
    return Segment(
        segment_id=sid, segment_index=idx, chapter_id="CH001",
        status=status, candidate_version=ver,
    )


def test_splice_gate_rejects_unusable_segments_and_orders_by_index():
    segs = [
        _seg("s3", 3, SegmentStatus.PASSED, "v1"),
        _seg("s1", 1, SegmentStatus.PASSED, "v1"),
        _seg("s2", 2, SegmentStatus.FAILED, "v2"),
        _seg("s4", 4, SegmentStatus.EXCLUDED, "v3"),
        _seg("s5", 5, SegmentStatus.FALLBACK_SELECTED, "v4"),
        _seg("s6", 6, SegmentStatus.PASSED, None),
        _seg("s7", 7, SegmentStatus.ABORTED, "v9"),
        _seg("s8", 8, SegmentStatus.PLANNED, "v1"),
    ]
    selected = [s.segment_id for s in select_final_versions(segs)]
    assert selected == ["s1", "s3", "s5"], (
        "只有 PASSED / FALLBACK_SELECTED 且已确定版本的段可进入拼接；"
        "兜底段应正常参与，其余一律排除"
    )


def test_full_review_refuses_placeholder_scorer(tmp_path: Path):
    """不允许用占位分数冒充审核通过 —— 那是把未审核成片说成已通过。"""
    movie = tmp_path / "m.mp4"
    movie.write_bytes(b"x" * 128)
    with pytest.raises(PipelineError, match="未配置评分模型"):
        full_review(movie, Config())


def test_full_review_missing_file_raises(tmp_path: Path):
    with pytest.raises(PipelineError, match="文件不存在"):
        full_review(tmp_path / "nope.mp4", Config(), scorer=lambda p, d: {})


def test_full_review_scores_and_threshold():
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fh:
        fh.write(b"v" * 64)
        path = Path(fh.name)
    try:
        counter: list[int] = []
        cfg = Config()
        dims = cfg.video_review.r1b_dimensions
        scorer = lambda p, d: {k: 80.0 for k in d}  # noqa: E731
        res = full_review(path, cfg, scorer=scorer, call_counter=counter)
        assert res["passed"] is True
        assert set(res["r1b_scores"]) == set(dims)
        assert res["final_score"] == pytest.approx(80.0)
        assert len(counter) == 1, "R_full 必须只执行一次"

        low = full_review(path, cfg, scorer=lambda p, d: {k: 10.0 for k in d})
        assert low["passed"] is False
        assert "低于阈值" in low["failure_reason"]
    finally:
        path.unlink(missing_ok=True)


def test_full_review_rejects_missing_dimension_and_out_of_range():
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fh:
        fh.write(b"v" * 64)
        path = Path(fh.name)
    try:
        cfg = Config()
        dims = cfg.video_review.r1b_dimensions
        with pytest.raises(PipelineError, match="缺少维度"):
            full_review(path, cfg, scorer=lambda p, d: {d[0]: 90.0})
        with pytest.raises(PipelineError, match=r"\[0,100\]"):
            full_review(path, cfg, scorer=lambda p, d: {k: 150.0 for k in d})
    finally:
        path.unlink(missing_ok=True)


def test_full_policy_requires_explicit_config():
    """方案第七部分·二 第 10 条：不得隐式采用未记录的行为。"""
    failed = {"passed": False, "final_score": 50.0, "failure_reason": "low"}
    with pytest.raises(ConfigError, match="未显式配置"):
        apply_full_policy(failed, Config())


def test_full_policy_degrade_and_block():
    failed = {"passed": False, "final_score": 50.0, "failure_reason": "low"}
    cfg = Config()

    cfg.video_review.full_review_policy = FullReviewPolicy.DEGRADE_OUTPUT
    out = apply_full_policy(failed, cfg)
    assert out["action"] == "degrade"
    assert DegradeFlag.FULL_REVIEW_DEGRADED in out["degrade_flags"]
    assert out["delivered_with_degradation"] is True

    cfg.video_review.full_review_policy = FullReviewPolicy.BLOCK_OUTPUT
    out2 = apply_full_policy(failed, cfg)
    assert out2["action"] == "block"
    assert out2["degrade_flags"] == []
    assert out2["delivered_with_degradation"] is False


def test_full_policy_pass_proceeds_without_degradation():
    cfg = Config()
    out = apply_full_policy({"passed": True, "final_score": 90.0}, cfg)
    assert out["action"] == "proceed"
    assert out["degrade_flags"] == []
    assert out["delivered_with_degradation"] is False


# ---------------------------------------------------------------------------
# 5. 索引损坏 vs 索引不存在：R3 门禁的可诊断性
# ---------------------------------------------------------------------------


def test_corrupt_zero_byte_index_is_not_silently_treated_as_unreviewed(tmp_path: Path):
    """**零字节 meta.json 必须报错，不能被当成「所有版本都未审核」。**

    这是一条真实的坑（由 bugfix-majors 复现、Lead 修复）：
    `read_json` 把「空内容」统一映射成 `default=None`，
    于是 0 字节的 meta.json 看起来与「文件不存在」一模一样，
    最终 `resolve_lock` 静默返回 None —— 把**索引损坏**误报成**未审核**。

    表面后果只是「拒绝生成」（无害），但真实索引已损坏却无人察觉，
    且下一次写入会把它永久覆盖 —— 那是不可恢复的数据丢失。
    """
    from bettercomfyui.store.assets import AssetStore
    from bettercomfyui.store.errors import CorruptFileError
    from bettercomfyui.store.ids import new_project_id
    from bettercomfyui.store.paths import ProjectPaths

    paths = ProjectPaths(root=tmp_path / "projects", project_id=new_project_id())
    paths.ensure_layout()
    store = AssetStore(paths)

    # 文件不存在 -> 正常返回空索引（首次写入前的合法状态）
    assert store.load_index()["assets"] == []
    assert store.resolve_lock("no_such_asset", "v1") is None

    # 文件存在但为 0 字节 -> 必须报错，不得伪装成「空素材库」
    paths.meta_json.write_bytes(b"")
    with pytest.raises(CorruptFileError, match="存在但内容为空"):
        store.load_index()
    with pytest.raises(CorruptFileError, match="存在但内容为空"):
        store.resolve_lock("no_such_asset", "v1")


def test_corrupt_index_error_is_classified_as_technical_failure(tmp_path: Path):
    """损坏必须归类为技术故障（FILE_ENCODING），而不是「审核未通过」。

    调用方要能区分这两者：前者应重试/报警，后者是正常的门禁拒绝。
    """
    from bettercomfyui.store.assets import AssetStore
    from bettercomfyui.store.errors import CorruptFileError, StoreError
    from bettercomfyui.store.ids import new_project_id
    from bettercomfyui.store.paths import ProjectPaths

    paths = ProjectPaths(root=tmp_path / "projects", project_id=new_project_id())
    paths.ensure_layout()
    store = AssetStore(paths)
    store.load_index()
    paths.meta_json.write_bytes(b"")

    with pytest.raises(StoreError) as exc_info:
        store.resolve_lock("a1", "v1")
    err = exc_info.value
    assert isinstance(err, CorruptFileError)
    ctx = getattr(err, "context", {}) or {}
    assert str(ctx.get("error_class")) == "FILE_ENCODING", (
        f"损坏异常必须携带 error_class=FILE_ENCODING，实际 context={ctx}"
    )


# ---------------------------------------------------------------------------
# 6. 默认配置必须可移植（不得含机器相关路径）
# ---------------------------------------------------------------------------


def test_dumped_default_config_has_no_machine_paths(repo_root: Path):
    """`config/default.yaml` **不得**包含任何机器相关路径。

    实测踩到的坑：用 `load_config()` 的结果去 dump，会把 `config/user.yaml` 里的
    `comfyui_base_path`（本机绝对路径）写进**默认**配置 —— 换台机器就指错地方。
    生成 default.yaml 必须用**纯净的 `Config()`**，机器相关值只放 user.yaml。
    """
    text = (repo_root / "config" / "default.yaml").read_text(encoding="utf-8")
    for needle in ("C:\\", "H:\\", "/home/", "/Users/"):
        assert needle not in text, (
            f"default.yaml 里出现了机器相关路径片段 {needle!r}："
            "默认配置必须可移植（机器相关路径应放 config/user.yaml）"
        )
    assert "comfyui_base_path: ''" in text or 'comfyui_base_path: ""' in text, (
        "default.yaml 里的 comfyui_base_path 应为空字符串（表示未配置）"
    )


def test_dumped_default_config_is_reproducible(repo_root: Path):
    """再次 dump 纯默认值应与磁盘上的 default.yaml 一致（防止手改漂移）。"""
    from bettercomfyui.config import Config, dump_config

    tmp = repo_root / "config" / "_tmp_default_check.yaml"
    try:
        dump_config(Config(), tmp)
        assert tmp.read_text(encoding="utf-8") == (
            repo_root / "config" / "default.yaml"
        ).read_text(encoding="utf-8"), (
            "config/default.yaml 与 `Config()` 的序列化结果不一致 —— "
            "说明它被手工改过或与代码漂移。请重新生成。"
        )
    finally:
        tmp.unlink(missing_ok=True)
