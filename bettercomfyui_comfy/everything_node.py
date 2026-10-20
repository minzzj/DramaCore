"""一体化总控台 —— **一个节点**完成「剧本 → 全量提示词 → 素材库 → 导演组」。

## 为什么是"一个大节点"（用户要求，2026-10-17 第二次强调）

用户原话：

> 「做成一个大节点就可以了，插上模型，插上输出，然后就可以用了……把它们整合起来，
> 整合的要完整，而不是重新造很多东西，或者把它生硬的接起来……我要它更加的自主化自动化。」

所以本节点**不再**要求用户手工接一串中间节点（配置→剧本→桥接→素材槽→导演台）。
节点内部按 `文档/BetterComfyUI_完整总流程图.dot` 的 SG1→SG2→SG4 顺序完成：

```
剧本输入
  → [A1/A1B/A2/A3/A4] 读项目素材库 → 程序生成"已有素材清单" → 原样注入提示词
  → [B] 提示词模型（外部 API / 本地千问 / 自配；密钥可接线也可读服务器配置）
  → [C1–C6] 系统提示词模板 / 语言 / 分镜粒度 / 输出 JSON+分隔符 / 风格标签
  → [D] AI 理解与拆解
  → [E1–E5] 一次调用产出：角色/场景/物品/分镜图提示词 + 视频提示词
  → [E6–E9] 程序确定性匹配（复用 prompt.match）：已存在→复用；不存在→登记进素材库；疑似→待确认
  → [G] 素材库：已 LOCK 素材（公共素材库）全量挂到每一段（身份一致性）
  → [J/P] 分段计划 → 导演组（MMX_DIR_GROUP）
```

**输出只需要接两处**：
* `导演组` → 真实导演台 `MiniMaxH3Director` 的 `r2v_groups`（导演台那边插模型：UNET/CLIP/VAE）；
* `导演台` 的 IMAGE/AUDIO → `CreateVideo` → `SaveVideo`（输出）。
其余输出（分镜脚本 / 提示词JSON / 素材清单 / 审计报告）只用于查看与追溯。

## 一次 API 调用（用户基准）

`bettercomfyui/prompt/script_build.py` 保证**一次** `complete()`；本节点只调用它一次。
占位模式（无密钥/显式勾选）**0 次调用**，并在审计首行大字标注，绝不冒充真实模型输出。

## 素材库整合（"导演节点那个素材库功能"）

* 生成出的 E1–E4 提示词会按**程序确定性匹配**（名称归一化）登记进当前项目的素材库：
  - 已存在同名素材 → 复用，不覆盖（R2）；
  - 不存在 → 新建素材条目（DRAFT，note 里带生成提示词），等待图像生成/审核；
  - 疑似重名 → 进 `suspects.json` 待人工确认（绝不自动覆盖）。
* 已 LOCK 的素材继续作为"身份参考图"全量挂到每一段（既有行为，不替代）。
* 占位模式**不写素材库**（避免演示数据污染真实项目）。

## 不静默

* 有 Key 但调用失败（401/超时/解析不了）→ 抛错，附可操作原因与原始输出片段；
* 没 Key → 审计首行明确写"占位（未调用真实模型）"，不会假装成功。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = [
    "BetterComfyUIEverything",
    "SYSTEM_PROMPT_CHOICES",
    "LEGACY_SYSTEM_PROMPT_LABELS",
    "SYSTEM_PROMPT_DEFAULT_KEY",
    "default_system_prompt_label",
    "system_prompt_key",
    "system_prompt_label",
    "is_system_prompt_preset",
    "resolve_template_choice",
    "STYLE_TAGS",
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
]

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 界面上的系统提示词选择 → 编排层 `SYSTEM_PROMPT_PRESETS` 的键（图一 C1）。
#: ROUND58：**默认**换成《MiniMax H3 提示词工程精简版》V1.0（2026-10，12,890 字）——
#: 系统提示词 1（编译器 Master）+ 系统提示词 2（素材图）+ 提示词片段库；
#: 历史完整手册（41,966 字）与旧核心章节**保留为可选项**（旧版不删）。
SYSTEM_PROMPT_CHOICES: dict[str, str] = {
    "H3 提示词精简版（2026-10，推荐）": "h3_concise_v1",
    "H3 手册（完整，历史版）": "h3_handbook",
    "H3 手册（精简）": "h3_core",
    "默认（影视拆解，推荐）": "default",
    "严格 JSON": "strict_json",
    "中文影视剧": "chinese_drama",
    "按镜头": "shot_by_shot",
    "本地简化（千问）": "local_qwen",
}

#: ROUND58 默认预设键（面板为空 / 节点用默认值时走这里）。
SYSTEM_PROMPT_DEFAULT_KEY = "h3_concise_v1"

#: ROUND56 及更早版本保存的工作流里存的是**旧标签**；改标签不能让它们静默掉到 default。
LEGACY_SYSTEM_PROMPT_LABELS: dict[str, str] = {
    "H3 手册（完整，推荐）": "h3_handbook",
    "H3 手册（完整）": "h3_handbook",
    "H3 提示词精简版": "h3_concise_v1",
    "H3 提示词精简版（推荐）": "h3_concise_v1",
}


def default_system_prompt_label() -> str:
    """默认系统提示词标签 = 列表第一项（ROUND58 起是 H3 提示词精简版）。"""
    return next(iter(SYSTEM_PROMPT_CHOICES))


def system_prompt_key(label: str) -> str:
    """界面标签 → 预设键；旧标签（ROUND56 之前保存的工作流）也解析。未知 → ``default``。"""
    text = str(label or "").strip()
    if text in SYSTEM_PROMPT_CHOICES:
        return SYSTEM_PROMPT_CHOICES[text]
    if text in LEGACY_SYSTEM_PROMPT_LABELS:
        return LEGACY_SYSTEM_PROMPT_LABELS[text]
    return "default"


def system_prompt_label(key: str) -> str:
    """预设键 → 界面标签；旧标签/别名也认。未知或空 → 默认标签（ROUND58 精简版）。"""
    text = str(key or "").strip()
    if not text:
        return default_system_prompt_label()
    for label, mapped in SYSTEM_PROMPT_CHOICES.items():
        if mapped == text:
            return label
    if text in LEGACY_SYSTEM_PROMPT_LABELS:
        return system_prompt_label(LEGACY_SYSTEM_PROMPT_LABELS[text])
    try:  # h3_handbook 的别名（concise / full / core / 精简版 …）也当预设键
        from bettercomfyui.prompt.h3_handbook import canonical_preset

        canonical = canonical_preset(text)
    except Exception:  # noqa: BLE001 - 手册模块不可用不该影响界面
        canonical = ""
    if canonical:
        for label, mapped in SYSTEM_PROMPT_CHOICES.items():
            if mapped == canonical:
                return label
    return default_system_prompt_label()


def is_system_prompt_preset(value: str) -> bool:
    """这个值是不是「预设」（而不是一整段自定义系统提示词）。"""
    text = str(value or "").strip()
    if not text:
        return False
    if text in SYSTEM_PROMPT_CHOICES or text in SYSTEM_PROMPT_CHOICES.values():
        return True
    if text in LEGACY_SYSTEM_PROMPT_LABELS:
        return True
    try:
        from bettercomfyui.prompt.h3_handbook import is_handbook_preset

        return bool(is_handbook_preset(text))
    except Exception:  # noqa: BLE001
        return False


def resolve_template_choice(label: str, custom: str = "", panel_value: str = "") -> str:
    """决定本次要用的系统提示词模板（预设键 / 预设标签 / 自定义全文）。

    优先级（与节点行为一致，供单测直接调用）：
    1. ``custom`` 非空 → 用户显式自定义全文，原样用；
    2. ``label`` 以「跟随面板配置」开头 → 用面板值；**面板为空 → ROUND58 默认精简版**；
    3. 其它 → 标签对应的预设键（未知标签回落 ``default``）。
    """
    custom_text = str(custom or "").strip()
    if custom_text:
        return custom_text
    if str(label or "").startswith("跟随面板配置"):
        panel = str(panel_value or "").strip()
        if not panel:
            return SYSTEM_PROMPT_DEFAULT_KEY
        if panel in SYSTEM_PROMPT_CHOICES or panel in LEGACY_SYSTEM_PROMPT_LABELS:
            return system_prompt_key(panel)  # 面板里存的是旧标签 → 收口成预设键
        return panel
    return system_prompt_key(label)

#: 图一 SG1 `C6` 风格标签库（可勾选的画风词表；`@标签名` 展开）。
STYLE_TAGS = {
    "写实电影感": "电影级写实画面，真实光影与材质，自然肤色，浅景深",
    "冷蓝电影光": "冷蓝色调，低饱和，青蓝阴影，冷白高光",
    "暖阳黄昏": "暖金色黄昏光，长影，柔和逆光，暖色调",
    "赛博朋克": "赛博朋克都市，霓虹灯牌，高对比色彩，雨夜反光地面",
    "水墨": "中国水墨画风格，留白，墨色晕染，纸本质感",
    "国风古装": "中国古典服饰与建筑，含蓄典雅，青绿山水色调",
    "日系动画": "日式赛璐璐动画风格，干净线条，明亮通透",
    "胶片颗粒": "胶片质感，细颗粒，轻微暗角，复古色彩",
    "黑白纪实": "黑白摄影，高对比，纪实风格，粗粝质感",
    "科技未来": "未来科技感，冷色高光，金属与玻璃材质，干净构图",
}

DEFAULT_STYLE = "电影感，真实自然，冷蓝电影光，低饱和，音画同步"


# ---------------------------------------------------------------------------
# 内部工具（复用编排包，不重复造）
# ---------------------------------------------------------------------------


def _local_gpu_status() -> dict:
    """本机能否真实出片（GPU + 显存）。取不到的项如实写 None，不猜。"""
    info: dict = {"has_cuda": None, "device": None, "vram_total_gb": None}
    try:
        import torch  # noqa: PLC0415

        info["has_cuda"] = bool(torch.cuda.is_available())
        if info["has_cuda"]:
            info["device"] = torch.cuda.get_device_name(0)
            info["vram_total_gb"] = round(
                torch.cuda.get_device_properties(0).total_memory / (1024 ** 3), 1)
    except Exception:  # noqa: BLE001 - 没有 torch 也不该让节点崩
        pass
    return info


def _repo_root() -> Path:
    """定位仓库根（复用 settings_node 的强标记探测，装成 custom_nodes 也不会读错）。"""
    try:
        from .settings_node import _resolve_repo_root

        return Path(_resolve_repo_root())
    except Exception:  # noqa: BLE001
        return REPO_ROOT


def _resolve_api() -> dict:
    """读服务器端 API 配置（环境变量 / user.yaml / 原生设置 / 巨人 settings）。"""
    key = ""
    model_name = "deepseek-chat"
    api_base = ""
    source = "none"
    try:
        from .settings_node import resolve_runtime_secret

        info = resolve_runtime_secret()
        key = str(info.get("api_key") or "")
        model_name = str(info.get("model") or model_name)
        api_base = str(info.get("api_base") or "")
        source = "config/user.yaml 或环境变量"
    except Exception:  # noqa: BLE001 - 读不到就继续走原生/巨人兜底
        pass
    if not key:
        try:
            from .api_settings import _read_giant_settings_key, _read_native_comfy_setting

            key = _read_native_comfy_setting() or _read_giant_settings_key() or ""
            if key:
                source = "ComfyUI 原生设置或巨人 settings.json"
        except Exception as exc:  # noqa: BLE001
            return {"api_key": "", "model": model_name, "api_base": api_base,
                    "configured": False, "source": f"读取失败：{exc}"}
    return {
        "api_key": key,
        "model": model_name,
        "api_base": api_base,
        "configured": bool(key),
        "source": source,
    }


def _expand_style(text: str, tags: list[str]) -> str:
    """把 `@标签` 与勾选标签展开成完整风格描述。"""
    parts: list[str] = []
    seen: set[str] = set()
    for tag in tags or []:
        desc = STYLE_TAGS.get(str(tag))
        if desc and desc not in seen:
            seen.add(desc)
            parts.append(desc)
    raw = (text or "").strip()
    try:
        from bettercomfyui.prompt.styles import expand_style_tokens

        raw = expand_style_tokens(raw)
    except Exception:  # noqa: BLE001
        pass
    merged = "，".join([p for p in [raw] if p] + parts)
    return merged.strip("，")


def _load_config():
    from bettercomfyui.config import load_config

    return load_config(repo_root=_repo_root())


def _open_asset_store(project_id: str):
    """打开项目素材库。返回 ``(store, cfg, error)``。

    **不静默吞错**（审计 R2）：素材库打开失败会让「已有素材注入/登记」整段失效，
    以前只返回 None，用户只看到「空库/未注入」却不知道原因（例如工作流里的
    项目引用不存在 = 开发机 id 遗留在新机器上）。现在把错误原样交给调用方写进
    审计与 warnings。
    """
    try:
        from bettercomfyui.store.assets import AssetStore
        from bettercomfyui.store.paths import ProjectPaths

        cfg = _load_config()
        root = Path(_repo_root()) / str(cfg.global_.project_root or "projects")
        return AssetStore(ProjectPaths(root=root, project_id=project_id)), cfg, None
    except Exception as exc:  # noqa: BLE001
        return None, None, f"{type(exc).__name__}: {exc}"


def _manifest_from_store(store) -> str:
    """图一 A1–A3：程序读素材库 → 生成"已有素材清单"原文（空库返回空串=A1B 空分支）。"""
    try:
        from bettercomfyui.store.inject import build_existing_asset_manifest

        return build_existing_asset_manifest(store) or ""
    except Exception:  # noqa: BLE001
        return ""


def _register_generated_assets(store, assets: list[dict], *, style: str,
                               per_class_limit: int | None = None) -> dict:
    """图一 E6–E9 + G：把 E1–E4 提示词**确定性**登记进素材库（不覆盖、不重复）。

    返回可审计的统计：复用/新建/疑似/失败。**不静默**：每类都记录原因。
    """
    from bettercomfyui.models.enums import AssetSource, LibraryScope
    from bettercomfyui.prompt.match import match_candidate

    stats: dict[str, Any] = {"reused": [], "created": [], "suspect": [], "errors": []}
    for asset in assets or []:
        name = str(asset.get("name") or "").strip()
        if not name:
            continue
        type_value = str(asset.get("asset_type") or "")
        prompt = str(asset.get("prompt") or "").strip()
        note = f"一体化总控台生成提示词：{prompt}"
        if style:
            note += f"。整片风格：{style}"
        candidate = {
            "name": name,
            "asset_type": type_value,
            "library_scope": LibraryScope.PROJECT.value,
            "source": AssetSource.AI.value,
        }
        try:
            result = match_candidate(candidate, store)
        except Exception as exc:  # noqa: BLE001 - 单个素材失败不拖垮整条链
            stats["errors"].append(f"{name}：匹配失败 {type(exc).__name__}: {exc}")
            continue
        outcome = getattr(result, "outcome", None)
        outcome_value = getattr(outcome, "value", str(outcome or ""))
        if "REUSE" in outcome_value:
            stats["reused"].append({"name": name, "asset_id": getattr(result, "asset_id", None)})
            continue
        if "SUSPECT" in outcome_value:
            stats["suspect"].append({"name": name, "suspect_id": getattr(result, "suspect_id", None)})
            continue
        # NEW_GENERATE：登记进素材库（DRAFT，等待图像生成与审核）
        try:
            created = store.create_asset(
                asset_type=type_value,
                name=name,
                source=AssetSource.AI,
                library_scope=LibraryScope.PROJECT,
                note=note,
                per_class_limit=per_class_limit,
            )
            stats["created"].append({
                "name": name,
                "asset_id": created.get("asset_id"),
                "asset_type": type_value,
            })
        except Exception as exc:  # noqa: BLE001 - 上限/重复等按可审计失败记录
            stats["errors"].append(f"{name}：登记失败 {type(exc).__name__}: {exc}")
    return stats


def _load_locked_assets(project_id: str) -> list[dict]:
    """取项目里**已 LOCK** 的素材（图二：公共素材库全量挂每一段）。"""
    store, _cfg, _err = _open_asset_store(project_id)
    if store is None:
        return []
    try:
        out: list[dict] = []
        for entry in store.list_assets():
            version = entry.get("current_version")
            if not version:
                continue
            lock = store.resolve_lock(entry["asset_id"], version)
            if not lock:
                continue
            out.append({
                "asset_id": entry["asset_id"],
                "asset_type": str(entry.get("asset_type")),
                "name": entry.get("name"),
                "version": version,
                "file_path": lock.get("file_path"),
                "content_hash": lock.get("content_hash"),
            })
        return out
    except Exception:  # noqa: BLE001 - 素材库不可用不该让节点崩
        return []


_ASSET_PRIORITY = {"CHARACTER": 0, "SCENE": 1, "PROP": 2, "SHOT": 3,
                   "AUDIO": 4, "VIDEO": 5}
_MAX_REFS = 9


def _segment_refs(segment_text: str, catalog: dict, locked: list[dict]) -> list[dict]:
    """段间素材：段文本里 `@名字` 且该名字在「段间素材」清单里 → 只挂这一段。

    只有 LOCK 过的版本才允许进组（R3）；未 LOCK 的引用记在审计里但不挂。
    """
    if not catalog:
        return []
    refs: list[dict] = []
    for name, meta in catalog.items():
        if f"@{name}" not in segment_text and name not in segment_text:
            continue
        asset_id = meta.get("asset_id")
        version = meta.get("version")
        for locked_item in locked:
            if locked_item["asset_id"] == asset_id and str(locked_item["version"]) == str(version):
                refs.append(locked_item)
                break
    return refs


class BetterComfyUIEverything:
    """一体化总控台：剧本进、导演组出；提示词生成 + 素材库登记全在节点内部完成。"""

    CATEGORY = "BetterComfyUI/总控台"
    FUNCTION = "run"
    RETURN_TYPES = ("MMX_DIR_GROUP", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("导演组", "分镜脚本", "提示词JSON", "素材清单", "审计报告")
    DESCRIPTION = (
        "一个大节点完成：剧本 → 一次 API 调用产出 E1–E5 全量提示词 → 确定性匹配登记素材库 → "
        "已 LOCK 素材全量挂每段 → 直接输出导演组。把导演组接导演台（模型接导演台）、"
        "导演台输出接成片即可；不需要手工串中间节点。"
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                # ---- SG1 剧本与提示词 ----
                "剧本": ("STRING", {
                    "multiline": True,
                    "default": ("第一章 雪夜\n"
                                "林秀在雪夜推门走进客栈，老陈抬头看她，两人对视。\n"
                                "林秀在桌边坐下，窗外雪落无声，灯影摇曳。"),
                    "tooltip": "一整段完整剧本（可多章节）。整条链**只调用一次 API**。",
                }),
                "系统提示词选择": (list(SYSTEM_PROMPT_CHOICES), {
                    "default": default_system_prompt_label(),
                    "tooltip": ("图一 C1 预置模板；默认「H3 提示词精简版（2026-10，12,890 字）」"
                                "（系统提示词 1 编译器 + 系统提示词 2 素材图 + 片段库）；"
                                "历史完整版/旧精简版仍可选。想完全自定义用下面的「自定义系统提示词」。"),
                }),
                "自定义系统提示词": ("STRING", {
                    "multiline": True, "default": "",
                    "tooltip": "留空 = 用预置；填了就覆盖（系统提示词的修改入口）。",
                }),
                "风格标签": ("STRING", {
                    "default": "@冷蓝电影光",
                    "tooltip": "图一 C6：写 @标签名 自动展开。可用：" + "、".join(STYLE_TAGS),
                }),
                "统一风格": ("STRING", {
                    "multiline": True,
                    "default": DEFAULT_STYLE,
                    "tooltip": "整片统一风格描述（会约束本次全部素材与镜头）。",
                }),
                "输出语言": (["中文", "英文"], {"default": "中文"}),
                "分镜粒度": (["按镜头", "按场景"], {"default": "按镜头"}),
                # ---- SG0/素材库 ----
                "项目": ("STRING", {
                    "default": "演示项目",
                    "tooltip": "项目 id 或显示名。决定读/写哪个素材库（项目之间隔离）。",
                }),
                "写入素材库": ("BOOLEAN", {
                    "default": True,
                    "tooltip": (
                        "开：本次生成的 E1–E4 素材提示词按确定性匹配登记进项目素材库\n"
                        "（已存在→复用不覆盖；不存在→新建待生成条目；疑似→待确认）。\n"
                        "关：只出提示词，不改素材库。占位模式永远不写。"
                    ),
                }),
                # ---- SG4 导演台 ----
                "每段时长_秒": ("FLOAT", {
                    "default": 10.0, "min": 1.0, "max": 120.0, "step": 0.5,
                    "tooltip": "图一 JP1：默认 10 秒。"}),
                "总时长_秒": ("INT", {
                    "default": 0, "min": 0, "max": 7200, "step": 5,
                    "tooltip": "0 = 按剧本自然分段；>0 = 按总时长平均切分。"}),
                "目标分辨率": (["720P", "1080P", "4K"], {"default": "1080P"}),
                "帧率": ("INT", {"default": 24, "min": 8, "max": 60, "step": 1}),
                "启用素材库": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "图二：项目里已 LOCK 的素材全量挂到每一段（身份一致性）。"}),
                "素材优先级": (["角色>场景>物品>分镜（推荐）", "按登记顺序"], {
                    "default": "角色>场景>物品>分镜（推荐）"}),
                "段间素材": ("STRING", {
                    "multiline": True, "default": "",
                    "tooltip": "可选：`名字 = asset_id@version`，段文本里写 @名字 才挂该段。"}),
                "公共素材模式": (["整个素材库（推荐）", "仅段间素材"], {
                    "default": "整个素材库（推荐）"}),
                "素材审核模式": (["模型审核", "人工审核", "跳过"], {"default": "模型审核"}),
                "素材审核阈值": ("INT", {"default": 70, "min": 0, "max": 100, "step": 1}),
                "段间接续": (["自动（导演台尾部帧辅助，推荐）", "全部硬切"], {
                    "default": "自动（导演台尾部帧辅助，推荐）"}),
                "使用占位结果": ("BOOLEAN", {
                    "default": False,
                    "tooltip": (
                        "True = 不调用模型（0 次 API），产出确定性占位结构；\n"
                        "False = 必须真实调用（未配置 API 时会回退占位并在审计首行标注）。"
                    ),
                }),
            },
            "optional": {
                # ↓ 这三根线来自「API 配置」节点（不接则读服务器配置）
                "api_key": ("STRING", {"forceInput": True,
                                       "tooltip": "接「API 配置」节点的 api_key；不接则读服务器配置。"}),
                "model": ("STRING", {"forceInput": True,
                                     "tooltip": "接 model；不接则读服务器配置。"}),
                "api_base": ("STRING", {"forceInput": True,
                                        "tooltip": "接 api_base；不接则读服务器配置。"}),
                "参考图_1": ("IMAGE", {"tooltip": "可选：额外全局参考图（直接连图）。"}),
                "参考图_2": ("IMAGE", {}),
                "参考图_3": ("IMAGE", {}),
            },
        }

    # -- 主流程 ---------------------------------------------------------
    def run(
        self,
        剧本: str,
        系统提示词选择: str = default_system_prompt_label(),
        自定义系统提示词: str = "",
        风格标签: str = "",
        统一风格: str = DEFAULT_STYLE,
        输出语言: str = "中文",
        分镜粒度: str = "按镜头",
        项目: str = "演示项目",
        写入素材库: bool = True,
        每段时长_秒: float = 10.0,
        总时长_秒: int = 0,
        目标分辨率: str = "1080P",
        帧率: int = 24,
        启用素材库: bool = True,
        素材优先级: str = "角色>场景>物品>分镜（推荐）",
        段间素材: str = "",
        公共素材模式: str = "整个素材库（推荐）",
        素材审核模式: str = "模型审核",
        素材审核阈值: int = 70,
        段间接续: str = "自动（导演台尾部帧辅助，推荐）",
        使用占位结果: bool = False,
        api_key: str = "",
        model: str = "",
        api_base: str = "",
        参考图_1: Any = None,
        参考图_2: Any = None,
        参考图_3: Any = None,
    ):
        from bettercomfyui.prompt.script_build import (
            ScriptBuildError,
            build_placeholder_prompts,
            generate_script_prompts,
        )
        from bettercomfyui.prompt.inject import SYSTEM_PROMPT_PRESETS

        warnings: list[str] = []
        script_text = str(剧本 or "").strip()
        if not script_text:
            raise RuntimeError("剧本为空：请在「剧本」里粘贴一整段剧本。")

        style = _expand_style(统一风格, [t.strip().lstrip("@") for t in (风格标签 or "").split()
                                        if t.strip()])

        # ---- 1) API 配置：端口优先，其次服务器配置 ------------------------
        api = _resolve_api()
        key_explicit = bool(str(api_key or "").strip())
        key = str(api_key or "").strip() or str(api.get("api_key") or "")
        model_name = str(model or "").strip() or str(api.get("model") or "deepseek-chat")
        panel_base = str(api.get("api_base") or "").strip().rstrip("/")
        node_base = str(api_base or "").strip().rstrip("/")
        # ROUND58 安全修复：工作流里的 api_base 不能把面板已保存的 Key 发到未配置地址。
        # 只有节点同时显式提供了 api_key（用户明确要用该地址）时才采用自定义 Base；
        # 否则忽略并记录告警（不静默改地址、也不外带密钥）。
        if node_base and not key_explicit and node_base != panel_base:
            warnings.append(
                "⚠️ 已忽略工作流里的节点 API Base：它会与面板已保存的 API Key 一起发往"
                "未配置地址（防止密钥外带）。请在右侧剧核面板配置 endpoint，或同时显式填写 Key。")
            node_base = ""
        base = node_base or panel_base
        configured = bool(key)

        # ---- 2) 项目 + 素材库（图一 A1/A1B/A2） ---------------------------
        project_id, resolve_note = self._resolve_project(项目)
        if resolve_note:
            warnings.append(resolve_note)
        store = None
        manifest = ""
        per_class_limit = None
        if project_id:
            store, cfg_store, store_error = _open_asset_store(project_id)
            if store is not None:
                manifest = _manifest_from_store(store)
                per_class_limit = getattr(getattr(cfg_store, "assets", None),
                                          "per_class_limit", None)
            else:
                warnings.append(
                    f"项目素材库打开失败（{store_error}）：本次不注入已有素材、不登记素材。")
        else:
            warnings.append(f"没找到项目「{项目}」：跳过素材库（不注入清单、不登记素材）。")

        # ---- 3) 一次 API 调用（或占位） ----------------------------------
        library_stats: dict[str, Any] = {"reused": [], "created": [], "suspect": [], "errors": []}
        api_calls = 0
        mode = "占位（未调用真实模型）"
        output_format_detected = "placeholder"
        generate_meta: dict[str, Any] = {}

        if 使用占位结果:
            result = build_placeholder_prompts(
                script_text, segment_seconds=float(每段时长_秒), style=style)
        elif not configured:
            warnings.append(
                "未读到 API 密钥：本次自动回退占位模式（**没有调用真实模型**）。"
                "请在右侧「Better ComfyUI」面板配置 API 密钥后重跑。")
            result = build_placeholder_prompts(
                script_text, segment_seconds=float(每段时长_秒), style=style)
        else:
            try:
                cfg = _load_config()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"读取服务器配置失败：{type(exc).__name__}: {exc}\n"
                    "请检查 config/default.yaml / config/user.yaml。") from exc
            cfg.prompt.api_key = key
            cfg.prompt.model_name = model_name
            if base:
                cfg.prompt.endpoint = base
                if base.startswith(("http://127.0.0.1", "http://localhost")):
                    cfg.prompt.model_provider = "local_qwen"
                else:
                    cfg.prompt.model_provider = "external_api"
            custom = (自定义系统提示词 or "").strip()
            _follow_panel = str(系统提示词选择 or "").startswith("跟随面板配置")
            # 跟随面板：保留 config/user.yaml 的面板提示词（含自定义全文）；
            # 面板为空 → ROUND58 默认《H3 提示词工程精简版》全文（12,912 字），
            # 既不是 2493 字要点摘要，也不再默认 41,966 字完整版。
            cfg.prompt.system_prompt_template = resolve_template_choice(
                系统提示词选择, custom,
                str(getattr(cfg.prompt, "system_prompt_template", "") or ""))
            _h3_meta: dict[str, Any] = {"follow_panel": _follow_panel}
            try:
                from bettercomfyui.prompt import h3_handbook as _h3hb
                from bettercomfyui.prompt import h3_manual as _h3m

                _handbook_mode = ""
                _tpl = str(cfg.prompt.system_prompt_template or "").strip()
                if not custom and _h3hb.is_handbook_preset(_tpl):
                    _hb_key = _h3hb.canonical_preset(_tpl)
                    _hb_text = _h3hb.resolve_preset(_hb_key) or ""
                    if _hb_text:
                        cfg.prompt.system_prompt_template = _hb_text
                        _handbook_mode = _hb_key
                        _h3_meta["handbook_mode"] = _handbook_mode
                        _h3_meta["handbook_chars"] = len(_hb_text)
                        _h3_meta["handbook_label"] = _h3hb.preset_label(_hb_key)
                        _h3_meta["handbook_is_default"] = (
                            _hb_key == _h3hb.DEFAULT_PRESET)
                        warnings.append(
                            "H3 提示词：已把{}全文（{} 字）作为系统提示词写入（不是要点摘要）".format(
                                _h3hb.preset_label(_hb_key), len(_hb_text))
                            + ("；这是 ROUND58 默认的精简版" if _h3_meta["handbook_is_default"]
                               else "；这是历史版，仍可正常使用"))
                    else:
                        warnings.append(
                            "⚠️ H3 提示词数据为空 → 未注入手册全文（请检查 {}）".format(
                                _h3hb.preset_file(_hb_key, "data/h3_handbook_full.txt")))
                if not _handbook_mode and bool(getattr(cfg.prompt, "h3_manual_enabled", True)):
                    _app = _h3m.build_system_prompt_appendix()
                    if _app:
                        cfg.prompt.system_prompt_template += "\n\n" + _app
                        _h3_meta["appendix_chars"] = len(_app)
                if bool(getattr(cfg.prompt, "no_subtitle", True)):
                    cfg.prompt.system_prompt_template += "\n\n" + _h3m.NO_SUBTITLE_SYSTEM_RULE
                    _h3_meta["no_subtitle_system"] = True
            except Exception as exc:  # noqa: BLE001 - 提示词增强失败不阻断一次 API 主流程
                warnings.append(f"⚠️ H3 手册/禁字幕系统提示词注入失败（继续）："
                                f"{type(exc).__name__}: {exc}")
            cfg.prompt.output_language = str(输出语言)
            cfg.prompt.story_granularity = str(分镜粒度)
            cfg.prompt.output_format = "auto"  # JSON 优先，失败自动按预设分隔符
            try:
                result = generate_script_prompts(
                    cfg=cfg,
                    script_text=script_text,
                    manifest=manifest,
                    output_format="auto",
                    style_hint=style,
                )
            except ScriptBuildError as exc:
                raise RuntimeError(str(exc)) from exc
            api_calls = result.api_calls
            mode = f"真实模型（一次调用：{model_name}）"
            output_format_detected = str(result.meta.get("output_format_detected") or "")
            generate_meta = {k: v for k, v in result.meta.items() if k != "notes"}
            # ROUND58：把解析/形状适配的 warnings 暴露给用户，避免"静默转换"。
            for _note in (result.meta.get("notes") or []):
                warnings.append(f"提示词解析：{_note}")
            if str(result.meta.get("output_shape") or "") == "handbook":
                warnings.append(
                    "模型按 H3 手册结构（video_prompt/material_image_prompts）输出，"
                    "已自动转换为标准剧本结构；建议系统提示词选择含【输出契约】的模板，"
                    "或让模型按 chapters/assets 输出。")
            for _drop in (result.meta.get("dropped_invalid_refs") or []):
                warnings.append(f"模型输出引用了不存在的素材，已丢弃该引用（不参与生成）：{_drop}")

        video_prompts = list(result.video_prompts)
        _h3_meta = locals().get("_h3_meta") or {}
        try:
            from bettercomfyui.prompt import h3_manual as _h3m

            _cfg_no_sub = True
            try:
                _cfg_r55 = _load_config()
                _cfg_no_sub = bool(getattr(_cfg_r55.prompt, "no_subtitle", True))
            except Exception:  # noqa: BLE001 - 配置读取失败按默认 True
                pass
            if _cfg_no_sub and video_prompts:
                video_prompts = [_h3m.inject_no_subtitle_instruction(str(p))
                                 for p in video_prompts]
                _h3_meta["no_subtitle_prompts"] = len(video_prompts)
                warnings.append(
                    f"已对 {len(video_prompts)} 段视频提示词注入「禁止生成字幕/文字/水印」要求"
                    "（系统提示词同步注入；保留剧本明确要求可见文字的双引号内容）")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"⚠️ 逐段禁字幕提示词注入失败（继续）：{type(exc).__name__}: {exc}")
        if not video_prompts:
            raise RuntimeError(
                "剧本拆解没有产出任何视频段（模型输出了素材但没有 segments）。"
                "请检查提示词或改用严格 JSON 模板。")
        assets = list(result.script.get("assets") or [])

        # ---- 4) 素材库登记（E6–E9；占位模式不写） ------------------------
        if store is not None and 写入素材库 and api_calls >= 1:
            library_stats = _register_generated_assets(
                store, assets, style=style, per_class_limit=per_class_limit)
            if library_stats["errors"]:
                warnings.append(
                    f"素材库登记有 {len(library_stats['errors'])} 处未成功（详见素材清单）。")
        elif api_calls == 0:
            library_stats = {"reused": [], "created": [], "suspect": [],
                             "errors": [], "skipped": "占位模式不写素材库"}
        elif not 写入素材库:
            library_stats = {"reused": [], "created": [], "suspect": [],
                             "errors": [], "skipped": "写入素材库=关"}

        # ---- 5) 总时长约束（可选项，保留原能力） --------------------------
        if 总时长_秒 and 总时长_秒 > 0:
            need = max(1, int(round(float(总时长_秒) / max(0.1, float(每段时长_秒)))))
            if need < len(video_prompts):
                video_prompts = video_prompts[:need]
            elif need > len(video_prompts):
                while len(video_prompts) < need:
                    video_prompts.append(video_prompts[len(video_prompts) % max(1, len(video_prompts))])
            warnings.append(
                f"已按总时长 {总时长_秒}s / 每段 {每段时长_秒}s 调整为 {len(video_prompts)} 段。")

        # ---- 6) 公共素材库：已 LOCK 素材全量挂每一段（图二） --------------
        locked: list[dict] = []
        if 启用素材库 and project_id:
            locked = _load_locked_assets(project_id)
            if not locked:
                warnings.append(
                    f"项目 {project_id[:8]} 里没有已 LOCK 的素材：导演组将不含参考图。"
                    "（素材需要先生成图像并通过审核才会 LOCK 挂载；本次生成的提示词已登记）")
        if 公共素材模式 == "仅段间素材":
            locked = []
            warnings.append("公共素材模式=仅段间素材：只挂段间素材。")
        if 素材优先级.startswith("角色>场景"):
            locked.sort(key=lambda a: _ASSET_PRIORITY.get(str(a["asset_type"]), 99))

        catalog: dict[str, dict] = {}
        if (段间素材 or "").strip():
            try:
                from .unified_director import parse_catalog

                catalog = parse_catalog(段间素材)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"段间素材解析失败（已忽略）：{type(exc).__name__}: {exc}")

        extra_global = [t for t in (参考图_1, 参考图_2, 参考图_3) if t is not None]

        # ---- 7) 组装导演组（MMX_DIR_GROUP） ------------------------------
        style_text = style or ""
        groups: list[dict] = []
        segment_texts: list[str] = []
        for index, prompt in enumerate(video_prompts):
            text = str(prompt or "").strip()
            if style_text:
                text = f"{text}。全片视觉风格：{style_text}"
            segment_texts.append(text)
            segment_refs = list(locked[:_MAX_REFS]) if 公共素材模式.startswith("整个素材库") else []
            for extra in _segment_refs(text, catalog, locked):
                if extra not in segment_refs:
                    segment_refs.append(extra)
            group = {
                "segment_index": index,
                "prompt": text,
                "duration_s": float(每段时长_秒),
                "resolution": 目标分辨率,
                "fps": int(帧率),
                "task_type": "r2v",
                "hardcut": False,
                "continuity_from_prev": (None if index == 0 else True),
                "refs": [
                    {"asset_id": a["asset_id"], "version": a["version"],
                     "content_hash": a.get("content_hash"),
                     "kind": a["asset_type"]}
                    for a in segment_refs
                ],
            }
            if 段间接续 == "全部硬切":
                group["hardcut"] = True
                group["continuity_from_prev"] = False
            groups.append(group)

        if extra_global:
            groups[0].setdefault("extra_ref_images", len(extra_global))

        # 超过 9 张参考：保留优先级高的，并告知（不静默）
        if len(locked) > _MAX_REFS:
            warnings.append(
                f"已 LOCK 素材 {len(locked)} 个，超过导演台上限 {_MAX_REFS}："
                f"每段按优先级挂前 {_MAX_REFS} 个，其余**会被导演台丢弃**。")

        # ---- 8) 输出 ------------------------------------------------------
        script_text_out = "\n---\n".join(segment_texts)
        prompt_payload = {
            "schema": "bettercomfyui.one-node/v1",
            "meta": {
                "mode": mode,
                "api_calls": api_calls,
                "model": model_name if api_calls else "（未调用）",
                "output_format_detected": output_format_detected,
                "project_id": project_id or "",
                "segment_count": len(groups),
                "segment_seconds": float(每段时长_秒),
                "resolution": 目标分辨率,
                "fps": int(帧率),
                "global_style": style_text,
                "manifest_injected": bool(manifest),
                "library": {
                    "created": len(library_stats.get("created") or []),
                    "reused": len(library_stats.get("reused") or []),
                    "suspect": len(library_stats.get("suspect") or []),
                    "errors": len(library_stats.get("errors") or []),
                    "skipped": library_stats.get("skipped", ""),
                },
                **generate_meta,
                "h3_manual": _h3_meta,
            },
            "assets_by_type": result.assets_by_type,
            "video_prompts": video_prompts,
            "asset_library": library_stats,
            "director_groups": [
                {k: g[k] for k in ("segment_index", "prompt", "duration_s", "task_type",
                                   "hardcut", "continuity_from_prev")}
                for g in groups
            ],
            "references": {
                "locked_assets": [
                    {"asset_id": a["asset_id"], "type": a["asset_type"],
                     "name": a.get("name"), "version": a["version"]}
                    for a in locked
                ],
                "max_refs": _MAX_REFS,
            },
            "warnings": warnings,
        }
        prompt_json = json.dumps(prompt_payload, ensure_ascii=False, indent=2)

        asset_lines = [f"项目：{项目 or '（未指定）'}（id={project_id or '未解析'}）"]
        asset_lines.append(
            f"本次提示词登记：新建 {len(library_stats.get('created') or [])} / "
            f"复用 {len(library_stats.get('reused') or [])} / "
            f"疑似 {len(library_stats.get('suspect') or [])}"
            + (f" / 跳过（{library_stats.get('skipped')}）" if library_stats.get("skipped") else ""))
        for item in library_stats.get("created") or []:
            asset_lines.append(f"  + 新建待生成 {item['asset_type']:<9} {item['name']}  {item['asset_id']}")
        for item in library_stats.get("reused") or []:
            asset_lines.append(f"  = 复用已存在 {item['name']}  {item.get('asset_id') or ''}")
        for item in library_stats.get("suspect") or []:
            asset_lines.append(f"  ? 疑似重复 {item['name']}  {item.get('suspect_id') or ''}")
        for err in library_stats.get("errors") or []:
            asset_lines.append(f"  ! 未成功 {err}")
        asset_lines.append(f"已 LOCK 素材（身份参考图）：{len(locked)} 个")
        for a in locked:
            asset_lines.append(
                f"  - {a['asset_type']:<9} {a.get('name') or a['asset_id'][:14]}"
                f" @{a['version']}  hash={str(a.get('content_hash'))[:8]}")
        if not locked:
            asset_lines.append("  （无：导演组 refs 为空，身份一致性暂时只能靠素材库后续补）")

        gpu = _local_gpu_status()
        audit = [
            "=== 一体化总控台 审计 ===",
            f"模式：{'⚠️ ' if api_calls == 0 else ''}{mode}",
            f"API 调用次数：{api_calls} 次（基准：全程只允许 1 次）",
            f"API：{('已配置（来源：' + str(api.get('source')) + '）') if configured else '未配置'}",
            f"输出格式解析：{output_format_detected}",
            f"素材清单注入：{'已注入（' + str(len(manifest)) + ' 字）' if manifest else '空库/未注入'}",
            f"素材库登记：新建 {len(library_stats.get('created') or [])} / "
            f"复用 {len(library_stats.get('reused') or [])} / "
            f"疑似 {len(library_stats.get('suspect') or [])}",
            f"段数：{len(groups)}  每段 {float(每段时长_秒)}s  总时长约 "
            f"{round(len(groups) * float(每段时长_秒), 1)}s",
            f"分辨率/帧率：{目标分辨率} @ {帧率}fps",
            f"风格：{style_text or '（无）'}",
            f"已 LOCK 素材：{len(locked)} 个（审核模式={素材审核模式}，阈值={素材审核阈值}）",
            f"接续：{段间接续}",
        ]
        if gpu["has_cuda"] is False:
            audit.append(
                "⚠️ 本机无 CUDA GPU：**真实出片必然失败**（要加载 34 GB 权重）。"
                "本节点输出可用于查看导演组结构；出片请到 GPU 机器。")
        elif gpu["has_cuda"] and (gpu["vram_total_gb"] or 0) < 24:
            audit.append(
                f"⚠️ 本机显存仅 {gpu['vram_total_gb']} GB（低于 24 GB）：真实出片很可能 OOM。")
        for warn in warnings:
            audit.append(f"⚠️ {warn}")
        audit_text = "\n".join(audit)

        print(f"[BetterComfyUI 总控台] {mode} | {len(groups)} 段 | "
              f"{len(locked)} LOCK 素材 | 素材库新建 {len(library_stats.get('created') or [])} | "
              f"API {api_calls} 次")
        return (groups, script_text_out, prompt_json, "\n".join(asset_lines), audit_text)

    # -- 项目解析 ---------------------------------------------------------
    def _resolve_project(self, value: str) -> tuple[str | None, str]:
        """解析「项目」widget：32 位 id 必须存在；显示名可复用/自动新建。

        返回 ``(project_id | None, note)``，note 会写入审计 warnings（不静默）。
        审计 R2 修复：此前遇到 32 位 id 直接原样返回、不校验存在性 —— 工作流里
        残留开发机 id 时，节点会静默拿不到素材库（只显示「空库/未注入」），
        而且在真实 API 模式下可能往一个不存在的项目目录里写素材（幽灵项目）。
        """
        text = (value or "").strip()
        try:
            from bettercomfyui.store.projects import ProjectStore

            cfg = _load_config()
            project_root = str(cfg.global_.project_root or "projects")
            root = Path(project_root)
            if not root.is_absolute():
                root = Path(_repo_root()) / root
            store = ProjectStore(root, cfg)
            if not text:
                projects = store.list()
                if projects:
                    pid = str(projects[-1]["project_id"])
                    return pid, (
                        f"项目留空：沿用最近项目 {pid}"
                        f"（{projects[-1].get('display_name')}）；如需别的项目请显式填写。"
                    )
                return None, "项目留空且还没有任何项目：跳过素材库（不注入清单、不登记素材）。"
            info, action = store.resolve_reference(text)
            pid = str(info.get("project_id") or text)
            if action == "CREATED_NAME":
                return pid, (
                    f"项目显示名「{text}」不存在，已自动新建项目 {pid}"
                    "（下次使用同一显示名会复用该项目的素材库）。"
                )
            if action == "EXISTING_NAME":
                return pid, (
                    f"项目引用「{text}」按显示名解析到已有项目 {pid}"
                    f"（{info.get('display_name')}）。"
                )
            return pid, ""
        except Exception as exc:  # noqa: BLE001 - 解析失败必须写进审计，不静默
            return None, (
                f"项目引用「{text}」无法解析（{type(exc).__name__}: {exc}）："
                "跳过素材库。请填写 32 位 project_id，或填写项目显示名（不存在会自动新建）。"
            )


NODE_CLASS_MAPPINGS = {"BetterComfyUIEverything": BetterComfyUIEverything}
NODE_DISPLAY_NAME_MAPPINGS = {
    "BetterComfyUIEverything": "一体化总控台（大节点：剧本→提示词→素材库→导演组）",
}
