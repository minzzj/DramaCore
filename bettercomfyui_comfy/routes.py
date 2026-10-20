"""网页面板调用的后端 HTTP 路由。

**为什么要走 HTTP 而不是只用节点**

ComfyUI 的工作流是「拉一次就排队执行」的模型，而本插件需要的是
「点一下、看结果、再点一下」的交互面板。因此面板通过 `PromptServer` 的
aiohttp 应用直接调用后端接口。

**路由全部挂在 `/bettercomfyui/` 前缀下**，不与 ComfyUI 自有路由冲突。

**注册必须是「尽力而为」**：拿不到 `PromptServer.instance` 时（例如被别处 import、
或在没有 ComfyUI 的环境里跑测试），本模块只记录原因并返回 False，**不抛异常** ——
否则会导致整个插件加载失败。但面板会因此拿不到数据，所以在 `LOG` 里写清楚。

**第二阶段路由保留但不被界面调用**（`/bettercomfyui/phase2/status`）。
"""

from __future__ import annotations

import logging
import traceback
from pathlib import Path
from typing import Any

logger = logging.getLogger("bettercomfyui.comfy.routes")

ROUTES_REGISTERED: bool = False
ROUTES_ERROR: str | None = None

PREFIX = "/bettercomfyui"


def _plugin_package_name() -> str:
    """本插件自身的**包名**（`custom_nodes.bettercomfyui_comfy` 或 `bettercomfyui_comfy`）。

    踩过的坑（务必不要再「优化」回去）：

    `__package__` 对包内子模块来说**已经是包名**
    （`routes.py` 位于 `custom_nodes.bettercomfyui_comfy/` 时，
    `__package__ == "custom_nodes.bettercomfyui_comfy"`）。
    再 `rsplit(".", 1)[0]` 会掉成 `custom_nodes`，
    于是 `_mod("nodes")` 去 import `custom_nodes.nodes` -> ModuleNotFoundError。
    这个 bug 在 `tests/test_comfy_plugin_load.py` 里被真实捕获。

    正确做法用 `__name__`：叶子模块 `...bettercomfyui_comfy.routes` -> 去一级；
    若本模块本身就是包（`...bettercomfyui_comfy`）-> 直接用它。
    """
    parts = __name__.rsplit(".", 1)
    if len(parts) == 2 and parts[1] == "routes":
        return parts[0]
    if __package__:
        return __package__
    return Path(__file__).resolve().parent.name


def _plugin_meta():
    """取得插件包自身的 `__version__` / `NODE_CLASS_MAPPINGS`。"""
    import importlib

    return importlib.import_module(_plugin_package_name())


def _orchestrator_helpers():
    """从 nodes 模块借两个路径助手，避免重复实现。"""
    nodes_mod = _mod("nodes")
    return nodes_mod._orch_repo_root, nodes_mod._projects_root  # noqa: SLF001


def _pkgn() -> str:
    """当前插件包名（暴露给测试断言）。"""
    return _plugin_package_name()


def _mod(suffix: str):
    """按当前加载上下文导入插件内的子模块（如 "nodes" / "phase2" / "_bridge"）。"""
    import importlib

    return importlib.import_module(f"{_plugin_package_name()}.{suffix}")


def _sub(mod_name: str, attr: str):
    """导入插件内子模块里的一个名字。"""
    return getattr(_mod(mod_name), attr)


def _json(data: Any, status: int = 200):
    from aiohttp import web

    return web.json_response(data, status=status, dumps=_dumps)


def _dumps(obj: Any, **_kwargs: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, default=str)


def _error(message: str, *, status: int = 400, kind: str = "error"):
    return _json({"error": message, "kind": kind}, status=status)


def _server_compute_capability() -> dict:
    """从**当前 ComfyUI 服务进程**探测计算设备，判断能否真实跑 H3。

    「权重齐全 + 版本够」不等于「能出片」：实测本机权重齐全、ComfyUI 0.41，
    但服务以 `--cpu` 启动、唯一 GPU 只有 4GB 显存 —— 旧版 /doctor 却报
    `real_generation_available=True`，面板显示「真实生成可用：是」，属假成功。

    这里在服务进程里直接问 torch：CUDA 是否可用、显存多大；探测失败一律按
    **不可用**处理（不猜）。
    """
    try:
        from bettercomfyui.pipeline.core import evaluate_compute_devices
    except Exception as exc:  # noqa: BLE001 - 编排包不可用时无法判断
        return {
            "available": False, "device": None, "device_type": None,
            "vram_mib": None, "min_vram_mib": None,
            "reason": f"无法导入编排包计算能力探测（{type(exc).__name__}: {exc}）",
        }
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            devices = [{
                "type": "cuda",
                "name": str(getattr(props, "name", "cuda")),
                "vram_total": int(getattr(props, "total_memory", 0) or 0),
            }]
        else:
            devices = [{"type": "cpu", "name": "cpu", "vram_total": 0}]
    except Exception as exc:  # noqa: BLE001 - 服务进程里 torch 异常时按不可用处理
        result = evaluate_compute_devices(None)
        result["reason"] = (
            f"无法探测服务进程计算设备（torch 不可用：{type(exc).__name__}: {exc}）"
            "—— 真实生成不可用"
        )
        return result
    return evaluate_compute_devices(devices)


# ---------------------------------------------------------------------------
# 处理器
# ---------------------------------------------------------------------------


async def h_doctor(request):  # noqa: ARG001 - aiohttp 处理器签名固定
    pkg = _plugin_meta()          # 插件包本身：取 __version__ 与 NODE_CLASS_MAPPINGS
    describe_bridge = _sub("_bridge", "describe_bridge")

    info = describe_bridge()
    out: dict[str, Any] = {
        # ⚠️ 不要写成裸 `__version__` —— 本模块里并不存在这个名字，
        # 会抛 NameError 让 /bettercomfyui/doctor 返回 500；
        # 而面板打开后的第一个请求恰好就是它。
        # 这个 bug 出现过两次（两次都是被陈旧副本覆盖所致）。
        # 现在由 tests/test_comfy_routes.py::test_route_module_has_no_bare_dunder_version 静态守卫。
        "plugin_version": getattr(pkg, "__version__", None),
        "node_count": len(getattr(pkg, "NODE_CLASS_MAPPINGS", {}) or {}),
        "node_names": sorted((getattr(pkg, "NODE_CLASS_MAPPINGS", {}) or {}).keys()),
        "phase2_exposed_in_ui": False,
        "orchestrator_available": info["available"],
        "orchestrator_error": info["error"] or None,
        "orchestrator_file": info["orchestrator_file"],
        "orchestrator_root": info["orchestrator_root"],
        "python": info["python"],
        "ffmpeg_available": False,
        "ffmpeg": None,
        "ffmpeg_error": None,
        "real_generation_available": False,
        "legacy_comfyui_warning": None,
    }

    # ComfyUI 版本（判断是否是过旧版本）
    try:
        import comfyui_version  # type: ignore

        ver = getattr(comfyui_version, "__version__", None)
        out["comfyui_version"] = ver
        if ver:
            try:
                major, minor = (int(x) for x in str(ver).split(".")[:2])
                if (major, minor) < (0, 30):
                    out["legacy_comfyui_warning"] = (
                        f"ComfyUI {ver} 低于 MiniMaxH3 导演台要求的 0.30.0；"
                        "真实视频生成路径不可用（升级到 ≥0.30，建议 0.41）"
                    )
            except (ValueError, TypeError):
                pass
    except Exception:  # noqa: BLE001
        out["comfyui_version"] = None

    if info["available"]:
        try:
            from importlib import import_module

            cfg_mod = import_module("bettercomfyui.config")
            core = import_module("bettercomfyui.pipeline.core")
            try:
                out["ffmpeg"] = core._ffmpeg_exe()  # noqa: SLF001
                out["ffmpeg_available"] = True
            except Exception as exc:  # noqa: BLE001
                out["ffmpeg_error"] = f"{type(exc).__name__}: {exc}"
            cfg = cfg_mod.load_config(repo_root=Path(info["orchestrator_root"]))
            out["project_root"] = str(cfg.global_.project_root)
        except Exception as exc:  # noqa: BLE001
            out["orchestrator_error"] = f"编排包已导入但初始化失败：{type(exc).__name__}: {exc}"

    # 真实生成可用性：需要 ComfyUI >= 0.30 + **必需类别齐全**。任一不满足都如实报 False。
    #
    # ⚠️ 这里曾经只看 `folder_paths.get_filename_list("checkpoints"/"diffusion_models"/"unet")`
    # 是否非空，导致两个假阳性：
    #   1. 一个**正在下载**的扩散模型（`*.part` 或半截文件）被算成可用；
    #   2. 完全不检查文本编码器与 VAE —— 只有扩散模型也出不了片。
    # 判据统一走 `probe_model_kinds()`（与 `cli.py` 同一份，唯一真源）。
    try:
        import folder_paths  # type: ignore

        from bettercomfyui.pipeline.core import probe_model_kinds

        base = getattr(folder_paths, "base_path", None)
        kinds = probe_model_kinds(Path(base) / "models" if base else None)

        # 用 ComfyUI 自己的清单做交叉核对：两处必须一致，不一致要如实说明
        listed: list[str] = []
        for folder in ("checkpoints", "diffusion_models", "unet"):
            try:
                listed.extend(folder_paths.get_filename_list(folder))
            except Exception:  # noqa: BLE001
                pass

        out["checkpoint_count"] = kinds["total"]
        out["model_kinds_present"] = {k: len(v) for k, v in kinds["present"].items()}
        out["model_kinds_missing"] = kinds["missing"]
        out["comfyui_diffusion_models_listed"] = len(listed)
        if listed and not kinds["present"].get("扩散模型"):
            out["model_probe_warning"] = (
                f"ComfyUI 列出的扩散模型有 {len(listed)} 个，但按必需类别探测没找到 —— "
                "可能是下载未完成（*.part）或目录布局与预期不同，请人工确认。"
            )

        ver = out.get("comfyui_version")
        # 版本比较按数字段（字符串比较会把 '0.5' 判成大于 '0.30'）
        from bettercomfyui.executors.comfyui_http import meets_min_comfyui

        # 权重/版本之外还必须有一块显存足够的加速卡（否则「有文件 ≠ 能出片」）。
        compute = _server_compute_capability()
        out["compute"] = compute
        out["real_generation_available"] = bool(
            kinds["available"]
            and meets_min_comfyui(ver)
            and compute.get("available")
        )
    except Exception as exc:  # noqa: BLE001
        out["checkpoint_count"] = None
        out["real_generation_error"] = str(exc)
        out["real_generation_available"] = False
        out.setdefault("compute", {
            "available": False,
            "reason": "模型/设备探测未完成，按不可用处理",
        })

    return _json(out)


async def h_projects(request):  # noqa: ARG001
    try:
        from importlib import import_module

        _bridge = _mod("_bridge")
        load_orchestrator = _bridge.load_orchestrator
        orchestrator_available = _bridge.orchestrator_available

        ok, err = orchestrator_available()
        if not ok:
            return _error(err, status=503, kind="orchestrator_unavailable")

        load_orchestrator()
        cfg_mod = import_module("bettercomfyui.config")
        proj_mod = import_module("bettercomfyui.store.projects")
        _orch_repo_root = _sub("nodes", "_orch_repo_root")
        _projects_root = _sub("nodes", "_projects_root")

        cfg = cfg_mod.load_config(repo_root=_orch_repo_root())
        ps = proj_mod.ProjectStore(_projects_root(cfg), cfg)
        return _json({"projects": ps.list()})
    except Exception as exc:  # noqa: BLE001
        logger.warning("列出项目失败：%s", traceback.format_exc())
        return _error(f"{type(exc).__name__}: {exc}", status=500)


async def h_project_open(request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _error("请求体必须是 JSON")

    try:
        from importlib import import_module

        _bridge = _mod("_bridge")
        load_orchestrator = _bridge.load_orchestrator
        orchestrator_available = _bridge.orchestrator_available

        ok, err = orchestrator_available()
        if not ok:
            return _error(err, status=503, kind="orchestrator_unavailable")
        load_orchestrator()

        cfg_mod = import_module("bettercomfyui.config")
        proj_mod = import_module("bettercomfyui.store.projects")
        _orch_repo_root = _sub("nodes", "_orch_repo_root")
        _projects_root = _sub("nodes", "_projects_root")

        cfg = cfg_mod.load_config(repo_root=_orch_repo_root())
        ps = proj_mod.ProjectStore(_projects_root(cfg), cfg)

        pid = str(body.get("project_id") or "").strip()
        name = str(body.get("project_name") or "").strip()
        if pid:
            info = ps.open(pid)
        elif name:
            existing = [r for r in ps.list() if r.get("display_name") == name]
            if existing:
                pid = existing[0]["project_id"]
                info = ps.open(pid)
            else:
                info = ps.create(name)
                pid = info["project_id"]
        else:
            return _error("project_id 与 project_name 至少填一个")

        return _json({
            "project_id": pid,
            "display_name": info.get("display_name"),
            "created_at": info.get("created_at"),
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("打开/创建项目失败：%s", traceback.format_exc())
        return _error(f"{type(exc).__name__}: {exc}", status=500)


async def h_run(request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _error("请求体必须是 JSON")

    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return _error("缺少 project_id")
    script = body.get("script")
    executor = str(body.get("executor") or "mock")
    if executor not in ("mock", "comfyui"):
        return _error(f"executor 只能是 mock 或 comfyui，收到 {executor!r}")
    if executor != "mock":
        # 本一键闭环只有 ffmpeg 占位媒体一条产出路径，没有真实生成执行器。
        # 若继续调用 run_demo，会被它失败闭合（ok=False）；这里直接给出可操作错误，
        # 避免前端误以为「真实 ComfyUI 已开始生成」。
        return _error(
            "真实 ComfyUI 执行器尚未接入本一键闭环：本接口只产出 ffmpeg 占位媒体，"
            "继续会构成「占位产出冒充真实生成」。真实出片请使用导演台/融合工作流"
            "（S2_单节点出片（GPU）/ S3_素材生成入库审核（GPU）/ W3_全流程出片 / "
            "00_【总流程】剧本→成片（GPU））。",
            status=400,
            kind="real_executor_not_wired",
        )

    try:
        import json as _jsonlib
        import tempfile
        from importlib import import_module

        _bridge = _mod("_bridge")
        load_orchestrator = _bridge.load_orchestrator
        orchestrator_available = _bridge.orchestrator_available

        ok, err = orchestrator_available()
        if not ok:
            return _error(err, status=503, kind="orchestrator_unavailable")
        load_orchestrator()

        cfg_mod = import_module("bettercomfyui.config")
        demo = import_module("bettercomfyui.demo")
        _orch_repo_root = _sub("nodes", "_orch_repo_root")

        repo_root = _orch_repo_root()
        cfg = cfg_mod.load_config(repo_root=repo_root)

        chapter_path = None
        tmp_path = None
        if isinstance(script, dict) and script:
            fd, tmp_name = tempfile.mkstemp(suffix=".json", prefix="bcu_script_")
            Path(tmp_name).write_text(
                _jsonlib.dumps(script, ensure_ascii=False), encoding="utf-8"
            )
            import os as _os

            _os.close(fd)
            chapter_path = Path(tmp_name)
            tmp_path = chapter_path

        try:
            report = demo.run_demo(
                repo_root=repo_root,
                config=cfg,
                project=project_id,
                chapter=chapter_path,
                executor=executor,
            )
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

        return _json({"report": report})
    except Exception as exc:  # noqa: BLE001
        logger.warning("运行失败：%s", traceback.format_exc())
        return _error(f"{type(exc).__name__}: {exc}", status=500)


async def h_minimal_workflow(request):  # noqa: ARG001
    """返回插件自带的最简工作流，供面板一键载入。"""
    try:
        import json

        wf_path = Path(__file__).resolve().parent / "workflows" / "minimal_workflow.json"
        if not wf_path.is_file():
            return _error(
                f"插件内未找到最简工作流：{wf_path}。"
                "请确认插件目录完整（含 workflows/minimal_workflow.json）。",
                status=404,
            )
        return _json(json.loads(wf_path.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001
        return _error(f"{type(exc).__name__}: {exc}", status=500)


async def h_phase2_status(request):  # noqa: ARG001
    """第二阶段接入点状态。

    ⚠️ **界面不调用此路由**（用户要求第二阶段不在 UI 展示）。
    保留它用于后端/运维查询，以及将来启用 UI 时的现成接口。
    """
    try:
        phase2_status = _sub("phase2", "phase2_status")

        return _json({"phase2": phase2_status(), "exposed_in_ui": False})
    except Exception as exc:  # noqa: BLE001
        return _error(f"{type(exc).__name__}: {exc}", status=500)



# ---------------------------------------------------------------------------
# 剧核 / DramaCore 面板配置（API + 创作默认值 + 多语言 + 默认模型 + 连接测试）
# ---------------------------------------------------------------------------
#
# 用户原话：「现在没有填 API 信息之类，信息一定要全、全自主化」——
# 所以这里是**一个接口覆盖面板全部配置**，而不是零散字段：
#
#   * API Key / 模型名 / API Base / API Key 环境变量  -> 复用 api_settings
#     （它已经处理「密钥遮罩、不回传明文、同步剧本巨人、白名单字段」）；
#   * 默认系统提示词 / 输出语言 / 界面语言 / 默认视频模型 / 默认图像模型
#     -> 写 config/user.yaml 的 prompt.system_prompt_template 与新增的 ui.* 段；
#   * 连接测试走**服务端**：浏览器直连第三方 API 会被 CORS 拦截，
#     用户会误以为"密钥不对"，所以由 ComfyUI 服务进程代发请求。
#
# 所有读接口都**不返回完整密钥**（复用 mask_secret）。

BRAND_ZH = "剧核"
BRAND_EN = "DramaCore"

#: 模型下拉候选的推荐值（扫描 ComfyUI models 目录之外给一个可用的起步选项）。
PANEL_CURATED_VIDEO_MODELS = (
    "minimax_h3_t2v_turbo.json",
    "minimax_h3_ref2va",
    "minimax_h3_fl2va",
)
PANEL_CURATED_IMAGE_MODELS = (
    "sd_xl_base_1.0.safetensors",
    "RealVisXL_V5.0_fp16.safetensors",
    "animagine-xl-4.0",
)

#: 面板可写的 ui.* 字段（白名单）。
UI_ALLOWED_FIELDS = ("language", "default_video_model", "default_image_model")

#: 面板可写的 prompt.* 扩展字段（api_settings 白名单之外的）。
PROMPT_EXTRA_FIELDS = ("system_prompt_template",)


def _panel_repo_root() -> Path:
    """仓库根（包含 config/user.yaml 与 bettercomfyui/ 编排包的那个目录）。"""
    try:
        from . import api_settings as _api_settings  # noqa: PLC0415

        return Path(_api_settings.REPO_ROOT)
    except Exception:  # noqa: BLE001 - 独立测试环境可能没有编排包
        return Path(__file__).resolve().parent.parent


def _panel_user_yaml() -> Path:
    return _panel_repo_root() / "config" / "user.yaml"


def _load_panel_user_yaml() -> dict:
    """读 config/user.yaml；文件不存在返回空字典。**解析失败必须抛错**（不覆盖）。"""
    import yaml  # noqa: PLC0415

    path = _panel_user_yaml()
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("user.yaml 顶层不是字典")
    return data


def _write_panel_user_yaml(data: dict) -> None:
    import yaml  # noqa: PLC0415

    path = _panel_user_yaml()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _read_panel_extended() -> dict:
    """读取面板「创作默认值」（api_settings 之外的字段）。"""
    out = {
        "system_prompt": "",
        "output_language": "zh",
        "ui_language": "auto",
        "video_model": "",
        "image_model": "",
    }
    try:
        data = _load_panel_user_yaml()
        prompt = data.get("prompt") or {}
        ui = data.get("ui") or {}
        if isinstance(prompt, dict):
            out["system_prompt"] = str(prompt.get("system_prompt_template") or "")
            lang = str(prompt.get("output_language") or "zh").lower()
            out["output_language"] = lang if lang in ("zh", "en") else "zh"
        if isinstance(ui, dict):
            ui_lang = str(ui.get("language") or "auto").lower()
            out["ui_language"] = ui_lang if ui_lang in ("auto", "zh", "en") else "auto"
            out["video_model"] = str(ui.get("default_video_model") or "")
            out["image_model"] = str(ui.get("default_image_model") or "")
    except Exception as exc:  # noqa: BLE001 - 读取失败要如实显示，不静默
        out["_error"] = f"{type(exc).__name__}: {exc}"
    return out


def _panel_model_options() -> dict:
    """扫描 ComfyUI 的 models 目录，给面板模型下拉用；扫不到就只给推荐值。"""
    video: list[str] = []
    image: list[str] = []
    try:
        import folder_paths  # type: ignore  # noqa: PLC0415

        for folder in ("diffusion_models", "unet", "checkpoints"):
            try:
                video.extend(str(x) for x in folder_paths.get_filename_list(folder))
            except Exception:  # noqa: BLE001 - 目录不存在就算了
                pass
        for folder in ("checkpoints", "loras"):
            try:
                image.extend(str(x) for x in folder_paths.get_filename_list(folder))
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001 - 非 ComfyUI 环境
        pass

    def _dedupe(values, limit=80):
        seen: set[str] = set()
        out: list[str] = []
        for value in values:
            text = str(value or "").strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
            if len(out) >= limit:
                break
        return out

    return {
        "video_models": _dedupe([*PANEL_CURATED_VIDEO_MODELS, *video]),
        "image_models": _dedupe([*PANEL_CURATED_IMAGE_MODELS, *image]),
    }


def _panel_snapshot() -> dict:
    """面板读取接口的完整快照（密钥已遮罩）。"""
    api: dict[str, Any] = {
        "configured": False, "masked": "", "source": "none",
        "endpoint": "", "model": "", "provider": "",
    }
    try:
        from . import api_settings as _api_settings  # noqa: PLC0415

        snap = _api_settings.settings_snapshot()
        if snap.get("ok"):
            api.update({
                "configured": bool(snap.get("api_key_configured")),
                "masked": snap.get("api_key_masked") or "",
                "source": snap.get("api_key_source") or "none",
                "endpoint": snap.get("endpoint") or "",
                "model": snap.get("model") or "",
                "provider": snap.get("provider") or "",
            })
        else:
            api["error"] = snap.get("error")
    except Exception as exc:  # noqa: BLE001 - 编排包不可用时面板仍要能打开
        api["error"] = f"{type(exc).__name__}: {exc}"
    return {
        "ok": True,
        "brand": {"zh": BRAND_ZH, "en": BRAND_EN},
        "api": api,
        "defaults": _read_panel_extended(),
        "options": _panel_model_options(),
        "paths": {"user_yaml": "config/user.yaml"},
    }


async def h_panel_config_get(request):  # noqa: ARG001 - aiohttp 签名固定
    """GET `/bettercomfyui/panel/config` —— 面板全部配置（密钥遮罩）。"""
    try:
        return _json(_panel_snapshot())
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取面板配置失败：%s", traceback.format_exc())
        return _error(f"{type(exc).__name__}: {exc}", status=500)


def _request_is_loopback(request) -> bool:
    remote = str(getattr(request, "remote", "") or "").strip()
    if not remote:
        return True  # 测试/fake request：没有远端信息时按本机处理
    return remote.split("%")[0] in ("127.0.0.1", "::1", "localhost")


def _panel_sensitive_guard(request):
    """敏感面板接口的本机/同源保护（ROUND58 安全修复）。

    ComfyUI 默认无登录；若按文档用 --listen 0.0.0.0，同网段任何人都能改
    endpoint / api_key_env 或触发带密钥探测。这里：
    - 只允许本机请求（可用 BETTERCOMFYUI_ALLOW_REMOTE_PANEL=1 显式放开，默认拒绝）；
    - 有 Origin/Referer 时必须是同源，挡住恶意网页 CSRF。
    """
    import os
    from urllib.parse import urlparse

    if str(os.environ.get("BETTERCOMFYUI_ALLOW_REMOTE_PANEL") or "").lower() in ("1", "true", "yes"):
        return None
    if not _request_is_loopback(request):
        return _json({
            "ok": False,
            "error": "为安全起见，面板配置/连接测试只允许本机访问；"
                     "远程使用请通过带认证的反向代理，或显式设置 "
                     "BETTERCOMFYUI_ALLOW_REMOTE_PANEL=1（不推荐）。",
        }, status=403)
    origin = str(request.headers.get("Origin") or request.headers.get("Referer") or "").strip()
    if origin:
        host = str(request.headers.get("Host") or "").strip()
        netloc = urlparse(origin).netloc
        if host and netloc and netloc != host:
            return _json({"ok": False, "error": "跨站请求被拒绝（Origin/Host 不一致）"}, status=403)
    return None


async def h_panel_config_save(request):
    """POST `/bettercomfyui/panel/config` —— 保存面板全部配置。"""
    denied = _panel_sensitive_guard(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        return _json({"ok": False, "error": f"请求体不是合法 JSON：{exc}"}, status=400)
    if not isinstance(body, dict):
        return _json({"ok": False, "error": "请求体必须是 JSON 对象"}, status=400)

    written: list[str] = []

    # 1) API 字段：复用 api_settings（遮罩、白名单、剧本巨人同步都在里面）
    api_fields = (
        "api_key", "model_name", "endpoint", "model_provider",
        "api_key_env", "output_format",
    )
    env_name = str(body.get("api_key_env") or "").strip()
    if env_name and env_name not in ("BETTERCOMFYUI_API_KEY", "DEEPSEEK_API_KEY"):
        return _json({
            "ok": False,
            "error": "api_key_env 只允许 BETTERCOMFYUI_API_KEY / DEEPSEEK_API_KEY；"
                     "不允许通过 HTTP 指定任意环境变量（防止外带进程环境变量）。",
        }, status=400)
    api_payload = {key: body[key] for key in api_fields if key in body}
    if api_payload:
        try:
            from . import api_settings as _api_settings  # noqa: PLC0415

            result = _api_settings.save_user_settings({"prompt": api_payload})
            if not result.get("ok"):
                return _json(
                    {"ok": False, "error": result.get("error") or "保存 API 配置失败"},
                    status=500,
                )
            written.extend(result.get("written") or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("保存面板 API 配置失败：%s", traceback.format_exc())
            return _json(
                {"ok": False, "error": f"保存 API 配置异常：{type(exc).__name__}: {exc}"},
                status=500,
            )

    # 2) 扩展字段：默认系统提示词 / 语言 / 默认模型（写 user.yaml，保留其它键）
    try:
        data = _load_panel_user_yaml()
    except Exception as exc:  # noqa: BLE001 - **不覆盖**解析失败的配置
        return _json(
            {"ok": False, "error": f"user.yaml 解析失败（未覆盖，避免毁配置）：{type(exc).__name__}: {exc}"},
            status=500,
        )

    changed = False
    prompt = data.get("prompt")
    if not isinstance(prompt, dict):
        prompt = {}
        data["prompt"] = prompt

    if "system_prompt" in body:
        value = str(body.get("system_prompt") or "").strip()
        # ROUND58：精简版 12,912 字 / 历史完整版 41,966 字都必须能整段保存；上限放宽到 200k。
        if len(value) > 200_000:
            return _json({"ok": False, "error": "默认系统提示词过长（上限 200000 字符）"}, status=400)
        prompt["system_prompt_template"] = value
        written.append("prompt.system_prompt_template")
        changed = True

    if "output_language" in body:
        lang = str(body.get("output_language") or "").strip().lower()
        if lang not in ("zh", "en"):
            return _json({"ok": False, "error": "output_language 只能是 zh 或 en"}, status=400)
        prompt["output_language"] = lang
        written.append("prompt.output_language")
        changed = True

    ui = data.get("ui")
    if not isinstance(ui, dict):
        ui = {}
        data["ui"] = ui

    if "ui_language" in body:
        lang = str(body.get("ui_language") or "").strip().lower()
        if lang not in ("auto", "zh", "en"):
            return _json({"ok": False, "error": "ui_language 只能是 auto / zh / en"}, status=400)
        ui["language"] = lang
        written.append("ui.language")
        changed = True

    for js_key, yaml_key in (
        ("video_model", "default_video_model"),
        ("image_model", "default_image_model"),
    ):
        if js_key not in body:
            continue
        value = str(body.get(js_key) or "").strip()
        if len(value) > 200:
            return _json({"ok": False, "error": f"{js_key} 过长（上限 200 字符）"}, status=400)
        ui[yaml_key] = value
        written.append(f"ui.{yaml_key}")
        changed = True

    if changed:
        try:
            _write_panel_user_yaml(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("写 user.yaml 失败：%s", traceback.format_exc())
            return _json(
                {"ok": False, "error": f"写 user.yaml 失败：{type(exc).__name__}: {exc}"},
                status=500,
            )

    snapshot = _panel_snapshot()
    snapshot["saved"] = {"written": written, "path": "config/user.yaml"}
    return _json(snapshot)


def _panel_configured_endpoint() -> str:
    """返回面板/配置里已保存的 endpoint（用于判断"请求体里的 endpoint 是不是用户新填的"）。"""
    try:
        from . import api_settings as _api_settings  # noqa: PLC0415

        snap = _api_settings.settings_snapshot()
        endpoint = str(snap.get("endpoint") or "").strip()
    except Exception:  # noqa: BLE001
        endpoint = ""
    return (endpoint or "https://api.deepseek.com").rstrip("/")


def _panel_endpoint_blocked(endpoint: str) -> str:
    """阻断云厂商元数据/明显内网元数据地址（SSRF 防护；本地 LLM 仍允许）。"""
    try:
        from urllib.parse import urlparse

        host = (urlparse(endpoint).hostname or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""
    if host in {"169.254.169.254", "metadata.google.internal",
                "metadata", "100.100.100.200"}:
        return f"出于安全考虑，不允许测试云元数据地址：{host}"
    return ""


def _panel_resolve_endpoint(payload: dict) -> str:
    endpoint = str(payload.get("endpoint") or "").strip()
    if not endpoint:
        try:
            from . import api_settings as _api_settings  # noqa: PLC0415

            snap = _api_settings.settings_snapshot()
            endpoint = str(snap.get("endpoint") or "").strip()
        except Exception:  # noqa: BLE001
            endpoint = ""
    if not endpoint:
        endpoint = "https://api.deepseek.com"
    return endpoint.rstrip("/")


def _panel_resolve_key(payload: dict) -> tuple[str, str]:
    """返回 (密钥, 来源)。密钥优先请求体（面板刚填的），其次环境变量/配置文件。"""
    key = str(payload.get("api_key") or "").strip()
    if key:
        return key, "request"
    try:
        import os
        from . import api_settings as _api_settings  # noqa: PLC0415

        snap = _api_settings.settings_snapshot()
        env_name = str(snap.get("api_key_env") or _api_settings.DEFAULT_ENV_NAME)
        env_value = os.environ.get(env_name)
        if env_value:
            return env_value, "env"
    except Exception:  # noqa: BLE001
        pass
    try:
        data = _load_panel_user_yaml()
        prompt = data.get("prompt") or {}
        value = str(prompt.get("api_key") or "").strip()
        if value:
            return value, "config"
    except Exception:  # noqa: BLE001
        pass
    return "", "none"


def _panel_resolve_model(payload: dict) -> str:
    model = str(payload.get("model_name") or payload.get("model") or "").strip()
    if model:
        return model
    try:
        data = _load_panel_user_yaml()
        prompt = data.get("prompt") or {}
        model = str(prompt.get("model_name") or "").strip()
    except Exception:  # noqa: BLE001
        model = ""
    return model or "deepseek-chat"


async def _panel_test_chat(session, endpoint: str, headers: dict, model: str, started: float):
    """`/models` 不存在时退化为一次最小的 chat/completions 探测。"""
    import time  # noqa: PLC0415

    url = f"{endpoint}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    try:
        async with session.post(url, headers=headers, json=payload,
                                allow_redirects=False) as resp:
            latency = int((time.monotonic() - started) * 1000)
            text = await resp.text()
            if resp.status == 200:
                return _json({
                    "ok": True, "latency_ms": latency, "status": 200,
                    "models_count": 1, "models_sample": [model],
                    "endpoint": endpoint, "method": "chat_completions",
                })
            return _json({
                "ok": False, "status": resp.status, "latency_ms": latency,
                "endpoint": endpoint,
                "error": f"接口返回 HTTP {resp.status}：{text[:300]}",
            })
    except Exception as exc:  # noqa: BLE001
        return _json({
            "ok": False, "endpoint": endpoint,
            "error": f"请求失败：{type(exc).__name__}: {exc}",
        })


async def h_panel_test_connection(request):
    """POST `/bettercomfyui/panel/test` —— 由服务端代发请求测试 API 是否真的可用。

    为什么不在浏览器里直接请求第三方 API：会被 CORS 拦，用户看到"失败"会误判成
    密钥写错。服务端探测能给出**真实**的 HTTP 状态、延迟与模型数量。
    """
    denied = _panel_sensitive_guard(request)
    if denied is not None:
        return denied
    import time  # noqa: PLC0415

    from aiohttp import ClientSession, ClientTimeout  # noqa: PLC0415

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}

    endpoint = _panel_resolve_endpoint(body)
    if not (endpoint.startswith("http://") or endpoint.startswith("https://")):
        return _json({
            "ok": False, "endpoint": endpoint,
            "error": f"API Base 必须是 http(s) 地址，收到 {endpoint!r}",
        })
    blocked = _panel_endpoint_blocked(endpoint)
    if blocked:
        return _json({"ok": False, "endpoint": endpoint, "error": blocked}, status=400)

    # ROUND58 安全修复：**绝不把已保存的 API Key 发送到请求体临时指定的 endpoint**。
    # 只有 endpoint 与面板已配置的一致（或请求体显式自带 key）时才允许带密钥探测；
    # 否则恶意页面/同网段请求可把 /panel/test 当 SSRF + 凭据外带通道。
    body_endpoint = str(body.get("endpoint") or "").strip().rstrip("/")
    configured_endpoint = _panel_configured_endpoint()
    custom_endpoint = bool(body_endpoint and body_endpoint != configured_endpoint)
    if custom_endpoint:
        key = str(body.get("api_key") or "").strip()
        key_source = "request" if key else "blocked"
        if not key:
            return _json({
                "ok": False, "endpoint": endpoint, "key_source": key_source,
                "error": "测试未配置过的自定义 API Base 时必须显式填写 API Key；"
                         "为防密钥外带，服务端不会把已保存的 Key 发送到该地址。",
            }, status=400)
    else:
        key, key_source = _panel_resolve_key(body)
    model = _panel_resolve_model(body)
    if not key:
        return _json({
            "ok": False, "endpoint": endpoint, "key_source": key_source,
            "error": "尚未配置 API Key：请在面板填写，或设置环境变量 "
                     "BETTERCOMFYUI_API_KEY（优先级最高）",
        })

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    started = time.monotonic()
    url_models = f"{endpoint}/models"
    try:
        timeout = ClientTimeout(total=20)
        async with ClientSession(timeout=timeout) as session:
            async with session.get(url_models, headers=headers,
                                   allow_redirects=False) as resp:
                latency = int((time.monotonic() - started) * 1000)
                text = await resp.text()
                if resp.status == 200:
                    models: list[str] = []
                    try:
                        import json as _jsonlib  # noqa: PLC0415

                        data = _jsonlib.loads(text)
                        raw = data.get("data") if isinstance(data, dict) else data
                        if isinstance(raw, list):
                            models = [
                                str(item.get("id") if isinstance(item, dict) else item)
                                for item in raw
                            ]
                    except Exception:  # noqa: BLE001 - 返回体不是标准 JSON 也照实报告
                        pass
                    return _json({
                        "ok": True, "latency_ms": latency, "status": 200,
                        "models_count": len(models),
                        "models_sample": models[:10],
                        "endpoint": endpoint, "key_source": key_source,
                        "method": "models",
                    })
                if resp.status in (401, 403):
                    return _json({
                        "ok": False, "status": resp.status, "latency_ms": latency,
                        "endpoint": endpoint, "key_source": key_source,
                        "error": f"密钥无效或没有权限（HTTP {resp.status}）",
                    })
                if resp.status == 404:
                    # 有的兼容接口没有 /models，用最小 chat 请求再探一次
                    return await _panel_test_chat(session, endpoint, headers, model, started)
                return _json({
                    "ok": False, "status": resp.status, "latency_ms": latency,
                    "endpoint": endpoint, "key_source": key_source,
                    "error": f"接口返回 HTTP {resp.status}：{text[:300]}",
                })
    except Exception as exc:  # noqa: BLE001 - 网络错误如实返回，不吞
        return _json({
            "ok": False, "endpoint": endpoint, "key_source": key_source,
            "error": f"请求失败：{type(exc).__name__}: {exc}",
            "hint": "检查 API Base 是否能从**服务器**访问（不要写 localhost，除非 API 与服务同机）",
        })




# ---------------------------------------------------------------------------
# 剧核 / DramaCore 项目上下文（只读）：项目列表 / 资产+提示词+成片 / 资产预览
# ---------------------------------------------------------------------------
#
# 用户需求：「节点顶部能选项目；选项目自动加载该项目已有资产图/提示词/成片清单；
# 输入新剧本时继续用该项目素材库，避免章节切换人物漂移」。
# 这里只读项目目录，不写、不锁、不触发执行 —— 写路径仍由 ProjectStore / 节点负责。

PROJECT_ID_RE = __import__("re").compile(r"^[0-9a-f]{32}$")
PREVIEW_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
PREVIEW_CONTENT_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}


def _panel_projects_root() -> Path:
    """项目根目录：BETTERCOMFYUI_HOME/projects 优先，其次编排包配置，最后仓库 projects/。"""
    import os  # noqa: PLC0415

    env_home = os.environ.get("BETTERCOMFYUI_HOME")
    if env_home:
        candidate = Path(env_home) / "projects"
        if candidate.is_dir():
            return candidate
    try:
        from importlib import import_module  # noqa: PLC0415

        _bridge = _mod("_bridge")
        ok, _err = _bridge.orchestrator_available()
        if ok:
            _bridge.load_orchestrator()
            cfg_mod = import_module("bettercomfyui.config")
            cfg = cfg_mod.load_config(repo_root=_sub("nodes", "_orch_repo_root")())
            return Path(_sub("nodes", "_projects_root")(cfg))
    except Exception:  # noqa: BLE001 - 编排包不可用时退回仓库根
        pass
    return Path(__file__).resolve().parent.parent / "projects"


def _safe_read_json(path: Path) -> dict:
    """只读 JSON；坏文件返回空 dict（面板展示不因单个坏文件整体失败）。"""
    import json as _jsonlib  # noqa: PLC0415

    try:
        data = _jsonlib.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _project_asset_rows(project_dir: Path, project_meta: dict) -> list[dict]:
    """从项目 meta.json 的 assets[] 取资产当前版本（分数/状态/预览相对路径）。"""
    rows: list[dict] = []
    assets = project_meta.get("assets")
    if not isinstance(assets, list):
        assets = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        current = str(asset.get("current_version") or "")
        versions = asset.get("versions")
        version = None
        if isinstance(versions, list):
            for item in versions:
                if isinstance(item, dict) and str(item.get("version")) == current:
                    version = item
                    break
            if version is None and versions and isinstance(versions[-1], dict):
                version = versions[-1]
        version = version or {}
        extra = version.get("extra") if isinstance(version.get("extra"), dict) else {}
        rows.append({
            "asset_id": str(asset.get("asset_id") or ""),
            "name": str(asset.get("name") or asset.get("normalized_name") or ""),
            "asset_type": str(asset.get("asset_type") or ""),
            "version": str(version.get("version") or current),
            "status": str(version.get("lock_status") or version.get("status") or "UNKNOWN"),
            "review_state": str(version.get("extra", {}).get("review_state")
                                if isinstance(version.get("extra"), dict) else ""),
            "score": extra.get("score"),
            "reason": str(extra.get("reason") or ""),
            "file_rel": str(version.get("file_path") or ""),
        })
    return rows


def _project_context(project_id: str) -> dict | None:
    """读取单个项目的只读上下文（资产/提示词/成片）。项目不存在返回 None。"""
    if not PROJECT_ID_RE.match(project_id or ""):
        return None
    project_dir = _panel_projects_root() / project_id
    project_json = project_dir / "project.json"
    if not project_json.is_file():
        return None
    project_meta = _safe_read_json(project_json)
    meta_index = _safe_read_json(project_dir / "meta.json")
    if not project_meta.get("assets") and isinstance(meta_index.get("assets"), list):
        project_meta = dict(project_meta, assets=meta_index["assets"])

    assets = _project_asset_rows(project_dir, project_meta)

    # 提示词：优先最新 one_stop 报告里的 prompts.segments；其次 DIRECTOR_PLAN 检查点的段清单
    prompts: list[str] = []
    review_dir = project_dir / "review"
    try:
        reports = sorted(
            (p for p in review_dir.glob("one_stop_*.json") if p.is_file()),
            key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:  # noqa: BLE001
        reports = []
    if reports:
        report = _safe_read_json(reports[0])
        segs = (report.get("prompts") or {}).get("segments") if isinstance(report.get("prompts"), dict) else None
        if isinstance(segs, list):
            prompts = [str(s) for s in segs]
        if not assets and isinstance(report.get("assets"), dict):
            assets = report["assets"].get("rows") or []
    if not prompts:
        plan = _safe_read_json(project_dir / "checkpoints" / "DIRECTOR_PLAN.1.json")
        payload = plan.get("payload") if isinstance(plan.get("payload"), dict) else {}
        seg_ids = payload.get("segments")
        if isinstance(seg_ids, list):
            prompts = [f"（段 {i + 1}：{sid}）" for i, sid in enumerate(seg_ids)]

    # 成片：output/ 下 mp4/mov/webm（最新在前，最多 12 条）
    films: list[dict] = []
    output_dir = project_dir / "output"
    try:
        files = [
            p for p in output_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in (".mp4", ".mov", ".webm")
        ]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files[:12]:
            try:
                rel = path.relative_to(project_dir).as_posix()
                stat = path.stat()
            except Exception:  # noqa: BLE001
                continue
            films.append({"rel": rel, "name": path.name,
                          "size": int(stat.st_size), "mtime": int(stat.st_mtime)})
    except Exception:  # noqa: BLE001
        pass

    locked = sum(1 for row in assets
                 if str(row.get("status")).upper() in ("LOCKED", "ALLOWED"))
    return {
        "ok": True,
        "project": {
            "project_id": project_id,
            "display_name": str(project_meta.get("display_name") or project_id),
            "updated_at": str(project_meta.get("updated_at") or ""),
            "created_at": str(project_meta.get("created_at") or ""),
        },
        "counts": {
            "assets": len(assets),
            "locked": locked,
            "prompts": len(prompts),
            "films": len(films),
        },
        "assets": assets,
        "prompts": prompts[:200],
        "films": films,
    }


def _project_summary(project_dir: Path) -> dict | None:
    """列表页用的轻量摘要（不读全部资产 meta，只读 project.json + 目录扫描）。"""
    project_json = project_dir / "project.json"
    if not project_json.is_file():
        return None
    project_meta = _safe_read_json(project_json)
    meta_index = _safe_read_json(project_dir / "meta.json")
    assets = project_meta.get("assets")
    if not isinstance(assets, list) or not assets:
        assets = meta_index.get("assets") if isinstance(meta_index.get("assets"), list) else []
    locked = 0
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        current = str(asset.get("current_version") or "")
        for version in asset.get("versions") or []:
            if isinstance(version, dict) and str(version.get("version")) == current:
                if str(version.get("lock_status") or "").upper() in ("LOCKED", "ALLOWED"):
                    locked += 1
                break
    prompts = 0
    plan = _safe_read_json(project_dir / "checkpoints" / "DIRECTOR_PLAN.1.json")
    payload = plan.get("payload") if isinstance(plan.get("payload"), dict) else {}
    if isinstance(payload.get("segments"), list):
        prompts = len(payload["segments"])
    films = 0
    try:
        films = sum(
            1 for p in (project_dir / "output").rglob("*")
            if p.is_file() and p.suffix.lower() in (".mp4", ".mov", ".webm")
        )
    except Exception:  # noqa: BLE001
        pass
    updated = str(project_meta.get("updated_at") or "")
    if not updated:
        try:
            updated = str(project_json.stat().st_mtime)
        except Exception:  # noqa: BLE001
            updated = ""
    return {
        "project_id": str(project_meta.get("project_id") or project_dir.name),
        "display_name": str(project_meta.get("display_name") or project_dir.name),
        "assets": len(assets),
        "locked": locked,
        "prompts": prompts,
        "films": films,
        "updated_at": updated,
        "status": str(project_meta.get("status") or "idle"),
    }


async def h_projects_list(request):  # noqa: ARG001 - aiohttp 签名固定
    """GET `/bettercomfyui/projects/list` —— 已有项目列表（只读扫描 projects/*/project.json）。"""
    try:
        root = _panel_projects_root()
        entries: list[dict] = []
        if root.is_dir():
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                summary = _project_summary(child)
                if summary:
                    entries.append(summary)
        entries.sort(key=lambda e: str(e.get("updated_at") or ""), reverse=True)
        return _json({
            "ok": True,
            "root": str(root),
            "count": len(entries),
            "projects": entries[:200],
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("扫描项目列表失败：%s", traceback.format_exc())
        return _error(f"{type(exc).__name__}: {exc}", status=500)


async def h_project_context(request):
    """GET `/bettercomfyui/project/context?project_id=` —— 项目已有资产/提示词/成片（只读）。"""
    project_id = str(request.query.get("project_id") or "").strip().lower()
    try:
        data = _project_context(project_id)
        if data is None:
            return _error("项目不存在或 project_id 非法（需 32 位小写 hex）", status=404,
                          kind="project_not_found")
        return _json(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取项目上下文失败：%s", traceback.format_exc())
        return _error(f"{type(exc).__name__}: {exc}", status=500)


async def h_asset_preview(request):
    """GET `/bettercomfyui/asset/preview?project_id=&rel=` —— 只读返回项目资产图预览。"""
    from aiohttp import web  # noqa: PLC0415

    project_id = str(request.query.get("project_id") or "").strip().lower()
    rel = str(request.query.get("rel") or "").strip()
    if not PROJECT_ID_RE.match(project_id) or not rel:
        return _error("project_id / rel 必填且需合法", status=400)
    project_dir = _panel_projects_root() / project_id
    try:
        target = (project_dir / rel).resolve()
        project_resolved = project_dir.resolve()
        if project_resolved not in target.parents:
            return _error("预览路径越界（只允许项目目录内文件）", status=403)
        if target.suffix.lower() not in PREVIEW_SUFFIXES:
            return _error(f"不支持的预览类型：{target.suffix}", status=415)
        if not target.is_file():
            return _error("预览文件不存在", status=404)
        return web.FileResponse(
            path=target,
            headers={"Content-Type": PREVIEW_CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream"),
                     "Cache-Control": "no-store"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("资产预览失败：%s", traceback.format_exc())
        return _error(f"{type(exc).__name__}: {exc}", status=500)




# ---------------------------------------------------------------------------
# ROUND55：素材版本回滚（list / apply）—— 后端逻辑在 bettercomfyui.store.rollback
# 契约：docs/ROUND55_版本回滚接口契约.md；前端 UI 调 /bettercomfyui/rollback/*
# ---------------------------------------------------------------------------


def _rollback_error(exc: Exception) -> tuple[dict, int]:
    """把 rollback 模块异常映射成 (json_body, http_status)。"""
    code = str(getattr(exc, "rollback_code", "") or "")
    mapping = {
        "TARGET_VERSION_NOT_FOUND": 404,
        "ASSET_NOT_FOUND": 404,
        "PROJECT_NOT_FOUND": 404,
        "CONTENT_HASH_MISMATCH": 422,
        "TARGET_FILE_MISSING": 422,
        "TARGET_VERSION_NOT_LOCKED": 409,
        "LOCK_RECORD_INCONSISTENT": 409,
        "AMBIGUOUS_ASSET": 409,
        "POSTCONDITION_FAILED": 409,
        "INVALID_ARGUMENT": 400,
        "IO_ERROR": 500,
    }
    status = mapping.get(code, 500)
    return {
        "ok": False,
        "code": code or type(exc).__name__,
        "error": f"{type(exc).__name__}: {exc}",
        "context": dict(getattr(exc, "context", {}) or {}),
    }, status



def _resolve_rollback_project(project_id_or_name: str) -> str:
    """UI「项目显示名或 project_id」→ 32 位 project_id（找不到原样返回，由模块报错）。"""
    s = str(project_id_or_name or "").strip()
    if not s:
        return s
    if PROJECT_ID_RE.match(s.lower()):
        return s.lower()
    try:
        root = _panel_projects_root()
        if root.is_dir():
            target = s.casefold()
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                meta_path = child / "project.json"
                if not meta_path.is_file():
                    continue
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    continue
                if str(meta.get("display_name") or "").strip().casefold() == target:
                    return child.name.lower()
    except Exception:  # noqa: BLE001
        pass
    return s


async def h_rollback_list(request):  # noqa: ARG001 - aiohttp 签名固定
    """GET /bettercomfyui/rollback/list?project_id=&asset= —— 素材版本历史。"""
    project_id = _resolve_rollback_project(str(request.query.get("project_id") or "").strip())
    asset = str(request.query.get("asset") or "").strip()
    if not project_id:
        return _json({"ok": False, "code": "INVALID_ARGUMENT",
                      "error": "project_id 必填"}, status=400)
    try:
        from bettercomfyui.store.rollback import list_asset_versions

        versions = list_asset_versions(project_id, asset or None)
        return _json({"ok": True, "project_id": project_id,
                      "asset": asset or None, "versions": versions})
    except Exception as exc:  # noqa: BLE001 - 统一转 JSON 错误
        body, status = _rollback_error(exc)
        logger.warning("回滚版本列表失败：%s", traceback.format_exc())
        return _json(body, status=status)


async def h_rollback_apply(request):
    """POST /bettercomfyui/rollback/apply —— 回滚到目标版本（哈希门禁+审计）。"""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return _json({"ok": False, "code": "INVALID_ARGUMENT",
                      "error": "请求体必须是 JSON 对象"}, status=400)
    project_id = _resolve_rollback_project(str(payload.get("project_id") or "").strip())
    asset = str(payload.get("asset") or "").strip()
    target = str(payload.get("target_version") or payload.get("version") or "").strip()
    if not (project_id and asset and target):
        return _json({"ok": False, "code": "INVALID_ARGUMENT",
                      "error": "project_id / asset / target_version 必填"}, status=400)
    try:
        from bettercomfyui.store.rollback import rollback_asset

        result = rollback_asset(
            project_id, asset, target,
            operator=str(payload.get("operator") or "panel"),
            reason=str(payload.get("reason") or "面板手动回滚"),
            require_hash_ok=bool(payload.get("require_hash_ok", True)),
            revalidate_lock=bool(payload.get("revalidate_lock", True)),
            lock_source=str(payload.get("lock_source") or "HUMAN_PASS"),
            review_cycle_id=(str(payload.get("review_cycle_id"))
                             if payload.get("review_cycle_id") else None),
        )
        return _json(result)
    except Exception as exc:  # noqa: BLE001
        body, status = _rollback_error(exc)
        logger.warning("版本回滚失败：%s", traceback.format_exc())
        return _json(body, status=status)



# ---------------------------------------------------------------------------
# ROUND56：H3 提示词手册全文（UI「提示词」面板加载/查看）
# ---------------------------------------------------------------------------


async def h_prompt_handbook(request):  # noqa: ARG001 - aiohttp 签名固定
    """GET /bettercomfyui/prompt/handbook?mode=concise|full|core|... —— 返回预设全文。

    ROUND58：默认 ``h3_concise_v1``（12,912 字精简版）；``h3_handbook`` 41,966 字历史完整版
    与 ``h3_core`` 旧核心章节仍可选；别名统一由 h3_handbook.canonical_preset 收口。
    """
    mode = str(request.query.get("mode") or "concise").strip()
    normalized = mode.strip().lower()
    try:
        from bettercomfyui.prompt import h3_handbook

        key = h3_handbook.canonical_preset(normalized) or h3_handbook.DEFAULT_PRESET
        text = h3_handbook.resolve_preset(key) or ""
        return _json({"ok": True, "mode": mode, "key": key, "label": h3_handbook.PRESET_LABELS.get(key, ""),
                      "chars": len(text), "text": text})
    except Exception as exc:  # noqa: BLE001 - 手册读取失败给清晰 JSON 错误
        logger.warning("读取 H3 手册失败：%s", traceback.format_exc())
        return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)


# ---------------------------------------------------------------------------
# ROUND57：硬件适配（检测报告 + 昇腾运行时补丁状态 + 中文报告）
# ---------------------------------------------------------------------------


async def h_hardware(request):
    """GET /bettercomfyui/hardware?refresh=1 —— 硬件报告/补丁状态/中文报告。

    实际逻辑在 `hardware_adapt.route_payload()`：路由只负责取参和兜异常。
    `refresh=1` 强制重新探测（用户换卡/装完驱动后不用重启 ComfyUI）。
    """
    force = str(request.query.get("refresh") or "").strip().lower() in (
        "1", "true", "yes", "on")
    try:
        mod = _mod("hardware_adapt")
        return _json(mod.route_payload(force=force))
    except Exception as exc:  # noqa: BLE001 - 探测失败也要给 JSON，不能 500 白屏
        logger.warning("硬件适配报告失败：%s", traceback.format_exc())
        return _json(
            {"ok": False, "available": False,
             "error": f"{type(exc).__name__}: {exc}",
             "notice": "硬件适配模块不可用；可运行 `python -m bettercomfyui.hardware doctor` 排查。"},
            status=500,
        )


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------


def register_routes() -> bool:
    """把路由挂到 ComfyUI 的 aiohttp 应用上。返回是否成功。"""
    global ROUTES_REGISTERED, ROUTES_ERROR

    try:
        from server import PromptServer  # type: ignore
    except Exception as exc:  # noqa: BLE001 - 非 ComfyUI 环境下没有 server 模块
        ROUTES_ERROR = f"无法导入 ComfyUI 的 server 模块：{type(exc).__name__}: {exc}"
        logger.info("Better ComfyUI：跳过面板路由注册（%s）", ROUTES_ERROR)
        return False

    server = getattr(PromptServer, "instance", None)
    if server is None:
        ROUTES_ERROR = "PromptServer.instance 不存在（服务尚未初始化）"
        logger.info("Better ComfyUI：跳过面板路由注册（%s）", ROUTES_ERROR)
        return False

    app = getattr(server, "app", None)
    if app is None:
        ROUTES_ERROR = "PromptServer.instance 没有 app 属性"
        logger.warning("Better ComfyUI：%s", ROUTES_ERROR)
        return False

    # 前端 pi.fetchApi() 会**自动加 `/api` 前缀**（新版 ComfyUI 的约定），
    # 而我们的路由原来只挂在 `/bettercomfyui/...` 上 —— 于是界面里
    # 体检/项目/工作流全部 404、POST 变 405（本轮实测：`/api/bettercomfyui/doctor` 404，
    # `/bettercomfyui/doctor` 200）。**两个前缀都注册**，前端怎么写都能通。
    API_PREFIX = "/api"

    def _both(register, path, handler):
        for prefix in (PREFIX, API_PREFIX + PREFIX):
            try:
                register(f"{prefix}{path}", handler)
            except Exception as exc:  # noqa: BLE001 - 单个前缀失败不影响另一个
                logger.warning("Better ComfyUI：路由 %s%s 注册失败：%s", prefix, path, exc)

    try:
        add_get = app.router.add_get
        add_post = app.router.add_post

        _both(add_get, "/doctor", h_doctor)
        _both(add_get, "/hardware", h_hardware)
        _both(add_get, "/projects", h_projects)
        # API 配置（DeepSeek / 千问 / 自配接口）：用户能在这里填密钥。
        # 读接口**只回遮罩后的密钥**；写接口只接受白名单字段。
        try:
            from . import api_settings as _api_settings

            _both(add_get, "/settings", _api_settings.handle_get)
            _both(add_post, "/settings", _api_settings.handle_save)
        except Exception as exc:  # noqa: BLE001 - 配置接口失败不该拖垮其它路由
            logger.warning("Better ComfyUI：API 配置路由注册失败：%s", exc)
        # 剧核 / DramaCore 项目上下文（只读）：列表 / 项目资产+提示词+成片 / 资产预览
        _both(add_get, "/projects/list", h_projects_list)
        _both(add_get, "/project/context", h_project_context)
        _both(add_get, "/asset/preview", h_asset_preview)
        # ROUND55：素材版本回滚入口（列表 / 应用）
        _both(add_get, "/prompt/handbook", h_prompt_handbook)
        _both(add_get, "/rollback/list", h_rollback_list)
        _both(add_post, "/rollback/apply", h_rollback_apply)
        # 剧核 / DramaCore 面板配置：全部配置的读写 + 连接测试
        _both(add_get, "/panel/config", h_panel_config_get)
        _both(add_post, "/panel/config", h_panel_config_save)
        _both(add_post, "/panel/test", h_panel_test_connection)
        _both(add_get, "/workflow/minimal", h_minimal_workflow)
        _both(add_get, "/phase2/status", h_phase2_status)   # 界面不调用
        _both(add_post, "/project/open", h_project_open)
        _both(add_post, "/run", h_run)
    except Exception as exc:  # noqa: BLE001 - 路由重复注册等
        ROUTES_ERROR = f"注册路由失败：{type(exc).__name__}: {exc}"
        logger.warning("Better ComfyUI：%s", ROUTES_ERROR)
        return False

    ROUTES_REGISTERED = True
    ROUTES_ERROR = None
    logger.info("Better ComfyUI：面板路由已注册于 %s/", PREFIX)
    return True


__all__ = ["register_routes", "ROUTES_REGISTERED", "ROUTES_ERROR", "PREFIX"]
