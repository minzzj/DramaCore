"""一致性回归测试：版本来源、版本比较、metadata mock 标记。

这三条都来自**真实事故**，且都属于「不报错但会误导人」的类型：
* 硬编码 ComfyUI 版本 -> 同一台机器上两个组件说两个版本；
* 字符串比版本 -> `"0.5" >= "0.30"` 判 True（错）；
* metadata 不标 mock -> 下游拿到 mp4 会误当真实产出。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# 1) 版本必须现场探测，不得硬编码
# ---------------------------------------------------------------------------


def test_no_hardcoded_comfyui_version_in_source():
    """源码里不得把**本机/实例的** ComfyUI 版本写成字面量。

    实测事故：`executors/comfyui_http.py` 写死 `LOCAL_COMFYUI_VERSION = "0.22.3"`，
    `demo.py` 的 `real_generation_reason` 也写死同样的字符串；
    而开发环境早已升级到 0.41.0。结果是 `doctor` 报 0.41.0、`demo` 报 0.22.3，
    **同一台机器两个答案**，用户无法判断该信哪个。

    ## 判据要分清两类常量（第一版把两者混在一起，误报了）

    * **禁止**：描述「本机/实例实际是什么版本」的常量
      （名字含 `LOCAL` / `CURRENT` / `INSTALLED`）—— 必须现场探测；
    * **允许**：描述「要求的最低版本」的常量（名字含 `MIN` / `REQUIRED`）
      —— 这是需求，本来就该固定在代码里。

    用 **AST** 扫描赋值语句而不是正则扫文本：否则注释/文档字符串里
    提到历史版本号也会被误判（第一版就踩过这个坑）。
    """
    import ast

    version_literal = re.compile(r"^\d+\.\d+")
    # 描述"实际是什么"的名字 -> 禁止；描述"要求至少多少"的名字 -> 允许
    forbidden_names = re.compile(r"(local|current|installed|actual)", re.IGNORECASE)
    allowed_names = re.compile(r"(min|required|minimum)", re.IGNORECASE)

    offenders: list[str] = []
    for py in sorted((REPO_ROOT / "bettercomfyui").rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                targets += [t.attr for t in node.targets if isinstance(t, ast.Attribute)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target.id]
            if not targets:
                continue
            name = " ".join(targets)
            if "comfyui" not in name.lower() and "comfy" not in name.lower():
                continue
            if allowed_names.search(name):
                continue
            if not forbidden_names.search(name):
                continue
            val = getattr(node, "value", None)
            if (isinstance(val, ast.Constant) and isinstance(val.value, str)
                    and version_literal.match(val.value)):
                offenders.append(
                    f"{py.relative_to(REPO_ROOT)}:{node.lineno}: {targets[0]} = {val.value!r}"
                )
    assert not offenders, (
        "源码里把**本机实例的** ComfyUI 版本写成了字面量（必须现场探测）：\n  "
        + "\n  ".join(offenders)
    )


def test_minimum_version_constant_is_allowed():
    """反向断言：要求类常量必须允许存在（防止上面那条把需求也禁掉）。"""
    from bettercomfyui.pipeline.core import MIN_COMFYUI_VERSION

    assert MIN_COMFYUI_VERSION == "0.30.0"


def test_read_comfyui_version_reads_the_real_file(tmp_path: Path):
    from bettercomfyui.executors.comfyui_http import read_comfyui_version

    (tmp_path / "comfyui_version.py").write_text(
        '# generated\n__version__ = "0.41.0"\n', encoding="utf-8"
    )
    assert read_comfyui_version(tmp_path) == "0.41.0"


def test_read_comfyui_version_returns_none_when_unknown(tmp_path: Path):
    """读不到时必须返回 None（调用方应如实说"未知"），不得猜一个版本。"""
    from bettercomfyui.executors.comfyui_http import read_comfyui_version

    assert read_comfyui_version(tmp_path) is None          # 目录里没有版本文件
    assert read_comfyui_version(None) is None              # 未配置
    assert read_comfyui_version(tmp_path / "nope") is None  # 路径不存在


# ---------------------------------------------------------------------------
# 2) 版本比较必须按数字段
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version,expected",
    [
        ("0.41.0", True),
        ("0.30.0", True),
        ("0.30", True),
        ("0.31.2", True),
        ("1.0.0", True),
        ("0.22.3", False),
        ("0.29.9", False),
        # ↓ 这两条是字符串比较会**错判**的用例，必须按数字段比较
        ("0.5", False),
        ("0.9", False),
    ],
)
def test_version_comparison_is_numeric(version, expected):
    from bettercomfyui.executors.comfyui_http import meets_min_comfyui

    assert meets_min_comfyui(version) is expected, (
        f"{version} 与 0.30.0 的比较结果应为 {expected}。"
        "若用了字符串比较，'0.5' >= '0.30' 会被错判为 True。"
    )


def test_unknown_version_does_not_count_as_meeting_minimum():
    """不知道版本就不能算满足 —— 『不知道』不等于『没问题』。"""
    from bettercomfyui.executors.comfyui_http import meets_min_comfyui

    assert meets_min_comfyui(None) is False
    assert meets_min_comfyui("") is False


def test_version_tuple_parses_numeric_segments():
    """解析结果必须**定长补齐**，否则短版本会因元组前缀规则被判小。"""
    from bettercomfyui.executors.comfyui_http import _version_tuple

    assert _version_tuple("0.41.0") == (0, 41, 0, 0)
    assert _version_tuple("1") == (1, 0, 0, 0)
    assert _version_tuple("0.5") < _version_tuple("0.30")   # 数字语义，非字符串
    # 版本写少一段不能因此"不满足自己的下限"
    assert _version_tuple("0.30") == _version_tuple("0.30.0")
    assert _version_tuple("0.30") >= _version_tuple("0.30.0")


# ---------------------------------------------------------------------------
# 3) 成片 metadata 必须带 mock 标记
# ---------------------------------------------------------------------------


def _make_real_media(path: Path) -> bool:
    """造一个**真实可解析**的 mp4，供 write_output 的交付校验通过。

    不能用 b"\\x00"*N 充数：`write_output` 会真跑 ffmpeg 校验（这正是它的价值），
    假字节会被 `PipelineError: 无法解析为合法媒体` 挡下。
    """
    import subprocess

    from bettercomfyui.pipeline.core import _ffmpeg_exe

    proc = subprocess.run(
        [_ffmpeg_exe(), "-y", "-f", "lavfi", "-i",
         "color=c=black:s=64x64:r=10:d=1", "-pix_fmt", "yuv420p", str(path)],
        capture_output=True, check=False,
    )
    return path.is_file() and path.stat().st_size > 0


@pytest.fixture()
def real_media(tmp_path: Path) -> Path:
    p = tmp_path / "in.mp4"
    if not _make_real_media(p):
        pytest.skip("未验证：无可用 ffmpeg，无法生成真实媒体夹具")
    return p


def test_write_output_refuses_empty_generation_provenance(tmp_path: Path, real_media: Path):
    """拒绝写出没有来源声明的 metadata。

    理由：mp4 在磁盘上无法区分「模型生成」与「占位媒体」。
    来源若只打印在控制台，下次有人拿到 mp4 + metadata 就会误当真实产出。
    """
    from bettercomfyui.config import Config
    from bettercomfyui.models.entities import MovieMetadata
    from bettercomfyui.pipeline.core import PipelineError, write_output

    meta = MovieMetadata(project_id="p", script_id="s", workflow_version="0.1.0")
    with pytest.raises(PipelineError, match="generation_provenance"):
        write_output(real_media, Config(), project_dir=tmp_path / "proj", metadata=meta)


def test_write_output_requires_is_mock_field(tmp_path: Path, real_media: Path):
    """provenance 里必须有 `is_mock` —— 少了它就无法判定真假。"""
    from bettercomfyui.config import Config
    from bettercomfyui.models.entities import MovieMetadata
    from bettercomfyui.pipeline.core import PipelineError, write_output

    meta = MovieMetadata(project_id="p", script_id="s", workflow_version="0.1.0")
    meta.generation_provenance = {"executor": "comfyui"}   # 故意缺 is_mock
    with pytest.raises(PipelineError, match="is_mock"):
        write_output(real_media, Config(), project_dir=tmp_path / "proj", metadata=meta)


def test_metadata_marker_survives_round_trip(tmp_path: Path, real_media: Path):
    """真实写盘后，metadata 里必须能读回 mock 标记与警告。"""
    from bettercomfyui.config import Config
    from bettercomfyui.models.entities import MovieMetadata
    from bettercomfyui.pipeline.core import write_output

    meta = MovieMetadata(project_id="p", script_id="s", workflow_version="0.1.0")
    meta.generation_provenance = {
        "is_mock": True,
        "executor": "placeholder-ffmpeg",
        "reason": "测试用",
        "unverified": ["真实视频模型生成"],
    }
    out = write_output(real_media, Config(), project_dir=tmp_path / "proj", metadata=meta)

    payload = json.loads(Path(out["metadata_path"]).read_text(encoding="utf-8"))
    assert payload["is_mock_output"] is True
    prov = payload["generation_provenance"]
    assert prov["is_mock"] is True
    assert prov["unverified"] == ["真实视频模型生成"]
    assert prov.get("warning"), "mock 产出必须在 metadata 里带显眼警告"


def test_metadata_marks_real_output_without_warning(tmp_path: Path, real_media: Path):
    """真实产出不得被误标成 mock（反向断言，防止恒真）。"""
    from bettercomfyui.config import Config
    from bettercomfyui.models.entities import MovieMetadata
    from bettercomfyui.pipeline.core import write_output

    meta = MovieMetadata(project_id="p", script_id="s", workflow_version="0.1.0")
    meta.generation_provenance = {"is_mock": False, "executor": "comfyui"}
    out = write_output(real_media, Config(), project_dir=tmp_path / "proj", metadata=meta)

    payload = json.loads(Path(out["metadata_path"]).read_text(encoding="utf-8"))
    assert payload["is_mock_output"] is False
    assert "warning" not in payload["generation_provenance"]


def test_movie_metadata_is_mock_output_property():
    from bettercomfyui.models.entities import MovieMetadata

    m = MovieMetadata(project_id="p", script_id="s", workflow_version="0.1.0")
    assert m.is_mock_output is False
    m.generation_provenance = {"is_mock": True}
    assert m.is_mock_output is True


def test_movie_metadata_rejects_non_dict_provenance():
    from bettercomfyui.models.entities import MovieMetadata

    with pytest.raises(ValueError, match="generation_provenance"):
        MovieMetadata(
            project_id="p", script_id="s", workflow_version="0.1.0",
            generation_provenance="not-a-dict",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# 4) doctor 与 demo 的版本必须一致（同一台机器不能有两个答案）
# ---------------------------------------------------------------------------


def test_doctor_and_demo_agree_on_version():
    """`doctor` 与 `demo` 探测到的 ComfyUI 版本必须来自同一处、给出同一结果。"""
    from bettercomfyui.cli import _probe_comfyui
    from bettercomfyui.config import load_config
    from bettercomfyui.executors.comfyui_http import read_comfyui_version

    cfg = load_config(repo_root=REPO_ROOT)
    base = cfg.global_.comfyui_base_path
    if not base or not Path(base).exists():
        pytest.skip("未验证：本机未配置可用的 ComfyUI 路径")

    probed = _probe_comfyui(base)
    direct = read_comfyui_version(base)
    assert probed["version"] == direct, (
        f"doctor 探测到 {probed['version']!r}，executor 探测到 {direct!r} —— "
        "同一台机器上两个组件给出了不同的版本，用户无法判断该信哪个"
    )


# ---------------------------------------------------------------------------
# 5) 真实生成判据：唯一真源（有文件 ≠ 能出片）
# ---------------------------------------------------------------------------


def test_probe_model_kinds_requires_all_categories(tmp_path: Path):
    """三类齐全才算可用；只有 LoRA 时必须判不可用。

    实测两次假阳性（详见 docs/ROUND3_FINDINGS.md）：
    把 11 个 yaml 当模型；下载 LoRA 后报「具备条件」。
    """
    from bettercomfyui.pipeline.core import probe_model_kinds

    models = tmp_path / "models"
    (models / "loras").mkdir(parents=True)
    (models / "loras" / "a.safetensors").write_bytes(b"x")
    r = probe_model_kinds(models)
    assert r["available"] is False
    assert set(r["missing"]) == {"扩散模型", "文本编码器", "VAE"}

    # 补上三类
    for sub, name in (("diffusion_models", "d.safetensors"),
                      ("text_encoders", "t.safetensors"),
                      ("vae", "v.safetensors")):
        (models / sub).mkdir(parents=True, exist_ok=True)
        (models / sub / name).write_bytes(b"x")
    r2 = probe_model_kinds(models)
    assert r2["available"] is True, r2
    assert r2["missing"] == []


def test_probe_model_kinds_excludes_partial_and_placeholder(tmp_path: Path):
    """下载中的 `*.part` 与占位 `put_*` 绝不能算成可用权重。

    `.part` 尤其重要：模型下载要几小时，期间磁盘上就是 `.part`，
    把它算成"有了"会让 doctor 在下载中途就报可用。
    """
    from bettercomfyui.pipeline.core import probe_model_kinds

    models = tmp_path / "models"
    (models / "diffusion_models").mkdir(parents=True)
    (models / "diffusion_models" / "big.safetensors.part").write_bytes(b"x" * 100)
    (models / "text_encoders").mkdir(parents=True)
    (models / "text_encoders" / "put_here.txt").write_text("x", encoding="utf-8")
    (models / "vae").mkdir(parents=True)
    (models / "vae" / "put_vae_here.safetensors").write_bytes(b"x")

    r = probe_model_kinds(models)
    assert r["total"] == 0, f"不应把 .part / put_* 算进权重，实际 {r['files']}"
    assert r["available"] is False


def test_probe_model_kinds_on_missing_dir():
    """目录不存在/未配置时必须返回不可用，而不是抛错或当可用。"""
    from bettercomfyui.pipeline.core import probe_model_kinds

    for bad in (None, "/definitely/not/a/dir"):
        r = probe_model_kinds(bad)
        assert r["available"] is False
        assert r["total"] == 0
        assert r["missing"], "必须点名缺哪几类"


def test_plugin_route_uses_the_shared_probe():
    """插件路由必须用同一份判据（不能自己再数一遍文件）。

    `routes.py` 曾经自己数 `folder_paths` 的扩散模型列表，
    结果把「正在下载的扩散模型」算成可用，且完全不检查文本编码器与 VAE。
    """
    import re

    src = (REPO_ROOT / "bettercomfyui_comfy" / "routes.py").read_text(encoding="utf-8")
    assert "probe_model_kinds" in src, (
        "插件路由没有使用统一的 probe_model_kinds —— 会与 doctor 得出不同结论"
    )
    # 不得再出现"只数个数就判可用"的旧写法
    assert not re.search(
        r'real_generation_available"\]\s*=\s*bool\(ckpts\)', src
    ), "routes.py 里仍有「只数 checkpoints 个数」的旧判据"

