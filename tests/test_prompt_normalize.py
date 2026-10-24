"""``prompt/normalize.py`` 测试：纯函数、幂等、全角/半角/空格/繁简/标点。

对应 task-3 硬要求：
* 对 200 个以上随机/边界名称断言 ``normalize(normalize(x)) == normalize(x)``；
* 全角/半角/空格差异的同一名称归一化后必须相等。
"""

from __future__ import annotations

import ast
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pytest

from bettercomfyui.prompt.normalize import (
    is_suspect,
    normalize_name,
    similarity,
    variant_key,
)

NORMALIZE_SRC = (_ROOT / "bettercomfyui" / "prompt" / "normalize.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 名称集合：确定性生成 200+ 个边界名称（不用随机数，保证可复现）
# ---------------------------------------------------------------------------

_BASES = ["林澈", "苏晚", "钢铁侠", "Iron Man", "IronMan", "主角A", "Scene 01", ""]
_PADS = ["", " ", "  ", "\u3000", "\u200b", "\t", "\n", "\u00a0", " \u3000 "]
_FULLWIDTH = ["Ａ", "Ｂ", "１", "２", "，", "！", "：", "（", "）"]
_PUNCT = ["。", "、", "，", ",", "！", "!", "：", ":", "\u201c", "\u201d", "「", "」", "[", "]"]


def _generate_names() -> list[str]:
    names: list[str] = []
    for base in _BASES:
        for left in _PADS:
            for right in _PADS:
                names.append(left + base + right)
    for punct in _PUNCT:
        for base in ("林澈", "钢铁侠"):
            names.append(punct + base + punct)
            names.append(base + punct)
    for ch in _FULLWIDTH:
        names.append("ＡＢ" + ch + "１２")
    names += [
        "",
        " ",
        "\u3000",
        "\u200b\u200b",
        "é",
        "e\u0301",
        "STRASSE",
        "straße",
        "ſ",
        "㈱",
        "\uff21\uff22\uff23",
        "林澈\u200b",
        "林\u3000澈",
        "林  澈",
        "  林澈  ",
        "林澈。",
        "林澈.",
        "鋼鐵俠",
        "１２３",
        "１２３",
    ]
    # 去重但保留顺序（数量 >= 200）
    seen: set[str] = set()
    unique: list[str] = []
    for item in names:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


NAMES = _generate_names()


# ---------------------------------------------------------------------------
# 幂等 / 纯函数
# ---------------------------------------------------------------------------


def test_name_set_is_large_enough():
    assert len(NAMES) >= 200, f"边界名称只有 {len(NAMES)} 个，不足以覆盖幂等性要求"


def test_normalize_is_idempotent_over_all_names():
    for name in NAMES:
        once = normalize_name(name)
        twice = normalize_name(once)
        assert twice == once, f"归一化不幂等：{name!r} -> {once!r} -> {twice!r}"


def test_normalize_is_deterministic():
    for name in NAMES:
        assert normalize_name(name) == normalize_name(name)


def test_normalize_returns_str_and_never_raises_on_str():
    for name in NAMES:
        assert isinstance(normalize_name(name), str)


def test_normalize_rejects_non_str():
    for bad in (None, 123, b"abc", ["a"], {"a": 1}):
        with pytest.raises(TypeError):
            normalize_name(bad)


def test_normalize_module_has_no_io_side_effects():
    """纯函数检查：源码里不得出现 open/网络/时间/随机。"""
    tree = ast.parse(NORMALIZE_SRC)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "unicodedata", "difflib", "typing", "re"}, imported
    for banned in ("open(", "requests.", "socket.", "time.time", "random."):
        assert banned not in NORMALIZE_SRC, f"归一化模块出现了非纯函数调用：{banned}"


# ---------------------------------------------------------------------------
# 全角/半角/空格/大小写/标点等价性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "left,right",
    [
        ("Iron Man", "ironman"),
        ("IronMan", "IRON  MAN"),
        ("林澈", "  林 澈 "),
        ("林澈", "林\u3000澈"),
        ("林澈", "林\u200b澈"),
        ("林澈", "林\n澈"),
        ("林澈", "\u3000林澈\u3000"),
        ("ABCD", "ＡＢＣＤ"),
        ("abc123", "ａｂｃ１２３"),
        ("角色:林澈", "角色：林澈"),
        ("【林澈】", "［林澈］"),
        ("é", "e\u0301"),
    ],
)
def test_equivalent_names_normalize_equal(left, right):
    """只有「空格/大小写/全半角」差异在归一化后相等。

    注意两处**刻意不等价**（属于疑似重复，由 ``is_suspect`` 处理，见下）：
    繁简差异、仅差标点差异 —— 归一化必须保留这些信息，否则无法判为疑似。
    """
    assert normalize_name(left) == normalize_name(right)


@pytest.mark.parametrize(
    "left,right",
    [
        ("林澈", "苏晚"),
        ("钢铁侠", "钢铁侠2"),
        ("Scene01", "Scene02"),
        ("abc", "abcd"),
    ],
)
def test_different_names_stay_different(left, right):
    assert normalize_name(left) != normalize_name(right)


def test_internal_spaces_are_removed_per_plan():
    """方案「去空格」要求内部空格也删除（因此仅差空格 = 精确匹配）。"""
    assert normalize_name("Iron Man") == "ironman"


def test_punctuation_is_unified_to_halfwidth():
    assert normalize_name("【林澈】") == "[林澈]"
    assert normalize_name("《序章》") == "<序章>"
    assert normalize_name("甲。乙") == "甲.乙"
    assert normalize_name("甲、乙") == "甲,乙"


# ---------------------------------------------------------------------------
# is_suspect / variant_key / similarity
# ---------------------------------------------------------------------------


def test_variant_key_is_idempotent():
    for name in NAMES:
        once = variant_key(name)
        assert variant_key(once) == once


def test_is_suspect_flags_variant_differences():
    assert is_suspect("林澈。", ["林澈"])
    assert is_suspect("林澈二号", ["林澈2"])
    assert is_suspect("鋼鐵俠", ["钢铁侠"])
    assert is_suspect("钢铁侠（青年）", ["钢铁侠青年"])


def test_is_suspect_false_for_exact_and_unrelated():
    assert not is_suspect("林澈", ["林澈"]), "归一化相等属于精确匹配，不是疑似"
    assert not is_suspect("林澈", ["苏晚"])
    assert not is_suspect("林澈", [])
    assert not is_suspect("", ["林澈"])


def test_is_suspect_accepts_dicts_and_rejects_non_str_candidate():
    assert is_suspect("林澈。", [{"name": "林澈"}] * 1)
    with pytest.raises(TypeError):
        is_suspect(123, ["林澈"])


def test_similarity_is_bounded_and_deterministic():
    assert similarity("林澈", "林澈") == 1.0
    assert 0.0 <= similarity("林澈", "苏晚") <= 1.0
    assert similarity("abc", "") == 0.0
    assert similarity("", "") == 1.0
    assert similarity("IronMan", "IronMen") > 0.8


# ---------------------------------------------------------------------------
# 反向自检：故意破坏实现必须让测试变红（见 tests/test_prompt_selftest_break.py）
# ---------------------------------------------------------------------------


def test_whitespace_set_covers_plan_mandated_characters():
    """U+3000 全角空格是方案点名的必须处理项。"""
    assert "\u3000" not in normalize_name("林\u3000澈")
    assert "\u3000" not in normalize_name("\u3000")
