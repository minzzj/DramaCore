"""`continuity_from_prev` 的契约测试。

## 这个键的来历（一次真实的能力缺口发现）

用真实 SmartDirector 跑一次生成的组有 **16 个键**，而 `pack_i2v_group` 只有 11 个。
多出来的 5 个里，`continuity_from_prev` 是 **Director 真的会读**的：

```python
# director/external_groups.py:737-739
row = timeline_row_for_index(timeline, int(src_index))
if not row and isinstance(g, dict):
    row = g                                    # ← 回退到组字典本身
...
continuity_from_prev=resolve_segment_continuity_from_prev(row, segment_index=plan_idx),
```

```python
# director/segment_continuity.py:210
def resolve_segment_continuity_from_prev(seg_data, *, segment_index) -> bool:
    '''Per-segment「引用上段」flag.
    Missing field defaults to True so existing workflows keep pinning every segment.
    Segment index 0 never pins.'''
    if int(segment_index) <= 0:
        return False
    ...
    else:
        return True            # 缺字段 -> True
```

所以缺省行为是「每段都接上一段」。**没有这个键，用户就无法表达
"这一段不要接上一段"**（刻意的硬切、跳新场景）—— 这是真实缺口。

（另外 4 个多出来的键 `segment_count` / `segment_index` / `source_shot_index` /
`source_task`，在 Director 包里 `source_shot_index` 与 `source_task` 命中共 **0** 处，
属 SmartDirector 自己的计划元数据，我的层不需要产出。）

## 设计：可选键，默认不产出

* 默认（不传）-> **不产出该键** -> 与 `pack_*` 逐字段一致（既有契约不破）；
* 显式传 True/False -> 加上该键 -> Director 会读。

这样既保住了"逐字段一致"，又补上了能力。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bettercomfyui_comfy import fusion  # noqa: E402

#: `pack_i2v_group` / `pack_r2v_group` 真实产出的键数（见 test_fusion_contract.py）
I2V_KEYS = 11
R2V_KEYS = 12


# ---------------------------------------------------------------------------
# 1) 默认必须与 pack_* 逐字段一致（不能因为新参数而漂移）
# ---------------------------------------------------------------------------


def test_i2v_default_has_no_continuity_key():
    """默认**不能**有这个键 —— 否则与 pack_i2v_group 不再逐字段一致。"""
    g = fusion.make_i2v_group(prompt="x", duration_sec=5.0)
    assert len(g) == I2V_KEYS, f"i2v 默认键数变了：{len(g)} != {I2V_KEYS}"
    assert "continuity_from_prev" not in g


def test_r2v_default_has_no_continuity_key():
    g = fusion.make_r2v_group(prompt="x", duration_sec=5.0)
    assert len(g) == R2V_KEYS, f"r2v 默认键数变了：{len(g)} != {R2V_KEYS}"
    assert "continuity_from_prev" not in g


def test_explicit_none_is_same_as_default():
    """显式传 None 与不传必须等价（避免"传了个 None 结果多出个键"）。"""
    a = fusion.make_i2v_group(prompt="x", duration_sec=5.0)
    b = fusion.make_i2v_group(prompt="x", duration_sec=5.0, continuity_from_prev=None)
    assert set(a) == set(b)
    assert len(b) == I2V_KEYS


# ---------------------------------------------------------------------------
# 2) 显式设置时真的加上该键，且取值正确
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [True, False])
def test_i2v_explicit_continuity_adds_exactly_one_key(value):
    g = fusion.make_i2v_group(prompt="x", duration_sec=5.0, continuity_from_prev=value)
    assert len(g) == I2V_KEYS + 1, f"应只多一个键，实际 {len(g)}"
    assert g["continuity_from_prev"] is value


@pytest.mark.parametrize("value", [True, False])
def test_r2v_explicit_continuity_adds_exactly_one_key(value):
    g = fusion.make_r2v_group(prompt="x", duration_sec=5.0, continuity_from_prev=value)
    assert len(g) == R2V_KEYS + 1, f"应只多一个键，实际 {len(g)}"
    assert g["continuity_from_prev"] is value


def test_adding_continuity_does_not_disturb_other_keys():
    """加键不能顺手改别的键 —— 用"去掉该键后应完全相等"来断言。"""
    base = fusion.make_i2v_group(prompt="p", duration_sec=5.0)
    withc = fusion.make_i2v_group(prompt="p", duration_sec=5.0, continuity_from_prev=False)
    stripped = {k: v for k, v in withc.items() if k != "continuity_from_prev"}
    assert stripped == base


# ---------------------------------------------------------------------------
# 3) 取值必须是真布尔（Director 侧用 _truthy_continuity_flag 解析）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["yes", 1, 0, "false", [], {}])
def test_non_bool_continuity_is_rejected(bad):
    """非布尔必须报错。

    传字符串 `"false"` 是个典型陷阱：Director 侧 `_truthy_continuity_flag`
    对非空字符串会判为真 —— "false" 会被当成 True["开启接续"]，与用户意图相反。
    所以在这里直接拒绝，让用户写 bool。
    """
    with pytest.raises(fusion.FusionError, match="continuity_from_prev"):
        fusion.make_i2v_group(prompt="x", duration_sec=5.0, continuity_from_prev=bad)
    with pytest.raises(fusion.FusionError, match="continuity_from_prev"):
        fusion.make_r2v_group(prompt="x", duration_sec=5.0, continuity_from_prev=bad)


def test_bool_is_accepted_even_though_bool_is_int_subclass():
    """`True`/`False` 是 `int` 的子类 —— 不能因此把它们误判成非法。

    （写 `isinstance(v, int)` 之类的宽松校验会漏掉这层，
     而写 `type(v) is not bool` 才是正确判据。）
    """
    for v in (True, False):
        g = fusion.make_i2v_group(prompt="x", duration_sec=5.0, continuity_from_prev=v)
        assert g["continuity_from_prev"] is v


# ---------------------------------------------------------------------------
# 4) 与 pack_* 的关系：我多的那个键必须是 Director 真会读的
# ---------------------------------------------------------------------------


def test_continuity_key_matches_what_director_reads():
    """键名必须与 Director 读的名字**逐字一致**。

    `resolve_segment_continuity_from_prev` 接受两种拼写：
    `continuityFromPrev`（驼峰，给 UI 用）与 `continuity_from_prev`（蛇形，给代码用）。
    我们产出蛇形，与 pack_* 及内部字段风格一致。
    """
    g = fusion.make_r2v_group(prompt="x", duration_sec=5.0, continuity_from_prev=False)
    assert "continuity_from_prev" in g
    assert "continuityFromPrev" not in g, "不该同时产出两种拼写（会造成歧义）"
