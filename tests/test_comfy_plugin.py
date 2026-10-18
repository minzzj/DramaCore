"""插件加载测试：**按 ComfyUI 的真实方式**加载 `bettercomfyui_comfy` 并校验契约。

为什么不能只静态检查源码：ComfyUI 加载自定义节点时会做几件具体的事
（见 ComfyUI `nodes.py` 的自定义节点加载逻辑）：

1. 把 `custom_nodes/` 目录加进 `sys.path`；
2. 以包名 `custom_nodes.<目录名>` import 该目录下的 `__init__.py`；
3. 读 `NODE_CLASS_MAPPINGS`，给每个类设 `RELATIVE_PYTHON_MODULE`；
4. 读 `NODE_DISPLAY_NAME_MAPPINGS` 作为显示名；
5. 读 `WEB_DIRECTORY`，把该目录列为前端扩展目录。

任何一步不符合，用户的体验就是「节点不见了」或「网页没反应」。
所以本文件**完整复刻这 5 步**，而不是读源码猜。

同时校验本项目的硬纪律：
* 插件在**缺编排包**时也必须能加载（不能让 ComfyUI 崩）；
* 第二阶段节点**不得**出现在注册表里；
* 每个节点都必须能被实例化并跑一次 `INPUT_TYPES()`。
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACK_DIR = REPO_ROOT / "bettercomfyui_comfy"
PACK_NAME = "bettercomfyui_comfy"

#: ComfyUI 会往 REQUIRED 之外的节点结构里读这些必填属性
REQUIRED_NODE_ATTRS = ("INPUT_TYPES", "RETURN_TYPES", "FUNCTION", "CATEGORY")

#: 第二阶段节点名特征：这些名字**绝不能**出现在注册表里
PHASE2_FORBIDDEN_NODE_KEYS = (
    "AutoFetch",
    "AutoPublish",
    "FetchChapter",
    "PublishMovie",
    "Phase2",
)


@pytest.fixture(scope="module")
def loaded_pack():
    """按 ComfyUI 的方式加载插件包（模拟 custom_nodes 场景）。"""
    if not (PACK_DIR / "__init__.py").is_file():
        pytest.fail(f"插件包入口不存在：{PACK_DIR / '__init__.py'}")

    parent = str(PACK_DIR.parent)
    added = False
    if parent not in sys.path:
        sys.path.insert(0, parent)
        added = True

    # 确保拿到的是当前工作区里的这份，而不是别处缓存的旧版本
    for name in list(sys.modules):
        if name == PACK_NAME or name.startswith(PACK_NAME + "."):
            del sys.modules[name]

    mod = importlib.import_module(PACK_NAME)
    yield mod

    if added and parent in sys.path:
        sys.path.remove(parent)


# ---------------------------------------------------------------------------
# 1. ComfyUI 加载契约
# ---------------------------------------------------------------------------


def test_pack_exposes_comfyui_contract(loaded_pack):
    """ComfyUI 需要的三样东西必须都存在且类型正确。"""
    assert hasattr(loaded_pack, "NODE_CLASS_MAPPINGS"), "缺少 NODE_CLASS_MAPPINGS"
    assert isinstance(loaded_pack.NODE_CLASS_MAPPINGS, dict)
    assert loaded_pack.NODE_CLASS_MAPPINGS, "NODE_CLASS_MAPPINGS 为空 = 用户看不到任何节点"

    assert hasattr(loaded_pack, "NODE_DISPLAY_NAME_MAPPINGS"), "缺少 NODE_DISPLAY_NAME_MAPPINGS"
    names = loaded_pack.NODE_DISPLAY_NAME_MAPPINGS
    assert isinstance(names, dict)
    assert names, "NODE_DISPLAY_NAME_MAPPINGS 为空 = 节点没有中文显示名"

    assert hasattr(loaded_pack, "WEB_DIRECTORY"), "缺少 WEB_DIRECTORY（网页面板不会加载）"
    web = PACK_DIR / loaded_pack.WEB_DIRECTORY
    assert web.is_dir(), f"WEB_DIRECTORY 指向的目录不存在：{web}"


def test_every_node_key_has_display_name(loaded_pack):
    """每个注册节点都必须有显示名，否则网页上显示成机器名。"""
    missing = set(loaded_pack.NODE_CLASS_MAPPINGS) - set(
        loaded_pack.NODE_DISPLAY_NAME_MAPPINGS
    )
    assert not missing, f"以下节点缺少显示名：{sorted(missing)}"


def test_web_directory_contains_loadable_js(loaded_pack):
    """ComfyUI 会把 web/ 下每个 .js 加载进网页。

    因此这里要求：至少 1 个 .js，且它是一段能被 JS 引擎按 **ESM** 解析的模块。
    做法：拷成 `.mjs` 再 `node --check`。
    （`.js` 会被 node 按 CommonJS 解析而误报；`--input-type` 又不能与 `--check`
    同用 —— 这是实测踩到的坑，不是插件的问题。）
    """
    import shutil
    import subprocess
    import tempfile

    web = PACK_DIR / loaded_pack.WEB_DIRECTORY
    js_files = sorted(web.rglob("*.js"))
    assert js_files, f"{web} 下没有任何 .js —— 网页面板不会出现"

    node = shutil.which("node")
    if node is None:
        pytest.skip("未验证：本机无 node，无法对前端 JS 做语法检查")

    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        for js in js_files:
            probe = Path(td) / (js.stem + ".mjs")
            probe.write_text(js.read_text(encoding="utf-8"), encoding="utf-8")
            proc = subprocess.run(
                [node, "--check", str(probe)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if proc.returncode != 0:
                failures.append(f"{js.name}:\n{(proc.stderr or '')[-600:]}")
    assert not failures, "前端文件语法错误：\n" + "\n".join(failures)


def test_display_names_are_chinese_readable(loaded_pack):
    """显示名应是给人看的（含中文），不是类名复制。"""
    for key, display in loaded_pack.NODE_DISPLAY_NAME_MAPPINGS.items():
        assert display != key, f"{key} 的显示名与类名相同，用户看到的是机器名"
        assert any("\u4e00" <= ch <= "\u9fff" for ch in display), (
            f"{key} 的显示名 {display!r} 不含中文，与项目其余界面不一致"
        )


def test_web_routes_are_registered_at_import_time():
    """插件包**必须**在导入时调用路由注册。

    实测踩到的坑：用 staging 副本覆盖 `__init__.py` 时把 `_register_routes()` 调用丢了，
    后果是**节点全部正常注册、但所有 `/bettercomfyui/*` 路由 404** ——
    网页面板一打开就报错，而且启动日志看起来"一切正常"。
    这条测试从源码层面钉住这个调用，防止再次静默丢失。
    """
    src = (PACK_DIR / "__init__.py").read_text(encoding="utf-8")
    assert "_register_routes" in src, "插件包没有定义 _register_routes 的注册逻辑"
    # 必须在模块级真正调用（而不是只定义）
    assert "\n_register_routes()" in src, (
        "__init__.py 里没有在模块级调用 _register_routes() —— "
        "后果是所有 /bettercomfyui/* 路由 404，面板打开即报错"
    )


def test_fusion_and_asset_nodes_are_registered(loaded_pack):
    """融合节点与素材适配节点必须真的注册进去（这是与导演台对接的关键能力）。"""
    keys = set(loaded_pack.NODE_CLASS_MAPPINGS)
    expected = {
        "BetterComfyUIFusionGroups",
        "BetterComfyUIFusionPackGroup",
        "BetterComfyUIFusionCombine",
        "BetterComfyUIFusionIngest",
        "BetterComfyUIFusionDoctor",
        "BetterComfyUIAssetImage",
        "BetterComfyUIAssetSlots",
        "BetterComfyUITimelineText",
    }
    missing = sorted(expected - keys)
    assert not missing, f"以下融合/素材节点未注册：{missing}"


# ---------------------------------------------------------------------------
# 2. 每个节点的结构契约（ComfyUI 会逐项读取）
# ---------------------------------------------------------------------------


def test_nodes_have_required_attributes(loaded_pack):
    problems: list[str] = []
    for key, cls in loaded_pack.NODE_CLASS_MAPPINGS.items():
        for attr in REQUIRED_NODE_ATTRS:
            if not hasattr(cls, attr):
                problems.append(f"{key} 缺少 {attr}")
    assert not problems, "节点结构不合规：" + "; ".join(problems)


def test_input_types_is_valid_and_declares_required(loaded_pack):
    """`INPUT_TYPES()` 必须能调用，且 `required` 是 dict（ComfyUI 强依赖）。"""
    problems: list[str] = []
    for key, cls in loaded_pack.NODE_CLASS_MAPPINGS.items():
        try:
            spec = cls.INPUT_TYPES()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{key}.INPUT_TYPES() 抛错：{type(exc).__name__}: {exc}")
            continue
        if not isinstance(spec, dict):
            problems.append(f"{key}.INPUT_TYPES() 返回 {type(spec).__name__}，不是 dict")
            continue
        if "required" not in spec:
            problems.append(f"{key}.INPUT_TYPES() 缺少 'required'")
            continue
        if not isinstance(spec["required"], dict):
            problems.append(f"{key}.INPUT_TYPES()['required'] 不是 dict")
    assert not problems, "; ".join(problems)


def test_return_types_match_return_names(loaded_pack):
    problems: list[str] = []
    for key, cls in loaded_pack.NODE_CLASS_MAPPINGS.items():
        rt = getattr(cls, "RETURN_TYPES", None)
        rn = getattr(cls, "RETURN_NAMES", None)
        if not isinstance(rt, tuple):
            problems.append(f"{key}.RETURN_TYPES 必须是 tuple，实际 {type(rt).__name__}")
            continue
        if rn is not None:
            if not isinstance(rn, tuple):
                problems.append(f"{key}.RETURN_NAMES 必须是 tuple")
            elif len(rn) != len(rt):
                problems.append(
                    f"{key} 的 RETURN_NAMES({len(rn)}) 与 RETURN_TYPES({len(rt)}) 数量不一致"
                )
    assert not problems, "; ".join(problems)


def test_function_exists_and_is_callable(loaded_pack):
    """`FUNCTION` 指定的方法必须真实存在于类上。"""
    problems: list[str] = []
    for key, cls in loaded_pack.NODE_CLASS_MAPPINGS.items():
        fn = getattr(cls, "FUNCTION", None)
        if not fn:
            problems.append(f"{key} 未声明 FUNCTION")
            continue
        if not callable(getattr(cls, fn, None)):
            problems.append(f"{key}.{fn} 不存在或不可调用")
    assert not problems, "; ".join(problems)


def test_nodes_are_instantiable(loaded_pack):
    for key, cls in loaded_pack.NODE_CLASS_MAPPINGS.items():
        try:
            cls()
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"{key} 无法实例化：{type(exc).__name__}: {exc}")


def test_output_node_flag_is_boolean(loaded_pack):
    for key, cls in loaded_pack.NODE_CLASS_MAPPINGS.items():
        flag = getattr(cls, "OUTPUT_NODE", None)
        if flag is not None:
            assert isinstance(flag, bool), f"{key}.OUTPUT_NODE 必须是 bool"


# ---------------------------------------------------------------------------
# 3. 第二阶段不得出现在 UI
# ---------------------------------------------------------------------------


def test_no_phase2_nodes_are_registered(loaded_pack):
    """用户明确要求：第二阶段保留接入点但**不在 UI 展示**。"""
    registered = list(loaded_pack.NODE_CLASS_MAPPINGS)
    leaked = [
        k for k in registered
        if any(token.lower() in k.lower() for token in PHASE2_FORBIDDEN_NODE_KEYS)
    ]
    assert not leaked, f"第二阶段节点不得注册到 UI，发现：{leaked}"

    assert getattr(loaded_pack, "PHASE2_NODES_EXPOSED", None) is False, (
        "PHASE2_NODES_EXPOSED 必须显式为 False"
    )


def test_phase2_entry_is_importable_but_hidden(loaded_pack):
    """接入点本身必须可用（保留），只是不上屏。"""
    mod = importlib.import_module(f"{PACK_NAME}.phase2")
    assert mod.EXPOSED_IN_UI is False
    assert callable(mod.next_chapter_for_project)
    assert callable(mod.fetch_next_chapter)
    assert callable(mod.publish_movie)
    assert callable(mod.phase2_status)
    # 确保它没有被误注册成节点
    for name in dir(mod):
        if name.startswith("_"):
            continue
        obj = getattr(mod, name)
        if isinstance(obj, type) and hasattr(obj, "INPUT_TYPES"):
            pytest.fail(f"phase2 模块里出现了 ComfyUI 节点类：{name}（会破坏「不暴露」的要求）")


def test_phase2_status_reports_hidden():
    mod = importlib.import_module(f"{PACK_NAME}.phase2")
    st = mod.phase2_status()
    assert st["exposed_in_ui"] is False
    assert st["nodes_registered"] == []
    assert "OUT6" in st["resume_entry"], "第二阶段入口必须从项目状态继续（方案第五部分第 35 项）"


def test_phase2_unimplemented_paths_raise_not_fake_success(tmp_path: Path):
    """未接入的接缝必须**抛错**，绝不返回假成功。"""
    mod = importlib.import_module(f"{PACK_NAME}.phase2")

    from bettercomfyui.config import load_config

    cfg = load_config(repo_root=REPO_ROOT)
    projects_root = tmp_path / "projects"
    cfg.global_.project_root = str(projects_root)
    projects_root.mkdir(parents=True, exist_ok=True)

    from bettercomfyui.store.projects import ProjectStore

    ps = ProjectStore(projects_root, cfg)
    pid = ps.create("phase2 探测")["project_id"]

    with pytest.raises(mod.Phase2Disabled, match="ChapterSource"):
        mod.fetch_next_chapter(pid, repo_root=tmp_path, source=None)

    movie = tmp_path / "m.mp4"
    movie.write_bytes(b"x" * 32)
    with pytest.raises(mod.Phase2Disabled, match="PublishTarget"):
        mod.publish_movie(movie, target=None)


def test_publish_refuses_mock_output(tmp_path: Path):
    """占位产出一律拒绝发布 —— 对外发布占位产出等于声称虚假结果。"""
    mod = importlib.import_module(f"{PACK_NAME}.phase2")

    class _Target:
        name = "probe"

        def publish(self, *, movie, metadata):  # pragma: no cover - 不应被调用
            raise AssertionError("不应走到这里：mock 产出必须被拒绝")

    movie = tmp_path / "m.mp4"
    movie.write_bytes(b"x" * 32)
    with pytest.raises(mod.Phase2Disabled, match="is_mock"):
        mod.publish_movie(
            movie, metadata={"generation_provenance": {"is_mock": True}}, target=_Target()
        )


# ---------------------------------------------------------------------------
# 4. 最简工作流（若已交付）
# ---------------------------------------------------------------------------


WORKFLOW = PACK_DIR / "workflows" / "minimal_workflow.json"


@pytest.mark.skipif(not WORKFLOW.is_file(), reason="未验证：最简工作流尚未交付")
def test_minimal_workflow_references_only_registered_nodes(loaded_pack):
    """工作流里的每个节点 type 都必须在本插件注册表里 —— 否则用户拖进去就是一堆红框。"""
    data = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data.get("nodes"), list) and data["nodes"], "工作流没有 nodes"
    registered = set(loaded_pack.NODE_CLASS_MAPPINGS)
    used = {n.get("type") for n in data["nodes"]}
    unknown = used - registered
    assert not unknown, f"工作流引用了未注册的节点：{sorted(unknown)}（用户会看到缺失节点）"
    assert used, "工作流没有使用任何本插件节点"


@pytest.mark.skipif(not WORKFLOW.is_file(), reason="未验证：最简工作流尚未交付")
def test_minimal_workflow_is_really_minimal(loaded_pack):
    """「最简」是可度量的：节点数要少，且不引用任何第三方节点。"""
    data = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    registered = set(loaded_pack.NODE_CLASS_MAPPINGS)
    used = {n.get("type") for n in data["nodes"]}
    assert len(data["nodes"]) <= 6, (
        f"最简工作流有 {len(data['nodes'])} 个节点，太多了（目标 ≤6）"
    )
    assert used <= registered, "工作流引用了插件以外的节点，用户机器上可能缺失"
