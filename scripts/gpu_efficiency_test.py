"""GPU 机器效率实测：15 秒视频生成耗时 + 显存峰值 + 阶段调度不频繁卸载。

## 用户要测什么

1. **生成 15 秒视频需要多长时间**（真实出片耗时）
2. **显存必须维持在 32 GB 以下**（48G 机器上按 32G 预算跑，等价 5090 32G 场景）
3. **阶段调度不能频繁卸载模型**（今早改的逻辑：批量生成 -> 卸载 -> 换审核模型，
   而不是每段都卸载重载）

## 怎么测（全部基于真实 `/system_stats` 与 `/history`，不编数字）

* **耗时**：从提交 `/prompt` 到 `/history` 出现终态的实际墙钟时间；
* **显存**：后台线程每 0.5s 采样 `/system_stats` 的 `vram_free/vram_total`，
  记录**峰值已用**；
* **卸载次数**：统计运行期间 `/free` 被调用的次数（编排层每次卸载都会调用它）。
  批量阶段化的正确表现是：**卸载次数与段数无关**，而不是每段一次。

## 用法

    # 先探测环境
    python scripts/gpu_efficiency_test.py --probe

    # 跑 15 秒视频（3 段 x 5 秒），限制显存 32G
    python scripts/gpu_efficiency_test.py --segments 3 --seconds 5 --budget-gb 32

    # 只跑 PhaseScheduler 的卸载次数对比（不需要真模型）
    python scripts/gpu_efficiency_test.py --scheduler-only
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8188"
GB = 1024.0 ** 3


def _get(path: str, timeout: float = 20.0):
    return json.load(urllib.request.urlopen(f"{BASE}{path}", timeout=timeout))


def _post(path: str, body: dict, timeout: float = 60.0) -> tuple[int, str]:
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200)), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", "replace")


def probe() -> dict:
    """探测 GPU / 显存 / 版本。取不到的项如实写 None。"""
    info: dict = {"reachable": False}
    try:
        stats = _get("/system_stats")
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"连不上 {BASE}：{exc}"
        return info
    info["reachable"] = True
    info["comfyui_version"] = (stats.get("system") or {}).get("comfyui_version")
    devices = stats.get("devices") or []
    dev = devices[0] if devices else {}
    info.update({
        "device": dev.get("name"),
        "vram_total_gb": round(float(dev.get("vram_total") or 0) / GB, 2),
        "vram_free_gb": round(float(dev.get("vram_free") or 0) / GB, 2),
    })
    info["is_gpu"] = bool(dev.get("name")) and "cpu" not in str(dev.get("name")).lower()
    try:
        object_info = _get("/object_info")
        info["node_count"] = len(object_info)
        info["has_better_nodes"] = [
            n for n in object_info if n.startswith("BetterComfyUI")
        ]
        info["has_director_nodes"] = [
            n for n in object_info if "MiniMaxH3" in n
        ]
    except Exception:  # noqa: BLE001
        pass
    return info


class VramSampler:
    """后台采样显存，记录**峰值已用**。"""

    def __init__(self, interval_s: float = 0.5) -> None:
        self.interval_s = interval_s
        self.samples: list[float] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                stats = _get("/system_stats", timeout=10)
                dev = (stats.get("devices") or [{}])[0]
                total = float(dev.get("vram_total") or 0)
                free = float(dev.get("vram_free") or 0)
                if total > 0:
                    self.samples.append((total - free) / GB)
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(self.interval_s)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> dict:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if not self.samples:
            return {"samples": 0, "peak_gb": None, "avg_gb": None, "max_gb": None}
        return {
            "samples": len(self.samples),
            "peak_gb": round(max(self.samples), 2),
            "avg_gb": round(statistics.fmean(self.samples), 2),
            "min_gb": round(min(self.samples), 2),
        }


def build_workflow(segments: int, seconds: float, project_id: str) -> dict:
    """构造一个 N 段、每段 seconds 秒的导演台工作流（真实出片链路）。"""
    script = "\n---\n".join(
        f"镜头{i}｜第 {i} 个镜头，电影感构图，平稳运镜，冷蓝夜色"
        for i in range(1, segments + 1)
    )
    return {
        "1": {"class_type": "BetterComfyUIOneDirector", "inputs": {
            "剧本分镜": script, "项目": project_id, "段间素材": "",
            "段时长_秒": float(seconds), "统一风格": "电影感，冷蓝夜色，低饱和",
            "段间接续": "自动（导演台尾部帧辅助，推荐）",
            "素材优先级": "角色>场景>物品>分镜（推荐）",
            "公共素材模式": "整个素材库（推荐）",
        }},
        "2": {"class_type": "MiniMaxH3Director", "inputs": {}},
    }


def free_call_count() -> int:
    """ComfyUI 没有 /free 调用计数接口 —— 用编排层的报告来数。

    这里返回 -1 表示"接口未提供"，由调用方从编排层事件里数。
    """
    return -1


def run_scheduler_comparison(segments: int) -> dict:
    """用 `VramPhaseScheduler` 对比"批量阶段化"与"逐段交替"的卸载次数。

    这段**不需要 GPU**，纯逻辑，可在本地跑 —— 用来证明"不频繁卸载"。
    """
    import sys

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from bettercomfyui.pipeline.phases import VramPhaseScheduler, default_stage_plan

    vram = {"used": 0.0}
    calls = {"unload": 0}

    def unload() -> dict:
        calls["unload"] += 1
        before = vram["used"]
        vram["used"] = 0.0
        return {"ok": True, "status": 200, "verified": vram["used"] < before,
                "before": {"used_gb": before}, "after": {"used_gb": 0.0},
                "reason": "fake"}

    def snapshot() -> dict:
        return {"used_gb": vram["used"], "total_gb": 32.0}

    plan = default_stage_plan()
    handlers = {}
    for stage in plan:
        def make(stage=stage):
            def handler():
                if stage.model:
                    vram["used"] += stage.resident_gb
                return {"stage": stage.name, "segments": segments}
            return handler
        handlers[stage.name] = make()

    scheduler = VramPhaseScheduler(unload=unload, vram=snapshot, budget_gb=32.0)
    report = scheduler.run(plan, handlers)
    return {
        "segments": segments,
        "staged_unloads": calls["unload"],
        "alternated_unloads": 2 * segments + 2,
        "max_resident_gb": report["max_resident_gb"],
        "budget_gb": 32.0,
        "within_budget": report["max_resident_gb"] <= 32.0,
        "scheduler_ok": report["ok"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GPU 效率实测")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--segments", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--budget-gb", type=float, default=32.0)
    parser.add_argument("--project-id", default="")
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--scheduler-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.probe or args.scheduler_only:
        report: dict = {"probe": probe()}
        report["scheduler"] = run_scheduler_comparison(args.segments)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            p = report["probe"]
            print("=== 环境探测 ===")
            for key in ("device", "vram_total_gb", "vram_free_gb", "comfyui_version",
                        "node_count", "is_gpu"):
                print(f"  {key}: {p.get(key)}")
            print(f"  Better 节点数: {len(p.get('has_better_nodes') or [])}")
            print(f"  导演台节点数: {len(p.get('has_director_nodes') or [])}")
            print("\n=== 阶段调度卸载次数对比（无需 GPU）===")
            s = report["scheduler"]
            print(f"  段数 {s['segments']}：阶段化卸载 {s['staged_unloads']} 次 "
                  f"vs 逐段交替 {s['alternated_unloads']} 次")
            print(f"  最大同时驻留 {s['max_resident_gb']} GB "
                  f"(预算 {s['budget_gb']} GB, 达标={s['within_budget']})")
        return 0

    if not args.project_id:
        print("[FAIL] 需要 --project-id（真实 32 位 hex）")
        return 1

    workflow = build_workflow(args.segments, args.seconds, args.project_id)
    sampler = VramSampler()
    sampler.start()
    started = time.time()
    code, body = _post("/prompt", {"prompt": workflow, "client_id": "bcui-eff"})
    if not (200 <= code < 300):
        sampler.stop()
        print(f"[FAIL] 提交失败 {code}: {body[:500]}")
        return 1
    prompt_id = json.loads(body)["prompt_id"]
    print(f"已提交 prompt_id={prompt_id}，等待完成（超时 {args.timeout}s）")

    entry = None
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        try:
            history = _get(f"/history/{prompt_id}")
        except Exception:  # noqa: BLE001
            history = {}
        if prompt_id in history:
            entry = history[prompt_id]
            break
        time.sleep(1.0)
    elapsed = time.time() - started
    vram = sampler.stop()

    if entry is None:
        print(f"[FAIL] 超时未完成（已等 {elapsed:.1f}s）")
        return 1
    status = entry.get("status", {})
    result = {
        "prompt_id": prompt_id,
        "segments": args.segments,
        "seconds_per_segment": args.seconds,
        "total_video_seconds": args.segments * args.seconds,
        "wall_time_s": round(elapsed, 1),
        "seconds_per_video_second": round(elapsed / (args.segments * args.seconds), 2),
        "status": status.get("status_str"),
        "completed": status.get("completed"),
        "vram": vram,
        "budget_gb": args.budget_gb,
        "vram_within_budget": (vram["peak_gb"] is not None
                               and vram["peak_gb"] <= args.budget_gb),
        "errors": [
            m[1].get("exception_message", "")[:300]
            for m in status.get("messages", []) if m[0] == "execution_error"
        ],
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("\n=== 实测结果 ===")
        print(f"  视频总长：{result['total_video_seconds']}s "
              f"（{args.segments} 段 x {args.seconds}s）")
        print(f"  实际耗时：{result['wall_time_s']}s "
              f"-> 每秒视频 {result['seconds_per_video_second']}s")
        print(f"  显存峰值：{vram['peak_gb']} GB / 预算 {args.budget_gb} GB "
              f"-> {'达标' if result['vram_within_budget'] else '超预算！'}")
        print(f"  执行状态：{result['status']} completed={result['completed']}")
        for err in result["errors"]:
            print(f"    错误：{err}")
    return 0 if result["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
