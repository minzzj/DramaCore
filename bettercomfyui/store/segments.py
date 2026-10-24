"""段版本持久化：把视频段及其版本记录落盘。

## 为什么需要它（一个真实缺口）

`store/paths.py` 早就声明了 `segments/<segment_id>.json` 并会创建
`segments/` 目录，但**没有任何代码往里写** —— 段只存在于内存里。

后果很具体：

* 「服务重启后从检查点恢复，**不重复创建版本**」（任务书第 9 节验收 12）
  在**段**这一层无法满足 —— 没有落盘就没有"已有版本"可查；
* 「新版本必须重新审核；旧版结论只对旧版本有效」（第 3 节 F 规则 4）
  也无法保证 —— 重启后不知道哪些版本已经审过；
* 融合层把导演台产物登记成"段版本"时无处可记。

所以这个 store 不是锦上添花，而是上面几条契约的前提。

## 硬规则

1. **只追加不覆盖**（与素材库同一条纪律）：同 `(segment_id, version)` 重复写入
   要么拒绝、要么幂等返回既有记录 —— 绝不静默改写已审核版本的内容。
2. **内容指纹变更必须升版本**：同一版本号配不同 `content_hash` 是数据损坏，
   必须报错而不是覆盖（那样会让旧审核结论"看起来"仍然有效）。
3. 所有写入经过 `atomic_write_json`（原子替换 + fsync）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..models.entities import Segment
from .atomic import atomic_write_json, read_json
from .errors import StoreError
from .paths import ProjectPaths


class SegmentStoreError(StoreError):
    """段存储层的可诊断错误。"""


class SegmentStore:
    """按项目隔离的段版本存储。

    布局：`<project>/segments/<segment_id>.json`，文件中保存整个
    :class:`~bettercomfyui.models.entities.Segment`（含 `attempts` = 版本历史）。
    """

    def __init__(self, project: ProjectPaths | Path | str) -> None:
        self.paths = project if isinstance(project, ProjectPaths) else ProjectPaths(
            root=Path(project).parent, project_id=Path(project).name
        )

    # -- 路径 -------------------------------------------------------------
    def path_for(self, segment_id: str) -> Path:
        return self.paths.segment_json(segment_id)

    # -- 读 ---------------------------------------------------------------
    def load(self, segment_id: str) -> Segment | None:
        """读一个段；不存在返回 None，损坏则抛错（不静默返回空）。"""
        p = self.path_for(segment_id)
        if not p.is_file():
            return None
        data = read_json(p)
        if not isinstance(data, dict):
            raise SegmentStoreError(
                f"段文件不是 JSON 对象：{p}（实际 {type(data).__name__}）—— 数据损坏，拒绝猜测"
            )
        return Segment.from_dict(data)

    def load_or_raise(self, segment_id: str) -> Segment:
        seg = self.load(segment_id)
        if seg is None:
            raise SegmentStoreError(f"段不存在：{segment_id}")
        return seg

    def list_segments(self) -> list[Segment]:
        """列出本项目所有段，按 `segment_index` 排序。"""
        d = self.paths.segments_dir
        if not d.is_dir():
            return []
        out: list[Segment] = []
        for p in sorted(d.glob("*.json")):
            seg = self.load(p.stem)
            if seg is not None:
                out.append(seg)
        out.sort(key=lambda s: (s.segment_index, s.segment_id))
        return out

    # -- 写 ---------------------------------------------------------------
    def save(self, segment: Segment) -> Path:
        """保存段（**幂等**：相同内容重复保存不报错）。"""
        p = self.path_for(segment.segment_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(p, segment.to_dict())
        return p

    def add_version(
        self,
        segment_id: str,
        *,
        version: str,
        content_hash: str,
        **fields: Any,
    ) -> Segment:
        """追加一个段版本。

        规则（与素材库一致）：

        * `(segment_id, version)` 已存在且 `content_hash` **相同**
          -> 幂等返回既有段（重启重跑不会重复建版本）；
        * `(segment_id, version)` 已存在但 `content_hash` **不同**
          -> **报错**。同一版本号配不同内容属数据损坏；
            静默覆盖会让旧审核结论"看起来"仍然有效，这是最危险的一类错误。
        """
        from ..models.entities import SegmentAttempt, now_iso

        seg = self.load(segment_id)
        if seg is None:
            raise SegmentStoreError(
                f"段不存在：{segment_id}。请先用 plan 创建段计划再追加版本 —— "
                "禁止凭空创建段（版本必须归属到具体段计划）。"
            )

        existing = next((a for a in seg.attempts if a.version == version), None)
        if existing is not None:
            if existing.content_hash and content_hash and existing.content_hash != content_hash:
                raise SegmentStoreError(
                    f"段 {segment_id} 的版本 {version} 已存在但内容指纹不同：\n"
                    f"  已有 {existing.content_hash}\n"
                    f"  本次 {content_hash}\n"
                    "同一版本号不得对应两份不同内容（会让旧审核结论看起来仍有效）。"
                    "请改用新版本号。"
                )
            return seg  # 幂等

        attempt = SegmentAttempt(
            attempt_id=f"{segment_id}_{version}",
            segment_id=segment_id,
            version=version,
            index=len(seg.attempts) + 1,
            content_hash=content_hash,
            **fields,
        )
        seg.attempts.append(attempt)
        seg.candidate_version = version
        # ⚠️ **必须同时推进 `current_version`**：审核器要求
        # `version == segment.current_version`，否则抛 `StaleSegmentVersion`
        # （见 `director/review.py` 的 docstring：「先登记候选尝试并把
        # `segment.current_version` 设成待审版本，再调用本方法」）。
        #
        # 只设 `candidate_version` 会让**每一个新版本都无法被审核**
        # —— 实测踩到：回灌 v3 后审核报 "审核对象版本 v3 与段当前版本 v1 不一致"。
        # 而"审核当前版本"正是迟到结果隔离（验收 9）赖以生效的前提：
        # 新版一落地，旧版的审核结论立刻变成 stale 并被拒绝。
        seg.current_version = version
        seg.updated_at = now_iso()
        self.save(seg)
        return seg

    def update(self, segment: Segment) -> Path:
        """整体更新已有段（**必须已存在**，不允许借 update 新建）。"""
        if not self.path_for(segment.segment_id).is_file():
            raise SegmentStoreError(
                f"段不存在，拒绝 update：{segment.segment_id}"
            )
        return self.save(segment)

    # -- 汇总 -------------------------------------------------------------
    def summary(self) -> dict:
        """给报告用的小结：段数、已落盘版本数、可复现信息完整度。"""
        segs = self.list_segments()
        versions = sum(len(s.attempts) for s in segs)
        # 「可复现」不看段级 seed 有没有填 —— 回灌路径把种子记在**候选**上
        # （`SegmentAttempt.used_seed`），那同样是完整信息。
        # 只数 `Segment.seed` 会得出"没有一个段可复现"的错误结论（实测踩到）。
        with_seed = sum(
            1 for s in segs
            if s.resolved_seed is not None
            or s.seed is not None
            or any(a.used_seed is not None for a in s.attempts)
        )
        return {
            "segment_count": len(segs),
            "version_count": versions,
            "segments_with_seed": with_seed,
            "segment_ids": [s.segment_id for s in segs],
        }
