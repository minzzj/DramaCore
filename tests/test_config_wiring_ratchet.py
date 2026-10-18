"""配置项「配了不用」的棘轮守卫。

## 为什么要有这个文件

ROUND18 审计 SG2 时发现：`per_class_limit`（每类上限 9）配置里有、边界也校验了，
但**没有任何业务代码读它** —— 实测连续造出 12 个同类素材，无一被拦。
同理 `assets.resolution`（素材统一分辨率）也从未被读取。

这类问题形态完全一样：**配置/字段/函数都在，就是没接上执行点**。
靠人肉一轮轮审计太慢，修好一批还会再长出新的。所以做一条**棘轮**：
把当前已知未接线项登记下来，测试断言**没有新增**。

> 棘轮不假装现状是好的 —— 它只保证**债务不再增长**。

## 判定口径（两种自动判据都有偏差，所以选"宁可多报"的那种）

| 判据 | 偏差方向 | 例子 |
|---|---|---|
| **裸字段名** | 假阳性（把无关同名**误判已接线**，会掩盖缺口） | `assets.resolution` 被 `cfg.director.resolution` 命中 |
| **完整路径** `.group.field` | 假阴性（中间变量读不到，夸大缺口） | `p = cfg.director; p.tail_frames` |

这里用**裸词**（宁可多报，不漏报），再由人逐条定性。
定性结果就是 `KNOWN_UNWIRED`：每条要么是**真缺口**、要么是**设计上由别层承担/行为已硬编码**。

## 排除项

`config.py`（只校验边界/解析枚举）、`entities.py`（字段定义）、
`gap_analysis.py`（清单本身）—— 都不算"产品代码使用"。
"""

from __future__ import annotations

import dataclasses
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bettercomfyui.models.entities import Config  # noqa: E402

PRODUCT_DIRS = (REPO_ROOT / "bettercomfyui", REPO_ROOT / "bettercomfyui_comfy")

#: 只排除**这几个具体路径**，不按文件名排除。
#:
#: ⚠️ 踩过的坑：第一版用文件名 `{"config.py"}` 排除，
#: 于是 `bettercomfyui/review/config.py` 也被排除了 ——
#: 而 `score_dimensions` 正是在那个文件里被读的，结果被误报成「未接线」。
#: **按文件名排除太宽**，同名的分层配置文件会被一并漏掉。
SKIP_PATHS = {
    REPO_ROOT / "bettercomfyui" / "config.py",           # 全局配置装配/校验
    REPO_ROOT / "bettercomfyui" / "models" / "entities.py",  # 字段定义
    REPO_ROOT / "bettercomfyui" / "gap_analysis.py",       # 覆盖盘点本身
}

#: 当前已知「产品代码零引用」的配置项 -> 定性原因（人工逐条确认过）。
#:
#: ⚠️ 这是**欠债清单**，不是"可接受清单"。
#: 能接上就接上并删条目；新加配置项若确实由别层承担，必须在此登记原因。
KNOWN_UNWIRED: dict[str, str] = {
    # ---- 设计上由别层承担 ----
    # ROUND27：assets.sampler / assets.batch_count 已由「素材生成·取任务」
    # （BetterComfyUIAssetGen）真实读取并输出到生成参数 JSON，故从本清单移除。
    # ---- 行为已硬编码，开关没接 ----
    "asset_review.retry_interval": "重试间隔固定为「立即」；开关未接",
    "video_review.regenerate_granularity": "硬编码为「只重做该段」；开关未接",
    "global_.checkpoint_every_stage": "检查点无条件保存；开关未接（行为本身符合要求）",
    "prompt.normalize_names": "名称归一化**行为**处处在用（normalize_name），但开关本身未接",
    # ---- 真缺口（未修）----
    # 已修并移除（ROUND19）：
    #   assets.per_class_limit、assets.resolution、encode.audio_join、
    #   global_.log_level、global_.log_all_manual_and_auto_switch、
    #   global_.config_import_export、asset_review.score_dimensions（见 KNOWN_UNDETECTABLE 说明）
    # ROUND20 已接线并移除：
    #   director.save_vram_all（VramPhaseScheduler.from_config）
    #   global_.task_timeout_s（SegmentGenerator.rework_timeout_s + 轮询看门狗）
    #   global_.comfyui_endpoint（resolve_comfyui_endpoint + CLI doctor HTTP 探测）
}

#: **已知漏报**：确实是「配了不用」，但裸词扫描**检测不到**。
#:
#: 两类原因，都只能靠人肉确认：
#:
#: 1. **跨组同名** —— 字段名与别的组的字段重名，裸词命中率不为 0；
#: 2. **校验式读取** —— 分层 `config.py` 里读过它来做范围校验，
#:    扫描认为「已接线」，但业务逻辑从没消费过。
#:
#: （`test_known_list_is_not_stale` 会把它们排除在外 ——
#:  否则反向守卫会误报"已经接线了，请删掉"。）
KNOWN_UNDETECTABLE: dict[str, str] = {
    "asset_review.score_dimensions": (
        "**【真缺口】**评分维度实际不可配：它只在 `review/config.py` 的 "
        "`resolve_review_params` 里被**校验**过，但 `AssetReviewer` 实际只接收单一 "
        "`score` 值，四维（画质/一致性/合规/构图）没有参与打分组织。"
        "扫描漏报原因：校验式读取被算成「已接线」。"
    ),
    "asset_review.on_timeout": (
        "行为硬编码为「超时切模型」且恰好等于默认配置值；开关本身无效。"
        "扫描漏报原因：`review/config.py` 里读过它做取值校验。"
    ),
    "asset_review.new_version_requires_review": (
        "「新版本必重审」由结构保证（新版本必进 PENDING_REVIEW），开关冗余。"
        "扫描漏报原因：`review/config.py` 里读过它做取值校验。"
    ),
}


def _config_fields() -> dict[str, str]:
    """返回 `{group.field: field}`。"""
    out: dict[str, str] = {}
    for gf in dataclasses.fields(Config):
        gobj = gf.default_factory()
        if not dataclasses.is_dataclass(gobj):
            continue
        for f in dataclasses.fields(gobj):
            out[f"{gf.name}.{f.name}"] = f.name
    return out


def _product_blob() -> tuple[str, int]:
    chunks: list[str] = []
    n = 0
    for d in PRODUCT_DIRS:
        for p in d.rglob("*.py"):
            if p.resolve() in SKIP_PATHS:
                continue
            chunks.append(p.read_text(encoding="utf-8"))
            n += 1
    return "\n".join(chunks), n


def _candidates() -> set[str]:
    """裸词零命中 = 候选未接线。"""
    blob, files = _product_blob()
    assert files > 40, f"只扫到 {files} 个产品文件，扫描口径可能退化"
    out: set[str] = set()
    for key, name in _config_fields().items():
        if not re.search(r"\b" + re.escape(name) + r"\b", blob):
            out.add(key)
    return out


# ---------------------------------------------------------------------------
# 棘轮
# ---------------------------------------------------------------------------


def test_no_new_unwired_config_fields():
    """**不允许新增**「配了不用」的配置项。

    红了说明：新加的配置项在产品代码里零引用。
    正解：① 真的接上执行点；② 若由别层承担/行为已硬编码，在
    `KNOWN_UNWIRED` 里登记原因。
    """
    new_ones = sorted(_candidates() - set(KNOWN_UNWIRED))
    assert not new_ones, (
        "发现**新的**「配了不用」配置项（产品代码零引用）：\n  "
        + "\n  ".join(new_ones)
        + "\n\n正解：① 接上执行点；② 在 tests/test_config_wiring_ratchet.py 的"
          " KNOWN_UNWIRED 里登记原因。\n"
          "（扫描：python scripts/scan_config_wiring.py）"
    )


def test_known_list_is_not_stale():
    """反向守卫：已接线的条目必须从清单删掉，避免清单变成垃圾场。

    这条保证 `KNOWN_UNWIRED` 只会**缩小**，不会留着过时信息骗人。
    """
    stale = sorted(set(KNOWN_UNWIRED) - _candidates())
    assert not stale, (
        "以下配置项**已经有产品代码引用了**，请从 KNOWN_UNWIRED 删掉：\n  "
        + "\n  ".join(stale)
    )


def test_every_known_entry_has_a_reason():
    """清单每条都必须写原因 —— 只列名字等于没解释。"""
    empty = [k for k, v in KNOWN_UNWIRED.items() if not (v or "").strip()]
    assert not empty, f"以下条目缺少原因说明：{empty}"


def test_scan_scope_is_intact():
    """扫描口径自检：字段数与分组必须完整，防止扫描悄悄失效。"""
    fields = _config_fields()
    assert len(fields) >= 60, f"只扫到 {len(fields)} 个配置字段"
    groups = {k.split(".", 1)[0] for k in fields}
    assert groups == {"prompt", "assets", "asset_review", "director",
                      "video_review", "encode", "global_"}, groups


def test_real_gaps_are_marked_as_gaps():
    """真缺口必须在原因里明确标注，不能混进「设计如此」里被淡化。

    这条**不锁数量** —— 修好一个缺口就该从清单删掉，数量本来会变小。
    它只保证两件事：
    1. 标注 `**【真缺口】**` 的条目必须都还在清单里（没被偷偷改名成设计如此）；
    2. 只要还有未修的缺口，就必须至少有一条带标记 ——
       防止有人一口气把标记全删掉、让欠债看起来像设计。
    """
    real_gaps = {k for k, v in KNOWN_UNWIRED.items() if "真缺口" in v}
    real_gaps |= {k for k, v in KNOWN_UNDETECTABLE.items() if "真缺口" in v}

    # ① 标记了缺口的条目必须仍在 Known 清单里（不是幽灵条目）
    assert real_gaps <= (set(KNOWN_UNWIRED) | set(KNOWN_UNDETECTABLE)), (
        f"有真缺口条目不在清单里：{real_gaps - (set(KNOWN_UNWIRED) | set(KNOWN_UNDETECTABLE))}"
    )

    # ② 每条标记了缺口的都必须有原因说明
    for k in real_gaps:
        reason = KNOWN_UNWIRED.get(k) or KNOWN_UNDETECTABLE.get(k) or ""
        assert len(reason) > 10, f"{k} 的缺口说明太短：{reason!r}"

    # ③ 若还有"行为已硬编码/设计如此"之外的未接线项，至少要标出一个真缺口
    unfixed = set(KNOWN_UNWIRED) | set(KNOWN_UNDETECTABLE)
    if unfixed:
        assert real_gaps, (
            "清单里还有未接线项，却一条「真缺口」都没标 —— 可能有人淡化了定性"
        )


def test_undetectable_list_documents_why():
    """「已知漏报」每条必须写清**为什么扫描检测不到**，否则无法判断可信度。"""
    for key, reason in KNOWN_UNDETECTABLE.items():
        assert "重名" in reason or "漏报" in reason, (
            f"{key} 的说明里没有解释漏报原因：{reason!r}"
        )


def test_undetectable_entries_are_really_undetectable():
    """交叉校验：「已知漏报」里的项，扫描结果必须**确实检测不到**。

    如果哪天它被扫描检测到了，说明可以从 `KNOWN_UNDETECTABLE`
    移到 `KNOWN_UNWIRED`（或已经修好了）。
    """
    cands = _candidates()
    wrongly = sorted(set(KNOWN_UNDETECTABLE) & cands)
    assert not wrongly, (
        f"以下项已被扫描检测到，请从 KNOWN_UNDETECTABLE 移到 KNOWN_UNWIRED：{wrongly}"
    )
