"""pipeline 层异常。

必须与「技术失败」语义区分清楚：`PipelineError` 表示流程层不可继续
（缺文件、缺配置、能力未实现），属于任务书 `ErrorClass.FILE_ENCODING`
或 `INPUT_VALIDATION` 范畴，**绝不消耗 CNT2/CNT3 质量重做计数**。
"""

from __future__ import annotations

from ..models.enums import ErrorClass


class PipelineError(Exception):
    """拼接 / 封装 / 整片审核 / 交付阶段的可诊断错误。"""

    error_class: ErrorClass = ErrorClass.FILE_ENCODING

    def __init__(self, message: str, *, error_class: ErrorClass | None = None) -> None:
        super().__init__(message)
        if error_class is not None:
            self.error_class = error_class


__all__ = ["PipelineError"]
