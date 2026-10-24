"""尝试一次**最小的真实生成**（CPU 模式）。

## ⛔ 不要在这台机器上再运行本脚本

2026-10-17 这台机器蓝屏三次，转储给出的错误码**三次完全一致**：

    BugCheckCode = 0x0000001A   （MEMORY_MANAGEMENT）
    参数           0x42001

时间 4:02 / 8:24 / 8:31 —— 与「内存被压到极限」的时刻吻合。
而 10 月**没有任何 WHEA 硬件错误事件**（最近一次在 2 月），
所以不是硬件或过热问题，是**内存耗尽**。

原因很直接：MiniMax H3 最小也让

    ref2va 扩散模型 19.53 GB + 文本编码器 14.61 GB = 34.14 GB

同时驻留，而本机物理内存只有 **15.75 GB**。
本脚本实测把它逼进了疯狂换页：可用内存 0.48 GB、
进程完全死锁（0 CPU / 0 磁盘 / 1 线程）。

**要真实出片，请换到 ≥24GB 显存 且 ≥64GB 内存的机器。**
本脚本保留是为了记录这次实测的方法与结论，**不是给人跑的**。

---

## 为什么值得冒险（原始动机，保留作记录）

整晚唯一没验证的就是「真实出片」。本机是 CPU + 4GB 显存，看起来不可能 ——
但**"看起来不可能"和"试过不行"是两件事**。权重已经下齐（55+ GB），
内存 15.7 GB + 49 GB 页面文件，值得真跑一次看结果。

无论成功还是失败，结论都比"我没试"强：

* **成功** → 整个 goal 的最后一项验证完成，而且是端到端真的；
* **失败** → 拿到**确切的失败原因**（OOM？算子不支持？超时？），
  写进文档比"本机跑不了"精确得多。

## 做法

最小图：两个 UNETLoader + CLIPLoader + 两个 VAELoader → Director(t2v, 最小尺寸)。
分辨率取 256x256、帧数取最小合法值，把计算量压到最低。
不接任何可选输入，先验证"导演台能不能自己跑起来"。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8188"


def post(path: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return exc.code, {"raw": raw[:800]}


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=120) as resp:
        return json.load(resp)


DIR = "MiniMaxH3Director"
graph = {
    "1": {"class_type": "UNETLoader",
          "inputs": {"unet_name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
                     "weight_dtype": "default"}},
    "2": {"class_type": "CLIPLoader",
          "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                     "type": "minimax", "device": "default"}},
    "3": {"class_type": "VAELoader",
          "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
    "4": {"class_type": "VAELoader",
          "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
    "10": {"class_type": DIR, "inputs": {
        "model": ["1", 0],
        "video_vae": ["3", 0],
        "audio_vae": ["4", 0],
        "clip": ["2", 0],
        "task_type": "t2v — 文生视频(Text to Video)",
        "global_prompt": "a red cube on a table, static shot",
        "bd_grp_sample": "采样设置",
        "cfg": 1.0,
        "seed": 12345,
        "frame_rate": 24.0,
        "width": 256,
        "height": 256,
        "ref_max_size": 256,
        "total_frames": 5,
        "timeline_data": "",
        "steps": 1,
        "clear_vram_between_segments": True,
    }},
    # ⚠️ 必须有输出节点，否则 ComfyUI 直接拒绝：
    #    {"error": {"type": "prompt_no_outputs", "message": "Prompt has no outputs"}}
    # （实测踩到：第一版没接输出，连执行都没开始）
    "20": {"class_type": "SaveImage", "inputs": {
        "images": ["10", 0], "filename_prefix": "BetterComfyUI/realtest"}},
    "21": {"class_type": "SaveImage", "inputs": {
        "images": ["10", 4], "filename_prefix": "BetterComfyUI/realtest_src"}},
}

print("=" * 66)
print("最小真实生成尝试（CPU）")
print("=" * 66)
print(f"节点数: {len(graph)}；分辨率 256x256；total_frames=5；steps=1")
print("提交中……（加载权重可能需要几分钟）")

code, body = post("/prompt", {"prompt": graph, "client_id": "bcui-realrun"})
print(f"HTTP {code}")
if code != 200:
    print("提交被拒绝：")
    print(json.dumps(body, ensure_ascii=False, indent=2)[:1500])
    sys.exit(2)

pid = body.get("prompt_id")
print(f"已入队 prompt_id = {pid}")
print("开始轮询（最多等 60 分钟；CPU 上会非常慢）……")

t0 = time.time()
deadline = t0 + 3600
last_report = 0.0
final = None
while time.time() < deadline:
    time.sleep(10)
    try:
        hist = get(f"/history/{pid}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [轮询异常] {type(exc).__name__}: {exc}")
        continue
    entry = hist.get(pid)
    if entry is not None:
        final = entry
        break
    elapsed = time.time() - t0
    if elapsed - last_report >= 60:
        last_report = elapsed
        try:
            q = get("/queue")
            running = len(q.get("queue_running") or [])
            pending = len(q.get("queue_pending") or [])
            print(f"  [{elapsed/60:.1f} 分钟] 仍在执行（running={running} pending={pending}）")
        except Exception:  # noqa: BLE001
            print(f"  [{elapsed/60:.1f} 分钟] 仍在执行")

if final is None:
    print()
    print("结论：**超时未完成**（60 分钟）。CPU 上这个模型太慢，未能验证出片。")
    sys.exit(3)

elapsed = time.time() - t0
status = final.get("status") or {}
print()
print("=" * 66)
print(f"耗时 {elapsed/60:.1f} 分钟")
print(f"status_str = {status.get('status_str')}  completed = {status.get('completed')}")
msgs = status.get("messages") or []
for m in msgs[-14:]:
    print("  ", json.dumps(m, ensure_ascii=False)[:300])
outs = final.get("outputs") or {}
print(f"输出节点: {list(outs)}")
for nid, o in outs.items():
    for k, v in o.items():
        print(f"  节点{nid}.{k} => {json.dumps(v, ensure_ascii=False)[:300]}")
print("=" * 66)

if status.get("status_str") == "success" and status.get("completed"):
    print("结论：**真实执行成功**（导出台确实跑通了）")
    sys.exit(0)
print("结论：**执行失败** —— 上面的 messages 就是确切原因。")
sys.exit(1)
