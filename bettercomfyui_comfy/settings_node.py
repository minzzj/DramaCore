"""配置节点：把服务器端的 API 配置喂给工作流，**密钥不落进工作流文件**。

## 为什么必须有这个节点

`StoryboardNode` 的 `api_key` 是一个 `STRING` widget。用户如果直接在里面填密钥，
密钥就会被**保存进工作流 JSON**（`user/default/workflows/*.json`）——
一旦分享工作流、提交仓库、或者截图，密钥就泄露了。

所以正确做法是：**密钥留在服务器配置里**（`config/user.yaml` 或环境变量），
工作流只通过本节点把它取出来喂给下游。工作流文件里永远只有一个节点引用，
没有密钥明文。

## 输出

* `api_key`  —— 生效的密钥（环境变量优先，其次配置文件）；**只作为连线传输，不显示**
* `model`    —— 模型名（如 `deepseek-chat`）
* `api_base` —— 接口地址（留空则用官方默认）
* `状态`     —— 人可读的状态摘要（"已配置（来源：环境变量 …）"）

## 安全

* 输入面板里**看不到密钥**（节点没有密钥输入框）；
* 密钥只在节点执行时从服务器读，输出到下游节点的端口上；
* ComfyUI 的 `/history` 会记录节点的输入输出 —— 所以本节点**刻意把
  `api_key` 设为 `forceInput=False` 的输出**，并在返回时把密钥放进
  `ui` 隐藏字段而不是主输出？不行，下游要拿到真值。
  折中：输出真值给下游（这是必须的），但在**节点自己的 UI 显示**里只显示遮罩，
  并把 `状态` 写成不含密钥的摘要。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "BetterComfyUISettings",
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "resolve_runtime_secret",
]

def _resolve_repo_root() -> Path:
    """定位仓库根。

    ⚠️ 装到 `<repo>/.comfyui/custom_nodes/` 下时 `parent.parent` 是
    `custom_nodes/`，不是仓库根 —— 会读错配置。复用 `_bridge` 的候选探测。
    """
    try:
        from . import _bridge

        for candidate in _bridge._candidate_roots():  # noqa: SLF001
            root = Path(candidate)
            # 强标记：**必须同时**有默认配置与编排包本体。
            # 只用 "有 config 目录" 太弱 —— \.comfyui/custom_nodes\ 下也可能
            # 出现 config 目录（本轮实测：错误的写入就在那里创建了一个），
            # 于是抢先匹配、定位到错误位置。
            if ((root / "config" / "default.yaml").is_file()
                    and (root / "bettercomfyui" / "__init__.py").is_file()):
                return root
    except Exception:  # noqa: BLE001
        pass
    return Path(__file__).resolve().parent.parent


REPO_ROOT = _resolve_repo_root()
DEFAULT_ENV_NAME = "BETTERCOMFYUI_API_KEY"


def _load_config():
    try:
        from bettercomfyui.config import load_config

        return load_config(repo_root=REPO_ROOT)
    except Exception:  # noqa: BLE001 - 配置不可用不该让节点崩
        return None


def resolve_runtime_secret(cfg=None) -> dict:
    """解析当前生效的 API 配置。环境变量优先，其次配置文件。"""
    cfg = cfg if cfg is not None else _load_config()
    prompt = getattr(cfg, "prompt", None) if cfg is not None else None

    env_name = getattr(prompt, "api_key_env", None) or DEFAULT_ENV_NAME
    env_value = os.environ.get(env_name)
    config_key = getattr(prompt, "api_key", None)

    if env_value:
        key, source = env_value.strip(), f"环境变量 {env_name}"
    elif config_key:
        key, source = str(config_key).strip(), "config/user.yaml"
    else:
        key, source = "", "未配置"

    return {
        "api_key": key,
        "model": getattr(prompt, "model_name", None) or "deepseek-chat",
        "api_base": getattr(prompt, "endpoint", None) or "",
        "provider": getattr(prompt, "model_provider", None) or "external_api",
        "api_key_env": env_name,
        "source": source,
        "configured": bool(key),
    }


def _mask(text: str) -> str:
    if not text:
        return "（空）"
    if len(text) <= 8:
        return "***"
    return text[:3] + "***" + text[-4:]


class BetterComfyUISettings:
    """读取服务器端 API 配置，输出给下游节点使用。"""

    CATEGORY = "BetterComfyUI/全局配置"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("api_key", "model", "api_base", "状态")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {},
            "optional": {
                "模型覆盖": ("STRING", {
                    "default": "",
                    "tooltip": (
                        "留空 = 用服务器配置里的模型。\n"
                        "DeepSeek 常用：deepseek-chat（快）/ deepseek-reasoner（推理强）"
                    ),
                }),
                "接口覆盖": ("STRING", {
                    "default": "",
                    "tooltip": (
                        "留空 = 用服务器配置。\n"
                        "DeepSeek 官方：https://api.deepseek.com\n"
                        "千问：https://dashscope.aliyuncs.com/compatible-mode/v1"
                    ),
                }),
            },
        }

    def run(self, 模型覆盖: str = "", 接口覆盖: str = ""):
        info = resolve_runtime_secret()
        model = (模型覆盖 or "").strip() or info["model"]
        base = (接口覆盖 or "").strip() or info["api_base"]

        if info["configured"]:
            status = (
                f"✅ API 已配置（来源：{info['source']}，密钥 {_mask(info['api_key'])}）\n"
                f"模型：{model}\n"
                f"接口：{base or '（官方默认）'}\n"
                f"环境变量名：{info['api_key_env']}"
            )
        else:
            status = (
                "❌ API 未配置 —— 下游剧本节点会调用失败。\n"
                "请在 ComfyUI 右侧「Better ComfyUI」面板的「API 配置」里填写密钥，\n"
                f"或设置环境变量 {info['api_key_env']}。\n"
                "（密钥保存在服务器 config/user.yaml，**不会写进工作流文件**）"
            )

        return (info["api_key"], str(model), str(base), status)


NODE_CLASS_MAPPINGS = {"BetterComfyUISettings": BetterComfyUISettings}
NODE_DISPLAY_NAME_MAPPINGS = {
    "BetterComfyUISettings": "API 配置（DeepSeek/千问）",
}
