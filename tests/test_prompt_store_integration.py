"""prompt 层 × **真实** ``bettercomfyui.store`` 的集成测试。

为什么必须有这个文件：``tests/_fake_store.py`` 只是契约替身，通过它只能证明
替身自洽。Lead 的硬要求是「至少一条走真实 store 的集成用例」，本文件就是那条。

同时校验替身与真实 store 的一致性（``index_key`` 语义），避免替身漂移导致
单元测试给出错误的安全感。
"""

from __future__ import annotations

import hashlib
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pytest

from _fake_store import index_key as fake_index_key
from bettercomfyui.models.enums import MatchOutcome
from bettercomfyui.prompt._conf import cfg_get
from bettercomfyui.prompt.match import build_suspect_queue, match_candidate

PROJECT_ID = "c" * 32


def _store_module():
    try:
        from bettercomfyui.store import assets as store_assets
    except Exception as exc:  # pragma: no cover - store 未就绪时
        pytest.skip(f"真实 store 尚不可用，无法做集成验证：{exc!r}")
    return store_assets


def _project_dir(tmp_path: pathlib.Path, project_id: str = PROJECT_ID) -> pathlib.Path:
    path = tmp_path / "projects" / project_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _real_store(tmp_path: pathlib.Path, project_id: str = PROJECT_ID):
    return _store_module().AssetStore(_project_dir(tmp_path, project_id))


def _write_source_file(store, name: str) -> pathlib.Path:
    """造一个素材源文件（add_version 会复制它；源文件在项目内、但不在版本目录里）。"""
    tmp_dir = store.paths.project_dir / "_incoming"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]
    path = tmp_dir / f"{digest}.png"
    path.write_bytes(f"fake-image:{name}".encode("utf-8"))
    return path


def _seed(store, name: str, *, asset_type: str = "CHARACTER", scope: str = "PROJECT", lock: bool = True):
    asset = store.create_asset(asset_type=asset_type, name=name, source="AI", library_scope=scope)
    version = store.add_version(
        asset["asset_id"], source="AI", file_path=_write_source_file(store, name), status="DRAFT"
    )
    if lock:
        store.set_lock(
            asset["asset_id"],
            version["version"],
            lock_source="MODEL_PASS",
            content_hash=version["content_hash"],
            review_cycle_id="REVIEW:integration:v1",
        )
    return store.get_asset(asset["asset_id"]), version


# ---------------------------------------------------------------------------


def test_fake_store_index_key_matches_real_store():
    """替身与真实 store 的索引归一化必须一致，否则替身测试会给出错误的信号。"""
    store_assets = _store_module()
    samples = [
        "林澈", "林 澈", "Iron Man", "ironman", "ＩＲＯＮ　ＭＡＮ", "钢铁侠", "鋼鐵俠",
        "「钢铁侠」", "钢铁侠。", "角色:林澈", "Scene 01", "SCENE-01", "é", "e\u0301", "",
    ]
    for text in samples:
        assert fake_index_key(text) == store_assets.index_key(text), text


def test_real_store_new_generate_and_exists_reuse(tmp_path):
    store = _real_store(tmp_path)
    asset, version = _seed(store, "林澈")
    meta_before = store.index_path.read_bytes()

    reused = match_candidate({"name": "林 澈", "asset_type": "CHARACTER"}, store)
    assert reused.outcome is MatchOutcome.EXISTS_REUSE
    assert reused.asset_id == asset["asset_id"]
    assert reused.version == version["version"] == "v1"
    assert "locked" in reused.reason

    fresh = match_candidate({"name": "苏晚", "asset_type": "CHARACTER"}, store)
    assert fresh.outcome is MatchOutcome.NEW_GENERATE

    assert store.index_path.read_bytes() == meta_before, "匹配过程本身不得改动素材库"


def test_real_store_unlocked_version_is_marked(tmp_path):
    store = _real_store(tmp_path)
    _seed(store, "苏晚", lock=False)

    result = match_candidate({"name": "苏晚"}, store)

    assert result.outcome is MatchOutcome.EXISTS_REUSE
    assert "unlocked" in result.reason


def test_real_store_suspect_branch_persists_and_keeps_meta_bytes(tmp_path):
    store = _real_store(tmp_path)
    store.create_asset(asset_type="CHARACTER", name="钢铁侠", source="AI", library_scope="PROJECT")
    meta_before = store.index_path.read_bytes()
    assert not store.suspects_path.exists()

    first = match_candidate({"name": "鋼鐵俠", "asset_type": "CHARACTER"}, store)

    assert first.outcome is MatchOutcome.SUSPECT_DUPLICATE
    assert first.version is None
    assert first.suspect_id and first.suspect_id.startswith("SUSPECT_")
    assert store.index_path.read_bytes() == meta_before, "SUSPECT 分支必须不触碰 meta.json"
    assert store.suspects_path.exists(), "疑似记录必须落盘（绝不静默丢弃）"

    queue = build_suspect_queue(store)
    assert len(queue) == 1
    assert queue[0]["suspect_id"] == first.suspect_id
    assert queue[0]["candidate_name"] == "鋼鐵俠"
    assert queue[0]["candidate_type"] == "CHARACTER"

    # 幂等：同一候选重复匹配不得产生第二条记录（Lead 硬要求）
    second = match_candidate({"name": "鋼鐵俠 ", "asset_type": "CHARACTER"}, store)
    assert second.outcome is MatchOutcome.SUSPECT_DUPLICATE, "复用既有疑似记录时结论不得改变"
    assert second.version is None
    assert second.suspect_id == first.suspect_id
    assert len(build_suspect_queue(store)) == 1
    assert store.index_path.read_bytes() == meta_before


def test_real_store_ambiguous_names_enter_suspect_queue(tmp_path):
    store = _real_store(tmp_path)
    store.create_asset(asset_type="CHARACTER", name="林澈", source="AI", library_scope="PROJECT")
    store.create_asset(asset_type="CHARACTER", name="林 澈", source="AI", library_scope="PROJECT")
    meta_before = store.index_path.read_bytes()

    result = match_candidate({"name": "林澈", "asset_type": "CHARACTER"}, store)

    assert result.outcome is MatchOutcome.SUSPECT_DUPLICATE, "同名多条 -> 歧义，绝不自动覆盖"
    assert "ambiguous_multiple_assets" in result.reason
    assert store.index_path.read_bytes() == meta_before
    assert len(build_suspect_queue(store)) == 1


def test_real_store_project_isolation(tmp_path):
    store_assets = _store_module()
    first = store_assets.AssetStore(_project_dir(tmp_path, "a" * 32))
    second = store_assets.AssetStore(_project_dir(tmp_path, "b" * 32))
    _seed(first, "林澈")

    result = match_candidate({"name": "林澈"}, second)

    assert result.outcome is MatchOutcome.NEW_GENERATE, "R5：不得跨项目复用素材"


def test_default_yaml_missing_credentials_is_actionable(tmp_path):
    """用仓库内置 config/default.yaml 走一遍：缺凭据必须报出真实键名。"""
    try:
        from bettercomfyui.config import load_config
    except Exception as exc:  # pragma: no cover - config 尚未就绪
        pytest.skip(f"bettercomfyui.config 尚不可用：{exc!r}")

    from bettercomfyui.prompt.gateway import GatewayError, build_gateway

    cfg = load_config(repo_root=_ROOT)
    provider = cfg_get(cfg, "prompt.model_provider", None)
    if provider is None:
        pytest.skip("默认配置读不到 prompt.model_provider，跳过")

    with pytest.raises(GatewayError) as excinfo:
        build_gateway(cfg)

    missing = set(excinfo.value.missing_keys)
    assert missing, "默认配置没有凭据，必须报出缺失键名"
    assert missing <= {"prompt.endpoint", "prompt.model_name", "prompt.api_key"}, missing
    for key in missing:
        assert key in str(excinfo.value)


def test_real_config_custom_provider_reads_flat_keys():
    """真实 PromptParams 用扁平键名 custom_response_path / custom_request_template。

    回归：早期实现只读点号路径 ``prompt.custom.response_path``，用户按
    default.yaml 配好 custom 接口后仍会报「缺少 response_path」——典型假失败。
    """
    try:
        from bettercomfyui.config import load_config
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"bettercomfyui.config 尚不可用：{exc!r}")

    from bettercomfyui.prompt.gateway import CustomGateway, build_gateway

    cfg = load_config(repo_root=_ROOT)
    section = cfg.prompt
    if not hasattr(section, "custom_response_path"):
        pytest.skip("PromptParams 未提供扁平 custom_* 键，跳过（键名方案已变更）")

    section.model_provider = "custom"
    section.endpoint = "http://127.0.0.1:9999/generate"
    section.model_name = "m"
    section.custom_response_path = "data.items.0.text"
    section.custom_request_template = {"prompt": "{system}\n{user}", "model": "{model}"}

    class _Transport:
        def __init__(self):
            self.calls = []

        def post_json(self, *, url, headers, payload, timeout_s):
            self.calls.append(payload)
            return {"data": {"items": [{"text": "自定义结果"}]}}

    transport = _Transport()
    gateway = build_gateway(cfg, transport=transport)
    assert isinstance(gateway, CustomGateway)
    assert gateway.complete(system="S", user="U") == "自定义结果"
    assert transport.calls[0]["prompt"] == "S\nU"


def test_real_config_blank_system_prompt_template_uses_default_preset():
    """default.yaml 里 system_prompt_template: '' 表示「未覆盖」，不得变成空角色块。"""
    try:
        from bettercomfyui.config import load_config
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"bettercomfyui.config 尚不可用：{exc!r}")

    from bettercomfyui.prompt.inject import SYSTEM_PROMPT_PRESETS, build_system_prompt

    cfg = load_config(repo_root=_ROOT)
    assert cfg_get(cfg, "prompt.system_prompt_template", None) in ("", None)

    system = build_system_prompt(cfg=cfg, manifest="")
    assert SYSTEM_PROMPT_PRESETS["default"] in system, "空模板必须回落到默认预置角色块"
    assert "存在性判断由程序" in system
