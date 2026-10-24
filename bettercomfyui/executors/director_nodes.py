"""导演节点适配层骨架（契约 §7 执行器清单）。

真实节点类名来自本地仓库实测（非臆测）：
- ChainDirector:      `保留备用的节点/ComfyUI-MiniMaxH3-ChainDirector/__init__.py`
                      -> MiniMaxH3ChainDirector / MiniMaxH3ChainDirectorEN
- AIMixer Director:   `保留备用的节点/ComfyUI_MiniMaxH3_Director/__init__.py`
                      -> MiniMaxH3Director（别名 ComfyMiniMaxH3Director）
- SmartDirector:      `保留备用的节点/ComfyUI_MiniMaxH3_Smart_Director/__init__.py`
                      -> MiniMaxH3SmartDirector / MiniMaxH3SmartDirectorAllInOne / MiniMaxH3SmartPlanInspector
- H3-Chinese-Director:`保留备用的节点/H3-Chinese-Director/.../H3_Prompt_Sidecar/__init__.py`
                      -> H3PromptSidecar

红线：节点缺失时**抛明确错误**（`DirectorNodeUnavailable`，携带缺失清单与安装前置条件），
不得静默失败、不得退化成 mock 继续跑。
"""

from __future__ import annotations

from typing import Iterable

from bettercomfyui.executors.base import (
    ExecutorError,
    ExecutorUnavailable,
    JobRef,
    validate_capabilities,
)
from bettercomfyui.executors.comfyui_http import ComfyUIHttpExecutor
from bettercomfyui.models.enums import ErrorClass

__all__ = [
    "DIRECTOR_NODE_MAP",
    "DirectorNodeExecutor",
    "DirectorNodeUnavailable",
]

#: 逻辑导演能力 -> 本地仓库中实际存在的 node class 名（任一同名即可）。
DIRECTOR_NODE_MAP: dict[str, tuple[str, ...]] = {
    "chain_director": ("MiniMaxH3ChainDirector", "MiniMaxH3ChainDirectorEN"),
    "aimixer_director": ("MiniMaxH3Director", "ComfyMiniMaxH3Director"),
    "smart_director": (
        "MiniMaxH3SmartDirector",
        "MiniMaxH3SmartDirectorAllInOne",
        "MiniMaxH3SmartPlanInspector",
    ),
    "h3_chinese_director": ("H3PromptSidecar", "H3ChineseDirector"),
}


class DirectorNodeUnavailable(ExecutorUnavailable):
    """导演节点未安装 / 不兼容。必须携带缺失清单。"""

    def __init__(self, message: str, *, missing: Iterable[str], detail: dict | None = None) -> None:
        payload = {"missing": list(missing)}
        payload.update(detail or {})
        super().__init__(
            message,
            error_class=ErrorClass.COMFY_WORKFLOW,
            detail=payload,
        )
        self.missing = list(missing)


class DirectorNodeExecutor(ComfyUIHttpExecutor):
    """在 ComfyUI HTTP 执行器之上加一层「导演节点是否存在」的硬门禁。"""

    name = "director_nodes"

    def __init__(
        self,
        base_url: str | None = None,
        *,
        required: Iterable[str] = ("aimixer_director",),
        transport=None,
        **kwargs,
    ) -> None:
        if base_url is None:
            super().__init__(transport=transport, **kwargs)
        else:
            super().__init__(base_url, transport=transport, **kwargs)
        unknown = [r for r in required if r not in DIRECTOR_NODE_MAP]
        if unknown:
            raise ExecutorError(
                f"未知的导演能力名：{unknown}；可选：{sorted(DIRECTOR_NODE_MAP)}",
                error_class=ErrorClass.INPUT_VALIDATION,
            )
        self.required = tuple(required)

    # -- 探测 -------------------------------------------------------------
    def probe_director_nodes(self) -> dict:
        probe = self.probe()
        present = set(probe.get("node_types") or [])
        available: dict[str, str | None] = {}
        missing: list[str] = []
        for capability in self.required:
            hit = next((c for c in DIRECTOR_NODE_MAP[capability] if c in present), None)
            available[capability] = hit
            if hit is None:
                missing.append(capability)
        return {
            "reachable": bool(probe.get("reachable")),
            "available": available,
            "missing": missing,
            "candidates": {k: list(DIRECTOR_NODE_MAP[k]) for k in self.required},
            # 节点探测同样不是真实生成验证（R8）
            "verified": False,
            "mock": False,
            "probe": probe,
        }

    def require_nodes(self) -> dict:
        """节点齐全则返回探测结果；缺失则抛 `DirectorNodeUnavailable`（绝不静默）。"""
        report = self.probe_director_nodes()
        if report["missing"]:
            detail = {
                "available": report["available"],
                "candidates": report["candidates"],
                "reachable": report["reachable"],
                "comfyui_version": report["probe"].get("comfyui_version"),
            }
            raise DirectorNodeUnavailable(
                "导演节点缺失或未安装，无法执行导演台工作流："
                + "，".join(
                    f"{cap}（候选类名：{'/'.join(report['candidates'][cap])}）"
                    for cap in report["missing"]
                )
                + "；请安装对应 custom_nodes 后重试。"
                + ("（当前 ComfyUI 不可达）" if not report["reachable"] else ""),
                missing=report["missing"],
                detail=detail,
            )
        return report

    # -- 门禁 -------------------------------------------------------------
    def submit(self, workflow: dict, params: dict, *, idempotency_key: str) -> JobRef:
        self.require_nodes()  # 先验节点，再提交；缺失一律报错，不退化为 mock
        return super().submit(workflow, params, idempotency_key=idempotency_key)

    def capabilities(self) -> dict:
        base = super().capabilities()
        try:
            report = self.require_nodes()
            nodes_ok = True
            available = report["available"]
        except DirectorNodeUnavailable as exc:
            nodes_ok = False
            available = exc.detail.get("available")
        caps = dict(base)
        caps["nodes_available"] = bool(nodes_ok)
        caps["nodes"] = available
        caps["required_nodes"] = list(self.required)
        caps["mock"] = False
        caps["verified"] = False
        caps["notes"] = (
            base["notes"]
            + f"；导演节点可用={nodes_ok}（需要 {list(self.required)}）。"
            "节点存在也不等于真实生成已验证：仍需模型权重与 ≥24GB 显存目标机实测。"
        )
        return validate_capabilities(caps)
