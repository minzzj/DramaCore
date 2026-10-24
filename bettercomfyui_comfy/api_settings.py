"""API 配置接口（DeepSeek / 千问 / 自配接口）。

## 用户提的问题

> 你没有设置 DeepSeek 之类的 API 的配置接口等，并不能真正实现。

确实：`prompt.api_key` / `prompt.model` / `prompt.endpoint` 在配置里都有、
`resolve_secret()` 也会「环境变量优先、其次配置字段」，
但**用户在界面上没有任何入口去填** —— 于是工作流里的剧本巨人永远拿不到密钥，
等于跑不起来。

本模块补的就是这个缺口：一个能**保存 / 读取** API 配置的 HTTP 接口。

## 三条安全底线

1. **绝不回传完整密钥**：读取接口只返回 `sk-***abcd` 这种遮罩形式，
   外加「是否已配置 / 来源是环境变量还是配置文件」。
2. **只写白名单字段**：`api_key` / `model` / `model_provider` / `endpoint` /
   `api_key_env` / `output_language` / `output_format`，其它键一律丢弃 ——
   防止有人借这个接口往用户配置里塞任意内容。
3. **写 `config/user.yaml`，不写 `config/default.yaml`**：
   `default.yaml` 是**自动生成**的默认值文件（有测试守着"必须等于 `Config()` 的序列化"），
   手写进去会被判为漂移；`user.yaml` 才是本地用户配置，且不随仓库分发。

## 密钥从哪里生效

`resolve_secret(cfg)` 的优先级是：

    环境变量 `BETTERCOMFYUI_API_KEY`（或 `prompt.api_key_env` 指定的名字）
        > `config/user.yaml` 的 `prompt.api_key`

所以界面上填的密钥会写进 `user.yaml`；如果同时设置了环境变量，
**环境变量优先**（更适合 CI/生产）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

__all__ = [
    "ALLOWED_FIELDS",
    "ALLOWED_SECTIONS",
    "DEFAULT_ENV_NAME",
    "REPO_ROOT",
    "handle_get",
    "handle_save",
    "mask_secret",
    "save_user_settings",
    "settings_snapshot",
]

#: 仓库根目录（插件装在 `<repo>/.comfyui/custom_nodes/bettercomfyui_comfy`）。
def _resolve_repo_root() -> Path:
    """定位**仓库根目录**。

    ⚠️ 不能用 `Path(__file__).parent.parent`：插件装到
    `<repo>/.comfyui/custom_nodes/bettercomfyui_comfy` 之后，这个表达式会得到
    `.../custom_nodes`，于是 `config/user.yaml` 被写到
    `custom_nodes/config/user.yaml` —— 而用户的真实配置在仓库根 `config/` 下，
    **写错地方等于没保存**（本轮实测踩到：POST 返回 ok=False 且 user.yaml 没变）。

    正确做法是复用 `_bridge` 已有的候选根目录探测（它也认
    `BETTERCOMFYUI_HOME` 环境变量，便于非标准部署）。
    """
    try:
        from . import _bridge

        for candidate in _bridge._candidate_roots():  # noqa: SLF001 - 内部约定
            root = Path(candidate)
            # 强标记：**必须同时**有默认配置与编排包本体。
            # 只用 "有 config 目录" 太弱 —— \.comfyui/custom_nodes\ 下也可能
            # 出现 config 目录（本轮实测：错误的写入就在那里创建了一个），
            # 于是抢先匹配、定位到错误位置。
            if ((root / "config" / "default.yaml").is_file()
                    and (root / "bettercomfyui" / "__init__.py").is_file()):
                return root
    except Exception:  # noqa: BLE001 - 探测失败退回保守值
        pass
    return Path(__file__).resolve().parent.parent


#: 仓库根目录（安装后也能正确定位）。
REPO_ROOT = _resolve_repo_root()

#: 允许通过界面配置的字段（白名单）。**绝不允许任意键写入用户配置**。
ALLOWED_FIELDS = (
    "model_provider", "model_name", "endpoint", "api_key",
    "api_key_env", "output_language", "output_format",
)

#: 允许写入 user.yaml 的顶层 section（白名单）。
ALLOWED_SECTIONS = ("prompt",)

#: 默认环境变量名（与 `config.resolve_secret` 的默认一致）。
DEFAULT_ENV_NAME = "BETTERCOMFYUI_API_KEY"


def mask_secret(secret: Any) -> str:
    """把密钥遮成 `abc***wxyz`，**绝不回传完整密钥**。"""
    if not secret:
        return ""
    text = str(secret)
    if len(text) <= 8:
        return "***"
    return text[:3] + "***" + text[-4:]


def _load_config():
    from bettercomfyui.config import load_config

    return load_config(repo_root=REPO_ROOT)


def settings_snapshot() -> dict:
    """当前 API 配置的**只读快照**（密钥已遮）。"""
    try:
        cfg = _load_config()
        prompt = getattr(cfg, "prompt", None)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False,
                "error": f"读取配置失败：{type(exc).__name__}: {exc}"}

    api_key = getattr(prompt, "api_key", None)
    env_name = getattr(prompt, "api_key_env", None) or DEFAULT_ENV_NAME
    env_value = os.environ.get(env_name)

    # 多来源合并：用户可能通过**任意一个**入口配置，
    # 界面状态要如实反映「到底哪一处生效」，不能只看我自己的 user.yaml。
    #   1. 环境变量（最高优先）
    #   2. config/user.yaml
    #   3. ComfyUI 原生设置（剧本巨人在那里注册了 StoryBoard.api_key）
    #   4. 剧本巨人自己的 settings.json
    native_key = _read_native_comfy_setting()
    giant_key = _read_giant_settings_key()

    effective = env_value or api_key or native_key or giant_key
    if env_value:
        source = "env"
    elif api_key:
        source = "config"
    elif native_key:
        source = "ComfyUI 原生设置（StoryBoard.api_key）"
    elif giant_key:
        source = "剧本巨人 settings.json"
    else:
        source = "none"

    if source == "env":
        hint = f"密钥来源：环境变量 {env_name}"
    elif source == "config":
        hint = "密钥来源：config/user.yaml"
    else:
        hint = ("尚未配置密钥。三种方式任选其一："
                "① ComfyUI 原生「设置」里填 StoryBoard.api_key（推荐，剧本巨人自带入口）；"
                "② 本面板填写；"
                f"③ 设置环境变量 {env_name}（优先级最高）。")

    return {
        "ok": True,
        "provider": getattr(prompt, "model_provider", None),
        "model": getattr(prompt, "model_name", None),
        "endpoint": getattr(prompt, "endpoint", None),
        "api_key_env": env_name,
        "api_key_masked": mask_secret(effective),
        "api_key_configured": bool(effective),
        "api_key_source": source,
        "output_language": getattr(prompt, "output_language", None),
        "output_format": getattr(prompt, "output_format", None),
        "hint": hint,
        "user_config_path": str((REPO_ROOT / "config" / "user.yaml")),
    }


def save_user_settings(updates: dict) -> dict:
    """把配置写入 `config/user.yaml`（**只写白名单字段，保留其它键**）。"""
    try:
        import yaml
    except ImportError:  # pragma: no cover - 环境相关
        return {"ok": False, "error": "缺少 PyYAML，无法写 user.yaml"}

    user_path = REPO_ROOT / "config" / "user.yaml"
    if user_path.is_file():
        try:
            existing = yaml.safe_load(user_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            # **不覆盖**：解析失败的配置可能包含用户其它重要设置
            return {"ok": False,
                    "error": f"user.yaml 解析失败（未覆盖，避免毁掉你的配置）：{exc}"}
        if not isinstance(existing, dict):
            return {"ok": False, "error": "user.yaml 顶层不是字典，拒绝覆盖"}
    else:
        existing = {}

    written: list[str] = []
    for section, values in (updates or {}).items():
        if section not in ALLOWED_SECTIONS or not isinstance(values, dict):
            continue
        bucket = existing.setdefault(section, {})
        if not isinstance(bucket, dict):
            bucket = {}
            existing[section] = bucket
        for key, value in values.items():
            if key not in ALLOWED_FIELDS:
                continue
            bucket[key] = value
            written.append(f"prompt.{key}")

    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text(
        yaml.safe_dump(existing, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    # ---- 同步给剧本巨人（**不做这一步，界面上填了密钥巨人也不认**）----
    # `ComfyUI-Storyboard-LLM` 没有 api_key 输入端口，它只读自己目录下的
    # settings.json（api_key / base_url / output_path）。
    prompt_now = existing.get("prompt") or {}
    # ROUND58 安全修复：**不再把 API Key 同步到 ComfyUI 原生设置 / 剧本巨人 settings.json**。
    # 那些文件可被无鉴权的 `/settings`、`/storyboard/settings` 读取，等于密钥明文外露。
    # 这里只同步 base_url，并主动清除旧版本可能已经写进去的 api_key。
    giant = sync_giant_settings(
        api_key="",
        base_url=str(prompt_now.get("endpoint") or ""),
    )
    native = _sync_native_comfy_setting(
        api_key="",
        base_url=str(prompt_now.get("endpoint") or ""),
    )
    return {"ok": True, "written": written,
            "path": str(user_path.relative_to(REPO_ROOT)),
            "giant": giant, "native": native}


def _sync_native_comfy_setting(*, api_key: str, base_url: str) -> dict:
    r"""ROUND58 安全修复：只同步 base_url，**绝不写 api_key** 到 ComfyUI 原生设置。

    ComfyUI 原生 `/settings` 无鉴权，写 `StoryBoard.api_key` 等于把密钥明文暴露给
    同进程/同网段任何能访问服务的人；这里会主动删除旧版本写入的 key 字段。
    """
    try:
        data: dict = {}
        if COMFY_SETTINGS_FILE.is_file():
            try:
                data = json.loads(COMFY_SETTINGS_FILE.read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001 - 坏文件不覆盖
                return {"ok": False, "reason": "comfy.settings.json 解析失败，未覆盖"}
            if not isinstance(data, dict):
                return {"ok": False, "reason": "comfy.settings.json 顶层不是字典"}
        scrubbed = False
        for key in ("StoryBoard.api_key", "storyboard.api_key"):
            if key in data:
                data.pop(key, None)
                scrubbed = True
        if base_url:
            data["StoryBoard.base_url"] = base_url
        COMFY_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        COMFY_SETTINGS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
        return {"ok": True, "path": str(COMFY_SETTINGS_FILE),
                "scrubbed_api_key": scrubbed, "note": "安全：不写 API Key"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"写原生设置失败：{exc}"}


#: ComfyUI 用户设置文件（原生「设置」对话框持久化到这里）。
COMFY_SETTINGS_FILE = REPO_ROOT / ".comfyui" / "user" / "default" / "comfy.settings.json"


def _read_native_comfy_setting() -> str:
    r"""读 ComfyUI **原生设置**里的剧本巨人密钥（`StoryBoard.api_key`）。

    巨人自带 web 扩展在原生设置里注册了 `StoryBoard.api_key`，
    改动它会 POST 到 `/storyboard/settings` 并写回巨人自己的 settings.json。
    所以这里把它也当作一个**合法来源**，状态显示才准确。
    """
    try:
        data = json.loads(COMFY_SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 文件不存在/坏掉都按未配置处理
        return ""
    for key in ("StoryBoard.api_key", "storyboard.api_key"):
        value = data.get(key)
        if value:
            return str(value).strip()
    return ""


def _read_giant_settings_key() -> str:
    """读剧本巨人自己 settings.json 里的密钥（它的节点实际用的就是这份）。"""
    giant = REPO_ROOT / ".comfyui" / "custom_nodes" / "ComfyUI-Storyboard-LLM" / "settings.json"
    try:
        data = json.loads(giant.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    return str(data.get("api_key") or "").strip()


def sync_giant_settings(*, api_key: str, base_url: str) -> dict:
    """ROUND58 安全修复：只同步 base_url，并清除旧版本写入的 api_key。

    剧本巨人的 settings.json 可被无鉴权的 `/storyboard/settings` 读取；写 key 等于明文外露。
    保留此函数是为了兼容旧调用点（例如其它节点/脚本），但**不再落盘密钥**。
    """
    giant_dir = REPO_ROOT / ".comfyui" / "custom_nodes" / "ComfyUI-Storyboard-LLM"
    if not giant_dir.is_dir():
        return {"ok": False, "reason": "巨人包未安装（跳过）"}
    target = giant_dir / "settings.json"
    existing: dict = {}
    if target.is_file():
        try:
            existing = json.loads(target.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 - 坏文件不覆盖（只尝试写安全字段）
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
    scrubbed = bool(existing.pop("api_key", None))
    existing["base_url"] = base_url or "https://api.deepseek.com"
    existing.setdefault("output_path", "")
    try:
        target.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"写巨人配置失败：{exc}"}
    return {"ok": True, "path": str(target), "scrubbed_api_key": scrubbed,
            "note": "安全：不写 API Key"}


def _normalize(body: dict) -> dict:
    """把请求体规整成 `{"prompt": {...}}`，只保留白名单字段。"""
    prompt: dict = {}
    for key in ALLOWED_FIELDS:
        if key not in body:
            continue
        value = body[key]
        if value is None:
            continue
        text = str(value).strip()
        if key == "api_key" and not text:
            # 空密钥不写：避免把已有密钥清成空串（清空请用 clear_api_key）
            continue
        prompt[key] = text
    return {"prompt": prompt}


# ---------------------------------------------------------------------------
# aiohttp 处理器（供 routes.py 注册）
# ---------------------------------------------------------------------------


async def handle_get(request):  # noqa: ARG001 - aiohttp 签名固定
    """GET `/bettercomfyui/settings` —— 读配置（密钥已遮）。"""
    from aiohttp import web

    return web.json_response(settings_snapshot(), dumps=_dumps)


async def handle_save(request):
    """POST `/bettercomfyui/settings` —— 保存配置到 config/user.yaml。

    ROUND58 安全：本机/同源保护（与 routes._panel_sensitive_guard 等价，避免循环导入）；
    远程使用必须显式 BETTERCOMFYUI_ALLOW_REMOTE_PANEL=1。
    """
    from aiohttp import web
    import os as _os
    from urllib.parse import urlparse as _urlparse

    if str(_os.environ.get("BETTERCOMFYUI_ALLOW_REMOTE_PANEL") or "").lower() not in ("1", "true", "yes"):
        remote = str(getattr(request, "remote", "") or "").strip().split("%")[0]
        if remote and remote not in ("127.0.0.1", "::1", "localhost"):
            return web.json_response(
                {"ok": False, "error": "为安全起见，/settings 写入只允许本机访问；"
                                       "远程请用带认证的反向代理或显式 BETTERCOMFYUI_ALLOW_REMOTE_PANEL=1"},
                status=403, dumps=_dumps)
        origin = str(request.headers.get("Origin") or request.headers.get("Referer") or "").strip()
        host = str(request.headers.get("Host") or "").strip()
        host_name = host.lower()
        if host_name.startswith("["):
            host_name = host_name[1:].split("]", 1)[0]
        elif host_name.count(":") == 1:
            host_name = host_name.rsplit(":", 1)[0]
        if host and host_name not in ("127.0.0.1", "localhost", "::1"):
            return web.json_response(
                {"ok": False, "error": "拒绝非本机 Host 的 /settings 请求（DNS rebinding 防护）"},
                status=403, dumps=_dumps)
        if origin:
            origin_host = (_urlparse(origin).hostname or "").lower()
            if origin_host not in ("127.0.0.1", "localhost", "::1"):
                return web.json_response(
                    {"ok": False, "error": "跨站请求被拒绝（Origin 非本机）"},
                    status=403, dumps=_dumps)
            if host and _urlparse(origin).netloc and _urlparse(origin).netloc != host:
                return web.json_response(
                    {"ok": False, "error": "跨站请求被拒绝（Origin/Host 不一致）"},
                    status=403, dumps=_dumps)

    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        return web.json_response(
            {"ok": False, "error": f"请求体不是合法 JSON：{exc}"}, status=400,
            dumps=_dumps)
    if not isinstance(body, dict):
        return web.json_response(
            {"ok": False, "error": "请求体必须是 JSON 对象"}, status=400,
            dumps=_dumps)

    normalized = _normalize(body)
    if not normalized["prompt"]:
        return web.json_response(
            {"ok": False, "error": "没有可保存的字段"}, status=400, dumps=_dumps)
    env_name = str(normalized["prompt"].get("api_key_env") or "").strip()
    if env_name and env_name not in ("BETTERCOMFYUI_API_KEY", "DEEPSEEK_API_KEY"):
        return web.json_response(
            {"ok": False, "error": "api_key_env 只允许 BETTERCOMFYUI_API_KEY / DEEPSEEK_API_KEY"},
            status=400, dumps=_dumps)

    result = save_user_settings(normalized)
    if not result.get("ok"):
        return web.json_response(result, status=500, dumps=_dumps)

    snapshot = settings_snapshot()
    snapshot["saved"] = result
    return web.json_response(snapshot, dumps=_dumps)


def _dumps(obj, **_kwargs) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)
