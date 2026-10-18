"""MockExecutor：无模型时证明编排闭环（契约 §7 / 任务书第 9 节末段）。

诚实性要求：
- `capabilities()["mock"] is True`，`verified=False`；
- 所有 `JobState.mock is True`；
- 产出物命名带 `.mock`，并附 `*.mock.json` 说明文件；
- `video=True` 仅表示「能在编排闭环中产出占位视频工件」，
  真实可解码性用 `video_decodable=False` 单独声明——绝不混为一谈。

产出确定性：同一 `idempotency_key` + 同一 params 必然得到同一组文件与相同字节。
"""

from __future__ import annotations

import hashlib
import json
import struct
import zlib
from pathlib import Path

from bettercomfyui.executors.base import (
    ExecutorError,
    JobRef,
    JobState,
    validate_capabilities,
)
from bettercomfyui.models.enums import ErrorClass, JobStatus

__all__ = ["MockExecutor"]

_MOCK_NOTES = (
    "MockExecutor 占位执行器：不调用任何模型，产出确定性占位文件。"
    "frame_first.png / frame_last.png 是真实可读 PNG（占位内容，非模型生成）；"
    "segment.mock.mp4 是 JSON 占位清单，不是可解码视频容器，故 video_decodable=False。"
    "video=True 仅表示『可在编排闭环中产出占位视频工件』。"
    "verified=False：mock 不代表任何真实生成能力；"
    "真实生成的已验证前置条件 = ComfyUI 装上导演节点（MiniMaxH3Director / MiniMaxH3ChainDirector 等）"
    "+ MiniMax-H3 权重 + 目标机显存 ≥24GB，本机（开发机）均未满足。"
)


def _png_bytes(width: int, height: int, seed: bytes) -> bytes:
    """生成确定性的、结构合法的 PNG（真 PNG，不是伪装）。"""
    digest = hashlib.sha256(seed).digest()
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # filter type 0
        for x in range(width):
            rows.append((digest[0] + x * 7) % 256)
            rows.append((digest[1] + y * 5) % 256)
            rows.append((digest[2] + (x + y) * 3) % 256)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + chunk(b"IEND", b"")
    )


class MockExecutor:
    """确定性占位执行器。"""

    name = "mock"

    def __init__(
        self,
        workdir: Path | str | None = None,
        *,
        seed: str = "bcui-mock",
        width: int = 64,
        height: int = 36,
        fail_times: int = 0,
        fail_error_class: ErrorClass | str = ErrorClass.COMFY_WORKFLOW,
        fail_message: str = "MockExecutor 注入的技术失败",
    ) -> None:
        self.workdir = Path(workdir) if workdir is not None else None
        self.seed = str(seed)
        self.width = int(width)
        self.height = int(height)
        self.fail_times = int(fail_times)
        self.fail_error_class = ErrorClass.parse(fail_error_class)
        self.fail_message = str(fail_message)
        self._jobs: dict[str, JobState] = {}
        self._idem: dict[str, JobRef] = {}
        self._cancelled: set[str] = set()
        self._submit_count = 0

    # -- 能力声明 ---------------------------------------------------------
    def capabilities(self) -> dict:
        caps = {
            "image": True,
            "video": True,
            "first_last_frame": True,
            "tail_frames": 22,
            "verified": False,
            "notes": _MOCK_NOTES,
            "mock": True,
            "video_decodable": False,
            "workdir": str(self.workdir) if self.workdir is not None else None,
        }
        return validate_capabilities(caps)

    # -- 生命周期 ---------------------------------------------------------
    def submit(self, workflow: dict, params: dict, *, idempotency_key: str) -> JobRef:
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ExecutorError(
                "idempotency_key 必须是非空字符串（防止重复提交）",
                error_class=ErrorClass.INPUT_VALIDATION,
            )
        cached = self._idem.get(idempotency_key)
        if cached is not None:
            return cached

        self._submit_count += 1
        if self._submit_count <= self.fail_times:
            raise ExecutorError(
                self.fail_message,
                error_class=self.fail_error_class,
                detail={"submit_count": self._submit_count, "injected": True},
            )

        payload = json.dumps(
            {"workflow": workflow, "params": params, "key": idempotency_key},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        digest = hashlib.sha256((self.seed + payload).encode("utf-8")).hexdigest()
        job_id = f"MOCKJOB_{digest[:16]}"
        ref = JobRef(job_id=job_id, executor=self.name)
        self._idem[idempotency_key] = ref
        self._jobs[job_id] = self._materialize(job_id, digest, params)
        return ref

    def poll(self, ref: JobRef) -> JobState:
        if ref.job_id in self._cancelled:
            return JobState(
                status=JobStatus.CANCELLED,
                progress=None,
                outputs=[],
                error="mock 作业已取消",
                error_class=ErrorClass.USER_CANCELLED,
                mock=True,
            )
        state = self._jobs.get(ref.job_id)
        if state is None:
            return JobState(
                status=JobStatus.FAILED,
                progress=None,
                outputs=[],
                error=f"未知 mock 作业：{ref.job_id}",
                error_class=ErrorClass.COMFY_WORKFLOW,
                mock=True,
            )
        return state

    def cancel(self, ref: JobRef) -> bool:
        if ref.job_id not in self._jobs:
            return False
        self._cancelled.add(ref.job_id)
        return True

    # -- 内部 -------------------------------------------------------------
    def _job_dir(self, job_id: str) -> Path:
        base = self.workdir
        if base is None:
            import tempfile

            base = Path(tempfile.gettempdir()) / "bcui_mock"
        out = base / job_id
        out.mkdir(parents=True, exist_ok=True)
        return out

    def _materialize(self, job_id: str, digest: str, params: dict) -> JobState:
        out = self._job_dir(job_id)
        first = out / "frame_first.png"
        last = out / "frame_last.png"
        video = out / "segment.mock.mp4"
        manifest = out / "mock_manifest.json"

        first.write_bytes(_png_bytes(self.width, self.height, (digest + ":first").encode()))
        last.write_bytes(_png_bytes(self.width, self.height, (digest + ":last").encode()))
        video.write_text(
            json.dumps(
                {
                    "mock": True,
                    "kind": "placeholder-video-manifest",
                    "decodable_container": False,
                    "job_id": job_id,
                    "frames": [first.name, last.name],
                    "params": params,
                },
                sort_keys=True,
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        manifest.write_text(
            json.dumps(
                {
                    "mock": True,
                    "executor": self.name,
                    "job_id": job_id,
                    "digest": digest,
                    "warning": "本目录全部产出均为占位文件，不是真实模型生成结果，禁止当真实成片使用。",
                },
                sort_keys=True,
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return JobState(
            status=JobStatus.SUCCEEDED,
            progress=1.0,
            outputs=[first, last, video],
            error=None,
            error_class=None,
            mock=True,
        )
