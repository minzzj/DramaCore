"""日志与配置导入导出（图一 SG7 `Z3` / `Z4` / `Z5`）。

## 对应流程图

    Z3["配置导入导出支持JSON"]
    Z4["日志级别默认INFO可调"]
    Z5["日志记录所有手动操作和自动切换"]

## 为什么单独一个模块

这三个都是**横切关注点**：不属于提示词/素材/导演任何一层，却要在多处生效。
放一起，调用方只需知道两个函数：

* :func:`apply_logging` —— 把 `global.log_level` 真正应用到根 logger；
* :func:`log_manual_or_switch` —— 在 `global.log_all_manual_and_auto_switch`
  打开时记录人工操作与自动切换。

## 修的是什么缺口（ROUND18/19）

配置里三个键都有值、`config.py` 也校验了取值范围，但**产品代码零读取**：

* `log_level` 只校验了 `DEBUG/INFO/WARN/ERROR`，从没用来设置日志级别；
* `log_all_manual_and_auto_switch` 从没被读过；
* `config_import_export` 没有对应的导入导出实现。

后果：愿景要求的「开源用户都是开发者，所有手动操作和自动切换仅在日志中记录」
这条**没有落地** —— 用户想排查"到底是人工批的还模型批的"时看不到日志。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

__all__ = [
    "LOG_LEVELS",
    "apply_logging",
    "log_manual_or_switch",
    "export_config_json",
    "import_config_json",
    "SECRET_KEYS",
]

#: 允许的日志级别（与 `config.py` 的校验口径一致）。
LOG_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")

#: 导入导出时**必须脱敏**的键。真实密钥绝不写进导出文件。
SECRET_KEYS = frozenset({"api_key", "api_key_env"})

_LOG = logging.getLogger("bettercomfyui")

#: 记录本模块是否已应用过级别（避免每次调用都改全局配置）
_APPLIED: dict[str, Any] = {"level": None}


def _level_name(value: Any) -> str:
    """把配置里的级别规整成 logging 认识的名字。"""
    text = str(value or "INFO").strip().upper()
    # 兼容 Python 的 WARNING 写法（配置里用 WARN）
    if text == "WARNING":
        text = "WARN"
    if text not in LOG_LEVELS:
        raise ValueError(
            f"log_level 只能是 {'/'.join(LOG_LEVELS)}，收到 {value!r}"
        )
    return text


def apply_logging(cfg: Any = None, *, level: str | None = None) -> str:
    """把日志级别真正应用到根 logger。返回最终级别名。

    * `level` 显式给出时优先（例如 CLI 的 `--log-level`）；
    * 否则从 `cfg.global_.log_level` 取；
    * 都没有时用 INFO（流程图 `Z4` 的默认值）。

    只配置**一次**：重复调用同样的级别不会反复重排 handler
    （否则每次加载配置都会往 root 上叠一个新的 StreamHandler，日志成倍重复）。
    """
    if level is None:
        global_params = getattr(cfg, "global_", None)
        level = getattr(global_params, "log_level", None) if global_params else None
    name = _level_name(level)
    if _APPLIED["level"] == name:
        return name

    numeric = getattr(logging, name if name != "WARN" else "WARNING")
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=numeric,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    else:
        root.setLevel(numeric)
    _LOG.setLevel(numeric)
    _APPLIED["level"] = name
    return name


def log_manual_or_switch(
    cfg: Any,
    message: str,
    *,
    operator: str | None = None,
    action: str | None = None,
    **detail: Any,
) -> bool:
    """记录一条「人工操作」或「自动切换」日志。返回是否真的记了。

    `global.log_all_manual_and_auto_switch` 为 False 时**不记**（返回 False）。
    默认 True —— 愿景要求「所有手动操作和自动切换仅在日志中记录即可」，
    开源用户都是开发者，这类信息对排查至关重要。

    为什么不直接用 `_LOG.info(...)` 完事：那样这个开关就永远是摆设。
    这里把开关变成"真的会改变行为"的代码路径。
    """
    global_params = getattr(cfg, "global_", None)
    enabled = getattr(global_params, "log_all_manual_and_auto_switch", True)
    if not enabled:
        return False
    payload = " ".join(f"{k}={v}" for k, v in detail.items() if v is not None)
    prefix = []
    if action:
        prefix.append(f"[{action}]")
    if operator:
        prefix.append(f"operator={operator}")
    line = " ".join(prefix + [message, payload]).strip()
    _LOG.info("%s", line)
    return True


def _redact(node: Any) -> Any:
    """递归脱敏：`api_key` 一类的值替换成 `***`。"""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if str(k) in SECRET_KEYS and v:
                out[k] = "***"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(node, list):
        return [_redact(v) for v in node]
    return node


def export_config_json(cfg: Any, path: Path | str | None = None) -> str:
    """把当前生效配置导出成 JSON（**已脱敏**）。返回 JSON 文本。

    图一 SG7 `Z3`「配置导入导出支持 JSON」。

    ⚠️ `api_key` / `api_key_env` 会被替换成 `***` ——
    真实密钥**不得**写进导出文件（任务书第 10 节：配置样例不得写入真实 API 密钥）。
    """
    payload = _redact(cfg.to_dict() if hasattr(cfg, "to_dict") else dict(cfg))
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if path is not None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
    return text


def import_config_json(source: Path | str | dict) -> dict:
    """读取一份导出过的配置 JSON，返回规范化的 `dict`。

    只做**结构与类型**的基本校验；真正的字段校验仍由 `config.py` 负责
    （导入一份坏配置不该在导入时就崩，而应在应用时给出明确错误）。

    脱敏值 `***` 会被**丢弃**而不是写回去 ——
    否则导入之后 `api_key` 就成了字面量 `***`，看起来"配了"实际是坏的。
    """
    if isinstance(source, dict):
        data = dict(source)
    else:
        p = Path(source)
        if not p.is_file():
            raise FileNotFoundError(f"配置文件不存在：{p}")
        data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"配置必须是 JSON 对象，收到 {type(data).__name__}")

    def _drop_masked(node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if str(k) in SECRET_KEYS and v == "***":
                    continue          # 脱敏占位符不是真值，丢掉
                out[k] = _drop_masked(v)
            return out
        if isinstance(node, list):
            return [_drop_masked(v) for v in node]
        return node

    return _drop_masked(data)
