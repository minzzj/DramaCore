"""store 层红线自检 + 契约签名冻结（对应 task-2 的「严禁」清单）。

1. 依赖方向单向：store 不得 import prompt/review/director/pipeline（运行期 + 静态双重检查）；
2. 不得重复定义枚举（枚举真源只有 ``models/enums.py``）；
3. 不得硬编码绝对路径（一律经 Config/root 解析）；
4. R2 静态检查：不得出现原地覆盖素材文件的调用模式（``json.dump`` / ``copyfile`` /
   ``write_bytes`` / 直接 ``os.replace``），原子写只能出现在 ``atomic.py`` 一处；
5. 契约第 3、4 节的公开名字与参数形态必须逐条存在（含 keyword-only 约束）。
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _path in (str(_REPO), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from test_store_support import assert_child_ok, run_child  # noqa: E402

STORE_DIR = _REPO / "bettercomfyui" / "store"
STORE_FILES = sorted(STORE_DIR.glob("*.py"))

POSITIONAL_OR_KEYWORD = inspect.Parameter.POSITIONAL_OR_KEYWORD
KEYWORD_ONLY = inspect.Parameter.KEYWORD_ONLY
POSITIONAL_ONLY = inspect.Parameter.POSITIONAL_ONLY


def test_store_sources_exist():
    names = {path.name for path in STORE_FILES}
    assert names == {
        "__init__.py",
        "assets.py",
        "atomic.py",
        "checkpoints.py",
        "errors.py",
        "events.py",
        "ids.py",
        "inject.py",
        "lockfile.py",
        "paths.py",
        "projects.py",
        "segments.py",
    }, names


def test_no_bom_and_all_files_parse():
    for path in STORE_FILES:
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{path.name} 含 UTF-8 BOM"
        ast.parse(raw.decode("utf-8"))


def test_importing_store_does_not_load_forbidden_layers():
    """在干净子进程里 import store，绝不能把 prompt/review/director/pipeline 拉进来。"""
    result = run_child(
        "import sys\n"
        "import bettercomfyui.store as store\n"
        "bad = sorted(\n"
        "    name for name in sys.modules\n"
        "    if name.startswith(\n"
        "        ('bettercomfyui.prompt', 'bettercomfyui.review',\n"
        "         'bettercomfyui.director', 'bettercomfyui.pipeline')\n"
        "    )\n"
        ")\n"
        "assert not bad, bad\n"
        "print('IMPORTS_OK', len(store.__all__))\n"
    )
    assert_child_ok(result)
    assert "IMPORTS_OK" in result.stdout


def test_no_forbidden_import_statements_in_source():
    pattern = re.compile(
        r"^\s*(?:from|import)\s+[\w\.]*(?:prompt|review|director|pipeline)\b", re.MULTILINE
    )
    for path in STORE_FILES:
        found = pattern.findall(path.read_text(encoding="utf-8"))
        assert not found, f"{path.name} 出现被禁止的跨层 import：{found}"


def test_no_duplicate_enum_definitions():
    for path in STORE_FILES:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"class\s+\w+\s*\([^)]*Enum[^)]*\)", text), path.name


def test_no_hardcoded_absolute_paths():
    pattern = re.compile(r"[A-Za-z]:\\\\|[A-Za-z]:\"|/Users/|/home/|\\\\Users\\\\")
    for path in STORE_FILES:
        assert not pattern.search(path.read_text(encoding="utf-8")), path.name


def test_no_in_place_overwrite_patterns():
    """R2 静态检查：store 源码里不得出现原地覆盖文件的写法。"""
    banned = ["json.dump(", "shutil.copyfile(", "shutil.copy2(", "shutil.move(", "write_bytes("]
    for path in STORE_FILES:
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{path.name} 含禁止写入模式 {token}"


def test_atomic_replace_only_in_atomic_module():
    for path in STORE_FILES:
        if path.name in {"atomic.py", "lockfile.py"}:
            continue
        assert "os.replace" not in path.read_text(encoding="utf-8"), path.name
    assert "os.replace" in (STORE_DIR / "atomic.py").read_text(encoding="utf-8")


def test_only_create_time_write_text_is_project_config():
    """唯一允许的非原子文本写：新建项目时写 config.yaml（全新文件，无覆盖风险）。"""
    hits = []
    for path in STORE_FILES:
        text = path.read_text(encoding="utf-8")
        for _ in re.finditer(r"write_text\(", text):
            hits.append(path.name)
    assert hits == ["projects.py"], hits
    assert "config_yaml" in (STORE_DIR / "projects.py").read_text(encoding="utf-8")


def test_meta_json_updates_use_locked_update_json():
    """所有 meta.json 写路径都必须经过 locked_update_json。"""
    assets = (STORE_DIR / "assets.py").read_text(encoding="utf-8")
    assert "locked_update_json(self.index_path" in assets
    assert "def _mutate_index" in assets
    # 索引写入只能有一个出口：_mutate_index
    assert assets.count("locked_update_json(self.index_path") == 1


# ---------------------------------------------------------------------------
# 契约签名冻结
# ---------------------------------------------------------------------------


def _assert_params(func, *, positional=(), keyword_only=(), name=None):
    label = name or getattr(func, "__qualname__", str(func))
    params = inspect.signature(func).parameters
    for item in positional:
        assert item in params, f"{label} 缺少位置参数 {item}"
        assert params[item].kind in (POSITIONAL_OR_KEYWORD, POSITIONAL_ONLY), (
            f"{label}.{item} 必须是位置参数，实际 {params[item].kind}"
        )
    for item in keyword_only:
        assert item in params, f"{label} 缺少关键字参数 {item}"
        assert params[item].kind is KEYWORD_ONLY, (
            f"{label}.{item} 必须是 keyword-only（契约要求），实际 {params[item].kind}"
        )


def test_atomic_module_signatures_match_contract():
    from bettercomfyui.store import atomic

    _assert_params(atomic.canonical_json, positional=("obj",))
    _assert_params(atomic.atomic_write_json, positional=("path", "obj"), keyword_only=("fsync",))
    _assert_params(
        atomic.locked_update_json, positional=("path", "mutator"), keyword_only=("timeout_s",)
    )
    _assert_params(atomic.file_lock, positional=("lock_path",), keyword_only=("timeout_s",))


def test_ids_functions_exist_with_expected_arity():
    from bettercomfyui.store import ids

    expected = {
        "new_project_id": (),
        "new_asset_id": ("project_name", "asset_type", "seq"),
        "next_version": ("value",),
        "content_hash_of": ("path",),
        "lock_id": ("asset_id", "version"),
        "new_suspect_id": ("normalized_name",),
        "chapter_id": ("seq",),
        "segment_id": ("project_id", "chapter", "seg"),
        "attempt_id": ("segment_id_value", "version", "seq"),
        "audit_id": ("segment_id_value", "version"),
        "review_cycle_id": ("object_type", "object_id", "version"),
        "mod_id": ("object_type", "object_id"),
        "new_cascade_id": ("segment_id_value",),
        "cnt_id": ("object_id", "kind"),
        "arb_id": ("human_review_id", "model_review_id"),
        "abort_id": ("review_record_id",),
        "new_finalization_token": (),
        "idempotency_key": ("namespace", "payload"),
    }
    for name, positional in expected.items():
        func = getattr(ids, name, None)
        assert callable(func), f"ids.{name} 不存在"
        _assert_params(func, positional=positional, name=f"ids.{name}")


def test_project_store_signatures_match_contract(tmp_path):
    from bettercomfyui.store import ProjectStore

    _assert_params(ProjectStore.__init__, positional=("root", "config"))
    _assert_params(ProjectStore.create, positional=("display_name",))
    _assert_params(ProjectStore.list, positional=())
    _assert_params(ProjectStore.open, positional=("project_id",))
    _assert_params(ProjectStore.close, positional=("project_id",))
    _assert_params(ProjectStore.delete, positional=("project_id",), keyword_only=("confirm_token",))
    _assert_params(ProjectStore.switch, positional=("from_id", "to_id"))


def test_asset_store_signatures_match_contract():
    from bettercomfyui.store import AssetStore

    _assert_params(AssetStore.__init__, positional=("project",))
    _assert_params(AssetStore.load_index, positional=())
    _assert_params(AssetStore.list_assets, positional=(), keyword_only=("asset_type", "scope"))
    _assert_params(
        AssetStore.find_by_normalized_name, positional=("normalized",), keyword_only=("scope",)
    )
    _assert_params(
        AssetStore.add_version,
        positional=("asset_id",),
        keyword_only=("source", "file_path", "status", "extra"),
    )
    _assert_params(
        AssetStore.create_asset,
        positional=(),
        keyword_only=("asset_type", "name", "source", "library_scope"),
    )
    _assert_params(
        AssetStore.set_version_status, positional=("asset_id", "version", "status")
    )
    _assert_params(
        AssetStore.set_lock,
        positional=("asset_id", "version"),
        keyword_only=("lock_source", "content_hash"),
    )
    _assert_params(AssetStore.clear_lock, positional=("asset_id", "version"))
    _assert_params(AssetStore.get_version, positional=("asset_id", "version"))
    _assert_params(AssetStore.resolve_lock, positional=("asset_id", "version"))
    _assert_params(AssetStore.references_of, positional=("asset_id",))
    _assert_params(AssetStore.record_reference, positional=("asset_id", "version", "ref"))


def test_checkpoint_and_inject_signatures_match_contract(tmp_path):
    from bettercomfyui.store import CheckpointStore, build_existing_asset_manifest

    _assert_params(CheckpointStore.__init__, positional=("project",))
    _assert_params(CheckpointStore.save, positional=("stage", "payload"))
    _assert_params(CheckpointStore.latest, positional=(), keyword_only=("at_or_before",))
    _assert_params(CheckpointStore.resolve_resume_target, positional=())
    _assert_params(
        build_existing_asset_manifest, positional=("store",), keyword_only=("library_scope",)
    )


def test_public_package_exports_contract_names():
    import bettercomfyui.store as store

    for name in (
        "canonical_json",
        "atomic_write_json",
        "locked_update_json",
        "file_lock",
        "AssetStore",
        "ProjectStore",
        "CheckpointStore",
        "build_existing_asset_manifest",
        "CheckpointStore",
        "ProjectPaths",
        "StoreError",
    ):
        assert hasattr(store, name), name
    for name in store.__all__:
        assert hasattr(store, name), f"__all__ 里的 {name} 实际不存在"


def test_error_hierarchy_is_uniform():
    from bettercomfyui.store import errors

    expected = [
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
    ]
    for name in expected:
        cls = getattr(errors, name)
        assert issubclass(cls, errors.StoreError), name
        assert cls.code and cls.code != "STORE_ERROR"
    assert issubclass(errors.StoreError, Exception)
