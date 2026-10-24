#!/usr/bin/env python
"""生成「融合工作流」：把 Better ComfyUI 编排层接到三个真实导演台。

**为什么用脚本生成，而不是手写 JSON**

手写工作流的常见失败是**控件顺序错位**：ComfyUI 的 `widgets_values` 是**按位置**对应
`INPUT_TYPES` 的控件槽位的，少一项、多一项、顺序错一项，载入后参数就静默错位
（例如把 cfg 填进 seed）。这种错误不会报错，只会让结果莫名其妙。

因此本脚本：

1. 从**运行中的 ComfyUI** 读 `object_info` 取每个节点的真实 schema；
2. 按 schema 的键顺序**自动拼** `widgets_values`；
3. 生成后用 `validate_workflow.py` 与真实注册表交叉校验。

用法：
    python bettercomfyui_comfy/workflows/build_fusion_workflow.py            # 输出三个工作流
    python bettercomfyui_comfy/workflows/build_fusion_workflow.py --port 8188
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def fetch_object_info(port: int, node_types: list[str]) -> dict:
    """只取需要的节点 schema（全量 object_info 有 2MB，没必要）。"""
    out: dict = {}
    for t in node_types:
        url = f"http://127.0.0.1:{port}/object_info/{t}"
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = json.load(resp)
            if t in data:
                out[t] = data[t]
        except Exception as exc:  # noqa: BLE001
            print(f"  [!] 取不到 {t} 的 schema：{type(exc).__name__}: {exc}", file=sys.stderr)
    return out


def widget_slots(schema: dict) -> list[tuple[str, dict]]:
    """按顺序返回该节点所有**控件**（非连线）槽位。

    ## 分类规则（结构化，不靠手维护清单）

    ComfyUI 的 INPUT_TYPES 里，一个输入是**控件**还是**连线口**可以从声明形状直接判断：

    * `(类型, {...})` 且类型是 `INT/FLOAT/STRING/BOOLEAN` -> 控件；
    * `(类型, {...})` 且类型是**列表**（`["BDGROUP", {...}]` 里的取值列表）-> COMBO 控件；
    * `("IMAGE", {...})` 这类裸大写字符串 -> 连线口。

    ## 为什么要写这么细（一个真实的坑）

    第一版我手维护了一个 `link_types` 集合，其中**误把 `BDGROUP` 当成连线类型**。
    而 `BDGROUP` 的真实声明是 `["BDGROUP", {"default": "采样设置"}]` ——
    **它是个 COMBO 控件**（取值列表就是 `["BDGROUP"]`）。

    后果很严重：它被当成连线口跳过，于是**这个节点之后的所有控件位置整体前移一格**，
    `widgets_values` 全部错位。表现是提交给 ComfyUI 时报：

        Value 0.0 smaller than min of 0.2  (输入: 默认时长)
        Required input is missing: bd_grp_sample

    —— 也就是「参数静默错位」这类最难查的问题。

    所以现在**不再手维护清单**：只要类型是裸字符串且**没有 UI 专属选项键**，
    就当连线口。

    ## 事故 2：用 `len(t) > 3` 判断"是不是类型名"

    `"VAE"` 长度正好是 **3**，于是那个条件不成立，
    `video_vae` / `audio_vae` 被当成控件塞进 `widgets_values`，
    又一次错位，报 `Required input is missing: bd_grp_sample`。

    **结论：别用长度/命名约定去猜语义。**
    改用"选项里有没有 `default`/`min`/`max`/`step`/`multiline`"这种结构性证据 ——
    它对 `VAE`（3 字符）与 `MMX_DIR_GROUP`（13 字符）一视同仁。
    """
    scalar_widgets = {"INT", "FLOAT", "STRING", "BOOLEAN"}
    #: 出现这些键说明它是给用户调的 UI 控件（纯连线口不会有 default/min/max/step/multiline）
    ui_only_keys = {"default", "min", "max", "step", "multiline", "multiline_text"}
    out: list[tuple[str, dict]] = []
    spec = schema.get("input", {})
    for section in ("required", "optional"):
        for name, decl in (spec.get(section) or {}).items():
            # 声明在 JSON 里就是 list：`[类型, 选项字典]`
            if not (isinstance(decl, list) and decl):
                continue
            t = decl[0]
            # ⚠️ 选项字典必须真的取出来。第一版写成 `opts = {}`，
            # 于是 `default_for()` 永远拿不到 `default`，回落到 `FLOAT -> 0.0`，
            # 生成的工作流报了 `Value 0.0 smaller than min of 0.2`。
            # 教训：**别把"提取选项"当成可省略的细节**。
            opts = decl[1] if len(decl) > 1 and isinstance(decl[1], dict) else {}

            if isinstance(t, list):
                # 形状 1：`[[取值...], {...}]` -> COMBO
                out.append((name, {"kind": "combo", "choices": t, "opts": opts}))
            elif isinstance(t, str) and t in scalar_widgets:
                out.append((name, {"kind": "widget", "type": t, "opts": opts}))
            elif isinstance(t, str) and not (ui_only_keys & set(opts)):
                # 裸类型名且没有任何 UI 专属键 -> 连线口
                continue
            else:
                out.append((name, {"kind": "widget", "type": t, "opts": opts}))
    return out


def default_for(name: str, meta: dict, overrides: dict):
    if name in overrides:
        return overrides[name]
    if meta["kind"] == "combo":
        return meta["choices"][0]
    opts = meta.get("opts", {})
    if "default" in opts:
        return opts["default"]
    return {"INT": 0, "FLOAT": 0.0, "STRING": "", "BOOLEAN": False}.get(meta["type"], "")


def make_node(nid: int, ntype: str, schema: dict, pos: list[int],
              overrides: dict | None = None,
              outputs: int | None = None, title: str | None = None,
              size: list[int] | None = None) -> dict:
    overrides = overrides or {}
    slots = widget_slots(schema)
    values = [default_for(n, m, overrides) for n, m in slots]
    n_out = outputs if outputs is not None else len(schema.get("output", []) or [])
    return {
        "id": nid,
        "type": ntype,
        "pos": pos,
        "size": size or [300, 100 + 20 * len(slots)],
        "flags": {},
        "order": 0,
        "mode": 0,
        "inputs": [],
        "outputs": [{"name": n, "type": t, "links": [], "slot_index": i}
                    for i, (n, t) in enumerate(
                        zip(schema.get("output_name", []) or [],
                            schema.get("output", []) or []))],
        "properties": {"Node name for S&R": ntype},
        "widgets_values": values,
        **({"title": title} if title else {}),
    }


def build(*, port: int, outdir: Path) -> int:
    # ⚠️ 这里的每个类型都必须列全。漏一个的后果是「工作流静默不生成」——
    # 因为 `need_c <= set(schemas)` 不成立就跳过，而日志只说"节点不可用"，
    # 看不出其实是**我自己忘了把它加进这个拉取清单**。
    # 已经踩过两次：LoraLoaderModelOnly、BetterComfyUIOneDirector。
    # 守卫：tests/test_fusion_workflow.py 的 test_all_workflow_node_types_are_fetched。
    need = [
        "BetterComfyUIProject", "BetterComfyUIFusionGroups", "BetterComfyUIFusionIngest",
        "BetterComfyUITimelineText", "BetterComfyUIAssetSlots", "BetterComfyUIAssetMedia",
        "BetterComfyUIOneDirector",
        "MiniMaxH3Director", "MiniMaxH3SmartDirector", "MiniMaxH3ChainDirector",
        "UNETLoader", "VAELoader", "CLIPLoader", "LoraLoaderModelOnly", "LoadImage",
        "VHS_VideoCombine", "CreateVideo", "SaveVideo", "PreviewAny",
    ]
    print(f"从 127.0.0.1:{port} 读取节点 schema...")
    schemas = fetch_object_info(port, need)
    missing = [t for t in need if t not in schemas]
    if missing:
        print(f"  [!] 以下节点在本机不可用，相关工作流会跳过：{missing}", file=sys.stderr)

    written: list[Path] = []

    # ---------- 工作流 A：我的编排层 + SmartDirector ----------
    # 这是「用我的项目/素材/审核 + AI 分镜」的组合
    if {"BetterComfyUIProject", "BetterComfyUIFusionGroups", "MiniMaxH3Director"} <= set(schemas):
        n1 = make_node(1, "BetterComfyUIProject", schemas["BetterComfyUIProject"], [40, 60],
                       {"project_name": "我的短剧", "action": "打开或创建"}, outputs=2,
                       title="① 选项目（可先点 Queue 创建）")
        n2 = make_node(2, "BetterComfyUIFusionGroups", schemas["BetterComfyUIFusionGroups"],
                       [40, 320], {
                           "script_text": json.dumps({
                               "chapters": [{
                                   "title": "第一章",
                                   "assets": [{"id": "c1", "type": "CHARACTER", "name": "主角"}],
                                   "segments": [
                                       {"prompt": "主角走进画面，镜头缓慢推进", "duration_s": 5},
                                       {"prompt": "主角停下，转头看向镜头", "duration_s": 5},
                                   ],
                               }]
                           }, ensure_ascii=False, indent=2),
                           "family": "r2v 参考生视频",
                           "default_duration": 5.0,
                           "max_segments": 6,
                           "use_locked_assets": True,
                       }, outputs=4, title="② 剧本 → 导演组（只取已 LOCK 素材）")
        n3 = make_node(3, "MiniMaxH3Director", schemas["MiniMaxH3Director"], [520, 60],
                       {"task_type": "r2v", "frame_rate": 24.0, "width": 864, "height": 480},
                       title="③ 导演台引擎（真实生成在这里）")
        wf = {
            "id": "bcu-fusion-smart",
            "revision": 0,
            "last_node_id": 3,
            "last_link_id": 1,
            "nodes": [n1, n2, n3],
            "links": [[1, 2, 0, 3, 4, "MMX_DIR_GROUP"]],
            "groups": [],
            "config": {},
            "extra": {"ds": {"scale": 1.0, "offset": [0, 0]}},
            "version": 0.4,
        }
        # 连线：n2 的 groups(输出0) -> n3 的 r2v_groups（optional 第 1 项 = slot 1，因 i2v_groups 是 slot 0）
        n3["inputs"] = [
            {"name": "i2v_groups", "type": "MMX_DIR_GROUP", "link": None},
            {"name": "r2v_groups", "type": "MMX_DIR_GROUP", "link": 1},
        ]
        n2["outputs"][0]["links"] = [1]
        n2["outputs"][0]["slot_index"] = 0
        wf["links"] = [[1, 2, 0, 3, 1, "MMX_DIR_GROUP"]]
        p = outdir / "fusion_A_director_r2v.json"
        p.write_text(json.dumps(wf, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(p)
        print(f"  [OK] {p.name}（3 节点：项目 → 融合组 → 导演台引擎）")

    # ---------- 工作流 B：ChainDirector 一站式 + 回灌 ----------
    if {"BetterComfyUITimelineText", "BetterComfyUIFusionIngest", "MiniMaxH3ChainDirector"} <= set(schemas):
        t = make_node(10, "BetterComfyUITimelineText", schemas["BetterComfyUITimelineText"],
                      [40, 60], {
                          "global_setting": "冷蓝电影光，湿润反光，低饱和；主角始终在场，外观以参考图为准",
                          "segment_prompts": "主角走进画面，镜头缓慢推进\n主角停下，转头看向镜头",
                          "segment_seconds": 5.0,
                      }, outputs=3, title="① 时间轴文本（设定逐段前置，规避全局被丢弃）")
        c = make_node(11, "MiniMaxH3ChainDirector", schemas["MiniMaxH3ChainDirector"],
                      [480, 60], {"时间轴提示词": "", "总时长预设": "10秒",
                                  "分段方式": "5秒每段 (推荐)", "自动锚点": True},
                      outputs=4, title="② ChainDirector 一站式出片")
        g = make_node(12, "BetterComfyUIFusionIngest", schemas["BetterComfyUIFusionIngest"],
                      [480, 480], {"segment_index": 1, "fps": 24.0},
                      outputs=3, title="③ 回灌：产物 → 段版本 + 审核材料")
        wf2 = {
            "id": "bcu-fusion-chain",
            "revision": 0,
            "last_node_id": 12,
            "last_link_id": 3,
            "nodes": [t, c, g],
            "links": [],
            "groups": [],
            "config": {},
            "extra": {"ds": {"scale": 1.0, "offset": [0, 0]}},
            "version": 0.4,
        }

        # ⚠️ 目标槽位必须等于该节点 `inputs` 数组里的下标。
        # 第一版我写了 7 / 2 / 3（照抄别人的编号），而实际只声明了 1~2 个输入
        # —— 校验器一加"目标槽越界"检查就抓到了。
        # 正确做法：按数组下标来算，别猜。
        #
        # ⚠️ 另一个实测抓到的缺口：ChainDirector 有 5 个**必需**连线输入
        # （model_r2v / model_i2v / video_vae / audio_vae / clip）。
        # 只接时间轴是不完整的 —— 提交给 ComfyUI 会报
        # `required_input_missing: clip / model_i2v / ...`。
        # 这里补上 loader，让工作流**真的能提交**。
        unet_r = make_node(30, "UNETLoader", schemas["UNETLoader"], [1500, 60],
                           {"unet_name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
                            "weight_dtype": "default"},
                           outputs=1, title="② 扩散模型（ref2va）")
        unet_i = make_node(31, "UNETLoader", schemas["UNETLoader"], [1500, 260],
                           {"unet_name": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
                            "weight_dtype": "default"},
                           outputs=1, title="③ 扩散模型（fl2va）")
        clip_b = make_node(32, "CLIPLoader", schemas["CLIPLoader"], [1500, 460],
                           {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                            "type": "minimax", "device": "default"},
                           outputs=1, title="④ 文本编码器")
        vae_vb = make_node(33, "VAELoader", schemas["VAELoader"], [1500, 660],
                           {"vae_name": "minimax_h3_video_vae_fp16.safetensors"},
                           outputs=1, title="⑤ 视频 VAE")
        vae_ab = make_node(34, "VAELoader", schemas["VAELoader"], [1500, 860],
                           {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"},
                           outputs=1, title="⑥ 音频 VAE")

        c["inputs"] = [
            {"name": "时间轴提示词", "type": "STRING", "link": 1},
            {"name": "model_r2v", "type": "MODEL", "link": 4},
            {"name": "model_i2v", "type": "MODEL", "link": 5},
            {"name": "video_vae", "type": "VAE", "link": 6},
            {"name": "audio_vae", "type": "VAE", "link": 7},
            {"name": "clip", "type": "CLIP", "link": 8},
            {"name": "image_0", "type": "IMAGE", "link": 9},
        ]
        # image_0 是必需输入（ChainDirector 直接 kw["image_0"]）——
        # 缺了会在运行期 KeyError。这里接一张参考图占位，用户可替换成素材节点的输出。
        img0 = make_node(35, "LoadImage", schemas["LoadImage"], [1500, 1060],
                         {}, outputs=2, title="⑦ 首帧参考图（必需，可换成素材节点）")
        t["outputs"][0]["links"] = [1]
        unet_r["outputs"][0]["links"] = [4]
        unet_i["outputs"][0]["links"] = [5]
        vae_vb["outputs"][0]["links"] = [6]
        vae_ab["outputs"][0]["links"] = [7]
        clip_b["outputs"][0]["links"] = [8]
        img0["outputs"][0]["links"] = [9]
        g["inputs"] = [
            {"name": "images", "type": "IMAGE", "link": 2},
            {"name": "audio", "type": "AUDIO", "link": 3},
        ]
        wf2["nodes"] = [t, c, g, unet_r, unet_i, clip_b, vae_vb, vae_ab, img0]
        wf2["last_node_id"] = 35
        wf2["last_link_id"] = 9
        wf2["links"] = [
            [1, 10, 0, 11, 0, "STRING"],      # t.时间轴提示词 -> c.时间轴提示词
            [2, 11, 0, 12, 0, "IMAGE"],       # c.画面帧 -> g.images
            [3, 11, 1, 12, 1, "AUDIO"],       # c.音频   -> g.audio
            [4, 30, 0, 11, 1, "MODEL"],       # unet_r2v -> c.model_r2v
            [5, 31, 0, 11, 2, "MODEL"],       # unet_fl2v -> c.model_i2v
            [6, 33, 0, 11, 3, "VAE"],         # video vae -> c.video_vae
            [7, 34, 0, 11, 4, "VAE"],         # audio vae -> c.audio_vae
            [8, 32, 0, 11, 5, "CLIP"],        # clip -> c.clip
            [9, 35, 0, 11, 6, "IMAGE"],       # 首帧参考图 -> c.image_0
        ]
        c["outputs"][0]["links"] = [2]
        c["outputs"][1]["links"] = [3]
        p2 = outdir / "fusion_B_chain_director.json"
        p2.write_text(json.dumps(wf2, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(p2)
        print(f"  [OK] {p2.name}（9 节点：时间轴 + 模型加载器 → ChainDirector → 回灌）")

    # ---------- 工作流 C：SmartDirector + Director 全链路（带模型加载器）----------
    # 这是**最能直接用**的一个：把 SmartDirector 的 AI 分镜接到 Director 执行引擎，
    # 并把 5 个必需输入（model/model_r2v 等）用标准 loader 填上。
    # 用户只需换掉模型文件名即可。
    need_c = {"MiniMaxH3SmartDirector", "MiniMaxH3Director",
              "UNETLoader", "VAELoader", "CLIPLoader", "LoraLoaderModelOnly",
              "CreateVideo", "SaveVideo"}
    if need_c <= set(schemas):
        smart = make_node(20, "MiniMaxH3SmartDirector", schemas["MiniMaxH3SmartDirector"],
                          [40, 60], {}, title="① SmartDirector：中文剧本 → AI 分镜 + 导演组")
        unet = make_node(21, "UNETLoader", schemas["UNETLoader"], [40, 400],
                         {"unet_name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
                          "weight_dtype": "default"},
                         outputs=1, title="② 扩散模型（ref2va，参考生视频）")
        lora = make_node(22, "LoraLoaderModelOnly", schemas["LoraLoaderModelOnly"],
                         [360, 400],
                         {"lora_name": ("minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_"
                                        "comfy_resized_avg_rank_21_bf16.safetensors"),
                          "strength_model": 0.75},
                         outputs=1, title="③ turbo LoRA（加速）")
        clip = make_node(23, "CLIPLoader", schemas["CLIPLoader"], [40, 620],
                         {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                          "type": "minimax", "device": "default"},
                         outputs=1, title="④ 文本编码器")
        vae_v = make_node(24, "VAELoader", schemas["VAELoader"], [40, 820],
                          {"vae_name": "minimax_h3_video_vae_fp16.safetensors"},
                          outputs=1, title="⑤ 视频 VAE")
        vae_a = make_node(25, "VAELoader", schemas["VAELoader"], [360, 820],
                          {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"},
                          outputs=1, title="⑥ 音频 VAE")
        director = make_node(26, "MiniMaxH3Director", schemas["MiniMaxH3Director"],
                             [700, 60],
                             {"task_type": "r2v", "frame_rate": 24.0,
                              "width": 864, "height": 480},
                             title="⑦ Director 执行引擎（真实出片）")
        mk = make_node(27, "CreateVideo", schemas["CreateVideo"], [1040, 60], {},
                       outputs=1, title="⑧ 合成视频")
        sv = make_node(28, "SaveVideo", schemas["SaveVideo"], [1040, 300],
                       {"filename_prefix": "BetterComfyUI/fusion_C", "format": "auto",
                        "codec": "auto"},
                       outputs=1, title="⑨ 保存成片")
        wf3 = {
            "id": "bcu-fusion-smart-director",
            "revision": 0,
            "last_node_id": 28,
            "last_link_id": 0,
            "nodes": [smart, unet, lora, clip, vae_v, vae_a, director, mk, sv],
            "links": [],
            "groups": [],
            "config": {},
            "extra": {"ds": {"scale": 0.85, "offset": [0, 0]}},
            "version": 0.4,
        }
        links: list[list] = []

        def connect(lid, src, sslot, dst_node, dst_name, dst_type):
            """把 src 的输出接到 dst 节点的具名输入。"""
            inputs = dst_node.setdefault("inputs", [])
            slot = len(inputs)
            inputs.append({"name": dst_name, "type": dst_type, "link": lid})
            src["outputs"][sslot]["links"] = (src["outputs"][sslot].get("links") or []) + [lid]
            return [lid, src["id"], sslot, dst_node["id"], slot, dst_type]

        # 模型链：UNETLoader -> LoRA -> Director.model
        links.append(connect(1, unet, 0, lora, "model", "MODEL"))
        links.append(connect(2, lora, 0, director, "model", "MODEL"))
        links.append(connect(3, clip, 0, director, "clip", "CLIP"))
        links.append(connect(4, vae_v, 0, director, "video_vae", "VAE"))
        links.append(connect(5, vae_a, 0, director, "audio_vae", "VAE"))
        # SmartDirector 的导演组 -> Director.r2v_groups（**这就是融合接缝**）
        links.append(connect(6, smart, 0, director, "r2v_groups", "MMX_DIR_GROUP"))
        # 出片 -> CreateVideo -> SaveVideo
        links.append(connect(7, director, 0, mk, "images", "IMAGE"))
        links.append(connect(8, director, 1, mk, "audio", "AUDIO"))
        links.append(connect(9, director, 2, mk, "fps", "FLOAT"))
        links.append(connect(10, mk, 0, sv, "video", "VIDEO"))
        wf3["links"] = links
        wf3["last_link_id"] = len(links)
        p3 = outdir / "fusion_C_smart_director_full.json"
        p3.write_text(json.dumps(wf3, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(p3)
        print(f"  [OK] {p3.name}（9 节点：SmartDirector → Director 全链路，含模型加载器）")

    # ---------- 工作流 D：一体化导演台（**主推的基础工作流**）----------
    # 这是「一个节点 + 导演台」的最小闭环：
    #
    #   一体化导演台（素材+多段提示词+一致性 -> 整条导演组）
    #        └── r2v_groups ──> MiniMaxH3Director（巨人，真实出片）
    #                                └── images/audio ──> CreateVideo ──> SaveVideo
    #
    # 用户只需：① 改模型文件名 ② 在「一体化导演台」里写剧本与素材清单。
    need_d = {"BetterComfyUIOneDirector", "MiniMaxH3Director",
              "UNETLoader", "VAELoader", "CLIPLoader", "LoraLoaderModelOnly",
              "CreateVideo", "SaveVideo"}
    if need_d <= set(schemas):
        one = make_node(40, "BetterComfyUIOneDirector",
                        schemas["BetterComfyUIOneDirector"], [40, 60], {},
                        outputs=4, title="★ 一体化导演台（剧本 + 素材 → 整条导演组）")
        d_unet = make_node(41, "UNETLoader", schemas["UNETLoader"], [560, 60],
                           {"unet_name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
                            "weight_dtype": "default"},
                           outputs=1, title="① 扩散模型（ref2va）")
        d_lora = make_node(42, "LoraLoaderModelOnly", schemas["LoraLoaderModelOnly"],
                           [840, 60],
                           {"lora_name": ("minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_"
                                          "comfy_resized_avg_rank_21_bf16.safetensors"),
                            "strength_model": 0.75},
                           outputs=1, title="② turbo LoRA")
        d_clip = make_node(43, "CLIPLoader", schemas["CLIPLoader"], [560, 260],
                           {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                            "type": "minimax", "device": "default"},
                           outputs=1, title="③ 文本编码器")
        d_vv = make_node(44, "VAELoader", schemas["VAELoader"], [560, 460],
                         {"vae_name": "minimax_h3_video_vae_fp16.safetensors"},
                         outputs=1, title="④ 视频 VAE")
        d_av = make_node(45, "VAELoader", schemas["VAELoader"], [840, 460],
                         {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"},
                         outputs=1, title="⑤ 音频 VAE")
        d_dir = make_node(46, "MiniMaxH3Director", schemas["MiniMaxH3Director"],
                          [1120, 60], {"task_type": "r2v", "frame_rate": 24.0,
                                       "width": 864, "height": 480},
                          title="⑥ MiniMaxH3Director（巨人：真实出片）")
        d_mk = make_node(47, "CreateVideo", schemas["CreateVideo"], [1500, 60], {},
                         outputs=1, title="⑦ 合成视频")
        d_sv = make_node(48, "SaveVideo", schemas["SaveVideo"], [1500, 300],
                         {"filename_prefix": "BetterComfyUI/一体化", "format": "auto",
                          "codec": "auto"},
                         outputs=1, title="⑧ 保存成片")
        wf4 = {
            "id": "bcu-one-director",
            "revision": 0,
            "last_node_id": 48,
            "last_link_id": 0,
            "nodes": [one, d_unet, d_lora, d_clip, d_vv, d_av, d_dir, d_mk, d_sv],
            "links": [],
            "groups": [],
            "config": {},
            "extra": {"ds": {"scale": 0.8, "offset": [0, 0]}},
            "version": 0.4,
        }
        dlinks: list[list] = []

        def dconnect(lid, src, sslot, dst_node, dst_name, dst_type):
            inputs = dst_node.setdefault("inputs", [])
            slot = len(inputs)
            inputs.append({"name": dst_name, "type": dst_type, "link": lid})
            src["outputs"][sslot]["links"] = (
                (src["outputs"][sslot].get("links") or []) + [lid])
            return [lid, src["id"], sslot, dst_node["id"], slot, dst_type]

        dlinks.append(dconnect(1, d_unet, 0, d_lora, "model", "MODEL"))
        dlinks.append(dconnect(2, d_lora, 0, d_dir, "model", "MODEL"))
        dlinks.append(dconnect(3, d_clip, 0, d_dir, "clip", "CLIP"))
        dlinks.append(dconnect(4, d_vv, 0, d_dir, "video_vae", "VAE"))
        dlinks.append(dconnect(5, d_av, 0, d_dir, "audio_vae", "VAE"))
        # ★ 唯一的融合点：我的一体化导演台 -> 导演台的真实接缝
        dlinks.append(dconnect(6, one, 0, d_dir, "r2v_groups", "MMX_DIR_GROUP"))
        dlinks.append(dconnect(7, d_dir, 0, d_mk, "images", "IMAGE"))
        dlinks.append(dconnect(8, d_dir, 1, d_mk, "audio", "AUDIO"))
        dlinks.append(dconnect(9, d_dir, 2, d_mk, "fps", "FLOAT"))
        dlinks.append(dconnect(10, d_mk, 0, d_sv, "video", "VIDEO"))
        wf4["links"] = dlinks
        wf4["last_link_id"] = len(dlinks)
        p4 = outdir / "one_director_demo.json"
        p4.write_text(json.dumps(wf4, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(p4)
        print(f"  [OK] {p4.name}（9 节点：★一体化导演台 → Director 全链路，主推）")

    if not written:
        print("[!] 没有生成任何工作流：本机缺少所需节点。", file=sys.stderr)
        return 2
    print(f"\n共生成 {len(written)} 个工作流到 {outdir}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="生成融合工作流（从运行中的 ComfyUI 读 schema）")
    ap.add_argument("--port", type=int, default=8188)
    ap.add_argument("--outdir", default=str(HERE))
    args = ap.parse_args()
    return build(port=args.port, outdir=Path(args.outdir))


if __name__ == "__main__":
    raise SystemExit(main())
