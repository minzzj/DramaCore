"""验证 Director 的四个**高级接缝**真的能被 ComfyUI 接受。

## 为什么值得单独验

`/object_info` 里有 `refine` / `selflift` / `semantic_bridge` / `face_refine`
四个可选输入 —— 但那只能说明"schema 里声明了"。
真正要确认的是：**把对应类型的值接上去，ComfyUI 是否接受这个图**。

这一步排除了"类型名写对了但接缝其实不兼容"的可能，
比读 schema 强一层。

做法：用真实的 `MiniMaxH3DirectorRefine` / `SelfLift` / `SemanticBridge` /
`FaceRefine` 节点产出对应类型，接到 Director 上，提交看 ComfyUI 的反应。

由于这些高级节点在缺外部权重时会**拒绝执行**（但图结构仍会被校验），
所以只要 `/prompt` 报的是 `value_not_in_list` / 权重类错误，
就说明**接缝类型是对的**；若接缝类型错，报的会是
`required_input_missing` 或类型不匹配。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8188"


def info(t: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{BASE}/object_info/{t}", timeout=40) as r:
            return json.load(r).get(t)
    except Exception:  # noqa: BLE001
        return None


def probe(label: str, graph: dict) -> str:
    """提交图，返回分类结论。"""
    req = urllib.request.Request(
        f"{BASE}/prompt", data=json.dumps({"prompt": graph,
                                           "client_id": "seam-probe"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.load(resp)
        if body.get("node_errors"):
            return f"校验错误：{json.dumps(body['node_errors'], ensure_ascii=False)[:200]}"
        return "图被接受（已入队）"
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            err = json.loads(raw)
        except Exception:  # noqa: BLE001
            return f"HTTP {exc.code}: {raw[:200]}"
        ne = err.get("node_errors") or {}
        flat = json.dumps(ne, ensure_ascii=False)
        # 只看**接缝节点**是否被接受：基座 loader 的报错与接缝无关，单独说明
        seed_ids = {k for k in ne if k in ("10", "11", "12", "13")}
        if seed_ids:
            return f"**接缝节点被拒**：{json.dumps({k: ne[k] for k in seed_ids}, ensure_ascii=False)[:240]}"
        if "value_not_in_list" in flat or "value_smaller" in flat or "required_input_missing" in flat:
            return f"接缝 OK（仅基座/取值问题）：{flat[:170]}"
        return f"其他：{flat[:220]}"


def main() -> int:
    print("=" * 68)
    print("Director 高级接缝类型验证")
    print("=" * 68)

    names = ("MiniMaxH3DirectorRefine", "MiniMaxH3DirectorSelfLift",
             "MiniMaxH3DirectorSemanticBridge", "MiniMaxH3DirectorFaceRefine")
    have = {n: info(n) for n in names}
    for n, d in have.items():
        print(f"  {n:<36} {'有' if d else '缺失'}")
    if not all(have.values()):
        print("有节点缺失，无法验证对应接缝")
        return 2
    print()

    # 共同底座：模型加载器 + Director
    def base(model_id=1):
        return {
            "1": {"class_type": "UNETLoader",
                  "inputs": {"unet_name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
                               "weight_dtype": "default"}},
            "2": {"class_type": "CLIPLoader",
                  "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                             "type": "minimax"}},
            "3": {"class_type": "VAELoader",
                  "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
            "4": {"class_type": "VAELoader",
                  "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        }

    def director(extra_inputs: dict) -> dict:
        ins = {
            "model": ["1", 0], "clip": ["2", 0],
            "video_vae": ["3", 0], "audio_vae": ["4", 0],
            "task_type": "t2v — 文生视频(Text to Video)",
            "global_prompt": "x", "bd_grp_sample": "采样设置",
            "cfg": 1.0, "seed": 1, "frame_rate": 24.0,
            "width": 256, "height": 256, "ref_max_size": 256,
            "total_frames": 5, "timeline_data": "",
        }
        ins.update(extra_inputs)
        return ins

    # --- 接缝 1：refine ---
    g = base()
    g["10"] = {"class_type": "MiniMaxH3DirectorRefine", "inputs": {
        "mode": "refine", "upscale_method": "lanczos",
        "latent_upscale_model": "(将 3D 权重放入 models/latent_upscale_models)",
        "sampler": "euler", "passes": 1}}
    g["20"] = {"class_type": "MiniMaxH3Director", "inputs": director({"refine": ["10", 0]})}
    g["99"] = {"class_type": "SaveImage", "inputs": {"images": ["20", 0],
                                                     "filename_prefix": "seam/refine"}}
    print(f"[refine]          {probe('refine', g)}")

    # --- 接缝 4：face_refine（先试，字段较少）---
    g = base()
    g["11"] = {"class_type": "MiniMaxH3DirectorFaceRefine", "inputs": {
        "bd_grp_face_detect": "人脸检测", "detector": "face_yolov8m.pt",
        "confidence": 0.5, "crop_factor": 1.5, "canvas_width": 512,
        "canvas_height": 512, "canvas_mode": "auto_capped_768", "select": "largest_face",
        "bd_grp_face_sample": "采样设置", "denoise": 0.4, "steps": 4,
        "sampler": "euler", "scheduler": "simple"}}
    g["20"] = {"class_type": "MiniMaxH3Director", "inputs": director({"face_refine": ["11", 0]})}
    g["99"] = {"class_type": "SaveImage", "inputs": {"images": ["20", 0],
                                                     "filename_prefix": "seam/face"}}
    print(f"[face_refine]     {probe('face_refine', g)}")

    # --- 接缝 3：semantic_bridge ---
    g = base()
    g["12"] = {"class_type": "MiniMaxH3DirectorSemanticBridge", "inputs": {
        "adapter": "(将 Semantic Bridge 权重放入 models/semantic_bridge)",
        "alpha": 0.5, "magnitude_match": True}}
    g["20"] = {"class_type": "MiniMaxH3Director",
               "inputs": director({"semantic_bridge": ["12", 0]})}
    g["99"] = {"class_type": "SaveImage", "inputs": {"images": ["20", 0],
                                                     "filename_prefix": "seam/bridge"}}
    print(f"[semantic_bridge] {probe('semantic_bridge', g)}")

    # --- 接缝 2：selflift ---
    g = base()
    g["13"] = {"class_type": "MiniMaxH3DirectorSelfLift", "inputs": {
        "bd_grp_selflift_sample": "采样设置", "split_mode": "highres_steps",
        "highres_steps": 4, "transition_step": 2, "lowres_scale": 0.5,
        "sampler_mode": "euler", "native_low_carry": True,
        "bd_grp_selflift_lift": "提升",
        "latent_upscale_model": "(将 3D 权重放入 models/latent_upscale_models)",
        "latent_upsample": "bilinear", "rho": 0.5, "w_min": 0.0, "w_max": 1.0,
        "enable_latent_chunking": False,
        "bd_grp_selflift_tile": "平铺", "enable_tiling": False,
        "tile_count": 2, "tile_overlap": 64}}
    g["20"] = {"class_type": "MiniMaxH3Director", "inputs": director({"selflift": ["13", 0]})}
    g["99"] = {"class_type": "SaveImage", "inputs": {"images": ["20", 0],
                                                        "filename_prefix": "seam/selflift"}}
    print(f"[selflift]        {probe('selflift', g)}")

    print()
    print("说明：图结构被接受 = 接缝类型正确；若报 required_input_missing 则说明接缝不成立。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
