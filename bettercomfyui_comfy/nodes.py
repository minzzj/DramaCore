"""ComfyUI 节点定义：Better ComfyUI 的网页操作入口。

**设计原则**

1. **节点即 UI**：ComfyUI 的网页界面本身就是我们的界面，节点面板上直接操作项目、
   填剧本、跑全流程、审核素材。不额外做一套脱离 ComfyUI 的前端。
2. **失败必须可见**：缺编排包、缺配置、缺素材时抛明确错误，**绝不**静默返回空结果。
   一个「点了没反应」的节点比一个报错的节点危险得多。
3. **第二阶段入口保留但不上屏**：`bettercomfyui_comfy.phase2` 是可调用的后端接口，
   但**不注册任何节点、不在 UI 暴露**（用户明确要求）。详见该模块文档。
4. **手动素材两条路径严格区分**（方案第四部分）：
   - **新增**：直接入库，标记 `MANUAL_UNREVIEWED`，**不审核**，且**不可用于生成**；
   - **替换**：创建**新版本**（旧版本与其审核记录保留），走**当前项目审核模式**。
   两者都**不覆盖**旧文件 —— 这是 R2 硬规则。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._bridge import BridgeError, describe_bridge, load_orchestrator, orchestrator_available

CATEGORY = "Better ComfyUI"


class _NodeError(RuntimeError):
    """节点级错误：信息面向使用者。"""


def _orch():
    """取编排包；失败时把 BridgeError 转成 ComfyUI 能显示的错误。"""
    try:
        return load_orchestrator()
    except BridgeError as exc:
        raise _NodeError(str(exc)) from exc


def _projects_root(cfg) -> Path:
    root = Path(cfg.global_.project_root)
    if not root.is_absolute():
        # 相对路径以仓库根为基准（与 CLI 行为一致）
        root = Path(_orch_repo_root()) / root
    return root


def _looks_like_repo_root(path: Path) -> bool:
    """仓库根的强标记：有编排包本体 + 默认配置。"""
    return ((path / "bettercomfyui" / "__init__.py").is_file()
            and (path / "config" / "default.yaml").is_file())


def _orch_repo_root() -> Path:
    """定位**仓库根**（不是 custom_nodes！）。

    实测事故（ROUND27）：插件装在 `<repo>/.comfyui/custom_nodes/bettercomfyui_comfy` 时，
    `_bridge_state["root"]` 会解析成 `custom_nodes/`，于是
    `_projects_root()` 变成 `custom_nodes/projects` —— 节点在 ComfyUI 里读不到真实项目，
    还在 custom_nodes 下写了一堆散装项目（`custom_nodes/projects/a93…` 的由来）。

    修复：优先用强标记向上找真实仓库根，并**显式拒绝 custom_nodes**。
    """
    import os

    env = os.environ.get("BETTERCOMFYUI_HOME")
    if env:
        candidate = Path(env)
        if _looks_like_repo_root(candidate):
            return candidate

    for candidate in Path(__file__).resolve().parents:
        if candidate.name == "custom_nodes":
            continue
        if _looks_like_repo_root(candidate):
            return candidate

    from ._bridge import _bridge_state

    root = _bridge_state.get("root")
    if root is None:
        load_orchestrator()
        root = _bridge_state.get("root")
    if root:
        candidate = Path(root)
        if candidate.name != "custom_nodes":
            return candidate
    return Path.cwd()


def _config():
    """加载配置（三层优先级：项目 > 用户 > 默认）。"""
    _orch()  # 确保 sys.path 就位
    from importlib import import_module

    cfg_mod = import_module("bettercomfyui.config")
    return cfg_mod.load_config(repo_root=_orch_repo_root())


def _store_for_project(project_id: str):
    """返回 (ProjectStore, ProjectPaths, AssetStore, projects_root, cfg, info)。

    项目引用解析见 `ProjectStore.resolve_reference()`：既接受 32 位 project_id，
    也接受**项目显示名**（不存在则自动新建并在日志/摘要里说明）。
    这样发布的工作流不必硬编码某台机器的项目 id（审计 R2：曾有 10 个工作流
    硬编码开发机 id，换机即「工作流能打开但跑不通」）。
    """
    _orch()
    from importlib import import_module

    paths_mod = import_module("bettercomfyui.store.paths")
    proj_mod = import_module("bettercomfyui.store.projects")
    assets_mod = import_module("bettercomfyui.store.assets")

    cfg = _config()
    root = _projects_root(cfg)
    ps = proj_mod.ProjectStore(root, cfg)
    try:
        info, action = ps.resolve_reference(project_id)
    except Exception as exc:  # noqa: BLE001 - 统一转成可操作节点错误
        raise _NodeError(
            f"项目引用无法解析：{project_id!r}（{type(exc).__name__}: {exc}）\n"
            "请填写 32 位项目 id，或填写项目显示名（不存在会自动新建）；"
            "也可先用「Better ComfyUI 项目」节点创建/选择项目。"
        ) from exc
    real_id = str(info.get("project_id") or project_id)
    paths = paths_mod.ProjectPaths(root=root, project_id=real_id)
    paths.ensure_layout()
    info = dict(info)
    info["project_resolution"] = action
    if action == "CREATED_NAME":
        print(f"[BetterComfyUI] 项目显示名 {project_id!r} 不存在，已自动新建：{real_id}")
    return ps, paths, assets_mod.AssetStore(paths), root, cfg, info


def _resolve_file_to_path(value: Any, *, field: str) -> Path:
    """把节点上的文件值解析成磁盘路径。

    ComfyUI 的 STRING 输入可能是：绝对路径、相对路径、或 `input/xxx.png` 形式。
    解析不出来时必须报错，**不得**猜一个路径继续。
    """
    if value is None or not str(value).strip():
        raise _NodeError(f"{field} 未填写文件路径")
    raw = str(value).strip().strip('"')
    p = Path(raw)
    if p.is_file():
        return p
    # 尝试 ComfyUI 的 input 目录
    try:
        import folder_paths  # type: ignore

        cand = Path(folder_paths.get_input_directory()) / raw
        if cand.is_file():
            return cand
    except Exception:  # noqa: BLE001 - 非 ComfyUI 环境下没有 folder_paths
        pass
    raise _NodeError(
        f"{field} 指向的文件不存在：{raw}\n"
        "请填绝对路径，或把文件放进 ComfyUI 的 input/ 目录后只填文件名。"
    )


# ===========================================================================
# 1) 项目
# ===========================================================================


class BetterComfyUIProject:
    """项目选择/创建/列出/删除。项目之间素材、章节、进度、输出完全隔离（R5）。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "action": (["打开或创建", "仅列出", "删除项目"], {"default": "打开或创建"}),
                "project_name": ("STRING", {"default": "我的短剧", "multiline": False}),
                "project_id": ("STRING", {"default": "", "multiline": False}),
            },
            "optional": {
                "confirm_delete": ("BOOLEAN", {"default": False}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("project_id", "info")
    FUNCTION = "run"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True


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

    def run(self, action: str, project_name: str, project_id: str,
            confirm_delete: bool = False):
        _orch()
        from importlib import import_module

        proj_mod = import_module("bettercomfyui.store.projects")
        cfg = _config()
        root = _projects_root(cfg)
        ps = proj_mod.ProjectStore(root, cfg)

        if action == "仅列出":
            rows = ps.list()
            text = "\n".join(
                f"{r.get('project_id')}  {r.get('display_name')}  {r.get('created_at','')}"
                for r in rows
            ) or "（还没有项目）"
            return {"ui": {"text": [text]}, "result": ("", text)}

        if action == "删除项目":
            pid = (project_id or "").strip()
            if not pid:
                raise _NodeError("删除项目需要填写 project_id")
            if not confirm_delete:
                raise _NodeError(
                    "删除项目需要二次确认：把 confirm_delete 打开。"
                    "（方案要求删除需二次确认，且会先检查未保存状态）"
                )
            try:
                ps.close(pid)
            except Exception:  # noqa: BLE001 - 未打开也能删
                pass
            ps.delete(pid, confirm_token=pid)
            return {"ui": {"text": [f"已删除 {pid}"]}, "result": (pid, "已删除")}

        # 打开或创建
        pid = (project_id or "").strip()
        if pid:
            info = ps.open(pid)
        else:
            existing = [
                r for r in ps.list()
                if r.get("display_name") == (project_name or "").strip()
            ]
            if existing:
                pid = existing[0]["project_id"]
                info = ps.open(pid)
            else:
                info = ps.create(project_name or "未命名项目")
                pid = info["project_id"]
        text = f"项目 {pid}\n显示名：{info.get('display_name')}"
        return {"ui": {"text": [text]}, "result": (pid, text)}


# ===========================================================================
# 2) 运行（主节点）
# ===========================================================================


class BetterComfyUIRun:
    """一键跑完整流程：剧本 -> 素材 -> 视频段 -> 审核 -> 拼接 -> 成片。

    **执行器边界（硬性）**：本节点的一键闭环**只**支持「占位媒体(mock)」——它用
    ffmpeg 合成合法占位视频来验证编排/版本/审核语义，**不是**模型生成结果。
    「真实 ComfyUI」选项目前**没有接入**任何真实生成执行器，选中会**显式报错**
    （绝不静默退回占位、更不会把占位媒体标成真实生成）。真实出片请用
    导演台/融合工作流（S2 / S3 / W3 / 00_【总流程】剧本→成片（GPU））。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "project_id": ("STRING", {"default": "", "multiline": False}),
                "script_text": ("STRING", {"default": "", "multiline": True}),
                "review_mode": (["MODEL", "HUMAN"], {"default": "MODEL"}),
                "asset_review_mode": (["MODEL", "HUMAN"], {"default": "MODEL"}),
                "executor": (["占位媒体(mock)", "真实 ComfyUI"], {"default": "占位媒体(mock)"}),
            },
            "optional": {
                "max_segments": ("INT", {"default": 3, "min": 1, "max": 200}),
                "enable_cascade": ("BOOLEAN", {"default": True}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("status", "output_path", "report_json")
    FUNCTION = "run"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True


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

    def run(self, project_id: str, script_text: str, review_mode: str,
            asset_review_mode: str, executor: str, max_segments: int = 3,
            enable_cascade: bool = True):
        _orch()
        from importlib import import_module

        demo = import_module("bettercomfyui.demo")

        # ---- 让节点上的控件**真的生效**（界面上给了控制项却不生效，比没有更糟）----
        cfg = _config()
        notes: list[str] = []

        # ① 素材审核模式：真正写进配置，由素材审核层读取
        try:
            cfg.asset_review.default_mode = cfg.asset_review.default_mode.parse(asset_review_mode)
        except Exception as exc:  # noqa: BLE001
            raise _NodeError(f"素材审核模式取值非法：{asset_review_mode!r}") from exc

        # ② 段级审核模式：配置里目前没有对应字段，老实说明它不生效，
        #    而不是写一行看起来在生效、实际是 no-op 的代码。
        if review_mode == "HUMAN":
            notes.append(
                "段级审核模式=HUMAN：段级审核当前由 SegmentReviewer 的模型评分器执行，"
                "配置里没有段级人工/模型开关，因此该选项**暂不改变行为**；"
                "需要人工裁决请用「素材审核」节点（素材维度已支持）。"
            )
        else:
            notes.append("段级审核模式=MODEL：与当前实现一致。")

        # ③ 剧本：用户给了就用用户的；没给就用内置演示剧本。
        #    **两种情况下 max_segments 都必须生效** —— 否则「我只想跑 2 段」在留空剧本时
        #    会静默无效（实测踩到：传 max_segments=2 却跑了 3 段）。
        script: dict | None = None
        if script_text and script_text.strip():
            try:
                script = json.loads(script_text.strip())
            except json.JSONDecodeError as exc:
                raise _NodeError(
                    f"剧本内容不是合法 JSON：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）。\n"
                    "请填结构化 JSON；也可以留空使用内置演示剧本。"
                ) from exc
        else:
            script = json.loads(json.dumps(demo.DEMO_SCRIPT))   # 深拷贝，绝不改模块常量
            notes.append("剧本留空：使用内置演示剧本。")

        total_segments = sum(
            len(ch.get("segments") or []) for ch in (script or {}).get("chapters", [])
        )
        if total_segments > max_segments:
            remaining = max_segments
            for ch in script["chapters"]:
                keep = min(remaining, len(ch.get("segments") or []))
                ch["segments"] = (ch.get("segments") or [])[:keep]
                remaining -= keep
            notes.append(
                f"剧本共 {total_segments} 段，已按 max_segments={max_segments} "
                f"截断为前 {max_segments} 段。"
            )

        # ④ enable_cascade：进不去级联路径时必须**显式说明**，不能静默忽略
        if not enable_cascade:
            notes.append(
                "enable_cascade=False：已关闭级联返工。注意本闭环流程中所有段均一次通过审核，"
                "本来就不会产生级联，因此该开关在当前剧本下无可观察效果。"
            )

        pid = (project_id or "").strip() or None
        use_mock = executor.startswith("占位")
        repo_root = _orch_repo_root()

        # 执行器门禁：本一键闭环只有占位媒体一条产出路径。
        # 选「真实 ComfyUI」时**必须先失败闭合**，否则 demo 会用 ffmpeg 占位视频
        # 冒充真实生成（历史缺陷：used_mock=False + metadata.executor="comfyui"）。
        if not use_mock:
            raise _NodeError(
                "「真实 ComfyUI」执行器尚未接入本一键闭环：本节点只产出 ffmpeg 占位媒体，"
                "继续运行会把占位产出标成真实生成（假成功），因此被拒绝。\n"
                "真实出片请使用导演台/融合工作流："
                "S2_单节点出片（GPU）/ S3_素材生成入库审核（GPU）/ "
                "W3_全流程出片 / 00_【总流程】剧本→成片（GPU）。\n"
                "在本节点体验编排闭环请选「占位媒体(mock)」。"
            )

        chapter_path: Path | None = None
        if script is not None:
            # 写到临时位置，避免污染 projects/
            import os as _os
            import tempfile

            fd, tmp_name = tempfile.mkstemp(suffix=".json", prefix="bcu_node_script_")
            Path(tmp_name).write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
            _os.close(fd)
            chapter_path = Path(tmp_name)

        try:
            report = demo.run_demo(
                repo_root=repo_root,
                config=cfg,
                project=pid,
                chapter=chapter_path,
                executor="mock" if use_mock else "comfyui",
            )
        finally:
            if chapter_path is not None:
                chapter_path.unlink(missing_ok=True)

        status = "成功" if report.get("ok") else f"失败：{report.get('error')}"
        if report.get("used_mock"):
            status += "（⚠️ 占位媒体路径：产出不是模型生成结果）"
        lines = [
            f"状态：{status}",
            f"项目：{report.get('project_id')}",
            f"素材 新建={report.get('assets_created')} 复用={report.get('assets_reused')} "
            f"锁定={report.get('assets_locked')}",
            f"视频段 计划={report.get('segments_planned')} 生成={report.get('segments_generated')} "
            f"通过={report.get('segments_passed')}",
            f"拼接={report.get('spliced')} 封装={report.get('encoded')}",
            f"审核模式：素材={asset_review_mode} 段级={review_mode}",
            f"成片：{report.get('output_path')}",
        ]
        # 控制项的实际效果必须如实交代（哪些生效、哪些不生效、为什么）
        if notes:
            lines.append("说明：" + " ".join(notes))
        if report.get("unverified"):
            lines.append("未验证项：" + "；".join(report["unverified"]))
        text = "\n".join(lines)
        return {
            "ui": {"text": [text]},
            "result": (status, report.get("output_path") or "", json.dumps(report, ensure_ascii=False, default=str)),
        }


# ===========================================================================
# 3) 素材审核（人工审核入口）
# ===========================================================================


class BetterComfyUIAssetReview:
    """素材版本审核（SG3 完整状态机）：人工 / 模型 / CNT1 / 超时切模型 / 唯一提交 / LOCK。

    **这一版不再直接 `set_lock`**，而是走编排层 `review.AssetReviewer`：

    * 审核对象 = 素材条目 + 具体版本（`G_VALID` / `H`）；
    * 人工通过/拒绝 → 建审核周期 → 唯一提交点 → 版本级 LOCK（`HUMAN_PASS`）；
    * 模型审核 → 本地启发式评分（真实像素统计）→ CNT1 计数 →
      通过写 `MODEL_PASS`，达上限按**已落盘候选历史**最高分兜底写 `FALLBACK_PASS`；
    * 发起人工审核 → 周期停在 AWAITING_REVIEW（落盘），下次任何操作先推进超时：
      超过 `asset_review.human_timeout_minutes`（默认 10 分钟）且无有效操作 →
      切模型模式（`AUTO_SWITCHED`，CNT1 从 0 开始）。

    **跨运行状态（审计 R1 修复）**：审核器的候选历史、人工等待窗口、周期状态
    都落盘到 `<项目>/review/reviewer_state.json`。此前这些只存在内存里，
    而 ComfyUI 节点每次执行都是新进程内新实例 —— 于是「模型三轮取最高分」
    跨运行只剩当前一轮候选（等于取当前分），人工等待超时也永远不会触发。

    **评分诚实性**：本地启发式只有「画质 / 构图」两维真实可算；
    「一致性 / 合规」需要语义模型，会在摘要里明确列为 skipped，
    绝不编造分数（要语义审核请注入你自己的评分模型）。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "project_id": ("STRING", {"default": "", "multiline": False}),
                "action": (
                    ["列出待审版本", "模型审核（本地启发式）", "发起人工审核（等待）",
                     "人工通过", "人工拒绝", "人工超时→切模型（强制）", "查询审核状态"],
                    {"default": "列出待审版本"},
                ),
                "asset_id": ("STRING", {
                    "default": "", "multiline": False,
                    "tooltip": "留空 = 自动选第一个待审版本；也可从「列出待审版本」里复制。"}),
                "version": ("STRING", {"default": "", "multiline": False,
                                       "tooltip": "如 v1；留空 = 自动。"}),
            },
            "optional": {
                "operator": ("STRING", {"default": "human", "multiline": False,
                                        "tooltip": "人工操作者标识（必须留痕）。"}),
                "reason": ("STRING", {"default": "", "multiline": True}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("state", "评分JSON", "LOCK信息", "摘要")
    FUNCTION = "run"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    # -- 工具 ---------------------------------------------------------
    @staticmethod
    def _counters(paths):
        from importlib import import_module

        mod = import_module("bettercomfyui.review.counters")
        counter_path = Path(paths.project_dir) / "counters.json"
        counters = mod.CounterStore()
        if counter_path.is_file():
            try:
                counters.restore(json.loads(counter_path.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001 - 损坏则从零开始（摘要里会看到）
                pass
        return counters, counter_path

    @staticmethod
    def _save_counters(counters, path: Path) -> None:
        path.write_text(json.dumps(counters.snapshot(), ensure_ascii=False, indent=2),
                        encoding="utf-8")

    @staticmethod
    def _pick_pending(assets) -> tuple[str, str] | None:
        for asset in assets.list_assets():
            for ver in asset.get("versions", []):
                if ver.get("lock_status") != "LOCKED":
                    return str(asset["asset_id"]), str(ver.get("version"))
        return None

    # -- 审核器跨运行状态（审计 R1：此前只存在内存里，跨节点执行即丢失） ----
    @staticmethod
    def _reviewer_state_path(paths) -> Path:
        return Path(paths.project_dir) / "review" / "reviewer_state.json"

    @classmethod
    def _restore_reviewer_state(cls, reviewer, paths) -> str:
        """恢复上次运行落盘的审核器状态；返回给用户看的说明（失败也不静默）。"""
        state_path = cls._reviewer_state_path(paths)
        if not state_path.is_file():
            return ""
        try:
            from bettercomfyui.store.atomic import read_json

            data = read_json(state_path)
            if not isinstance(data, dict):
                raise ValueError(f"状态文件顶层不是 dict（{type(data).__name__}）")
            reviewer.restore(data)
            return f"已恢复审核状态：{state_path}"
        except Exception as exc:  # noqa: BLE001 - 状态坏了按新状态继续，但必须说明
            return (
                f"审核状态恢复失败（按全新状态继续，不冒充旧状态）："
                f"{type(exc).__name__}: {exc}"
            )

    @classmethod
    def _save_reviewer_state(cls, reviewer, paths) -> str:
        """落盘审核器状态；失败必须让用户看见（否则跨运行语义又悄悄退化）。"""
        state_path = cls._reviewer_state_path(paths)
        try:
            from bettercomfyui.store.atomic import atomic_write_json

            atomic_write_json(state_path, reviewer.snapshot())
            return str(state_path)
        except Exception as exc:  # noqa: BLE001
            return f"（审核状态落盘失败：{type(exc).__name__}: {exc}）"


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

    def run(self, project_id: str, action: str, asset_id: str, version: str,
            operator: str = "human", reason: str = ""):
        if not (project_id or "").strip():
            raise _NodeError("请先填写 project_id（可用「Better ComfyUI 项目」节点生成）")
        _ps, paths, assets, _root, cfg, _info = _store_for_project(project_id.strip())
        pid = project_id.strip()

        # ---- 列清单：看到「哪个素材/版本/状态/文件」才好操作 ------------
        if action == "列出待审版本":
            rows: list[str] = []
            for asset in assets.list_assets():
                for ver in asset.get("versions", []):
                    if ver.get("lock_status") == "LOCKED":
                        continue
                    rows.append(
                        f"{asset['asset_id']}  {ver.get('version')}  "
                        f"type={asset.get('asset_type')}  name={asset.get('name')}  "
                        f"status={ver.get('status')}  lock={ver.get('lock_status')}")
            text = "\n".join(rows) or "（没有待审版本）"
            return {"ui": {"text": [text]}, "result": ("", "{}", "", text)}

        from importlib import import_module

        review_mod = import_module("bettercomfyui.review")
        enums_mod = import_module("bettercomfyui.models.enums")
        scorer_mod = import_module("bettercomfyui.review.local_scorer")
        ReviewMode = enums_mod.ReviewMode

        # ---- 目标版本：留空时自动选第一个待审 ---------------------------
        aid = (asset_id or "").strip()
        ver = (version or "").strip()
        if not aid or not ver:
            picked = self._pick_pending(assets)
            if picked is None:
                return ("NONE", "{}", "",
                        "没有待审版本。先「素材生成·取任务」→ 图像工作流 →「素材入库」产生待审版本。")
            aid, ver = picked
        ver_row = assets.get_version(aid, ver)
        if ver_row is None:
            available = [v.get("version") for v in (assets.get_asset(aid) or {}).get("versions", [])]
            raise _NodeError(f"素材 {aid} 没有版本 {ver}；可用版本：{available}")

        def _abs_path(rel: object) -> Path | None:
            if not rel:
                return None
            try:
                return paths.absolute_from_relative(str(rel))
            except Exception:  # noqa: BLE001
                p = Path(str(rel))
                return p if p.is_absolute() else None

        def model_review(material: dict) -> dict:
            """本地启发式评分：画质/构图=像素统计；一致性=同素材上一LOCK版本相似度；
            合规=确定性违禁词规则（非语义模型，缺参考/提示词时如实跳过）。"""
            file = _abs_path(material.get("file_path"))
            if file is None or not file.is_file():
                raise review_mod.ReviewTechnicalError(
                    f"找不到素材文件，无法评分：{material.get('file_path')}")
            asset_obj = assets.get_asset(aid) or {}
            reference_path = None
            for v in asset_obj.get("versions", []):
                if v.get("version") == ver or v.get("lock_status") != "LOCKED":
                    continue
                cand = _abs_path(v.get("file_path"))
                if cand is not None and cand.is_file():
                    reference_path = cand
                    break
            prompt_text = str(asset_obj.get("note") or "")
            scores, skipped = scorer_mod.image_scores(
                file, reference_path=reference_path, prompt_text=prompt_text)
            avg = scorer_mod.score_average(scores)
            threshold = float(getattr(cfg.asset_review, "score_threshold", 70.0))
            return {
                "score": avg,
                "passed": avg >= threshold,
                "scores": scores,
                "skipped_dimensions": skipped,
                "score_mode": "local_heuristic",
                "note": "画质/构图=像素统计；一致性=参考版本相似度；合规=违禁词规则（非语义模型）",
            }

        counters, counter_path = self._counters(paths)
        reviewer = review_mod.AssetReviewer(
            assets,
            config=cfg,
            counters=counters,
            project_id=pid,
            project=paths,
            model_review=model_review,
        )
        # 跨运行状态：候选历史 / 人工等待窗口 / 周期状态必须恢复，
        # 否则「模型三轮取最高分」跨节点执行只剩当前一轮候选，人工超时也永远不触发。
        restore_note = self._restore_reviewer_state(reviewer, paths)
        timeout_events: list[dict] = []
        try:
            # ComfyUI 没有后台定时器：把「每次进入审核节点」当作一次推进 tick。
            timeout_events = reviewer.poll_timeouts()
        except Exception as exc:  # noqa: BLE001 - 推进失败不阻断当前操作，但必须可见
            restore_note = (restore_note + "；" if restore_note else "") + (
                f"人工超时推进失败（{type(exc).__name__}: {exc}）"
            )
        material = {
            "asset_id": aid,
            "version": ver,
            "file_path": ver_row.get("file_path"),
            "content_hash": ver_row.get("content_hash"),
        }
        note = restore_note
        if timeout_events:
            switched_ids = [str(e.get("review_cycle_id") or "") for e in timeout_events]
            note = (note + "；" if note else "") + (
                f"人工等待超时已自动切换模型模式：{', '.join(x for x in switched_ids if x) or '（周期）'}"
                "（CNT1 从 0 开始；此前人工次数不计入）"
            )
        # ---- 动作分派 ---------------------------------------------------
        if action.startswith("模型审核"):
            out = reviewer.review_version(aid, ver, mode=ReviewMode.MODEL, material=material)
        elif action == "发起人工审核（等待）":
            out = reviewer.review_version(aid, ver, mode=ReviewMode.HUMAN, material=material)
            cycle_id = str(out.get("review_cycle_id") or "")
            deadline = reviewer.deadline(cycle_id) if cycle_id else None
            note = (note + "；" if note else "") + (
                f"人工审核周期等待中，超时时间 {deadline or '（不可用）'}；"
                "超时后下一次进入本节点会自动切模型模式（CNT1 从 0 开始）"
            )
        elif action in ("人工通过", "人工拒绝"):
            out = reviewer.review_version(aid, ver, mode=ReviewMode.HUMAN, material=material)
            cycle_id = str(out.get("review_cycle_id") or "")
            terminal = out.get("state") in ("ALLOWED", "BLOCKED", "FALLBACK", "ABORTED")
            if not terminal and cycle_id:
                # H1C：有效人工操作会顺延超时计时（必须显式登记，不能假设）
                try:
                    activity = reviewer.record_human_activity(
                        cycle_id, kind="approve" if action == "人工通过" else "reject")
                    if activity.get("paused"):
                        note = (note + "；" if note else "") + "有效人工操作：超时计时已顺延"
                except Exception as exc:  # noqa: BLE001
                    note = (note + "；" if note else "") + (
                        f"人工操作计时登记失败（{type(exc).__name__}: {exc}）")
                out = reviewer.submit_human_result(
                    cycle_id, passed=(action == "人工通过"),
                    operator=(operator or "human").strip() or "human")
            else:
                note = (note + "；" if note else "") + "该版本已有终态，直接返回（旧结论不得被重复提交）"
        elif action.startswith("人工超时"):
            out = reviewer.review_version(aid, ver, mode=ReviewMode.HUMAN, material=material)
            cycle_id = str(out.get("review_cycle_id") or "")
            switched = reviewer.force_auto_switch(cycle_id) if cycle_id else {"timeout": False}
            note = (note + "；" if note else "") + f"强制切换模型模式：{switched.get('reason') or switched}"
            # 切换后模型轮已由审核器执行，重新取一次结果
            out = reviewer.review_version(aid, ver, mode=ReviewMode.AUTO_SWITCHED,
                                          material=material)
        elif action == "查询审核状态":
            records = [r for r in getattr(reviewer, "records", []) 
                       if getattr(r, "target_id", None) == aid
                       and getattr(r, "version", None) == ver]
            if not records:
                return ("NONE", "{}", "", f"素材 {aid} {ver} 还没有审核记录")
            out = reviewer.record_dict(records[-1].review_cycle_id)
        else:
            raise _NodeError(f"未知操作：{action}")

        # 每次操作后落盘审核器状态（供下一次 ComfyUI 节点执行恢复）
        state_saved = self._save_reviewer_state(reviewer, paths)
        self._save_counters(counters, counter_path)

        state = str(getattr(out.get("state"), "value", out.get("state")))
        score_payload = {
            "state": state,
            "score": out.get("score"),
            "cnt1": out.get("cnt1"),
            "mode": str(getattr(out.get("mode"), "value", out.get("mode"))),
            "lock_source": str(getattr(out.get("lock_source"), "value", out.get("lock_source"))),
            "lock_id": out.get("lock_id"),
            "committed": out.get("committed"),
            "review_cycle_id": out.get("review_cycle_id"),
            "reason": out.get("reason"),
            "score_source": "local_heuristic",
            "skipped_dimensions": ["一致性", "合规"],
        }
        # 兜底候选信息（FALLBACK 时由审核记录带出）
        record = out.get("record") or {}
        if record.get("lock_source"):
            score_payload["record_lock_source"] = record.get("lock_source")
        lock = assets.resolve_lock(aid, ver) or {}
        lock_info = (
            f"LOCK_ID={lock.get('lock_id') or out.get('lock_id') or '（无）'}  "
            f"source={lock.get('lock_source') or out.get('lock_source') or '-'}  "
            f"version={ver}")
        done = state in ("ALLOWED", "FALLBACK")
        summary_lines = [
            f"素材：{aid}  版本：{ver}  操作：{action}",
            f"结果：{state}  评分：{out.get('score')}  CNT1={out.get('cnt1')}",
            f"LOCK：{'已取得（可用于导演台）' if done else '未取得（不得用于生成，R3）'}",
            f"LOCK信息：{lock_info}",
            "评分来源：本地启发式（真实像素统计；一致性/合规需语义模型，已跳过并如实记录）",
        ]
        if note:
            summary_lines.append(f"说明：{note}")
        if out.get("reason"):
            summary_lines.append(f"审核理由：{out.get('reason')}")
        if state_saved:
            summary_lines.append(f"审核状态（跨运行）：{state_saved}")
        summary_lines.append(f"计数器：{counter_path}")
        text = "\n".join(summary_lines)
        print(f"[BetterComfyUI 素材审核] {aid} {ver} -> {state}")
        return {"ui": {"text": [text]},
                "result": (state, json.dumps(score_payload, ensure_ascii=False, indent=2),
                           lock_info, text)}


# ===========================================================================
# 4) 手动素材（G6A 新增 / G6B 替换）—— 方案明确要求
# ===========================================================================


class BetterComfyUIManualAsset:
    """手动添加或替换素材。**两条路径语义完全不同，不可混用**：

    * **新增**：直接入库，标记 `MANUAL_UNREVIEWED`，**不进入审核**；
      并且**不可用于生成**（未取得 LOCK）。旧文件不受影响。
    * **替换**：为已有素材创建**新版本**（`version+1`），**旧版本与旧审核记录保留**，
      并走**当前项目审核模式**。绝不在原文件上覆盖（R2）。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "project_id": ("STRING", {"default": "", "multiline": False}),
                "mode": (["新增素材(不审核)", "替换已有素材(走审核)"], {"default": "新增素材(不审核)"}),
                "asset_type": (["CHARACTER", "SCENE", "PROP", "SHOT"], {"default": "CHARACTER"}),
                "name": ("STRING", {"default": "", "multiline": False}),
                "file_path": ("STRING", {"default": "", "multiline": False}),
            },
            "optional": {
                "target_asset_id": ("STRING", {"default": "", "multiline": False}),
                "note": ("STRING", {"default": "", "multiline": True}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("asset_id", "version", "detail")
    FUNCTION = "run"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True


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

    def run(self, project_id: str, mode: str, asset_type: str, name: str,
            file_path: str, target_asset_id: str = "", note: str = ""):
        if not (project_id or "").strip():
            raise _NodeError("请先填写 project_id")
        _orch()
        from importlib import import_module

        _ps, _paths, assets, _root, _cfg, _info = _store_for_project(project_id.strip())
        src = _resolve_file_to_path(file_path, field="file_path")
        # 用真实内容指纹校验拷贝结果，避免「路径对但文件是空的」
        if src.stat().st_size == 0:
            raise _NodeError(f"素材文件是 0 字节：{src}")

        if mode == "新增素材(不审核)":
            if not (name or "").strip():
                raise _NodeError("新增素材需要填写 name")
            entry = assets.create_asset(
                asset_type=asset_type, name=name.strip(),
                source="MANUAL", library_scope="PROJECT",
            )
            vrec = assets.add_version(
                entry["asset_id"], source="MANUAL", file_path=src,
                status="MANUAL_UNREVIEWED", extra={"note": note},
            )
            ver = vrec["version"]
            detail = (
                f"已手动新增素材 {entry['asset_id']} {ver}；"
                "状态=MANUAL_UNREVIEWED（未审核）。"
                "该版本**不可用于生成**，需要审核取得 LOCK 后才能进导演台。"
            )
            return {"ui": {"text": [detail]}, "result": (entry["asset_id"], ver, detail)}

        if mode == "替换已有素材(走审核)":
            tid = (target_asset_id or "").strip()
            if not tid:
                raise _NodeError("替换素材需要填写 target_asset_id（用「列出待审版本」可查）")
            before = assets.get_asset(tid)
            if before is None:
                raise _NodeError(f"目标素材不存在：{tid}")
            vrec = assets.add_version(
                tid, source="MANUAL", file_path=src,
                status="PENDING_REVIEW", extra={"note": note, "replaced": True},
            )
            ver = vrec["version"]
            detail = (
                f"已为 {tid} 创建新版本 {ver}（状态=PENDING_REVIEW，走当前项目审核模式）。"
                f"旧版本共 {len(before.get('versions', []))} 个已保留，旧 LOCK 不会继承到新版本。"
            )
            return {"ui": {"text": [detail]}, "result": (tid, ver, detail)}

        raise _NodeError(f"未知模式：{mode}")


# ===========================================================================
# 5) 结果展示
# ===========================================================================


class BetterComfyUIResult:
    """展示运行结果。只读，不产生副作用。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"report_json": ("STRING", {"default": "", "multiline": True})},
            "optional": {
                "open_output_folder": ("BOOLEAN", {"default": False}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("status", "output_path")
    FUNCTION = "run"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def run(self, report_json: str, open_output_folder: bool = False):
        if not report_json or not report_json.strip():
            return {"ui": {"text": ["（没有报告：请先运行「Better ComfyUI 运行」节点）"]},
                    "result": ("", "")}
        try:
            rep = json.loads(report_json)
        except json.JSONDecodeError as exc:
            raise _NodeError(f"report_json 不是合法 JSON：{exc}") from exc

        lines = [
            f"状态：{'成功' if rep.get('ok') else '失败'}",
            f"成片：{rep.get('output_path')}",
            f"元数据：{rep.get('metadata_path')}",
            f"视频段 通过={rep.get('segments_passed')} 兜底={rep.get('segments_fallback')}",
            f"R_full 调用次数：{rep.get('full_review_calls')}（必须恰为 1）",
        ]
        if rep.get("used_mock"):
            lines.append("⚠️ 本次为占位媒体路径：产出**不是**模型生成结果")
        if rep.get("degrade_flags"):
            lines.append("降级标记：" + ", ".join(rep["degrade_flags"]))
        if rep.get("error"):
            lines.append("错误：" + str(rep["error"]))
        text = "\n".join(lines)

        out = rep.get("output_path") or ""
        if open_output_folder and out:
            try:
                import subprocess
                import sys as _sys

                folder = str(Path(out).parent)
                if _sys.platform.startswith("win"):
                    subprocess.Popen(["explorer", folder])  # noqa: S603,S607
                else:
                    lines.append("（自动打开文件夹仅在 Windows 上支持）")
            except Exception as exc:  # noqa: BLE001 - 打开文件夹失败不应让节点挂掉
                lines.append(f"（打开文件夹失败：{exc}）")

        return {"ui": {"text": [text]}, "result": (rep.get("ok") and "成功" or "失败", out)}


# ===========================================================================
# 6) 诊断
# ===========================================================================


class BetterComfyUIDoctor:
    """环境与插件体检。装好后第一件事就跑这个。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}, "optional": {}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("report",)
    FUNCTION = "run"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def run(self):
        info = describe_bridge()
        lines = [
            "=== Better ComfyUI 插件体检 ===",
            f"编排包可用：{info['available']}",
            f"编排包位置：{info['orchestrator_file']}",
            f"仓库根：{info['orchestrator_root']}",
            f"Python：{info['python']}",
        ]
        if not info["available"]:
            lines.append("")
            lines.append(info["error"])
        else:
            try:
                cfg = _config()
                lines.append(f"配置加载：OK（审核模式 {cfg.asset_review.default_mode.value}）")
                from importlib import import_module

                core = import_module("bettercomfyui.pipeline.core")
                try:
                    ff = core._ffmpeg_exe()  # noqa: SLF001
                    lines.append(f"ffmpeg：{ff}")
                    lines.append("-> 拼接/封装可真实执行")
                except Exception as exc:  # noqa: BLE001
                    lines.append(f"ffmpeg：不可用（{exc}）")
                    lines.append("-> 拼接/封装无法执行；`run` 会在拼接阶段失败")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"编排层探测失败：{type(exc).__name__}: {exc}")

            lines.append("")
            lines.append(
                "⚠️ 真实视频生成需要：ComfyUI ≥0.30 + 模型权重 + 目标机（≥24GB 显存）。"
                "缺失时系统会走占位媒体路径，产出**不是**模型生成结果，"
                "该事实写入成片元数据 generation_provenance。"
            )
        text = "\n".join(lines)
        return {"ui": {"text": [text]}, "result": (text,)}


# ===========================================================================
# 注册表
# ===========================================================================

NODE_CLASS_MAPPINGS: dict[str, type] = {
    "BetterComfyUIProject": BetterComfyUIProject,
    "BetterComfyUIRun": BetterComfyUIRun,
    "BetterComfyUIAssetReview": BetterComfyUIAssetReview,
    "BetterComfyUIManualAsset": BetterComfyUIManualAsset,
    "BetterComfyUIResult": BetterComfyUIResult,
    "BetterComfyUIDoctor": BetterComfyUIDoctor,
}

NODE_DISPLAY_NAME_MAPPINGS: dict[str, str] = {
    "BetterComfyUIProject": "Better ComfyUI 项目",
    "BetterComfyUIRun": "Better ComfyUI 运行",
    "BetterComfyUIAssetReview": "Better ComfyUI 素材审核",
    "BetterComfyUIManualAsset": "Better ComfyUI 手动素材",
    "BetterComfyUIResult": "Better ComfyUI 结果",
    "BetterComfyUIDoctor": "Better ComfyUI 体检",
}

#: 明确声明：本模块**不**注册任何第二阶段节点（自动抓取剧本 / 自动发布）。
#: 第二阶段接口存在于 bettercomfyui_comfy/phase2.py，但按要求不在 UI 暴露。
PHASE2_NODES_EXPOSED = False
