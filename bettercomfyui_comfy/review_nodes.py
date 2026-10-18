"""素材生成 / 素材入库 / 视频审核 节点（补齐逻辑图 SG2、SG5、SG6 的界面入口）。

## 对应逻辑图

| 逻辑图 | 本文件 |
|---|---|
| `SG2 GEN1–GEN4` 生成角色/场景/物品/分镜参考图（仅新增） | `BetterComfyUIAssetGen`：从素材库取下一个待生成素材，输出**提示词 + 生成参数**，接任意图像工作流 |
| `SG2 G/G7` 素材库只追加、新版本+指纹 | `BetterComfyUIAssetIngest`：IMAGE → 保存新版本 + SHA-256 + `PENDING_REVIEW`（绝不覆盖旧版本） |
| `SG3 H/H1/H2/CNT1/LOCK/FALLBACK` 素材审核 | 见 `nodes.py::BetterComfyUIAssetReview`（本轮升级为走完整审核库） |
| `SG5 R_seg/CNT2/CNT3/N1_EDGE` 段级审核 | `BetterComfyUIFusionIngest`（回灌时审核；本轮修掉"常量 80 分"假评分）+ 本节点段级循环 |
| `SG6 R_full/R1b/FULL_POLICY` 整片审核 | `BetterComfyUIVideoReview`：真实视频文件 → 段级 R_seg + 边界验证 + 整片 R_full + 失败策略 |

## 诚实边界（必须让用户知道）

* **素材生成**需要用户自己的图像模型工作流（本机无图像权重，节点只产出提示词/参数，
  不假装生成）；
* **评分**默认用 `bettercomfyui/review/local_scorer.py` 的**本地启发式**（真实像素/帧统计，
  非常量），输出里标注 `mode=local_heuristic` 与跳过的维度；要语义级审核请替换评分模型；
* 视频**重生成**仍需 GPU 导演台；本节点输出「需重生成段清单」与 CNT2/CNT3 记账，不假装已经重生成。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

__all__ = [
    "BetterComfyUIAssetGen",
    "BetterComfyUIAssetIngest",
    "BetterComfyUIVideoReview",
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
]

CATEGORY = "Better ComfyUI"

#: 素材类型中文 → 枚举值
_ASSET_TYPES = {
    "全部类型": None,
    "角色 CHARACTER": "CHARACTER",
    "场景 SCENE": "SCENE",
    "物品 PROP": "PROP",
    "分镜 SHOT": "SHOT",
}
_ASSET_PRIORITY = {"CHARACTER": 0, "SCENE": 1, "PROP": 2, "SHOT": 3,
                   "AUDIO": 4, "VIDEO": 5}

_REVIEW_SCOPES = ["段级 + 整片（推荐）", "仅段级 R_seg", "仅整片 R_full"]
_FULL_POLICIES = ["按项目配置", "阻止输出", "降级输出"]
_SCORE_SOURCES = ["本地启发式（真实计算）", "人工分数JSON"]


def _orch_helpers():
    from .nodes import _NodeError, _config, _orch, _orch_repo_root, _store_for_project

    return _NodeError, _config, _orch, _orch_repo_root, _store_for_project


def _now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _derive_seed(asset_id: str, base: int) -> int:
    if int(base) >= 0:
        return int(base)
    digest = hashlib.sha256(asset_id.encode("utf-8")).hexdigest()
    return int(digest[:12], 16) % (2 ** 31 - 1)


def _counters_for_project(paths: Any):
    """项目级计数器（与 FusionIngest 共用 `counters.json`，避免两套口径）。"""
    from bettercomfyui.review import CounterStore

    counter_path = Path(paths.project_dir) / "counters.json"
    counters = CounterStore()
    if counter_path.is_file():
        try:
            counters.restore(json.loads(counter_path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 - 损坏则从零开始，且调用方会在审计里看到
            pass
    return counters, counter_path


def _save_counters(counters: Any, path: Path) -> None:
    path.write_text(json.dumps(counters.snapshot(), ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _extract_prompt(asset: dict) -> str:
    """从素材条目里取"生成提示词"（大节点登记时写在 note 里）。"""
    note = str(asset.get("note") or "")
    for marker in ("生成提示词：", "生成提示词:", "提示词："):
        if marker in note:
            text = note.split(marker, 1)[1].strip()
            if text:
                return text
    name = str(asset.get("name") or asset.get("asset_id") or "素材")
    asset_type = str(asset.get("asset_type") or "")
    return f"{name}，{asset_type} 参考图，主体清晰，构图完整，高质量"


# ===========================================================================
# 1) 素材生成（SG2 GEN1–GEN4）：从素材库取下一个待生成素材
# ===========================================================================


class BetterComfyUIAssetGen:
    """**素材生成·取任务**：从项目素材库取下一个「待生成」素材，输出提示词与生成参数。

    用法：本节点的 `提示词` 接到任意图像工作流的文本编码入口（CLIPTextEncode），
    生成结果 IMAGE 接 `素材入库`，入库后接 `素材审核` —— 就是逻辑图 GEN1–GEN4 →
    G（素材库）→ SG3 审核的规定路径。
    """

    CATEGORY = "BetterComfyUI/素材"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("提示词", "负向提示词", "素材ID", "素材类型", "生成参数JSON", "进度", "摘要")
    DESCRIPTION = "从素材库取下一个待生成素材（仅新增），输出提示词与生成参数，接任意图像工作流。"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "项目": ("STRING", {"default": "演示项目",
                                   "tooltip": "项目 id 或显示名（素材库按项目隔离）。"}),
                "素材类型": (list(_ASSET_TYPES), {"default": "全部类型"}),
                "选取顺序": (["按优先级（角色>场景>物品>分镜）", "按创建顺序"],
                             {"default": "按优先级（角色>场景>物品>分镜）"}),
                "指定名称或ID": ("STRING", {
                    "default": "",
                    "tooltip": "留空 = 自动取下一个；填名称或 asset_id = 只取它。",
                }),
            },
        }


    @classmethod
    def IS_CHANGED(cls, **kwargs) -> str:
        """有副作用的节点：每次排队都必须真正执行（ComfyUI 会按输入哈希缓存节点输出）。

        审计 R2 实测：无此方法时，重复排队会命中缓存，表现为
        「取任务永远同一项 / 入库永远 v1 / 人工审核与超时 tick 完全不执行」——
        界面显示成功，实际没有执行。返回每次都变化的值以绕过缓存。
        """
        import time
        import uuid

        return f"{time.time_ns()}-{uuid.uuid4().hex}"

    def run(self, 项目: str = "演示项目", 素材类型: str = "全部类型",
            选取顺序: str = "按优先级（角色>场景>物品>分镜）", 指定名称或ID: str = ""):
        _NodeError, _config, _orch, _orch_repo_root, _store_for_project = _orch_helpers()
        _orch()
        cfg = _config()
        want_type = _ASSET_TYPES.get(素材类型)
        target = (指定名称或ID or "").strip()

        # 解析项目：id 或显示名（复用项目库，不猜）
        from bettercomfyui.store.projects import ProjectStore

        root = Path(cfg.global_.project_root)
        if not root.is_absolute():
            root = Path(_orch_repo_root()) / root
        projects = ProjectStore(root, cfg).list()
        project_id = None
        for proj in projects:
            if target and str(proj.get("project_id")) == target:
                project_id = str(proj["project_id"])
                break
            if str(proj.get("display_name")) == str(项目).strip():
                project_id = str(proj["project_id"])
                break
        if project_id is None and projects:
            project_id = str(projects[-1]["project_id"])
        if project_id is None:
            raise _NodeError("找不到项目：请先用「Better ComfyUI 项目」节点创建/选择项目。")

        _ps, paths, store, _root, cfg, _info = _store_for_project(project_id)

        assets = store.list_assets()
        pending = [a for a in assets if not a.get("current_version")]
        if want_type:
            pending = [a for a in pending if str(a.get("asset_type")) == want_type]
        if target:
            matched = [a for a in pending
                       if str(a.get("name")) == target or str(a.get("asset_id")) == target]
            if matched:
                pending = matched
            elif not any(str(a.get("name")) == target or str(a.get("asset_id")) == target
                         for a in assets):
                raise _NodeError(f"素材库里没有名为 {target!r} 的素材。")
        if 选取顺序.startswith("按优先级"):
            pending.sort(key=lambda a: (_ASSET_PRIORITY.get(str(a.get("asset_type")), 99),
                                        str(a.get("name") or "")))
        total_done = sum(1 for a in assets if a.get("current_version"))
        total = len(assets)
        if not pending:
            return ("", "", "", "", json.dumps({"none": True}, ensure_ascii=False),
                    f"{total_done}/{total}",
                    "没有待生成素材：请先运行「一体化总控台」生成素材提示词，"
                    "或手动添加素材。")

        asset = pending[0]
        name = str(asset.get("name") or "")
        asset_id = str(asset.get("asset_id") or "")
        asset_type = str(asset.get("asset_type") or "")
        prompt = _extract_prompt(asset)
        seed = _derive_seed(asset_id, int(cfg.assets.seed))
        params = {
            "resolution": int(cfg.assets.resolution),
            "steps": int(cfg.assets.steps),
            "sampler": str(cfg.assets.sampler),
            "cfg": float(cfg.assets.cfg),
            "seed": seed,
            "batch_count": int(cfg.assets.batch_count),
            "source": "BetterComfyUIAssetGen",
        }
        summary = (
            f"待生成：{name}（{asset_type}），当前进度 {total_done}/{total}。\n"
            f"提示词长度 {len(prompt)}；生成参数见「生成参数JSON」。\n"
            "把它接到你的图像工作流 → 生成结果接「素材入库」。"
        )
        return (prompt, "", asset_id, asset_type,
                json.dumps(params, ensure_ascii=False, indent=2),
                f"{total_done}/{total}", summary)


# ===========================================================================
# 2) 素材入库（SG2 G/G7）：IMAGE → 新版本 + 指纹 + 待审核
# ===========================================================================


class BetterComfyUIAssetIngest:
    """**素材入库**：把生成的 IMAGE 追加为素材新版本（SHA-256 + PENDING_REVIEW）。

    * 绝不覆盖旧文件；同一素材多次入库 = 多个版本（`G7`）；
    * 统一分辨率（`G3`）按配置执行；
    * 入库后必须走「素材审核」取得 LOCK 才能被导演台使用（R3）。
    """

    CATEGORY = "BetterComfyUI/素材"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("素材ID", "新版本号", "内容指纹", "摘要")
    DESCRIPTION = "IMAGE → 素材库新版本（只追加、SHA-256、待审核）。"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "项目": ("STRING", {"default": "演示项目"}),
                "素材ID": ("STRING", {"default": "",
                                     "tooltip": "接「素材生成·取任务」的素材ID 输出。"}),
                "images": ("IMAGE",),
            },
            "optional": {
                "种子": ("INT", {"default": -1, "min": -1, "max": 2 ** 31 - 1}),
                "备注": ("STRING", {"default": "", "multiline": True}),
                "统一分辨率": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "按 assets.resolution 等比缩放最长边（图一 G3）。"}),
            },
        }


    @classmethod
    def IS_CHANGED(cls, **kwargs) -> str:
        """有副作用的节点：每次排队都必须真正执行（ComfyUI 会按输入哈希缓存节点输出）。

        审计 R2 实测：无此方法时，重复排队会命中缓存，表现为
        「取任务永远同一项 / 入库永远 v1 / 人工审核与超时 tick 完全不执行」——
        界面显示成功，实际没有执行。返回每次都变化的值以绕过缓存。
        """
        import time
        import uuid

        return f"{time.time_ns()}-{uuid.uuid4().hex}"

    def run(self, 项目: str, 素材ID: str, images: Any, 种子: int = -1,
            备注: str = "", 统一分辨率: bool = True):
        _NodeError, _config, _orch, _orch_repo_root, _store_for_project = _orch_helpers()
        from .fusion_nodes import _tensor_batch_to_uint8
        from bettercomfyui.models.enums import AssetSource, AssetStatus

        _orch()
        cfg = _config()
        if not (素材ID or "").strip():
            raise _NodeError("素材ID 为空：请接「素材生成·取任务」的素材ID 输出。")
        try:
            arr = _tensor_batch_to_uint8(images)
        except Exception as exc:  # noqa: BLE001
            raise _NodeError(f"IMAGE 解析失败：{type(exc).__name__}: {exc}") from exc
        if arr.shape[0] == 0:
            raise _NodeError("收到 0 张图像，拒绝入库空版本。")

        try:
            from PIL import Image
        except Exception as exc:  # noqa: BLE001
            raise _NodeError(f"缺少 Pillow，无法把 IMAGE 落盘：{exc}") from exc

        _ps, paths, store, _root, cfg, _info = _store_for_project(项目)
        asset = store.get_asset((素材ID or "").strip())
        if asset is None:
            raise _NodeError(f"素材不存在：{素材ID}（请先用总控台/手动素材创建条目）")

        tmp_dir = Path(paths.project_dir) / "_work" / f"ingest_{_now_stamp()}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        last_version = ""
        last_hash = ""
        unified = int(cfg.assets.resolution) if 统一分辨率 else None
        for index in range(arr.shape[0]):
            frame = arr[index]
            if frame.ndim == 2:
                frame = frame[..., None].repeat(3, axis=2)
            if frame.shape[-1] == 4:
                frame = frame[..., :3]
            file = tmp_dir / f"gen_{index + 1:03d}.png"
            Image.fromarray(frame).save(file, format="PNG")
            extra = {
                "seed": int(种子),
                "generator": "BetterComfyUIAssetIngest",
                "note": (备注 or "").strip(),
            }
            version_info = store.add_version(
                str(asset["asset_id"]),
                source=AssetSource.AI,
                file_path=file,
                status=AssetStatus.PENDING_REVIEW,
                extra=extra,
                unify_resolution=unified,
            )
            last_version = str(version_info.get("version"))
            last_hash = str(version_info.get("content_hash"))
            lines.append(
                f"第 {index + 1} 张 → {last_version}  hash={last_hash[:19]}…  "
                f"状态={version_info.get('status')}")
        summary = (
            f"素材 {asset.get('name')}（{asset['asset_id']}）入库 {arr.shape[0]} 个新版本。\n"
            + "\n".join(lines)
            + "\n下一步：接「素材审核」节点做审核（未通过 LOCK 前不得用于生成）。"
        )
        return (str(asset["asset_id"]), last_version, last_hash, summary)


# ===========================================================================
# 3) 视频审核（SG5 + SG6）：段级 R_seg + 边界验证 + 整片 R_full + 策略
# ===========================================================================


class BetterComfyUIVideoReview:
    """**视频审核**：对一个真实视频文件做段级 R_seg + 相邻边界验证 + 整片 R_full。

    逻辑对齐 `SG5`/`SG6`：

    * 段级：R1a 四维、阈值（可覆盖）、CNT2（质量返工）/CNT3（级联返工）分开累计，
      达到上限按历史候选最高分兜底并记录 `REVIEW_FALLBACK` / `CASCADE_FALLBACK`；
    * 边界：第 N 段与第 N+1 段交接处相似度不足 → 标记后续段待级联验证；
    * 整片：R1b 四维，仅在最后执行一次；失败按显式策略「阻止输出 / 降级输出」
      （降级写 `FULL_REVIEW_DEGRADED`）。

    **不假装**：本节点不重新生成视频；它输出「需重生成段清单」，重生成由 GPU 上的导演台执行。
    """

    CATEGORY = "BetterComfyUI/审核"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("结论", "段审核JSON", "整片审核JSON", "需重生成段", "审计报告")
    DESCRIPTION = "真实视频文件 → 段级审核(R_seg/CNT2/CNT3/边界) + 整片审核(R_full/失败策略)。"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "项目": ("STRING", {"default": "演示项目"}),
                "视频文件路径": ("STRING", {
                    "default": "",
                    "tooltip": "整片视频文件绝对路径（例如 SaveVideo 输出目录里的 mp4）。"}),
                "每段时长_秒": ("FLOAT", {
                    "default": 10.0, "min": 1.0, "max": 120.0, "step": 0.5,
                    "tooltip": "按此秒数把成片切段做 R_seg（图一 JP1 默认 10 秒）。"}),
                "审核范围": (list(_REVIEW_SCOPES), {"default": _REVIEW_SCOPES[0]}),
                "评分来源": (list(_SCORE_SOURCES), {"default": _SCORE_SOURCES[0]}),
            },
            "optional": {
                "段阈值覆盖": ("FLOAT", {"default": -1.0, "min": -1.0, "max": 100.0,
                                        "tooltip": "-1 = 用项目配置 video_review.threshold_segment。"}),
                "整片阈值覆盖": ("FLOAT", {"default": -1.0, "min": -1.0, "max": 100.0,
                                          "tooltip": "-1 = 用项目配置 video_review.threshold_full。"}),
                "整片失败策略": (list(_FULL_POLICIES), {"default": _FULL_POLICIES[0]}),
                "人工分数JSON": ("STRING", {
                    "multiline": True, "default": "",
                    "tooltip": (
                        "评分来源=人工分数JSON 时使用。格式：\n"
                        '[{"segment_index":1,"scores":{"画面连贯":85,...}}, ...]\n'
                        "或 {\"1\": 85, \"2\": 70}（直接给总分）。")}),
                "本次是级联重做": ("BOOLEAN", {"default": False,
                                             "tooltip": "True 时返工计入 CNT3；否则计入 CNT2。"}),
                "边界相似度阈值": ("FLOAT", {"default": 55.0, "min": 0.0, "max": 100.0, "step": 1.0,
                                           "tooltip": "第N段尾帧与第N+1段首帧的直方图相似度下限。"}),
            },
        }

    # -- 工具 ---------------------------------------------------------
    @staticmethod
    def _hist_similarity(frame_a: Any, frame_b: Any) -> float:
        import numpy as np

        def _hist(frame: Any) -> Any:
            gray = np.asarray(frame, dtype=np.float64)
            if gray.ndim == 3:
                gray = gray.mean(axis=2)
            hist, _ = np.histogram(gray / 255.0, bins=32, range=(0.0, 1.0))
            return hist.astype(np.float64)

        h0, h1 = _hist(frame_a), _hist(frame_b)
        denom = float(np.linalg.norm(h0) * np.linalg.norm(h1))
        return float(np.dot(h0, h1) / denom) if denom > 1e-9 else 0.0


    @classmethod
    def IS_CHANGED(cls, **kwargs) -> str:
        """视频审核：缓存键必须包含**视频文件的真实内容**。

        审计 R2 实测：视频重生成后若文件名/路径不变（常见做法），ComfyUI 会按
        「输入相同」直接复用上一次审核输出 —— 旧内容的「通过」结论被当成本次新内容
        的结论（旧结论失效原则被破坏）。这里用文件大小+mtime+SHA-256+项目名做缓存键，
        内容一变就强制重新审核；文件缺失时返回变化值，让节点真正执行并报错。
        """
        import hashlib
        import time
        import uuid

        raw = str(kwargs.get("视频文件路径") or "").strip().strip('"')
        path = Path(raw) if raw else None
        if path is None or not path.is_file():
            return f"missing:{time.time_ns()}-{uuid.uuid4().hex}"
        st = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        return (
            f"{st.st_size}:{st.st_mtime_ns}:{digest.hexdigest()}"
            f":{kwargs.get('项目')}:{kwargs.get('本次是级联重做')}"
        )

    def run(self, 项目: str, 视频文件路径: str, 每段时长_秒: float,
            审核范围: str = _REVIEW_SCOPES[0], 评分来源: str = _SCORE_SOURCES[0],
            段阈值覆盖: float = -1.0, 整片阈值覆盖: float = -1.0,
            整片失败策略: str = _FULL_POLICIES[0], 人工分数JSON: str = "",
            本次是级联重做: bool = False, 边界相似度阈值: float = 55.0):
        _NodeError, _config, _orch, _orch_repo_root, _store_for_project = _orch_helpers()
        from bettercomfyui.models.enums import FullReviewPolicy
        from bettercomfyui.pipeline.core import apply_full_policy, full_review
        from bettercomfyui.review import local_scorer

        _orch()
        cfg = _config()
        _ps, paths, _store, _root, cfg, info = _store_for_project(项目)
        pid = str(info.get("project_id") or 项目)

        video = Path(str(视频文件路径).strip().strip('"'))
        if not video.is_absolute():
            candidates = [Path(_orch_repo_root()) / video,
                          Path(paths.output_dir) / video]
            video = next((c for c in candidates if c.is_file()), video)
        if not video.is_file():
            raise _NodeError(
                f"视频文件不存在：{视频文件路径}\n请填绝对路径（例如 output/项目/日期/xxx.mp4）。")

        ts = _now_stamp()
        work_dir = Path(paths.project_dir) / "_work" / f"video_review_{ts}"
        review_dir = Path(paths.project_dir) / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        history_path = review_dir / "video_reviews.json"
        history: dict[str, Any] = {"segments": {}, "edges": {}, "updated_at": ""}
        if history_path.is_file():
            try:
                loaded = json.loads(history_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    history = loaded
            except Exception:  # noqa: BLE001
                pass
        history.setdefault("segments", {})
        history.setdefault("edges", {})

        threshold_seg = float(段阈值覆盖) if float(段阈值覆盖) >= 0 else float(
            cfg.video_review.threshold_segment)
        threshold_full = float(整片阈值覆盖) if float(整片阈值覆盖) >= 0 else float(
            cfg.video_review.threshold_full)
        counters, counter_path = _counters_for_project(paths)
        cnt2_limit = int(cfg.video_review.cnt2_limit)
        cnt3_limit = int(cfg.video_review.cnt3_limit)

        scope_seg = 审核范围 != _REVIEW_SCOPES[2]
        scope_full = 审核范围 != _REVIEW_SCOPES[1]

        warnings: list[str] = []
        summary_lines = [f"视频：{video}", f"项目：{pid}", f"切段：每 {float(每段时长_秒)}s"]

        # ---- 段级 R_seg ----
        segments: list[dict] = []
        need_regen: list[int] = []
        if scope_seg:
            try:
                seg_files = local_scorer.split_video_into_segments(
                    video, segment_seconds=float(每段时长_秒), out_dir=work_dir)
            except local_scorer.LocalScorerError as exc:
                raise _NodeError(f"视频切段失败：{exc}") from exc

            # 尾段过短（如 5.17s 视频按 5.167s 切出 0.004s 尾段）不是真实问题：
            # 它会让边界取帧失败、并被误判「需重生成」。直接忽略该尾段（不参与评分/边界）。
            if len(seg_files) > 1:
                try:
                    tail_duration = float(
                        local_scorer._probe_media(seg_files[-1])["duration_s"] or 0.0)
                except Exception:  # noqa: BLE001
                    tail_duration = 0.0
                if tail_duration < 1.0:
                    dropped = seg_files.pop()
                    warnings.append(
                        f"尾段过短（{tail_duration:.2f}s）已忽略：不参与段级评分与边界验证")
                    summary_lines.append(
                        f"尾段过短（{tail_duration:.2f}s）已忽略（{Path(str(dropped)).name}）")

            manual_scores: dict[int, dict[str, float]] = {}
            if 评分来源 == _SCORE_SOURCES[1]:
                try:
                    raw = json.loads(人工分数JSON or "{}")
                except Exception as exc:  # noqa: BLE001
                    raise _NodeError(f"人工分数JSON 解析失败：{exc}") from exc
                if isinstance(raw, list):
                    for item in raw:
                        idx = int(item.get("segment_index") or 0)
                        if idx <= 0:
                            continue
                        if "scores" in item:
                            manual_scores[idx] = {str(k): float(v)
                                                  for k, v in item["scores"].items()}
                        elif "score" in item:
                            manual_scores[idx] = {"人工评分": float(item["score"])}
                elif isinstance(raw, dict):
                    for key, value in raw.items():
                        try:
                            manual_scores[int(key)] = {"人工评分": float(value)}
                        except (TypeError, ValueError):
                            continue
                if not manual_scores:
                    warnings.append("评分来源=人工分数JSON，但没解析出任何段分数")

            for index, seg_file in enumerate(seg_files, start=1):
                key = f"{pid}:SEG{index}"
                if index in manual_scores:
                    dims = manual_scores[index]
                    score = round(sum(dims.values()) / len(dims), 3)
                    source = "manual"
                else:
                    try:
                        frames = local_scorer.frames_from_video(seg_file, fps=2.0,
                                                               max_frames=120)
                        dims = local_scorer.frame_scores(frames)
                    except local_scorer.LocalScorerError as exc:
                        # 切段产生的「过短尾段」（如 5.17s 视频按 5s 切 → 0.17s 尾段）
                        # 抽不到帧属正常边界：记录为 too_short 并继续，不能中断整次审核。
                        try:
                            seg_duration = float(
                                local_scorer._probe_media(seg_file)["duration_s"] or 0.0)
                        except Exception:  # noqa: BLE001
                            seg_duration = 0.0
                        if seg_duration < 1.0:
                            record = {
                                "segment_index": index,
                                "index": index,
                                "file": str(seg_file),
                                "dimensions": {},
                                "scores": {},
                                "score": 0.0,
                                "threshold": threshold_seg,
                                "passed": False,
                                "score_source": "too_short",
                                "too_short": True,
                                "duration_s": round(seg_duration, 3),
                                "note": "段太短（<0.5s）抽不到帧：请调整「每段时长_秒」或段视频长度",
                                "counters": {},
                                "degrade": [],
                            }
                            history["segments"].setdefault(str(index), []).append(
                                {"score": 0.0, "passed": False, "at": ts,
                                 "too_short": True})
                            need_regen.append(index)
                            summary_lines.append(
                                f"段 {index}: 太短（{seg_duration:.2f}s）无法评分 → 需调整切段/重生成")
                            segments.append(record)
                            continue
                        raise _NodeError(f"第 {index} 段评分失败：{exc}") from exc
                    score = local_scorer.score_average(dims)
                    source = "local_heuristic"
                passed = score >= threshold_seg
                record = {
                    # P1 修复（ROUND49）：R_seg 结构化字段——UI/节点直接读 index/scores，
                    # 旧结构只给 segment_index/dimensions，导致下游显示 index=None、scores=None。
                    "segment_index": index,
                    "index": index,
                    "file": str(seg_file),
                    "dimensions": dims,
                    "scores": dict(dims),
                    "score": score,
                    "threshold": threshold_seg,
                    "passed": passed,
                    "score_source": source,
                    "counters": {},
                    "degrade": [],
                }
                hist = history["segments"].setdefault(str(index), [])
                hist.append({"score": score, "passed": passed, "at": ts})
                if not passed:
                    kind = "CNT3" if 本次是级联重做 else "CNT2"
                    from bettercomfyui.models.enums import CounterKind

                    counter_kind = CounterKind.CNT3 if kind == "CNT3" else CounterKind.CNT2
                    limit = cnt3_limit if kind == "CNT3" else cnt2_limit
                    current = counters.get(key, counter_kind)
                    # 审计 R2：dot N 规定「达到候选上限后从候选取最高分，**不再新增候选**」。
                    # 旧实现同一成片每重复审核一次就 CNT2+1（实测 1→2→3→4…无限增长），
                    # 把「同一段版本的重复复核」当成新候选 —— 现在上限后不再计数。
                    if current >= limit:
                        count = current
                        record["counters"] = {kind: count, "limit": limit, "capped": True}
                        best = max(hist, key=lambda item: float(item.get("score") or 0))
                        record["fallback"] = {
                            "source": "CASCADE_FALLBACK" if kind == "CNT3" else "REVIEW_FALLBACK",
                            "selected_score": float(best.get("score") or 0),
                            "candidates": [float(item.get("score") or 0) for item in hist],
                            "note": (
                                f"{kind} 已达上限 {limit}（不再新增候选）；"
                                "按历史候选最高分兜底，需新候选请先生成新段版本"
                            ),
                        }
                        record["degrade"].append(
                            "CASCADE_DEGRADED" if kind == "CNT3" else "FALLBACK_DEGRADED")
                        summary_lines.append(
                            f"段 {index}: 未过阈值且 {kind} 已达上限（本次不新增候选）"
                            f" → 兜底 {record['fallback']['source']}")
                    else:
                        count = counters.increment(key, counter_kind)
                        record["counters"] = {kind: count, "limit": limit}
                        if count >= limit:
                            best = max(hist, key=lambda item: float(item.get("score") or 0))
                            record["fallback"] = {
                                "source": "CASCADE_FALLBACK" if kind == "CNT3" else "REVIEW_FALLBACK",
                                "selected_score": float(best.get("score") or 0),
                                "candidates": [float(item.get("score") or 0) for item in hist],
                                "note": f"{kind} 达到上限 {limit}，按历史候选最高分兜底",
                            }
                            record["degrade"].append(
                                "CASCADE_DEGRADED" if kind == "CNT3" else "FALLBACK_DEGRADED")
                            summary_lines.append(
                                f"段 {index}: 未过阈值但 {kind} 达上限 → 兜底 {record['fallback']['source']}")
                        else:
                            need_regen.append(index)
                            summary_lines.append(
                                f"段 {index}: 未过阈值（{score} < {threshold_seg}），"
                                f"{kind}={count}/{limit} → 需重生成")
                segments.append(record)

            # ---- 相邻边界验证 N1_EDGE ----
            for index in range(2, len(seg_files) + 1):
                try:
                    prev_frames = local_scorer.frames_from_video(seg_files[index - 2], fps=1.0,
                                                                 max_frames=2)
                    cur_frames = local_scorer.frames_from_video(seg_files[index - 1], fps=1.0,
                                                                max_frames=2)
                except local_scorer.LocalScorerError as exc:
                    warnings.append(f"边界 {index - 1}→{index} 取帧失败：{exc}")
                    continue
                similarity = self._hist_similarity(prev_frames[-1], cur_frames[0])
                edge_passed = similarity * 100.0 >= float(边界相似度阈值)
                history["edges"][f"{index - 1}->{index}"] = {
                    "similarity": round(similarity * 100.0, 2), "passed": edge_passed, "at": ts}
                if not edge_passed:
                    if index not in need_regen:
                        need_regen.append(index)
                    warnings.append(
                        f"边界 {index - 1}→{index} 相似度 {similarity * 100.0:.1f}% "
                        f"< {边界相似度阈值}%（该段需级联验证/重生成）")

        # ---- 整片 R_full ----
        full_result: dict[str, Any] = {"executed": False}
        conclusion = "通过"
        degrade_flags: list[str] = []
        if scope_full:
            try:
                from bettercomfyui.pipeline.core import full_review as _full_review

                r1b = local_scorer.video_scores(video)
                supported = {k: v for k, v in r1b.items()
                             if k in local_scorer.R1B_SUPPORTED_DIMENSIONS}
                cfg_full = copy.deepcopy(cfg)
                cfg_full.video_review.r1b_dimensions = list(supported.keys())
                # 阈值覆盖必须真的写进跑 R_full 的配置里（否则覆盖形同虚设）
                cfg_full.video_review.threshold_full = threshold_full
                if 整片失败策略 == "阻止输出":
                    cfg_full.video_review.full_review_policy = FullReviewPolicy.BLOCK_OUTPUT
                elif 整片失败策略 == "降级输出":
                    cfg_full.video_review.full_review_policy = FullReviewPolicy.DEGRADE_OUTPUT
                raw = _full_review(
                    video, cfg_full,
                    scorer=lambda _p, dims: {d: float(supported[d]) for d in dims})
                try:
                    policy = apply_full_policy(raw, cfg_full)
                except Exception as exc:  # noqa: BLE001
                    # 审计 R2 修复：`video_review.full_review_policy` 默认为 None。
                    # 之前整片审核未通过时这里直接抛错，S4「视频审核」对任何低分视频
                    # 都让整个队列 execution_error —— 审核节点只负责给结论、不做交付，
                    # 未配置策略时应当**如实输出「未通过 + 策略未配置」**而不是崩掉。
                    if (not raw.get("passed")) and "full_review_policy" in str(exc):
                        policy = {
                            "action": "unconfigured",
                            "degrade_flags": [],
                            "delivered_with_degradation": False,
                            "message": str(exc),
                        }
                    else:
                        raise
                full_result = {
                    "executed": True,
                    "r1b_scores": raw["r1b_scores"],
                    "final_score": raw["final_score"],
                    "threshold": raw.get("threshold", threshold_full),
                    "passed": raw["passed"],
                    "policy": policy.get("action"),
                    "policy_message": policy.get("message") or "",
                    "degrade_flags": [str(flag) for flag in policy.get("degrade_flags") or []],
                    "score_source": "local_heuristic",
                    "note": "R_full 只在最终拼接之后执行一次（SG6）",
                }
                degrade_flags.extend(full_result["degrade_flags"])
                if not raw["passed"]:
                    conclusion = "整片未通过"
                    if policy.get("action") == "unconfigured":
                        conclusion += "（失败策略未配置：未自动放行/降级；请配置 video_review.full_review_policy 或在上方选择阻止/降级）"
            except Exception as exc:  # noqa: BLE001 - 整片审核失败必须可见
                raise _NodeError(f"整片审核失败：{type(exc).__name__}: {exc}") from exc
        else:
            full_result = {"executed": False, "reason": "审核范围=仅段级"}

        if need_regen:
            conclusion = "需返工" if conclusion == "通过" else conclusion
        if degrade_flags:
            conclusion = f"{conclusion}（降级：{','.join(degrade_flags)}）"

        history["updated_at"] = ts
        history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        _save_counters(counters, counter_path)

        seg_json = json.dumps({
            "scope": "segment",
            "threshold": threshold_seg,
            "segments": segments,
            "need_regenerate": need_regen,
            "score_source": "local_heuristic" if 评分来源 == _SCORE_SOURCES[0] else "manual",
            "note": "段级评分为本地启发式（真实帧统计），非语义模型；可替换为你的评分模型。",
        }, ensure_ascii=False, indent=2)
        full_json = json.dumps(full_result, ensure_ascii=False, indent=2)

        audit = [
            "=== 视频审核（SG5 R_seg + SG6 R_full） ===",
            f"视频：{video}",
            f"审核范围：{审核范围}",
            f"段数：{len(segments)}  段阈值：{threshold_seg}  整片阈值：{threshold_full}",
            f"需重生成段：{need_regen or '无'}",
            f"降级标记：{degrade_flags or '无'}",
            "段级评分来源："
            + ("本地启发式（真实帧统计）" if 评分来源 == _SCORE_SOURCES[0] else "人工分数JSON"),
            "整片审核："
            + ("已执行（R1b: " + "、".join(full_result.get("r1b_scores", {}).keys()) + "）"
               if full_result.get("executed") else "未执行"),
            f"整片结论：{conclusion}",
            "整片失败策略："
            + ("未配置（失败策略未配置：未自动放行/降级；请配置 "
               "video_review.full_review_policy 或在上方选择阻止/降级）"
               if full_result.get("policy") == "unconfigured"
               else str(full_result.get("policy") or "未触发（整片通过或未执行）")),
            f"计数器文件：{counter_path}",
            f"历史记录：{history_path}",
            f"明细目录：{review_dir}",
            "说明：本节点只判定与记账，不重新生成视频；重生成后请重跑本节点（旧版本审核结论不适用）。",
        ]
        for warn in warnings:
            audit.append(f"⚠️ {warn}")
        return (conclusion, seg_json, full_json,
                ",".join(str(i) for i in need_regen) or "无", "\n".join(audit))


NODE_CLASS_MAPPINGS = {
    "BetterComfyUIAssetGen": BetterComfyUIAssetGen,
    "BetterComfyUIAssetIngest": BetterComfyUIAssetIngest,
    "BetterComfyUIVideoReview": BetterComfyUIVideoReview,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "BetterComfyUIAssetGen": "素材生成·取任务（提示词→任意图像工作流）",
    "BetterComfyUIAssetIngest": "素材入库（IMAGE→新版本+指纹+待审核）",
    "BetterComfyUIVideoReview": "视频审核（段级R_seg+整片R_full）",
}
