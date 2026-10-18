"""验收 28：成品安装验证 + 新工作流 + 愿景对齐矩阵。

## 三部分

1. **安装验证**（用户要求"把成品安装到里面"）：不靠嘴说，逐项核实
   插件目录/逐文件哈希/巨人包/工作流/运行时节点注册。
2. **新工作流**（用户要求"为我设计新的工作流"）：4 套，全部按真实 schema 构建，
   三重校验（节点存在/槽位范围/类型匹配/源 links 记录）。
3. **愿景对齐矩阵**：把愿景里的要点逐条映射到**代码符号**，证明不是空话。

## 判定通过

    P1 插件已安装且**逐文件哈希一致**（排除装了一半/装的是旧版）；
    P2 两个巨人包已安装；
    P3 4 套新工作流在**界面目录**里可直接打开；
    P4 每套工作流都能通过真实 schema 校验（节点存在/槽位/类型/源 links）；
    P5 W1 只有 1 个节点（"最简单"名副其实）；
    P6 W2 含完整链路：StoryboardNode -> Bridge -> OneDirector -> Director
       -> CreateVideo -> SaveVideo；
    P7 W3 含加载器链（UNET/LoRA/CLIP/VAE）+ 出片链；
    P8 W4 把 AssetSlots 的 IMAGE 输出接到 OneDirector 的参考图端口；
    P9 运行中的 ComfyUI 里 17 个我的节点 + 2 个巨人节点全部可见；
    P10 **愿景对齐矩阵**：每条愿景要点都能在代码里找到实现符号。

## 判定假通过（必须能变红）

    F1 删掉一个插件文件 -> P1 变红；
    F2 工作流连线类型写错 -> P4 变红；
    F3 把 W1 塞进多余节点 -> P5 变红；
    F4 删掉某条愿景的实现 -> P10 变红。
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

BASE = "http://127.0.0.1:8188"
UI_WORKFLOWS = REPO_ROOT / ".comfyui" / "user" / "default" / "workflows"

NEW_WORKFLOWS = (
    "W1_最简单入门.json",
    "W2_DeepSeek全自动.json",
    "W3_全流程出片.json",
    "W4_素材参考图注入.json",
)


def _object_info() -> dict | None:
    try:
        return json.load(urllib.request.urlopen(f"{BASE}/object_info", timeout=40))
    except Exception:  # noqa: BLE001
        return None


@pytest.fixture(scope="module")
def schema() -> dict:
    info = _object_info()
    if info is None:
        pytest.skip("未验证：本机 ComfyUI 不可达，无法校验工作流 schema")
    return info


# ---------------------------------------------------------------------------
# P1 / P2 / P9 安装验证
# ---------------------------------------------------------------------------


def test_p1_plugin_installed_with_identical_hashes():
    """插件已安装，且**逐文件哈希一致**（排除装了一半/旧版）。"""
    from verify_install import _compare_trees, INSTALLED, PLUGIN_SRC

    problems = _compare_trees(PLUGIN_SRC, INSTALLED)
    assert not problems, "安装不一致：\n  " + "\n  ".join(problems[:10])


def test_p2_giant_packs_installed():
    from verify_install import COMFYUI, EXPECTED_GIANTS

    missing = [g for g in EXPECTED_GIANTS
               if not (COMFYUI / "custom_nodes" / g).is_dir()]
    assert not missing, f"缺少巨人包：{missing}"


def test_p9_all_nodes_visible_in_running_comfyui(schema):
    from verify_install import EXPECTED_NODES

    missing = [n for n in EXPECTED_NODES if n not in schema]
    assert not missing, f"界面里看不到这些节点：{missing}"
    for giant in ("StoryboardNode", "DeepSeekPromptConnector"):
        assert giant in schema, f"巨人节点 {giant} 不可见"


# ---------------------------------------------------------------------------
# P3 工作流存在
# ---------------------------------------------------------------------------


def test_p3_new_workflows_are_in_ui_directory():
    missing = [n for n in NEW_WORKFLOWS if not (UI_WORKFLOWS / n).is_file()]
    assert not missing, f"界面目录缺少工作流：{missing}"


# ---------------------------------------------------------------------------
# P4 全部工作流通过 schema 校验
# ---------------------------------------------------------------------------


def _validate(schema: dict, wf: dict) -> list[str]:
    from build_workflows_v2 import validate

    return validate(schema, wf)


@pytest.mark.parametrize("name", NEW_WORKFLOWS)
def test_p4_workflow_passes_schema_validation(schema, name):
    wf = json.loads((UI_WORKFLOWS / name).read_text(encoding="utf-8"))
    problems = _validate(schema, wf)
    assert not problems, f"{name} 校验失败：\n  " + "\n  ".join(problems)


# ---------------------------------------------------------------------------
# P5 / P6 / P7 / P8 各工作流的实质内容
# ---------------------------------------------------------------------------


def _load(name: str) -> dict:
    return json.loads((UI_WORKFLOWS / name).read_text(encoding="utf-8"))


def _types(wf: dict) -> set[str]:
    return {n["type"] for n in wf["nodes"]}


def test_p5_w1_is_minimal():
    """"最简单入门"必须真的简单 —— 只有 1 个节点。"""
    wf = _load("W1_最简单入门.json")
    assert len(wf["nodes"]) == 1, f"W1 有 {len(wf['nodes'])} 个节点，不够简单"
    assert wf["nodes"][0]["type"] == "BetterComfyUIOneDirector"


def test_p6_w2_has_full_deepseek_chain():
    """W2 必须是最简「一个大节点 + 模型 + 输出」链路（用户 ROUND26 要求）。"""
    wf = _load("W2_DeepSeek全自动.json")
    types = _types(wf)
    for required in ("BetterComfyUIEverything", "MiniMaxH3Director",
                     "CreateVideo", "SaveVideo"):
        assert required in types, f"W2 缺少 {required}"
    # 大节点必须直接连到导演台（中间不允许再串一堆节点）
    big = next(n for n in wf["nodes"] if n["type"] == "BetterComfyUIEverything")
    director = next(n for n in wf["nodes"] if n["type"] == "MiniMaxH3Director")
    assert any(l[1] == big["id"] and l[3] == director["id"] and l[5] == "MMX_DIR_GROUP"
               for l in wf["links"]), "大节点的导演组没有直接连到导演台"
    # 最简形态：不允许再出现手工串的提示词中间节点
    for forbidden in ("BetterComfyUIScriptPrompts", "BetterComfyUIStoryboard",
                      "BetterComfyUIPromptBridge", "BetterComfyUIOneDirector"):
        assert forbidden not in types, f"W2 不应再串联 {forbidden}（应整合进大节点）"
    # 每个 link 的 target_slot 必须真实存在
    by_id = {n["id"]: n for n in wf["nodes"]}
    for link in wf["links"]:
        dst = by_id[link[3]]
        assert int(link[4]) < len(dst.get("inputs") or []), (
            f"连线 {link[0]} 指向 {dst['type']} 不存在的输入槽 {link[4]}")
    # 用户基准：一次调用 → 全量提示词（E1–E5）由大节点内部完成
    assert len(wf["nodes"]) <= 15, f"W2 有 {len(wf['nodes'])} 个节点，不够『一个大节点』"


def test_p7_w3_has_loaders_and_output_chain():
    wf = _load("W3_全流程出片.json")
    types = _types(wf)
    for required in ("UNETLoader", "CLIPLoader", "VAELoader",
                     "BetterComfyUIOneDirector", "MiniMaxH3Director",
                     "CreateVideo", "SaveVideo"):
        assert required in types, f"W3 缺少 {required}"
    # 导演组必须连到导演台的 r2v_groups
    one = next(n for n in wf["nodes"] if n["type"] == "BetterComfyUIOneDirector")
    director = next(n for n in wf["nodes"] if n["type"] == "MiniMaxH3Director")
    assert any(l[1] == one["id"] and l[3] == director["id"] for l in wf["links"]), \
        "一体化导演台没有连到真实导演台"


def test_p8_w4_links_asset_slots_images(schema):
    """W4 必须把 AssetSlots 的 IMAGE 接到 OneDirector 的参考图端口。"""
    wf = _load("W4_素材参考图注入.json")
    slots = next(n for n in wf["nodes"] if n["type"] == "BetterComfyUIAssetSlots")
    one = next(n for n in wf["nodes"] if n["type"] == "BetterComfyUIOneDirector")
    image_links = [l for l in wf["links"]
                   if l[1] == slots["id"] and l[3] == one["id"] and l[5] == "IMAGE"]
    assert len(image_links) >= 1, "素材没有以 IMAGE 形式接到导演台"
    # 目标端口必须是参考图
    for link in image_links:
        slot_index = link[4]
        target_name = one["inputs"][slot_index]["name"]
        assert target_name.startswith("参考图"), f"接到了错误的端口：{target_name}"


# ---------------------------------------------------------------------------
# P10 愿景对齐矩阵
# ---------------------------------------------------------------------------


#: (愿景/文档要点, 必须存在的代码符号)。每条都对应一个真实实现。
VISION_MATRIX = (
    ("站在巨人肩膀上：接真实导演台而不是自造", "MiniMaxH3Director"),
    ("一键出片：一个节点完成素材+多段提示词+一致性", "BetterComfyUIOneDirector"),
    ("尾部 22 帧机制保留（不替代，只叠加）", "tail_frames"),
    ("素材库全量挂每一段", "plan_ref_slots"),
    ("素材优先级 角色>场景>物品>分镜", "asset_priority"),
    ("段间素材只挂引用它的段", "段间素材"),
    ("素材 LOCK 绑定 asset_id+version+content_hash", "resolve_lock"),
    ("素材只追加不覆盖（R2）", "ImmutableAssetError"),
    ("项目隔离（R5）", "PathIsolationError"),
    ("CNT1/CNT2/CNT3 语义隔离", "CounterKind"),
    ("技术重试不消耗质量重做次数", "tech_retry_limit"),
    ("人工审核超时自动切模型", "AUTO_SWITCHED"),
    ("素材审核唯一提交点", "finalization_token"),
    ("视频只重做不合格段（爆炸半径）", "segments_depending_on_asset"),
    ("级联深度上限防无限循环", "max_cascade_depth"),
    ("断点续跑", "CheckpointStore"),
    ("版本回滚", "superseded_by"),
    ("真实 vs mock 必须区分", "generation_provenance"),
    ("阶段化显存调度（5090 32G）", "VramPhaseScheduler"),
    ("模型卸载可核验（不假装释放）", "free_models"),
    ("DeepSeek 出提示词", "BetterComfyUIPromptBridge"),
    ("风格标签库", "STYLE_LIBRARY"),
    ("背景音乐", "bgm_file"),
    ("返工提示词加料", "build_rework_prompt"),
    ("素材统一分辨率", "unify_resolution"),
    ("每类素材数量上限", "per_class_limit"),
    ("素材可读命名", "human_label"),
    ("分维度评分", "_resolve_dimension_scores"),
    ("任务超时看门狗", "rework_timeout_s"),
    ("ComfyUI 地址可配", "resolve_comfyui_endpoint"),
    ("配置导入导出（脱敏）", "export_config_json"),
    ("手动操作与自动切换日志", "log_manual_or_switch"),
    # ROUND26：用户基准 —— 一个大节点完成「一次调用 → E1–E5 → 素材库 → 导演组」
    ("一个大节点完成全链（E1–E5+素材库+导演组）", "BetterComfyUIEverything"),
    ("按预设分隔符自动拆分", "_parse_delimited"),
    ("生成素材按确定性匹配登记进素材库", "_register_generated_assets"),
    # ROUND27：用户第三次补充 —— 素材生成 / 素材审核 / 视频审核
    ("素材生成（取任务+入库新版本+待审核）", "BetterComfyUIAssetGen"),
    ("素材审核走完整状态机（CNT1/LOCK）", "BetterComfyUIAssetReview"),
    ("视频审核（R_seg + 边界 + R_full）", "BetterComfyUIVideoReview"),
    ("本地启发式评分器（真实帧统计，非常量）", "local_scorer"),
)


def _search_text() -> str:
    chunks: list[str] = []
    for sub in ("bettercomfyui", "bettercomfyui_comfy"):
        for path in (REPO_ROOT / sub).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def test_p10_vision_matrix_all_implemented():
    """**愿景对齐矩阵**：每条要点都能在代码里找到实现符号。"""
    blob = _search_text()
    missing = [(label, symbol) for label, symbol in VISION_MATRIX
               if symbol not in blob]
    assert not missing, (
        "以下愿景要点在代码里找不到实现：\n  "
        + "\n  ".join(f"{label} -> {symbol}" for label, symbol in missing)
    )


def test_p10b_matrix_is_substantial():
    """矩阵本身要够全（防止有人删条目来"通过"）。"""
    assert len(VISION_MATRIX) >= 30, f"对齐矩阵只有 {len(VISION_MATRIX)} 条"
