"""第二阶段接入点：**保留但不暴露**。

用户明确要求：「第二阶段那个入口保留那个接入点，但是又去掉 UI 界面，不要让他在用户在上面展示出来」。

因此本模块的约定是：

1. **不注册任何 ComfyUI 节点**（`nodes.py` 的 `NODE_CLASS_MAPPINGS` 里没有它们，
   `PHASE2_NODES_EXPOSED = False` 是显式声明）；
2. **不在网页面板里出现**（`web/js/bettercomfyui_panel.js` 里有对应的隐藏注释）；
3. 但接口本身**完整实现为可调用的后端 API**，将来只需在 `nodes.py` 里加两行注册即可上屏。

这样既满足「保留接入点」，又满足「不在 UI 展示」。

方案第九节原文：
    第二阶段希望它能自动获取剧本、自动获取下一段剧本、继续开启新的工作流，
    通过外部平台 API 或操作方式直接把视频发布上去，实现真正的全自动化。

方案第五部分第 35 项修复：
    "第二阶段接口从成片输出继续还是从项目状态继续" -> **OUT6 从 PROJ 继续，不从成片输出继续**。

所以 `next_chapter_for_project()` 刻意从**项目状态**（章节列表 + 进度）推导下一步，
而不是从成片文件推导 —— 这是方案明确的裁定，实现时不要改。

⚠️ 未实现部分如实标注：`fetch_next_chapter` 的剧本来源、`publish_movie` 的平台客户端
在本阶段都是**接缝（seam）**，默认实现会抛 `NotImplementedError` 并说明需要什么。
**绝不返回假成功**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

__all__ = [
    "Phase2Disabled",
    "ChapterSource",
    "PublishTarget",
    "PublicationResult",
    "next_chapter_for_project",
    "fetch_next_chapter",
    "publish_movie",
    "phase2_status",
]

#: 显式声明：本模块不产生任何 UI 节点。
EXPOSED_IN_UI = False


class Phase2Disabled(RuntimeError):
    """第二阶段功能被调用但尚未配置。信息面向使用者。"""


class ChapterSource(Protocol):
    """剧本来源接缝。实现方负责真正去取剧本（爬虫 / 作者接口 / 人工上传）。"""

    name: str

    def fetch_chapter(self, *, project_id: str, chapter_index: int) -> dict:
        """返回结构化剧本 dict（至少含 chapters[0].segments）。

        实现方**必须**返回真实内容或抛错；不允许返回占位剧本冒充成功。
        """
        ...


class PublishTarget(Protocol):
    """发布目标接缝。实现方负责调用外部平台 API。"""

    name: str

    def publish(self, *, movie: Path, metadata: dict) -> "PublicationResult":
        ...


@dataclass(frozen=True)
class PublicationResult:
    ok: bool
    platform: str
    url: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# OUT6：从项目状态继续（方案明确的裁定）
# ---------------------------------------------------------------------------


def next_chapter_for_project(project_id: str, *, repo_root: Path, config: Any = None) -> dict:
    """从**项目状态**推导下一章应该做什么（OUT6）。

    返回：
        {"project_id", "has_next", "next_chapter_index", "done_chapters",
         "resume_from", "reason"}

    `resume_from` 的语义是「从哪个子流程进入」：
        * `"PROJ"` —— 项目还没有任何章节，需要先导入剧本；
        * `"DIRECTOR"` —— 有章节但还没进入导演台；
        * `"OUTPUT"` —— 已有章节且已产出成片，可以进入下一章或发布。

    为什么从项目状态而不是成片：第二章可能还没拍，但项目已经在推进；
    只看成片文件会把「有进度但没输出」的项目误判为「没开始」。
    """
    from importlib import import_module

    paths_mod = import_module("bettercomfyui.store.paths")
    proj_mod = import_module("bettercomfyui.store.projects")
    cfg_mod = import_module("bettercomfyui.config")

    repo_root = Path(repo_root)
    if config is None:
        config = cfg_mod.load_config(repo_root=repo_root)

    projects_root = Path(config.global_.project_root)
    if not projects_root.is_absolute():
        projects_root = repo_root / projects_root

    ps = proj_mod.ProjectStore(projects_root, config)
    ps.open(project_id)                                # 不存在即抛错
    paths = paths_mod.ProjectPaths(root=projects_root, project_id=project_id)

    chapters_dir = paths.chapters_dir
    chapter_files = sorted(chapters_dir.glob("*.json")) if chapters_dir.exists() else []
    done = [f.stem for f in chapter_files]

    out_dir = paths.output_dir
    has_output = any(out_dir.rglob("*.mp4")) if out_dir.exists() else False

    if not done:
        return {
            "project_id": project_id,
            "has_next": True,
            "next_chapter_index": 1,
            "done_chapters": [],
            "resume_from": "PROJ",
            "reason": "项目还没有任何章节；需要先导入第 1 章剧本",
        }
    if has_output:
        return {
            "project_id": project_id,
            "has_next": True,
            "next_chapter_index": len(done) + 1,
            "done_chapters": done,
            "resume_from": "OUTPUT",
            "reason": f"已完成 {len(done)} 章并产出成片；可进入第 {len(done) + 1} 章或发布",
        }
    return {
        "project_id": project_id,
        "has_next": True,
        "next_chapter_index": len(done) + 1,
        "done_chapters": done,
        "resume_from": "DIRECTOR",
        "reason": f"已有 {len(done)} 章但尚无成片；应先跑导演台与输出",
    }


# ---------------------------------------------------------------------------
# 自动抓取剧本 / 自动发布（接缝，默认未实现）
# ---------------------------------------------------------------------------


def fetch_next_chapter(
    project_id: str,
    *,
    repo_root: Path,
    source: ChapterSource | None = None,
    save: bool = True,
) -> dict:
    """自动获取下一章剧本。

    `source=None` 时抛 `Phase2Disabled` 并说明需要什么 —— **不返回假剧本**。
    """
    if source is None:
        plan = next_chapter_for_project(project_id, repo_root=repo_root)
        raise Phase2Disabled(
            "自动抓取剧本尚未接入剧本来源（ChapterSource）。\n"
            f"当前项目状态：{plan['reason']}（下一章索引 {plan['next_chapter_index']}）\n"
            "接入方法：实现 ChapterSource.fetch_chapter(project_id, chapter_index) 并传入 source=。\n"
            "设计约束（方案第九节）：这是**第二阶段**功能；第一阶段不做自动抓取，"
            "以便主流程先稳定。"
        )

    plan = next_chapter_for_project(project_id, repo_root=repo_root, config=None)
    chapter = source.fetch_chapter(
        project_id=project_id, chapter_index=plan["next_chapter_index"]
    )
    if not isinstance(chapter, dict) or not chapter.get("chapters"):
        raise ValueError(
            f"ChapterSource {getattr(source, 'name', '?')!r} 返回的剧本结构非法："
            "必须是含 chapters 的 dict。拒绝写入空剧本。"
        )

    if save:
        from importlib import import_module

        paths_mod = import_module("bettercomfyui.store.paths")
        cfg_mod = import_module("bettercomfyui.config")

        cfg = cfg_mod.load_config(repo_root=Path(repo_root))
        projects_root = Path(cfg.global_.project_root)
        if not projects_root.is_absolute():
            projects_root = Path(repo_root) / projects_root
        paths = paths_mod.ProjectPaths(root=projects_root, project_id=project_id)
        paths.ensure_layout()
        cid = f"CH{plan['next_chapter_index']:03d}"
        target = paths.chapter_json(cid)
        target.write_text(json.dumps(chapter, ensure_ascii=False, indent=2), encoding="utf-8")
        chapter["_saved_to"] = str(target)

    return chapter


def publish_movie(
    movie: Path,
    *,
    metadata: dict | None = None,
    target: PublishTarget | None = None,
) -> PublicationResult:
    """把成片发布到外部平台。

    `target=None` 时抛 `Phase2Disabled`。**绝不假装已发布。**

    许可提醒：方案候选清单里的 `ComfyUI-CometAPI` 为**非商用许可**
    （明文禁止公司内部业务 / 客户交付），商业交付不可直接采用。
    """
    movie = Path(movie)
    if not movie.exists():
        raise FileNotFoundError(f"待发布成片不存在：{movie}")
    if target is None:
        raise Phase2Disabled(
            "自动发布尚未接入平台客户端（PublishTarget）。\n"
            f"待发布文件：{movie}\n"
            "接入方法：实现 PublishTarget.publish(movie=..., metadata=...) 并传入 target=。\n"
            "注意许可：方案候选的 ComfyUI-CometAPI 为非商用许可，商业交付不可直接用。"
        )
    meta = metadata or {}
    if meta.get("generation_provenance", {}).get("is_mock"):
        raise Phase2Disabled(
            "拒绝发布：该成片的 generation_provenance.is_mock=True，"
            "说明它是占位媒体而非模型生成结果。"
            "把占位产出发布到外部平台等同于对外声称虚假产出。"
        )
    return target.publish(movie=movie, metadata=meta)


def phase2_status() -> dict:
    """汇总第二阶段接入点状态（供后端调用 / 日志，不在 UI 展示）。"""
    return {
        "exposed_in_ui": EXPOSED_IN_UI,
        "nodes_registered": [],
        "chapter_source": "未接入（seam: ChapterSource）",
        "publish_target": "未接入（seam: PublishTarget）",
        "resume_entry": "OUT6 -> next_chapter_for_project()（从项目状态继续，不从成片继续）",
        "blocked_on": [
            "自动抓取剧本需要确定剧本来源与授权",
            "自动发布需要目标平台 API 与账号授权",
            "发布前必须确认 generation_provenance.is_mock is False",
        ],
    }
