"""剧核 / DramaCore —— ComfyUI 自定义节点包入口。

**ComfyUI 会读取本模块的三样东西**（见 ComfyUI `nodes.py` 的自定义节点加载逻辑）：

    NODE_CLASS_MAPPINGS        节点类注册表
    NODE_DISPLAY_NAME_MAPPINGS 节点显示名
    WEB_DIRECTORY              前端扩展目录（里面的 .js 会被自动加载）

**加载健壮性**

ComfyUI 在启动时 import 本模块。如果这里抛异常，整个节点包会被跳过
（用户会看到「节点不见了」而不是「哪里错了」）。因此：

* 节点模块的导入被包在 try/except 里，失败时打印**可操作**的修复指引，
  并让包以「0 个节点」的状态加载完成 —— 这样 ComfyUI 不会崩，
  用户也能在启动日志里看到确切原因；
* 但**不静默**：缺编排包时 `BetterComfyUI 体检` 节点仍会出现，
  点一下就告诉你缺什么、怎么修。

编排包（`bettercomfyui`）与插件是分开安装的两部分，定位逻辑见 `_bridge.py`。
"""

from __future__ import annotations

import logging
import os
import traceback

__version__ = "0.2.0-internal"

logger = logging.getLogger("bettercomfyui.comfy")
if not logger.handlers:
    logger.addHandler(logging.NullHandler())

#: 前端扩展目录：ComfyUI 会把这里的 .js 全部加载进网页
WEB_DIRECTORY = "./web"

#: 第二阶段接入点（**不注册节点、不在 UI 展示**，详见 phase2.py）
PHASE2_ENTRY_MODULE = "bettercomfyui_comfy.phase2"

NODE_CLASS_MAPPINGS: dict[str, type] = {}
NODE_DISPLAY_NAME_MAPPINGS: dict[str, str] = {}


def _load_nodes() -> None:
    """导入节点定义并填充注册表。失败时打印修复指引，但不让 ComfyUI 崩。"""
    try:
        from . import nodes as _nodes
    except Exception as exc:  # noqa: BLE001 - 启动期必须兜住任何异常
        import traceback

        logger.error("剧核 DramaCore：节点模块导入失败，节点不会被注册。")
        logger.error("原因：%s: %s", type(exc).__name__, exc)
        logger.error("%s", traceback.format_exc())
        print(
            "\n[剧核 DramaCore] 节点加载失败，未注册任何节点。\n"
            f"  原因：{type(exc).__name__}: {exc}\n"
            "  修复：确认插件目录完整（至少含 __init__.py / _bridge.py / nodes.py / web/），"
            "并确认 ComfyUI 的 Python 环境可 `import bettercomfyui`。\n"
            "  详情见上方 traceback。\n"
        )
        return

    NODE_CLASS_MAPPINGS.update(getattr(_nodes, "NODE_CLASS_MAPPINGS", {}) or {})
    NODE_DISPLAY_NAME_MAPPINGS.update(
        getattr(_nodes, "NODE_DISPLAY_NAME_MAPPINGS", {}) or {}
    )

    # 启动期就报出编排包是否可用，省得用户点进节点才发现
    try:
        from ._bridge import describe_bridge

        info = describe_bridge()
        if info["available"]:
            logger.info(
                "剧核 DramaCore：插件 v%s 加载完成，%d 个节点；编排包=%s",
                __version__, len(NODE_CLASS_MAPPINGS), info["orchestrator_file"],
            )
        else:
            logger.warning(
                "剧核 DramaCore：插件已加载 %d 个节点，但**编排包不可用**，"
                "节点运行时会报错。原因见 `BetterComfyUI 体检` 节点或下方指引。",
                len(NODE_CLASS_MAPPINGS),
            )
            print(f"\n[剧核 DramaCore] 编排包不可用：\n{info['error']}\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("剧核 DramaCore：编排包可用性探测失败：%s", exc)


_load_nodes()


def _register_fusion_nodes() -> None:
    """注册一体化导演台、融合节点与素材适配节点。

    `unified_director` 是**主入口**：一个节点完成「素材 + 多段提示词 + 一致性」，
    产出整条 MMX_DIR_GROUP 交给真实导演台出片（见该模块 docstring 里的愿景依据）。

    其余两组是对接辅助：融合节点负责回灌与体检，素材节点把已 LOCK 的素材变成 IMAGE。
    失败时不影响其余节点，但**要留下明确原因**（不静默）。
    """
    for mod_name, label in (
        ("unified_director", "一体化导演台"),
        ("everything_node", "一体化总控台（大节点）"),
        ("settings_node", "API 配置节点"),
        ("storyboard_node", "剧本→提示词节点"),
        ("prompt_bridge", "提示词桥接（DeepSeek → 导演台）"),
        ("asset_nodes", "素材适配节点"),
        ("review_nodes", "素材生成/入库/视频审核节点"),
        ("finalize_nodes", "成片剪辑/封装节点"),
        ("one_stop_node", "单节点（剧本→成片）"),
        ("fusion_nodes", "融合节点"),
    ):
        try:
            module = __import__(f"{__name__}.{mod_name}", fromlist=["*"])
            NODE_CLASS_MAPPINGS.update(getattr(module, "NODE_CLASS_MAPPINGS", {}) or {})
            NODE_DISPLAY_NAME_MAPPINGS.update(
                getattr(module, "NODE_DISPLAY_NAME_MAPPINGS", {}) or {}
            )
            logger.info(
                "剧核 DramaCore：%s 已加载 %d 个", label,
                len(getattr(module, "NODE_CLASS_MAPPINGS", {}) or {}),
            )
        except Exception as exc:  # noqa: BLE001 - 单组失败不拖垮整包
            logger.error("剧核 DramaCore：%s 加载失败：%s: %s", label, type(exc).__name__, exc)
            logger.error("%s", traceback.format_exc())


_register_fusion_nodes()



# ---------------------------------------------------------------------------
# 品牌层：显示名 / 分类统一为「剧核 / DramaCore」
# ---------------------------------------------------------------------------
#
# **节点类型 ID 一律不改**（`BetterComfyUI*`）—— 改了会让用户的历史工作流
# 全部变成"找不到节点类型"。这里只改：
#   * NODE_DISPLAY_NAME_MAPPINGS（节点搜索栏/标题显示的中英双语名）
#   * 每个节点类的 CATEGORY（左侧"添加节点"菜单里的分组）
# 这样旧工作流照常加载，界面上的品牌/语言全部是剧核。

BRAND_ZH = "剧核"
BRAND_EN = "DramaCore"

#: 节点类型 ID -> 中英双语显示名（与 web/js/dramacore_i18n.js 的 NODE_NAMES 同义）。
_BRAND_NODE_NAMES: dict[str, str] = {
    "BetterComfyUIOneStop": "剧核·单节点（剧本→成片）｜DramaCore One-Stop (Script→Final Cut)",
    "BetterComfyUIEverything": "剧核·总控台（剧本→素材→导演组）｜DramaCore Control Hub",
    "BetterComfyUIOneDirector": "剧核·一体化导演台｜DramaCore Unified Director",
    "BetterComfyUIProject": "剧核·项目｜DramaCore Project",
    "BetterComfyUIRun": "剧核·运行｜DramaCore Run",
    "BetterComfyUIAssetReview": "剧核·素材审核｜DramaCore Asset Review",
    "BetterComfyUIManualAsset": "剧核·手动素材｜DramaCore Manual Asset",
    "BetterComfyUIResult": "剧核·结果｜DramaCore Result",
    "BetterComfyUIDoctor": "剧核·体检｜DramaCore Doctor",
    "BetterComfyUISettings": "剧核·API 配置｜DramaCore API Settings",
    "BetterComfyUIStoryboard": "剧核·剧本→提示词｜DramaCore Script→Prompts",
    "BetterComfyUIPromptBridge": "剧核·提示词桥接｜DramaCore Prompt Bridge",
    "BetterComfyUIAssetGen": "剧核·素材生成（取任务）｜DramaCore Asset Generation",
    "BetterComfyUIAssetIngest": "剧核·素材入库｜DramaCore Asset Ingest",
    "BetterComfyUIVideoReview": "剧核·视频审核｜DramaCore Video Review",
    "BetterComfyUIFinalize": "剧核·成片剪辑与自检｜DramaCore Finalize & QA",
    "BetterComfyUIAssetImage": "剧核·素材｜取已锁定图｜DramaCore Asset Locked Image",
    "BetterComfyUIAssetSlots": "剧核·素材｜多槽输出(1-9)｜DramaCore Asset Multi-slot (1-9)",
    "BetterComfyUIAssetMedia": "剧核·素材｜取已锁定音/视频｜DramaCore Asset Locked A/V",
    "BetterComfyUITimelineText": "剧核·融合｜时间轴文本｜DramaCore Fusion Timeline Text",
    "BetterComfyUIFusionGroups": "剧核·融合｜段计划→导演组｜DramaCore Fusion Segments→Groups",
    "BetterComfyUIFusionPackGroup": "剧核·融合｜单组打包｜DramaCore Fusion Pack Group",
    "BetterComfyUIFusionCombine": "剧核·融合｜组合并｜DramaCore Fusion Combine",
    "BetterComfyUIFusionIngest": "剧核·融合｜回灌段版本｜DramaCore Fusion Ingest Take",
    "BetterComfyUIFusionDoctor": "剧核·融合｜体检｜DramaCore Fusion Doctor",
}

#: 旧分类 -> 剧核分类（保留子分组，节点不会被"藏起来"）。
_BRAND_CATEGORIES: dict[str, str] = {
    "Better ComfyUI": "剧核 DramaCore",
    "Better ComfyUI/融合": "剧核 DramaCore/融合 Fusion",
    "BetterComfyUI/单节点": "剧核 DramaCore/单节点 One-Stop",
    "BetterComfyUI/总控台": "剧核 DramaCore/总控台 Control Hub",
    "BetterComfyUI/素材": "剧核 DramaCore/素材 Assets",
    "BetterComfyUI/审核": "剧核 DramaCore/审核 Review",
    "BetterComfyUI/提示词": "剧核 DramaCore/提示词 Prompts",
    "BetterComfyUI/全局配置": "剧核 DramaCore/配置 Settings",
    "BetterComfyUI/成片": "剧核 DramaCore/成片 Output",
}

#: 品牌默认分类（旧分类不在上表、但又是本插件节点时用）。
_BRAND_DEFAULT_CATEGORY = "剧核 DramaCore"


def _brand_fallback_display(node_id: str, current: str | None) -> str:
    """未登记节点：把旧的 BetterComfyUI 前缀换成剧核，不丢原语义。"""
    name = str(current or node_id)
    for prefix in ("Better ComfyUI ", "BetterComfyUI ", "Better ComfyUI", "BetterComfyUI"):
        if name.startswith(prefix):
            name = name[len(prefix):].lstrip("·|- ")
            break
    return f"{BRAND_ZH}·{name}" if name else f"{BRAND_ZH}·{node_id}"


def _apply_branding() -> int:
    """把已注册节点的显示名与分类统一为「剧核 / DramaCore」。返回改了几个显示名。"""
    changed = 0
    for node_id, node_cls in list(NODE_CLASS_MAPPINGS.items()):
        try:
            display = _BRAND_NODE_NAMES.get(node_id)
            if display is None:
                display = _brand_fallback_display(
                    node_id, NODE_DISPLAY_NAME_MAPPINGS.get(node_id))
            if NODE_DISPLAY_NAME_MAPPINGS.get(node_id) != display:
                NODE_DISPLAY_NAME_MAPPINGS[node_id] = display
                changed += 1
            category = getattr(node_cls, "CATEGORY", None)
            if isinstance(category, str):
                new_category = _BRAND_CATEGORIES.get(category)
                if new_category is None and "Better" in category:
                    new_category = _BRAND_DEFAULT_CATEGORY
                if new_category is not None and new_category != category:
                    node_cls.CATEGORY = new_category
        except Exception as exc:  # noqa: BLE001 - 单个节点失败不拖垮整包
            logger.warning("剧核 DramaCore：节点 %s 品牌化失败：%s", node_id, exc)
    logger.info(
        "剧核 DramaCore：品牌层已应用（显示名 %d 个，分类 %d 条）",
        changed, len(_BRAND_CATEGORIES),
    )
    return changed


_apply_branding()


def _register_routes() -> None:
    """注册网页面板需要的 HTTP 路由。

    失败时必须让插件整体仍然加载成功（节点可用、只是面板拿不到数据），
    因此这里兜住异常并只记录原因。**不静默**：日志写明为什么没注册上，
    也可通过 `/bettercomfyui/doctor` 查询。

    这个调用**必须**留在模块级。曾因用暂存副本覆盖本文件而丢失，
    后果是节点全部正常但所有 `/bettercomfyui/*` 路由 404（面板打开即报错，
    而启动日志看起来"一切正常"）。守卫：tests/test_comfy_plugin.py。
    """
    try:
        from . import routes as _routes

        if not _routes.register_routes():
            logger.warning(
                "剧核 DramaCore：面板路由未注册（%s）。节点仍可用，"
                "但网页侧边栏面板无法工作。", _routes.ROUTES_ERROR,
            )
    except Exception as exc:  # noqa: BLE001 - 启动期必须兜住
        logger.warning("剧核 DramaCore：面板路由注册异常：%s: %s", type(exc).__name__, exc)


_register_routes()


def _env_flag(name: str) -> bool:
    """环境变量开关（与 hardware_adapt 的判定保持同一套真值表）。"""
    return str(os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _hardware_boot_summary() -> None:
    """启动时打印一行硬件摘要 + 需要用户知道的补丁状态（ROUND57）。

    **必须完全兜住异常**：ComfyUI 启动时 import 本包，任何未捕获异常都会让
    用户看到「节点全部消失」。硬件探测依赖 torch/驱动/编排包，属于最可能
    在陌生机器上出错的环节，所以这里连 import 都放进 try 里。

    输出刻意很短：详细报告在 `/bettercomfyui/hardware` 或
    `python -m bettercomfyui.hardware doctor`。
    """
    try:
        from .hardware_adapt import preflight

        info = preflight()
        if not info.get("available"):
            logger.warning("剧核 DramaCore：硬件适配不可用（%s），节点仍可用。",
                           info.get("error") or "原因未知")
            print(
                "\n[剧核 DramaCore] 硬件适配不可用（节点仍可用）。\n"
                f"  原因：{info.get('error') or '未知'}\n"
                "  修复：确认编排包 bettercomfyui 已安装，然后运行 "
                "`python -m bettercomfyui.hardware doctor`。\n"
            )
            return

        vram_gb = int(info.get("vram_mib") or 0) // 1024
        device = info.get("device_name") or "未识别设备"
        logger.info(
            "剧核 DramaCore：硬件 %s / %s · %s · %dGB · Tier %s · 补丁=%s",
            info.get("vendor"), info.get("backend"), device, vram_gb,
            info.get("tier"), info.get("patches_status"),
        )
        lines = [
            "",
            "[剧核 DramaCore] 硬件适配摘要",
            f"  设备  : {info.get('vendor')} / {info.get('backend')} · {device} · {vram_gb}GB",
            f"  分级  : {info.get('tier_label')} · H3 全流程="
            f"{'支持/有条件' if info.get('full_h3_supported') else '不保证/不可用'}",
        ]
        if info.get("launch_args"):
            lines.append("  启动参数: " + " ".join(info["launch_args"]))
        patches = info.get("patches_status")
        if patches not in (None, "", "not_required"):
            lines.append(
                f"  运行时补丁: {patches} — {info.get('patches_reason') or ''}".rstrip())
            if patches in ("partial", "deferred", "disabled_by_env"):
                lines.append("  手动步骤见 /bettercomfyui/hardware 或 "
                             "`python -m bettercomfyui_comfy.hardware_adapt patches`。")
        if info.get("warnings"):
            lines.append(f"  注意  : {info['warnings'][0]}")
        lines.append("  详情  : /bettercomfyui/hardware")
        print("\n".join(lines) + "\n")
    except Exception as exc:  # noqa: BLE001 - 启动期绝不允许拖垮节点加载
        logger.warning("剧核 DramaCore：硬件摘要打印失败（不影响节点）：%s: %s",
                       type(exc).__name__, exc)


#: 允许用户用环境变量彻底关掉启动期硬件探测（老机器/无驱动环境排障用）
if not _env_flag("BETTERCOMFYUI_DISABLE_HARDWARE"):
    _hardware_boot_summary()

#: 显式声明第二阶段不在 UI 暴露（用户要求：保留接入点但不展示）
PHASE2_NODES_EXPOSED = False


def get_phase2_entry():
    """按需取得第二阶段接入点模块。**不注册为节点。**

    设计意图：接入点保留，用户界面上不出现。将来要上屏时，
    只需在 `nodes.py` 里加两行注册，无需改动本函数。
    """
    from importlib import import_module

    return import_module(PHASE2_ENTRY_MODULE)


__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
    "PHASE2_NODES_EXPOSED",
    "PHASE2_ENTRY_MODULE",
    "get_phase2_entry",
    "__version__",
]
