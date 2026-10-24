"""验收 15：至少完成一次端到端演示：剧本→素材→视频段→审核→拼接→输出。

原文（任务书第 9 节第 15 条，第 197 行）：
    「至少完成一次端到端演示：剧本→素材→视频段→审核→拼接→输出。」
同节第 199 行硬约束：
    「若真实视频模型暂不可用，必须明确区分真实集成与 mock 测试，
      不能把模拟结果说成真实生成已通过。」

实现入口（Lead 提供，已实测可跑）：
    `python -m bettercomfyui.cli run --executor mock [--workdir DIR]`
    输出为单块 JSON，字段含 ok / used_mock / real_generation / real_generation_reason /
    unverified[] / output_path / full_review_calls / event_lines / checkpoints[]。

实测结论（2026-10-16，3.3s 完成）：ok=true、used_mock=true、real_generation=false，
输出 104KB mp4 + metadata.json + cover.jpg；真实生成能力如实标注为不可用。

判定通过：
    P1 CLI 全流程退出码 0 且 JSON 可解析、ok=true；
    P2 交付物真实存在（mp4 非空 + metadata + cover）；
    P3 章节/素材/段/审核/拼接/输出各阶段都有实际推进（计数非零、full_review_calls == 1）；
    P4 **真实 vs mock 边界**：used_mock=true、real_generation=false 且给出原因，
       并列出 unverified 清单 —— 绝不允许把 mock 说成真实生成已通过。

判定假通过（必须能变红）：
    F1 只打印 ok=true 却没有产出文件 —— P2 变红；
    F2 阶段计数全 0（空跑）—— P3 变红；
    F3 把 mock 结果标成真实生成（real_generation=true 或 used_mock 缺失）—— P4 变红。

未验证边界：
    本机无模型权重、ComfyUI 0.22.3 < 导演台下限、无第三方导演节点，
    因此**真实视频生成本身仍未验证**；本用例只验「mock 编排闭环 + 边界标注正确」。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _harness import REPO_ROOT, load_optional

CLI_TIMEOUT_S = 600


def _run_cli_demo(tmp_path: Path) -> dict:
    """跑一次 mock 端到端演示，返回解析后的 JSON。"""
    if load_optional("bettercomfyui.cli") is None:
        pytest.skip("未验证：bettercomfyui.cli 尚未实现，端到端演示入口不可用")

    workdir = tmp_path / "media_workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "bettercomfyui.cli",
        "run",
        "--executor",
        "mock",
        "--workdir",
        str(workdir),
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CLI_TIMEOUT_S,
    )
    assert proc.returncode == 0, (
        f"端到端演示必须成功退出（exit={proc.returncode}）。\n"
        f"命令：{' '.join(cmd)}\nstdout尾：{proc.stdout[-1500:]}\nstderr尾：{proc.stderr[-1500:]}"
    )
    raw = proc.stdout.strip()
    start = raw.find("{")
    assert start >= 0, f"CLI 必须输出 JSON 报告，实际 stdout：{raw[:600]!r}"
    try:
        # CLI 在 JSON 之后还会打印一行「注意：本次使用 mock 执行器…」提示，
        # 因此必须只解析第一个 JSON 文档（raw_decode），不能要求整段 stdout 都是 JSON。
        payload, consumed = json.JSONDecoder().raw_decode(raw[start:])
        trailing = raw[start + consumed :].strip()
    except json.JSONDecodeError as exc:
        raise AssertionError(f"CLI 输出不是合法 JSON：{exc}；stdout尾={raw[-800:]!r}") from exc
    payload["__trailing"] = trailing
    payload["__cmd"] = " ".join(cmd)
    payload["__project_dir"] = str(REPO_ROOT / "projects" / str(payload.get("project_id", "")))
    return payload


def _cleanup_projects(payload: dict) -> None:
    """清理本次演示在仓库 projects/ 下产生的项目目录（同名 32 位 hex 才删）。"""
    pid = str(payload.get("project_id") or "")
    if len(pid) != 32 or any(c not in "0123456789abcdef" for c in pid):
        return
    target = (REPO_ROOT / "projects" / pid).resolve()
    root = (REPO_ROOT / "projects").resolve()
    if target.parent == root and target.is_dir():
        shutil.rmtree(target, ignore_errors=True)


def test_end_to_end_demo_runs_and_is_labeled_as_mock(tmp_path):
    """P1-P4：端到端演示跑通 + 交付物存在 + mock 边界标注正确。"""
    payload = _run_cli_demo(tmp_path)
    try:
        # P1
        assert payload.get("ok") is True, f"演示必须 ok=true，实际 {payload.get('ok')}（{payload['__cmd']}）"

        # P2：交付物真实存在
        out_path = Path(str(payload.get("output_path") or ""))
        assert payload.get("output_path"), f"必须返回 output_path，实际字段 {sorted(payload)}"
        assert out_path.exists(), (
            f"声称的成片不存在：{out_path}（命令：{payload['__cmd']}）"
        )
        assert out_path.stat().st_size > 10_000, (
            f"成片过小（{out_path.stat().st_size} 字节），疑似空文件：{out_path}"
        )
        meta_path = Path(str(payload.get("metadata_path") or ""))
        assert meta_path.exists(), f"元数据文件不存在：{meta_path}"

        # P3：每个阶段都真的推进了
        assert int(payload.get("segments_planned") or 0) >= 1, "必须建立段计划"
        assert int(payload.get("segments_generated") or 0) >= 1, "必须生成视频段"
        assert int(payload.get("segments_passed") or 0) >= 1, "必须有段通过审核"
        assert int(payload.get("assets_created") or 0) + int(payload.get("assets_reused") or 0) >= 1, (
            "必须创建或复用素材"
        )
        assert int(payload.get("assets_locked") or 0) >= 1, "素材必须取得 LOCK 才能进入生成（R3）"
        assert payload.get("spliced") is True and payload.get("encoded") is True, "必须完成拼接与封装"
        assert int(payload.get("full_review_calls") or 0) == 1, (
            f"R_full 只能在最终拼接后执行一次，实际 {payload.get('full_review_calls')}"
        )
        assert int(payload.get("event_lines") or 0) > 0, "必须写事件日志（契约第 10 节）"
        assert payload.get("checkpoints"), "必须保存检查点"

        # P4：真实 vs mock 边界（任务书第 9 节第 199 行）
        assert payload.get("used_mock") is True, (
            f"mock 演示必须显式标注 used_mock=true，实际 {payload.get('used_mock')!r}"
        )
        assert payload.get("real_generation") is False, (
            "本机无模型权重，绝不能声称真实生成（任务书第 9 节第 199 行）"
        )
        reason = str(payload.get("real_generation_reason") or "")
        assert reason.strip(), "必须给出「真实生成不可用」的可读原因"
        unverified = payload.get("unverified")
        assert isinstance(unverified, list) and unverified, (
            "必须显式列出未验证项（真实生成 / 第三方节点 / 目标机性能等）"
        )

        # 成片元数据里的 mock 标注：CLI JSON 已明确标注，元数据文件目前没有。
        # 这是 **MINOR 发现**（下游拿到 mp4+metadata 时可能误当真实产出），
        # 记录为可复现证据，不计入验收 15 的失败 —— 验收 15 的判据是
        # 「演示跑通 + 真实/mock 边界在报告中明确区分」，两者均已成立。
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        blob = json.dumps(meta, ensure_ascii=False)
        marked = any(k in blob for k in ("mock", "MOCK", "占位", "unverified", "未验证", "real_generation"))
        print(
            "METADATA_MOCK_MARKER="
            + json.dumps(
                {
                    "marked": marked,
                    "metadata_path": str(meta_path),
                    "finding": None
                    if marked
                    else "MINOR: 成片 metadata.json 未标注 mock/未验证；CLI JSON 报告已标注",
                },
                ensure_ascii=False,
            )
        )
        assert meta, "元数据文件必须可解析且非空"
    finally:
        _cleanup_projects(payload)


def test_workdir_carries_intermediate_media(tmp_path):
    """发现记录（MINOR，非验收 15 的失败项）：`--workdir` 只影响中间媒体文件。

    复现：
        python -m bettercomfyui.cli run --executor mock --workdir <tmp>
    实测：
        * `<tmp>` 收到 seg*.mp4 / spliced.mp4 / encoded.mp4 / concat_list.txt（中间媒体）；
        * 成片与项目数据落在 `<repo>/projects/<project_id>/output/...`（未被 --workdir 覆盖）。
    影响：端到端演示会写入仓库工作区；`--help` 里的「工作目录」语义与实现不一致。
    建议：把项目根也纳入 `--workdir`，或在 `--help`/文档中明确「媒体暂存目录 ≠ 项目根」。

    本用例只固化**可复现证据**（中间媒体确实落在 workdir），
    不把这条 MINOR 偏差算作验收 15 的失败 —— 验收 15 的判据是「演示跑通 + mock 边界标注正确」。
    """
    payload = _run_cli_demo(tmp_path)
    try:
        out_path = Path(str(payload.get("output_path") or ""))
        assert out_path.exists(), f"成片必须存在：{out_path}"
        media_root = tmp_path / "media_workdir"
        intermediate = [p for p in media_root.rglob("*") if p.is_file()] if media_root.exists() else []
        assert intermediate, (
            f"`--workdir` 未承载任何中间媒体（{media_root} 为空）—— 说明该选项完全没生效"
        )
        inside = str(out_path.resolve()).startswith(str(tmp_path.resolve()))
        print(
            "WORKDIR_SCOPE="
            + json.dumps(
                {
                    "workdir_files": len(intermediate),
                    "output_inside_workdir": inside,
                    "output_path": str(out_path),
                    "finding": None
                    if inside
                    else "MINOR: --workdir 不含项目数据与成片输出，演示会写入仓库 projects/",
                },
                ensure_ascii=False,
            )
        )
    finally:
        _cleanup_projects(payload)
