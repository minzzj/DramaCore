"""Lead 负责的 CLI 与 doctor 诊断测试。

doctor 是本项目最重要的诊断入口：它决定「能不能真跑」这个判断。
因此它最容易犯两类错误，本文件逐一钉住：

  F1 漏报：明明不可用却报可用（假成功）。
  F2 误报：把非模型文件（yaml 配置、占位文件）当成模型权重（假可用）。

F2 是真实发生过的缺陷：早期实现只排除 `put_*` 占位文件，
把 `models/configs/` 下 11 个 yaml 当成了模型，`doctor` 一度输出「模型权重文件数: 11」。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bettercomfyui import cli


# ---------------------------------------------------------------------------
# 权重探测：不许多报
# ---------------------------------------------------------------------------


def _make_fake_comfyui(base: Path, *, version: str, weights: int, configs: int) -> Path:
    root = base / "ComfyUI"
    (root / "models" / "checkpoints").mkdir(parents=True, exist_ok=True)
    (root / "models" / "configs").mkdir(parents=True, exist_ok=True)
    (root / "custom_nodes").mkdir(parents=True, exist_ok=True)
    (root / "comfyui_version.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    for i in range(weights):
        (root / "models" / "checkpoints" / f"w{i}.safetensors").write_bytes(b"\x00" * 16)
    for i in range(configs):
        (root / "models" / "configs" / f"c{i}.yaml").write_text("a: 1\n", encoding="utf-8")
    # 占位文件绝不能被算成权重
    (root / "models" / "checkpoints" / "put_checkpoints_here").write_text("", encoding="utf-8")
    return root


def test_probe_counts_only_real_weights(tmp_path: Path):
    """F2 回归：yaml 配置与 put_* 占位文件都不算权重。"""
    root = _make_fake_comfyui(tmp_path, version="0.41.0", weights=3, configs=11)
    result = cli._probe_comfyui(root)
    assert result["found"] is True
    assert result["model_files"] == 3, (
        f"只应统计 3 个 .safetensors，实际 {result['model_files']}"
        f"（yaml 配置与 put_* 占位文件必须排除）"
    )
    assert all(s.endswith(".safetensors") for s in result["weight_samples"])


def test_probe_reports_missing_config_instead_of_guessing():
    """未配置路径时必须如实报告，而不是去猜一个路径然后可能猜错。"""
    result = cli._probe_comfyui(None)
    assert result["found"] is False
    assert result["configured"] is False
    assert any("comfyui_base_path" in n for n in result["notes"]), result["notes"]


def test_probe_reports_nonexistent_configured_path(tmp_path: Path):
    result = cli._probe_comfyui(tmp_path / "does_not_exist")
    assert result["found"] is False
    assert result["configured"] is True
    assert any("不存在" in n for n in result["notes"]), result["notes"]


# ---------------------------------------------------------------------------
# 结论：不许把不可用报成可用
# ---------------------------------------------------------------------------


def _run_doctor(capsys, monkeypatch, *, version: str, weights: int) -> tuple[int, dict]:
    """用一个伪造的 ComfyUI 布局跑 doctor，返回 (exit_code, report)。"""
    import io
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = _make_fake_comfyui(Path(td), version=version, weights=weights, configs=0)
        monkeypatch.setattr(cli, "_load_config", _fake_config(root))
        # 计算设备探测打桩：本文件只测「权重/版本」判据，不访问真实 ComfyUI。
        monkeypatch.setattr(cli, "_probe_compute_capability", lambda cfg: _fake_compute(True))
        args = type("A", (), {"json": True})()
        code = cli.cmd_doctor(args)
        out = capsys.readouterr().out
        payload = json.loads(out.split("[JSON]", 1)[1])
        return code, payload


def _fake_compute(available: bool) -> dict:
    """测试替身：一块 24GB GPU（available=True）或只有 4GB 的卡（False）。"""
    return {
        "available": available,
        "device": "Fake GPU",
        "device_type": "cuda",
        "vram_mib": 24576 if available else 4096,
        "min_vram_mib": 24000,
        "reason": ("测试替身：满足" if available else "测试替身：显存不足"),
    }


def _fake_config(comfyui_root: Path):
    from bettercomfyui.config import Config

    def loader(*a, **kw):
        cfg = Config()
        cfg.global_.comfyui_base_path = str(comfyui_root)
        return cfg

    return loader


def test_doctor_declares_real_generation_unavailable_without_weights(capsys, monkeypatch):
    """F1 回归：没有权重时必须明确判定「真实生成不可用」。"""
    code, report = _run_doctor(capsys, monkeypatch, version="0.41.0", weights=0)
    assert report["comfyui"]["model_files"] == 0
    assert report["real_generation_available"] is False, (
        "没有模型权重却报告真实生成可用 —— 这是最严重的假成功"
    )


def test_doctor_declares_real_generation_unavailable_on_old_comfyui(capsys, monkeypatch):
    """版本低于 0.30 时，即使有权重也必须判定真实生成不可用（导演台不可用）。"""
    code, report = _run_doctor(capsys, monkeypatch, version="0.22.3", weights=5)
    assert report["comfyui"]["model_files"] == 5
    assert report["real_generation_available"] is False, (
        "ComfyUI 0.22.3 低于导演台要求的 0.30，不得报告真实生成可用"
    )


def test_doctor_only_claims_available_when_both_prerequisites_met(capsys, monkeypatch):
    """前提同时满足才允许报告可用。

    这里的「前提」不只是「版本够 + 有文件」，还包括**必需类别齐全**
    （扩散模型 / 文本编码器 / VAE）。早期只数文件个数，
    导致「只放了 2 个 LoRA」也报可用 —— 那是假阳性（见 F3 三条测试）。
    """
    code, report = _run_doctor_dirs(
        capsys, monkeypatch, version="0.41.0",
        subdirs={"diffusion_models": 1, "text_encoders": 1, "vae": 1},
    )
    assert report["real_generation_available"] is True


# ---------------------------------------------------------------------------
# CLI 行为
# ---------------------------------------------------------------------------


def test_cli_module_has_no_hardcoded_absolute_paths():
    """CONTRACTS.md 第 1 节：不得硬编码绝对路径（C8 回归）。"""
    text = Path(cli.__file__).read_text(encoding="utf-8")
    for needle in ("C:\\Users", "C:/Users", "D:\\", "/home/"):
        assert needle not in text, f"cli.py 出现硬编码绝对路径片段：{needle}"


def test_cli_help_works(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for sub in ("doctor", "project", "asset", "run"):
        assert sub in out


def test_cli_unknown_command_exits_nonzero():
    with pytest.raises(SystemExit) as exc:
        cli.main(["definitely-not-a-command"])
    assert exc.value.code != 0


def test_cli_missing_module_reports_actionable_error(monkeypatch, capsys):
    """依赖未就绪时必须给出可操作错误并以 NOT_READY 退出，不得假成功。"""
    import importlib

    real_import = importlib.import_module

    def fake_import(name, *a, **kw):
        if name == "bettercomfyui.store.projects":
            raise ModuleNotFoundError(f"No module named '{name}'", name=name)
        return real_import(name, *a, **kw)

    monkeypatch.setattr(cli.importlib, "import_module", fake_import)
    code = cli.main(["project", "list"])
    assert code == cli.EXIT_NOT_READY
    err = capsys.readouterr().err
    assert "尚未就绪" in err and "doctor" in err, err


# ---------------------------------------------------------------------------
# F3 部分权重：有文件 ≠ 能出片（真实发生过的假阳性）
# ---------------------------------------------------------------------------


def _fake_instance(base: Path, *, version: str, subdirs: dict[str, int]) -> Path:
    """造一个 ComfyUI 实例：`subdirs` = {models 下的子目录: 文件数}。"""
    root = base / "ComfyUI"
    (root / "models").mkdir(parents=True, exist_ok=True)
    (root / "custom_nodes").mkdir(parents=True, exist_ok=True)
    (root / "comfyui_version.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    for sub, count in subdirs.items():
        d = root / "models" / sub
        d.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            (d / f"w{i}.safetensors").write_bytes(b"\x00" * 16)
    return root


def _run_doctor_dirs(capsys, monkeypatch, *, version: str,
                     subdirs: dict[str, int], compute_ok: bool = True) -> tuple[int, dict]:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = _fake_instance(Path(td), version=version, subdirs=subdirs)
        monkeypatch.setattr(cli, "_load_config", _fake_config(root))
        monkeypatch.setattr(
            cli, "_probe_compute_capability", lambda cfg: _fake_compute(compute_ok))
        args = type("A", (), {"json": True})()
        code = cli.cmd_doctor(args)
        out = capsys.readouterr().out
        payload = json.loads(out.split("[JSON]", 1)[1])
        return code, payload


def test_only_lora_present_is_not_reported_as_available(capsys, monkeypatch):
    """**真实发生过的假阳性**：只有 LoRA 时不得报 `real_generation_available=True`。

    实测经过：往 `models/loras` 放了 2 个 LoRA 文件后，`model_files` 变成 2，
    旧实现（只数文件个数）于是报出「真实生成：环境看起来具备条件」——
    但那两个是 LoRA，**没有任何扩散模型**，根本出不了片。

    有文件 ≠ 能出片。这条断言钉住"必需类别齐全"这个更强条件。
    """
    code, report = _run_doctor_dirs(
        capsys, monkeypatch, version="0.41.0",
        subdirs={"loras": 2, "vae": 1, "text_encoders": 1},
    )
    assert report["comfyui"]["model_files"] == 4, (
        f"应先数到 4 个权重文件，实际 {report['comfyui']['model_files']}"
    )
    assert report["real_generation_available"] is False, (
        "只有 LoRA + VAE + 文本编码器、**没有扩散模型**，却报告真实生成可用 —— "
        "这就是「有文件 ≠ 能出片」的假阳性"
    )
    assert "扩散模型" in report["real_generation_missing_kinds"]


def test_all_required_kinds_present_reports_available(capsys, monkeypatch):
    """反向断言：三类齐全时才应报可用（防止上面那条恒真）。"""
    code, report = _run_doctor_dirs(
        capsys, monkeypatch, version="0.41.0",
        subdirs={"diffusion_models": 1, "text_encoders": 1, "vae": 1},
    )
    assert report["real_generation_available"] is True, report
    assert report["real_generation_missing_kinds"] == []


def test_lora_only_names_every_missing_kind(capsys, monkeypatch):
    """只有 LoRA 时必须点名缺哪几类，便于用户照着补。"""
    code, report = _run_doctor_dirs(
        capsys, monkeypatch, version="0.41.0", subdirs={"loras": 3},
    )
    missing = report["real_generation_missing_kinds"]
    assert "扩散模型" in missing and "文本编码器" in missing and "VAE" in missing, missing
    assert report["real_generation_available"] is False


# ---------------------------------------------------------------------------
# F4：权重齐全 + 版本够，但**没有够显存的 GPU** 时不得报可用（审计 R1 实测假阳性）
# ---------------------------------------------------------------------------


def test_doctor_declares_unavailable_when_weights_ready_but_gpu_insufficient(
        capsys, monkeypatch):
    """实测假阳性：本机 H3 权重齐全、ComfyUI 0.41，但服务以 --cpu 启动、
    唯一 GPU 只有 4GB —— 旧版 doctor 报「环境看起来具备条件」。

    真实 H3 生成需要 ≥24GiB 显存；权重与版本不是全部前置条件。
    """
    code, report = _run_doctor_dirs(
        capsys, monkeypatch, version="0.41.0",
        subdirs={"diffusion_models": 1, "text_encoders": 1, "vae": 1},
        compute_ok=False,
    )
    assert report["real_generation_available"] is False, (
        "权重齐全但计算设备不满足，却报告真实生成可用 —— 假阳性")
    assert report["compute"]["available"] is False
    assert str(report["compute"]["reason"]).strip()


def test_doctor_requires_compute_capability_fields(capsys, monkeypatch):
    """JSON 报告里必须带上计算设备判定，便于面板/用户看到「为什么不可用」。"""
    code, report = _run_doctor_dirs(
        capsys, monkeypatch, version="0.41.0",
        subdirs={"diffusion_models": 1, "text_encoders": 1, "vae": 1},
        compute_ok=True,
    )
    compute = report.get("compute") or {}
    for key in ("available", "device", "device_type", "vram_mib",
                "min_vram_mib", "reason"):
        assert key in compute, f"doctor JSON 缺少计算设备字段 {key}：{compute}"
    assert compute["available"] is True


# ---------------------------------------------------------------------------
# 计算设备判定函数本身（唯一真源，cli / 插件路由共用）
# ---------------------------------------------------------------------------


def test_evaluate_compute_devices_cpu_only_is_unavailable():
    from bettercomfyui.pipeline.core import evaluate_compute_devices

    result = evaluate_compute_devices(
        [{"name": "cpu", "type": "cpu", "vram_total": 16 * 1024**3}])
    assert result["available"] is False
    assert "CPU" in result["reason"]


def test_evaluate_compute_devices_small_vram_is_unavailable():
    from bettercomfyui.pipeline.core import evaluate_compute_devices

    result = evaluate_compute_devices(
        [{"name": "GTX 1050 Ti", "type": "cuda", "vram_total": 4 * 1024**3}])
    assert result["available"] is False
    assert "显存" in result["reason"]
    assert result["vram_mib"] == 4096


def test_evaluate_compute_devices_enough_vram_is_available():
    from bettercomfyui.pipeline.core import evaluate_compute_devices

    result = evaluate_compute_devices(
        [{"name": "RTX 4090", "type": "cuda", "vram_total": 24 * 1024**3}])
    assert result["available"] is True
    assert result["vram_mib"] == 24576


def test_evaluate_compute_devices_empty_is_unavailable_not_guessed():
    from bettercomfyui.pipeline.core import evaluate_compute_devices

    result = evaluate_compute_devices(None)
    assert result["available"] is False
    assert result["vram_mib"] is None
    assert "未获取" in result["reason"]
