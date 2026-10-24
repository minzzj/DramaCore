"""prompt 层的异常基类与依赖缺失错误。

约定：**缺依赖、缺配置一律显式抛错，绝不静默降级到占位实现**。
静默回退（例如「store 没就绪就用内存假库」「网关没配 key 就用 mock」）会把
假成功带进流水线，是本项目明令禁止的行为。
"""

from __future__ import annotations

__all__ = ["PromptError", "PromptDependencyError", "SuspectPersistenceError"]


class PromptError(Exception):
    """prompt 层所有可预期错误的基类。"""


class PromptDependencyError(PromptError):
    """依赖模块/接口缺失（例如 ``bettercomfyui.store`` 尚未就绪）。

    抛这个错而不是回退到假实现——调用方必须显式提供替身（测试）或等待依赖。
    """


class SuspectPersistenceError(PromptDependencyError):
    """疑似重复记录无法落盘。

    对应 R1 红线中的「绝不静默丢弃」：宁可报错，也不能把疑似重复丢掉。
    """
