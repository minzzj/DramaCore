"""端到端演示流程：剧本 → 素材 → 视频段 → 审核 → 拼接 → 输出。

**这份模块的地位（务必先读）**

它是第一阶段的「最小闭环」证据，也是任务书验收 15 的落点。限定条件必须写在最前面：

    视频内容由 **ffmpeg 合成的占位媒体**（纯色 + 正弦音）产生，**不是模型生成结果**。
    因此本流程证明的是：
        「编排逻辑、版本与审核语义、LOCK 授权门禁、计数器口径、级联终止、落盘与恢复」
    它**不证明**「真实视频生成可用」。

`run_demo()` 返回值里带 `used_mock` / `real_generation` / `unverified` 字段，
调用方（CLI / 报告 / 验收）**必须**据此区分。把 `used_mock=True` 的结果描述成
「真实生成已通过」是任务书第 9 节明令禁止的行为。

真实视频链路的前提（**一律以 `python -m bettercomfyui.cli doctor` 现场探测为准，不要写死版本**）：
    1. ComfyUI 版本满足 MiniMaxH3 导演台系的下限（>=0.30）；
    2. `models/` 下存在必需类别的真实权重。
不满足时只能验证编排逻辑；真实出片请到 ComfyUI 里运行导演台/融合工作流。

## 执行器契约（防假成功）

`run_demo` **只**有「ffmpeg 占位媒体」一条产出路径，没有任何真实 ComfyUI 生成接线。
因此 `executor` 只接受 `"mock"`；传 `"comfyui"` 等值会**失败闭合**（ok=False + 可操作错误），
绝不允许「继续跑占位媒体却把 `is_mock=False / executor=comfyui` 写进元数据」。


为什么不用 MockExecutor 直接产出视频：占位媒体必须是**合法可拼接**的媒体，
否则后面的 splice/encode 验证会退化成「假通过」——链条最弱的一环会掩盖真实问题。
所以这里调用 ffmpeg 生成真实可播放的占位片段，并明确标注其性质。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models.entities import MovieMetadata, SegmentAttempt, SegmentManifestItem
from .models.enums import (
    AssetSource,
    AssetStatus,
    AssetType,
    DegradeFlag,
    FullReviewPolicy,
    LibraryScope,
    ReviewMode,
    SegmentSource,
    SegmentStatus,
    Stage,
)

__all__ = ["DEMO_SCRIPT", "DemoReport", "run_demo"]


def _hash_file(path: Path) -> str:
    """段文件内容指纹（sha256:<hex>）。元数据必须能追溯到具体文件内容。"""
    import hashlib

    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


#: 内置演示剧本。刻意包含：
#:   - 两个不同角色（验证「不存在 -> 新增」分支）
#:   - 场景复用（验证「已存在 -> 复用，不重复生成」，R1 的核心）
#:   - 三个镜头（验证分段、顺序拼接与依赖链）
#:
#: ⚠️ 形状必须是 **`validate_script` 的规范形状**：`assets` 在**顶层**、
#: 用 `asset_type` 键。此前它把 assets 放在章节级、用 `type` 键 ——
#: 于是「内置剧本通不过自己的校验」，而消费方读章节级又能跑，
#: 两半各自都能过、接缝对不上（ROUND16 实测为假成功）。
#: 现在统一到规范形状，并由 `run_demo` 做结构校验闸门。
DEMO_SCRIPT: dict[str, Any] = {
    "script_id": "demo",
    "assets": [
        {"id": "c1", "asset_type": "CHARACTER", "name": "林秀",
         "prompt": "年轻女性剑客，青色长袍"},
        {"id": "c2", "asset_type": "CHARACTER", "name": "老陈",
         "prompt": "白须老者，粗布衣裳"},
        {"id": "s1", "asset_type": "SCENE", "name": "客栈大堂",
         "prompt": "木质客栈大堂，油灯昏黄"},
    ],
    "chapters": [
        {
            "title": "演示章节",
            "segments": [
                {"prompt": "林秀推门走进客栈大堂，镜头缓推", "asset_refs": ["c1", "s1"],
                 "duration_s": 3},
                {"prompt": "老陈抬头看向门口，特写", "asset_refs": ["c2", "s1"],
                 "duration_s": 3},
                {"prompt": "林秀在桌边坐下，中景", "asset_refs": ["c1", "s1"],
                 "duration_s": 3},
            ],
        }
    ],
}


@dataclass
class DemoReport:
    """端到端运行报告。所有「未做/未验证」必须显式列出，不得省略。"""

    ok: bool = False
    project_id: str | None = None
    used_mock: bool = True
    real_generation: bool = False
    real_generation_reason: str = ""
    assets_created: int = 0
    assets_reused: int = 0
    assets_suspect: int = 0
    assets_locked: int = 0
    segments_planned: int = 0
    segments_generated: int = 0
    segments_passed: int = 0
    segments_fallback: int = 0
    full_review_calls: int = 0
    spliced: bool = False
    encoded: bool = False
    output_path: str | None = None
    metadata_path: str | None = None
    degrade_flags: list[str] = field(default_factory=list)
    event_lines: int = 0
    checkpoints: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["degrade_flags"] = list(self.degrade_flags)
        return d


def _step(report: DemoReport, name: str, **detail: Any) -> None:
    report.steps.append({"step": name, **detail})


def _make_placeholder_video(
    path: Path, *, duration_s: float, fps: int, resolution: str, color: str
) -> None:
    """用 ffmpeg 合成一段**合法可拼接**的占位视频（纯色 + 正弦音轨）。

    必须是合法媒体：这样 splice/encode 才是在验证真实链路，
    而不是在验证「一堆坏文件拼起来也会成功」。
    """
    from .pipeline.core import _ffmpeg_exe

    height = {"720P": 720, "1080P": 1080, "4K": 2160}.get(resolution, 720)
    width = int(height * 16 / 9)
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _ffmpeg_exe(), "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}:r={fps}:d={duration_s}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration_s}",
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(
            "占位视频生成失败（占位媒体也必须是合法媒体，否则后续验证形同虚设）："
            f"{(proc.stderr or '')[-600:]}"
        )


def _make_placeholder_image(path: Path, *, seed: str) -> bytes:
    """产出确定性的占位参考图。返回其字节，供调用方直接落盘。"""
    from .executors.mock import _png_bytes  # noqa: PLC2701 - 复用其确定性 PNG 生成

    path.parent.mkdir(parents=True, exist_ok=True)
    data = _png_bytes(256, 256, seed.encode("utf-8"))
    path.write_bytes(data)
    return data


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def run_demo(
    *,
    repo_root: Path,
    config,
    project: str | None = None,
    chapter: Path | None = None,
    executor: str = "mock",
    workdir: Path | None = None,
) -> dict:
    """跑一次端到端最小闭环。失败不吞异常：写入 `report.error` 且 `ok=False`。

    **执行器契约（硬性，防假成功）**

    本函数**只**产出 ffmpeg 合成的占位媒体。它没有任何「真实 ComfyUI 生成」接线：
    `executor != "mock"` 时若继续往下跑，产出仍是占位媒体，却会被标成
    `is_mock=False / executor="comfyui"` 写进成片元数据 —— 那是把模拟结果
    说成真实生成（任务书第 9 节明令禁止）。因此这里**失败闭合**：
    非 mock 执行器直接返回 `ok=False` 且带可操作错误，不产出任何假成片。

    真实出片请在 ComfyUI 里运行导演台/融合工作流（S2 / S3 / W3 / 00_【总流程】剧本→成片（GPU））。
    """
    repo_root = Path(repo_root)
    report = DemoReport()

    # 图一 SG7 Z4：把 global.log_level 真正应用到根 logger。
    # 放在最前面 —— 后面每一步的日志都该按配置的级别输出。
    try:
        from .logging_setup import apply_logging

        apply_logging(config)
    except Exception:  # noqa: BLE001 - 日志没配上不该挡住主流程
        pass

    # 本函数只有占位媒体一条路径：`used_mock` 恒为 True，任何分支都必须如实标注。
    report.used_mock = True
    report.real_generation = False
    report.unverified = [
        "真实视频模型生成（占位媒体由 ffmpeg 合成，非模型输出）",
        "第三方导演节点加载与运行（ChainDirector / AIMixer / SmartDirector / H3-Chinese-Director）",
        "真实提示词模型调用（未配置外部 API）",
        "24GB 目标机的显存与性能表现",
        "真实质量评分模型（演示使用确定性打分器）",
    ]

    if executor != "mock":
        report.ok = False
        report.real_generation_reason = (
            f"executor={executor!r} 未接入本编排演示：本函数只产出 ffmpeg 占位媒体，"
            "没有真实 ComfyUI 生成执行器"
        )
        report.error = (
            f"拒绝以 executor={executor!r} 运行 end-to-end 演示：run_demo 只产出 "
            "ffmpeg 合成的占位媒体，没有真实 ComfyUI 生成执行器接线。"
            "继续运行的后果是「占位媒体被标成真实生成」的假成功。\n"
            "真实出片请到 ComfyUI 里运行导演台/融合工作流："
            "S2_单节点出片（GPU）/ S3_素材生成入库审核（GPU）/ W3_全流程出片 / "
            "00_【总流程】剧本→成片（GPU）。"
        )
        return report.to_dict()

    # 原因必须**现场探测**，不能写死版本。
    # 曾经这里硬编码「本机 ComfyUI 0.22.3 < 0.30」，而开发环境早已是 0.41.0 ——
    # 同一台机器上 doctor 报 0.41.0、demo 报 0.22.3，用户不知道该信哪个。
    from bettercomfyui.cli import _probe_comfyui

    base = getattr(getattr(config, "global_", None), "comfyui_base_path", None)
    probe = _probe_comfyui(base)
    ver = probe.get("version")
    weights = probe.get("model_files", 0)
    bits: list[str] = []
    if not base:
        bits.append("未配置 global.comfyui_base_path")
    elif not probe.get("found"):
        bits.append(f"配置的 ComfyUI 路径不存在（{base}）")
    else:
        bits.append(f"本机 ComfyUI {ver or '版本未知'}")
    if weights == 0:
        bits.append("models/ 无任何模型权重")
    report.real_generation_reason = (
        "；".join(bits) + "，真实生成不可用；视频内容由 ffmpeg 合成的占位媒体产生"
    )
    try:
        _run_flow(
            repo_root=repo_root, config=config,
            project=project, chapter=chapter, workdir=workdir, report=report,
        )
        report.ok = True
    except Exception as exc:  # noqa: BLE001 - 演示流程必须给出可诊断的失败
        report.ok = False
        report.error = f"{type(exc).__name__}: {exc}"
    return report.to_dict()


def _run_flow(
    *,
    repo_root: Path,
    config,
    project: str | None,
    chapter: Path | None,
    workdir: Path | None,
    report: DemoReport,
) -> None:
    from .director.deps import default_counters, default_ids
    from .director.generate import SegmentGenerator
    from .director.plan import plan_segments
    from .director.review import SegmentReviewer
    from .pipeline.core import (
        apply_full_policy,
        build_splice_inputs,
        encode,
        full_review,
        probe_media,
        splice,
        write_output,
    )
    from .prompt.match import match_candidate
    from .review.asset_review import AssetReviewer
    from .review.counters import CounterStore
    from .store.assets import AssetStore
    from .store.checkpoints import CheckpointStore
    from .store.paths import ProjectPaths
    from .store.projects import ProjectStore

    projects_root = repo_root / config.global_.project_root
    pstore = ProjectStore(projects_root, config)

    # ---- 1. 项目 ----
    if project:
        info = pstore.open(project)
        project_id = project
    else:
        info = pstore.create("端到端演示")
        project_id = info["project_id"]
    report.project_id = project_id
    paths = ProjectPaths(root=projects_root, project_id=project_id)
    paths.ensure_layout()
    assets = AssetStore(paths)
    checkpoints = CheckpointStore(paths)
    _step(report, "project", project_id=project_id, display_name=info.get("display_name"))

    # ---- 2. 剧本 ----
    if chapter is not None:
        script = json.loads(Path(chapter).read_text(encoding="utf-8"))
    else:
        script = json.loads(json.dumps(DEMO_SCRIPT))

    # ---- 2a. 边界归一 + **结构校验闸门** ----
    # 两件事，一起修了两个缺口：
    #
    # ① 形状接缝：内置 DEMO_SCRIPT 与历史调用方用的是「章节级 assets + type 键」，
    #    而 validate_script 的规范形状是「顶层 assets + asset_type 键」。
    #    不归一的话，下面读规范 `assets` 会一次都不进循环 ——
    #    素材阶段整段静默跳过，而本函数仍返回 ok=True（**假成功**）。
    #
    # ② 结构校验闸门：任务书 §B 要求「缺字段、无效引用、重复 ID 或格式错误时，
    #    **不得直接进入素材生成**」。此前这条路径只做了 json.loads 语法检查。
    #
    # ⚠️ 必须**抛异常**而不是 `return`：`run_demo` 在外层 try 之后会无条件
    #    置 `report.ok = True`，提前 return 只会退出 `_run_flow` 而 ok 仍是 True
    #    （第一版就这么写的，实测 ok=True 但确实没生成 —— 报表却显示成功）。
    from .prompt.validate import canonicalize_script, validate_script

    script = canonicalize_script(script)
    try:
        existing_names = [a.get("name") for a in assets.list_assets() if a.get("name")]
        script = validate_script(script, existing_assets=existing_names)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "剧本结构校验未通过，已在进入素材生成前中止"
            f"（任务书 §B：缺字段/无效引用/重复 ID 不得直接进入生成）：\n{exc}"
        ) from exc

    script["project_id"] = project_id
    _step(report, "script", chapters=len(script.get("chapters", [])),
          assets=len(script.get("assets") or []))

    # ---- 3. 素材：程序确定性匹配 -> 生成 -> 审核 -> LOCK ----
    # 用真实 AssetReviewer 走正式审核周期与唯一提交点，**不绕过**审核直接写 LOCK。
    reviewer_assets = AssetReviewer(
        assets,
        config=config,
        counters=CounterStore(),
        project_id=project_id,
        project=paths,  # 必须传：否则审核事件只留在内存，不算「已记录」
        model_review=lambda material: {"score": 88.0, "passed": True, "reason": "演示模型审核"},
    )

    created: dict[str, str] = {}
    locked_assets: dict[str, dict] = {}
    seen_names: set[str] = set()

    # ⚠️ 读**规范形状**（顶层 assets + asset_type）。
    # 老代码读 `chapter_obj.get("assets")` + `cand["type"]`，与
    # validate_script 的输出对不上 —— 规范剧本进来时这里一次都不进循环，
    # 素材阶段静默跳过却返回 ok=True（**假成功**，ROUND16 实测发现）。
    for cand in script.get("assets") or []:
        cand_type = cand["asset_type"]
        cand_name = cand["name"]
        result = match_candidate(
            {"type": cand_type, "name": cand_name,
             "library_scope": LibraryScope.PROJECT.value},
            assets,
        )
        outcome = str(getattr(result, "outcome", result))
        if "EXISTS_REUSE" in outcome:
            report.assets_reused += 1
            asset_id, version = result.asset_id, result.version
        elif "SUSPECT" in outcome:
            report.assets_suspect += 1
            _step(report, "asset_suspect", name=cand_name, suspect_id=result.suspect_id)
            continue
        else:
            entry = assets.create_asset(
                asset_type=AssetType.parse(cand_type),
                name=cand_name, source=AssetSource.AI,
                library_scope=LibraryScope.PROJECT,
                per_class_limit=config.assets.per_class_limit,
            )
            asset_id = entry["asset_id"]
            seed_path = paths.project_dir / "_seed" / f"{asset_id}.png"
            _make_placeholder_image(seed_path, seed=f"{project_id}:{cand_name}")
            vrec = assets.add_version(
                asset_id, source=AssetSource.AI,
                file_path=seed_path, status=AssetStatus.DRAFT,
                # 图一 SG2 G3：素材库统一分辨率（等比缩放到最长边 = assets.resolution）。
                unify_resolution=config.assets.resolution,
            )
            version = vrec["version"]
            report.assets_created += 1

        created[cand["id"]] = asset_id
        seen_names.add(cand_name)

        ver = assets.get_version(asset_id, version)
        out = reviewer_assets.review_version(
            asset_id, version, mode=ReviewMode.MODEL,
            material={"file_path": ver.get("file_path"), "content_hash": ver["content_hash"]},
        )
        if out.get("state") != "ALLOWED":
            raise RuntimeError(
                f"素材 {asset_id} {version} 审核未通过：state={out.get('state')} "
                f"reason={out.get('reason')}"
            )
        report.assets_locked += 1
        locked_assets[asset_id] = {
            "asset_id": asset_id, "version": version,
            "type": cand_type, "name": cand_name,
        }

    _step(report, "assets", created=report.assets_created,
          reused=report.assets_reused, locked=report.assets_locked,
          suspect=report.assets_suspect)
    checkpoints.save(Stage.ASSET_REVIEW,
                     {"assets_locked": report.assets_locked, "names": sorted(seen_names)})
    report.checkpoints.append(Stage.ASSET_REVIEW.value)

    # 剧本里的短 id -> asset_id + version（导演台只认 asset_id + version）
    for chapter_obj in script.get("chapters", []):
        for seg in chapter_obj.get("segments", []):
            refs = []
            for r in seg.get("asset_refs", []):
                if r in created:
                    refs.append({"asset_id": created[r],
                                 "version": locked_assets[created[r]]["version"]})
            seg["asset_refs"] = refs

    # ---- 4. 段计划 ----
    # 传 `store` 让 plan_segments 记录「素材 -> 段」反向索引
    # （图七 GF「引用次数」、图四 F2B、SG8 中程失败都依赖它）。
    segments = plan_segments(script, config, project_id=project_id,
                             locked_assets=locked_assets, store=assets)
    report.segments_planned = len(segments)

    # ⚠️ **段落盘**（任务书第 79 行：「状态不能只保存在 UI 内存中」）。
    # 此前 demo 只把段 id 写进检查点，段本身的状态（审核结论、候选版本、
    # 首尾帧、尝试记录）**全在内存**，进程一退就没了，恢复时无法判断
    # 「哪些段已通过、哪些要重做」。
    # 现在计划一出来就落盘，之后每次状态变化再更新（见下面的 `_persist`）。
    from .store.segments import SegmentStore

    seg_store = SegmentStore(paths)

    def _persist(segment: "./models.entities.Segment") -> None:
        try:
            seg_store.save(segment)
        except Exception:  # noqa: BLE001
            # 落盘失败不应让出片流程崩掉，但必须让报告里看得见（不静默成功）。
            report.degrade_flags.append(f"SEGMENT_PERSIST_FAILED:{segment.segment_id}")

    for _seg in segments:
        _persist(_seg)

    _step(report, "plan", segments=[s.segment_id for s in segments])
    checkpoints.save(Stage.DIRECTOR_PLAN, {"segments": [s.segment_id for s in segments]})
    report.checkpoints.append(Stage.DIRECTOR_PLAN.value)

    # ---- 5. 生成 + 段级审核 ----
    ids = default_ids()
    counters = default_counters(paths.project_dir)
    generator = SegmentGenerator(
        assets, counters, cfg=config, ids=ids, project_id=project_id
    )

    # R_seg 的 R1a 四维打分器。演示用确定性分数；
    # 真实链路必须接**本地**评分模型（方案要求视频/图片审核全部本地完成）。
    r1a_dims = list(config.video_review.r1a_dimensions)

    def _demo_r1a_scorer(segment, version: str) -> dict[str, float]:
        return {d: 88.0 for d in r1a_dims}
    seg_reviewer = SegmentReviewer(
        counters, cfg=config, ids=ids, project_id=project_id,
        scorer=_demo_r1a_scorer,
    )

    fps = int(config.director.fps)
    resolution = str(config.director.resolution)
    palette = ["0x1f3b5c", "0x5c1f3b", "0x1f5c3b"]
    work = Path(workdir) if workdir else (paths.project_dir / "_work")
    work.mkdir(parents=True, exist_ok=True)

    for idx, seg in enumerate(segments):
        dur = float(seg.segment_duration or 3)
        video = work / f"{seg.segment_id}_v1.mp4"
        _make_placeholder_video(video, duration_s=dur, fps=fps,
                                resolution=resolution, color=palette[idx % len(palette)])
        report.segments_generated += 1

        # 候选尝试必须先登记，且 current_version 必须先指向它 ——
        # 否则 Reviewer 会以「审核对象版本与段当前版本不一致」拒绝（这是 R3 的保护）。
        attempt = SegmentAttempt(
            attempt_id=ids.attempt_id(seg.segment_id, "v1", 1),
            segment_id=seg.segment_id, version="v1", index=1,
            video_path=str(video),
        )
        seg.attempts.append(attempt)
        seg.current_version = "v1"
        seg.candidate_version = "v1"

        # 段级审核：绑定 AUDIT_{segment_id}_{version}，只审当前段当前版本。
        # material=None 才会走注入的 scorer；若传 material 则直接读取其中维度。
        review = seg_reviewer.review(seg, "v1", material=None)
        state = str(review.get("review_state") or "")
        score = float(review.get("score", 0.0))

        # 用审核返回的 updated copy 覆盖，保持「审核结果与段状态一致」
        reviewed_seg = review.get("segment")
        if reviewed_seg is not None:
            reviewed_seg.attempts = seg.attempts
            reviewed_seg.candidate_version = "v1"
            segments[idx] = reviewed_seg
            seg = reviewed_seg

        attempt.score = score
        attempt.passed = state.endswith("ALLOWED")
        seg.review_result = {k: v for k, v in review.items() if k != "segment"}

        if state.endswith("ALLOWED"):
            seg.status = SegmentStatus.PASSED
            seg.source = SegmentSource.NORMAL
            report.segments_passed += 1
        else:
            # 未通过：按策略取兜底，并**如实标记来源**，不伪装成正常通过
            seg.status = SegmentStatus.FALLBACK_SELECTED
            seg.source = SegmentSource.REVIEW_FALLBACK
            seg.degrade_flags.append(DegradeFlag.FALLBACK_DEGRADED)
            report.segments_fallback += 1

        # 段状态变了就落盘 —— 否则「哪些段已通过」只存在内存里，
        # 恢复/重跑时无法判断，违反任务书第 79 行。
        _persist(seg)

    report.full_review_calls = seg_reviewer.full_review_calls
    _step(report, "segment_review",
          passed=report.segments_passed, fallback=report.segments_fallback,
          full_review_calls=report.full_review_calls,
          note="段级审核路径不得触发 R_full，因此 full_review_calls 必须为 0")
    if report.full_review_calls != 0:
        raise RuntimeError(
            f"段级审核路径触发了 {report.full_review_calls} 次整片审核；"
            "R_full 只允许在最终拼接后执行一次"
        )
    checkpoints.save(Stage.SEGMENT_REVIEW,
                     {"passed": report.segments_passed,
                      "segments": [s.segment_id for s in segments]})
    report.checkpoints.append(Stage.SEGMENT_REVIEW.value)

    # ---- 6. 拼接 + 封装 ----
    splices = build_splice_inputs(segments, project_dir=paths.project_dir)
    if not splices:
        raise RuntimeError("没有可拼接的段：所有段都未通过或无候选版本")
    spliced = splice(splices, workdir=work, out_path=work / "spliced.mp4")
    report.spliced = True
    _step(report, "splice", inputs=len(splices), out=str(spliced))

    # 背景音乐（图一 O7）：从素材库里挑一个已 LOCK 的 AUDIO 素材。
    # 取不到就**没有 BGM**（不报错，但报告里要能看出来 —— 见下面的 step 记录）。
    bgm_path: Path | None = None
    for _entry in assets.list_assets():
        if str(_entry.get("asset_type")) == "AUDIO":
            _lock = None
            try:
                _lock = assets.resolve_lock(
                    _entry["asset_id"], _entry.get("current_version"))
            except Exception:  # noqa: BLE001
                _lock = None
            if _lock:
                _candidate = paths.project_dir / _lock["file_path"]
                if _candidate.is_file():
                    bgm_path = _candidate
                    _step(report, "bgm", asset=_entry["asset_id"],
                          name=_entry.get("name"), out=str(_candidate))
                    break

    encoded = encode(spliced, config, out_path=work / "encoded.mp4",
                     bgm_file=bgm_path)
    report.encoded = True
    _step(report, "encode", out=str(encoded), bgm=bool(bgm_path))

    # ---- 7. 整片审核（拼接后、输出前，仅一次）----
    def _demo_full_scorer(path: Path, dims: list[str]) -> dict[str, float]:
        # 演示用确定性打分器；真实链路必须接本地评分模型（方案要求审核全部本地完成）
        return {d: 85.0 for d in dims}

    fr = full_review(encoded, config, scorer=_demo_full_scorer)
    report.full_review_calls += 1
    _step(report, "full_review", passed=fr["passed"],
          score=fr["final_score"], threshold=fr["threshold"])

    if config.video_review.full_review_policy is None:
        # 仅在**整片审核未通过**时才需要策略。通过时不需要，也不应写入一个
        # 与本次结果无关的策略值（否则元数据里会留下误导性的 DEGRADE_OUTPUT）。
        if not fr["passed"]:
            raise RuntimeError(
                "整片审核未通过，但 video_review.full_review_policy 未显式配置；"
                "必须明确选择 BLOCK_OUTPUT 或 DEGRADE_OUTPUT（不得隐式决定）"
            )
    if not fr["passed"]:
        # 显式选择策略；apply_full_policy 在未配置时会**故意抛错**（不得隐式决定）
        policy = apply_full_policy(fr, config)
    else:
        policy = apply_full_policy(fr, config)
    if policy["action"] == "block":
        raise RuntimeError("整片审核未通过且策略为 BLOCK_OUTPUT：按设计阻止输出")
    report.degrade_flags = [f.value for f in policy["degrade_flags"]]
    _step(report, "full_policy", action=policy["action"],
          configured_policy=(config.video_review.full_review_policy.value
                             if config.video_review.full_review_policy else None))

    # ---- 8. 交付 ----
    metadata = MovieMetadata(
        project_id=project_id,
        project_display_name=str(info.get("display_name", "端到端演示")),
        script_id=script.get("script_id"),
        chapters=[c.get("chapter_id", "CH001") for c in script.get("chapters", [])],
        segments=[
            SegmentManifestItem(
                segment_id=s.segment_id,
                segment_version=s.candidate_version or "v1",
                asset_refs=list(s.asset_refs),
                video_path=(s.current_attempt().video_path if s.current_attempt() else None),
                content_hash=(
                    _hash_file(Path(s.current_attempt().video_path))
                    if s.current_attempt() and s.current_attempt().video_path
                    else None
                ),
                final_score=(s.current_attempt().score if s.current_attempt() else None),
                review_source=s.source.value,
                degrade_flags=list(s.degrade_flags),
                source=s.source,
            )
            for s in segments
        ],
        r1b_scores=fr["r1b_scores"],
        r1b_threshold=fr["threshold"],
        # 审核状态用语义字符串，不用 str(bool)（"True"/"False" 无法区分
        # 「通过」与「降级/阻止」两种失败形态，会让元数据读者误判）。
        full_review_state="PASSED" if fr["passed"] else "FAILED",
        full_review_policy=config.video_review.full_review_policy,
        delivered_with_degradation=bool(policy["delivered_with_degradation"]),
        degrade_flags=list(policy["degrade_flags"]),
        # 生成来源声明必须随成片落盘：占位媒体产物在日后必须仍能与真实产物区分。
        # run_demo 只有占位媒体一条路径，因此这里是**常量**，不随参数变化 ——
        # 若将来接入真实执行器，必须在此处按实际来源填写并配可执行验证。
        generation_provenance={
            "is_mock": True,
            "executor": "placeholder-ffmpeg",
            "reason": report.real_generation_reason,
            "unverified": list(report.unverified),
        },
    )
    delivered = write_output(encoded, config, project_dir=paths.project_dir, metadata=metadata)
    report.output_path = delivered["out_path"]
    report.metadata_path = delivered["metadata_path"]
    _step(report, "deliver", out=report.output_path, checks=delivered["checks"])

    checkpoints.save(Stage.OUTPUT, {"output_path": report.output_path, "ok": True})
    report.checkpoints.append(Stage.OUTPUT.value)

    # 事件落盘证据
    if paths.events_jsonl.exists():
        report.event_lines = len(
            [ln for ln in paths.events_jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]
        )
    _step(report, "events", lines=report.event_lines)
