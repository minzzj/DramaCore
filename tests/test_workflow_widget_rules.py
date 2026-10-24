"""工作流生成器的**控件分类**与 UI→API 转换测试（不需要 ComfyUI）。

## 为什么这个文件重要

工作流的 `widgets_values` 是**按位置**对应控件的。
分类错一个输入（把连线口当成控件、或反过来），**该节点之后的所有参数整体错位** ——
不报错、不崩溃，只是值悄悄填到了别的参数上。这是最难查的一类问题。

本文件钉住两个**真实事故**：

* **事故 1**：手维护 `link_types` 清单，把 `BDGROUP` 误当成连线类型。
  它真实声明是 `["BDGROUP", {"default": "采样设置"}]` —— 是个 COMBO 控件。
* **事故 2**：用 `len(t) > 3` 判断"是不是类型名"。`"VAE"` 长度正好是 **3**，
  于是 `video_vae` / `audio_vae` 被当成控件，又一次错位。

两次都表现为提交 ComfyUI 时报
`Value 0.0 smaller than min of 0.2` 或 `Required input is missing: bd_grp_sample`。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WF_DIR = REPO_ROOT / "bettercomfyui_comfy" / "workflows"
sys.path.insert(0, str(WF_DIR))

from build_fusion_workflow import default_for, widget_slots  # noqa: E402


# ---------------------------------------------------------------------------
# 分类规则
# ---------------------------------------------------------------------------


def test_bare_short_type_name_is_a_link_not_a_widget():
    """`"VAE"` 只有 3 个字符，但它是**连线口**。

    事故 2 的守卫：早期用 `len(t) > 3` 判断类型名，
    `"VAE"` 恰好不满足，被当成控件 —— 于是所有参数错位。
    """
    schema = {"input": {"required": {
        "video_vae": ["VAE", {"tooltip": "video vae"}],
    }}}
    slots = widget_slots(schema)
    assert slots == [], f"VAE 是连线口，不该出现在控件里：{slots}"


@pytest.mark.parametrize("type_name", ["VAE", "MODEL", "CLIP", "IMAGE", "AUDIO",
                                       "MMX_DIR_GROUP", "LATENT"])
def test_all_link_types_are_excluded_regardless_of_name_length(type_name):
    """所有裸类型名都算连线口，**与长度无关**（3 字符到 13 字符都覆盖）。"""
    schema = {"input": {"required": {f"端口": [type_name, {"tooltip": "x"}]}}}
    assert widget_slots(schema) == [], f"{type_name} 被误判成控件"


def test_combo_with_options_is_a_widget():
    """`[[取值...], {...}]` 是 COMBO 控件。"""
    schema = {"input": {"required": {
        "task_type": [["t2v", "i2v"], {"default": "t2v"}],
    }}}
    slots = widget_slots(schema)
    assert len(slots) == 1
    assert slots[0][0] == "task_type"
    assert slots[0][1]["kind"] == "combo"


def test_bdgroup_with_default_is_a_widget_not_a_link():
    """**事故 1 的守卫**：`["BDGROUP", {"default": ...}]` 是 COMBO 控件。

    它看起来像个自定义类型名（全大写、13 字符），但带 `default` 就说明是控件。
    早期把它当连线口跳过，导致该节点之后所有控件位置前移一格。
    """
    schema = {"input": {"required": {
        "bd_grp_sample": ["BDGROUP", {"default": "采样设置"}],
    }}}
    slots = widget_slots(schema)
    assert len(slots) == 1, f"BDGROUP 带 default 应是控件，实际 {slots}"
    assert slots[0][0] == "bd_grp_sample"
    # 关键：default 必须被取出来，否则会回落到 0 / False
    assert slots[0][1]["opts"].get("default") == "采样设置"


def test_scalar_types_are_widgets():
    schema = {"input": {"required": {
        "cfg": ["FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0}],
        "seed": ["INT", {"default": 0}],
        "prompt": ["STRING", {"default": "", "multiline": True}],
        "flag": ["BOOLEAN", {"default": False}],
    }}}
    names = [n for n, _ in widget_slots(schema)]
    assert names == ["cfg", "seed", "prompt", "flag"]


def test_widget_order_follows_required_then_optional():
    """顺序必须是 required 后 optional，且保持声明顺序（位置即语义）。"""
    schema = {"input": {
        "required": {"a": ["INT", {"default": 1}], "b": ["FLOAT", {"default": 2.0}]},
        "optional": {"c": ["STRING", {"default": ""}]},
    }}
    assert [n for n, _ in widget_slots(schema)] == ["a", "b", "c"]


def test_link_and_widget_interleaved_keeps_widget_positions():
    """**最关键的一条**：连线口夹在控件之间时，控件位置不能被它挤偏。

    这正是两次事故的实质 —— 少算一个连线口或把控件当连线口，
    后面的控件全部前移。
    """
    schema = {"input": {"required": {
        "model": ["MODEL", {"tooltip": "x"}],      # 连线口
        "cfg": ["FLOAT", {"default": 1.0}],        # 控件 [0]
        "bd_grp_sample": ["BDGROUP", {"default": "采样设置"}],  # 控件 [1]
        "seed": ["INT", {"default": 0}],           # 控件 [2]
        "video_vae": ["VAE", {"tooltip": "y"}],    # 连线口
        "width": ["INT", {"default": 864}],        # 控件 [3]
    }}}
    slots = widget_slots(schema)
    assert [n for n, _ in slots] == ["cfg", "bd_grp_sample", "seed", "width"], slots


# ---------------------------------------------------------------------------
# 默认值：必须从 opts 里取，不能回落
# ---------------------------------------------------------------------------


def test_default_for_reads_schema_default():
    meta = {"kind": "widget", "type": "FLOAT", "opts": {"default": 5.0, "min": 0.2}}
    assert default_for("默认时长", meta, {}) == 5.0


def test_default_for_prefers_override():
    meta = {"kind": "widget", "type": "FLOAT", "opts": {"default": 5.0}}
    assert default_for("默认时长", meta, {"默认时长": 12.0}) == 12.0


def test_default_for_falls_back_by_type_when_no_default():
    """没有 default 时才按类型回落；这条用于确认回落**不是**首选路径。"""
    assert default_for("x", {"kind": "widget", "type": "INT", "opts": {}}, {}) == 0
    assert default_for("x", {"kind": "widget", "type": "FLOAT", "opts": {}}, {}) == 0.0
    assert default_for("x", {"kind": "widget", "type": "STRING", "opts": {}}, {}) == ""
    assert default_for("x", {"kind": "widget", "type": "BOOLEAN", "opts": {}}, {}) is False


def test_float_widget_with_min_but_no_default_does_not_violate_min():
    """回归：`默认时长` 曾因拿不到 default 而回落成 0.0，低于 min=0.2。

    这里断言"有 default 就一定用 default"，从根上避免越界。
    """
    meta = {"kind": "widget", "type": "FLOAT",
            "opts": {"default": 5.0, "min": 0.2, "max": 120.0}}
    value = default_for("默认时长", meta, {})
    assert value >= meta["opts"]["min"], f"{value} 低于 min={meta['opts']['min']}"


# ---------------------------------------------------------------------------
# 生成器与校验器必须同规则
# ---------------------------------------------------------------------------


def test_validator_and_builder_share_the_same_rule():
    """`validate_fusion_workflow.widget_count` 必须与 `widget_slots` 计数一致。

    两边各算一套的话，校验只会给出假安心。
    这条用一批**真实形状**的声明同时喂给两个实现。
    """
    import inspect

    sys.path.insert(0, str(WF_DIR))
    import validate_fusion_workflow as V

    schemas = [
        {"input": {"required": {"model": ["MODEL", {"tooltip": "x"}]}}},
        {"input": {"required": {"video_vae": ["VAE", {"tooltip": "x"}]}}},
        {"input": {"required": {"bd_grp_sample": ["BDGROUP", {"default": "采样设置"}]}}},
        {"input": {"required": {"cfg": ["FLOAT", {"default": 1.0, "min": 0.0}]}}},
        {"input": {"required": {"task_type": [["a", "b"], {"default": "a"}]}}},
        {"input": {"required": {"prompt": ["STRING", {"default": "", "multiline": True}]},
                  "optional": {"mode": [["x"], {"default": "x"}]}}},
        {"input": {"required": {
            "model": ["MODEL", {"tooltip": "x"}],
            "cfg": ["FLOAT", {"default": 1.0}],
            "video_vae": ["VAE", {"tooltip": "y"}],
            "width": ["INT", {"default": 864}]}}},
    ]
    for i, s in enumerate(schemas):
        mine = len(widget_slots(s))
        theirs = V.widget_count(s)
        assert mine == theirs, (
            f"第 {i} 个 schema 两边算法不一致：widget_slots={mine}，widget_count={theirs}\n"
            f"schema={s}"
        )
    # 也确认校验器的实现不含脆弱的长度判断。
    # ⚠️ 必须**剥离 docstring 再扫**：docstring 里正好在讲"早期用了 len(t) > 3"，
    # 直接 grep 源码会误报（第一版就踩了这个）。
    import ast
    import textwrap

    src = textwrap.dedent(inspect.getsource(V.widget_count))
    tree = ast.parse(src)
    fn = tree.body[0]
    if (isinstance(fn, ast.FunctionDef) and fn.body
            and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        code_lines = src.splitlines()
        # 去掉 docstring 所在行区间
        ds = fn.body[0]
        body_src = "\n".join(code_lines[ds.end_lineno:])
    else:
        body_src = src
    assert "len(t) > 3" not in body_src, (
        "校验器里仍有基于长度的类型判断（docstring 以外）\n" + body_src
    )
