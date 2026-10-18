"""Better ComfyUI 一体化导演台 —— **一个节点**完成「素材库 + 多段提示词 → 整部片子」。

规格依据：`文档/流程图-总.txt`（图一 主流程图 + 图二 素材流转与身份一致性图）。

## 两套机制：各自管一件事，**叠加**使用

| 机制 | 管什么 | 谁实现 |
|---|---|---|
| **素材库全量参考图** | **身份**：人物/服装/场景/物品 | 本节点 |
| **尾部 22 帧**（`JP4` 默认 22 可调） | **风格/动作/镜头/光线**的连续 | 导演台**原生支持**，默认自动（`O1`），无需手动切换 |

流程图原文：

    O1["自动取尾部22帧或前一帧无需手动切换"]
    O2["主连贯性使用素材库参考图采用参考模式"]
    P1["人物不在尾部22帧时由素材库锁定"]
    P3["尾部22帧仅作动作镜头光线辅助连续"]

**为什么两套都要**（`P1`）：人物不一定出现在最后 22 帧里。只靠尾部帧角色会丢；
只靠素材图则动作与镜头会跳。所以本节点**不替代**尾部帧机制，只负责把素材库送对。

## 素材库怎么送（图二「素材复用：所有段都用这一套」）

图二写得很清楚 —— **同一套 LOCK 素材，被所有段引用**：

    C_LOCK -->|"@引用"| SEG1
    C_LOCK -->|"@引用"| SEG5
    C_LOCK -->|"@引用"| SEG9
    C_LOCK -->|"下一部剧本复用"| NEXT

所以本节点的做法是：

1. **公共素材库**：自动读取项目素材库里**全部已 LOCK 的素材**，
   **每一段都挂上**（这就是「完整的提交给下一段视频」）。
2. **段间素材库**：某些素材只在特定段出现时，写在「段间素材」里，
   再在段文本里用 `@名字` 引用 —— 只挂到那些段。
3. **槽位按 `G5` 优先级排**（角色 > 场景 > 物品 > 分镜），
   让同一素材在所有段落到同一槽位。
4. **两份清单都可以提前设置好** —— 大模型生成提示词时就能把它们一起定下来。

## 超过 9 个参考图怎么办

Director 侧只接受 `MAX_REFERENCE_IMAGES`（9）个，超出的会**被丢弃**。
本节点**不静默截断**（那会让人以为素材都用上了），也不硬性拒绝
（那样整个工作流跑不起来）。做法是：**全部照传，但在摘要里明确列出
哪些会因为超限而失效**，并把优先级高的排前面，让角色这类关键素材先占位。

## 剧本写法（多段用独占一行的 `---` 分段）

```
镜头1｜主角推开木门走进房间，镜头缓慢推进。
---
镜头2｜主角在桌前坐下，抬头看向窗外。@道具_01_旧信
---
[硬切] 镜头3｜镜头拉远，画面定格。
```

* 段里可以不写任何 `@` —— 公共素材库本来就会全部挂上；
* `@名字` 只用于**段间素材**（该段独有的东西）；
* `[硬切]` 写在段首 = 这一段**不接上一段**（默认都接，交给尾部帧机制）。

「段间素材」写法（每行一条，`名字 = asset_id@version`）：

```
道具_01_旧信 = 道具_01_旧信@v1
```
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# 图一 SG1 C6 风格标签库（@标签名 展开）。
# 用**绝对导入**：插件包是顶层 bettercomfyui_comfy，
# 不能写 `from ..bettercomfyui`（跨层导入会越界）。
from bettercomfyui.prompt.styles import expand_style_tokens  # noqa: E402

from .fusion import (
    MAX_REFERENCE_IMAGES,
    FusionError,
    locked_asset_ref_images,
    make_r2v_group,
)

CATEGORY = "Better ComfyUI"

#: 分段分隔符：独占一行的 `---` / `===` / `***`
_SPLIT_RE = re.compile(r"^\s*(?:-{3,}|={3,}|\*{3,})\s*$", re.M)

#: `@素材类型_序号_名称`；名字不含空白与常用标点
_AT_RE = re.compile(r"@([^\s@，,。；;、]+)")

#: 段首硬切标记
_HARDCUT_RE = re.compile(r"^\s*\[\s*硬切\s*\]\s*")

#: 素材优先级（流程图 G5：角色>场景>物品>分镜）。数值小者排前。
_TYPE_PRIORITY = {"角色": 0, "场景": 1, "物品": 2, "分镜": 3}


def normalize_name(text: str) -> str:
    """名称归一化：去空白、去全/半角标点、统一小写。

    愿景要求「名称归一化 开启 —— 去空格统一小写统一标点」。
    不归一化的话，「角色_01_李明」与「角色 - 01 - 李明」会被当成两个素材。
    """
    if not text:
        return ""
    out = re.sub(r"[\s\u3000]+", "", text)
    out = re.sub(r"[，,。.；;：:、！!？?（）()\[\]【】\"'“”‘’]", "", out)
    return out.lower()


def asset_priority(name: str, *, order_by_priority: bool) -> tuple:
    """排序键：开启时按 角色>场景>物品>分镜，未知类型排最后。"""
    if not order_by_priority:
        return (0,)
    head = normalize_name(name).split("_", 1)[0]
    for key, pri in _TYPE_PRIORITY.items():
        if head == normalize_name(key):
            return (pri,)
    return (len(_TYPE_PRIORITY),)


def parse_segments(script: str) -> list[dict]:
    """把剧本切成段。返回 `[{index, text, hardcut, refs}]`。

    段号从 0 开始（与 Director 的 `plan_idx` 对齐 —— 它的
    `resolve_segment_continuity_from_prev` 里「第 0 段永不接续」）。
    """
    if not (script or "").strip():
        raise FusionError(
            "剧本分镜为空。请写成多段提示词，用独占一行的 --- 分隔，例如：\n"
            "镜头1｜主角推门进来。\n---\n镜头2｜镜头拉远。"
        )
    segs: list[dict] = []
    for chunk in _SPLIT_RE.split(script):
        text = (chunk or "").strip()
        if not text:
            continue
        hardcut = bool(_HARDCUT_RE.match(text))
        text = _HARDCUT_RE.sub("", text).strip()
        if not text:
            continue
        refs: list[str] = []
        for name in _AT_RE.findall(text):
            if name not in refs:
                refs.append(name)
        segs.append({"index": len(segs), "text": text, "hardcut": hardcut, "refs": refs})
    if not segs:
        raise FusionError("剧本里没有解析出任何有效镜头（分隔符之间是空的）。")
    return segs


def parse_catalog(text: str) -> dict[str, Any]:
    """解析「段间素材」清单。返回 `{归一化名: {asset_id, version, display}}`。

    每行 `名字 = asset_id@version`；行首 `#` 整行当注释。
    """
    catalog: dict[str, Any] = {}
    for lineno, line in enumerate((text or "").splitlines(), start=1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            raise FusionError(
                f"段间素材第 {lineno} 行格式不对：{line!r}\n"
                "正确写法：名字 = asset_id@version"
            )
        name, _, spec = s.partition("=")
        name, spec = name.strip(), spec.strip()
        if not name or not spec:
            raise FusionError(f"段间素材第 {lineno} 行左右有一边是空的：{line!r}")
        if "@" not in spec:
            raise FusionError(
                f"段间素材第 {lineno} 行缺少版本：{line!r}\n"
                "R3 要求 LOCK 必须绑定具体版本，所以必须写 asset_id@version。"
            )
        asset_id, _, version = spec.rpartition("@")
        asset_id, version = asset_id.strip(), version.strip()
        if not asset_id or not version:
            raise FusionError(f"段间素材第 {lineno} 行的 asset_id 或 version 是空的：{line!r}")
        key = normalize_name(name)
        if key in catalog:
            raise FusionError(
                f"段间素材里名字重复：{name!r}（归一化后都是 {key!r}）—— "
                "会让匹配结果不确定，请改名或合并。"
            )
        catalog[key] = {"asset_id": asset_id, "version": version, "display": name}
    return catalog


def load_library_assets(project_id: str, *, repo_root: Path, cfg: Any) -> list[dict]:
    """读取项目素材库里**全部已 LOCK 的素材**（公共素材库，图二「所有段都用这一套」）。

    返回 `[{asset_id, version, display, asset_type}]`。

    只取已 LOCK 的：R3 规定只有取得 LOCK 的版本才允许进入生成。
    未 LOCK 的素材**不是错误**（它们可能还在审核中），所以这里跳过。
    """
    from importlib import import_module

    paths_mod = import_module("bettercomfyui.store.paths")
    assets_mod = import_module("bettercomfyui.store.assets")

    projects_root = Path(cfg.global_.project_root)
    if not projects_root.is_absolute():
        projects_root = Path(repo_root) / projects_root
    paths = paths_mod.ProjectPaths(root=projects_root, project_id=project_id)
    store = assets_mod.AssetStore(paths)

    out: list[dict] = []
    for asset in store.list_assets():
        aid = asset.get("asset_id")
        if not aid:
            continue
        version = store.latest_locked_version(aid)
        if not version:
            continue                      # 还没 LOCK：不能进生成，跳过（非错误）
        atype = asset.get("asset_type")
        out.append({
            "asset_id": aid,
            "version": version,
            # 命名规则 类型_序号_名称（流程图 G2）；没有名字时退回 asset_id
            "display": asset.get("name") or aid,
            "asset_type": getattr(atype, "value", atype),
        })
    return out


def plan_ref_slots(segments: list[dict], library: list[dict],
                   catalog: dict[str, Any], *,
                   order_by_priority: bool = True) -> tuple[list[list[dict]], list[str]]:
    """算出每一段要挂哪些素材引用（**纯程序，无模型参与**）。

    返回 `(每段的引用列表, 警告列表)`。

    * **公共素材库**（`library`）-> 每一段都挂；
    * **段间素材**（`catalog` + 段里的 `@名字`）-> 只挂到引用了它的段；
      并且**从公共素材库里排除**（否则它在"整个素材库"模式下会挂到每一段，
      「段间」就失去意义了）；
    * 段里 `@名字` 但清单里没有 -> **报错**（不能静默忽略）；
    * 按 `G5` 优先级排序，让同一素材在所有段落到同一槽位。
    """
    # 段间素材先从公共集合里摘出去 —— 这一步是「段间」语义成立的前提。
    segment_only = {normalize_name(str(c["display"])) for c in catalog.values()}
    lib_sorted = sorted(
        (dict(a) for a in library
         if normalize_name(str(a["display"])) not in segment_only),
        key=lambda a: asset_priority(str(a["display"]), order_by_priority=order_by_priority),
    )
    warnings: list[str] = []
    planned: list[list[dict]] = []
    used_keys: set[str] = set()

    for seg in segments:
        refs: list[dict] = list(lib_sorted)
        seen = {normalize_name(str(r["display"])) for r in lib_sorted}
        picked: list[dict] = []
        for name in seg["refs"]:
            key = normalize_name(name)
            hit = catalog.get(key)
            if hit is None:
                known = ("、".join(sorted(c["display"] for c in catalog.values()))
                         or "（段间素材清单为空）")
                raise FusionError(
                    f"第 {seg['index'] + 1} 段引用了 @{name}，但它不在「段间素材」清单里。\n"
                    f"清单里现有：{known}\n"
                    f"请补上 `{name} = asset_id@version`，或把段里的 @ 去掉"
                    "（公共素材库本来就会全部挂上，段里不需要再写）。"
                )
            used_keys.add(key)
            if key in seen:
                continue
            seen.add(key)
            picked.append(hit)

        picked.sort(key=lambda c: asset_priority(
            str(c["display"]), order_by_priority=order_by_priority))
        refs.extend(picked)

        # ⚠️ 上限是**硬性**的，而且在导演台侧是**静默丢弃**：
        #   external_groups.py:360
        #       if 0 <= i < MAX_REFERENCE_IMAGES: images[i] = t[:1].clone()
        # 槽位 ≥9 的参考图会被直接丢掉，不报错也不提示。
        # 所以这里按优先级排序后截断，并**把被丢弃的名字写进警告** ——
        # 结果与导演台一致，但从"静默丢"变成"明确告知丢了谁"。
        # （不能只排序不截断：那样会撞上 locked_asset_ref_images 的槽位守卫。）
        if len(refs) > MAX_REFERENCE_IMAGES:
            dropped = [str(r["display"]) for r in refs[MAX_REFERENCE_IMAGES:]]
            kept = [str(r["display"]) for r in refs[:MAX_REFERENCE_IMAGES]]
            warnings.append(
                f"第 {seg['index'] + 1} 段有 {len(refs)} 个参考图，但导演台只认前 "
                f"{MAX_REFERENCE_IMAGES} 个（槽位 0..{MAX_REFERENCE_IMAGES - 1}），"
                "超出的会被它**静默丢弃**。\n"
                f"        生效（按素材优先级排序）：{'、'.join(kept)}\n"
                f"        已丢弃：{'、'.join(dropped)}\n"
                "        建议：把全片共用的角色素材控制在 9 个以内，"
                "或把只在个别段出现的素材改成「段间素材」。"
            )
            refs = refs[:MAX_REFERENCE_IMAGES]
        planned.append(refs)

    unused = [c["display"] for k, c in catalog.items() if k not in used_keys]
    if unused:
        warnings.append("以下段间素材没有任何镜头 @ 到它（不会进组）：" + "、".join(unused))
    return planned, warnings


def _style_tooltip() -> str:
    """风格标签库 tooltip（列出可勾选标签名）。延迟导入，避免加载期顺序问题。"""
    try:
        from bettercomfyui.prompt.styles import style_tooltip

        return style_tooltip()
    except Exception:  # noqa: BLE001 - 标签库不可用不该让节点加载失败
        return "注入到每一段，保证整片色调与画风统一。支持 @标签名 展开风格标签。"


class BetterComfyUIOneDirector:
    """一体化导演台：素材库 + 多段提示词 -> 整条 MMX_DIR_GROUP。

    接到 `MiniMaxH3Director` 的 `r2v_groups` 即可出片。
    尾部 22 帧的段间风格连续由导演台原生处理（默认开启），本节点不重复实现。
    """

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {
            "required": {
                "剧本分镜": ("STRING", {
                    "multiline": True,
                    "default": (
                        "镜头1｜主角推开木门走进房间，镜头缓慢推进。\n"
                        "---\n"
                        "镜头2｜主角在桌前坐下，抬头看向窗外。\n"
                        "---\n"
                        "镜头3｜镜头拉远，画面定格。"
                    ),
                    "tooltip": (
                        "多段提示词，用独占一行的 --- 分段。\n"
                        "公共素材库会自动全部挂到每一段，所以段里**不必**写 @；\n"
                        "只有该段独有的素材才写 @名字（需在下方「段间素材」里定义）。\n"
                        "段首写 [硬切] 表示该段不接上一段。"
                    ),
                }),
                "项目": ("STRING", {
                    "default": "默认项目",
                    "tooltip": "项目 id —— 决定读哪个项目的素材库（项目之间隔离）。",
                }),
                "段间素材": ("STRING", {
                    "multiline": True,
                    "default": (
                        "# 只给指定段用的素材：名字 = asset_id@version\n"
                        "# 段文本里写 @名字 才会挂上；公共素材库不需要写在这里\n"
                    ),
                    "tooltip": (
                        "段间素材库：某些素材只在特定段出现时用。\n"
                        "每行 `名字 = asset_id@version`；只有取得 LOCK 的版本才允许进组（R3）。"
                    ),
                }),
                "段时长_秒": ("FLOAT", {
                    "default": 10.0, "min": 0.2, "max": 120.0, "step": 0.1,
                    "tooltip": "每一段的时长。流程图 JP1：默认分段时长 10 秒（可调 5/10/15）。",
                }),
                "统一风格": ("STRING", {
                    "multiline": True,
                    "default": "电影感，真实自然，冷蓝电影光，低饱和，音画同步",
                    # 图一 SG1 C6「风格标签库预置供勾选」：
                    # 不新增输入（会改 schema、打乱已保存的工作流），
                    # 而是在文本层支持 @标签名 展开 —— 见 prompt/styles.py。
                    "tooltip": _style_tooltip(),
                }),
                "段间接续": (["自动（导演台尾部帧辅助，推荐）", "全部硬切"], {
                    "default": "自动（导演台尾部帧辅助，推荐）",
                    "tooltip": (
                        "流程图 O1：导演台默认自动取尾部 22 帧（或前一帧）衔接，"
                        "无需手动切换 —— 选「自动」即交给它。\n"
                        "尾部帧负责风格/动作/镜头/光线的连续；"
                        "身份一致性始终由素材库参考图保证"
                        "（P1：人物不一定在尾部 22 帧里）。"
                    ),
                }),
                "素材优先级": (["角色>场景>物品>分镜（推荐）", "按登记顺序"], {
                    "default": "角色>场景>物品>分镜（推荐）",
                    "tooltip": (
                        "流程图 G5：素材优先级 角色>场景>物品>分镜。"
                        "按优先级排槽位，能让同一素材在所有段落到同一槽位；"
                        "槽位不够时也是优先级高的先占位。"
                    ),
                }),
            },
            "optional": {
                "公共素材模式": (["整个素材库（推荐）", "仅段间素材"], {
                    "default": "整个素材库（推荐）",
                    "tooltip": (
                        "流程图图二「素材复用：所有段都用这一套」：\n"
                        "「整个素材库」= 自动读取项目里**全部已 LOCK 素材**，每段都挂上；\n"
                        "「仅段间素材」= 只挂「段间素材」里点名的那几个。"
                    ),
                }),
                "参考图_1": ("IMAGE", {"tooltip": "也可直接连图，作为额外全局参考。"}),
                "参考图_2": ("IMAGE", {}),
                "参考图_3": ("IMAGE", {}),
            },
        }

    RETURN_TYPES = ("MMX_DIR_GROUP", "INT", "STRING", "STRING")
    RETURN_NAMES = ("导演组", "段数", "一致性摘要", "审计JSON")
    FUNCTION = "build"
    CATEGORY = CATEGORY
    DESCRIPTION = (
        "一个节点完成「素材库 + 多段提示词」：剧本按 --- 分段，"
        "把项目素材库里全部已 LOCK 素材（公共素材库）挂到每一段，"
        "段间素材按 @ 引用补充，一次性产出整条 MMX_DIR_GROUP 交给 MiniMaxH3Director。"
        "身份一致性由素材库保证；风格/动作连续由导演台尾部 22 帧原生机制负责，两者叠加。"
    )

    @classmethod
    def IS_CHANGED(cls, **kwargs) -> str:
        """导演组规划依赖素材库 LOCK 状态：每次排队都必须重新读库。

        审计 R2：ComfyUI 按输入哈希缓存节点输出；素材审核拿到新 LOCK 后，
        重复排队会命中旧缓存，导演组仍挂旧素材（“看起来更新了，实际没变”）。
        """
        import time
        import uuid

        return f"{time.time_ns()}-{uuid.uuid4().hex}"

    def build(
        self,
        *,
        剧本分镜: str,
        项目: str,
        段间素材: str,
        段时长_秒: float,
        统一风格: str,
        段间接续: str,
        素材优先级: str,
        公共素材模式: str = "整个素材库（推荐）",
        参考图_1: Any = None,
        参考图_2: Any = None,
        参考图_3: Any = None,
    ):
        # ⚠️ `_cfg` 在 `fusion_nodes` 里，**不在** `nodes` 里 ——
        # 写成 `from .nodes import _cfg` 会在运行时 ImportError
        # （节点注册不执行 build，启动看着正常，一按 Queue 才炸）。
        # `fusion_nodes._cfg` 也是既有测试注入配置的接缝。
        from .fusion_nodes import _cfg
        from .nodes import _orch_repo_root

        cfg = _cfg(_orch_repo_root())
        repo_root = Path(_orch_repo_root())

        segments = parse_segments(剧本分镜)
        catalog = parse_catalog(段间素材)
        order_by_priority = str(素材优先级).startswith("角色>")
        all_hardcut = str(段间接续).startswith("全部硬切")
        whole_library = str(公共素材模式).startswith("整个素材库")

        library: list[dict] = []
        if whole_library:
            library = load_library_assets(str(项目), repo_root=repo_root, cfg=cfg)

        planned, warnings = plan_ref_slots(segments, library, catalog,
                                           order_by_priority=order_by_priority)

        extra_global = [t for t in (参考图_1, 参考图_2, 参考图_3) if t is not None]
        # 图一 SG1 C6：把 @标签名 展开成完整风格描述。
        # 不写 @ 就是原样自由文本 —— 老工作流完全不受影响。
        style = expand_style_tokens((统一风格 or "").strip())

        groups: list[dict] = []
        audit: list[dict] = []
        for seg, refs in zip(segments, planned):
            ref_images: dict[int, Any] = {}
            seg_audit: list[dict] = []
            if refs:
                # refs 已在 plan_ref_slots 里按优先级排序并截到上限，
                # 所以这里不会撞上 locked_asset_ref_images 的槽位守卫。
                ref_images, seg_audit = locked_asset_ref_images(
                    str(项目), refs, repo_root=repo_root, config=cfg)
            for tensor in extra_global:
                slot = len(ref_images)
                ref_images[slot] = tensor
                seg_audit.append({"slot": slot, "asset_id": "(直连图)", "version": "-"})

            prompt = seg["text"]
            if style:
                prompt = f"{prompt}。全片视觉风格：{style}"

            # 第 0 段永不接续（Director 侧同样如此）；其余段默认接上一段。
            hardcut = seg["hardcut"] or all_hardcut
            continuity = None if (seg["index"] == 0 and not hardcut) else (not hardcut)

            groups.append(make_r2v_group(
                prompt=prompt,
                duration_sec=float(段时长_秒),
                ref_image_size="match",
                ref_images=ref_images,
                continuity_from_prev=continuity,
            ))
            audit.append({
                "segment_index": seg["index"],
                "prompt": prompt,
                "hardcut": hardcut,
                "continuity_from_prev": continuity,
                "refs": seg_audit,
                "slots": len(ref_images),
                "effective_slots": min(len(ref_images), MAX_REFERENCE_IMAGES),
            })

        return (
            groups,
            len(groups),
            self._summary(segments, planned, audit, library, catalog,
                          extra_global, warnings, all_hardcut, whole_library),
            json.dumps({"project": str(项目), "segment_count": len(groups),
                        "public_library_count": len(library),
                        "segments": audit}, ensure_ascii=False, indent=2),
        )

    @staticmethod
    def _summary(segments, planned, audit, library, catalog, extra_global,
                 warnings, all_hardcut, whole_library) -> str:
        lines = [
            f"一体化导演台：{len(segments)} 段。",
            f"  公共素材库：{'整个素材库' if whole_library else '已关闭'}"
            + (f"，取到 {len(library)} 个已 LOCK 素材（每段都挂）" if whole_library else ""),
            f"  段间素材库：{len(catalog)} 条",
        ]
        if extra_global:
            lines.append(f"  直连参考图：{len(extra_global)} 张")
        for seg, refs in zip(segments, planned):
            marks = "、".join(str(r["display"]) for r in refs) or "（无参考图）"
            cut = "  [硬切]" if (seg["hardcut"] or all_hardcut) else ""
            eff = min(len(refs), MAX_REFERENCE_IMAGES)
            count = f"{len(refs)} 个参考" + (
                f"（生效 {eff}）" if len(refs) > MAX_REFERENCE_IMAGES else "")
            lines.append(f"  第 {seg['index'] + 1} 段{cut}：{count} —— {marks}")
        for w in warnings:
            lines.append(f"  [!] {w}")
        lines.append("")
        lines.append("把「导演组」接到 MiniMaxH3Director 的 r2v_groups 即可出片。")
        lines.append("身份一致性 = 素材库参考图（每段全量挂）；"
                     "风格/动作连续 = 导演台尾部 22 帧原生机制。两者叠加。")
        return "\n".join(lines)


NODE_CLASS_MAPPINGS = {
    "BetterComfyUIOneDirector": BetterComfyUIOneDirector,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "BetterComfyUIOneDirector": "Better ComfyUI 一体化导演台（素材库+剧本→整条导演组）",
}
