"""验收 25：理论计算（无法真跑项的量化依据）。

## 为什么要有这一条

用户说：「对于视频生成等不能完成的任务则需要进行理论计算」。
本机纯 CPU、内存 15.75 GB、无可用 GPU —— 真实视频生成**不可能跑**。
但"不能跑"不等于"不能算"：权重体积、显存峰值、加载次数都是**确定可算**的。

## 这条验收真正断言的东西

不是"算得好看"，而是**算得能自证**：

    P1 权重体积来自**磁盘实测**（与真实文件大小一致，误差 < 1%）；
    P2 一次性全加载峰值 = 各件之和（算术自洽）；
    P3 阶段化峰值 = max(编码阶段, 生成阶段)，**严格小于**一次性峰值；
    P4 在 32 GB 预算下：一次性**装不下**、阶段化**装得下**
       —— 这正是"阶段化是必需品而非优化项"的量化证据；
    P5 加载次数：阶段化**与段数无关**，逐段交替**随段数线性增长**；
    P6 成片体积给的是**区间**而不是单点（CRF 是恒定质量，单点等于假装精确）；
    P7 所有"估算值"必须被明确标注为估算（不得把估算冒充实测）。

## 判定假通过（必须能变红）

    F1 把 phase_video 算成 unet+encoder+vae（没卸编码器）-> P3/P4 变红；
    F2 让 staged_loads 随段数增长 -> P5 变红；
    F3 把码率给成单点 -> P6 变红。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from theory_model import (  # noqa: E402
    GB,
    TARGET_VRAM_GB,
    bitrate_estimate,
    compute,
    load_counts,
    scan_weights,
    time_estimate,
)


@pytest.fixture(scope="module")
def base_dir() -> Path:
    from bettercomfyui.config import load_config

    cfg = load_config(repo_root=REPO_ROOT)
    base = Path(cfg.global_.comfyui_base_path or "")
    if not base.is_dir():
        pytest.skip("未验证：配置里没有可用的 ComfyUI 根目录，无法读权重体积")
    return base


# ---------------------------------------------------------------------------
# P1 体积来自磁盘实测
# ---------------------------------------------------------------------------


def test_p1_weight_sizes_come_from_disk(base_dir):
    """权重体积必须与磁盘上的真实文件一致。"""
    groups = scan_weights(base_dir)
    assert groups, "一个权重都没扫到 —— 分类逻辑可能坏了"

    checked = 0
    for items in groups.values():
        for item in items:
            found = list(base_dir.rglob(item["name"]))
            assert found, f"扫到了不存在的文件 {item['name']}（说明不是实测）"
            actual = round(found[0].stat().st_size / GB, 3)
            assert abs(actual - item["gb"]) < 0.01, (
                f"{item['name']} 体积不符：扫描 {item['gb']} vs 实测 {actual}"
            )
            checked += 1
    assert checked >= 4, f"只核对了 {checked} 个权重，样本太少"


def test_p1b_required_weights_are_identified(base_dir):
    """四类关键权重都要被识别出来（否则峰值算不出来）。"""
    sizes = compute(base_dir)["size_gb"]
    for key in ("video_unet", "text_encoder", "video_vae", "audio_vae"):
        assert sizes[key] > 0, f"没有识别出 {key} 的体积"


# ---------------------------------------------------------------------------
# P2 / P3 / P4 显存峰值与预算
# ---------------------------------------------------------------------------


def test_p2_all_at_once_is_the_sum(base_dir):
    """一次性全加载 = 各件之和（算术自洽）。"""
    result = compute(base_dir)
    s = result["size_gb"]
    expected = s["video_unet"] + s["text_encoder"] + s["video_vae"] + s["audio_vae"]
    assert abs(result["peak_gb"]["all_at_once"] - expected) < 0.01


def test_p3_staged_peak_is_strictly_lower(base_dir):
    """阶段化峰值必须**严格小于**一次性峰值（否则阶段化没有意义）。"""
    result = compute(base_dir)
    peak = result["peak_gb"]
    assert peak["staged"] < peak["all_at_once"], (
        f"阶段化峰值 {peak['staged']} 不低于一次性 {peak['all_at_once']}"
    )
    # 阶段化峰值必须是两个阶段里较大的那个
    assert peak["staged"] == max(peak["phase_text_encode"],
                                 peak["phase_video_generate"])


def test_p4_on_32gb_all_at_once_fails_but_staged_fits(base_dir):
    """**核心结论**：32 GB 下一次性装不下、阶段化装得下。"""
    result = compute(base_dir, target_vram_gb=TARGET_VRAM_GB)
    budget = result["budget"]

    assert budget["target_vram_gb"] == 32.0
    assert budget["all_at_once_fits"] is False, (
        "按当前权重算，一次性竟然装得下 —— 结论与之前相反，需重新核对模型版本"
    )
    assert budget["staged_fits"] is True, "阶段化装不下 —— 需要更强剪枝/量化"
    assert budget["all_at_once_over_by_gb"] > 0
    assert budget["staged_headroom_gb"] > 0

    # 结论文字必须与布尔一致，不能自相矛盾
    assert "必须阶段化" in result["verdict"], result["verdict"]


def test_p4b_budget_is_parameterised(base_dir):
    """换成更大的卡时结论要跟着变（说明计算不是写死的）。"""
    result = compute(base_dir, target_vram_gb=80.0)
    assert result["budget"]["all_at_once_fits"] is True
    assert result["budget"]["staged_fits"] is True
    assert "都装得下" in result["verdict"]


# ---------------------------------------------------------------------------
# P5 加载次数
# ---------------------------------------------------------------------------


def test_p5_staged_loads_independent_of_segments():
    """阶段化加载次数与段数**无关**；逐段交替随段数**线性增长**。"""
    for segments in (1, 5, 10, 50):
        counts = load_counts(segments)
        assert counts["staged_loads"] == 5, (
            f"{segments} 段时阶段化加载 {counts['staged_loads']} 次 —— 应当恒定"
        )
        assert counts["alternated_loads"] == 2 * segments + 2
        assert counts["saved_loads"] >= 0
    # 段数越多，优势越大
    assert load_counts(50)["ratio"] > load_counts(5)["ratio"]


def test_p5b_time_estimate_uses_staged_loads():
    """时长估算的假设必须写出来（估算值不得冒充实测）。"""
    est = time_estimate(10, seconds_per_segment=60.0, load_seconds=45.0)
    assert est["assumptions"]["load_seconds_per_model"] == 45.0
    assert "估算" in est["assumptions"]["note"], "估算值没有被标注"
    # 阶段化必然更快（省下 17 次加载 = 765 秒）
    assert est["saved_seconds"] > 0
    assert est["staged_seconds"] < est["alternated_seconds"]


def test_p5c_time_math_is_consistent():
    """手算校验：阶段化 = 段数*每段 + 5*加载。"""
    est = time_estimate(10, seconds_per_segment=60.0, load_seconds=45.0)
    assert est["staged_seconds"] == pytest.approx(10 * 60 + 5 * 45, abs=0.5)
    assert est["alternated_seconds"] == pytest.approx(10 * 60 + 22 * 45, abs=0.5)


# ---------------------------------------------------------------------------
# P6 / P7 码率与估算标注
# ---------------------------------------------------------------------------


def test_p6_bitrate_gives_a_range_not_a_point():
    """CRF 恒定质量 -> 必须给区间，不能给单点（单点等于假装精确）。"""
    br = bitrate_estimate(seconds=3600)
    low, high = br["bitrate_mbps_range"]
    assert low < high, f"码率给成了单点：{br['bitrate_mbps_range']}"
    size_low, size_high = br["size_gb_range"]
    assert size_low < size_high
    # 1 小时 8-12 Mbps -> 约 3.5-5.3 GB，量级要对
    assert 2.0 < size_low < 6.0 and 3.0 < size_high < 8.0
    assert "区间" in br["note"] or "变化" in br["note"]


def test_p7_every_estimate_is_labelled(base_dir):
    """凡是估算值，都必须明确标注为估算。"""
    result = compute(base_dir)
    # 权重体积是实测（没有 note），时长/码率是估算（必须有 note）
    assert "估算" in time_estimate(1, seconds_per_segment=1)["assumptions"]["note"]
    assert result["peak_gb"]["staged"] > 0
    # 计算结果的 JSON 必须可序列化（能落盘给报告用）
    import json

    json.dumps(result, ensure_ascii=False)
