"""风格标签库（图一 SG1 `C6`「风格标签库预置供勾选」）。

## 修的是什么缺口

`C6` 此前是「部分实现」：项目里只有 **5 套系统提示词模板**
（`SYSTEM_PROMPT_PRESETS`），那是「提示词结构」模板，
**不是**「风格标签库」—— 用户想勾选"赛博朋克 / 水墨 / 胶片"这类画风词时无处可点。

## 怎么用

节点里的「统一风格」输入支持 `@标签名` 写法：

    统一风格 = "@写实电影感 @冷蓝电影光"   -> 展开成完整风格描述
    统一风格 = "古风, 暖色调"               -> 不是 @ 开头的原样保留

也就是说标签库是**可选增强**：不写 `@` 就完全是原来的自由文本，
老工作流一个字都不用改。

## 为什么用 `@标签名` 而不是新增 COMBO 输入

加一个新输入会**改节点 schema**，已有工作流里保存的连线/参数会错位。
而 `@` 展开是纯文本层的糖：schema 不变、老工作流不受影响、用户还能混写自由文本。

## 标签内容原则

* **正向描述**，不用否定词（扩散模型对否定词常常无效）；
* 每个标签只描述**视觉/氛围**，不涉及剧情与人物（那是剧本的事）；
* 中文为主（方案要求默认中文输出）。
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "STYLE_LIBRARY",
    "STYLE_NAMES",
    "expand_style_tokens",
    "available_styles",
    "style_tooltip",
]

#: 预置风格标签库：名称 -> 描述。
#:
#: 名称要短、可记、可直接当 `@` 标记用；描述要能直接拼进视频提示词。
STYLE_LIBRARY: dict[str, str] = {
    "写实电影感": "电影级写实画面，真实光影与材质，自然肤色，浅景深",
    "冷蓝电影光": "冷蓝色调，低饱和，青蓝阴影，冷白高光",
    "暖阳黄昏": "暖金色黄昏光，长影，柔和逆光，暖色调",
    "赛博朋克": "赛博朋克都市，霓虹灯牌，高对比色彩，雨夜反光地面",
    "水墨": "中国水墨画风格，留白，墨色晕染，纸本质感",
    "国风古装": "中国古典服饰与建筑，含蓄典雅，青绿山水色调",
    "日系动画": "日式赛璐璐动画风格，干净线条，明亮通透",
    "厚涂插画": "厚涂笔触，强体积感，油画质感",
    "胶片颗粒": "胶片质感，细颗粒，轻微暗角，复古色彩",
    "黑白纪实": "黑白摄影，高对比，纪实风格，粗粝质感",
    "科技未来": "未来科技感，冷色高光，金属与玻璃材质，干净构图",
    "纪录片": "纪录片风格，自然光，手持微晃，真实环境音",
    "广告大片": "广告级画面，精致布光，高饱和，浅景深产品感",
    "恐怖惊悚": "阴暗压抑，低照度，冷色光源，构图失衡感",
    "梦幻童话": "柔和梦幻，高光泛光，粉紫渐变，柔和景深",
    "硬朗动作": "硬派动作片，强对比，快速运镜，冲击力构图",
}

#: 标签名列表（供 tooltip / 界面提示用）。
STYLE_NAMES: tuple[str, ...] = tuple(STYLE_LIBRARY)

#: 匹配 `@标签名`。标签名可能含中文、字母、数字、下划线、连字符。
_TOKEN_PATTERN = re.compile(r"@([\u4e00-\u9fffA-Za-z0-9_\-]{1,32})")


def available_styles() -> list[str]:
    """返回全部可用的风格标签名（顺序稳定）。"""
    return list(STYLE_NAMES)


def expand_style_tokens(text: Any, *, separator: str = "，") -> str:
    """把文本里的 `@标签名` 展开成对应的风格描述。

    * **未知标签**保持原样（`@乱写的` 不会被吞掉 —— 用户能看出自己写错了）；
    * 非 `@` 开头的普通文本原样保留；
    * 同一个标签重复出现只展开一次（避免提示词里同一句话重复三遍）；
    * 展开后的描述用 `separator` 连接，接在原文之后。

    ## 为什么未知标签不报错

    风格只是锦上添花，不该因为写错一个标签就让整个生成失败。
    但**也不能静默丢掉** —— 保留原文，用户回看提示词时能发现 `@乱写的` 没被展开。
    """
    raw = str(text or "")
    if not raw.strip():
        return raw

    expanded: list[str] = []
    seen: set[str] = set()
    for match in _TOKEN_PATTERN.finditer(raw):
        name = match.group(1)
        description = STYLE_LIBRARY.get(name)
        if description is None:
            continue                      # 未知标签：原文保留在 raw 里
        if name in seen:
            continue
        seen.add(name)
        expanded.append(description)

    if not expanded:
        return raw
    # 原文里的 `@标签名` 替换成空格清理一下，再追加展开后的描述
    cleaned = _TOKEN_PATTERN.sub(lambda m: "" if m.group(1) in STYLE_LIBRARY
                                 else m.group(0), raw)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip().strip("，,")
    tail = separator.join(expanded)
    return f"{cleaned}{separator}{tail}" if cleaned else tail


def style_tooltip() -> str:
    """给节点输入用的 tooltip：列出可勾选的标签名。"""
    return (
        "全片统一的视觉风格描述。\n"
        "支持风格标签：在文本里写 `@标签名` 会自动展开成完整描述，例如\n"
        "  @写实电影感 @冷蓝电影光\n"
        f"可用标签（{len(STYLE_NAMES)} 个）：\n  "
        + "、".join(STYLE_NAMES)
        + "\n也可以直接写自由文本，不写 @ 就按原文使用。"
    )
