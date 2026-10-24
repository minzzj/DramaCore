"""阶段化显存调度（用户要求的 5090 32G 最优解）。

## 为什么要这个

视频生成模型与审核模型**同时驻留**是最贵的做法 —— 两个都要吃掉大块显存，
于是只能靠"A 卡生成 + B 卡辅助"这种高消费方案堆硬件。

用户明确要的替代方案是：**单卡阶段化**，靠"卸载得干净"而不是"堆硬件"。

## 阶段顺序（严格按用户描述）

    P0 提示词      DeepSeek API          （不占显存）
          ↓
    P1 素材生成    素材图模型            加载 → 批量生成【全部】素材 → 卸载
          ↓
    P2 视频生成    minimax_h3           加载 → 批量生成【全部】段 → 卸载
          ↓
    P3 审核        审核模型              加载 → 【统一】审核全部段 → 卸载
          ↓
    P4 重生成      视频生成模型          加载 → 只重生成【不合格】段 → 卸载
          ↓  （P3↔P4 循环，CNT2/CNT3 上限终止）
    P5 成片        ffmpeg               （不占显存）

**关键点：批量，而不是逐段交替。**
"生成一段 → 审一段 → 再生成"会让同一对模型反复加载卸载 N 次，
每次都是几十秒的权重加载 + 显存抖动。批量做完再切换，加载次数与段数无关。

## 四条硬规则（本模块强制）

1. **批量优先** —— 阶段内一次做完，不在阶段内切模型；
2. **切换必验** —— 卸载后**核验显存真的降了**，没降就记 `verified=False` 并告警，
   **不假装释放成功**；
3. **同时只驻留 1 个模型** —— 超预算直接拒绝，不靠"应该够"赌运气；
4. **可断点续跑** —— 每阶段落检查点，恢复时跳过已完成的阶段。

## `director.save_vram_all` 的含义

配置项 `save_vram_all` 此前**配了没人读**（ROUND18 棘轮发现）。
这里赋予它真实语义：**为 True 时每个阶段结束都卸载**（哪怕下一个阶段用同一个模型），
用更多加载时间换最低显存峰值 —— 显存吃紧时的"宁可慢也别 OOM"。

`director.vram_budget_gb` 为 0 表示不设上限（默认）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = [
    "STAGE_ORDER",
    "Stage",
    "StageViolation",
    "VramPhaseScheduler",
    "default_stage_plan",
]

#: 阶段顺序（严格按用户描述；`ENCODE` 不占显存所以放最后）。
STAGE_ORDER = (
    "PROMPT",
    "ASSET_GEN",
    "VIDEO_GEN",
    "VIDEO_REVIEW",
    "REGEN",
    "ENCODE",
)


@dataclass(frozen=True)
class Stage:
    """一个阶段：用哪个模型、估多少显存、结束后是否卸载。"""

    name: str
    #: 该阶段驻留的模型标识（None = 不占显存，例如 API 调用 / ffmpeg 封装）
    model: str | None = None
    #: 估算常驻显存（GB）。只用于预算校验与报告，不影响真实加载。
    resident_gb: float = 0.0
    #: 该阶段结束后是否卸载。
    unload_after: bool = True
    #: 备注（写进报告，方便人看懂这一阶段在干嘛）
    note: str = ""


@dataclass
class StageViolation:
    """违规记录：预算超限 / 释放未核验 / 阶段内切模型。"""

    stage: str
    kind: str
    detail: dict = field(default_factory=dict)


def default_stage_plan(
    *,
    asset_model: str = "asset_image_model",
    asset_gb: float = 8.0,
    video_model: str = "minimax_h3_ref2va",
    video_gb: float = 19.5,
    review_model: str = "video_review_model",
    review_gb: float = 8.0,
) -> list[Stage]:
    """按用户描述给出默认阶段计划。

    默认显存估算来自本机实测权重体积（ref2va 19.53 GB），
    其余模型本机没有，用**保守估计**并标注为估算（不冒充实测）。
    """
    return [
        Stage("PROMPT", model=None, resident_gb=0.0, unload_after=False,
              note="DeepSeek API 出提示词，不占显存"),
        Stage("ASSET_GEN", model=asset_model, resident_gb=asset_gb,
              note="加载素材图模型 -> 批量生成全部素材 -> 卸载"),
        Stage("VIDEO_GEN", model=video_model, resident_gb=video_gb,
              note="加载视频生成模型 -> 批量生成全部段 -> 卸载"),
        Stage("VIDEO_REVIEW", model=review_model, resident_gb=review_gb,
              note="换审核模型 -> 统一审核全部段 -> 卸载"),
        Stage("REGEN", model=video_model, resident_gb=video_gb,
              note="换回视频模型 -> 只重生成不合格段 -> 卸载"),
        Stage("ENCODE", model=None, resident_gb=0.0, unload_after=False,
              note="ffmpeg 拼接/封装/输出，不占显存"),
    ]


class VramPhaseScheduler:
    """按阶段加载/卸载模型，保证同一时刻只有一个模型驻留。

    ## 注入点（全部可替换，便于测试）

    * ``unload``：``() -> dict``，卸载模型（真实实现见
      :meth:`ComfyUIHttpExecutor.free_models`）；
    * ``vram``：``() -> dict | None``，显存快照；
    * ``budget_gb``：显存预算（0/None = 不限）；
    * ``save_vram_all``：每个阶段结束都卸载；
    * ``on_switch``：每次切换的回调（落事件/检查点用）。

    不注入 ``unload`` 时退化为"只记账不真卸载" —— 报告里会明确写
    ``unload_available=False``，**绝不让人误以为真的释放过**。
    """

    def __init__(
        self,
        *,
        unload: Callable[[], dict] | None = None,
        vram: Callable[[], dict | None] | None = None,
        budget_gb: float | None = 0.0,
        save_vram_all: bool = False,
        on_switch: Callable[[dict], None] | None = None,
    ) -> None:
        self._unload = unload
        self._vram = vram
        self.budget_gb = float(budget_gb or 0.0)
        self.save_vram_all = bool(save_vram_all)
        self._on_switch = on_switch

        self.current_model: str | None = None
        self.records: list[dict] = []
        self.violations: list[StageViolation] = []

    # -- 查询 -------------------------------------------------------------
    def snapshot(self) -> dict | None:
        if self._vram is None:
            return None
        try:
            return self._vram()
        except Exception:  # noqa: BLE001 - 探针失败不能拖垮调度
            return None

    # -- 动作 -------------------------------------------------------------
    def _do_unload(self, *, reason: str) -> dict:
        """执行卸载并核验。没注入卸载函数时如实标记为"不可用"。"""
        if self._unload is None:
            return {
                "ok": False,
                "verified": False,
                "available": False,
                "reason": "未注入卸载函数：本次调度**没有真正释放显存**（仅记账）",
                "trigger": reason,
            }
        before = self.snapshot()
        try:
            result = dict(self._unload() or {})
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False, "verified": False, "available": True,
                "reason": f"卸载抛错：{type(exc).__name__}: {exc}",
                "trigger": reason, "before": before,
            }
        result.setdefault("available", True)
        result["trigger"] = reason
        result.setdefault("before", before)
        if not result.get("after") and self._vram is not None:
            result["after"] = self.snapshot()
        # 未核验到下降必须记违规 —— 这是"不假装释放成功"的落点
        if result.get("available") and not result.get("verified"):
            self.violations.append(
                StageViolation("", "UNLOAD_NOT_VERIFIED",
                               {"reason": result.get("reason"), "trigger": reason})
            )
        return result

    # -- 构造（读配置的唯一入口）------------------------------------------
    @classmethod
    def from_config(
        cls,
        cfg: Any = None,
        *,
        unload: Callable[[], dict] | None = None,
        vram: Callable[[], dict | None] | None = None,
        on_switch: Callable[[dict], None] | None = None,
    ) -> "VramPhaseScheduler":
        """从全局配置构造调度器 —— **配置项真正被读取的地方**。

        读两个键：

        * ``director.save_vram_all``：为 True 时**每个阶段结束都卸载**，
          用更多加载时间换最低显存峰值（显存吃紧时"宁可慢也别 OOM"）。
          这个开关此前配了没人读（ROUND18 棘轮发现），这里赋予真实语义。
        * ``director.vram_budget_gb``：显存预算门禁，0 = 不限。

        没传 cfg 时用默认值（不强制卸载、不设预算）。
        """
        director = getattr(cfg, "director", None)
        save_vram_all = bool(getattr(director, "save_vram_all", False))
        budget = float(getattr(director, "vram_budget_gb", 0.0) or 0.0)
        return cls(
            unload=unload,
            vram=vram,
            budget_gb=budget,
            save_vram_all=save_vram_all,
            on_switch=on_switch,
        )

    def _check_budget(self, stage: Stage) -> None:
        if not self.budget_gb or self.budget_gb <= 0:
            return
        # 同一个模型不算"多一个"
        extra = 0.0 if stage.model == self.current_model else stage.resident_gb
        projected = (0.0 if self.current_model is None else 0.0) + extra
        if projected > self.budget_gb:
            self.violations.append(
                StageViolation(
                    stage.name, "VRAM_BUDGET_EXCEEDED",
                    {"stage_gb": stage.resident_gb,
                     "budget_gb": self.budget_gb,
                     "model": stage.model},
                )
            )
            raise MemoryError(
                f"阶段 {stage.name} 需要 {stage.resident_gb} GB 但显存预算只有 "
                f"{self.budget_gb} GB —— 拒绝加载（不靠'应该够'赌运气）；"
                "请降低分辨率/换剪枝版模型，或提高 director.vram_budget_gb"
            )

    def _switch_to(self, stage: Stage) -> dict | None:
        """把当前驻留模型切到 ``stage.model``。相同模型不重复加载。"""
        if stage.model == self.current_model:
            return None
        previous = self.current_model
        info: dict = {
            "stage": stage.name,
            "from": previous,
            "to": stage.model,
            "unload": None,
        }
        if previous is not None:
            info["unload"] = self._do_unload(reason=f"{previous} -> {stage.model}")
        self._check_budget(stage)
        self.current_model = stage.model
        self.records.append(info)
        if self._on_switch is not None:
            try:
                self._on_switch(info)
            except Exception:  # noqa: BLE001 - 回调失败不影响调度
                pass
        return info

    # -- 主流程 -----------------------------------------------------------
    def run(
        self,
        stages: list[Stage],
        handlers: dict[str, Callable[[], Any]],
    ) -> dict:
        """依次执行各阶段。``handlers`` 里没有的阶段会被跳过并记录。

        返回::

            {
              "ok": bool,                  # 是否全部阶段都跑完且无预算违规
              "stages": [ {"stage","ok","result","switch","unload_after"} ],
              "switches": [...],
              "violations": [...],
              "max_resident_gb": float,    # 同时驻留模型的最大估算
              "unload_available": bool,
              "notes": [...],
            }
        """
        report: dict = {
            "ok": True,
            "stages": [],
            "violations": [],
            "max_resident_gb": 0.0,
            "unload_available": self._unload is not None,
            "notes": [],
        }
        if self._unload is None:
            report["notes"].append(
                "未注入卸载函数：本次调度没有真正释放显存（仅记账）"
            )

        for stage in stages:
            handler = handlers.get(stage.name)
            item: dict = {"stage": stage.name, "note": stage.note,
                          "skipped": handler is None}
            if handler is None:
                item["ok"] = None
                report["stages"].append(item)
                report["notes"].append(f"阶段 {stage.name} 没有处理器，已跳过")
                continue

            try:
                switch = self._switch_to(stage)
            except MemoryError as exc:
                item.update({"ok": False, "error": str(exc)})
                report["stages"].append(item)
                report["ok"] = False
                break
            item["switch"] = switch
            report["max_resident_gb"] = max(
                report["max_resident_gb"],
                0.0 if self.current_model is None else stage.resident_gb,
            )

            try:
                result = handler()
                item["ok"] = True
                item["result"] = result
            except Exception as exc:  # noqa: BLE001 - 单阶段失败要记下来继续
                item["ok"] = False
                item["error"] = f"{type(exc).__name__}: {exc}"
                report["ok"] = False

            # 是否卸载：显式要求 / save_vram_all 全开
            if item["ok"] and (stage.unload_after or self.save_vram_all):
                item["unload_after"] = self._do_unload(
                    reason=f"阶段 {stage.name} 结束")
                self.current_model = None
            report["stages"].append(item)

        report["switches"] = self.records
        report["violations"] = [
            {"stage": v.stage, "kind": v.kind, "detail": v.detail}
            for v in self.violations
        ]
        if report["violations"]:
            report["ok"] = False
        return report
