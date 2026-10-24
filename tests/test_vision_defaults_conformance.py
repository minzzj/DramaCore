"""配置默认值 × 流程图默认值 —— 一致性守卫。

## 为什么要有这个文件

`文档/流程图-总.txt`（用户说「整篇文章的核心」）在各节点里写明了默认值，
例如：

    C3["输出语言默认中文可选英文"]
    JP1["默认分段时长10秒可调5/10/15"]
    JP4["尾部帧数默认22可调"]
    AP5["模型审核候选上限 3轮 1-10轮 ..."]
    ENC3["字幕默认不加可开"]

代码里的 `config/default.yaml` 是这些默认值的落地处。
**两边一旦漂移**，用户会看到「文档说默认 10 秒，实际跑了别的值」这类问题，
而且很难发现（没人会去逐条比对）。

所以这里把对照**写成断言**，每条都注明流程图出处。
改动默认值时这条测试会红 —— 那就是提醒你「改之前先确认是不是有意为之」。

## 数据来源

* 期望值：`文档/流程图-总.txt`（图一 主流程图 SG1–SG8）
* 实际值：`config/default.yaml`（由纯 `Config()` 生成，见 `bettercomfyui/config.py`）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bettercomfyui.config import load_config  # noqa: E402


@pytest.fixture(scope="module")
def cfg():
    return load_config(repo_root=REPO_ROOT)


# ---------------------------------------------------------------------------
# SG1 / 提示词（图一 C*）
# ---------------------------------------------------------------------------


def test_prompt_defaults_match_flowchart(cfg):
    """`C3 输出语言默认中文` / `C4 输出格式结构化JSON+分隔符` /
    `C5 分镜粒度默认按镜头` / `已有素材注入 开启` / `名称归一化 开启`。"""
    p = cfg.prompt
    assert p.output_language == "zh", "C3：输出语言应默认中文"
    assert p.output_format == "json", "C4：输出格式应默认结构化 JSON"
    assert p.story_granularity == "shot", "C5：分镜粒度应默认按镜头"
    assert p.inject_existing_assets is True, "参数建议：已有素材注入 开启（多章节必须开启）"
    assert p.normalize_names is True, "参数建议：名称归一化 开启（去空格统一小写统一标点）"


# ---------------------------------------------------------------------------
# SG2 / 素材生成参数
# ---------------------------------------------------------------------------


def test_asset_generation_defaults_match_flowchart(cfg):
    """`GP1 分辨率` 1024x1024、`GP2 采样步数` 25、`GP3 采样器` DPM++ 2M Karras、
    `GP4 CFG` 7、`GP6 批量数量` 4、`G4 数量上限每类默认最多9张可调`。"""
    a = cfg.assets
    assert a.resolution == 1024, "GP1：素材图像分辨率默认 1024x1024"
    assert a.steps == 25, "GP2：采样步数默认 25"
    assert a.sampler == "DPM++ 2M Karras", "GP3：采样器默认 DPM++ 2M Karras"
    assert a.cfg == 7.0, "GP4：CFG 默认 7"
    assert a.batch_count == 4, "GP6：批量数量默认 4"
    assert a.per_class_limit == 9, "G4：每类数量上限默认 9"


# ---------------------------------------------------------------------------
# SG3 / 素材审核
# ---------------------------------------------------------------------------


def test_asset_review_defaults_match_flowchart(cfg):
    """`H2 模型审核最多3次取最高分`、`H3 无人响应超时默认10分钟`、
    `H4 自动切换为模型模式`、`H6 评分阈值默认70可调`、
    `N 直接取最高分`（兜底 FALLBACK_PASS）。"""
    r = cfg.asset_review
    assert str(r.default_mode).endswith("MODEL"), "参数建议：默认审核模式 模型审核"
    assert r.human_timeout_minutes == 10, "H3：人工超时默认 10 分钟"
    assert r.model_candidate_limit == 3, "H2：模型审核候选上限 3 轮（CNT1）"
    assert r.score_threshold == 70.0, "H6：评分阈值默认 70"
    assert r.on_timeout == "switch_to_model", "H4：超时后自动切换为模型模式"
    assert r.new_version_requires_review is True, "素材版本规则：新版本必重审"
    assert r.fallback == "highest_of_candidates", "N：三轮仍不合格取最高分（FALLBACK_PASS）"
    assert r.timeout_pause_on_active_op is True, "超时暂停：有效审核操作期间暂停"


def test_asset_review_dimensions_match_flowchart(cfg):
    """`H5 评分维度画质一致性合规构图`。"""
    assert list(cfg.asset_review.score_dimensions) == ["画质", "一致性", "合规", "构图"], (
        "H5：评分维度应为 画质/一致性/合规/构图 四维"
    )


# ---------------------------------------------------------------------------
# SG4 / 导演台
# ---------------------------------------------------------------------------


def test_director_defaults_match_flowchart(cfg):
    """`JP1 默认分段时长10秒`、`JP2 默认分辨率1080P`、`JP3 默认帧率24fps`、
    `JP4 尾部帧数默认22可调`、`JP5 首尾帧选取默认第一帧和最后一帧`、
    `JP6 显存档位自动检测`、`JP7 剪枝模型默认开启`。"""
    d = cfg.director
    assert d.segment_seconds == 10, "JP1：默认分段时长 10 秒"
    assert d.resolution == "1080P", "JP2：默认分辨率 1080P"
    assert d.fps == 24, "JP3：默认帧率 24fps"
    assert d.tail_frames == 22, "JP4：尾部帧数默认 22"
    assert d.first_frame == "first" and d.last_frame == "last", (
        "JP5：首尾帧默认取第一帧和最后一帧"
    )
    assert d.vram_tier == "auto", "JP6：显存档位自动检测"
    assert d.pruned_model is True, "JP7：剪枝模型默认开启"
    assert d.save_vram_all is True, "O5：显存节省策略应默认全开"


# ---------------------------------------------------------------------------
# SG5 / 视频审核与重生成
# ---------------------------------------------------------------------------


def test_video_review_defaults_match_flowchart(cfg):
    """`R_scope` 默认 both、`R2 评分阈值默认70`、CNT2/CNT3 分离、
    `T 定位不合格段` -> `U 只重生成该段`、明确最大级联深度（禁止无限级联）。"""
    v = cfg.video_review
    assert v.audit_scope == "both", "审核范围默认建议 both（段级+整片）"
    assert v.threshold_segment == 70.0 and v.threshold_full == 70.0, "R2：评分阈值默认 70"
    assert v.regenerate_granularity == "segment", "T/U：只重做问题段"
    assert v.cnt2_limit == 3, "CNT2：段质量重做上限 3"
    assert v.cnt3_limit == 3, "CNT3：级联重做上限 3"
    assert isinstance(v.max_cascade_depth, int) and v.max_cascade_depth >= 1, (
        "任务书 §6：必须明确最大级联深度，不得无限级联"
    )


def test_video_review_dimensions_match_flowchart(cfg):
    """`R1 画面连贯角色一致动作合理无闪烁`，
    整片级：叙事流畅 / 音画同步 / 整体节奏 / 明显跳变。"""
    v = cfg.video_review
    assert list(v.r1a_dimensions) == ["画面连贯", "角色一致", "动作合理", "无闪烁"], (
        "R1（段级）：画面连贯/角色一致/动作合理/无闪烁"
    )
    assert list(v.r1b_dimensions) == ["叙事流畅", "音画同步", "节奏合理", "无明显跳变"], (
        "R1（整片级）：叙事流畅/音画同步/节奏合理/无明显跳变"
    )


# ---------------------------------------------------------------------------
# SG4 / 成片封装（ENC）
# ---------------------------------------------------------------------------


def test_encode_defaults_match_flowchart(cfg):
    """`ENC2 转场默认无可选淡入淡出`、`ENC3 字幕默认不加可开`、
    `ENC4 水印默认不加可开`、`ENC5 输出格式默认MP4`、`ENC6 编码默认H264 CRF18`、
    `OUT4 封面默认取第一帧可指定`。"""
    e = cfg.encode
    assert e.transition == "none", "ENC2：转场默认无"
    assert e.subtitle is False, "ENC3：字幕默认不加"
    assert e.watermark is False, "ENC4：水印默认不加"
    assert e.format == "MP4", "ENC5：输出格式默认 MP4"
    assert e.codec == "H.264", "ENC6：编码默认 H264"
    assert e.crf == 18, "ENC6：CRF 默认 18"
    assert e.cover == "first_frame", "OUT4：封面默认取第一帧"


# ---------------------------------------------------------------------------
# SG7 / 全局配置横切
# ---------------------------------------------------------------------------


def test_global_defaults_match_flowchart(cfg):
    """`Z4 日志级别默认INFO可调`、`Z5 日志记录所有手动操作和自动切换`、
    `检查点 关键阶段自动保存`、`Z3 配置导入导出支持JSON`、
    `技术重试上限 可配置`（且与质量返工分离 —— 见 CNT 测试）。"""
    g = cfg.global_
    assert g.log_level == "INFO", "Z4：日志级别默认 INFO"
    assert g.log_all_manual_and_auto_switch is True, (
        "Z5：日志记录所有手动操作和自动切换（开源用户都是开发者，只记日志即可）"
    )
    assert g.checkpoint_every_stage is True, "检查点：关键阶段自动保存"
    assert g.config_import_export is True, "Z3：配置导入导出支持 JSON"
    assert isinstance(g.tech_retry_limit, int) and g.tech_retry_limit >= 1, (
        "技术重试上限必须可配置且为正"
    )


def test_tech_retry_is_separate_from_quality_counters(cfg):
    """任务书 §F 规则 6：**技术重试不消耗 CNT2/CNT3**。

    所以技术重试上限与 CNT2/CNT3 必须是**不同的配置项**。
    这条防止有人图省事把它们合并成一个。
    """
    g = cfg.global_
    v = cfg.video_review
    assert not hasattr(g, "cnt2_limit") and not hasattr(g, "cnt3_limit"), (
        "CNT2/CNT3 属于 video_review，不该出现在 global_（语义隔离）"
    )
    assert not hasattr(v, "tech_retry_limit"), (
        "技术重试属于 global_，不该出现在 video_review（语义隔离）"
    )
