"""``prompt/gateway.py`` 测试：配置校验、三种 provider、可注入传输层（离线）。

红线：
* 未配置必要字段 -> ``GatewayError``，且错误信息**包含缺失的键名**；
* **绝不静默回退到 mock**；
* 测试**不得真发网络请求** —— 全部走 FakeTransport，并把 ``urlopen`` 打桩成爆炸。
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys
import urllib.request

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pytest

from _prompt_testutil import min_gateway_config
from bettercomfyui.models.enums import ErrorClass
from bettercomfyui.prompt.errors import PromptError
from bettercomfyui.prompt.gateway import (
    CustomGateway,
    ExternalApiGateway,
    GatewayError,
    LocalQwenGateway,
    PromptModelGateway,
    TransportError,
    build_gateway,
)

GATEWAY_SRC = (_ROOT / "bettercomfyui" / "prompt" / "gateway.py").read_text(encoding="utf-8")


class FakeTransport:
    """记录调用并按脚本返回/抛错的传输层。"""

    def __init__(self, responses=None, errors=None):
        self.responses = list(responses or [])
        self.errors = list(errors or [])
        self.calls: list[dict] = []

    def post_json(self, *, url, headers, payload, timeout_s):
        self.calls.append(
            {"url": url, "headers": dict(headers), "payload": payload, "timeout_s": timeout_s}
        )
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        if not self.responses:
            raise AssertionError("FakeTransport 没有配置响应")
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    def _boom(*args, **kwargs):  # pragma: no cover - 只有真的发请求才会走到
        raise AssertionError("测试中禁止真实网络请求（必须注入 FakeTransport）")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)


def _openai_response(text: str = "你好"):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


# ---------------------------------------------------------------------------
# 配置校验：缺失字段必须报错并写出键名
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cfg,missing",
    [
        ({}, "prompt.model_provider"),
        ({"prompt": {}}, "prompt.model_provider"),
        ({"prompt": {"model_provider": ""}}, "prompt.model_provider"),
        ({"prompt": {"model_provider": "external_api"}}, "prompt.endpoint"),
        (
            {"prompt": {"model_provider": "external_api", "api_base": "http://x/v1"}},
            "prompt.model_name",
        ),
        (
            {
                "prompt": {
                    "model_provider": "external_api",
                    "api_base": "http://x/v1",
                    "model_name": "m",
                }
            },
            "prompt.api_key",
        ),
        (
            {"prompt": {"model_provider": "local_qwen", "api_base": "http://x/v1"}},
            "prompt.model_name",
        ),
        (
            {"prompt": {"model_provider": "local_qwen", "api_base": "http://x/v1", "model_name": "m"}},
            None,
        ),
        (
            {"prompt": {"model_provider": "custom", "api_base": "http://x/v1", "model_name": "m"}},
            "prompt.custom_response_path",
        ),
    ],
)
def test_missing_config_raises_with_key_names(cfg, missing):
    if missing is None:
        build_gateway(cfg, transport=FakeTransport([_openai_response()]))
        return
    with pytest.raises(GatewayError) as excinfo:
        build_gateway(cfg, transport=FakeTransport([_openai_response()]))
    error = excinfo.value
    assert missing in str(error), f"错误信息必须包含缺失键名 {missing}：{error}"
    assert missing in error.missing_keys
    assert error.error_class is ErrorClass.INPUT_VALIDATION


def test_custom_response_path_alias_is_mentioned():
    """真实 Config 用扁平键 prompt.custom_response_path；dict 配置常用点号别名。

    回归：只认一种拼写会让人「明明配了却报缺失」——两种拼写都要能读，
    错误信息里也要同时给出可操作的两个键名。
    """
    flat = {
        "prompt": {
            "model_provider": "custom",
            "endpoint": "http://x/v1",
            "custom_response_path": "text",
        }
    }
    assert build_gateway(flat, transport=FakeTransport([{"text": "ok"}])).complete(
        system="s", user="u"
    ) == "ok"

    nested = {
        "prompt": {
            "model_provider": "custom",
            "endpoint": "http://x/v1",
            "custom": {"response_path": "text"},
        }
    }
    assert build_gateway(nested, transport=FakeTransport([{"text": "ok"}])).complete(
        system="s", user="u"
    ) == "ok"

    with pytest.raises(GatewayError) as excinfo:
        build_gateway({"prompt": {"model_provider": "custom", "endpoint": "http://x/v1"}})
    message = str(excinfo.value)
    assert "prompt.custom_response_path" in message
    assert "prompt.custom.response_path" in message, "别名也要写出来，避免用户改错地方"


def test_gateway_error_carries_error_class_and_is_prompt_error():
    with pytest.raises(GatewayError) as excinfo:
        build_gateway({"prompt": {"model_provider": "external_api"}})
    error = excinfo.value
    assert isinstance(error, PromptError)
    assert error.error_class in set(ErrorClass)
    assert error.missing_keys


def test_missing_config_reports_all_missing_keys_at_once():
    """一次列出全部缺失键，避免「改一个报一个」。"""
    with pytest.raises(GatewayError) as excinfo:
        build_gateway({"prompt": {"model_provider": "external_api"}})
    assert set(excinfo.value.missing_keys) == {
        "prompt.endpoint",
        "prompt.model_name",
        "prompt.api_key",
    }


def test_endpoint_alias_api_base_is_accepted():
    """仓库内置 config/default.yaml 用 prompt.endpoint；api_base 作为兼容别名。"""
    cfg = {"prompt": {"model_provider": "external_api", "api_base": "http://x/v1", "model_name": "m", "api_key": "k"}}
    transport = FakeTransport([_openai_response("ok")])
    gateway = build_gateway(cfg, transport=transport)
    assert gateway.complete(system="s", user="u") == "ok"
    assert transport.calls[0]["url"] == "http://x/v1/chat/completions"


def test_unknown_provider_is_rejected_with_allowed_values():
    with pytest.raises(GatewayError) as excinfo:
        build_gateway({"prompt": {"model_provider": "gemini"}})
    message = str(excinfo.value)
    assert "external_api" in message and "local_qwen" in message and "custom" in message


def test_provider_aliases_are_accepted():
    cfg = min_gateway_config("DEEPSEEK", model_name="deepseek-chat", api_key="k")
    gateway = build_gateway(cfg, transport=FakeTransport([_openai_response()]))
    assert isinstance(gateway, ExternalApiGateway)
    assert gateway.provider == "external_api"


def test_non_string_provider_is_rejected():
    with pytest.raises(GatewayError):
        build_gateway({"prompt": {"model_provider": 123}})


def test_api_key_env_expansion(monkeypatch):
    monkeypatch.setenv("BCUI_TEST_KEY", "sk-from-env")
    cfg = min_gateway_config(api_key="${ENV:BCUI_TEST_KEY}")
    transport = FakeTransport([_openai_response()])
    gateway = build_gateway(cfg, transport=transport)
    gateway.complete(system="s", user="u")
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer sk-from-env"


def test_api_key_env_missing_variable_is_actionable(monkeypatch):
    monkeypatch.delenv("BCUI_ABSENT_KEY", raising=False)
    cfg = min_gateway_config(api_key="${ENV:BCUI_ABSENT_KEY}")
    with pytest.raises(GatewayError) as excinfo:
        build_gateway(cfg, transport=FakeTransport([_openai_response()]))
    assert "BCUI_ABSENT_KEY" in str(excinfo.value)
    assert "prompt.api_key" in excinfo.value.missing_keys


def test_api_key_env_key_name_style(monkeypatch):
    monkeypatch.setenv("BCUI_TEST_KEY2", "sk-env2")
    cfg = min_gateway_config(api_key=None, api_key_env="BCUI_TEST_KEY2")
    cfg["prompt"].pop("api_key")
    transport = FakeTransport([_openai_response()])
    build_gateway(cfg, transport=transport).complete(system="s", user="u")
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer sk-env2"


# ---------------------------------------------------------------------------
# 正常调用（全部离线）
# ---------------------------------------------------------------------------


def test_external_api_happy_path_payload_and_parsing():
    transport = FakeTransport([_openai_response("拆解结果")])
    gateway = build_gateway(min_gateway_config(), transport=transport, sleep=lambda _s: None)

    text = gateway.complete(system="系统", user="用户", temperature=0.3, max_tokens=100, bogus=1)

    assert text == "拆解结果"
    call = transport.calls[0]
    assert call["url"] == "http://127.0.0.1:9999/v1/chat/completions"
    assert call["headers"]["Content-Type"].startswith("application/json")
    assert call["headers"]["Authorization"] == "Bearer test-key"
    assert call["payload"]["model"] == "test-model"
    assert call["payload"]["messages"][0] == {"role": "system", "content": "系统"}
    assert call["payload"]["messages"][1] == {"role": "user", "content": "用户"}
    assert call["payload"]["temperature"] == 0.3
    assert call["payload"]["max_tokens"] == 100
    assert call["payload"]["stream"] is False
    assert "bogus" not in call["payload"], "未知参数不得泄漏到请求体"


def test_gateway_satisfies_protocol():
    gateway = build_gateway(min_gateway_config(), transport=FakeTransport([_openai_response()]))
    assert isinstance(gateway, PromptModelGateway)


def test_chat_completions_suffix_not_duplicated():
    cfg = min_gateway_config(api_base="http://127.0.0.1:9999/v1/chat/completions")
    transport = FakeTransport([_openai_response()])
    build_gateway(cfg, transport=transport).complete(system="s", user="u")
    assert transport.calls[0]["url"] == "http://127.0.0.1:9999/v1/chat/completions"


def test_local_qwen_needs_no_api_key_and_simplified_call():
    cfg = {"prompt": {"model_provider": "local_qwen", "api_base": "http://127.0.0.1:8001/v1", "model_name": "qwen"}}
    transport = FakeTransport([_openai_response("本地结果")])
    gateway = build_gateway(cfg, transport=transport)
    assert isinstance(gateway, LocalQwenGateway)
    assert gateway.complete(system="s", user="u") == "本地结果"
    assert "Authorization" not in transport.calls[0]["headers"]
    assert gateway.describe()["authenticated"] is False


def test_custom_gateway_template_and_response_path():
    cfg = {
        "prompt": {
            "model_provider": "custom",
            "api_base": "http://127.0.0.1:9999/generate",
            "model_name": "my-model",
            "custom": {
                "request_template": {
                    "prompt": "{system}\n{user}",
                    "meta": {"model": "{model}", "temperature": "{temperature}"},
                },
                "response_path": "data.items.0.text",
            },
        }
    }
    transport = FakeTransport([{"data": {"items": [{"text": "自定义结果"}]}}])
    gateway = build_gateway(cfg, transport=transport)
    assert isinstance(gateway, CustomGateway)

    text = gateway.complete(system="S", user="U", temperature=0.7)

    assert text == "自定义结果"
    assert transport.calls[0]["payload"]["prompt"] == "S\nU"
    assert transport.calls[0]["payload"]["meta"] == {"model": "my-model", "temperature": "0.7"}


def test_custom_gateway_unknown_placeholder_is_actionable():
    cfg = {
        "prompt": {
            "model_provider": "custom",
            "api_base": "http://127.0.0.1:9999/generate",
            "model_name": "m",
            "custom": {
                "request_template": {"prompt": "{nope}"},
                "response_path": "text",
            },
        }
    }
    gateway = build_gateway(cfg, transport=FakeTransport([{"text": "x"}]))
    with pytest.raises(GatewayError) as excinfo:
        gateway.complete(system="s", user="u")
    assert "nope" in str(excinfo.value)
    assert "system" in str(excinfo.value)


def test_custom_gateway_missing_response_path_value():
    cfg = {
        "prompt": {
            "model_provider": "custom",
            "api_base": "http://127.0.0.1:9999/generate",
            "model_name": "m",
            "custom": {"request_template": {"p": "{user}"}, "response_path": "a.b.c"},
        }
    }
    gateway = build_gateway(cfg, transport=FakeTransport([{"a": {"b": {}}}]))
    with pytest.raises(GatewayError) as excinfo:
        gateway.complete(system="s", user="u")
    assert "a.b.c" in str(excinfo.value)
    assert excinfo.value.error_class is ErrorClass.MODEL_API


def test_custom_gateway_response_template():
    cfg = {
        "prompt": {
            "model_provider": "custom",
            "api_base": "http://127.0.0.1:9999/generate",
            "model_name": "m",
            "custom": {
                "request_template": {"p": "{user}"},
                "response_path": "text",
                "response_template": ">>{text}<<",
            },
        }
    }
    gateway = build_gateway(cfg, transport=FakeTransport([{"text": "内容"}]))
    assert gateway.complete(system="s", user="u") == ">>内容<<"


# ---------------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------------


def test_transport_failure_retries_then_raises_model_api():
    transport = FakeTransport(errors=[TransportError("连接被重置"), TransportError("连接被重置"), TransportError("连接被重置")])
    cfg = min_gateway_config(max_retries=2)
    gateway = build_gateway(cfg, transport=transport, sleep=lambda _s: None)

    with pytest.raises(GatewayError) as excinfo:
        gateway.complete(system="s", user="u")

    assert len(transport.calls) == 3, "max_retries=2 -> 共 3 次尝试"
    assert excinfo.value.error_class is ErrorClass.MODEL_API


def test_transport_recovers_after_retry():
    transport = FakeTransport(
        responses=[_openai_response("第二次成功")],
        errors=[TransportError("第一次超时")],
    )
    gateway = build_gateway(min_gateway_config(max_retries=2), transport=transport, sleep=lambda _s: None)
    assert gateway.complete(system="s", user="u") == "第二次成功"
    assert len(transport.calls) == 2


def test_auth_failure_is_not_retried():
    transport = FakeTransport(errors=[TransportError("HTTP 401 Unauthorized", status=401)])
    gateway = build_gateway(min_gateway_config(max_retries=3), transport=transport, sleep=lambda _s: None)

    with pytest.raises(GatewayError) as excinfo:
        gateway.complete(system="s", user="u")

    assert len(transport.calls) == 1, "认证失败重试无意义"
    assert excinfo.value.error_class is ErrorClass.MODEL_API
    assert "认证失败" in str(excinfo.value)


def test_bad_request_is_not_retried():
    transport = FakeTransport(errors=[TransportError("HTTP 400 bad request", status=400)])
    gateway = build_gateway(min_gateway_config(max_retries=3), transport=transport, sleep=lambda _s: None)
    with pytest.raises(GatewayError):
        gateway.complete(system="s", user="u")
    assert len(transport.calls) == 1


def test_empty_model_content_is_an_error_not_a_success():
    transport = FakeTransport([{"choices": [{"message": {"content": "   "}}]}])
    gateway = build_gateway(min_gateway_config(), transport=transport)
    with pytest.raises(GatewayError) as excinfo:
        gateway.complete(system="s", user="u")
    assert "空内容" in str(excinfo.value)
    assert excinfo.value.error_class is ErrorClass.MODEL_API


def test_unexpected_response_shape_is_an_error():
    transport = FakeTransport([{"unexpected": True}])
    gateway = build_gateway(min_gateway_config(), transport=transport)
    with pytest.raises(GatewayError):
        gateway.complete(system="s", user="u")


def test_non_string_prompt_is_rejected():
    gateway = build_gateway(min_gateway_config(), transport=FakeTransport([_openai_response()]))
    with pytest.raises(GatewayError):
        gateway.complete(system=None, user="u")  # type: ignore[arg-type]


def test_describe_and_errors_never_leak_api_key():
    cfg = min_gateway_config(api_key="sk-super-secret")
    transport = FakeTransport(errors=[TransportError("网络故障")] * 3)
    gateway = build_gateway(cfg, transport=transport, sleep=lambda _s: None)

    assert "sk-super-secret" not in json.dumps(gateway.describe(), ensure_ascii=False)
    with pytest.raises(GatewayError) as excinfo:
        gateway.complete(system="s", user="u")
    assert "sk-super-secret" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# 「绝不静默回退到 mock」
# ---------------------------------------------------------------------------


def test_no_mock_gateway_exists_in_production_code():
    tree = ast.parse(GATEWAY_SRC)
    class_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    assert not any("mock" in name.lower() for name in class_names), class_names
    lowered = GATEWAY_SRC.lower()
    assert "fallback_to_mock" not in lowered
    assert "mockgateway" not in lowered


def test_config_object_and_nested_dict_both_supported():
    class PromptParams:
        model_provider = "local_qwen"
        api_base = "http://127.0.0.1:8001/v1"
        model_name = "qwen"

    class Config:
        prompt = PromptParams()

    transport = FakeTransport([_openai_response("ok")])
    gateway = build_gateway(Config(), transport=transport)
    assert gateway.complete(system="s", user="u") == "ok"

    flat = {"prompt.model_provider": "local_qwen", "prompt.api_base": "http://127.0.0.1:8001/v1", "prompt.model_name": "qwen"}
    assert build_gateway(flat, transport=FakeTransport([_openai_response("ok2")])).complete(
        system="s", user="u"
    ) == "ok2"
