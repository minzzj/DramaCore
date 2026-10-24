"""ROUND59：LLM 整集分镜 JSON 截断/结构不符的解析容错与自动恢复。

复现（2026-10-24 远端实测，prompt_id 5ec8fb85 / fc7f9027 等）：
- 3 分 15 秒整集剧本 → DeepSeek 单次输出 8000+ token → ``finish_reason="length"``；
- 截断后旧解析器从输出内部取到 chapters 数组 / 单个素材对象，
  误报 ``invalid_top_level_type`` / ``unrecognized_output_shape``，8/8 集全部失败。

本文件锁定（P = 必须能变红）：
    P1 未闭合 ```json 围栏 + 截断 → ``truncated_json``，消息含 finish_reason / 结尾 / 切片位置；
    P2 完整对象混在说明文字 / 前导数组里 → 仍取到对象（不误报数组）；
    P3 只有数组 → 仍是 ``invalid_top_level_type``（保留原契约）；
    P4 截断整集输出 → ``generate_script_prompts`` 自动按场景拆分、合并 assets/chapters，
        ``api_calls`` 如实计数、``degraded="scene_split"``；
    P5 结构不符（只给了单个素材对象）→ 纠正重试 1 次并成功（``degraded="schema_retry"``）；
    P6 正常一次成功 → 仍然只调用 1 次（恢复逻辑不得改变成功路径）；
    P7 拆分后仍有坏块 → 成功块保留 + ``failed_chunks`` 明示（不静默丢场景）；
    P8 ``split_script_into_chunks`` 纯函数：按场景标题切、无标题按行切、块数上限；
    P9 网关记录 finish_reason / usage（解析层据此识别截断）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bettercomfyui.prompt.script_build import (  # noqa: E402
    ScriptBuildError,
    ScriptPromptResult,
    generate_script_prompts,
    split_script_into_chunks,
)
from bettercomfyui.prompt.validate import (  # noqa: E402
    ScriptValidationError,
    explain_json_extraction,
    parse_with_report,
)

# ---------------------------------------------------------------------------
# 真实故障形状的截断样本（结构真实、内容为测试用占位）
# ---------------------------------------------------------------------------

#: 截断发生在 assets 里：chapters 数组是完整的 → 旧解析器会先取到「数组」。
TRUNCATED_IN_ASSETS = """```json
{
  "title": "测试集：一句玩笑",
  "chapters": [
    {
      "chapter_id": "CH001",
      "title": "一句玩笑",
      "summary": "酒会上两位老板开玩笑要给儿女结亲家。",
      "segments": [
        {"segment_index": 1, "prompt": "integrated_multimodal_description: [Shot 1] 酒会全景，冷蓝电影光。", "asset_refs": ["A1"], "duration_s": 8},
        {"segment_index": 2, "prompt": "integrated_multimodal_description: [Shot 1] 两位老板碰杯特写。", "asset_refs": ["A1", "A2"], "duration_s": 6}
      ]
    }
  ],
  "assets": [
    {"id": "A1", "asset_type": "CHARACTER", "name": "王振东", "prompt": "49 岁，精致西服，现实尺度，真实皮肤质感……"},
    {"id": "A2", "asset_type": "CHARACTER", "name": "张建国", "prompt": "48 岁，中式立领装，镜头还没写完就断在这里"""

#: 截断发生在 chapters 里：assets 数组是完整的 → 旧解析器会取到「单个素材对象」。
TRUNCATED_IN_CHAPTERS = """```json
{
  "title": "测试集：热搜风暴",
  "assets": [
    {"id": "A1", "asset_type": "CHARACTER", "name": "张浩宇", "prompt": "24 岁，剑桥硕士，黑色大衣，现实尺度，真实皮肤质感。", "negative_prompt": "CGI, plastic look", "source": "AI", "library_scope": "PROJECT"},
    {"id": "A2", "asset_type": "CHARACTER", "name": "王思涵", "prompt": "23 岁，留法归来，米白风衣，真实皮肤质感。", "negative_prompt": "CGI, plastic look", "source": "AI", "library_scope": "PROJECT"}
  ],
  "chapters": [
    {
      "chapter_id": "CH001",
      "title": "机场围堵",
      "segments": [
        {"segment_index": 1, "prompt": "integrated_multimodal_description: [Shot 1] 机场 VIP 走廊，左右分屏，记者围堵，镜头还没写完"""

#: 完整但被说明文字包着的输出（旧实现能解析；回归保护）
COMPLETE_WITH_PROSE = """好的，下面是拆解结果：

[1, 2, 3]

{
  "title": "完整剧本",
  "chapters": [
    {"chapter_id": "CH001", "title": "第一章", "segments": [
      {"segment_index": 1, "prompt": "林澈推门走进雪夜客栈。", "asset_refs": ["A1"], "duration_s": 8}
    ]}
  ],
  "assets": [
    {"id": "A1", "asset_type": "CHARACTER", "name": "林澈", "prompt": "青色长袍女剑客，现实尺度。"}
  ]
}

以上为全部内容。"""


def _mini_script(index: int) -> dict:
    """分块恢复用的最小合法剧本（每块 1 段；角色名跨块去重测试）。"""
    asset_name = "林澈" if index % 2 else "老陈"
    return {
        "title": f"第{index}块",
        "chapters": [
            {
                "chapter_id": "CH001",
                "title": f"场景{index}",
                "segments": [
                    {
                        "segment_index": 1,
                        "prompt": f"第{index}块的视频提示词：镜头缓慢推进。",
                        "asset_refs": [asset_name],
                        "duration_s": 8,
                    }
                ],
            }
        ],
        "assets": [
            {
                "id": "A1",
                "asset_type": "CHARACTER",
                "name": asset_name,
                "prompt": f"{asset_name}的人物提示词，现实尺度。",
            }
        ],
    }


class _ScriptedGateway:
    """按顺序返回脚本化响应，并像真实网关一样记录 finish_reason。"""

    def __init__(self, responses, finish_reasons=None) -> None:
        self.responses = list(responses)
        self.finish_reasons = list(finish_reasons or [])
        self.calls = 0
        self.users: list[str] = []
        self.systems: list[str] = []
        self.last_finish_reason = None

    def complete(self, *, system: str, user: str, **_params) -> str:
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        self.users.append(user)
        self.systems.append(system)
        if self.calls - 1 < len(self.finish_reasons):
            self.last_finish_reason = self.finish_reasons[self.calls - 1]
        else:
            self.last_finish_reason = "stop"
        return self.responses[index]

    def describe(self) -> dict:
        return {"provider": "test", "model": "scripted", "url": ""}


def _cfg():
    from bettercomfyui.config import Config

    cfg = Config()
    cfg.prompt.output_language = "中文"
    cfg.prompt.story_granularity = "按镜头"
    cfg.prompt.output_format = "auto"
    cfg.prompt.system_prompt_template = "strict_json"
    return cfg


def _three_scene_script() -> str:
    return (
        "《测试》第一集：雪夜\n"
        "场景1—1：雪夜客栈门口 夜\n林澈推门走进客栈。\n"
        "场景1—2：客栈大堂 夜\n老陈抬头看她。\n"
        "场景1—3：后院 夜\n两人在雪中对峙。\n"
    )


# ---------------------------------------------------------------------------
# P1/P2/P3：解析层
# ---------------------------------------------------------------------------


def test_p1_truncated_unclosed_fence_is_diagnosed_as_truncated_json():
    with pytest.raises(ScriptValidationError) as info:
        parse_with_report(TRUNCATED_IN_ASSETS, output_format="json", finish_reason="length")
    exc = info.value
    assert "truncated_json" in exc.codes, exc.codes
    message = str(exc)
    assert "finish_reason=length" in message or "max_tokens" in message
    assert "围栏只有开头没有闭合" in message
    assert "原始输出" in message and "结尾" in message
    # 不能再笼统地报「顶层收到数组」——那是旧 bug 的表现
    assert "invalid_top_level_type" not in exc.codes


def test_p1b_truncated_output_never_returns_inner_fragment():
    """旧实现会从截断文本里取到单个素材对象 → 误报 unrecognized_output_shape。"""
    with pytest.raises(ScriptValidationError) as info:
        parse_with_report(TRUNCATED_IN_CHAPTERS, output_format="json", finish_reason="length")
    assert "truncated_json" in info.value.codes, info.value.codes


def test_p2_complete_object_with_prose_and_leading_array_is_extracted():
    parsed, report = parse_with_report(COMPLETE_WITH_PROSE, output_format="json")
    assert isinstance(parsed, dict)
    assert parsed["title"] == "完整剧本"
    assert len(parsed["chapters"]) == 1
    assert report.extracted_from in ("fence", "balanced", "fence_balanced", "raw")


def test_p2b_unclosed_fence_with_complete_object_is_still_parsed():
    raw = "```json\n" + json.dumps(
        {"title": "x", "chapters": [{"chapter_id": "CH001", "segments": [
            {"segment_index": 1, "prompt": "p", "asset_refs": []}]}], "assets": []},
        ensure_ascii=False,
    )  # 没有结尾的 ```
    parsed, _report = parse_with_report(raw, output_format="json")
    assert parsed["title"] == "x"


def test_p3_array_only_output_still_reports_invalid_top_level_type():
    with pytest.raises(ScriptValidationError) as info:
        parse_with_report('["不是对象"]', output_format="json")
    assert "invalid_top_level_type" in info.value.codes


def test_p1c_explain_json_extraction_reports_positions_and_types():
    diag = explain_json_extraction(TRUNCATED_IN_ASSETS, finish_reason="length")
    assert diag["truncated"] is True
    assert diag["array_candidates"] >= 1, "样本里 chapters 数组应能被单独解析"
    assert any(row["parsed"] == "list" for row in diag["candidates"])
    assert any(isinstance(row.get("start"), int) for row in diag["candidates"])


# ---------------------------------------------------------------------------
# P4/P5/P6/P7：编排层自动恢复
# ---------------------------------------------------------------------------


def test_p4_truncated_full_episode_recovers_by_scene_split():
    script_text = _three_scene_script()
    chunks = split_script_into_chunks(script_text)
    assert len(chunks) == 3, chunks
    gateway = _ScriptedGateway(
        [TRUNCATED_IN_ASSETS]
        + [json.dumps(_mini_script(index), ensure_ascii=False) for index in range(1, 4)],
        finish_reasons=["length", "stop", "stop", "stop"],
    )
    result = generate_script_prompts(
        cfg=_cfg(), script_text=script_text, gateway=gateway, output_format="auto"
    )
    assert isinstance(result, ScriptPromptResult)
    assert gateway.calls == 1 + len(chunks)
    assert result.meta["api_calls"] == 1 + len(chunks)
    assert result.meta["degraded"] == "scene_split"
    assert result.meta["chunk_count"] == len(chunks)
    assert result.meta["success_chunks"] == len(chunks)
    assert len(result.video_prompts) == len(chunks), result.video_prompts
    assert len(result.script["chapters"]) == len(chunks)
    # assets 按归一化名去重：3 块里只有「林澈 / 老陈」两个角色
    names = [a["name"] for a in result.script["assets"]]
    assert sorted(names) == ["林澈", "老陈"], names
    # 跨块引用仍能解析（第 2/3 块引用了第 1 块就有的「林澈」）
    for chapter in result.script["chapters"]:
        assert chapter["segments"][0]["asset_refs"], chapter
    assert any("降级" in note for note in result.meta["notes"])


def test_p5_unknown_shape_triggers_one_corrective_retry():
    single_asset = json.dumps(
        {"asset_type": "CHARACTER", "id": "A1", "name": "林澈", "prompt": "青色长袍"},
        ensure_ascii=False,
    )
    full = json.dumps(
        {
            "title": "纠正后",
            "chapters": [
                {"chapter_id": "CH001", "segments": [
                    {"segment_index": 1, "prompt": "林澈推门。", "asset_refs": ["A1"],
                     "duration_s": 8}]}
            ],
            "assets": [{"id": "A1", "asset_type": "CHARACTER", "name": "林澈",
                        "prompt": "青色长袍女剑客。"}],
        },
        ensure_ascii=False,
    )
    gateway = _ScriptedGateway([single_asset, full], finish_reasons=["stop", "stop"])
    result = generate_script_prompts(
        cfg=_cfg(), script_text="林澈在雪夜推门走进客栈。", gateway=gateway,
        output_format="auto",
    )
    assert gateway.calls == 2, gateway.calls
    assert result.meta["api_calls"] == 2
    assert result.meta["degraded"] == "schema_retry"
    assert result.video_prompts == ["林澈推门。"]
    # 纠正重试的提示词里必须明确写出正确 schema / 精简要求
    assert "assets" in gateway.users[1] and "chapters" in gateway.users[1]
    assert "纠正重试" in gateway.users[1]


def test_p6_success_path_still_uses_exactly_one_call():
    full = json.dumps(_mini_script(1), ensure_ascii=False)
    gateway = _ScriptedGateway([full])
    result = generate_script_prompts(
        cfg=_cfg(), script_text="林澈推门走进客栈。", gateway=gateway, output_format="auto"
    )
    assert gateway.calls == 1
    assert result.meta["api_calls"] == 1
    assert result.meta["degraded"] == ""
    assert result.meta["finish_reason"] == "stop"


def test_p7_failed_chunk_is_reported_not_hidden():
    script_text = _three_scene_script()
    chunks = split_script_into_chunks(script_text)
    assert len(chunks) == 3
    bad = "这不是 JSON，只是一段说明文字。"
    responses = [TRUNCATED_IN_ASSETS]
    # chunk1：正常；chunk2：两次都坏；chunk3：正常
    responses += [json.dumps(_mini_script(1), ensure_ascii=False)]
    responses += [bad, bad]
    responses += [json.dumps(_mini_script(3), ensure_ascii=False)]
    gateway = _ScriptedGateway(responses, finish_reasons=["length", "stop", "stop", "stop", "stop"])
    result = generate_script_prompts(
        cfg=_cfg(), script_text=script_text, gateway=gateway, output_format="auto"
    )
    assert result.meta["degraded"] == "scene_split"
    assert result.meta["success_chunks"] == 2
    assert result.meta["failed_chunks"], "坏块必须写进 failed_chunks，不能静默丢场景"
    assert any("失败" in note for note in result.meta["notes"])
    assert len(result.video_prompts) == 2


def test_p7b_recovery_disabled_raises_with_actionable_message():
    gateway = _ScriptedGateway([TRUNCATED_IN_ASSETS], finish_reasons=["length"])
    with pytest.raises(ScriptBuildError) as info:
        generate_script_prompts(
            cfg=_cfg(), script_text=_three_scene_script(), gateway=gateway,
            output_format="auto", allow_recovery=False,
        )
    message = str(info.value)
    assert "finish_reason=length" in message
    assert "原始输出前 500 字" in message
    assert "结尾 200 字" in message
    assert gateway.calls == 1


# ---------------------------------------------------------------------------
# P8：拆分纯函数
# ---------------------------------------------------------------------------


def test_p8_split_by_scene_headers():
    chunks = split_script_into_chunks(_three_scene_script())
    assert len(chunks) == 3
    assert "场景1—1" in chunks[0]
    assert "场景1—2" in chunks[1]
    assert "场景1—3" in chunks[2]


def test_p8b_split_without_headers_by_lines():
    text = "\n".join(f"第{i}行剧本内容，人物在走。" for i in range(1, 60))
    chunks = split_script_into_chunks(text, max_chunks=4)
    assert 2 <= len(chunks) <= 4, len(chunks)
    assert all(chunk.strip() for chunk in chunks)


def test_p8c_split_respects_max_chunks():
    text = "\n".join(f"场景{i}：某地 夜\n第{i}场的剧情。" for i in range(1, 21))
    chunks = split_script_into_chunks(text, max_chunks=3)
    assert len(chunks) <= 3, len(chunks)


def test_p8d_split_short_single_scene_returns_one_chunk():
    assert split_script_into_chunks("林澈推门走进客栈。") == ["林澈推门走进客栈。"]


# ---------------------------------------------------------------------------
# P9：网关 finish_reason 记录
# ---------------------------------------------------------------------------


def test_p9_gateway_records_finish_reason_and_usage():
    from bettercomfyui.prompt.gateway import ExternalApiGateway

    class _Transport:
        def post_json(self, *, url, headers, payload, timeout_s):
            return {
                "choices": [
                    {"message": {"content": "{}"}, "finish_reason": "length"}
                ],
                "usage": {"completion_tokens": 8192},
            }

    gateway = ExternalApiGateway(
        endpoint="https://api.example.com", api_key="k", model_name="m",
        transport=_Transport(),
    )
    gateway.complete(system="s", user="u")
    assert gateway.last_finish_reason == "length"
    assert gateway.response_meta()["finish_reason"] == "length"
    assert gateway.response_meta()["usage"]["completion_tokens"] == 8192


def test_p9b_prompt_contract_has_output_budget_rule():
    from bettercomfyui.prompt.inject import build_system_prompt

    system = build_system_prompt(cfg=_cfg())
    assert "输出预算" in system
    assert "6000 tokens" in system


# ---------------------------------------------------------------------------
# P10：节点审计必须暴露降级（不能让人以为「一次成功」）
# ---------------------------------------------------------------------------


def test_p10_everything_node_audits_degraded_recovery(monkeypatch):
    import bettercomfyui_comfy.everything_node as ev
    import bettercomfyui.prompt.script_build as sb

    placeholder = sb.build_placeholder_prompts("林澈推门。老陈抬头。", style="")
    degraded = sb.ScriptPromptResult(
        raw_text=placeholder.raw_text,
        script=placeholder.script,
        assets_by_type=placeholder.assets_by_type,
        video_prompts=placeholder.video_prompts,
        meta={
            **placeholder.meta,
            "mode": "真实模型（4 次调用，含降级恢复）",
            "api_calls": 4,
            "degraded": "scene_split",
            "fallback_reason": "输出被截断（finish_reason=length / max_tokens 上限）",
            "finish_reason": "length",
            "chunk_count": 3,
            "success_chunks": 3,
            "failed_chunks": [],
            "notes": ["首次整份输出不可用 → 已降级为按场景拆分"],
        },
    )
    monkeypatch.setattr(sb, "generate_script_prompts", lambda **kwargs: degraded)
    monkeypatch.setattr(
        ev,
        "_resolve_api",
        lambda: {"api_key": "sk-mock", "model": "deepseek-chat",
                 "api_base": "http://127.0.0.1:8199/v1", "configured": True, "source": "test"},
    )
    monkeypatch.setattr(ev, "_open_asset_store",
                        lambda project_id: (None, None, "测试替身：不碰真实素材库"))
    monkeypatch.setattr(ev, "_manifest_from_store", lambda store: "")
    monkeypatch.setattr(ev, "_load_locked_assets", lambda project_id: [])

    outputs = ev.BetterComfyUIEverything().run(
        剧本="林澈在雪夜推门走进客栈。老陈抬头看她。",
        使用占位结果=False, 写入素材库=False, 项目="",
        api_key="sk-mock", model="deepseek-chat", api_base="http://127.0.0.1:8199/v1",
    )
    _groups, _script_text, prompt_json, _assets_text, audit = outputs
    assert "降级恢复：scene_split" in audit, audit
    assert "分块 3 / 成功 3" in audit, audit
    assert "API 调用次数：4 次" in audit, audit
    payload = json.loads(prompt_json)
    assert payload["meta"]["degraded"] == "scene_split"
    assert payload["meta"]["finish_reason"] == "length"
    assert any("已自动降级" in str(item) for item in payload["warnings"])

