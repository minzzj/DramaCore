"""ComfyUI HTTP 执行器（契约 §7）。

接口：`/prompt` 提交、`/history` 轮询、`/interrupt` 取消、`/object_info` 能力探测。

红线：
- 连不上时**抛 `ExecutorError(error_class=COMFY_WORKFLOW)`**（由调用方映射为
  `JobStatus.FAILED`），绝不返回一个假成功的 JobRef，也绝不把失败当 UNKNOWN 静默放过。
- 轮询期网络抖动重试耗尽后返回 `JobStatus.UNKNOWN` + 说明（UNKNOWN 不是成功）。
- `idempotency_key` 去重：同 key 重复提交只返回同一 JobRef。
- 本机开发环境：ComfyUI 已装 **0.41.0**，四个导演台包已安装；但 `models/` 无任何权重，
  因此真实生成能力一律 `verified=False` 并在 notes 写明前置条件。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from bettercomfyui.executors.base import (
    ExecutorError,
    ExecutorUnavailable,
    JobRef,
    JobState,
    validate_capabilities,
)
from bettercomfyui.models.enums import ErrorClass, JobStatus

__all__ = [
    "ComfyUIHttpExecutor",
    "HttpResponse",
    "HttpTransport",
    "UrllibTransport",
    "read_comfyui_version",
    "resolve_comfyui_endpoint",
]

DEFAULT_BASE_URL = "http://127.0.0.1:8000"


def read_comfyui_version(base_path: str | Path | None) -> str | None:
    """从配置的 ComfyUI 实例读取真实版本号。

    **绝不硬编码版本**。此前这里写着 `LOCAL_COMFYUI_VERSION = "0.22.3"`，
    而开发环境早已换成 0.41.0 —— 结果是 `doctor` 报 0.41.0，
    而 `demo` 的 `real_generation_reason` 仍写「本机 ComfyUI 0.22.3 < 0.30」，
    **同一台机器上两个组件说两个版本**，用户无法判断该信哪个。

    读不到时返回 `None`（调用方必须如实说"未知"，不要猜）。
    """
    if not base_path:
        return None
    ver_file = Path(base_path) / "comfyui_version.py"
    if not ver_file.is_file():
        return None
    try:
        for line in ver_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if "__version__" in line and "=" in line:
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        return None
    return None


def _version_tuple(text: str, *, width: int = 4) -> tuple[int, ...]:
    """把 "0.41.0" 变成定长 (0, 41, 0, 0) 以便**数值**比较。

    两处坑都要避开：

    1. **字符串比较是错的**：`"0.5" >= "0.30"` 为 True（'5' > '3'），
       但 0.5 < 0.30 在版本语义下应为 False。
    2. **不定长元组比较是错的**：Python 里 `(0, 30) >= (0, 30, 0)` 是 **False**
       （前缀相等时短元组更小）。于是 `meets_min_comfyui("0.30")` 会返回 False ——
       **一个版本竟然不满足它自己的下限**。实测抓到的，所以这里补齐到定长。
    """
    parts: list[int] = []
    for chunk in (text or "").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    if len(parts) < width:
        parts.extend([0] * (width - len(parts)))
    return tuple(parts)


def meets_min_comfyui(version: str | None, minimum: str = "0.30.0") -> bool:
    """版本是否满足下限。`version` 为 None -> **False**（不知道就不算满足）。"""
    if not version:
        return False
    return _version_tuple(version) >= _version_tuple(minimum)

#: 目标机（非本开发机）前置条件，写入 capabilities().notes。
_TARGET_PREREQUISITES = (
    "真实生成前置条件：① ComfyUI 已启动且 /object_info 可达；"
    "② custom_nodes 安装导演节点（MiniMaxH3Director / MiniMaxH3ChainDirector / "
    "MiniMaxH3SmartDirector / H3PromptSidecar）；③ 存放 MiniMax-H3 视频/音频 VAE、"
    "CLIP、UNET 权重；④ 目标机显存 ≥24GB（本开发机为 GTX 1050 Ti 4GB，仅用于开发）。"
    "以上均未在目标机验证，故 verified=False。"
)


class HttpResponse(Protocol):
    status: int
    body: Any


class HttpTransport(Protocol):
    """可注入的 HTTP 传输层（离线测试用；测试绝不发真实网络请求）。"""

    def request(
        self, method: str, url: str, *, json_body: dict | None = None, timeout: float | None = None
    ) -> tuple[int, Any]: ...


class UrllibTransport:
    """默认传输层：标准库 urllib，无第三方依赖。"""

    def request(
        self, method: str, url: str, *, json_body: dict | None = None, timeout: float | None = None
    ) -> tuple[int, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or 30.0) as resp:  # noqa: S310 - 本机回环
                raw = resp.read()
                status = int(resp.status)
        except urllib.error.HTTPError as exc:  # 4xx/5xx：读取 body 以便报错
            raw = exc.read() if hasattr(exc, "read") else b""
            status = int(exc.code)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ExecutorUnavailable(
                f"无法连接 ComfyUI：{url}（{exc}）",
                error_class=ErrorClass.COMFY_WORKFLOW,
                detail={"url": url, "method": method},
            ) from exc
        body: Any = None
        if raw:
            try:
                body = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                body = raw.decode("utf-8", errors="replace")
        return status, body


def resolve_comfyui_endpoint(cfg: Any = None, *, default: str = DEFAULT_BASE_URL) -> str:
    """从全局配置解析 ComfyUI 的 HTTP 地址（图一 SG7 global.comfyui_endpoint）。

    ## 为什么需要它（修一个"配了不读"）

    global.comfyui_endpoint 在 config/user.yaml 里早就写着
    http://127.0.0.1:8188，但**产品代码零读取** ——
    执行器一直用硬编码的 DEFAULT_BASE_URL = http://127.0.0.1:8000（端口都不对）。
    用户改了配置发现不生效，这种"假可配置"比没有配置更糟。

    ## 注意：comfyui_base_path 与 comfyui_endpoint 不是一回事

    * comfyui_base_path = ComfyUI 的**安装目录**（找权重/节点用，文件系统路径）；
    * comfyui_endpoint  = ComfyUI 的**HTTP 地址**（提交任务/探测用）。

    两者作用不同，**不应该合并**（ROUND19 曾误判为重复，已更正）。

    校验：必须以 http:// 或 https:// 开头；空值退回默认并保持可用。
    """
    raw = getattr(getattr(cfg, "global_", None), "comfyui_endpoint", None)
    text = str(raw or "").strip()
    if not text:
        return default
    if not text.startswith(("http://", "https://")):
        raise ExecutorError(
            f"global.comfyui_endpoint 必须以 http:// 或 https:// 开头，收到 {text!r}",
            error_class=ErrorClass.INPUT_VALIDATION,
        )
    return text.rstrip("/")


class ComfyUIHttpExecutor:
    """ComfyUI HTTP 执行器。"""

    name = "comfyui_http"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        transport: HttpTransport | None = None,
        timeout_s: float = 30.0,
        poll_interval_s: float = 1.0,
        poll_max_attempts: int = 3,
        output_dir: Path | str | None = None,
        client_id: str = "bettercomfyui",
        expected_comfyui_version: str | None = None,
        submit_grace_s: float = 10.0,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.transport: HttpTransport = transport or UrllibTransport()
        self.timeout_s = float(timeout_s)
        self.poll_interval_s = float(poll_interval_s)
        self.poll_max_attempts = int(poll_max_attempts)
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self.client_id = str(client_id)
        #: 提交后多久之内「查不到作业」算正常（而不是作业丢失）。
        #: 见 `poll()` 里的说明：提交与入队之间存在真实的时间窗口。
        self.submit_grace_s = float(submit_grace_s)
        # 版本来源优先级：显式入参 > 自动探测（不硬编码）
        self.expected_comfyui_version = expected_comfyui_version or "unknown"
        self._idem: dict[str, JobRef] = {}
        self._submitted_at: dict[str, float] = {}
        self._last_probe: dict[str, Any] | None = None

    def _within_submit_grace(self, ref: JobRef) -> bool:
        """该作业是否仍在「提交后宽限期」内。

        用 `time.monotonic()` 而不是 `time.time()`：单调时钟不受系统时间调整影响。
        """
        import time as _time

        t0 = self._submitted_at.get(ref.job_id)
        if t0 is None:
            return False
        return (_time.monotonic() - t0) < self.submit_grace_s

    # -- 传输辅助 ---------------------------------------------------------
    def _request(
        self, method: str, path: str, *, json_body: dict | None = None
    ) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        try:
            return self.transport.request(
                method, url, json_body=json_body, timeout=self.timeout_s
            )
        except ExecutorError:
            raise
        except Exception as exc:  # 传输层的任何异常都属于 ComfyUI 工作流层技术失败
            raise ExecutorUnavailable(
                f"ComfyUI 请求失败：{method} {url}（{type(exc).__name__}: {exc}）",
                error_class=ErrorClass.COMFY_WORKFLOW,
                detail={"url": url, "method": method},
            ) from exc

    # -- 提交 -------------------------------------------------------------
    def submit(self, workflow: dict, params: dict, *, idempotency_key: str) -> JobRef:
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ExecutorError(
                "idempotency_key 必须是非空字符串（幂等键缺失会造成重复提交）",
                error_class=ErrorClass.INPUT_VALIDATION,
            )
        cached = self._idem.get(idempotency_key)
        if cached is not None:
            return cached

        payload = {
            "prompt": workflow,
            "client_id": self.client_id,
            "extra_data": {"idempotency_key": idempotency_key, "params": params},
        }
        status, body = self._request("POST", "/prompt", json_body=payload)
        if status >= 400:
            error_class = (
                ErrorClass.INPUT_VALIDATION if 400 <= status < 500 else ErrorClass.COMFY_WORKFLOW
            )
            raise ExecutorError(
                f"ComfyUI /prompt 返回 HTTP {status}：{_short(body)}",
                error_class=error_class,
                detail={"status": status, "idempotency_key": idempotency_key},
            )
        if not isinstance(body, dict):
            raise ExecutorError(
                f"ComfyUI /prompt 返回体不是 JSON 对象：{_short(body)}",
                error_class=ErrorClass.COMFY_WORKFLOW,
            )
        node_errors = body.get("node_errors") or {}
        prompt_id = body.get("prompt_id")
        if node_errors:
            raise ExecutorError(
                f"ComfyUI /prompt 校验失败（node_errors）：{_short(node_errors)}",
                error_class=ErrorClass.COMFY_WORKFLOW,
                detail={"node_errors": node_errors},
            )
        if not prompt_id:
            raise ExecutorError(
                f"ComfyUI /prompt 未返回 prompt_id（禁止当作提交成功）：{_short(body)}",
                error_class=ErrorClass.COMFY_WORKFLOW,
            )
        ref = JobRef(job_id=str(prompt_id), executor=self.name)
        self._idem[idempotency_key] = ref
        # 记录提交时刻：供 poll() 判断「还没出现」是否仍属正常窗口
        import time as _time

        self._submitted_at[str(prompt_id)] = _time.monotonic()
        return ref

    # -- 轮询 -------------------------------------------------------------
    def poll(self, ref: JobRef) -> JobState:
        last_error: str | None = None
        for attempt in range(max(1, self.poll_max_attempts)):
            try:
                status, body = self._request("GET", f"/history/{ref.job_id}")
            except ExecutorError as exc:
                last_error = str(exc)
                continue
            if status >= 400:
                last_error = f"/history 返回 HTTP {status}：{_short(body)}"
                continue
            entry = None
            if isinstance(body, dict):
                entry = body.get(ref.job_id)
                if entry is None and body and all(isinstance(v, dict) for v in body.values()):
                    # 部分版本不带 prompt_id 键直接返回作业体
                    entry = next(iter(body.values())) if len(body) == 1 else None
            if entry is None:
                try:
                    queued = self._queue_state(ref.job_id)
                except ExecutorError as exc:
                    # 查询失败 ≠ 作业不存在：只报「无法判定」，交给外层重试（绝不归因成「作业丢失」）
                    last_error = str(exc)
                    continue
                if queued is not None:
                    return queued
                # ⚠️ 提交后的**短暂窗口**里，作业可能既不在 /history 也不在 /queue。
                # 真实原因：/prompt 返回 prompt_id 之后，作业要经过一小段
                # 「已受理、尚未入队/开始执行」的过程。此时立刻轮询会两头都查不到。
                #
                # 实测抓到的 bug：紧接 submit 调 poll 会返回
                # `FAILED（history 与 queue 中都不存在作业）` —— 而作业其实成功产出了。
                # 把「还没出现」误判成「已丢失」会让**正常作业被当成失败**，
                # 进而触发不必要的重试甚至 CNT 记账。
                #
                # 正确做法：宽限期内返回 UNKNOWN（诚实地说"还不知道"），
                # 只有超过宽限期仍查不到，才判定作业丢失。
                if self._within_submit_grace(ref):
                    return JobState(
                        status=JobStatus.UNKNOWN,
                        progress=None,
                        outputs=[],
                        error=(
                            f"作业 {ref.job_id} 尚未出现在 ComfyUI 的 history/queue 中"
                            f"（提交后 {self.submit_grace_s:.1f}s 宽限期内，属正常现象）；"
                            "请稍后再次轮询。这是「状态未知」，不是「已失败」。"
                        ),
                        error_class=ErrorClass.COMFY_WORKFLOW,
                    )
                # 宽限期已过，/queue 查询成功且其中确实没有该作业 -> 作业丢失
                return JobState(
                    status=JobStatus.FAILED,
                    progress=None,
                    outputs=[],
                    error=(
                        f"ComfyUI history 与 queue 中都不存在作业 {ref.job_id}"
                        f"（已超过提交后 {self.submit_grace_s:.1f}s 宽限期，可能被重启清除）；"
                        "按技术失败处理"
                    ),
                    error_class=ErrorClass.COMFY_WORKFLOW,
                )
            return self._state_from_history(ref, entry)
        # 重试耗尽：无法判定 -> UNKNOWN（带说明；调用方必须按失败处理）
        return JobState(
            status=JobStatus.UNKNOWN,
            progress=None,
            outputs=[],
            error=f"轮询 ComfyUI /history 连续 {self.poll_max_attempts} 次失败：{last_error}",
            error_class=ErrorClass.COMFY_WORKFLOW,
        )

    def _queue_state(self, job_id: str) -> JobState | None:
        """查询 ``/queue``。

        返回 ``None`` **仅**表示「查询成功，且队列里确实没有该作业」（= 作业丢失）；
        查询本身失败（连不上 / HTTP >= 400 / 返回体不是 JSON 对象 / 缺少 ``queue_*`` 键）
        一律**抛** :class:`ExecutorError`：一次瞬时故障绝不能被归因成「作业不存在」，
        调用方（``poll``）据此按「状态未知、可重试」处理。
        """
        try:
            status, body = self._request("GET", "/queue")
        except ExecutorError as exc:
            raise ExecutorError(
                f"查询 ComfyUI /queue 失败（瞬时故障），无法判定作业 {job_id} 是否仍在队列中；"
                f"按「状态未知/可重试」处理，绝不当作「作业不存在」：{exc}",
                error_class=ErrorClass.COMFY_WORKFLOW,
                detail={"job_id": job_id, "cause": type(exc).__name__},
            ) from exc
        if status >= 400 or not isinstance(body, dict):
            raise ExecutorError(
                f"ComfyUI /queue 返回无法判定的响应（HTTP {status}，body={_short(body)}）："
                f"无法判定作业 {job_id} 是否仍在队列中；按「状态未知/可重试」处理",
                error_class=ErrorClass.COMFY_WORKFLOW,
                detail={"job_id": job_id, "status": status},
            )
        if "queue_running" not in body and "queue_pending" not in body:
            raise ExecutorError(
                f"ComfyUI /queue 响应缺少 queue_running/queue_pending 键（body={_short(body)}）："
                f"无法判定作业 {job_id} 是否仍在队列中；按「状态未知/可重试」处理",
                error_class=ErrorClass.COMFY_WORKFLOW,
                detail={"job_id": job_id, "status": status},
            )
        running = [item for item in body.get("queue_running", []) if _job_in_queue_item(item, job_id)]
        pending = [item for item in body.get("queue_pending", []) if _job_in_queue_item(item, job_id)]
        if running:
            return JobState(status=JobStatus.RUNNING, progress=None, outputs=[], error=None)
        if pending:
            return JobState(status=JobStatus.QUEUED, progress=None, outputs=[], error=None)
        return None

    def _state_from_history(self, ref: JobRef, entry: dict) -> JobState:
        status_field = entry.get("status") or {}
        status_str = str(status_field.get("status_str", "")).lower()
        completed = bool(status_field.get("completed"))
        messages = status_field.get("messages") or []
        outputs = self._collect_outputs(entry.get("outputs") or {})
        if status_str == "error":
            return JobState(
                status=JobStatus.FAILED,
                progress=None,
                outputs=outputs,
                error=f"ComfyUI 执行失败：{_short(messages)}",
                error_class=ErrorClass.COMFY_WORKFLOW,
            )
        if not completed and status_str not in ("success", ""):
            return JobState(
                status=JobStatus.RUNNING,
                progress=None,
                outputs=outputs,
                error=None,
            )
        if not completed:
            return JobState(
                status=JobStatus.FAILED,
                progress=None,
                outputs=outputs,
                error=f"ComfyUI 作业 {ref.job_id} history 无 completed 标记：{_short(status_field)}",
                error_class=ErrorClass.COMFY_WORKFLOW,
            )
        if not outputs:
            return JobState(
                status=JobStatus.FAILED,
                progress=None,
                outputs=[],
                error=f"ComfyUI 作业 {ref.job_id} 标记完成但没有任何输出文件（禁止当作成功）",
                error_class=ErrorClass.FILE_ENCODING,
            )
        return JobState(status=JobStatus.SUCCEEDED, progress=1.0, outputs=outputs, error=None)

    def _collect_outputs(self, outputs: dict) -> list[Path]:
        found: list[Path] = []
        for node_out in outputs.values():
            if not isinstance(node_out, dict):
                continue
            for kind in ("images", "gifs", "videos", "audio", "files"):
                for item in node_out.get(kind) or []:
                    if not isinstance(item, dict):
                        continue
                    filename = item.get("filename")
                    if not filename:
                        continue
                    sub = item.get("subfolder") or ""
                    if self.output_dir is not None:
                        found.append(self.output_dir / sub / str(filename) if sub else self.output_dir / str(filename))
                    else:
                        found.append(Path(str(filename)))
        return found

    # -- 取消 -------------------------------------------------------------
    def cancel(self, ref: JobRef) -> bool:
        try:
            status, _ = self._request("POST", "/interrupt", json_body={})
        except ExecutorError:
            status = 0
        if 200 <= int(status) < 300:
            return True
        try:
            status, _ = self._request("POST", "/queue", json_body={"delete": [ref.job_id]})
        except ExecutorError:
            return False
        return 200 <= int(status) < 300

    # -- 显存管理（阶段化调度用）------------------------------------------
    def vram_snapshot(self) -> dict | None:
        """取一次显存快照。取不到返回 ``None``（**不编造数字**）。

        返回 ``{"total_gb", "free_gb", "used_gb", "device", "raw"}``。
        """
        try:
            status, body = self._request("GET", "/system_stats")
        except ExecutorError:
            return None
        if not (200 <= int(status) < 300) or not isinstance(body, dict):
            return None
        devices = body.get("devices") or []
        if not devices or not isinstance(devices[0], dict):
            return None
        dev = devices[0]
        try:
            total = float(dev.get("vram_total") or 0)
            free = float(dev.get("vram_free") or 0)
        except (TypeError, ValueError):
            return None
        if total <= 0:
            return None
        gb = 1024.0 ** 3
        return {
            "device": dev.get("name"),
            "total_gb": round(total / gb, 3),
            "free_gb": round(free / gb, 3),
            "used_gb": round((total - free) / gb, 3),
            "raw": dev,
        }

    def free_models(self, *, verify: bool = True, settle_s: float = 1.0) -> dict:
        """让 ComfyUI **卸载模型并释放显存**（`POST /free`）。

        这是阶段化显存调度的物理基础：视频生成模型与审核模型不同时驻留，
        每次切换前必须真的把上一个卸掉，否则"省显存"只是口号。

        参数
        ----
        verify:
            为 True 时前后各取一次显存快照，**核对已用显存确实下降**。
            没下降时 ``verified=False`` + ``reason``，**不假装释放成功**
            （真实原因可能是引用未释放、有后台任务、或设备不支持）。
        settle_s:
            两帧快照之间的等待秒数（驱动释放需要一点时间）。

        返回::

            {
              "ok": bool,            # /free 调用是否 2xx
              "status": int,
              "before": {...} | None,
              "after":  {...} | None,
              "freed_gb": float | None,
              "verified": bool,      # 是否核验到下降
              "reason": str,
            }
        """
        before = self.vram_snapshot() if verify else None
        try:
            status, _body = self._request(
                "POST", "/free",
                json_body={"unload_models": True, "free_memory": True},
            )
        except ExecutorError as exc:
            return {
                "ok": False, "status": 0, "before": before, "after": None,
                "freed_gb": None, "verified": False,
                "reason": f"/free 调用失败：{exc}",
            }

        ok = 200 <= int(status) < 300
        if not verify:
            return {"ok": ok, "status": int(status), "before": None, "after": None,
                    "freed_gb": None, "verified": False,
                    "reason": "未要求核验（verify=False）"}

        if settle_s > 0:
            import time as _time

            _time.sleep(settle_s)
        after = self.vram_snapshot()
        if before is None or after is None:
            return {
                "ok": ok, "status": int(status), "before": before, "after": after,
                "freed_gb": None, "verified": False,
                "reason": "取不到显存快照，无法核验是否真的释放",
            }
        freed = round(float(after["used_gb"]) - float(before["used_gb"]), 3)
        # 注意方向：释放后 used 应该**变小**，所以 freed 为负才是"降了"
        dropped = freed < -0.05
        return {
            "ok": ok, "status": int(status), "before": before, "after": after,
            "freed_gb": round(-freed, 3), "verified": dropped,
            "reason": ("已核验：已用显存下降 %.3f GB" % (-freed)) if dropped
                      else ("未观测到显存下降（已用变化 %.3f GB）—— 不假装释放成功" % freed),
        }

    # -- 能力探测 ---------------------------------------------------------
    def probe(self) -> dict:
        """真实探测本机 ComfyUI：/system_stats + /object_info。任何项都如实记录。"""
        info: dict[str, Any] = {
            "base_url": self.base_url,
            "reachable": False,
            "comfyui_version": None,
            "node_type_count": None,
            "node_types": [],
            "checkpoints": [],
            "error": None,
            # probe 是原始诊断视图，也必须如实声明「未验证真实生成」（R8）
            "verified": False,
            "mock": False,
            "verified_reason": "probe 只做连通性与节点探测，不代表真实生成能力已验证",
        }
        try:
            status, body = self._request("GET", "/system_stats")
        except ExecutorError as exc:
            info["error"] = f"{exc.error_class}: {exc}"
            self._last_probe = info
            return info
        if status >= 400 or not isinstance(body, dict):
            info["error"] = f"/system_stats HTTP {status}: {_short(body)}"
            self._last_probe = info
            return info
        info["reachable"] = True
        system = (body.get("system") or {}) if isinstance(body.get("system"), dict) else {}
        info["comfyui_version"] = system.get("comfyui_version") or body.get("comfyui_version")
        info["devices"] = body.get("devices")

        try:
            status, obj = self._request("GET", "/object_info")
        except ExecutorError as exc:
            info["error"] = f"/object_info 不可用：{exc}"
            self._last_probe = info
            return info
        if status >= 400 or not isinstance(obj, dict):
            info["error"] = f"/object_info HTTP {status}: {_short(obj)}"
            self._last_probe = info
            return info
        node_types = sorted(str(k) for k in obj.keys())
        info["node_types"] = node_types
        info["node_type_count"] = len(node_types)
        info["checkpoints"] = _combo_options(obj, "CheckpointLoaderSimple", "ckpt_name")
        info["video_nodes_present"] = sorted(
            n for n in node_types if n in _VIDEO_NODE_HINTS
        )
        info["director_nodes_present"] = sorted(
            n for n in node_types if n in _DIRECTOR_NODE_HINTS
        )
        self._last_probe = info
        return info

    def capabilities(self) -> dict:
        probe = self.probe()
        reachable = bool(probe.get("reachable"))
        has_video_nodes = bool(probe.get("video_nodes_present"))
        has_models = bool(probe.get("checkpoints")) or bool(probe.get("director_nodes_present"))
        image = reachable and has_video_nodes
        video = reachable and has_video_nodes and has_models
        notes = (
            f"ComfyUI {self.base_url}：reachable={reachable}"
            f"，检测到节点类型 {probe.get('node_type_count')} 个"
            f"，版本 {probe.get('comfyui_version')!r}，checkpoint 数 {len(probe.get('checkpoints') or [])}。"
            "verified=False：" + _TARGET_PREREQUISITES
        )
        if probe.get("error"):
            notes += f"；探测错误：{probe['error']}"
        caps = {
            "image": bool(image),
            "video": bool(video),
            "first_last_frame": bool(video),
            "tail_frames": 22 if video else None,
            "verified": False,  # 本机没有任何模型权重与导演节点，真实生成未验证
            "notes": notes,
            "mock": False,
            "reachable": reachable,
            "verified_reason": "本机未验证：无模型权重 / custom_nodes 为空（真实生成能力从未成功跑通）",
            "comfyui_version": probe.get("comfyui_version"),
            "expected_comfyui_version": self.expected_comfyui_version,
            "node_type_count": probe.get("node_type_count"),
            "checkpoints": probe.get("checkpoints") or [],
            "video_nodes_present": probe.get("video_nodes_present") or [],
            "director_nodes_present": probe.get("director_nodes_present") or [],
            "probe": probe,
        }
        return validate_capabilities(caps)


_VIDEO_NODE_HINTS = {
    "MiniMaxH3Director",
    "ComfyMiniMaxH3Director",
    "MiniMaxH3ChainDirector",
    "MiniMaxH3ChainDirectorEN",
    "MiniMaxH3SmartDirector",
    "MiniMaxH3SmartDirectorAllInOne",
    "CreateVideo",
    "SaveVideo",
    "VHS_VideoCombine",
    "SaveWEBM",
    "SaveAnimatedWEBP",
}

_DIRECTOR_NODE_HINTS = {
    "MiniMaxH3Director",
    "ComfyMiniMaxH3Director",
    "MiniMaxH3ChainDirector",
    "MiniMaxH3ChainDirectorEN",
    "MiniMaxH3SmartDirector",
    "MiniMaxH3SmartDirectorAllInOne",
    "MiniMaxH3SmartPlanInspector",
    "H3PromptSidecar",
    "H3ChineseDirector",
}


def _job_in_queue_item(item: Any, job_id: str) -> bool:
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        return str(item[1]) == job_id or str(item[1]) == job_id
    if isinstance(item, dict):
        return str(item.get("prompt_id")) == job_id
    return False


def _combo_options(object_info: dict, node: str, field: str) -> list[str]:
    spec = object_info.get(node)
    if not isinstance(spec, dict):
        return []
    required = (spec.get("input") or {}).get("required") or {}
    entry = required.get(field)
    if isinstance(entry, (list, tuple)) and entry and isinstance(entry[0], list):
        return [str(x) for x in entry[0]]
    return []


def _short(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."
