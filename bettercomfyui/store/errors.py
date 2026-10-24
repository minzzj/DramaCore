"""store 层异常体系（唯一真源）。

设计原则（对应 docs/CONTRACTS.md 第 0 节的五条硬规则）：

* 所有可预期错误都是 :class:`StoreError` 的子类 —— 上层只需捕获一个基类即可，
  但每类错误都携带可操作的上下文，绝不用「宽泛异常 + 空消息」掩盖真实原因。
* **绝不静默失败**：拿不到、对不上、锁不住、越界，一律抛错。任何「返回 None／
  返回空字典」都必须有明确的契约语义（例如 ``resolve_lock() -> None`` 表示
  「该版本不可用于生成」，这是 R3 的显式门禁，不是失败吞掉）。
* 错误消息里必须带 **可定位的信息**（asset_id / version / 路径 / 期望值 / 实际值）。

本模块不得 import prompt/review/director/pipeline（依赖方向单向）。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "StoreError",
    "SerializationError",
    "CorruptFileError",
    "LockTimeoutError",
    "PathIsolationError",
    "ProjectNotFoundError",
    "ProjectExistsError",
    "ProjectBusyError",
    "ConfirmTokenMismatchError",
    "AssetNotFoundError",
    "VersionNotFoundError",
    "ContentHashMismatchError",
    "LockAlreadyExistsError",
    "ImmutableAssetError",
    "InvalidIdentifierError",
    "parse_enum",
]


class StoreError(Exception):
    """store 层所有可预期错误的基类。

    额外的 ``context`` 以关键字形式携带结构化信息，便于写入事件日志，
    不会污染 ``str(exc)``（``str`` 只包含人类可读的主消息 + 关键上下文摘要）。
    """

    #: 稳定的机器可读错误码，便于上层分类（技术失败 vs 质量失败）
    code = "STORE_ERROR"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = str(message)
        self.context: dict[str, Any] = dict(context)

    def __str__(self) -> str:  # pragma: no cover - 便利方法
        if not self.context:
            return self.message
        parts = ", ".join(f"{key}={value!r}" for key, value in sorted(self.context.items()))
        return f"{self.message} ({parts})"


class SerializationError(StoreError):
    """对象无法被规范化为确定性 JSON（不支持的类型 / NaN / Inf / 键冲突）。"""

    code = "SERIALIZATION_ERROR"


class CorruptFileError(StoreError):
    """磁盘上的 JSON 文件损坏或结构不符合预期。绝不当作空内容继续。"""

    code = "CORRUPT_FILE"


class LockTimeoutError(StoreError):
    """在 ``timeout_s`` 内未能取得文件锁，或锁文件无法使用。"""

    code = "LOCK_TIMEOUT"


class PathIsolationError(StoreError):
    """R5：路径越界或标识符非法（包含分隔符、``..``、绝对路径等）。"""

    code = "PATH_ISOLATION"


class ProjectNotFoundError(StoreError):
    code = "PROJECT_NOT_FOUND"


class ProjectExistsError(StoreError):
    code = "PROJECT_EXISTS"


class ProjectBusyError(StoreError):
    """项目有运行中的任务或未保存状态，禁止关闭/删除。"""

    code = "PROJECT_BUSY"


class ConfirmTokenMismatchError(StoreError):
    """删除项目时二次确认 token 不匹配。"""

    code = "CONFIRM_TOKEN_MISMATCH"


class AssetNotFoundError(StoreError):
    code = "ASSET_NOT_FOUND"


class VersionNotFoundError(StoreError):
    code = "VERSION_NOT_FOUND"


class ContentHashMismatchError(StoreError):
    """R3：``content_hash`` 与磁盘实际内容不一致，LOCK 必须被拒绝。

    典型场景：素材文件被外部篡改、传入的 hash 是旧版本的、或元数据被手工编辑过。
    """

    code = "CONTENT_HASH_MISMATCH"


class LockAlreadyExistsError(StoreError):
    """R3 第二道防线：该版本已有 LOCK，拒绝用不同的审核结论静默改写。

    合法路径：先用 :meth:`AssetStore.clear_lock` 显式解绑（留痕），再重新 ``set_lock``。
    """

    code = "LOCK_ALREADY_EXISTS"


class ImmutableAssetError(StoreError):
    """R2：试图原地覆盖已存在的素材文件 / 版本目录。素材库只追加。"""

    code = "IMMUTABLE_ASSET"


class InvalidIdentifierError(StoreError):
    """标识符格式不符合 docs/CONTRACTS.md 第 2 节规范。"""

    code = "INVALID_IDENTIFIER"


def parse_enum(enum_cls: Any, value: Any, *, kind: str | None = None) -> Any:
    """统一的枚举解析入口：非法取值抛 :class:`StoreError`（不静默回退）。

    ``models.enums._StrEnum.parse`` 抛的是 ``ValueError``；store 层把它统一成
    ``StoreError``，这样调用方只需捕获一个基类。**绝不返回默认值**——
    「把未识别状态当成正常状态」是假成功的典型来源。
    """
    try:
        return enum_cls.parse(value)
    except ValueError as exc:
        raise StoreError(
            f"{enum_cls.__name__} 取值非法：{exc}",
            kind=kind,
            value=repr(value),
        ) from exc
