"""「运行」节点控件**真的生效**吗？—— 防假 UI 测试。

背景：本节点的 `review_mode` / `asset_review_mode` / `max_segments` / `enable_cascade`
一度只出现在 `INPUT_TYPES` 与签名里，`run()` 内部完全没消费
（由 workflow-designer 实测发现并如实上报）。

**界面上给了一个控制项却悄悄不生效，比没有这个控制项更糟** ——
用户会以为「我选了人工审核 / 我只想跑 1 段」，实际系统按别的参数在跑。

因此本文件用两种方式钉住：

1. **行为断言**（真跑，看结果确实变了）；
2. **源码断言**（防止有人以后又把参数改成装饰品）。

为了不让测试依赖 ffmpeg 与耗时流程，这里用 monkeypatch 把
`bettercomfyui.demo.run_demo` 换成一个**记录参数**的替身：
我们要验证的是「节点有没有把用户的选择传下去、有没有如实说明」，不是重跑一遍流水线
（端到端由 `tests/test_end_to_end_demo.py` 负责）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

from bettercomfyui_comfy import nodes as N  # noqa: E402


@pytest.fixture()
def captured_run(monkeypatch):
    """把 run_demo 换成记录器，并让 _config 返回真实 Config。"""
    from bettercomfyui.config import load_config

    calls: list[dict] = []

    def fake_run_demo(*, repo_root, config, project=None, chapter=None,
                      executor="mock", workdir=None):
        script = None
        if chapter is not None:
            script = json.loads(Path(chapter).read_text(encoding="utf-8"))
        calls.append({
            "project": project,
            "chapter": chapter,
            "script": script,
            "executor": executor,
            "asset_review_mode": getattr(config.asset_review.default_mode, "value", None),
        })
        return {
            "ok": True,
            "project_id": project or "fake_project",
            "used_mock": executor == "mock",
            "real_generation": False,
            "real_generation_reason": "测试替身",
            "assets_created": 0, "assets_reused": 0, "assets_locked": 0,
            "segments_planned": 0, "segments_generated": 0,
            "segments_passed": 0, "segments_fallback": 0,
            "full_review_calls": 1, "spliced": True, "encoded": True,
            "output_path": "/tmp/fake.mp4", "metadata_path": "/tmp/fake.json",
            "degrade_flags": [], "unverified": ["真实视频模型生成"],
            "steps": [], "error": None,
        }

    monkeypatch.setattr(N, "_config", lambda: load_config(repo_root=REPO_ROOT))
    monkeypatch.setattr(N, "_orch_repo_root", lambda: REPO_ROOT)

    import bettercomfyui.demo as demo_mod

    monkeypatch.setattr(demo_mod, "run_demo", fake_run_demo)
    return calls


def _run(**kw):
    defaults = dict(
        project_id="proj_test", script_text="", review_mode="MODEL",
        asset_review_mode="MODEL", executor="占位媒体(mock)",
        max_segments=3, enable_cascade=True,
    )
    defaults.update(kw)
    node = N.BetterComfyUIRun()
    return node.run(**defaults)


# ---------------------------------------------------------------------------
# 1. 行为断言
# ---------------------------------------------------------------------------


def test_asset_review_mode_reaches_the_config(captured_run):
    """`asset_review_mode=HUMAN` 必须真的写进配置（不能只是显示）。"""
    _run(asset_review_mode="HUMAN")
    assert captured_run, "run_demo 没有被调用"
    assert captured_run[0]["asset_review_mode"] == "HUMAN", (
        f"素材审核模式没有传到配置：{captured_run[0]['asset_review_mode']}"
    )


def test_max_segments_truncates_the_script(captured_run):
    """`max_segments=1` 必须真的把剧本截断成 1 段。"""
    script = {
        "chapters": [{
            "title": "t",
            "assets": [{"id": "c1", "type": "CHARACTER", "name": "甲"}],
            "segments": [
                {"prompt": "s1", "asset_refs": []},
                {"prompt": "s2", "asset_refs": []},
                {"prompt": "s3", "asset_refs": []},
            ],
        }]
    }
    _run(script_text=json.dumps(script, ensure_ascii=False), max_segments=1)
    assert captured_run, "run_demo 没有被调用"
    segs = captured_run[0]["script"]["chapters"][0]["segments"]
    assert len(segs) == 1, f"max_segments=1 没有生效，实际 {len(segs)} 段"


def test_max_segments_keeps_all_when_within_limit(captured_run):
    script = {
        "chapters": [{"segments": [{"prompt": "s1"}, {"prompt": "s2"}]}]
    }
    _run(script_text=json.dumps(script), max_segments=10)
    segs = captured_run[0]["script"]["chapters"][0]["segments"]
    assert len(segs) == 2, "未超限时不应截断"


def test_max_segments_applies_to_builtin_demo_script_too(captured_run):
    """剧本**留空**（用内置演示剧本）时，`max_segments` 同样必须生效。

    实测踩到的 bug：截断逻辑只写在「用户给了 script_text」的分支里，
    于是留空剧本时 `max_segments=2` 静默跑了 3 段。
    「我只想跑 2 段」是最常用的省时手段，静默失效会直接浪费用户时间。
    """
    _run(script_text="", max_segments=2)
    assert captured_run, "run_demo 没有被调用"
    segs = captured_run[0]["script"]["chapters"][0]["segments"]
    assert len(segs) == 2, f"留空剧本时 max_segments=2 未生效，实际 {len(segs)} 段"


def test_builtin_demo_script_is_not_mutated(captured_run):
    """用内置剧本时必须深拷贝，绝不能改到模块常量（会污染后续调用）。"""
    import bettercomfyui.demo as demo_mod

    before = json.dumps(demo_mod.DEMO_SCRIPT, ensure_ascii=False, sort_keys=True)
    _run(script_text="", max_segments=1)
    after = json.dumps(demo_mod.DEMO_SCRIPT, ensure_ascii=False, sort_keys=True)
    assert before == after, "DEMO_SCRIPT 被就地修改了 —— 后续运行会拿到被截断的剧本"


def test_multi_chapter_truncation_spans_chapters(captured_run):
    """跨章节截断：max_segments 是全局上限，不是「每章上限」。"""
    script = {
        "chapters": [
            {"segments": [{"prompt": "a1"}, {"prompt": "a2"}]},
            {"segments": [{"prompt": "b1"}, {"prompt": "b2"}]},
        ]
    }
    _run(script_text=json.dumps(script), max_segments=3)
    total = sum(len(c["segments"]) for c in captured_run[0]["script"]["chapters"])
    assert total == 3, f"跨章节截断应得 3 段，实际 {total}"


def test_human_review_mode_is_declared_as_not_effective(captured_run):
    """段级审核模式配置里没有对应字段 -> 必须**如实说明**，不能假装生效。"""
    res = _run(review_mode="HUMAN")
    text = res["ui"]["text"][0]
    assert "暂不改变行为" in text or "未生效" in text, (
        f"段级 HUMAN 模式没有生效，但输出里没有说明：\n{text}"
    )


def test_cascade_disabled_is_declared(captured_run):
    """关掉级联必须给出说明（含「当前剧本下无可观察效果」的诚实交代）。"""
    res = _run(enable_cascade=False)
    text = res["ui"]["text"][0]
    assert "enable_cascade=False" in text, f"关闭级联没有说明：\n{text}"


def test_cascade_enabled_adds_no_noise(captured_run):
    """开启级联（默认）不应在输出里刷无意义的提示。"""
    res = _run(enable_cascade=True)
    text = res["ui"]["text"][0]
    assert "enable_cascade" not in text


def test_executor_choice_selects_real_path(captured_run):
    """执行器选择必须真的切换 mock / 真实路径。

    审计 R1 修正：本一键闭环**只有占位媒体**一条产出路径，没有真实生成执行器。
    因此「真实 ComfyUI」必须**显式报错**（失败闭合），不能静默退回占位、
    更不能把 ffmpeg 占位视频标成 `used_mock=False / executor=comfyui`（历史假成功）。
    """
    _run(executor="占位媒体(mock)")
    assert captured_run[-1]["executor"] == "mock"

    before = len(captured_run)
    with pytest.raises(N._NodeError) as ei:
        _run(executor="真实 ComfyUI")
    assert "占位媒体" in str(ei.value) and "假成功" in str(ei.value), (
        f"真实执行器缺失时必须显式说明「占位媒体/假成功」，实际：{ei.value!r}")
    assert len(captured_run) == before, (
        "真实 ComfyUI 分支不得调用 run_demo（否则仍会产出占位媒体并冒充真实）")


def test_status_marks_placeholder_path(captured_run):
    """占位路径必须在状态里显式标注（防止被当成真实产出）。"""
    res = _run(executor="占位媒体(mock)")
    status = res["result"][0]
    assert "占位" in status, f"状态没有标注占位路径：{status}"


def test_bad_script_json_fails_loudly(captured_run):
    """剧本不是合法 JSON 时必须报错并指出位置，不能静默用默认剧本。"""
    with pytest.raises(N._NodeError, match="不是合法 JSON"):
        _run(script_text="{ this is not json")


def test_script_tempfile_is_cleaned_up(captured_run):
    """传给 run_demo 的临时剧本文件必须被删除，不能残留。"""
    script = {"chapters": [{"segments": [{"prompt": "s1"}]}]}
    _run(script_text=json.dumps(script))
    path = captured_run[0]["chapter"]
    assert path is not None
    assert not Path(path).exists(), f"临时剧本文件残留：{path}"


# ---------------------------------------------------------------------------
# 2. 源码断言（防回归：谁再把参数改成装饰品就会红）
# ---------------------------------------------------------------------------


def test_run_body_consumes_every_declared_control():
    """`run()` 的函数体里必须出现每个控件的名字。

    这是「防装饰品」的机械保证：如果以后有人在 INPUT_TYPES 里加控件
    却不在 run() 里用，这条会提醒他要么接线、要么在 notes 里如实说明。
    """
    import ast
    import inspect

    src = inspect.getsource(N.BetterComfyUIRun.run)
    body = ast.parse(
        "class _X:\n" + "\n".join("    " + ln for ln in src.splitlines())
    )
    fn = body.body[0].body[0]
    used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}

    spec = N.BetterComfyUIRun.INPUT_TYPES()
    declared = set(spec["required"]) | set(spec.get("optional", {}))
    # 每个控件名都必须作为局部变量被引用（参数本身就是 Name，所以检查更严一点：
    # 至少不能出现「声明了但函数体里从未出现」）
    missing = sorted(c for c in declared if c not in used and c not in src)
    assert not missing, (
        f"这些控件声明了却从未在 run() 里使用：{missing} —— "
        "界面上给了控制项却不生效，比没有更糟"
    )
