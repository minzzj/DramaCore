"""提示词模型网关（CONTRACTS §5）。

支持三种 provider（``cfg.prompt.model_provider``）：

=============== ==========================================================
``external_api``  外部大模型，OpenAI 兼容 ``POST {api_base}/chat/completions``
``local_qwen``    本地千问/本地 OpenAI 兼容服务（llama.cpp / vLLM / Ollama）
``custom``        开发者自配接口：自定义请求模板 + 响应取值路径
=============== ==========================================================

硬约束：

* **未配置必要字段必须抛 :class:`GatewayError`，且错误信息里写出缺失的键名**
  （例如 ``prompt.api_key``），让人一眼知道去改哪里。
* **绝不静默回退到 mock**。这里没有、也不会有「配置缺失就返回假文本」的分支——
  那是假成功。测试用的假网关放在 ``tests/`` 里，不进生产代码。
* **HTTP 传输层可注入**（``transport`` 参数），测试不发真实网络请求。
* 外部调用的任何失败（超时/认证/非 JSON 响应/空内容）都抛
  :class:`GatewayError`，并携带 :class:`~bettercomfyui.models.enums.ErrorClass`。

凭据写法：``prompt.api_key`` 支持 ``"${ENV:DEEPSEEK_API_KEY}"``，
配置样例里因此不必出现真实密钥。
"""

from __future__ import annotations

import json
import string
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from ..models.enums import ErrorClass
from ._conf import cfg_float, cfg_get, cfg_get_any, cfg_int, resolve_env_value
from .errors import PromptError
from .normalize import normalize_name

__all__ = [
    "PromptModelGateway",
    "GatewayError",
    "HttpTransport",
    "TransportError",
    "UrllibTransport",
    "ExternalApiGateway",
    "LocalQwenGateway",
    "CustomGateway",
    "build_gateway",
    "ProviderName",
    "PROVIDER_ALIASES",
    "REQUIRED_KEYS",
    "PARAM_PASSTHROUGH",
]


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class GatewayError(PromptError):
    """接口未配置 / 认证失败 / 超时 / 响应不可用。必须携带 ``ErrorClass``。"""

    def __init__(
        self,
        message: str,
        *,
        error_class: ErrorClass = ErrorClass.INPUT_VALIDATION,
        missing_keys: Sequence[str] | None = None,
        provider: str | None = None,
        url: str | None = None,
    ) -> None:
        self.error_class: ErrorClass = ErrorClass.parse(error_class)
        self.missing_keys: list[str] = list(missing_keys or [])
        self.provider = provider
        self.url = url
        detail = message
        if self.missing_keys:
            detail = f"{detail}（缺失配置键：{', '.join(self.missing_keys)}）"
        if provider:
            detail = f"[provider={provider}] {detail}"
        super().__init__(detail)

    @classmethod
    def missing_config(
        cls,
        provider: str,
        missing_keys: Sequence[str],
        *,
        hint: str = "",
    ) -> "GatewayError":
        keys = ", ".join(missing_keys)
        message = (
            f"模型网关配置不完整：{keys} 未配置。"
            f"请在 config/default.yaml 或项目 config.yaml 的 prompt 段补齐"
        )
        if hint:
            message = f"{message}；{hint}"
        return cls(
            message,
            error_class=ErrorClass.INPUT_VALIDATION,
            missing_keys=list(missing_keys),
            provider=provider,
        )


class TransportError(Exception):
    """传输层错误（连接失败、超时、非 2xx、响应不是 JSON）。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)


# ---------------------------------------------------------------------------
# 传输层
# ---------------------------------------------------------------------------


@runtime_checkable
class HttpTransport(Protocol):
    """HTTP 传输层。实现必须同步、返回已解析的 JSON 对象。"""

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Any,
        timeout_s: float,
    ) -> Any: ...


class UrllibTransport:
    """默认传输层（标准库 urllib，无第三方依赖）。"""

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Any,
        timeout_s: float,
    ) -> Any:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers = {"Content-Type": "application/json; charset=utf-8"}
        request_headers.update({k: v for k, v in headers.items() if v is not None})
        request = urllib.request.Request(
            url, data=body, headers=request_headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:  # HTTPError 是 URLError 的子类，必须先捕获
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:  # pragma: no cover - 防御性
                detail = ""
            raise TransportError(
                f"HTTP {exc.code} {exc.reason}: {detail}",
                status=int(exc.code),
            ) from exc
        except urllib.error.URLError as exc:
            raise TransportError(f"连接失败：{exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise TransportError(f"请求失败：{exc}") from exc
        if not raw.strip():
            raise TransportError("响应体为空")
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise TransportError(f"响应不是合法 JSON：{raw[:200]!r}") from exc


# ---------------------------------------------------------------------------
# 网关协议
# ---------------------------------------------------------------------------


@runtime_checkable
class PromptModelGateway(Protocol):
    """提示词模型网关（CONTRACTS §5）。"""

    provider: str

    def complete(self, *, system: str, user: str, **params) -> str: ...


#: 允许直接透传给模型 API 的参数（其余参数忽略，避免把内部字段泄漏到请求体）。
PARAM_PASSTHROUGH = frozenset(
    {"temperature", "max_tokens", "top_p", "top_k", "stop", "presence_penalty", "frequency_penalty", "seed", "stream"}
)

_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"


def _chat_url(api_base: str) -> str:
    base = api_base.strip().rstrip("/")
    if base.endswith(_CHAT_COMPLETIONS_SUFFIX):
        return base
    return base + _CHAT_COMPLETIONS_SUFFIX


def _dig(obj: Any, path: str) -> Any:
    """按 ``a.0.b[1].c`` 取值；取不到抛 KeyError。"""
    if not path:
        raise KeyError(path)
    normalized = path.replace("[", ".").replace("]", "")
    node = obj
    for part in normalized.split("."):
        if part == "":
            continue
        if isinstance(node, Mapping):
            if part not in node:
                raise KeyError(path)
            node = node[part]
        elif isinstance(node, (list, tuple)):
            if not part.isdigit() or int(part) >= len(node):
                raise KeyError(path)
            node = node[int(part)]
        else:
            raise KeyError(path)
    return node


def _content_to_text(value: Any) -> str:
    """把 ``message.content`` 之类的字段转成文本（兼容多段 content 数组/对象）。

    ROUND58：自配接口的 ``custom_response_path`` 可能指向
    ``{"content": "..."}`` / ``{"text": "..."}`` 这类对象；旧实现只处理 str/list，
    其它类型会被 ``str(dict)`` 成 Python repr（单引号），下游 JSON 解析必然失败。
    这里统一按「文本字段优先」抽取，抽不到返回空串，由调用方给出明确错误。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("content", "text", "message", "value"):
            if key in value:
                text = _content_to_text(value[key])
                if text:
                    return text
        return ""
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, Mapping):
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
                else:
                    nested = _content_to_text(item)
                    if nested:
                        chunks.append(nested)
        return "".join(chunks)
    return ""


class _BaseHttpGateway:
    """共享的请求/重试/取值逻辑。"""

    provider = "unknown"
    default_response_path = "choices.0.message.content"

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str | None,
        model_name: str,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        transport: HttpTransport | None = None,
        extra_headers: Mapping[str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        backoff_s: float = 0.5,
        max_tokens: int | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.model_name = model_name
        self.timeout_s = timeout_s
        self.max_retries = max(0, int(max_retries))
        self.transport: HttpTransport = transport or UrllibTransport()
        self.extra_headers = dict(extra_headers or {})
        self._sleep = sleep
        self.backoff_s = backoff_s
        self.max_tokens = max_tokens
        self._api_key = api_key
        # ROUND59：最近一次响应的可审计元数据（finish_reason / usage）。
        # ``finish_reason="length"`` 表示模型输出被 max_tokens 截断 —— 解析层
        # 据此把「截断后只剩内层数组/片段」诊断为 truncated_json 并触发降级，
        # 而不是误报「顶层不是对象」。这里只存小字段，不缓存响应正文。
        self.last_finish_reason: str | None = None
        self.last_usage: dict | None = None

    def _capture_response_meta(self, response: Any) -> None:
        """记录 finish_reason / usage（取不到就置 None，绝不因此让调用失败）。"""
        self.last_finish_reason = None
        self.last_usage = None
        try:
            finish = _dig(response, "choices.0.finish_reason")
        except KeyError:
            finish = None
        if isinstance(finish, str) and finish.strip():
            self.last_finish_reason = finish.strip().lower()
        try:
            usage = _dig(response, "usage")
        except KeyError:
            usage = None
        if isinstance(usage, Mapping):
            self.last_usage = dict(usage)

    def response_meta(self) -> dict:
        """最近一次响应的元数据（供编排层写进审计；不含密钥/正文）。"""
        return {
            "finish_reason": self.last_finish_reason,
            "usage": dict(self.last_usage) if self.last_usage else None,
        }

    # -- 请求头（绝不外泄密钥：日志只写 provider/url） --------------------
    @property
    def url(self) -> str:
        return _chat_url(self.endpoint)

    def _headers(self) -> dict[str, str]:
        # Content-Type 在网关层显式给出：注入自定义传输层时也必须带上，
        # 否则真实服务端会静默按表单解析导致「调用成功但结果为空」。
        headers: dict[str, str] = {"Content-Type": "application/json; charset=utf-8"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        headers.update(self.extra_headers)
        return headers

    def describe(self) -> dict:
        """可安全写日志的描述（**不含 api_key**）。"""
        return {
            "provider": self.provider,
            "url": self.url,
            "model": self.model_name,
            "timeout_s": self.timeout_s,
            "max_retries": self.max_retries,
            "authenticated": bool(self._api_key),
        }

    # -- 请求体 ---------------------------------------------------------
    def build_payload(self, *, system: str, user: str, params: Mapping[str, Any]) -> Any:
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        for key, value in params.items():
            if key in PARAM_PASSTHROUGH and value is not None:
                payload[key] = value
        return payload

    # -- 响应取值 -------------------------------------------------------
    def extract_text(self, response: Any) -> str:
        try:
            value = _dig(response, self.default_response_path)
        except KeyError:
            return ""
        return _content_to_text(value)

    # -- 调用 -----------------------------------------------------------
    def _post(self, payload: Any) -> Any:
        attempt = 0
        last_error: TransportError | None = None
        while attempt <= self.max_retries:
            if attempt:
                self._sleep(self.backoff_s * attempt)
            attempt += 1
            try:
                return self.transport.post_json(
                    url=self.url,
                    headers=self._headers(),
                    payload=payload,
                    timeout_s=self.timeout_s,
                )
            except TransportError as exc:
                last_error = exc
                if exc.status in (401, 403):
                    raise GatewayError(
                        f"模型接口认证失败（HTTP {exc.status}）：请检查 prompt.api_key 是否有效",
                        error_class=ErrorClass.MODEL_API,
                        provider=self.provider,
                        url=self.url,
                    ) from exc
                if exc.status is not None and 400 <= exc.status < 500:
                    raise GatewayError(
                        f"模型接口拒绝请求（HTTP {exc.status}）：{exc}",
                        error_class=ErrorClass.MODEL_API,
                        provider=self.provider,
                        url=self.url,
                    ) from exc
        raise GatewayError(
            f"模型调用失败（已重试 {self.max_retries} 次）：{last_error}",
            error_class=ErrorClass.MODEL_API,
            provider=self.provider,
            url=self.url,
        )

    def complete(self, *, system: str, user: str, **params) -> str:
        if not isinstance(system, str) or not isinstance(user, str):
            raise GatewayError(
                "complete(system=..., user=...) 必须是字符串",
                error_class=ErrorClass.INPUT_VALIDATION,
                provider=self.provider,
            )
        payload = self.build_payload(system=system, user=user, params=params)
        response = self._post(payload)
        self._capture_response_meta(response)
        text = self.extract_text(response)
        if not text.strip():
            raise GatewayError(
                f"模型返回空内容（响应结构不符合预期或模型未输出）：{str(response)[:300]}",
                error_class=ErrorClass.MODEL_API,
                provider=self.provider,
                url=self.url,
            )
        return text


class ExternalApiGateway(_BaseHttpGateway):
    """外部大模型（OpenAI 兼容 chat completions）。"""

    provider = "external_api"


class LocalQwenGateway(_BaseHttpGateway):
    """本地千问/本地 OpenAI 兼容服务。"""

    provider = "local_qwen"


def _coerce_template_node(node: Any) -> Any:
    """把「JSON 字符串模板」解析成对象，其余模板原样返回。

    ``prompt.custom_request_template`` 在 YAML 里常被写成 dict；但用户也可能写成一整段
    JSON 字符串。后者若直接走 :func:`_render_template`，JSON 自己的 ``{`` 会被当成
    格式占位符而报「未知占位符」。这里先尝试 ``json.loads``：能解析就按对象递归渲染，
    解析失败（例如 ``'{"prompt": "{system}"}'`` 本身是合法 JSON，会成功）才保留原字符串。
    """
    if isinstance(node, str):
        stripped = node.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                return node
    return node


class CustomGateway(_BaseHttpGateway):
    """开发者自配接口：自定义请求模板 + 响应取值路径。"""

    provider = "custom"

    def __init__(
        self,
        *,
        request_template: Any,
        response_path: str,
        response_template: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.request_template = _coerce_template_node(request_template)
        self.response_path = response_path
        self.response_template = (
            _coerce_template_node(response_template) if response_template else None
        )

    def build_payload(self, *, system: str, user: str, params: Mapping[str, Any]) -> Any:
        mapping = {
            "system": system,
            "user": user,
            "model": self.model_name,
            "endpoint": self.endpoint,
            # 自配接口把密钥放在 body（而不是 Header）时可直接引用；
            # 未配置时给空串，避免模板里出现字符串 "None"。
            "api_key": self._api_key or "",
            "max_tokens": self.max_tokens if self.max_tokens is not None else "",
        }
        mapping.update({k: v for k, v in params.items() if k in PARAM_PASSTHROUGH})
        return _render_template(self.request_template, mapping, provider=self.provider)

    def extract_text(self, response: Any) -> str:
        try:
            value = _dig(response, self.response_path)
        except KeyError as exc:
            raise GatewayError(
                f"响应中取不到 prompt.custom_response_path={self.response_path!r} 指向的值；"
                "请检查该路径（例如 choices.0.message.content / data.text）",
                error_class=ErrorClass.MODEL_API,
                provider=self.provider,
                url=self.url,
            ) from exc
        text = _content_to_text(value)
        if not text.strip():
            raise GatewayError(
                f"prompt.custom_response_path={self.response_path!r} 指向的值不是可用文本"
                f"（收到 {type(value).__name__}，内容片段 {str(value)[:200]!r}）；"
                "请把路径指向实际存放模型文本的字段",
                error_class=ErrorClass.MODEL_API,
                provider=self.provider,
                url=self.url,
            )
        if self.response_template:
            rendered = _render_template(
                self.response_template, {"text": text}, provider=self.provider
            )
            if isinstance(rendered, str):
                return rendered
            return json.dumps(rendered, ensure_ascii=False)
        return text


class _MissingPlaceholder(dict):
    def __missing__(self, key: str) -> str:  # pragma: no cover - 由 _render_template 捕获
        raise KeyError(key)


def _render_template(node: Any, mapping: Mapping[str, Any], *, provider: str) -> Any:
    if isinstance(node, str):
        try:
            return string.Formatter().vformat(node, (), _MissingPlaceholder(mapping))
        except KeyError as exc:
            raise GatewayError(
                f"请求/响应模板引用了未知占位符 {exc.args[0]!r}；"
                f"可用占位符：{', '.join(sorted(mapping))}",
                error_class=ErrorClass.INPUT_VALIDATION,
                provider=provider,
            ) from exc
        except (IndexError, ValueError) as exc:
            raise GatewayError(
                f"模板格式错误：{exc}",
                error_class=ErrorClass.INPUT_VALIDATION,
                provider=provider,
            ) from exc
    if isinstance(node, Mapping):
        return {k: _render_template(v, mapping, provider=provider) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [_render_template(v, mapping, provider=provider) for v in node]
    return node


# ---------------------------------------------------------------------------
# 构造入口
# ---------------------------------------------------------------------------

_PROVIDER_ALIASES: dict[str, str] = {
    "externalapi": "external_api",
    "external": "external_api",
    "api": "external_api",
    "openai": "external_api",
    "deepseek": "external_api",
    "remote": "external_api",
    "localqwen": "local_qwen",
    "qwen": "local_qwen",
    "local": "local_qwen",
    "ollama": "local_qwen",
    "llamacpp": "local_qwen",
    "vllm": "local_qwen",
    "custom": "custom",
    "compatible": "custom",
    "自配": "custom",
}

#: 每个 provider 的必填配置键（用于 GatewayError 的可操作提示）。
#: 键名与仓库内置 ``config/default.yaml`` / ``models.entities.PromptParams`` 对齐；
#: ``prompt.api_base`` / ``prompt.base_url`` 是 ``prompt.endpoint`` 的兼容别名。
REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "external_api": ("prompt.endpoint", "prompt.model_name", "prompt.api_key"),
    "local_qwen": ("prompt.endpoint", "prompt.model_name"),
    "custom": ("prompt.endpoint", "prompt.custom_response_path"),
}

#: 端点候选键（按优先级）。
ENDPOINT_KEYS: tuple[str, ...] = ("prompt.endpoint", "prompt.api_base", "prompt.base_url")
MODEL_KEYS: tuple[str, ...] = ("prompt.model_name", "prompt.model")

#: custom 段候选键：**扁平名优先**（models.PromptParams / config/default.yaml 的真实键名），
#: 点号嵌套名作为别名（供 dict 形态的配置使用）。两套都读不到才算缺失。
CUSTOM_RESPONSE_PATH_KEYS: tuple[str, ...] = (
    "prompt.custom_response_path",
    "prompt.custom.response_path",
)
CUSTOM_REQUEST_TEMPLATE_KEYS: tuple[str, ...] = (
    "prompt.custom_request_template",
    "prompt.custom.request_template",
)
CUSTOM_RESPONSE_TEMPLATE_KEYS: tuple[str, ...] = (
    "prompt.custom_response_template",
    "prompt.custom.response_template",
)

_KEY_ALIAS_HINT = "prompt.endpoint 的别名：prompt.api_base / prompt.base_url"

#: 对外暴露的 provider 名（``ProviderName`` 便于调用方做常量引用）。
class ProviderName:
    EXTERNAL_API = "external_api"
    LOCAL_QWEN = "local_qwen"
    CUSTOM = "custom"

    ALL = ("external_api", "local_qwen", "custom")


def _resolve_provider(raw: Any) -> str:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise GatewayError.missing_config(
            "unknown",
            ["prompt.model_provider"],
            hint=f"允许值：{' / '.join(ProviderName.ALL)}",
        )
    if not isinstance(raw, str):
        raise GatewayError(
            f"prompt.model_provider 必须是字符串，收到 {type(raw).__name__}",
            error_class=ErrorClass.INPUT_VALIDATION,
            missing_keys=["prompt.model_provider"],
        )
    key = normalize_name(raw).replace(" ", "").replace("_", "").replace("-", "")
    provider = _PROVIDER_ALIASES.get(key)
    if provider is None:
        raise GatewayError(
            f"未知的 prompt.model_provider={raw!r}；允许值：{' / '.join(ProviderName.ALL)}",
            error_class=ErrorClass.INPUT_VALIDATION,
            missing_keys=["prompt.model_provider"],
        )
    return provider


def _read_api_key(cfg: Any, *, required: bool, provider: str) -> str | None:
    raw = cfg_get(cfg, "prompt.api_key", None)
    env_name = cfg_get(cfg, "prompt.api_key_env", None)
    if raw is None and env_name:
        # 只配了 api_key_env：按环境变量名解析，同样支持 ${ENV:NAME}
        raw = "${ENV:" + str(env_name).strip() + "}"
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if required:
            raise GatewayError.missing_config(provider, ["prompt.api_key"])
        return None
    try:
        value = resolve_env_value(raw)
    except KeyError as exc:
        env_name = exc.args[0]
        raise GatewayError(
            f"prompt.api_key 指向的环境变量 {env_name} 不存在；"
            f"请设置该环境变量，或直接把密钥写入 prompt.api_key",
            error_class=ErrorClass.INPUT_VALIDATION,
            missing_keys=["prompt.api_key"],
            provider=provider,
        ) from exc
    text = str(value).strip()
    if not text:
        if required:
            raise GatewayError.missing_config(provider, ["prompt.api_key"])
        return None
    return text


def _api_key_configured(cfg: Any) -> bool:
    """是否配置了 api_key（字面值或 api_key_env，后者只要求名字存在）。"""
    raw = cfg_get(cfg, "prompt.api_key", None)
    if isinstance(raw, str) and raw.strip():
        return True
    env_name = cfg_get(cfg, "prompt.api_key_env", None)
    return bool(isinstance(env_name, str) and env_name.strip())


def _common_kwargs(
    cfg: Any,
    *,
    provider: str,
    transport: HttpTransport | None,
    sleep: Callable[[float], None] | None,
) -> dict:
    endpoint = cfg_get_any(cfg, ENDPOINT_KEYS)
    if endpoint is None:
        raise GatewayError.missing_config(
            provider,
            ["prompt.endpoint"],
            hint=f"例如 https://api.deepseek.com/v1 或 http://127.0.0.1:8001/v1；{_KEY_ALIAS_HINT}",
        )
    model_name = cfg_get_any(cfg, MODEL_KEYS)
    required = REQUIRED_KEYS.get(provider, ())
    if model_name is None and "prompt.model_name" in required:
        raise GatewayError.missing_config(provider, ["prompt.model_name"])
    headers = cfg_get(cfg, "prompt.extra_headers", None)
    if headers is not None and not isinstance(headers, Mapping):
        raise GatewayError(
            "prompt.extra_headers 必须是键值对映射",
            error_class=ErrorClass.INPUT_VALIDATION,
            missing_keys=["prompt.extra_headers"],
            provider=provider,
        )
    kwargs: dict[str, Any] = {
        "endpoint": str(endpoint).strip(),
        "model_name": str(model_name).strip() if model_name else "",
        "timeout_s": cfg_float(cfg, "prompt.timeout_s", 60.0),
        "max_retries": cfg_int(cfg, "prompt.max_retries", 2),
        "extra_headers": dict(headers or {}),
        "backoff_s": cfg_float(cfg, "prompt.retry_backoff_s", 0.5),
    }
    if transport is not None:
        kwargs["transport"] = transport
    if sleep is not None:
        kwargs["sleep"] = sleep
    max_tokens = cfg_get(cfg, "prompt.max_tokens", None)
    if max_tokens is not None:
        try:
            kwargs["max_tokens"] = int(max_tokens)
        except (TypeError, ValueError):
            raise GatewayError(
                f"prompt.max_tokens 必须是整数，收到 {max_tokens!r}",
                error_class=ErrorClass.INPUT_VALIDATION,
                missing_keys=["prompt.max_tokens"],
                provider=provider,
            ) from None
    return kwargs


def build_gateway(
    cfg: Any,
    *,
    transport: HttpTransport | None = None,
    sleep: Callable[[float], None] | None = None,
) -> PromptModelGateway:
    """按 ``cfg.prompt.model_provider`` 构造网关。

    :param transport: 可注入的 HTTP 传输层（测试必须注入，避免真实网络请求）。
    :param sleep: 可注入的退避函数（测试传 ``lambda _: None``）。

    配置缺失 -> :class:`GatewayError`（消息含缺失键名）。
    **绝不返回 mock 网关**：没有配置就没有网关，必须显式失败。
    """
    provider = _resolve_provider(cfg_get(cfg, "prompt.model_provider", None))

    if provider == ProviderName.EXTERNAL_API:
        missing = _missing_common(cfg, "prompt.model_name")
        if not _api_key_configured(cfg):
            missing.append("prompt.api_key")
        if missing:
            raise GatewayError.missing_config(provider, missing, hint=_KEY_ALIAS_HINT)
        api_key = _read_api_key(cfg, required=True, provider=provider)
        kwargs = _common_kwargs(cfg, provider=provider, transport=transport, sleep=sleep)
        return ExternalApiGateway(api_key=api_key, **kwargs)

    if provider == ProviderName.LOCAL_QWEN:
        missing = _missing_common(cfg, "prompt.model_name")
        if missing:
            raise GatewayError.missing_config(
                provider, missing, hint=f"本地服务不需要 api_key；{_KEY_ALIAS_HINT}"
            )
        api_key = _read_api_key(cfg, required=False, provider=provider)
        kwargs = _common_kwargs(cfg, provider=provider, transport=transport, sleep=sleep)
        return LocalQwenGateway(api_key=api_key, **kwargs)

    # custom
    missing = _missing_common(cfg, None)
    response_path = cfg_get_any(cfg, CUSTOM_RESPONSE_PATH_KEYS)
    if response_path is None:
        missing.append("prompt.custom_response_path")
    if missing:
        raise GatewayError.missing_config(
            provider,
            missing,
            hint=(
                "custom 需要 endpoint + response_path（没有 response_path 无法从响应取值）；"
                "响应路径键名：prompt.custom_response_path（别名 prompt.custom.response_path）；"
                f"{_KEY_ALIAS_HINT}"
            ),
        )
    request_template = cfg_get_any(cfg, CUSTOM_REQUEST_TEMPLATE_KEYS)
    if request_template is None or request_template == {}:
        # 空模板（default.yaml 里就是空 dict）视为「未配置」-> 用 OpenAI 兼容默认模板
        request_template = {
            "model": "{model}",
            "messages": [
                {"role": "system", "content": "{system}"},
                {"role": "user", "content": "{user}"},
            ],
        }
    if not isinstance(request_template, (Mapping, str)):
        raise GatewayError(
            "prompt.custom_request_template 必须是对象或字符串模板",
            error_class=ErrorClass.INPUT_VALIDATION,
            missing_keys=["prompt.custom_request_template"],
            provider=provider,
        )
    response_template = cfg_get_any(cfg, CUSTOM_RESPONSE_TEMPLATE_KEYS)
    api_key = _read_api_key(cfg, required=False, provider=provider)
    kwargs = _common_kwargs(cfg, provider=provider, transport=transport, sleep=sleep)
    return CustomGateway(
        api_key=api_key,
        request_template=request_template,
        response_path=str(response_path),
        response_template=str(response_template) if response_template else None,
        **kwargs,
    )


def _missing_common(cfg: Any, model_key: str | None) -> list[str]:
    """检查 common 必填键（endpoint + 可选 model_name），返回缺失键名列表。"""
    missing: list[str] = []
    if cfg_get_any(cfg, ENDPOINT_KEYS) is None:
        missing.append("prompt.endpoint")
    if model_key and cfg_get_any(cfg, MODEL_KEYS) is None:
        missing.append(model_key)
    return missing
