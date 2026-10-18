"""融合体检的**运行时契约比对**（`_contract_selfcheck`）测试。

## 这条检查曾经是个假守卫

第一版实现里写着：

```python
want_i2v = {"version", "family", "kind", ...}   # 手抄的字段清单
real_i2v = set(pack_i2v_group(prompt="x").keys())
assert real_i2v == want_i2v
```

它比对的是「**手抄清单** vs 真实包」——
也就是说，**我要是把 builder 改坏了，这条检查依然是绿的**。
只证明了我抄得对，没证明我的产出对。

现在改为与 `fusion.make_i2v_group()` / `fusion.make_r2v_group()` 的**真实输出**比。
本文件钉住这个语义，并覆盖包定位逻辑。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _doctor():
    from bettercomfyui_comfy.fusion_nodes import BetterComfyUIFusionDoctor

    return BetterComfyUIFusionDoctor


# ---------------------------------------------------------------------------
# 包定位：必须优先干净包名（确定性）
# ---------------------------------------------------------------------------


def test_finder_prefers_clean_package_name(monkeypatch):
    """`sys.modules` 里有多个候选键时必须优先干净包名。

    ComfyUI 对目录型 custom node 的注册键是「完整路径把 `.` 换成 `_x_`」，
    所以本机同时存在干净名与路径式键（ChainDirector 的 `_autopatch.py` 佐证了这点）。
    按遍历顺序取第一个是不确定的 —— 报告里会时而打印这个键、时而那个键。
    """
    fake_clean = types.ModuleType("ComfyUI_MiniMaxH3_Director")
    fake_clean.__path__ = ["/fake/clean"]
    fake_path = types.ModuleType(r"H:\x\_x_comfyui\custom_nodes\ComfyUI_MiniMaxH3_Director")
    fake_path.__path__ = ["/fake/path"]

    # 故意让路径式键**排在前面**，验证实现不是"取第一个"
    monkeypatch.setitem(sys.modules,
                        r"H:\x\_x_comfyui\custom_nodes\ComfyUI_MiniMaxH3_Director", fake_path)
    monkeypatch.setitem(sys.modules, "ComfyUI_MiniMaxH3_Director", fake_clean)

    chosen, candidates = _doctor()._find_director_package()
    assert chosen == "ComfyUI_MiniMaxH3_Director", (
        f"没有优先干净包名，拿到了 {chosen!r}"
    )
    assert len(candidates) >= 2, f"应报告全部候选，实际 {candidates}"


def test_finder_returns_none_when_absent(monkeypatch):
    """找不到时必须返回 (None, ...) —— 调用方据此如实报「未验证」。"""
    for name in list(sys.modules):
        if "MiniMaxH3_Director" in name:
            monkeypatch.delitem(sys.modules, name, raising=False)
    chosen, candidates = _doctor()._find_director_package()
    assert chosen is None
    assert candidates == []


# ---------------------------------------------------------------------------
# 自检语义：与**我构造的组**比，而不是与硬编码清单比
# ---------------------------------------------------------------------------


def test_selfcheck_reports_unverified_when_package_absent(monkeypatch):
    """包不在时必须报「未验证」，**不许**报通过。"""
    for name in list(sys.modules):
        if "MiniMaxH3_Director" in name:
            monkeypatch.delitem(sys.modules, name, raising=False)
    text = _doctor()._contract_selfcheck()
    assert "[未验证]" in text, text
    assert "全部通过" not in text, f"包不在却报通过：{text}"


def test_selfcheck_compares_my_builder_not_a_hardcoded_list(monkeypatch):
    """**关键语义**：自检必须比对我**实际构造**的组。

    做法：注入一个假的导演台包，其 `pack_i2v_group` 返回一个与我的 schema
    明显不同的字典。如果实现是拿硬编码清单去比，它会（错误地）通过；
    只有真的比对我的产出，才会报「不一致」。
    """
    pkg_name = "Fake_MiniMaxH3_Director_Loose"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = ["/fake"]
    director = types.ModuleType(f"{pkg_name}.director")
    ext = types.ModuleType(f"{pkg_name}.director.external_groups")

    def pack_i2v_group(*, prompt="", duration_sec=5.0, first_frame=None, last_frame=None):
        # 故意与我的 schema 不同：少 kind、多 bogus
        return {"version": 1, "family": "i2v", "prompt": prompt,
                "duration_sec": duration_sec, "bogus": True}

    def pack_r2v_group(*, prompt="", duration_sec=5.0, ref_image_size="match", **kw):
        # 故意不拒绝空组（真实包会拒绝），用于验证"空组保护一致性"检查也会报错
        return {"version": 1, "family": "r2v", "kind": "r2v", "prompt": prompt,
                "duration_sec": duration_sec, "ref_image_size": ref_image_size,
                "first_frame": None, "last_frame": None, "ref_images": {},
                "ref_videos": {}, "ref_video_audios": {}, "ref_audios": {}}

    ext.pack_i2v_group = pack_i2v_group
    ext.pack_r2v_group = pack_r2v_group
    director.external_groups = ext

    monkeypatch.setitem(sys.modules, pkg_name, pkg)
    monkeypatch.setitem(sys.modules, f"{pkg_name}.director", director)
    monkeypatch.setitem(sys.modules, f"{pkg_name}.director.external_groups", ext)

    text = _doctor()._contract_selfcheck()
    assert "[不一致]" in text, (
        "自检没有发现字段差异 —— 说明它比的是硬编码清单而不是我构造的组：\n" + text
    )
    assert "全部通过" not in text, text


def test_selfcheck_passes_with_a_faithful_fake(monkeypatch):
    """反向断言：当假包与我的 schema 完全一致时应报通过（防止恒红）。"""
    from bettercomfyui_comfy import fusion

    pkg_name = "Fake_MiniMaxH3_Director_Faithful"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = ["/fake"]
    director = types.ModuleType(f"{pkg_name}.director")
    ext = types.ModuleType(f"{pkg_name}.director.external_groups")

    def pack_i2v_group(*, prompt="", duration_sec=5.0, first_frame=None, last_frame=None):
        # 与我的 builder 用同一实现 -> 必然一致
        return dict(fusion.make_i2v_group(prompt=prompt, duration_sec=duration_sec,
                                          first_frame=first_frame, last_frame=last_frame))

    def pack_r2v_group(*, prompt="", duration_sec=5.0, ref_image_size="match", **kw):
        return dict(fusion.make_r2v_group(prompt=prompt, duration_sec=duration_sec,
                                          ref_image_size=ref_image_size, **kw))

    ext.pack_i2v_group = pack_i2v_group
    ext.pack_r2v_group = pack_r2v_group
    director.external_groups = ext

    monkeypatch.setitem(sys.modules, pkg_name, pkg)
    monkeypatch.setitem(sys.modules, f"{pkg_name}.director", director)
    monkeypatch.setitem(sys.modules, f"{pkg_name}.director.external_groups", ext)

    text = _doctor()._contract_selfcheck()
    assert "全部通过" in text, text
    assert "[不一致]" not in text, text


def test_selfcheck_reports_module_file_for_traceability(monkeypatch):
    """报告里要写出 `external_groups` 的**实际文件路径**（便于追查加载了哪一份）。"""
    from bettercomfyui_comfy import fusion

    pkg_name = "Fake_MiniMaxH3_Director_Traced"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = ["/fake"]
    director = types.ModuleType(f"{pkg_name}.director")
    ext = types.ModuleType(f"{pkg_name}.director.external_groups")
    ext.__file__ = "/fake/path/director/external_groups.py"
    ext.pack_i2v_group = lambda **kw: dict(fusion.make_i2v_group(**kw))
    ext.pack_r2v_group = lambda **kw: dict(fusion.make_r2v_group(**kw))
    director.external_groups = ext

    monkeypatch.setitem(sys.modules, pkg_name, pkg)
    monkeypatch.setitem(sys.modules, f"{pkg_name}.director", director)
    monkeypatch.setitem(sys.modules, f"{pkg_name}.director.external_groups", ext)

    text = _doctor()._contract_selfcheck()
    assert "external_groups.py" in text, text


def test_static_ast_check_still_exists_and_is_separate():
    """静态（AST）比对是另一条独立证据，不能被运行时比对取代。

    运行时比对需要 ComfyUI 进程；CI 里跑不了。
    所以 `tests/test_fusion_contract.py` 的 AST 比对必须继续存在。
    """
    p = REPO_ROOT / "tests" / "test_fusion_contract.py"
    assert p.is_file(), "静态契约测试不见了"
    src = p.read_text(encoding="utf-8")
    assert "ast" in src and "pack_r2v_group" in src, "静态比对逻辑缺失"
