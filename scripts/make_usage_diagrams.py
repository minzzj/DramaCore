# -*- coding: utf-8 -*-
"""生成「一页纸使用说明」用的自绘流程图（清晰中文，普通用户友好）。

为什么不用 ComfyUI 截图：节点在缩放下 LiteGraph 会隐藏标题文字，普通用户看不清。
这里用 PIL 画简化流程卡（每套工作流一张），配合一页纸说明使用。

用法：python -B scripts/make_usage_diagrams.py
输出：docs/images/流程图_*.png
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "images"
OUT.mkdir(parents=True, exist_ok=True)

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]


def font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


W, H = 1800, 1000
BG = (248, 249, 251)
INK = (32, 37, 46)
MUTED = (95, 105, 120)
ACCENT = (46, 108, 200)
OK = (28, 138, 92)
WARN = (196, 120, 24)


def _wrap(text: str, f: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur); cur = ""; continue
        if f.getlength(cur + ch) > max_w and cur:
            lines.append(cur); cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def box(draw, xy, title, body, *, color=ACCENT, title_size=34, body_size=24, max_w=None):
    x0, y0, x1, y1 = xy
    draw.rounded_rectangle(xy, radius=18, fill=(255, 255, 255), outline=color, width=4)
    draw.rounded_rectangle((x0, y0, x1, y0 + 62), radius=18, fill=color)
    draw.rectangle((x0, y0 + 40, x1, y0 + 62), fill=color)
    ft, fb = font(title_size), font(body_size)
    draw.text((x0 + 20, y0 + 12), title, font=ft, fill=(255, 255, 255))
    y = y0 + 82
    for para in _wrap(body, fb, (max_w or (x1 - x0 - 40))):
        draw.text((x0 + 20, y), para, font=fb, fill=INK)
        y += body_size + 10
    return y


def arrow(draw, x0, y0, x1, y1, *, color=ACCENT, width=6):
    draw.line([(x0, y0), (x1, y1)], fill=color, width=width)
    import math
    ang = math.atan2(y1 - y0, x1 - x0)
    size = 18
    p1 = (x1 - size * math.cos(ang - 0.45), y1 - size * math.sin(ang - 0.45))
    p2 = (x1 - size * math.cos(ang + 0.45), y1 - size * math.sin(ang + 0.45))
    draw.polygon([(x1, y1), p1, p2], fill=color)


def new_canvas(title: str, subtitle: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    draw.text((60, 46), title, font=font(52), fill=INK)
    draw.text((60, 116), subtitle, font=font(26), fill=MUTED)
    draw.line([(60, 170), (W - 60, 170)], fill=(214, 220, 230), width=3)
    return img, draw


def flow_one() -> None:
    img, d = new_canvas("① 剧本 → 全部内容（本机可跑，0 次 API）",
                        "工作流：00_【总流程】剧本→全部内容（本机可跑）  |  普通用户 4 步")
    ys = 300
    b1 = (60, ys, 460, ys + 260)
    b2 = (530, ys, 1030, ys + 260)
    b3 = (1100, ys, 1440, ys + 260)
    b4 = (1510, ys, 1745, ys + 260)
    box(d, b1, "第 1 步", "打开工作流\n00_【总流程】\n剧本→全部内容\n（本机可跑）", color=ACCENT)
    box(d, b2, "第 2 步", "在「一体化总控台」的\n「剧本」框里粘贴整段剧本\n（人工输入）", color=ACCENT)
    box(d, b3, "第 3 步", "点右上角「运行/队列」\n（无需 API Key；\n「使用占位结果」保持开）", color=ACCENT)
    box(d, b4, "第 4 步", "看右侧 5 个预览：\n分镜脚本 / 提示词JSON /\n素材清单 / 审计 / API状态", color=OK)
    for a, b in ((b1, b2), (b2, b3), (b3, b4)):
        arrow(d, a[2], (a[1] + a[3]) // 2, b[0], (b[1] + b[3]) // 2)
    d.text((60, 650), "看到的结果（全部内容）：", font=font(32), fill=INK)
    items = [
        "分镜脚本（--- 分隔，可直接接导演台）",
        "提示词 JSON：E1 角色 / E2 场景 / E3 物品 / E4 分镜 / E5 视频段 + director_groups 导演组",
        "素材清单：本次提示词已登记进素材库（占位模式不写库；真实 API 模式会写）",
        "审计报告：API 调用次数、模式、素材数、段数、警告",
        "导演组结构：在提示词 JSON 的 director_groups 里",
    ]
    y = 705
    for it in items:
        d.text((80, y), "•", font=font(30), fill=ACCENT)
        d.text((110, y), it, font=font(26), fill=INK)
        y += 50
    d.text((60, 940), "提示：看不到工作流时按 Ctrl+F5 刷新；本机无 GPU，出片请用 GPU 总流程。",
           font=font(24), fill=WARN)
    img.save(OUT / "流程图_01_剧本到全部内容.png")


def flow_two() -> None:
    img, d = new_canvas("② 段视频 → 剪好成片 + 视频审核（本机可跑，不需要 GPU）",
                        "工作流：00_【总流程】段视频→剪好成片（本机可跑）  |  三步出成片")
    ys = 290
    b1 = (60, ys, 560, ys + 280)
    b2 = (640, ys, 1180, ys + 280)
    b3 = (1260, ys, 1740, ys + 280)
    box(d, b1, "第 1 步", "把段视频路径粘进\n「成片剪辑 → 段视频路径列表」\n每行一个（按播放顺序）\n只有一段就填一行", color=ACCENT)
    box(d, b2, "第 2 步", "点「运行/队列」\n它会：按顺序拼接 → 封装编码\n→ 自检完整性/剪辑性/质量\n→ 成片路径自动接视频审核", color=ACCENT)
    box(d, b3, "第 3 步", "看右侧 8 个预览：\n成片路径 / 完整性JSON / 质量JSON\n剪辑审计 / 审核结论 / 段审核 /\n整片审核 / 审核审计", color=OK)
    arrow(d, b1[2], (b1[1] + b1[3]) // 2, b2[0], (b2[1] + b2[3]) // 2)
    arrow(d, b2[2], (b2[1] + b2[3]) // 2, b3[0], (b3[1] + b3[3]) // 2)

    d.text((60, 640), "机器可算的证据（在 JSON/审计里）：", font=font(32), fill=INK)
    rows = [
        ("完整性", "成片时长 vs Σ段时长（容差 2%）、计划段数、音轨有无"),
        ("剪辑性", "黑帧 / 冻帧 / 静音段检测（判断是否真的拼接剪辑过）"),
        ("质量", "本地启发式四维：叙事流畅 / 音画同步 / 节奏合理 / 无明显跳变"),
        ("占位检测", "码率/画面过低 → 明确标「疑似占位，不是真实生成」"),
        ("视频审核", "段级 R_seg + 相邻边界 + 整片 R_full，给出结论与需返工段"),
    ]
    y = 700
    for name, text in rows:
        d.text((80, y), "•", font=font(30), fill=ACCENT)
        d.text((110, y), name + "：", font=font(26), fill=OK)
        d.text((280, y), text, font=font(26), fill=INK)
        y += 54
    d.text((60, 950), "诚实边界：转场特效未实现（不暴露、不静默忽略）；画质高低最终仍需人工抽检。",
           font=font(24), fill=WARN)
    img.save(OUT / "流程图_02_段视频到成片.png")


def flow_three() -> None:
    img, d = new_canvas("③ 剧本 → 成片（GPU 全链，需要显卡与模型）",
                        "工作流：00_【总流程】剧本→成片（GPU）  |  分两段跑，控制显存 <32GB")
    stages = [
        ("① 配置/剧本", "面板填 API Key\n大节点粘剧本", ACCENT),
        ("② 素材生成", "取任务 → 图像模型\n→ 入库 → 素材审核", ACCENT),
        ("③ 导演台出片", "H3 权重 + 已 LOCK 素材\n→ 视频段", ACCENT),
        ("④ 成片剪辑", "拼接 → 封装\n→ 完整性/质量自检", OK),
        ("⑤ 视频审核", "R_seg + 边界\n+ R_full + 策略", OK),
        ("⑥ 结果", "预览：提示词/审核/\n成片路径/审计", OK),
    ]
    x = 60
    y = 330
    w, h, gap = 250, 300, 40
    prev = None
    for title, body, color in stages:
        bx = (x, y, x + w, y + h)
        box(d, bx, title, body, color=color, title_size=26, body_size=21)
        if prev:
            arrow(d, prev[2], (prev[1] + prev[3]) // 2, bx[0], (bx[1] + bx[3]) // 2)
        prev = bx
        x += w + gap
        if x + w > W - 40:
            x = 60
            y += h + 90
            prev = None
    d.text((60, 760), "显存红线（生产 5090-32G）：", font=font(32), fill=INK)
    notes = [
        "图像模型 + H3 同时驻留约 46GB → 必分两段：先只跑 ①② 组（Ctrl+M 静音③④⑤），再跑 ③④⑤ 组；",
        "H3 分阶段峰值理论 24.94GB；生产先 720p、VAE 用 tiled、batch=1、启动加 --reserve-vram 1.5；",
        "全程看 nvidia-smi 峰值，超过 30.5GB 立即停；>30.5GB 判失败并回退。",
        "第二阶段会把这些步骤做成自动阶段化，不再手动静音。",
    ]
    yy = 820
    for n in notes:
        d.text((80, yy), "•", font=font(28), fill=WARN)
        d.text((110, yy), n, font=font(24), fill=INK)
        yy += 44
    img.save(OUT / "流程图_03_剧本到成片GPU.png")


def main() -> int:
    flow_one()
    flow_two()
    flow_three()
    print("[OK] 已生成：")
    for p in sorted(OUT.glob("流程图_*.png")):
        print("  ", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
