# -*- coding: utf-8 -*-
"""构建「总流程」工作流（ROUND28，第一阶段：人工输入一段剧本 → 尽可能完整输出）。

两套（普通用户只需认这两套）：
  1. `00_【总流程】剧本→全部内容（本机可跑）.json`
     本机无 GPU/无 Key 也能看：剧本进 → 提示词/素材清单/导演组结构/审计 全部可见。
  2. `00_【总流程】剧本→成片（GPU）.json`
     剧本 → 一次 API 调用 → 素材生成 → 入库 → 素材审核 → 导演台出片 → 视频审核 → 结果。

设计原则（用户要求）：
  * 节点尽量统一、数量少、普通用户看得懂；
  * 每套工作流左侧放一块「使用说明」（PrimitiveStringMultiline 文本），旁边用分组标题标阶段；
  * 参数尽量收在「一体化总控台」大节点里；说明区解释每一步。

用法（ComfyUI 8188 运行）：python -B scripts/build_master_flow_workflows.py
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_one_node_workflows as B  # noqa: E402

PROJECT_ID = B.PROJECT_ID
PREVIEW_INPUT = "source"
NOTE_NODE = "PrimitiveStringMultiline"

LOCAL_NAME = "00_【总流程】剧本→全部内容（本机可跑）"
GPU_NAME = "00_【总流程】剧本→成片（GPU）"
FINAL_NAME = "00_【总流程】段视频→剪好成片（本机可跑）"

SCRIPT_TEXT = B.SCRIPT_TEXT

NOTE_LOCAL = """【这是一套什么工作流】

第一阶段目标：你粘贴一段剧本，它把「全部内容」一次性产出来给你看：
  1. 分镜脚本（可直接接导演台）
  2. 提示词 JSON（角色/场景/物品/分镜 + 视频段，标准 JSON）
  3. 素材清单（占位模式不写库；真实 API 模式会登记：新建/复用/疑似）
  4. 审计报告（API 调用次数、模式、素材/段数、警告）
  5. 导演组结构（在提示词 JSON 的 director_groups 里）

【项目怎么填（不用手抄 project_id）】
「一体化总控台 → 项目」默认填的是**项目显示名「我的短剧」**：
  · 第一次运行：自动新建这个项目（审计里会写明）；
  · 想用已有项目：把它改成那个项目的显示名，或填它的 32 位 project_id；
  · 填错 32 位 id 不会静默新建，而是报错并跳过素材库（审计里可见）。

【普通用户三步】

第 1 步：在大节点「一体化总控台 → 剧本」里粘贴你的剧本（人工输入，整段贴进去）。
第 2 步：看「使用占位结果」开关：
   · 开（默认）= 不用 API 密钥也能看完整结构（占位内容，首行会标"占位"）
   · 关 = 真实调用 DeepSeek 生成（需要先在右侧面板配置 API 密钥）
第 3 步：点「队列」。右边 5 个预览框就是全部结果。

【本机提醒】
本机没有 GPU，不能出片；出片请到 GPU 机器用
《00_【总流程】剧本→成片（GPU）》。
"""

NOTE_GPU = """【这是一套什么工作流】

第一阶段总目标：人工输入一段剧本 → 自动产出全部内容直到成片与审核结论。
链路（从左到右）：
  ① 配置/剧本 → ② 素材生成→入库→审核 → ③ 导演台出片
  → ④ 成片剪辑（拼接+封装+自检） → ⑤ 视频审核（段级+整片） → ⑥ 结果

【项目怎么填（不用手抄 project_id）】
各节点的「项目」默认填的是**项目显示名「我的短剧」**：
  · 第一次运行：自动新建这个项目（审计里会写明）；
  · 想用已有项目：改成那个项目的显示名，或填它的 32 位 project_id；
  · 填错 32 位 id 不会静默新建，而是明确报错（避免往幽灵项目里写素材）。

【使用前准备（只做一次）】
  1. 右侧「Better ComfyUI」面板 → API 配置 → 填 DeepSeek 密钥并保存；
  2. 本工作流里「图像模型」节点（CheckpointLoaderSimple）**必须**选一个你已安装的图像模型；
     没有图像模型时该分支会明确失败（不会静默跳过）；
  3. 视频模型（UNET / CLIP / VAE 三处）选 MiniMax H3 对应权重（已预填常见文件名）；
  4. GPU 要求：显存 ≥24GB、内存 ≥64GB（H3 权重约 34GB 需同时驻留）。

【每次使用（三步）】
  第 1 步：在「一体化总控台 → 剧本」粘贴整段剧本（人工输入）。
  第 2 步：点「队列」：它会
      · 只调用一次 API，产出角色/场景/物品/分镜/视频提示词，并自动登记进素材库；
      · 取下一个待生成素材 → 用你的图像模型生成 → 入库为新版本 → 素材审核 → 通过写 LOCK；
      · 已 LOCK 素材全量挂到每一段 → 导演台出片 → 保存视频；
      · 视频审核在下一轮手动填入成片路径后执行（见下节）。
  第 3 步：看右侧预览框（提示词JSON、素材审核、视频审核结论与审计）。

【成片剪辑 + 视频审核怎么用（逻辑图 SG6：SPLICE→ENC→R_full）】
  1) 出片后，从 SaveVideo 的输出目录复制段视频绝对路径（多段就每行一个）；
     也可以：路径列表留空 + 勾选「自动发现最新视频」→ 自动扫描最近 N 分钟的 mp4；
  2) 填到「成片剪辑 → 段视频路径列表」：它会按顺序拼接 → 封装编码 → 自检
     （时长/段数/黑帧/冻帧/静音/画质评分/占位检测），并输出成片路径；
  3) 成片路径自动接到「视频审核 → 视频文件路径」，点队列即可做段级+整片审核。
注意：首次点队列时路径为空，节点会明确报错要求填路径（不是静默跳过）。
本机想单独测试剪辑/审核：用《00_【总流程】段视频→剪好成片（本机可跑）》。

【显存提示（理论测试结论，重要）】
图像模型 + 视频模型 H3 若同时驻留，约需 46GB 显存 —— 32GB 显卡会 OOM。
现阶段请「分两段跑」（很简单）：
  A. 先只跑 ①② 组：框选③④组节点按 Ctrl+M 静音，点队列 → 素材生成并审核；
  B. 再只跑 ③④ 组：把①②组静音、取消③④组静音，点队列 → 出片并审核。
第二阶段会做成自动分阶段调度（不用手动切）。

【普通用户记住一句】
剧本写进大节点，点队列；结果全在右边预览框。本机请用「本机可跑」那套。
"""



def _add(wf: dict, info: dict, node_type: str, widgets: list, *, pos, size) -> dict:
    """add_node + 按 /object_info 预置 outputs（多输出节点不会因连线顺序错位）。"""
    node = B.add_node(wf, node_type, widgets, pos=pos, size=size)
    spec = info.get(node_type) or {}
    out_types = spec.get("output") or []
    out_names = spec.get("output_name") or []
    node["outputs"] = [
        {"name": (out_names[i] if i < len(out_names) else f"out{i}"),
         "type": out_types[i], "links": []}
        for i in range(len(out_types))
    ]
    return node


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------



def _note(wf: dict, info: dict, text: str, pos, size) -> dict:
    values = B.node_defaults(info, NOTE_NODE)
    node = _add(wf, info, NOTE_NODE, values, pos=pos, size=size)
    node["title"] = "📖 使用说明（先读我）"
    idx = B.node_widget_index(info, NOTE_NODE)
    node["widgets_values"][idx["value"]] = text
    return node


def _preview(wf: dict, info: dict, src: dict, slot: int, label: str, pos,
             size=(520, 260)) -> dict:
    view = _add(wf, info, "PreviewAny", [], pos=pos, size=size)
    view["title"] = label
    B.add_link(wf, src, slot, view, PREVIEW_INPUT, "STRING")
    return view


def _group(wf: dict, title: str, bounding, color: str = "#3f789e") -> None:
    wf.setdefault("groups", []).append({
        "id": len(wf.get("groups") or []),
        "title": title,
        "bounding": list(bounding),
        "color": color,
        "font_size": 22,
        "flags": {},
    })


# ---------------------------------------------------------------------------
# 工作流 1：本机可跑
# ---------------------------------------------------------------------------


def build_local(info: dict) -> dict:
    wf = {"last_node_id": 0, "last_link_id": 0, "nodes": [], "links": [],
          "groups": [], "config": {}, "extra": {}, "version": 0.4}
    note = _note(wf, info, NOTE_LOCAL, (-2600, -500), (620, 980))
    settings = _add(wf, info, "BetterComfyUISettings",
                         B.node_defaults(info, "BetterComfyUISettings"),
                          pos=(-2400, 620), size=(380, 180))
    values = B.node_defaults(info, "BetterComfyUIEverything")
    big = _add(wf, info, "BetterComfyUIEverything", values, pos=(-1750, 500), size=(560, 720))
    B.set_widget(big, info, "剧本", SCRIPT_TEXT)
    B.set_widget(big, info, "使用占位结果", True)  # 本机无 Key 也能看全内容
    B.set_widget(big, info, "写入素材库", True)
    B.set_widget(big, info, "项目", PROJECT_ID)
    B.add_link(wf, settings, 0, big, "api_key", "STRING")
    B.add_link(wf, settings, 1, big, "model", "STRING")
    B.add_link(wf, settings, 2, big, "api_base", "STRING")

    _preview(wf, info, big, 1, "① 分镜脚本（视频提示词）", (-950, 300))
    _preview(wf, info, big, 2, "② 提示词JSON（E1–E5 + 导演组结构）", (-950, 600), (560, 300))
    _preview(wf, info, big, 3, "③ 素材清单（新建/复用/LOCK）", (-950, 950))
    _preview(wf, info, big, 4, "④ 审计报告（API 调用次数/模式/警告）", (-950, 1250))
    _preview(wf, info, settings, 3, "⑤ API 配置状态", (-2400, 900))

    _group(wf, "📖 使用说明（先读）", (-2640, -540), "#8a8a8a")
    _group(wf, "② 项目与异步配置", (-2440, 580), "#3f789e")
    _group(wf, "③ 剧本输入（大节点：一次调用产出全部内容）", (-1790, 460), "#a1309b")
    _group(wf, "④ 全部内容输出（看这里）", (-990, 260), "#59a130")
    return wf


# ---------------------------------------------------------------------------
# 工作流 2：GPU 全链
# ---------------------------------------------------------------------------


def _copy_weight_chain(wf: dict, info: dict, base_wf: dict, x0: int = -3900, y0: int = 1250) -> dict:
    """从旧 W0 里复制权重链节点（保留真实文件名），返回新 id → 节点字典。"""
    id_map: dict[int, int] = {}
    created: dict[str, dict] = {}
    positions = {
        41: (x0, y0), 42: (x0 + 420, y0),
        43: (x0 + 420, y0 + 220), 44: (x0 + 420, y0 + 400), 45: (x0 + 420, y0 + 560),
        46: (x0 + 900, y0 - 100), 47: (x0 + 1500, y0 - 100), 48: (x0 + 1980, y0 - 100),
    }
    names = {41: "UNETLoader", 42: "LoraLoaderModelOnly", 43: "CLIPLoader",
             44: "VAELoader", 45: "VAELoader", 46: "MiniMaxH3Director",
             47: "CreateVideo", 48: "SaveVideo"}
    by_old = {n["id"]: n for n in base_wf["nodes"]}
    for old_id in (41, 42, 43, 44, 45, 46, 47, 48):
        src = by_old[old_id]
        node = _add(wf, info, src["type"], copy.deepcopy(src.get("widgets_values") or []),
                          pos=positions[old_id], size=copy.deepcopy(src.get("size") or [420, 120]))
        id_map[old_id] = node["id"]
        created[names[old_id]] = node
    # 链接（按旧 W0 的真实连线）
    B.add_link(wf, created["UNETLoader"], 0, created["LoraLoaderModelOnly"], "model", "MODEL")
    B.add_link(wf, created["LoraLoaderModelOnly"], 0, created["MiniMaxH3Director"], "model", "MODEL")
    B.add_link(wf, created["CLIPLoader"], 0, created["MiniMaxH3Director"], "clip", "CLIP")
    vaes = [n for n in wf["nodes"] if n["type"] == "VAELoader"]
    B.add_link(wf, vaes[0], 0, created["MiniMaxH3Director"], "video_vae", "VAE")
    B.add_link(wf, vaes[1], 0, created["MiniMaxH3Director"], "audio_vae", "VAE")
    B.add_link(wf, created["MiniMaxH3Director"], 0, created["CreateVideo"], "images", "IMAGE")
    B.add_link(wf, created["MiniMaxH3Director"], 1, created["CreateVideo"], "audio", "AUDIO")
    B.add_link(wf, created["MiniMaxH3Director"], 2, created["CreateVideo"], "fps", "FLOAT")
    B.add_link(wf, created["CreateVideo"], 0, created["SaveVideo"], "video", "VIDEO")
    return created


def build_gpu(info: dict, base_wf: dict) -> dict:
    wf = {"last_node_id": 0, "last_link_id": 0, "nodes": [], "links": [],
          "groups": [], "config": {}, "extra": {}, "version": 0.4}
    _note(wf, info, NOTE_GPU, (-4700, -900), (680, 1500))

    # ---- ① 配置 + 剧本（大节点） ----
    settings = _add(wf, info, "BetterComfyUISettings",
                         B.node_defaults(info, "BetterComfyUISettings"),
                          pos=(-4550, 300), size=(380, 180))
    values = B.node_defaults(info, "BetterComfyUIEverything")
    big = _add(wf, info, "BetterComfyUIEverything", values, pos=(-4000, 300), size=(560, 720))
    B.set_widget(big, info, "剧本", SCRIPT_TEXT)
    B.set_widget(big, info, "使用占位结果", False)
    B.set_widget(big, info, "写入素材库", True)
    B.set_widget(big, info, "项目", PROJECT_ID)
    B.add_link(wf, settings, 0, big, "api_key", "STRING")
    B.add_link(wf, settings, 1, big, "model", "STRING")
    B.add_link(wf, settings, 2, big, "api_base", "STRING")

    # ---- ② 素材生成 → 入库 → 审核 ----
    gen = _add(wf, info, "BetterComfyUIAssetGen",
                     B.node_defaults(info, "BetterComfyUIAssetGen"),
                     pos=(-3250, 300), size=(480, 340))
    B.set_widget(gen, info, "项目", PROJECT_ID)
    ckpt = _add(wf, info, "CheckpointLoaderSimple",
                      B.node_defaults(info, "CheckpointLoaderSimple"),
                      pos=(-3250, 900), size=(420, 120))
    pos_enc = _add(wf, info, "CLIPTextEncode", B.node_defaults(info, "CLIPTextEncode"),
                         pos=(-2700, 200), size=(460, 240))
    B.add_link(wf, gen, 0, pos_enc, "text", "STRING")
    B.add_link(wf, ckpt, 1, pos_enc, "clip", "CLIP")
    neg_enc = _add(wf, info, "CLIPTextEncode", B.node_defaults(info, "CLIPTextEncode"),
                         pos=(-2700, 520), size=(460, 160))
    B.set_widget(neg_enc, info, "text", "低质量，模糊，变形，水印")
    B.add_link(wf, ckpt, 1, neg_enc, "clip", "CLIP")
    latent = _add(wf, info, "EmptyLatentImage", B.node_defaults(info, "EmptyLatentImage"),
                        pos=(-2700, 760), size=(420, 120))
    sampler = _add(wf, info, "KSampler", B.node_defaults(info, "KSampler"),
                         pos=(-2200, 200), size=(420, 420))
    B.add_link(wf, ckpt, 0, sampler, "model", "MODEL")
    B.add_link(wf, pos_enc, 0, sampler, "positive", "CONDITIONING")
    B.add_link(wf, neg_enc, 0, sampler, "negative", "CONDITIONING")
    B.add_link(wf, latent, 0, sampler, "latent_image", "LATENT")
    decode = _add(wf, info, "VAEDecode", B.node_defaults(info, "VAEDecode"),
                        pos=(-1760, 200), size=(380, 120))
    B.add_link(wf, sampler, 0, decode, "samples", "LATENT")
    B.add_link(wf, ckpt, 2, decode, "vae", "VAE")
    ingest = _add(wf, info, "BetterComfyUIAssetIngest",
                        B.node_defaults(info, "BetterComfyUIAssetIngest"),
                        pos=(-1330, 200), size=(480, 340))
    B.set_widget(ingest, info, "项目", PROJECT_ID)
    B.add_link(wf, gen, 2, ingest, "素材ID", "STRING")
    B.add_link(wf, decode, 0, ingest, "images", "IMAGE")
    review = _add(wf, info, "BetterComfyUIAssetReview",
                        B.node_defaults(info, "BetterComfyUIAssetReview"),
                        pos=(-800, 200), size=(480, 320))
    B.set_widget(review, info, "project_id", PROJECT_ID)
    B.set_widget(review, info, "action", "模型审核（本地启发式）")
    B.set_widget(review, info, "asset_id", "")
    B.set_widget(review, info, "version", "")
    B.set_widget(review, info, "operator", "workflow")

    # ---- ③ 导演台 → 出片 ----
    chain = _copy_weight_chain(wf, info, base_wf)
    director = chain["MiniMaxH3Director"]
    B.add_link(wf, big, 0, director, "r2v_groups", "MMX_DIR_GROUP")

    # ---- ④ 成片剪辑（SPLICE + ENC + 自检） ----
    finalize = _add(wf, info, "BetterComfyUIFinalize",
                    B.node_defaults(info, "BetterComfyUIFinalize"),
                    pos=(-2600, 1500), size=(520, 460))
    B.set_widget(finalize, info, "项目", PROJECT_ID)
    B.set_widget(finalize, info, "输出文件名", "")
    B.set_widget(finalize, info, "编码CRF", 18)
    B.set_widget(finalize, info, "完整性检查", "快速抽样（≤10分钟）")

    # ---- ⑤ 视频审核 ----
    video_review = _add(wf, info, "BetterComfyUIVideoReview",
                              B.node_defaults(info, "BetterComfyUIVideoReview"),
                              pos=(-1950, 1500), size=(560, 560))
    B.add_link(wf, finalize, 0, video_review, "视频文件路径", "STRING")
    B.set_widget(video_review, info, "项目", PROJECT_ID)
    B.set_widget(video_review, info, "每段时长_秒", 10.0)
    B.set_widget(video_review, info, "审核范围", "段级 + 整片（推荐）")
    B.set_widget(video_review, info, "评分来源", "本地启发式（真实计算）")
    B.set_widget(video_review, info, "整片失败策略", "按项目配置")

    # ---- ⑥ 结果预览 ----
    _preview(wf, info, big, 2, "① 提示词JSON（E1–E5 + 导演组）", (-260, 200), (540, 300))
    _preview(wf, info, big, 4, "② 总控台审计（API 调用次数）", (-260, 560))
    _preview(wf, info, review, 3, "③ 素材审核（LOCK/评分/CNT1）", (-260, 860), (540, 300))
    _preview(wf, info, finalize, 3, "④ 成片剪辑审计（完整性/剪辑性/质量）", (-260, 1200), (560, 340))
    _preview(wf, info, video_review, 0, "⑤ 视频审核结论", (-1300, 1500))
    _preview(wf, info, video_review, 4, "⑥ 视频审核审计（R_seg/R_full）", (-1300, 1800), (560, 320))

    _group(wf, "📖 使用说明（先读）", (-4740, -940), "#8a8a8a")
    _group(wf, "① 配置与剧本（人工粘贴剧本）", (-4590, 260), "#3f789e")
    _group(wf, "② 素材生成 → 入库 → 审核", (-3290, 160), "#a1309b")
    _group(wf, "③ 导演台 → 出片（视频模型）", (-3940, 1100), "#b58b2a")
    _group(wf, "④ 成片剪辑（拼接+封装+自检）", (-2640, 1460), "#8a5a2a")
    _group(wf, "⑤ 视频审核（段级 + 整片）", (-1990, 1460), "#59a130")
    _group(wf, "⑥ 结果（看这里）", (-300, 160), "#2f6f5f")
    return wf


NOTE_FINAL = """【这是一套什么工作流（本机可跑，不需要 GPU）】

作用：把**已经生成好的段视频**按顺序拼接、封装成最终成片，并自检：
  · 完整性：成片时长 vs Σ段时长、计划段数核对、是否有音轨
  · 剪辑性：黑帧 / 冻帧 / 静音段
  · 质量：本地启发式评分（叙事流畅/音画同步/节奏合理/无明显跳变）
  · 占位检测：码率/画面过低会明确标"疑似占位，不是真实生成"
  · 然后把成片路径自动交给「视频审核」做段级 R_seg + 整片 R_full

【普通用户三步】
  1) 「成片剪辑 → 段视频路径列表」：每行填一个段视频绝对路径（按播放顺序）；只有一段就填一行；
  2) 可选：填计划段数/每段时长，让完整性核对更严格；
  3) 点「队列」。右边预览：成片路径 / 完整性JSON / 质量JSON / 剪辑审计 / 视频审核结论。

【注意】
  · 本工作流不做"生成"，只做"剪辑+封装+审核"；段视频要么来自 GPU 机器出片，要么是任意 mp4 测试文件；
  · 转场特效未实现，不暴露、不静默忽略；
  · 画质高低的最终判断需人工抽检，本工作流只给机器可算的证据。
"""


def build_finalize_local(info: dict) -> dict:
    """本机可跑：段视频 → 剪辑/封装 → 自检 → 视频审核。"""
    wf = {"last_node_id": 0, "last_link_id": 0, "nodes": [], "links": [],
          "groups": [], "config": {}, "extra": {}, "version": 0.4}
    _note(wf, info, NOTE_FINAL, (-1900, -500), (640, 900))
    finalize = _add(wf, info, "BetterComfyUIFinalize",
                    B.node_defaults(info, "BetterComfyUIFinalize"),
                    pos=(-1700, 520), size=(540, 500))
    B.set_widget(finalize, info, "项目", PROJECT_ID)
    B.set_widget(finalize, info, "段视频路径列表", "")
    B.set_widget(finalize, info, "输出文件名", "")
    B.set_widget(finalize, info, "编码CRF", 18)
    B.set_widget(finalize, info, "完整性检查", "完整扫描")
    review = _add(wf, info, "BetterComfyUIVideoReview",
                  B.node_defaults(info, "BetterComfyUIVideoReview"),
                  pos=(-1050, 520), size=(560, 560))
    B.set_widget(review, info, "项目", PROJECT_ID)
    B.set_widget(review, info, "每段时长_秒", 10.0)
    B.set_widget(review, info, "审核范围", "段级 + 整片（推荐）")
    B.set_widget(review, info, "评分来源", "本地启发式（真实计算）")
    B.set_widget(review, info, "整片失败策略", "按项目配置")
    B.add_link(wf, finalize, 0, review, "视频文件路径", "STRING")

    _preview(wf, info, finalize, 0, "① 成片路径", (-380, 200))
    _preview(wf, info, finalize, 1, "② 完整性JSON（时长/段数/黑帧/冻帧/静音/占位）", (-380, 500), (560, 300))
    _preview(wf, info, finalize, 2, "③ 质量JSON（本地启发式四维）", (-380, 850), (560, 260))
    _preview(wf, info, finalize, 3, "④ 剪辑审计（人话版）", (-380, 1160), (560, 340))
    _preview(wf, info, review, 0, "⑤ 视频审核结论", (420, 520))
    _preview(wf, info, review, 1, "⑥ 段审核JSON（R1a/CNT2/边界）", (420, 880), (560, 300))
    _preview(wf, info, review, 2, "⑦ 整片审核JSON（R_full/策略）", (420, 1240), (560, 320))
    _preview(wf, info, review, 4, "⑧ 视频审核审计（R_seg/R_full）", (420, 1620), (560, 320))
    _group(wf, "📖 使用说明（先读）", (-1940, -540), "#8a8a8a")
    _group(wf, "① 成片剪辑（拼接+封装+自检）", (-1740, 460), "#8a5a2a")
    _group(wf, "② 视频审核（段级+整片）", (-1090, 460), "#59a130")
    _group(wf, "③ 结果（看这里）", (-420, 160), "#2f6f5f")
    return wf


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def main() -> int:
    info = B.object_info()
    missing = [n for n in (NOTE_NODE, "BetterComfyUIEverything", "BetterComfyUIAssetGen",
                           "BetterComfyUIAssetIngest", "BetterComfyUIAssetReview",
                           "BetterComfyUIVideoReview", "BetterComfyUIFinalize",
                           "MiniMaxH3Director", "CheckpointLoaderSimple", "PreviewAny")
               if n not in info]
    if missing:
        print(f"[FAIL] ComfyUI 缺少节点：{missing}（先同步插件并重启）")
        return 1
    base_wf = json.loads(
        (B.SRC_DIR / "W0_完整逻辑图全流程.json").read_text(encoding="utf-8"))
    # 新版 W0 已不含权重链：从备份来源读旧结构不可靠 → 直接按 object_info 默认值构建，
    # 模型文件名用已知值覆盖（与旧 W0 一致）。
    B.install(info, LOCAL_NAME, build_local(info))
    B.install(info, FINAL_NAME, build_finalize_local(info))
    gpu = build_gpu(info, base_wf)
    _apply_known_weights(gpu)
    B.install(info, GPU_NAME, gpu)
    return 0


def _apply_known_weights(wf: dict) -> None:
    """给权重节点填入项目已验证的 H3 文件名（用户可在界面改）。"""
    known = {
        "UNETLoader": ["minimax_h3_ref2va_pruned_int8_convrot.safetensors", "default"],
        "LoraLoaderModelOnly": [
            "minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy_resized_avg_rank_21_bf16.safetensors", 0.75],
        "CLIPLoader": ["qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "minimax", "default"],
    }
    vae_files = ["minimax_h3_video_vae_fp16.safetensors", "minimax_h3_audio_vae_fp32.safetensors"]
    vae_index = 0
    for node in wf["nodes"]:
        if node["type"] in known:
            node["widgets_values"] = list(known[node["type"]])
        elif node["type"] == "VAELoader":
            node["widgets_values"] = [vae_files[min(vae_index, 1)]]
            vae_index += 1


if __name__ == "__main__":
    raise SystemExit(main())
