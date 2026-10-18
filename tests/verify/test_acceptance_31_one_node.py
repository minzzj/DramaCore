"""验收 31：一个大节点完成「一次调用 → E1–E5 → 素材库 → 导演组」。

## 用户基准（2026-10-17 两次强调，原话）

> 「输入为一整段完整剧本，全程仅发起**一次 API 调用**，一次性输出全链路所需的全部提示词」
> 「按预设分隔符号自动拆分……以标准 JSON 格式自动填充到对应节点」
> 「做成一个大节点就可以了，插上模型，插上输出，然后就可以用了……把它们整合起来，
>  整合的要完整，而不是重新造很多东西，或者把它生硬的接起来。」

## 判定通过

    P1  编排层：真实模式 **API 调用次数 = 1**（计数替身）；
    P2  JSON 返回 → CHARACTER/SCENE/PROP/SHOT 四类拆分正确；
    P3  预设分隔符返回（=== 角色 === …）→ auto 路径自动识别并拆分；
    P4  全量 JSON 标准结构（schema/meta.api_calls/assets/chapters/…）；
    P5  视频提示词 `---` 独占行分隔（可直接喂导演台）；
    P6  占位模式：0 次调用、四类素材齐全、明确标注占位；
    P7  API 401 → 可读错误（含 401 与"已发起 API 调用 1 次"）；
    P8  输出解析不了 → 错误含原始输出前 500 字（不返回部分结果）；
    P9  大节点 `BetterComfyUIEverything`：5 个输出；占位运行 0 次调用；
        提示词JSON 是 `bettercomfyui.one-node/v1`，含四类素材 + 视频段 + 导演组摘要；
    P10 大节点真实路径：内部**只调用一次**；审计明确写"API 调用次数：1 次"；
        输出里含 E1–E5、素材库统计、导演组；
    P11 素材库整合：确定性匹配三态（新建/复用；疑似不覆盖）—— FakeStore 验证；
    P12 工作流形态：W0/W2 只有一个大节点直连导演台（不再串手工中间节点）；
        P1/W0L 本机可跑（零权重）；四套都有 ≥3 条"结果可见"预览连线；
    P13 输出端口名 = 导演组/分镜脚本/提示词JSON/素材清单/审计报告。

## 判定假通过（必须能变红）

    F1 generate_script_prompts 调两次 → P1/P10 变红；
    F2 四类拆分写死空数组 → P2/P3 变红；
    F3 工作流里重新塞回 ScriptPrompts/OneDirector 中间节点 → P12 变红。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

WORKFLOW_DIR = REPO_ROOT / "bettercomfyui_comfy" / "workflows"

CANNED_JSON = {
    "title": "雪夜客栈",
    "assets": [
        {"id": "A1", "asset_type": "CHARACTER", "name": "林秀", "prompt": "青色长袍女剑客"},
        {"id": "A2", "asset_type": "CHARACTER", "name": "老陈", "prompt": "白须老者"},
        {"id": "A3", "asset_type": "SCENE", "name": "客栈大堂", "prompt": "油灯昏黄木桌"},
        {"id": "A4", "asset_type": "PROP", "name": "旧信", "prompt": "泛黄信纸特写"},
        {"id": "A5", "asset_type": "SHOT", "name": "对视特写", "prompt": "双人中景构图"},
    ],
    "chapters": [
        {
            "chapter_id": "CH001",
            "title": "雪夜",
            "segments": [
                {"segment_index": 1, "prompt": "林秀推门走进客栈", "asset_refs": ["A1", "A3"]},
                {"segment_index": 2, "prompt": "老陈抬头看向门口", "asset_refs": ["A2", "A3"]},
            ],
        }
    ],
}

DELIMITED_TEXT = """=== 角色 ===
编号: A1
名称: 林秀
提示词: 青色长袍女剑客，正面清晰

=== 角色 ===
编号: A2
名称: 老陈
提示词: 白须老者，粗布衣裳

=== 场景 ===
编号: A3
名称: 客栈大堂
提示词: 油灯昏黄，木质结构

=== 物品 ===
编号: A4
名称: 旧信
提示词: 泛黄信纸特写

=== 分镜 ===
编号: A5
名称: 对视特写
提示词: 双人中景构图

=== 章节 ===
编号: CH001
标题: 雪夜

=== 段 ===
段号: 1
提示词: 林秀推门走进客栈
素材引用: A1, A3

=== 段 ===
段号: 2
提示词: 老陈抬头看向门口
素材引用: A2, A3
"""

#: 大节点 widgets_values 的下标（顺序 = INPUT_TYPES 中 widget 型输入顺序）
BIG_WIDGET_INDEX = {
    "剧本": 0, "系统提示词选择": 1, "自定义系统提示词": 2, "风格标签": 3,
    "统一风格": 4, "输出语言": 5, "分镜粒度": 6, "项目": 7, "写入素材库": 8,
    "每段时长_秒": 9, "总时长_秒": 10, "目标分辨率": 11, "帧率": 12,
    "启用素材库": 13, "素材优先级": 14, "段间素材": 15, "公共素材模式": 16,
    "素材审核模式": 17, "素材审核阈值": 18, "段间接续": 19, "使用占位结果": 20,
}


# ---------------------------------------------------------------------------
# 替身与工具
# ---------------------------------------------------------------------------


class _StubGateway:
    """计数替身：证明"只调用一次"，且不碰网络。"""

    def __init__(self, text: str = "", *, error: Exception | None = None) -> None:
        self.calls = 0
        self.text = text
        self.error = error

    def complete(self, *, system: str, user: str, **_params) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.text

    def describe(self) -> dict:
        return {"provider": "external_api",
                "url": "https://api.deepseek.com/chat/completions",
                "model": "deepseek-chat"}


class _FakeStore:
    """素材库契约替身（只实现 match_candidate 需要的方法）。"""

    def __init__(self, assets: list[dict] | None = None) -> None:
        self.assets = list(assets or [])
        self.created: list[dict] = []

    def find_by_normalized_name(self, normalized, scope=None):
        return [a for a in self.assets if a.get("normalized_name") == normalized]

    def list_assets(self, *, asset_type=None, scope=None):
        return list(self.assets)

    def resolve_lock(self, asset_id, version):
        return {"asset_id": asset_id, "version": version, "lock_status": "LOCKED"}

    def create_asset(self, **kwargs):
        created = {"asset_id": f"asset-{len(self.created) + 1}", **kwargs}
        self.created.append(created)
        return created


def _cfg():
    from bettercomfyui.config import Config

    cfg = Config()
    cfg.prompt.model_provider = "external_api"
    cfg.prompt.api_key = "sk-acceptance-31-fake"
    cfg.prompt.model_name = "deepseek-chat"
    cfg.prompt.endpoint = "https://api.deepseek.com"
    return cfg


def _script_build():
    import bettercomfyui.prompt.script_build as module

    return module


def _run(gateway, *, output_format: str = "auto", script: str = "林秀在雪夜推门走进客栈。"):
    return _script_build().generate_script_prompts(
        cfg=_cfg(), script_text=script, gateway=gateway, output_format=output_format)


def _load_workflow(name: str) -> dict:
    path = WORKFLOW_DIR / f"{name}.json"
    assert path.is_file(), f"工作流不存在：{path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _big_nodes(wf: dict) -> list[dict]:
    return [n for n in wf["nodes"] if n["type"] == "BetterComfyUIEverything"]


# ---------------------------------------------------------------------------
# P1–P8 编排层（一次调用 / 分类 / 分隔符 / JSON / 错误可读）
# ---------------------------------------------------------------------------


def test_p1_exactly_one_api_call():
    gateway = _StubGateway(json.dumps(CANNED_JSON, ensure_ascii=False))
    result = _run(gateway)
    assert gateway.calls == 1, f"实际调用了 {gateway.calls} 次（要求恰好 1 次）"
    assert result.meta["api_calls"] == 1
    assert "API 调用 1 次" in result.summary


def test_p2_json_splits_into_four_asset_types():
    result = _run(_StubGateway(json.dumps(CANNED_JSON, ensure_ascii=False)))
    grouped = result.assets_by_type
    assert [a["name"] for a in grouped["CHARACTER"]] == ["林秀", "老陈"]
    assert [a["name"] for a in grouped["SCENE"]] == ["客栈大堂"]
    assert [a["name"] for a in grouped["PROP"]] == ["旧信"]
    assert [a["name"] for a in grouped["SHOT"]] == ["对视特写"]
    assert result.meta["output_format_detected"] == "json"


def test_p3_preset_delimiters_are_auto_parsed():
    result = _run(_StubGateway(DELIMITED_TEXT))
    assert result.meta["output_format_detected"] == "delimited"
    assert len(result.assets_by_type["CHARACTER"]) == 2
    assert len(result.assets_by_type["SCENE"]) == 1
    assert len(result.assets_by_type["PROP"]) == 1
    assert len(result.assets_by_type["SHOT"]) == 1
    assert result.video_prompts == ["林秀推门走进客栈", "老陈抬头看向门口"]


def test_p4_full_json_is_standard_and_traceable():
    result = _run(_StubGateway(json.dumps(CANNED_JSON, ensure_ascii=False)))
    payload = json.loads(result.full_json)
    assert payload["schema"] == "bettercomfyui.prompts/v1"
    assert payload["meta"]["api_calls"] == 1
    assert payload["assets"] and payload["chapters"]
    assert set(payload["asset_prompts_by_type"]) >= {"CHARACTER", "SCENE", "PROP", "SHOT"}


def test_p5_video_prompts_text_uses_triple_dash_separator():
    result = _run(_StubGateway(json.dumps(CANNED_JSON, ensure_ascii=False)))
    assert result.video_prompts_text.split("\n---\n") == ["林秀推门走进客栈", "老陈抬头看向门口"]
    assert json.loads(result.video_prompts_json)["separator"] == "---"


def test_p6_placeholder_mode_makes_zero_calls_and_is_labelled():
    result = _script_build().build_placeholder_prompts(
        "林秀在雪夜推门走进客栈，老陈抬头看她。林秀坐下。", style="冷蓝夜色")
    assert result.meta["api_calls"] == 0
    assert "占位" in result.meta["mode"]
    for type_name in ("CHARACTER", "SCENE", "PROP", "SHOT"):
        assert result.assets_by_type[type_name], f"占位模式缺少 {type_name} 素材提示词"
    assert result.video_prompts
    assert "占位" in result.summary


def test_p7_auth_error_is_actionable():
    from bettercomfyui.models.enums import ErrorClass
    from bettercomfyui.prompt.gateway import GatewayError

    gateway = _StubGateway(error=GatewayError(
        "模型接口认证失败（HTTP 401）：请检查 prompt.api_key 是否有效",
        error_class=ErrorClass.MODEL_API))
    with pytest.raises(_script_build().ScriptBuildError) as info:
        _run(gateway)
    message = str(info.value)
    assert "401" in message
    assert "已发起 API 调用 1 次" in message


def test_p8_unparseable_output_reports_raw_head():
    gateway = _StubGateway("这不是 JSON，也不是分隔符文本，只是一段废话。")
    with pytest.raises(_script_build().ScriptBuildError) as info:
        _run(gateway, output_format="json")
    message = str(info.value)
    assert "原始输出前 500 字" in message
    assert "这不是 JSON" in message


# ---------------------------------------------------------------------------
# P9 大节点：5 输出 + 占位 0 调用
# ---------------------------------------------------------------------------


def test_p9_big_node_outputs_and_placeholder_mode():
    import bettercomfyui_comfy.everything_node as ev

    node_cls = ev.BetterComfyUIEverything
    assert node_cls.RETURN_NAMES == ("导演组", "分镜脚本", "提示词JSON", "素材清单", "审计报告")
    outputs = node_cls().run(
        剧本="林秀在雪夜推门走进客栈。老陈抬头看她。林秀坐下。",
        使用占位结果=True, 项目="", 启用素材库=False)
    assert len(outputs) == 5
    groups, script_text, prompt_json, assets_text, audit = outputs
    assert len(groups) == 3, f"占位应产出 3 段，实际 {len(groups)}"
    assert "\n---\n" in script_text
    payload = json.loads(prompt_json)
    assert payload["schema"] == "bettercomfyui.one-node/v1"
    assert payload["meta"]["api_calls"] == 0
    assert payload["meta"]["mode"].startswith("占位")
    assert set(payload["assets_by_type"]) >= {"CHARACTER", "SCENE", "PROP", "SHOT"}
    assert "API 调用次数：0 次" in audit
    assert "占位" in audit


# ---------------------------------------------------------------------------
# P10 大节点真实路径：内部只调用一次
# ---------------------------------------------------------------------------


def test_p10_big_node_calls_api_exactly_once(monkeypatch):
    import bettercomfyui_comfy.everything_node as ev
    import bettercomfyui.prompt.script_build as sb

    placeholder = sb.build_placeholder_prompts(
        "林秀在雪夜推门走进客栈。老陈抬头看她。", style="冷蓝夜色")
    scripted = sb.ScriptPromptResult(
        raw_text=placeholder.raw_text, script=placeholder.script,
        assets_by_type=placeholder.assets_by_type, video_prompts=placeholder.video_prompts,
        meta={**placeholder.meta, "mode": "真实模型（一次调用）", "api_calls": 1,
              "model": "deepseek-chat", "output_format_detected": "delimited"})
    calls = {"n": 0, "style": ""}

    def fake_generate(**kwargs):
        calls["n"] += 1
        calls["style"] = kwargs.get("style_hint")
        assert kwargs.get("script_text")
        return scripted

    monkeypatch.setattr(sb, "generate_script_prompts", fake_generate)
    monkeypatch.setattr(ev, "_resolve_api", lambda: {
        "api_key": "sk-mock", "model": "deepseek-chat",
        "api_base": "http://127.0.0.1:8199/v1", "configured": True, "source": "test"})
    # 不碰真实素材库（_open_asset_store 返回 (store, cfg, error) 三元组）
    monkeypatch.setattr(
        ev, "_open_asset_store",
        lambda project_id: (None, None, "测试替身：不碰真实素材库"))
    monkeypatch.setattr(ev, "_manifest_from_store", lambda store: "")
    monkeypatch.setattr(ev, "_load_locked_assets", lambda project_id: [])

    outputs = ev.BetterComfyUIEverything().run(
        剧本="林秀在雪夜推门走进客栈。老陈抬头看她。",
        使用占位结果=False, 写入素材库=True, 项目="",
        api_key="sk-mock", model="deepseek-chat", api_base="http://127.0.0.1:8199/v1")
    groups, _script_text, prompt_json, assets_text, audit = outputs
    payload = json.loads(prompt_json)
    assert calls["n"] == 1, f"大节点内部调用了 {calls['n']} 次 API（要求 1 次）"
    assert payload["meta"]["api_calls"] == 1
    assert len(groups) == 3
    assert payload["assets_by_type"]["CHARACTER"]
    assert "API 调用次数：1 次" in audit
    assert "素材库" in assets_text
    # 风格提示必须传进那唯一一次调用
    assert calls["style"], "统一风格没有传给编排层"


# ---------------------------------------------------------------------------
# P11 素材库整合：确定性匹配三态（新建 / 复用；疑似不覆盖）
# ---------------------------------------------------------------------------


def test_p11_asset_library_registration_is_deterministic():
    import bettercomfyui_comfy.everything_node as ev

    # 素材库里已有「林秀」（同名，应复用），其余不存在（应新建）
    existing = [{
        "asset_id": "asset-existing",
        "asset_type": "CHARACTER",
        "name": "林秀",
        "normalized_name": "林秀",
        "current_version": "v1",
        "versions": ["v1"],
    }]
    store = _FakeStore(existing)
    assets = [
        {"name": "林秀", "asset_type": "CHARACTER", "prompt": "青色长袍"},
        {"name": "客栈大堂", "asset_type": "SCENE", "prompt": "油灯"},
        {"name": "旧信", "asset_type": "PROP", "prompt": "泛黄"},
    ]
    stats = ev._register_generated_assets(store, assets, style="冷蓝", per_class_limit=9)
    assert len(stats["reused"]) == 1 and stats["reused"][0]["name"] == "林秀"
    assert len(stats["created"]) == 2
    created_names = {item["name"] for item in stats["created"]}
    assert created_names == {"客栈大堂", "旧信"}
    # 新建条目必须带上生成提示词（可审计）
    for item in store.created:
        assert "生成提示词" in item["note"]
    assert stats["errors"] == []


# ---------------------------------------------------------------------------
# P12 工作流形态：一个大节点直连；本机可跑版零权重；结果可见
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "workflow_name",
    ["W0_完整逻辑图全流程", "W2_DeepSeek全自动",
     "P1_剧本→全量提示词（本机可跑）", "W0L_本机可跑通体验版"],
)
def test_p12_workflows_have_one_big_node_and_visible_results(workflow_name):
    wf = _load_workflow(workflow_name)
    by_id = {n["id"]: n for n in wf["nodes"]}
    big_nodes = _big_nodes(wf)
    assert len(big_nodes) == 1, f"{workflow_name} 里应恰好有 1 个大节点"
    big = big_nodes[0]
    # 不允许再把手工作坊式的中间节点串在大节点前面
    for forbidden in ("BetterComfyUIScriptPrompts", "BetterComfyUIOneDirector"):
        assert forbidden not in by_id and forbidden not in {n["type"] for n in wf["nodes"]}, (
            f"{workflow_name} 仍串联 {forbidden}（应整合进大节点）")
    # 结果可见：提示词JSON / 素材清单 / 审计报告 三条输出各接预览
    preview_links = [l for l in wf["links"]
                     if int(l[1]) == big["id"] and by_id[int(l[3])]["type"] == "PreviewAny"]
    assert len(preview_links) >= 3, f"{workflow_name} 只有 {len(preview_links)} 条结果预览"
    for link in wf["links"]:
        dst = by_id[int(link[3])]
        assert int(link[4]) < len(dst.get("inputs") or []), (
            f"{workflow_name} 的连线 {link[0]} 指向不存在的输入槽")


def test_p12b_weight_chain_only_where_needed():
    """W0/W2 直连导演台出片；P1/W0L 是本机可跑（零权重）。"""
    def types(name):
        return {n["type"] for n in _load_workflow(name)["nodes"]}

    for name in ("W0_完整逻辑图全流程", "W2_DeepSeek全自动"):
        t = types(name)
        bigger = _big_nodes(_load_workflow(name))[0]
        director = next(n for n in _load_workflow(name)["nodes"]
                        if n["type"] == "MiniMaxH3Director")
        wf = _load_workflow(name)
        assert any(l[1] == bigger["id"] and l[3] == director["id"] and l[5] == "MMX_DIR_GROUP"
                   for l in wf["links"]), f"{name} 的大节点没有直连导演台"
        assert {"MiniMaxH3Director", "CreateVideo", "SaveVideo"} <= t

    for name in ("P1_剧本→全量提示词（本机可跑）", "W0L_本机可跑通体验版"):
        t = types(name)
        assert "MiniMaxH3Director" not in t, f"{name} 不应加载权重（本机可跑）"
        assert "UNETLoader" not in t, f"{name} 不应加载权重（本机可跑）"


def test_p12c_placeholder_switch_is_correct():
    """W0/W2/P1 真调（False）；W0L 本机体验用占位（True）。"""
    def placeholder_flag(name: str) -> bool:
        big = _big_nodes(_load_workflow(name))[0]
        return bool((big.get("widgets_values") or [])[BIG_WIDGET_INDEX["使用占位结果"]])

    assert placeholder_flag("W0_完整逻辑图全流程") is False
    assert placeholder_flag("W2_DeepSeek全自动") is False
    assert placeholder_flag("P1_剧本→全量提示词（本机可跑）") is False
    assert placeholder_flag("W0L_本机可跑通体验版") is True


def test_p12d_w2_is_minimal_one_big_node_chain():
    """W2 = 最简形态：大节点 + 模型 + 输出；不应该再有一堆中间节点。"""
    wf = _load_workflow("W2_DeepSeek全自动")
    types = {n["type"] for n in wf["nodes"]}
    assert "BetterComfyUISettings" not in types, "W2 最简形态不该再需要单独配置节点（大节点自动读）"
    assert len(wf["nodes"]) <= 15, f"W2 有 {len(wf['nodes'])} 个节点，不够精简"
